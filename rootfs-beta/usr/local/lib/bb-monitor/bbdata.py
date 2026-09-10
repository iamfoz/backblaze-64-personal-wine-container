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
import calendar, html, ipaddress, json, os, re, socket, struct, time
from collections import deque

# ---- config (identical to bb-monitor) ------------------------------------
BZ = "/config/wine/dosdevices/c:/ProgramData/Backblaze/bzdata"
LOGDIR = BZ + "/bzlogs/bztransmit"
BZSTAT_TOTAL = BZ + "/bzreports/bzstat_totalbackup.xml"
BZSTAT_REMAIN = BZ + "/bzreports/bzstat_remainingbackup.xml"
TDIR = BZ + "/bzthread"
PROC = "/proc"
PAGE = os.sysconf("SC_PAGE_SIZE")
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
# One row per completed HTTPS request, one file per day of the month. The rows
# for files the datacenter already held read "dedup - 0 bytes", and they are the
# only record that a small file was checked rather than sent.
RPTLOG = BZ + "/bzlogs/bzreports_lastfilestransmitted"
BZINFO = BZ + "/bzinfo.xml"

# The "seen" maps below are keyed by file path or file name and were only ever
# added to. A first backup walks millions of files, so on a container the notes
# describe as memory-tight that is a monitor process that grows all week. Each is
# bounded to its most recently touched entries, oldest dropped first; anything
# still in flight is touched on every poll, so it is never the one dropped.
_SEEN_MAX = 400           # per-file entries kept in _part_seen and _logged
_CHUNK_FILES_MAX = 50     # files kept in _chunk_seen, each a whole chunk map


def _bound(d, limit=_SEEN_MAX):
    """Trim a dict to its `limit` newest keys. Insertion ordered, so a caller
    that wants a key kept must re-insert it rather than overwrite it in place."""
    while len(d) > limit:
        d.pop(next(iter(d)))


def _touch(d, key, value, limit=_SEEN_MAX):
    """Set d[key], moving it to the newest end, and keep d within limit."""
    d.pop(key, None)
    d[key] = value
    _bound(d, limit)
    return value


_logged = {}                # file name -> True, an ordered set for the one-off log line
_inflight = {}
_recent = []
_last_named = [None]        # last whole file the client named
_sess = 0
_rate_ema = 0.0
# Throughput history for the ETA: (bytes, seconds) per completed transfer,
# meaning a whole file, or a whole multi-part file once all its parts are in. Weighting by actual bytes/duration over the last several completions
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
# round-trip time is then a fraction of a millisecond. That value describes
# nothing useful. A real path to a storage pod needs milliseconds. A lower
# value shows that something local answered, so the monitor reports n/a. This
# costs nothing on Unraid, where containers get routed networking and this
# condition does not occur. It also finds a transparent proxy on your network.
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
    # cur_state is coarse. It stays "transmitting" during work that is not a
    # transmission. The activity is in current_file. That field holds a phrase,
    # not a name, when there is no file: "Producing File Lists..." with a
    # fullpath of "none". The phrase is then the state.
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


# bzdata/pauseinfo.xml exists only while a pause is set. bzcli, bzserv and
# bztransmit all name it, and bzserv's own wording is that the file has to go
# "to come out of pause", so its presence is the state rather than a flag inside
# it. Read every poll: it is a small file and costs nothing, where asking bzcli
# would spawn a Wine process.
PAUSEINFO = BZ + "/pauseinfo.xml"


def pause_state():
    """{"paused": bool, "until": epoch or None, "reason": str or None}."""
    t = read(PAUSEINFO)
    if not t:
        return {"paused": False, "until": None, "reason": None}
    m = re.search(r'pauseuntil_gmt_millis="(\d+)"', t)
    until = int(m.group(1)) // 1000 if m else None
    # pauseuntil_reason, spelled out: a bare reason=" would match this by
    # accident today and stop doing so the moment a real reason attribute appears.
    r = re.search(r'pauseuntil_reason="([^"]*)"', t)
    # A pause with a deadline that has passed is over, whether or not the client
    # has got round to removing the file.
    paused = True if until is None else time.time() < until
    return {"paused": paused, "until": until if paused else None,
            "reason": r.group(1) if r else None}


def scan_progress(live=None):
    """Progress of a file-list scan, or None when no scan is running.

    A scan writes a parallel .future set alongside the live one and marks it
    totally_final="false" until it finishes. topdirs gives directories indexed
    out of the total, which is the only real percentage the client exposes.
    """
    # The client does not remove the .future set when a scan ends. The presence
    # of the set proves nothing. It stayed at 14 of 28 for one hour while the
    # client uploaded files. A scan is live
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


def _chunk_seen_for(name):
    """The chunk map for one file, created if new and kept as the newest entry
    so the bound on _chunk_seen drops files nobody is uploading any more."""
    return _touch(_chunk_seen, name, _chunk_seen.get(name) or {}, _CHUNK_FILES_MAX)


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


def health(data=None):
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
    if last and _caught_up(data):
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


def _caught_up(data=None):
    # gather() already has a backup_totals() reading and hands it down, so one
    # poll reads the two stat files once rather than once per caller. Every one
    # of these still reads for itself when called on its own, which is how the
    # web dashboard and the tests use them.
    b = data if data is not None else backup_totals()
    if not b or not b.get("total"):
        return True                       # nothing to judge against; do not suppress
    remaining = max(0, b["total"] - b["done"])
    return remaining <= b["total"] * CAUGHT_UP_FRACTION


SKIPPED = "/bzlist_skipped_files.txt"
_skipped_cache = {"key": None, "counts": None, "rows": None, "reasons": None}


def _skipped_parse():
    """(counts, rows, reasons) from the skipped-files report, parsed once per
    change of the file.

    Both public readers run on every poll, and a permissions mistake on one
    mounted directory puts tens of thousands of rows in this report: parsing it
    twice a second, twice over, was a measurable share of the container's CPU
    for a list that changes hourly at most. Cached on st_mtime_ns the way
    dedup_names() caches its own report. The row dicts are shared with every
    caller, so a caller that needs to change one must copy it first.
    """
    p = RPTS + SKIPPED
    try:
        stt = os.stat(p)
        key = (stt.st_mtime_ns, stt.st_size)
    except OSError:
        key = None
    if key is not None and key == _skipped_cache["key"]:
        return _skipped_cache["counts"], _skipped_cache["rows"], _skipped_cache["reasons"]
    counts = rows = reasons = None
    t = read(p)
    if t:
        by_reason, found = {}, []
        for line in t.splitlines():
            # The report opens with a "# SkippedFilesReportStarted" line. As
            # observed it carries no tabs, so the field test below already
            # excludes it, but a tabbed variant would be counted as a file with
            # a date for a reason. Excluding comments outright costs one
            # condition and makes this agree with bb-doctor, which already does
            # it.
            if not line or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) < 3 or not f[1]:
                continue
            by_reason[f[1]] = by_reason.get(f[1], 0) + 1
            # The path is located by drive letter rather than taken from a fixed
            # column, since only the reason column is established from a real
            # capture.
            path = next((x for x in f if re.match(r'^[A-Za-z]:\\', x)), None)
            if path:
                found.append({"path": path, "reason": f[1],
                              "name": path.split("\\")[-1]})
        if by_reason:
            counts = {"total": sum(by_reason.values()),
                      "top_reason": max(by_reason, key=by_reason.get),
                      "reasons": by_reason}
        if found:
            found.reverse()     # the client appends, so the newest are last
            rows = found
            reasons = {}
            for r in rows:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    _skipped_cache.update(key=key, counts=counts, rows=rows, reasons=reasons)
    return counts, rows, reasons


def skipped_files():
    """Files the client has given up on, counted by its own reason.

    These are not queued and not retried: they are simply not backed up, and
    nothing in the GUI says so. Under this container the usual cause is a file
    the container user cannot read, so a large count here often means a
    permissions or ownership problem on the mounted source rather than anything
    wrong with Backblaze.
    """
    return _skipped_parse()[0]


def skipped_list(limit=500):
    """The skipped files themselves: [{path, reason, name}], newest first.

    skipped_files() counts them; this names them, which is what someone trying
    to fix the problem actually needs. Capped, because a permissions mistake on
    one directory can list tens of thousands and nobody reads past the first
    screen; the count beside it says how many there are in total.
    """
    _, rows, reasons = _skipped_parse()
    if not rows:
        return None
    # The count uses every row, not only the rows that this function returns. A
    # display that shows the first few hundred files can thus give the correct
    # number for each reason.
    return {"total": len(rows), "shown": min(len(rows), limit),
            "reasons": reasons, "files": rows[:limit]}


def first_backup(data=None):
    """Progress of a first upload still working through the set, or None.

    The client exposes no "initial backup finished" flag, so this is inferred:
    it is a first pass while a real share of the set has never been sent. Gives
    the day the first file went up, so a long upload reads as progress rather
    than as something being wrong.
    """
    if _caught_up(data):
        return None
    b = data if data is not None else backup_totals()
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
    """(successes, retried_attempts, reasons) from the client's most recent day.

    The second figure is failed ATTEMPTS, not failed files. CvtTooBusy and
    CvtNoRoom mean the storage vault turned an attempt away and the client
    retried against another, which is routine load balancing on Backblaze's
    side; the file still goes up. Nothing here names a file, because no file
    failed, and displaying this as "failed" sent people hunting through logs
    for five files that never existed. The number that means data at risk is
    skipped_files, not this.
    """
    rows = re.findall(r'<one_upload_success_stat ([^/]*)/>', read(RPTS + "/bzstat_upload_success.xml"))
    if not rows:
        return None
    last = rows[-1]
    def g(k):
        m = re.search(k + r'="(\d+)"', last)
        return int(m.group(1)) if m else 0
    reasons = {"vault_busy": g("num_upload_fail_CvtTooBusy"),
               "vault_full": g("num_upload_fail_CvtNoRoom"),
               "unknown": g("num_upload_fail_UnknownReason")}
    return (g("num_upload_success"), sum(reasons.values()), reasons)


def upload_history(days=7):
    """The last `days` recorded days, oldest first: [{day, success, retried}].

    The client keeps one row per day and only the newest was read before. The
    date attribute's name was never established from a real file (only the
    num_* counters were), so the date is found by shape: whichever attribute
    holds an eight-digit YYYYMMDD value. Rows with no such value still count,
    labelled by position, so the chart degrades to "last N days" rather than
    vanishing on a naming surprise.
    """
    rows = re.findall(r'<one_upload_success_stat ([^/]*)/>',
                      read(RPTS + "/bzstat_upload_success.xml"))
    if not rows:
        return None
    out = []
    for row in rows[-days:]:
        def g(k):
            m = re.search(k + r'="(\d+)"', row)
            return int(m.group(1)) if m else 0
        md = re.search(r'[a-z_]+="((?:19|20)\d{6})"', row)
        out.append({"day": md.group(1) if md else None,
                    "success": g("num_upload_success"),
                    "retried": (g("num_upload_fail_CvtTooBusy")
                                + g("num_upload_fail_CvtNoRoom")
                                + g("num_upload_fail_UnknownReason"))})
    return out


# ---- the completion moment ----------------------------------------------
DONE_MARK = "/config/.bb-first-backup-done"
CELEBRATE_DAYS = 7


def completion(data=None):
    """The first time the backup catches up, remember it and say so for a week.

    A latch rather than a live reading: once written it is never celebrated
    again, so new files pushing the remainder back over the threshold cannot
    replay it. Returns {days, total_bytes, done_at} while the moment is fresh,
    None otherwise.
    """
    b = data if data is not None else backup_totals()
    mark = read(DONE_MARK)
    if mark:
        m = re.search(r'done_at=(\d+) days=(\d+) bytes=(\d+)', mark)
        if not m:
            return None
        done_at = int(m.group(1))
        if time.time() - done_at > CELEBRATE_DAYS * 86400:
            return None
        return {"done_at": done_at, "days": int(m.group(2)),
                "total_bytes": int(m.group(3))}
    # Not yet marked, so decide whether this is the moment. Deliberately NOT via
    # _caught_up(), which answers True when there are no totals to judge against:
    # that default exists so a missing figure cannot raise a false staleness
    # warning, and it is exactly wrong here, where the same answer declares a
    # backup finished. During a file-list scan the totals are absent or zero, and
    # a container 21.9% through its first backup announced it had completed, then
    # latched that to disk for a week. This asks for positive evidence instead.
    if not b or not b.get("total") or not b.get("done"):
        return None
    if max(0, b["total"] - b["done"]) > b["total"] * CAUGHT_UP_FRACTION:
        return None
    # The day count comes from the client's own record of when the first file
    # went up, not from first_backup(), which returns None the moment a backup
    # is caught up and so was always None by the time this ran: every completion
    # would have read "in 0 days". No record means no first backup to have
    # finished, so there is nothing to announce.
    t = read(RPTS + "/bzstat_firstbackupfirstfileuploadedmillis.txt").strip()
    if not t.isdigit():
        return None
    days = int((time.time() - int(t) / 1000.0) / 86400.0)
    try:
        with open(DONE_MARK, "w", encoding="utf-8") as fh:
            fh.write("done_at=%d days=%d bytes=%d\n"
                     % (int(time.time()), days, b.get("total") or 0))
    except OSError:
        return None
    return {"done_at": int(time.time()), "days": days,
            "total_bytes": b.get("total") or 0}


def eta_date(eta_seconds):
    """An ETA as a date, because "done around 30 January" is a thing a person
    can picture and "171 days" is not. Day resolution on purpose: an estimate
    from a moving average should not pretend to know the hour."""
    if not eta_seconds or eta_seconds <= 0:
        return None
    return time.strftime("%d %b %Y", time.localtime(time.time() + eta_seconds))


def composition():
    """What the backup is made of, from the completed filestats.xml.

    The category attributes (docsnum, pictnum, videonum, musicnum alongside
    everythingnum) are established from a real capture. "other" is the
    remainder, so the parts always account for the whole even if the client
    counts categories this does not know about.
    """
    t = read(FLISTS + "/filestats.xml")
    if not t:
        return None
    def g(k):
        m = re.search(k + r'="(\d+)"', t)
        return int(m.group(1)) if m else None
    total = g("everythingnum")
    if not total:
        return None
    cats = {"photos": g("pictnum"), "documents": g("docsnum"),
            "music": g("musicnum"), "video": g("videonum")}
    cats = {k: v for k, v in cats.items() if v}
    other = total - sum(cats.values())
    if other > 0:
        cats["other"] = other
    return {"files": total, "bytes": g("everythingtotbytes"),
            "categories": cats}


def backing_up_since():
    """When this backup began, as YYYYMMDD, or None.

    ahguidcreated is established from a real capture and marks the account-host
    pairing. firstfilescantime.xml is older still but its internal shape was
    never captured, so it contributes by shape (any YYYYMMDD attribute) and,
    failing that, by its own mtime, since it is written once at install and the
    observed timestamp matched the install date.
    """
    for t in (read(FLISTS + "/filestats.xml"), read(BZINFO)):
        m = re.search(r'ahguidcreated="((?:19|20)\d{6})"', t)
        if m:
            return m.group(1)
    f = FLISTS + "/firstfilescantime.xml"
    m = re.search(r'[a-z_]+="((?:19|20)\d{6})"', read(f))
    if m:
        return m.group(1)
    mt = _mtime(f)
    return time.strftime("%Y%m%d", time.localtime(mt)) if mt else None


# A century. The worst plausible real case is something like a 100 TB set on a
# 1 Mbit/s uplink, which is roughly 25 years, so a bound has to sit above that
# to avoid hiding an estimate that is dismal but true. The startup arithmetic
# that prompted this lands in the thousands of years, so there is a wide gap
# between the worst real answer and the first absurd one, and the bound goes in
# the gap rather than near either edge.
ETA_MAX = 100 * 365 * 86400
ETA_MIN_SAMPLES = 3       # see _eta_trend

ETA_HIST = "/config/.bb-eta-history"
ETA_STEADY = 0.02         # the steady band: see _eta_trend


def _eta_trend(backup):
    """Is the estimate getting better? {direction, delta_seconds} or None.

    One sample per day is recorded, because the question this answers is "is it
    better than yesterday", and an estimate compared with itself an hour ago
    only measures the jitter of the moving average it came from.
    """
    if not backup or not backup.get("eta_seconds"):
        return None
    # Do not record, or compare against, an estimate resting on a couple of
    # completed transfers. Those are whatever the client happened to send first,
    # and comparing today's against yesterday's measures the sample, not the
    # backup. A recorded bad value is worse than a missing one: it stays in the
    # history and poisons tomorrow's comparison too.
    if backup.get("eta_samples", 0) < ETA_MIN_SAMPLES:
        return None
    eta = backup["eta_seconds"]
    today = time.strftime("%Y%m%d")
    try:
        hist = json.loads(read(ETA_HIST) or "{}")
        if not isinstance(hist, dict):
            hist = {}
    except ValueError:
        hist = {}
    if hist.get(today) is None:
        hist[today] = eta
        for day in sorted(hist)[:-14]:      # keep a fortnight
            del hist[day]
        try:
            with open(ETA_HIST, "w", encoding="utf-8") as fh:
                json.dump(hist, fh)
        except OSError:
            pass
    prev_days = [d for d in sorted(hist) if d < today]
    if not prev_days:
        return None
    prev = hist[prev_days[-1]]
    if not prev:
        return None
    delta = eta - prev
    # Steady means within two percent or within a day, whichever is larger: a
    # sub-day wobble on a short ETA is sampling noise, and on a six-month one
    # even two percent is several days, which is news.
    if abs(delta) <= max(86400, prev * ETA_STEADY):
        direction = "steady"
    else:
        direction = "improving" if delta < 0 else "worsening"
    return {"direction": direction, "delta_seconds": delta}


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


def human(n, dp=1):
    # Runs to PB. Stopping at GB meant a 250 TB backup rendered as
    # "257524.9 GB", eleven characters that overflowed the gauge label and got
    # clipped. Reported by gandalf15.
    #
    # dp is for the one figure that needs it: the backup total. At one decimal
    # place a TB reading only moves every 102.4 GB, which on a large backup is
    # most of a day, so a correct figure sat unchanged long enough to be
    # reported as a stuck counter. Everything else keeps one place, because a
    # file size in the completed table gains nothing from a second and the
    # gauge label above has a width to respect.
    if n >= 1125899906842624:
        return "%.*f PB" % (dp, n / 1125899906842624)
    if n >= 1099511627776:
        return "%.*f TB" % (dp, n / 1099511627776)
    if n >= 1073741824:
        return "%.*f GB" % (dp, n / 1073741824)
    if n >= 1048576:
        return "%.0f MB" % (n / 1048576)
    return "%d KB" % (n / 1024)


def eta_str(secs):
    """Human ETA from a seconds estimate.

    None covers two cases that both mean "no usable estimate": no measurable
    rate at all, and a rate so low the projection is absurd, which is what the
    first few small files produce at startup. "Stalled" was the old word and it
    is wrong for the second case, where the backup is uploading briskly and only
    the estimate is unusable. There is no way to tell them apart here, so the
    word has to be true of both.
    """
    if secs is None:
        return "not yet"
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
        # kbit is 1000 bits, as the measured branch above reads it, and the
        # printed figure is mebibytes as human() uses them: 1000 / 8 / 1048576
        # inverted. Dividing by 8192 mixed the two and read 2.4% high, so the
        # same physical rate printed differently depending on which branch ran.
        return "%.2f MB/s" % (kbit_fallback / 8388.608)
    return "%d kbit/s" % kbit_fallback if kbit_fallback else ""


# Field 12 of the bz_done line held the configured part size (10485760, ten
# mebibytes) on every multi-part upload inspected so far. It is used in
# preference to the live counter, with the largest value seen for a file as a
# fallback when the field is absent or implausible.
_PART_FIELD = 12
_MIN_PART = 1 << 20          # a part below a mebibyte is the counter, not the size
_part_seen = {}


def _part_size(fields, live):
    """Configured part size for this upload, in bytes.

    The part size is constant for a file, so the largest credible reading seen for
    it is the answer, and a later short reading cannot undo it. Field 12 used to
    return early without recording, so a line that lacked the field fell back to
    the live counter with nothing better remembered, and one short reading set a
    bundle's total for good: a 221 MB file came out as 236 parts rather than 22,
    which is that file divided by about 937 KB.
    """
    key = fields[-1] if fields else ""
    best = _part_seen.get(key, 0)
    if len(fields) > _PART_FIELD:
        try:
            v = int(fields[_PART_FIELD])
            if v >= _MIN_PART:
                best = max(best, v)
        except ValueError:
            pass
    # The live counter belongs to the part a thread is carrying. A file's last
    # part is short by definition, so it is only worth believing when it is at
    # least a part-sized reading and nothing better is known.
    if live >= _MIN_PART:
        best = max(best, live)
    if best:
        # _touch rather than a plain assignment: a file being uploaded is asked
        # about on every poll, so re-inserting it keeps it off the end that the
        # bound drops.
        return _touch(_part_seen, key, best)
    return live


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


_dedup_cache = {"key": None, "names": {}}
DEDUP_TAIL = 400          # rows read from the end of the day's report


def dedup_names():
    """{file name: HH:MM:SS} for recent "dedup - 0 bytes" rows in the client's
    transmission report: files it checked against the datacenter and did not
    send because the datacenter already had them.

    After a restart the client re-checks the small files between its checkpoint
    and where it had got to, one round trip each, and names each one as it goes.
    Without this those showed in Recently Completed with dashes in every column,
    which read as a transfer nobody could measure rather than a file that did not
    need one. Confirmed against a live log: the row for a file appeared three
    seconds before the monitor saw the client move on from it.

    Today's file and, in the first minutes of a day, yesterday's. Re-read only
    when the file changes, since this runs on every poll.
    """
    now = time.localtime()
    # The rows inside the file are UTC-stamped, and the client's own naming
    # convention for the file is not established from a live capture, so both
    # candidates are tried. They are the same file for most of the day; on a
    # container well east or west of UTC they differ for the hours the offset
    # covers, and reading only the local one returned nothing for that whole
    # window.
    days = []
    if now.tm_hour == 0 and now.tm_min < 10:
        days.append(time.localtime(time.time() - 86400).tm_mday)
    days.append(time.gmtime().tm_mday)
    days.append(now.tm_mday)
    paths = []
    for mday in days:
        p = RPTLOG + "/%02d.log" % mday
        if p not in paths:
            paths.append(p)
    key = []
    for p in paths:
        try:
            key.append((p, os.stat(p).st_mtime_ns))
        except OSError:
            key.append((p, None))
    if key == _dedup_cache["key"]:
        return _dedup_cache["names"]
    names = {}
    for p, _ in key:
        try:
            with open(p, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - DEDUP_TAIL * 200))
                tail = fh.read().decode("utf-8", "replace").splitlines()[-DEDUP_TAIL:]
        except OSError:
            continue
        for line in tail:
            if "dedup - 0 bytes" not in line:
                continue
            # "2026-09-06 01:02:56 -  small  - throttle x  -  -  dedup - 0 bytes - D:\...\name"
            # Taken from the fixed prefix rather than by splitting on the last
            # " - ": the row's own separator is " - " too, so a file name that
            # contains one ("Artist - Track.mp3") lost everything before it and
            # never matched the name the client reports.
            mp = re.search(r"dedup - 0 bytes - (.*)$", line)
            if not mp:
                continue
            path = mp.group(1).strip()
            name = path.replace("/", "\\").split("\\")[-1]
            m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})", line)
            if name:
                names[name] = m.group(1) if m else ""
    _dedup_cache["key"] = key
    _dedup_cache["names"] = names
    return names


def mark_dedup(rows):
    """Flag the small rows whose file the report shows as already held."""
    names = None
    for r in rows:
        if r.get("small") and not r.get("dedup"):
            if names is None:
                names = dedup_names()
            if r["name"] in names:
                r["dedup"] = True


def rec_cols(r, wide=True):
    # A file too small to catch in flight has no thread, no measured size and no
    # rate. Its name and the time it was dealt with are all there is, unless the
    # report says the datacenter already held it, which is worth a word. The
    # terminal's Size column is nine characters, so it gets the client's own
    # word for it; the web page has room for the sentence.
    if r.get("small"):
        held = ("already backed up" if wide else "dedup") if r.get("dedup") else "\u2014"
        return ("\u2014", held, "\u2014")
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


# The programs worth attributing memory to. The client comes first, because it
# is almost always the answer: a first scan over a large set is the memory peak
# of this container, and bzfilelist holds the file list while it builds it.
_MEM_NAMES = ("bzfilelist", "bztransmit", "bzserv", "bzbuitray", "bzbui",
              "wineserver", "bb-monitor-web", "bb-monitor", "nginx", "Xvfb")


def memory_by_process(limit=5):
    """[{name, rss_bytes}], largest first, or None.

    The monitor gives the memory that the container uses. It could not give the
    program that uses it. A user then reported high memory after an update, when
    the cause was the client that read a large file list. That is normal. This
    function answers the question that the memory number asks.
    """
    try:
        pids = [x for x in os.listdir(PROC) if x.isdigit()]
    except OSError:
        return None
    by_name = {}
    for pid in pids:
        try:
            with open(os.path.join(PROC, pid, "cmdline"), "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace").replace(chr(0), " ")
            if not cmd.strip():
                continue
            name = next((n for n in _MEM_NAMES if n in cmd), None)
            if not name:
                continue
            with open(os.path.join(PROC, pid, "statm")) as fh:
                # Field 2 of statm is the resident set, in pages. statm has one
                # short line. status has 50 lines that this must search.
                rss = int(fh.read().split()[1]) * PAGE
        except (OSError, ValueError, IndexError):
            continue
        by_name[name] = by_name.get(name, 0) + rss
    if not by_name:
        return None
    rows = sorted(({"name": k, "rss_bytes": v} for k, v in by_name.items()),
                  key=lambda r: r["rss_bytes"], reverse=True)
    return rows[:limit]


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
    o["memory_by_process"] = memory_by_process()
    o["backup"] = backup_totals()

    threads, xmls, has_fl, has_bt = scan_procs()
    o["threads"] = threads
    # The client's own state when it offers one, since it knows what it is doing
    # better than a guess from which processes exist.
    reported = client_state()[0]
    # The client writes cur_state="none" when it has no state to report. The
    # capitalised form is the text "None". A reader sees that as a null value
    # that reached the payload by mistake. Treat it as no answer and fall back to
    # what the running processes show.
    if reported and reported.strip().lower() in ("none", "unknown", ""):
        reported = None
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
            eta = remaining / rate_bps
            # A backup starts on the small files, and a handful of those gives a
            # per-byte rate low enough to extrapolate into the millennia: one
            # observed run read "4259966d", about 11,600 years. A slow uplink
            # can take years, so the ceiling is generous, but past it the
            # number is arithmetic rather than an estimate, and showing
            # nothing beats showing that.
            o["backup"]["eta_seconds"] = eta if eta <= ETA_MAX else None
        else:
            o["backup"]["eta_seconds"] = None
        o["backup"]["eta_samples"] = len(_completed_hist)

    blob = tail_log()
    comps = [l for l in blob.splitlines() if "Leaving bztrans_thread_push" in l]
    times = [_secs(m.group(1)) for l in comps for m in [re.search(r'(\d\d:\d\d:\d\d)', l)] if m]
    # Against now, not against the newest line in the tail. Measured against the
    # tail the figure froze at whatever the last burst reached and stayed there
    # for hours after uploads stopped, and the modular arithmetic also let a line
    # a whole day old satisfy the test. The stamps are UTC, so the comparison
    # point is UTC seconds of day too.
    g = time.gmtime(ts)
    now_sod = g.tm_hour * 3600 + g.tm_min * 60 + g.tm_sec
    nchunks = 0
    for t in times:
        age = now_sod - t
        if age < 0:
            age += 86400          # the line is on the other side of midnight
        if age <= 60:
            nchunks += 1
    o["chunks"] = nchunks
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
        # numBytes_to_send_in_shm gives the size of the part that a thread carries.
        # It does not give the size of the file. A value much smaller than the part
        # size made filesize/part very large ("21/36010"). The last part of a file
        # is always short. Other values below the part size also occur, and the
        # cause of those is not known. The bz_done line gives a part size that
        # stays constant for the file, so the code uses that value.
        part = _part_size(fields, int(mp.group(1)))
        thr = int(mw.group(1)) if mw else -1
        fsize = 0
        if len(win) > 2 and win[1] == ":":
            try:
                fsize = os.path.getsize("/config/wine/dosdevices/%s:%s" % (win[0].lower(), win[2:].replace("\\", "/")))
            except OSError:
                pass
        if fsize > part * 1.5 and name not in _logged:
            _touch(_logged, name, True)
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
            seen = _chunk_seen_for(cname)
            seen[cmap[sha][0]] = "inflight"
            inflight_now.add(cmap[sha][0])
        files.append((name, part, fsize, pct, _parts_progress(name, fsize, part)))
        cur[x] = (name, fsize, part, thr, newest_line(thr))
    act = activity()
    # A small file completes before a poll can find a thread that carries it.
    # These files thus never enter the in-flight table. They also never reach
    # the completed table.
    # Backblaze pushes them in bundles rather than singly, which is why the log has
    # no per-file record of them. The client does name each one as it deals with
    # it, so a change of name means it has finished with the one before.
    if act and not act["internal"] and act["part"] is None and act["file"] \
            and act["phase"] == "Uploading":
        # Not `prev`: that is this function's parameter, the previous (bytes,
        # timestamp) sample, and rebinding it here would hand a file name to
        # anything later that read it.
        last_named = _last_named[0]
        if last_named and last_named != act["file"] \
                and not any(r["name"] == last_named for r in _recent):
            _recent.append({"chunked": False, "small": True, "name": last_named, "thr": 0,
                            "t": time.strftime("%H:%M:%S"), "bytes": 0,
                            "secs": 0, "kbit": 0, "dedup": False})
        _last_named[0] = act["file"]
    # The report row for a checked file lands a few seconds around the client
    # moving on from it, so every poll looks again at the rows not yet marked.
    mark_dedup(_recent)
    # bzcurrentlargefile/ is not cleared when a file finishes, so its presence
    # proves nothing: it still named a completed film, at 0/21, while the client
    # was producing file lists. The map is only real while that file is the one
    # being worked on, which is either a thread carrying one of its chunks or the
    # client naming it.
    live = bool(inflight_now) or bool(act and act.get("file") == cname)
    if cname and live:
        seen = _chunk_seen_for(cname)
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
    o["pause"] = pause_state()
    # A paused backup still leaves bztransmit running and current_file naming
    # whatever it had last, so without this the monitors read "Uploading" while
    # nothing moves.
    #
    # A pause is not instant. The client finishes the transfers already in flight
    # before it stops, which is the point of asking rather than killing, and only
    # then does its own window show Paused. Saying "Paused" the moment the pause
    # file appears is therefore ahead of the truth: observed on a live backup,
    # transfers kept completing after the request while threads were still
    # draining. Threads still running means it is on its way, not there yet.
    if o["pause"]["paused"]:
        draining = bool(threads)
        o["pause"]["draining"] = draining
        o["state"] = "Pausing" if draining else "Paused"
    else:
        o["pause"]["draining"] = False
        # A restart re-checks the small files between the client's checkpoint and
        # where it had got to. The client names each one, the datacenter answers
        # that it already holds it, and nothing is sent: threads sit at zero
        # while the state reads Transmitting, which looks like a stall and was
        # reported as one. Named for what it is instead.
        if not threads and o["state"] in ("Transmitting", "Uploading"):
            small = [r for r in _recent[-6:] if r.get("small")]
            if small and all(r.get("dedup") for r in small):
                o["state"] = "Checking"
    # The reading taken at the top of this poll, handed to everything below that
    # would otherwise re-read the same two stat files for itself. Seven of them
    # did, so one poll read that pair sixteen times.
    bt = o.get("backup")
    o["per_volume"] = per_volume()
    o["remaining_shape"] = remaining_shape(bt)
    o["perf"] = measured_perf()
    o["health"] = health(bt)
    o["upload_success"] = upload_success_today()
    o["upload_history"] = upload_history()
    o["completion"] = completion(bt)
    o["composition"] = composition()
    o["backing_up_since"] = backing_up_since()
    o["eta_trend"] = _eta_trend(bt)
    o["compress_saved"] = compress_saved()
    o["last_backup_days"] = last_backup_days()
    o["first_backup"] = first_backup(bt)
    o["skipped"] = skipped_files()
    o["skipped_list"] = skipped_list()
    o["pause_label"] = pause_label(o["pause"])
    o["milestones"] = milestones(bt)
    o["progress_history"] = progress_history(data=bt)
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
                if part < _MIN_PART:
                    # A part size this small cannot be the configured one, and a
                    # total set from it is wrong for the life of the row. Wait for
                    # a reading that makes sense: the next push for this file
                    # brings one, at the cost of one uncounted part.
                    continue
                total = max(1, -(-fs // part))
                try:
                    with open(MPLOG, "a") as fh:
                        fh.write("%s  BUNDLE %s file=%d part=%d total=%d\n"
                                 % (time.strftime("%F %T"), nm[:60], fs, part, total))
                except OSError:
                    pass
                b = {"chunked": True, "name": nm, "done": 0, "total": total,
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




# ---- why paused ------------------------------------------------------------------
# The client writes a reason code with every pause. Shown raw it is an identifier;
# read as words it answers the question a bare "Paused" raises, and it says who
# to look at: a pause set from here is a button, a pause the client chose is a
# wait. The code stays alongside for anyone searching Backblaze's own help.
PAUSE_REASONS = {
    "action_pause_backup": ("here", "Paused from here",
                            "Set from the Monitor, the API or bzcli."),
    "ca_down_but_network_alive": ("client", "Paused by the client",
                                  "Backblaze's cluster authority is not answering. "
                                  "Usually their maintenance; it resumes on its own."),
    "on_blocked_network": ("client", "Paused by the client",
                           "This network is one the client is set not to use."),
    "isOffline": ("client", "Paused by the client", "No network."),
}


def pause_label(p):
    """Words for a pause record, or None when not paused.

    {who: "here"|"client"|"unknown", title, detail, code, until, until_str}.
    `until_str` is local wall-clock, because "until 21:40" is what a person can
    act on and an epoch is not.
    """
    if not p or not p.get("paused"):
        return None
    code = p.get("reason")
    who, title, detail = PAUSE_REASONS.get(
        code, ("unknown", "Paused by the client", "Reason %s." % code if code else ""))
    until = p.get("until")
    return {"who": who, "title": title, "detail": detail, "code": code,
            "until": until,
            "until_str": time.strftime("%H:%M", time.localtime(until)) if until else None}


# ---- milestones -----------------------------------------------------------------
# The first backup passing a quarter, a half, three quarters, and its first
# terabyte. Latched to disk the way completion() is, so each fires once and a set
# that grows later cannot replay it. Shown for a week, then quiet. Plain words.
MILESTONES_MARK = "/config/.bb-milestones"
MILESTONE_FRESH = 7 * 86400
_MILESTONES = (("pct25", "A quarter of the way"),
               ("pct50", "Halfway"),
               ("pct75", "Three quarters of the way"),
               ("tb1", "The first terabyte is up"))


def milestones(data=None):
    """[{key, label, at}] for milestones reached within the last week, or None."""
    b = data if data is not None else backup_totals()
    # Positive evidence only, as completion() demands: no totals, no judgement.
    if not b or not b.get("total") or not b.get("done"):
        return None
    try:
        marks = json.loads(read(MILESTONES_MARK) or "{}")
        if not isinstance(marks, dict):
            marks = {}
    except ValueError:
        marks = {}
    pct = b.get("pct") or 0.0
    # A tebibyte, because human() renders TB at 1024^4 and the banner sits beside
    # a gauge drawn by human(). At a decimal 10^12 the banner announced the first
    # terabyte over a gauge reading 931.3 GB, and the mark is latched to disk so
    # waiting could not correct it.
    reached = {"pct25": pct >= 25, "pct50": pct >= 50, "pct75": pct >= 75,
               "tb1": b["done"] >= 1099511627776}
    now = int(time.time())
    changed = False
    for key, _ in _MILESTONES:
        if reached[key] and key not in marks:
            marks[key] = now
            changed = True
    if changed:
        try:
            with open(MILESTONES_MARK, "w", encoding="utf-8") as fh:
                json.dump(marks, fh)
        except OSError:
            pass
    fresh = [{"key": k, "label": lbl, "at": marks[k]}
             for k, lbl in _MILESTONES
             if k in marks and now - marks[k] <= MILESTONE_FRESH]
    return fresh or None


# ---- progress over time ---------------------------------------------------------
# One sample a day of how far the backup has got, kept for a little over a year.
# The ETA trend already keeps a fortnight of estimates; this keeps the fact the
# estimates are about. After months of watching a bar, the line that shows it
# moving is the thing worth looking at.
PROGRESS_HIST = "/config/.bb-progress-history"
PROGRESS_KEEP = 400


def progress_history(record=True, data=None):
    """[{day: YYYYMMDD, pct}] oldest first, or None. Records today's sample once."""
    try:
        hist = json.loads(read(PROGRESS_HIST) or "{}")
        if not isinstance(hist, dict):
            hist = {}
    except ValueError:
        hist = {}
    if record:
        b = data if data is not None else backup_totals()
        if b and b.get("total") and b.get("pct") is not None:
            today = time.strftime("%Y%m%d")
            if hist.get(today) is None:
                hist[today] = round(b["pct"], 2)
                for day in sorted(hist)[:-PROGRESS_KEEP]:
                    del hist[day]
                try:
                    with open(PROGRESS_HIST, "w", encoding="utf-8") as fh:
                        json.dump(hist, fh)
                except OSError:
                    pass
    if not hist:
        return None
    return [{"day": d, "pct": hist[d]} for d in sorted(hist)]


# ---- per-drive progress ----------------------------------------------------------
# The two stat files carry a bzvolume element per drive alongside the totals, and
# nothing read them until now. On a single-share container this repeats the total;
# on a container backing up several shares it answers which drive is holding the
# backup up, which the totals cannot.
def per_volume():
    """[{guid, path, total, done, remaining, pct, remaining_files}] or None."""
    def by_guid(text, *attrs):
        out = {}
        for a in re.findall(r'<bzvolume ([^/]*)/>', text):
            g = re.search(r'bzVolumeGuid="([^"]*)"', a)
            if not g:
                continue
            vals = []
            for name in attrs:
                m = re.search(name + r'="(\d+)"', a)
                vals.append(int(m.group(1)) if m else None)
            out[g.group(1)] = vals
        return out

    sel = by_guid(read(BZSTAT_TOTAL), "pervol_sel_for_backup_numbytes",
                  "pervol_sel_for_backup_numfiles")
    rem = by_guid(read(BZSTAT_REMAIN), "pervol_remaining_files_numbytes",
                  "pervol_remaining_files_numfiles")
    if not sel:
        return None
    # The mount path lives in a third file, keyed by the same guid when it
    # carries one. Where it does not, the drive is named by the head of its guid
    # rather than left blank: an unlabelled bar is worse than a short label, and
    # the head is the part that differs. Two guids on a live container shared
    # their last six characters and differed by the fourth.
    paths = {}
    for a in re.findall(r'<bzvolume ([^/]*)/>', read(BZ + "/bzvolumes.xml")):
        g = re.search(r'bzVolumeGuid="([^"]*)"', a)
        mp = re.search(r'mountPointPathHex="([0-9a-f]*)"', a)
        if g and mp:
            try:
                paths[g.group(1)] = bytes.fromhex(mp.group(1)).decode("utf-8", "replace")
            except ValueError:
                pass
    out = []
    for guid, (total, tfiles) in sel.items():
        if not total:
            continue
        rbytes, rfiles = rem.get(guid, (0, 0))
        rbytes = min(rbytes or 0, total)
        done = max(0, total - rbytes)
        out.append({"guid": guid, "path": paths.get(guid) or (guid[:10] + "\u2026"),
                    "total": total, "done": done, "remaining": rbytes,
                    "pct": done / total * 100.0,
                    "total_files": tfiles, "remaining_files": rfiles})
    out.sort(key=lambda v: v["total"], reverse=True)
    return out or None


# ---- what is left, against what has gone --------------------------------------
# The client gives no size breakdown of the files still to send, so a histogram
# is not available. The average size of what remains against the average of what
# has gone is available, and it is the figure that explains a long ETA: a
# remainder made of large files takes far longer than its file count suggests.
def remaining_shape(data=None):
    """{avg_remaining, avg_done, ratio} in bytes, or None."""
    b = data if data is not None else backup_totals()
    if not b or not b.get("total_files") or b.get("remaining_files") is None:
        return None
    rem_files = b["remaining_files"]
    done_files = (b["total_files"] or 0) - rem_files
    if rem_files <= 0 or done_files <= 0 or not b.get("done"):
        return None
    avg_rem = (b["total"] - b["done"]) / rem_files
    avg_done = b["done"] / done_files
    return {"avg_remaining": avg_rem, "avg_done": avg_done,
            "ratio": (avg_rem / avg_done) if avg_done else None}


# ---- what limits the rate -------------------------------------------------------
# Wine answers SIO_IDEAL_SEND_BACKLOG_QUERY with a fixed 64 KB and the client
# sizes its send buffer from it, so a connection can have only that much in
# flight and its rate is bounded by buffer divided by round trip. That is the
# finding behind the Wine patches this beta carries, and it is the number nobody
# outside this container knows to look for.
#
# Reported rather than turned into advice. On a live container the observed rate
# has been seen above what this predicts, so the model is a floor of unknown
# tightness, not a ceiling to tune against. Saying which it is beats a
# confident figure that is wrong.
WINE_SNDBUF = 65536


def rate_limits(rate_bytes_per_sec, threads, rtt_ms):
    """{per_thread, modelled, observed, verdict} in bytes/sec, or None."""
    if not rtt_ms or rtt_ms <= 0 or not threads:
        return None
    per_thread = WINE_SNDBUF / (rtt_ms / 1000.0)
    modelled = per_thread * threads
    observed = rate_bytes_per_sec or 0
    if observed > modelled * 1.1:
        verdict = "above"      # more in flight than the stub value implies
    elif observed >= modelled * 0.8:
        verdict = "at"         # the send buffer and the thread count are the limit
    else:
        verdict = "below"      # something else is the limit
    return {"per_thread": per_thread, "modelled": modelled,
            "observed": observed, "verdict": verdict}
