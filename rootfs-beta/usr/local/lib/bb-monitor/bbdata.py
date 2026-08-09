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
import calendar, ipaddress, os, re, socket, struct, threading, time, unicodedata
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


def rec_cols(r):
    if r["chunked"]:
        span = r["last"] - r["first"]
        if span < 0:
            span += 86400
        return ("%d/%d" % (r["done"], r["total"]), human(r["bytes"]), rate_str(r["bytes"], span))
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
    o["state"] = ("Transmitting" if threads else
                  "Scanning" if has_fl else
                  "Preparing" if has_bt else "Idle")

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
        part = int(mp.group(1))
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
        files.append((name, part, fsize, pct))
        cur[x] = (name, fsize, part, thr, newest_line(thr))
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


