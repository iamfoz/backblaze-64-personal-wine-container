#!/usr/bin/env python3
"""Shared data layer for bb-monitor and bb-monitor-web.

Both dashboards read the same things: /proc, the cgroup files, and Backblaze's
own bzdata logs and XML. Everything that does the reading lives here so the two
front ends cannot drift apart. They did drift: the web dashboard gained overall
backup progress, an ETA, files remaining and round-trip time while the terminal
one kept the original narrower view, because each carried its own copy of this
code. A feature added here now appears in both or neither.

No curses, no HTTP, no subprocesses. Pure reads plus one netlink socket for the
kernel's own round-trip-time figures. The front ends own presentation and
nothing else.

Imported by path rather than installed as a package, since both callers live in
/usr/local/bin and this is deliberately not a general-purpose library:

    sys.path.insert(0, "/usr/local/lib/bb-monitor")
    import bbdata
"""
import calendar, html, ipaddress, os, re, socket, struct, threading, time, unicodedata
from collections import deque

# ---- config (identical to bb-monitor) ------------------------------------
BZ = "/config/wine/dosdevices/c:/ProgramData/Backblaze/bzdata"
LOGDIR = BZ + "/bzlogs/bztransmit"
BZSTAT_TOTAL = BZ + "/bzreports/bzstat_totalbackup.xml"
BZSTAT_REMAIN = BZ + "/bzreports/bzstat_remainingbackup.xml"
TDIR = BZ + "/bzthread"
PROC = "/proc"
NETDEV = "/proc/net/dev"
MEMINFO = "/proc/meminfo"
CG2 = "/sys/fs/cgroup"
CG1 = "/sys/fs/cgroup/memory"
INT = 2.0
MPLOG = "/tmp/bb_multipart_bzdone.log"

# Files the client keeps its own state in. Everything here was found by surveying
# a live bzdata tree; see scratch/bzdata-survey-findings.md for what each holds.
OVERVIEW = BZ + "/overviewstatus.xml"
LFDIR = BZ + "/bzbackup/bzdatacenter/bzcurrentlargefile"
FLISTS = BZ + "/bzfilelists"
PERFXML = BZ + "/bzreports/bzperf_measured_upload.xml"
RPTS = BZ + "/bzreports"
BZINFO = BZ + "/bzinfo.xml"

_logged = set()
_inflight = {}
_recent = []
_sess = 0
_rate_ema = 0.0
# Throughput history for the ETA: (bytes, seconds) per genuinely-completed
# transfer (a whole file, or a whole multi-part file once all its parts are
# in). Weighting by actual bytes/duration over the last several completions
# is far steadier than the live 2s network-counter sample.
_completed_hist = deque(maxlen=10)

# ---- round-trip time to the storage pod ----------------------------------
# Read from the kernel, not measured by us. Every established TCP connection
# already carries a smoothed RTT the kernel maintains from its own ACK timing,
# and NETLINK_SOCK_DIAG exposes it for sockets this process does not own. That
# is the same interface `ss -ti` uses.
#
# So the figure comes from the upload connections themselves rather than from a
# probe of our own: nothing extra is sent to Backblaze, there is no traffic when
# idle, and the number describes the actual upload path rather than a separate
# handshake that merely resembles it. An earlier version opened its own TCP
# connection every ten seconds; this replaced it.
#
# Contributed in concept by rogman; reworked to read the kernel instead.
BZDC_SYNCHOSTINFO = BZ + "/bzreports/bzdc_synchostinfo.xml"

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLM_F_REQUEST = 0x001
NLM_F_DUMP = 0x300
NLMSG_ERROR = 0x2
NLMSG_DONE = 0x3
INET_DIAG_INFO = 2
TCP_ESTABLISHED = 1
# struct tcp_info: eight u8 fields, then u32s. tcpi_rtt is the 16th u32, so it
# sits at 8 + 15*4. Microseconds, smoothed. This offset has been stable across
# the whole 3.x/4.x/5.x/6.x series because fields are only ever appended.
TCPI_RTT_OFFSET = 68

# Below this, a connection to a public address is not crossing the internet.
# Docker Desktop on Mac and Windows routes container traffic through a userspace
# proxy on the host, and where that proxy terminates the TCP connection the
# socket the kernel reports on ends at the proxy rather than at Backblaze. Its
# round-trip time is then a fraction of a millisecond and describes nothing
# useful. Real paths to a storage pod are milliseconds at best, so anything this
# low means something local is answering, and n/a is the honest reading. Costs
# nothing on Unraid, where containers get real routed networking and this never
# fires. Also catches a transparent proxy on someone's own network.
IMPLAUSIBLE_RTT_MS = 1.0


def _upload_host():
    m = re.search(r'bz_upload_url="https?://([^/"]+)', read(BZDC_SYNCHOSTINFO))
    return m.group(1) if m else None


def _threadpush_uid():
    """UID owning the -threadpush upload processes, or None when nothing is
    uploading. Read from /proc/<pid>/status, which needs no capability, unlike
    /proc/<pid>/fd."""
    try:
        entries = os.listdir(PROC)
    except OSError:
        return None
    for pid in entries:
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join(PROC, pid, "cmdline"), "rb") as fh:
                if b"-threadpush" not in fh.read():
                    continue
            with open(os.path.join(PROC, pid, "status")) as fh:
                for line in fh:
                    if line.startswith("Uid:"):
                        return line.split()[1]
        except OSError:
            continue
    return None


def _diag_dump(family):
    """One NETLINK_SOCK_DIAG dump of established TCP sockets for a family,
    yielding (peer_ip, uid, rtt_us) per socket that reported tcp_info."""
    req = struct.pack("=BBBBI", family, socket.IPPROTO_TCP,
                       1 << (INET_DIAG_INFO - 1), 0, 1 << TCP_ESTABLISHED)
    req += bytes(48)                       # inet_diag_sockid: all-zero = match any
    hdr = struct.pack("=IHHII", 16 + len(req), SOCK_DIAG_BY_FAMILY,
                       NLM_F_REQUEST | NLM_F_DUMP, 1, 0)
    sk = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_SOCK_DIAG)
    try:
        sk.settimeout(2.0)
        sk.send(hdr + req)
        while True:
            buf = sk.recv(1 << 17)
            off = 0
            while off + 16 <= len(buf):
                mlen, mtype = struct.unpack_from("=IH", buf, off)
                if mlen < 16 or off + mlen > len(buf):
                    return
                if mtype in (NLMSG_DONE, NLMSG_ERROR):
                    return
                body = off + 16
                # inet_diag_msg: 4 bytes, then a 48-byte sockid, then five u32s.
                # Take the family from the record rather than from the request,
                # so the address is always decoded as whatever it actually is.
                msg_family = buf[body]
                dport = struct.unpack_from("!H", buf, body + 6)[0]
                dst = buf[body + 24:body + 40]
                uid = struct.unpack_from("=I", buf, body + 52 + 12)[0]
                rtt_us = None
                apos = body + 72
                while apos + 4 <= off + mlen:
                    rta_len, rta_type = struct.unpack_from("=HH", buf, apos)
                    if rta_len < 4:
                        break
                    if rta_type == INET_DIAG_INFO and rta_len - 4 > TCPI_RTT_OFFSET:
                        rtt_us = struct.unpack_from("=I", buf, apos + 4 + TCPI_RTT_OFFSET)[0]
                    apos += (rta_len + 3) & ~3
                if rtt_us:
                    raw = dst[:4] if msg_family == socket.AF_INET else dst
                    try:
                        yield socket.inet_ntop(msg_family, raw), uid, dport, rtt_us
                    except (ValueError, OSError):
                        pass
                off += (mlen + 3) & ~3
    finally:
        sk.close()


def _is_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _upload_rtt():
    """(rtt_ms, peer, note) for the upload connections. rtt_ms is None when
    there is nothing trustworthy to report, and note says why.

    Median across the live upload sockets, which is steadier than any single
    connection and unaffected by one thread that has just opened."""
    if not hasattr(socket, "AF_NETLINK"):
        return None, None, "not available on this platform"
    uid = _threadpush_uid()
    if uid is None:
        return None, None, "nothing uploading"
    samples = []
    reached_kernel = False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            for peer, sock_uid, dport, rtt_us in _diag_dump(family):
                reached_kernel = True
                if dport == 443 and str(sock_uid) == uid:
                    samples.append((rtt_us / 1000.0, peer))
        except OSError:
            continue
    if not samples:
        return None, None, ("no upload connections found" if reached_kernel
                             else "kernel socket table unavailable")
    samples.sort()
    ms, peer = samples[len(samples) // 2]
    if ms < IMPLAUSIBLE_RTT_MS and _is_public(peer):
        return None, None, "connection appears to terminate locally, not at the pod"
    return ms, peer, None



START_TIME = time.time()


def client_state():
    """What the client says it is doing, and the file it names.

    overviewstatus.xml is rewritten continuously and holds the client's own word
    for its state, which beats inferring one from which processes exist.
    current_file reads like "Part 14 of Something.mkv" during a large upload.
    """
    t = read(OVERVIEW)
    m = re.search(r'cur_state="([^"]*)"', t)
    f = re.search(r'current_file="([^"]*)"', t)
    p = re.search(r'current_file_fullpath="([^"]*)"', t)
    state = m.group(1) if m else None
    cur = unesc(f.group(1)) if f else None
    # cur_state is coarse and stays "transmitting" through work that is nothing of
    # the sort. The activity is in current_file, which holds a phrase rather than a
    # name when there is no file: "Producing File Lists..." with a fullpath of
    # "none". Where that happens the phrase is the state.
    if cur and (not p or p.group(1) in ("none", "")):
        state = cur.rstrip(".").strip() or state
    return (state, cur)


# current_file carries a small vocabulary, seen across a full backup pass:
#   Part 20 of <name>        a part of a multi-part file going up
#   Preparing <name>         and "Preparing 0 of <name>", before the parts start
#   Finishing <name>         after the last part
#   Producing File Lists...  a scan, with no file at all
#   <name>                   a whole small file, which is most of them
#   caNNN/bz_done_*.bzff     the client's own records, not anything of the user's
_ACTS = (
    (re.compile(r"^Producing File Lists", re.I),      "Producing file lists", 0, 0),
    (re.compile(r"^Preparing (?:\d+ of )?(.+)$"),     "Preparing",            1, 0),
    (re.compile(r"^Finishing (.+)$"),                 "Finishing",            1, 0),
    (re.compile(r"^Part (\d+) of (.+)$"),             "Uploading",            2, 1),
)
_INTERNAL = re.compile(r"(^|/)bz_done_[\w]+\.bzff$|^ca\d+/")


def activity():
    """{phase, file, part, internal} for whatever the client is doing now."""
    cur = client_state()[1]
    if not cur:
        return None
    if _INTERNAL.search(cur):
        return {"phase": "Uploading backup records", "file": cur,
                "part": None, "internal": True}
    for rx, phase, fg, pg in _ACTS:
        m = rx.match(cur)
        if m:
            return {"phase": phase,
                    "file": m.group(fg) if fg else None,
                    "part": int(m.group(pg)) if pg else None,
                    "internal": False}
    return {"phase": "Uploading", "file": cur, "part": None, "internal": False}


def scan_progress(live=None):
    """Progress of a file-list scan, or None when no scan is running.

    A scan writes a parallel .future set alongside the live one and marks it
    totally_final="false" until it finishes. topdirs gives directories indexed
    out of the total, which is the only real percentage the client exposes.
    """
    # The .future set is not removed when a scan ends, so its mere presence means
    # nothing: it sat at 14 of 28 for an hour while files uploaded. A scan is live
    # only while bzfilelist is running and the files are still being written to.
    if live is False:
        return None
    fresh = max(_mtime(FLISTS + "/topdirs.xml.future"),
                _mtime(FLISTS + "/filestats.xml.future"))
    if not fresh or time.time() - fresh > SCAN_STALE:
        return None
    fut = read(FLISTS + "/topdirs.xml.future")
    stats = read(FLISTS + "/filestats.xml.future")
    if not fut or 'totally_final="true"' in stats:
        return None
    mt = re.search(r'numtopdirs="(\d+)"', fut)
    mn = re.search(r'next_to_index="(\d+)"', fut)
    if not (mt and mn):
        return None
    total, done = int(mt.group(1)), int(mn.group(1))
    mf = re.search(r'everythingnum="(\d+)"', stats)
    mb = re.search(r'everythingtotbytes="(\d+)"', stats)
    return {"dirs_done": min(done, total), "dirs_total": total,
            "pct": (min(done, total) * 100.0 / total) if total else 0.0,
            "files": int(mf.group(1)) if mf else None,
            "bytes": int(mb.group(1)) if mb else None}


def measured_perf():
    """The client's own measured throughput, in kbit/s, split by file size."""
    t = read(PERFXML)
    big = re.search(r'perf_in_kbits_per_sec_larger_1mb="(\d+)"', t)
    small = re.search(r'perf_in_kbits_per_sec_smaller_1mb="(\d+)"', t)
    if not big:
        return None
    return {"large_kbit": int(big.group(1)),
            "small_kbit": int(small.group(1)) if small else None}


# ---- chunk map for the large file in flight ------------------------------------
# onechunk_seq*.dat describes every chunk of the file named in currentlargefile.xml:
# its index, its byte offset, its size and its SHA-1. That SHA-1 is also field 8 of
# the bz_done line carried in each thread's instruction, so a transfer in flight can
# be matched to the exact chunk it is carrying.
_chunk_cache = {"file": None, "map": {}, "total": 0}
_chunk_seen = {}          # file name -> {index: "sent"|"inflight"}


def _chunk_map():
    """{sha1: (index, offset, size)} plus the file it belongs to."""
    cur = read(LFDIR + "/currentlargefile.xml")
    m = re.search(r'bzfname="([^"]*)"', cur)
    name = unesc(m.group(1)).split("\\")[-1] if m else None
    if not name:
        return None, {}, 0
    if _chunk_cache["file"] == name:
        return name, _chunk_cache["map"], _chunk_cache["total"]
    out = {}
    try:
        names = [f for f in os.listdir(LFDIR) if f.startswith("onechunk_seq")]
    except OSError:
        names = []
    for f in names:
        c = read(os.path.join(LFDIR, f))
        mm = re.search(r'chunkSeqNum="(\w+)"[^>]*?startByteOffsetInOrigFile="(\d+)"'
                       r'[^>]*?numBytesInMyChunk="(\d+)"[^>]*?sha1ofMyChunkInOrigFile="(\w+)"', c)
        if mm:
            out[mm.group(4)] = (int(mm.group(1), 16), int(mm.group(2)), int(mm.group(3)))
    _chunk_cache.update({"file": name, "map": out, "total": len(out)})
    _chunk_seen.pop(name, None)
    return name, out, len(out)


def health():
    """Conditions worth warning about, from the client's own records.

    A safety freeze stops backups entirely, an unclean file check means the
    client thinks something is wrong, and a backup that has not completed within
    the user's own threshold is the warning the GUI would give.
    """
    out = []
    j = read(RPTS + "/status.json")
    if '"frozen"' in j and re.search(r'"frozen"\s*:\s*true', j):
        out.append(("frozen", "Backups are safety-frozen"))
    fc = read(RPTS + "/bzdc_filecheck.xml")
    if 'file_check_is_clean="false"' in fc:
        out.append(("filecheck", "Backblaze reports a failed file check"))
    # Only meaningful once the backup has caught up. bzstat_lastbackupcompleted
    # marks a pass finishing, not the whole set: on the machine this was written
    # against it read 4 August while 87% of 85 TB was still unsent. Warning about
    # that would be warning about a first upload doing exactly what it should.
    last = re.search(r'gmt_millis="(\d+)"', read(RPTS + "/bzstat_lastbackupcompleted.xml"))
    warn = re.search(r'numdays_warn_if_no_backup="(\d+)"', read(BZINFO))
    if last and _caught_up():
        days = (time.time() - int(last.group(1)) / 1000.0) / 86400.0
        limit = int(warn.group(1)) if warn else 7
        if days > limit:
            out.append(("stale", "No completed backup for %d days (limit %d)"
                                  % (int(days), limit)))
    sk = skipped_files()
    if sk and sk["total"]:
        out.append(("skipped", "%d files skipped and not backed up (%s)"
                                % (sk["total"], sk["top_reason"].replace("_", " ").lower())))
    return out


# Below this share of the set still to send, the backup counts as caught up and a
# missing completion is worth remarking on. Above it there is simply work left.
CAUGHT_UP_FRACTION = 0.02
SCAN_STALE = 300          # seconds without a write before a scan counts as over


def _caught_up():
    b = backup_totals()
    if not b or not b.get("total"):
        return True                       # nothing to judge against; do not suppress
    remaining = max(0, b["total"] - b["done"])
    return remaining <= b["total"] * CAUGHT_UP_FRACTION


def skipped_files():
    """Files the client has given up on, counted by its own reason.

    These are not queued and not retried: they are simply not backed up, and
    nothing in the GUI says so. Under this container the usual cause is a file
    the container user cannot read, so a large count here often means a
    permissions or ownership problem on the mounted source rather than anything
    wrong with Backblaze.
    """
    t = read(RPTS + "/bzlist_skipped_files.txt")
    if not t:
        return None
    reasons = {}
    for line in t.splitlines():
        f = line.split("\t")
        if len(f) >= 3 and f[1]:
            reasons[f[1]] = reasons.get(f[1], 0) + 1
    if not reasons:
        return None
    total = sum(reasons.values())
    top = max(reasons, key=reasons.get)
    return {"total": total, "top_reason": top, "reasons": reasons}


def first_backup():
    """Progress of a first upload still working through the set, or None.

    The client exposes no "initial backup finished" flag, so this is inferred:
    it is a first pass while a real share of the set has never been sent. Gives
    the day the first file went up, so a long upload reads as progress rather
    than as something being wrong.
    """
    if _caught_up():
        return None
    b = backup_totals()
    t = read(RPTS + "/bzstat_firstbackupfirstfileuploadedmillis.txt").strip()
    if not t.isdigit():
        return None
    days = (time.time() - int(t) / 1000.0) / 86400.0
    return {"days": days, "pct": b["pct"] if b else 0.0}


def last_backup_days():
    """Days since a backup last completed, or None."""
    m = re.search(r'gmt_millis="(\d+)"', read(RPTS + "/bzstat_lastbackupcompleted.xml"))
    return (time.time() - int(m.group(1)) / 1000.0) / 86400.0 if m else None


def upload_success_today():
    """(successes, failures) from the most recent day the client recorded."""
    rows = re.findall(r'<one_upload_success_stat ([^/]*)/>', read(RPTS + "/bzstat_upload_success.xml"))
    if not rows:
        return None
    last = rows[-1]
    def g(k):
        m = re.search(k + r'="(\d+)"', last)
        return int(m.group(1)) if m else 0
    fails = (g("num_upload_fail_CvtTooBusy") + g("num_upload_fail_CvtNoRoom")
             + g("num_upload_fail_UnknownReason"))
    return (g("num_upload_success"), fails)


def compress_saved():
    """Bytes the client says compression has saved, or None."""
    m = re.search(r'num_bytes_saved="(\d+)"', read(RPTS + "/bzstat_compress_save.xml"))
    return int(m.group(1)) if m else None


def speed_curve():
    """Throughput by payload size from the client's own speed test, kbit/s.

    Written as 10KB_112__100KB_632__1MB_2712__10MB_4304, which is the clearest
    statement of why small files are slow: each costs a round trip, so the rate
    climbs with payload size.
    """
    t = read(RPTS + "/bzperf_lastspeedtest.txt").strip()
    pairs = re.findall(r'(\d+[KM]B)_(\d+)', t)
    return [(sz, int(v)) for sz, v in pairs] or None


def volumes():
    """Source volumes with their space, from the client's own view."""
    out = []
    for a in re.findall(r'<bzvolume ([^/]*)/>', read(BZ + "/bzvolumes.xml")):
        mp = re.search(r'mountPointPathHex="([0-9a-f]*)"', a)
        tot = re.search(r'numBytesTotalOnVolume="(\d+)"', a)
        free = re.search(r'numBytesFreeOnVolume="(\d+)"', a)
        if not (tot and free):
            continue
        try:
            path = bytes.fromhex(mp.group(1)).decode("utf-8", "replace") if mp else "?"
        except ValueError:
            path = "?"
        out.append({"path": path, "total": int(tot.group(1)), "free": int(free.group(1))})
    return out or None


def uptime_str():
    secs = int(time.time() - START_TIME)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return "%02dD:%02dH:%02dM" % (d, h, m)


def _build_info():
    """Fields stamped into /etc/bb-build at image build time. Single source for
    every bb-* tool, so a bug report names the exact image rather than a
    per-tool version that can drift."""
    try:
        with open("/etc/bb-build") as fh:
            return {k: v.strip() for k, v in
                    (l.split("=", 1) for l in fh.read().splitlines() if "=" in l)}
    except OSError:
        return {}


def _build_label(info):
    v = info.get("version", "")
    if not v:
        return ""
    label = "v" + v if v[0].isdigit() else v
    # The :beta tag is mutable, so "beta" alone identifies nothing. The build
    # number (CI run) tells one published beta from the next in a bug report.
    b = info.get("build", "")
    return "%s+%s" % (label, b) if b and b != "0" else label


BUILD_INFO = _build_info()
BUILD_LABEL = _build_label(BUILD_INFO)

# ---- helpers (identical to bb-monitor) -----------------------------------
def unesc(t):
    """XML attribute values arrive escaped: a file called "Mike Judge's" reads as
    "Mike Judge&apos;s" and an ampersand as "&amp;". Anything pulled out of an
    attribute goes through here before it reaches a screen."""
    return html.unescape(t) if t else t


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def human(n):
    # Runs to PB. Stopping at GB meant a 250 TB backup rendered as
    # "257524.9 GB", eleven characters that overflowed the gauge label and got
    # clipped. Reported by gandalf15.
    if n >= 1125899906842624:
        return "%.1f PB" % (n / 1125899906842624)
    if n >= 1099511627776:
        return "%.1f TB" % (n / 1099511627776)
    if n >= 1073741824:
        return "%.1f GB" % (n / 1073741824)
    if n >= 1048576:
        return "%.0f MB" % (n / 1048576)
    return "%d KB" % (n / 1024)


def eta_str(secs):
    """Human ETA from a seconds estimate. None = rate too low/unknown to project."""
    if secs is None:
        return "stalled"
    if secs <= 0:
        return "done"
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d > 0:
        return "%dd %dh" % (d, h)
    if h > 0:
        return "%dh %dm" % (h, m)
    if m > 0:
        return "%dm" % m
    return "<1m"


def _secs(t):
    hh, mm, ss = t.split(":")
    return int(hh) * 3600 + int(mm) * 60 + int(ss)


def _local_hms(line, fallback):
    """Backblaze's own log lines are stamped in UTC regardless of the
    container's TZ setting. Returns (utc_hms, local_hms) so callers can keep
    using the UTC value for self-consistent internal span/duration math
    while showing the local value to the user."""
    m = re.match(r'^(\d{4}-\d{2}-\d{2}) (\d\d:\d\d:\d\d)', line)
    if not m:
        return fallback, fallback
    utc_hms = m.group(2)
    try:
        epoch = calendar.timegm(time.strptime(m.group(1) + " " + utc_hms, "%Y-%m-%d %H:%M:%S"))
        return utc_hms, time.strftime("%H:%M:%S", time.localtime(epoch))
    except ValueError:
        return utc_hms, utc_hms


def rate_str(nbytes, secs, kbit_fallback=0):
    if secs > 0 and nbytes > 0:
        kbit = nbytes / secs * 8 / 1000.0
        return "%.2f MB/s" % (nbytes / secs / 1048576) if kbit > 1024 else "%d kbit/s" % kbit
    if kbit_fallback > 1024:
        return "%.2f MB/s" % (kbit_fallback / 8192.0)
    return "%d kbit/s" % kbit_fallback if kbit_fallback else ""


# Field 12 of the bz_done line held the configured part size (10485760, ten
# mebibytes) on every multi-part upload inspected so far. It is used in
# preference to the live counter, with the largest value seen for a file as a
# fallback when the field is absent or implausible.
_PART_FIELD = 12
_MIN_PART = 1 << 20          # a part below a mebibyte is the counter, not the size
_part_seen = {}


def _part_size(fields, live):
    """Configured part size for this upload, in bytes."""
    if len(fields) > _PART_FIELD:
        try:
            v = int(fields[_PART_FIELD])
            if v >= _MIN_PART:
                return v
        except ValueError:
            pass
    # No usable field: the largest live reading seen for this file is the closest
    # thing to the part size, since the counter starts full and falls.
    key = fields[-1] if fields else ""
    best = max(_part_seen.get(key, 0), live)
    _part_seen[key] = best
    return best or live


def _parts_progress(name, fsize, part):
    """(done, total) for a file Backblaze has split, or None for a single part.

    done comes from the completed-parts bundle already tracked for the file, so a
    part counts only once it has actually landed.
    """
    if part <= 0 or fsize <= part * 1.5:
        return None
    total = max(1, -(-fsize // part))
    for r in _recent:
        if r.get("chunked") and r["name"] == name and r["done"] < r["total"]:
            return (min(r["done"], total), total)
    return (0, total)


def rec_cols(r):
    if r["chunked"]:
        span = r["last"] - r["first"]
        if span < 0:
            span += 86400
        # Clamp for display: a wrong total once let a bundle never finish, so every
        # later completion for the same name kept accumulating into it.
        return ("%d/%d" % (min(r["done"], r["total"]), r["total"]),
                human(r["bytes"]), rate_str(r["bytes"], span))
    return ("thr%d" % r["thr"], human(r["bytes"]) if r["bytes"] else "?",
             rate_str(r["bytes"], r["secs"], r["kbit"]))


# ---- data sources (identical to bb-monitor) ------------------------------
def scan_procs():
    thr = 0
    xmls = set()
    has_fl = False
    has_bt = False
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return 0, [], False, False
    for p in pids:
        try:
            with open(os.path.join(PROC, p, "cmdline"), "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace").replace("\0", " ")
        except OSError:
            continue
        if "bztransmit" in cmd:
            has_bt = True
        if "bzfilelist" in cmd:
            has_fl = True
        if "-threadpush" in cmd:
            thr += 1
            xmls.update(re.findall(r'bzt_\w+_bzt\.xml', cmd))
    return thr, sorted(xmls), has_fl, has_bt


def tail_log(n=800):
    try:
        fs = [os.path.join(LOGDIR, f) for f in os.listdir(LOGDIR) if f.endswith(".log")]
        if not fs:
            return ""
        p = max(fs, key=os.path.getmtime)
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            sz = fh.tell()
            fh.seek(max(0, sz - 262144))
            return "\n".join(fh.read().decode("utf-8", "replace").splitlines()[-n:])
    except OSError:
        return ""


def _tx():
    t = 0
    for ln in read(NETDEV).splitlines():
        f = ln.replace(":", " ").split()
        if len(f) >= 10 and f[0] != "lo" and f[1].isdigit():
            try:
                t += int(f[9])
            except ValueError:
                pass
    return t


def mem_info(host_total):
    for cur, mx, stat, key in (
        (CG2 + "/memory.current", CG2 + "/memory.max", CG2 + "/memory.stat", "inactive_file"),
        (CG1 + "/memory.usage_in_bytes", CG1 + "/memory.limit_in_bytes", CG1 + "/memory.stat", "total_inactive_file"),
    ):
        u = read(cur).strip()
        if not u.isdigit():
            continue
        m = re.search(r'^%s (\d+)' % key, read(stat), re.M)
        used = max(0, int(u) - (int(m.group(1)) if m else 0))
        l = read(mx).strip()
        lim = int(l) if l.isdigit() else 0
        if not 0 < lim < (1 << 60):
            lim = host_total
        if lim > 0:
            return (used, lim, used * 100.0 / lim)
    return None


def backup_totals():
    """Overall backup progress: total selected for backup vs. still remaining,
    from Backblaze's own bzstat_totalbackup.xml / bzstat_remainingbackup.xml
    (both rewritten periodically by the client itself, independent of any
    single upload session)."""
    tot_txt = read(BZSTAT_TOTAL)
    rem_txt = read(BZSTAT_REMAIN)
    tot_m = re.search(r'totnumbytesforbackup="(\d+)"', tot_txt)
    rem_m = re.search(r'remainingnumbytesforbackup="(\d+)"', rem_txt)
    if not (tot_m and rem_m):
        return None
    tot = int(tot_m.group(1))
    rem = min(int(rem_m.group(1)), tot)
    done = max(0, tot - rem)
    pct = (done / tot * 100.0) if tot > 0 else 0.0

    tot_f_m = re.search(r'totnumfilesforbackup="(\d+)"', tot_txt)
    rem_f_m = re.search(r'remainingnumfilesforbackup="(\d+)"', rem_txt)
    tot_files = int(tot_f_m.group(1)) if tot_f_m else None
    rem_files = int(rem_f_m.group(1)) if rem_f_m else None
    done_files = (tot_files - rem_files) if (tot_files is not None and rem_files is not None) else None

    return {"total": tot, "done": done, "pct": pct, "total_files": tot_files,
             "done_files": done_files, "remaining_files": rem_files}


def uptime_str():
    secs = int(time.time() - START_TIME)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return "%02dD:%02dH:%02dM" % (d, h, m)


# ---- data collection (identical logic to bb-monitor's gather()) ---------
def gather(prev):
    global _inflight, _recent, _sess
    o = {}
    ts = time.time()

    mi = read(MEMINFO)
    m = re.search(r'MemTotal:\s+(\d+)', mi)
    host_total = int(m.group(1)) * 1024 if m else 0
    mt = re.search(r'SwapTotal:\s+(\d+)', mi)
    mf = re.search(r'SwapFree:\s+(\d+)', mi)
    o["swap"] = None
    if mt and mf and int(mt.group(1)) > 0:
        tot = int(mt.group(1)) * 1024
        used = tot - int(mf.group(1)) * 1024
        o["swap"] = (used, tot, used * 100.0 / tot)
    o["mem"] = mem_info(host_total)
    o["backup"] = backup_totals()

    threads, xmls, has_fl, has_bt = scan_procs()
    o["threads"] = threads
    # The client's own state when it offers one, since it knows what it is doing
    # better than a guess from which processes exist.
    reported = client_state()[0]
    o["state"] = (reported.capitalize() if reported else
                  ("Transmitting" if threads else
                   "Scanning" if has_fl else
                   "Preparing" if has_bt else "Idle"))

    now = _tx()
    if prev and now > 0 and now >= prev[0] and ts > prev[1]:
        o["rate"] = (now - prev[0]) / (ts - prev[1]) / 1048576
        _sess += now - prev[0]
    else:
        o["rate"] = 0.0
    o["_tx"] = (now, ts) if now > 0 else prev
    o["sess"] = _sess

    # ETA rate: weighted average of (bytes / duration) over the last several
    # *completed* transfers, not the raw 2s network-counter sample. Individual
    # completions are real measured data points, so this only moves when a
    # new file actually finishes -- no more jitter from bursty in-flight
    # sampling. A live EMA is kept only as a fallback for the first minute or
    # so, before any completions have landed yet.
    global _rate_ema
    if prev is not None:
        _rate_ema = 0.15 * o["rate"] + 0.85 * _rate_ema
    if o.get("backup"):
        remaining = max(0, o["backup"]["total"] - o["backup"]["done"])
        o["backup"]["remaining"] = remaining
        hist_bytes = sum(b for b, s in _completed_hist)
        hist_secs = sum(s for b, s in _completed_hist)
        rate_bps = (hist_bytes / hist_secs) if hist_secs > 0 else (_rate_ema * 1048576)
        if remaining == 0:
            o["backup"]["eta_seconds"] = 0
        elif rate_bps > 1e-6:
            o["backup"]["eta_seconds"] = remaining / rate_bps
        else:
            o["backup"]["eta_seconds"] = None
        o["backup"]["eta_samples"] = len(_completed_hist)

    blob = tail_log()
    comps = [l for l in blob.splitlines() if "Leaving bztrans_thread_push" in l]
    times = [_secs(m.group(1)) for l in comps for m in [re.search(r'(\d\d:\d\d:\d\d)', l)] if m]
    o["chunks"] = sum(1 for t in times if (times[-1] - t) % 86400 <= 60) if times else 0
    sb = ss = 0
    for l in comps[-20:]:
        m = re.search(r'elapsedSec=(\d+).*?numBytes=(\d+) bytes', l)
        if m:
            ss += int(m.group(1))
            sb += int(m.group(2))
    ptbps = int(sb / ss) if ss > 0 else 175000

    def newest_line(thr):
        return next((l for l in reversed(comps)
                     if thr >= 0 and re.search(r'which_threadStr=0*%d(?!\d)' % thr, l)), None)

    files = []
    cur = {}
    cname, cmap, ctotal = _chunk_map()
    inflight_now = set()
    for x in xmls:
        xml = read(os.path.join(TDIR, x))
        mp = re.search(r'numBytes_to_send_in_shm="(\d+)"', xml)
        mg = re.search(r'gmt_started="(\d{14})"', xml)
        mh = re.search(r'hex_encoded_bz_done_line="([0-9a-f]*)"', xml)
        mw = re.search(r'which_thread="(\d+)"', xml)
        if not (mp and mg and mh):
            continue
        try:
            fields = bytes.fromhex(mh.group(1)).decode('utf-8', 'replace').rstrip("\n").split("\t")
        except ValueError:
            continue
        win = fields[-1]
        name = win.split("\\")[-1]
        # numBytes_to_send_in_shm belongs to the part a thread is carrying, not to
        # the file, and readings well below the part size turned filesize/part into
        # tens of thousands ("21/36010"). A file's last part is short by definition,
        # and other readings below the part size have been observed without the
        # cause being established. The bz_done line carries a part size that is
        # constant for the file, so it is used instead.
        part = _part_size(fields, int(mp.group(1)))
        thr = int(mw.group(1)) if mw else -1
        fsize = 0
        if len(win) > 2 and win[1] == ":":
            try:
                fsize = os.path.getsize("/config/wine/dosdevices/%s:%s" % (win[0].lower(), win[2:].replace("\\", "/")))
            except OSError:
                pass
        if fsize > part * 1.5 and name not in _logged:
            _logged.add(name)
            try:
                with open(MPLOG, "a") as fh:
                    fh.write("%s  file=%d part=%d ~parts=%d\n" % (time.strftime("%F %T"), fsize, part, -(-fsize // part)))
                    for i, fl in enumerate(fields):
                        fh.write("   [%02d] %r\n" % (i, fl))
                    fh.write("\n")
            except OSError:
                pass
        try:
            el = ts - calendar.timegm(time.strptime(mg.group(1), "%Y%m%d%H%M%S"))
        except ValueError:
            el = 0
        pct = min(99.0, max(0.0, el * ptbps / part * 100)) if part > 0 else 0
        sha = fields[8] if len(fields) > 8 else ""
        if sha and sha in cmap:
            seen = _chunk_seen.setdefault(cname, {})
            seen[cmap[sha][0]] = "inflight"
            inflight_now.add(cmap[sha][0])
        files.append((name, part, fsize, pct, _parts_progress(name, fsize, part)))
        cur[x] = (name, fsize, part, thr, newest_line(thr))
    act = activity()
    # bzcurrentlargefile/ is not cleared when a file finishes, so its presence
    # proves nothing: it still named a completed film, at 0/21, while the client
    # was producing file lists. The map is only real while that file is the one
    # being worked on, which is either a thread carrying one of its chunks or the
    # client naming it.
    live = bool(inflight_now) or bool(act and act.get("file") == cname)
    if cname and live:
        seen = _chunk_seen.setdefault(cname, {})
        for idx, st in list(seen.items()):
            if st == "inflight" and idx not in inflight_now:
                seen[idx] = "sent"
        o["chunkmap"] = {"file": cname, "total": ctotal,
                         "sent": sorted(i for i, v in seen.items() if v == "sent"),
                         "inflight": sorted(inflight_now)}
    else:
        o["chunkmap"] = None
    o["state_reported"], o["current_file"] = client_state()
    o["scan"] = scan_progress(has_fl)
    o["activity"] = act
    o["perf"] = measured_perf()
    o["health"] = health()
    o["upload_success"] = upload_success_today()
    o["compress_saved"] = compress_saved()
    o["last_backup_days"] = last_backup_days()
    o["first_backup"] = first_backup()
    o["skipped"] = skipped_files()
    o["files"] = files

    for xb, (nm, fs, part, thr, seen) in _inflight.items():
        if xb in cur and cur[xb][0] == nm:
            continue
        line = newest_line(thr)
        if line is None or line == seen:
            continue
        m = re.search(r'(\d\d:\d\d:\d\d)', line)
        fallback = m.group(1) if m else time.strftime("%H:%M:%S")
        tstr, tstr_local = _local_hms(line, fallback)
        end = _secs(tstr)
        mn = re.search(r'elapsedSec=(\d+).*?numBytes=(\d+) bytes', line)
        el = int(mn.group(1)) if mn else 0
        nb = int(mn.group(2)) if mn else 0
        if not nb:
            mb = re.search(r'\((\d+) MBytes\)', line)
            nb = int(mb.group(1)) * 1048576 if mb else 0
        mk = re.search(r'kBitsPerSec=(\d+)', line)
        kb = int(mk.group(1)) if mk else 0
        if part > 0 and fs > part * 1.5:
            b = next((r for r in _recent if r["chunked"] and r["name"] == nm and r["done"] < r["total"]), None)
            if b is None:
                # Every multi-part file produces one push beyond its part count,
                # consistently, across every sample looked at. Once a file's bundle
                # is full that trailing push is that file finishing, not a fresh
                # upload, so it is dropped rather than opening a second row.
                if any(r["chunked"] and r["name"] == nm for r in _recent):
                    continue
                b = {"chunked": True, "name": nm, "done": 0, "total": max(1, -(-fs // part)),
                     "bytes": 0, "first": end - el, "last": end}
                _recent.append(b)
            b["done"] += 1
            b["bytes"] += nb
            b["t"] = tstr_local
            b["first"] = min(b["first"], end - el)
            b["last"] = max(b["last"], end)
            _recent.remove(b)
            _recent.append(b)
            if b["done"] >= b["total"]:      # whole multi-part file now finished
                span = b["last"] - b["first"]
                if span < 0:
                    span += 86400            # parts straddling midnight
                if span > 0 and b["bytes"] > 0:
                    _completed_hist.append((b["bytes"], span))
        else:
            _recent.append({"chunked": False, "name": nm, "thr": thr, "t": tstr_local,
                             "bytes": fs or nb, "secs": el, "kbit": kb})
            if el > 0 and (fs or nb) > 0:
                _completed_hist.append((fs or nb, el))
    del _recent[:-10]
    _inflight = cur
    o["recent"] = list(_recent)
    return o


