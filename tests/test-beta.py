#!/usr/bin/env python3
"""Tests for the beta overlay: key store, file-name gate, settings whitelist,
log parsing, quiet hours and notifications.

Nothing in rootfs-beta was covered before this. The cases below are the ones
where a silent regression costs something real. A scope check that stops
enforcing hands a read-only key the pause verb. A gap in strip_file_names()
puts the names of someone's files on a key that was never granted them. The
parsers are pure functions of a fixture string and each of them has been wrong
at least once already: the dedup row lost every file whose name contains " - ",
the chunk counter froze at whatever the last burst reached, and the first
terabyte was announced over a gauge reading 931.3 GB.

Nothing here touches /config. Every module constant that hard-codes a path is
repointed at a temporary tree first, which is how the container's own paths are
stood up on a development machine.

Run:  python3 tests/test-beta.py
"""
import calendar, contextlib, importlib.util, io, json, os, sys, tempfile, threading, time
import urllib.error
from importlib.machinery import SourceFileLoader

# The modules under test are imported from the tree that ships in the image, so
# without this the run leaves __pycache__ directories inside rootfs-beta, which
# then land in the build context.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BETA = os.path.join(HERE, "..", "rootfs-beta", "usr", "local")
LIB = os.path.join(BETA, "lib", "bb-monitor")
sys.path.insert(0, LIB)

FIX = tempfile.mkdtemp(prefix="bb-test-beta-")

FAIL = []
XPASS = []


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def raises(fn, *args, **kw):
    """True when the call raises ValueError, which is the whole vocabulary the
    validators and the key store use to refuse something."""
    try:
        fn(*args, **kw)
    except ValueError:
        return True
    return False


# ---- the key store ------------------------------------------------------------------
# Repointed before bbnotify and bbquiet are imported: both build their own file
# names out of bbapi.DIR at import time.
import bbapi

bbapi.DIR = os.path.join(FIX, "bb-api")
bbapi.KEYS = bbapi.DIR + "/keys.json"
bbapi.LOCK = bbapi.DIR + "/.lock"
bbapi.SETTINGS = bbapi.DIR + "/settings.json"

read_rec, read_secret = bbapi.create("read only", ["read"])
pause_rec, pause_secret = bbapi.create("pauser", ["control:pause"])

ok(bbapi.verify(read_secret, "read") == (True, read_rec["id"]),
   "a read key passes its own scope")
ok(bbapi.verify(read_secret, "control:pause") == (False, read_rec["id"]),
   "a read-only key is refused on a control scope")
ok(bbapi.verify(read_secret, "control:backup-now") == (False, read_rec["id"]),
   "a read-only key is refused on the other control scope")
ok(bbapi.verify(read_secret, None) == (True, read_rec["id"]),
   "scope None means any valid key, so the key still passes")

# A tuple is any-of, which is how the tools routes admit either diagnose scope.
ok(bbapi.verify(pause_secret, ("control:backup-now", "control:pause"))
   == (True, pause_rec["id"]), "a tuple scope admits a key holding either one")
ok(bbapi.verify(pause_secret, ("report", "diagnose")) == (False, pause_rec["id"]),
   "a tuple scope refuses a key holding none of them")

# The key id is public by design and is returned on a failure so the attempt can
# be logged; the secret never is. A wrong secret must not authenticate whatever
# the id says.
forged = "bb64_%s_%s" % (read_rec["id"], "A" * 43)
ok(bbapi.verify(forged, "read") == (False, read_rec["id"]),
   "a wrong secret fails and reports the key id it claimed")
ok(bbapi.verify("bb64_%s_%s" % ("f" * 8, "A" * 43), "read") == (False, "f" * 8),
   "an unknown key id fails")
ok(bbapi.verify("not-a-key", "read") == (False, None),
   "an unparseable key reports no id at all")
ok(bbapi.verify("", "read") == (False, None) and bbapi.verify(None, "read") == (False, None),
   "an absent key fails without touching the store")

rev_rec, rev_secret = bbapi.create("doomed", ["read"])
ok(bbapi.verify(rev_secret, "read")[0], "a fresh key authenticates")
bbapi.revoke(rev_rec["id"])
ok(bbapi.verify(rev_secret, "read") == (False, rev_rec["id"]), "a revoked key is refused")

# int(days * 86400) with a small enough number is zero seconds, so the key is
# already past its expiry the moment it exists. That beats sleeping in a test.
exp_rec, exp_secret = bbapi.create("stale", ["read"], expires_in_days=1e-9)
ok(exp_rec["expires"] is not None, "an expiring key records an expiry")
ok(bbapi.verify(exp_secret, "read") == (False, exp_rec["id"]), "an expired key is refused")
ok([r for r in bbapi.listing() if r["id"] == exp_rec["id"]][0]["state"] == "expired",
   "the listing calls it expired rather than active")
ok(exp_rec["id"] not in [r["id"] for r in bbapi.active()],
   "an expired key does not keep the surface alive")

# expand() is the door every scope goes through on the way in. "diagnose" being
# a group as well as a permission would turn a request for the check-only scope
# into permission to run bb-doctor --fix, which writes to the prefix.
ok(bbapi.expand(["diagnose"]) == ["diagnose"],
   "expand(diagnose) does not add diagnose:repair")
ok("diagnose:repair" not in bbapi.expand(["diagnose", "read"]),
   "diagnose alongside another scope still does not add the repair scope")
ok(bbapi.expand(["control"]) == ["control:backup-now", "control:pause"],
   "expand(control) is the two control verbs and nothing else")
ok(bbapi.expand(["read"]) == ["read"], "read is not a group, so it does not add read:files")
dia_rec, dia_secret = bbapi.create("checker", ["diagnose"])
ok(dia_rec["scopes"] == ["diagnose"], "what is stored is the explicit list")
ok(bbapi.verify(dia_secret, "diagnose:repair") == (False, dia_rec["id"]),
   "a diagnose key cannot run the repair verb")
ok(raises(bbapi.create, "bad", ["read", "invent"]), "an unknown permission is refused")
ok(raises(bbapi.create, "bad", []), "a key with no permission is refused")

# The store sits on the user's own appdata share and holds key hashes, so the
# directory is the service's alone. _mutate() tightens it on every write rather
# than _write(), because quiet hours and the endpoints create it first.
os.chmod(bbapi.DIR, 0o755)
with bbapi._mutate():
    pass
ok(oct(os.stat(bbapi.DIR).st_mode & 0o777) == "0o700",
   "_mutate() tightens the key directory to 0700")
ok(oct(os.stat(bbapi.KEYS).st_mode & 0o777) == "0o600", "the key file is owner-only")

# The switch is the other half of the gate: a key that would otherwise pass is
# refused while the API is off, and /api/v1 answers 404 rather than 403.
bbapi.set_enabled(False)
ok(bbapi.verify(read_secret, "read") == (False, read_rec["id"]),
   "a valid key is refused while the API switch is off")
ok(not bbapi.live(), "the surface is not live with the switch off")
bbapi.set_enabled(True)
ok(bbapi.verify(read_secret, "read")[0], "the same key passes again once the switch is on")

# A label is written to the container log, which is the audit trail for the
# control routes, so a newline in it could forge a line there.
long_rec, _ = bbapi.create("a" * 200 + "\nbb-monitor-web: key deadbeef ran pause: ok", ["read"])
ok(len(long_rec["label"]) == 60 and "\n" not in long_rec["label"],
   "a key label is one bounded line")


# ---- the file-name gate on /api/v1/status -------------------------------------------
# bb-monitor-web is a script rather than a module, and its name has a dash in it,
# so it is loaded by path. Loaded under any name but __main__ it defines its
# functions and starts neither the poll thread nor the server.
_web_path = os.path.join(BETA, "bin", "bb-monitor-web")
_spec = importlib.util.spec_from_loader("bbweb", SourceFileLoader("bbweb", _web_path))
bbweb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bbweb)

payload = {
    "ok": True, "rate_bytes_per_sec": 12345,
    "files": [{"name": "tax return 2019.pdf", "pct": 12}],
    "skipped_list": [{"path": "C:\\Jane\\payslip.pdf", "reason": "cannot_read"}],
    "skipped_files": {"total": 120, "top_reason": "cannot_read"},
    "activity": {"state": "Transmitting", "file": "D:\\Music\\Artist - Track.mp3"},
    "client": {"drive_selection": {"D:\\": {"backed_up": True}},
               "excluded_dirs": ["d:\\photos of anna\\"],
               "license": {"status": "billing_active"}},
    "per_volume": [
        {"guid": "{9f1c-0000}", "path": "D:\\", "bytes": 12, "files": 3},
        {"guid": "{aa}", "path": "/mnt/user/Photos of Anna", "bytes": 5, "files": 1},
        {"guid": "{bb}", "path": "e:", "bytes": 1, "files": 1},
    ],
}
stripped = bbweb.strip_file_names(payload)

ok(stripped["files"] is None, "the in-flight file list is dropped")
ok(stripped["skipped_list"] is None, "the skipped list is dropped")
ok(stripped["activity"]["file"] is None, "the activity file name is dropped")
ok(stripped["activity"]["state"] == "Transmitting", "the activity state survives")
ok(stripped["client"]["drive_selection"] is None, "the drive selection is dropped")
ok(stripped["client"]["excluded_dirs"] is None, "the exclusion list is dropped")
ok(stripped["client"]["license"]["status"] == "billing_active",
   "the licence reading survives, naming nobody")
ok(stripped["skipped_files"]["total"] == 120,
   "the skipped count survives: how much is not backed up, without saying what")
ok(payload["files"] is not None and payload["activity"]["file"],
   "the caller's own payload is not mutated")

rows = stripped["per_volume"]
ok(all("guid" not in r for r in rows), "no per_volume row carries a guid")
ok(all(("path" in r) for r in rows), "every per_volume row still has a path field")
ok([r["path"] for r in rows] == ["D:", None, "E:"],
   "a drive root survives uppercased and anything longer becomes null")
ok([r["bytes"] for r in rows] == [12, 5, 1], "the per-drive numbers survive")
# The whole point of the strip is that no path fragment reaches the caller.
blob = json.dumps(stripped)
for term in ["Photos of Anna", "payslip", "tax return", "Artist - Track", "9f1c"]:
    ok(term not in blob, "'%s' does not survive the strip" % term)


# ---- the client-settings whitelist ---------------------------------------------------
import bbconfig

bbconfig.BZCLI = os.path.join(FIX, "no-such-bzcli.exe")

# WRITABLE is the only door. Anything that decides what is backed up is outside
# it, and a request naming one must not reach an argv at all.
_argv_seen = []
bbconfig._run = lambda args: (_argv_seen.append(args) or (True, "set", ""))
for key in ("bzdirfilter_add", "excludefiletypes_add", "backup_schedule_type",
            "lock_PEK", "lock_schedule", "lock_exclusion", ""):
    ok(raises(bbconfig.write, key, "anything"), "write() refuses %r" % key)
    ok(raises(bbconfig.preview, key, "anything"), "preview() refuses %r" % key)
ok(not _argv_seen, "a refused key never reaches an argv")

check_int = bbconfig.WRITABLE["num_backup_threads"]["check"]
ok(check_int(1) == 1 and check_int(100) == 100, "_int_in accepts both ends of its range")
ok(check_int("4") == 4, "_int_in takes the string a form field sends")
ok(raises(check_int, 0) and raises(check_int, 101), "_int_in refuses either side of its range")
ok(raises(check_int, "four") and raises(check_int, None) and raises(check_int, "4; rm -rf /"),
   "_int_in refuses what is not a whole number")
ok(raises(bbconfig.WRITABLE["net_throttle"]["check"], 10001),
   "the throttle ceiling is enforced too")

ok(bbconfig._bool(True) is True and bbconfig._bool(False) is False, "_bool passes a real bool")
ok(bbconfig._bool("true") is True and bbconfig._bool("True") is True,
   "_bool accepts true in either case")
ok(bbconfig._bool("1") is True and bbconfig._bool("0") is False, "_bool accepts 1 and 0")
ok(bbconfig._bool("false") is False and bbconfig._bool("FALSE") is False,
   "_bool accepts false in either case")
ok(raises(bbconfig._bool, "maybe") and raises(bbconfig._bool, "") and raises(bbconfig._bool, "2"),
   "_bool refuses what is neither")
ok(bbconfig._bool("yes") is True and bbconfig._bool("on") is True
   and bbconfig._bool("no") is False and bbconfig._bool("off") is False,
   "_bool reads yes/no and on/off as well, and its message says so")

# The hostname is the one free-text setting, and it becomes half of an argv
# element. A value starting with a dash would make bzcli read it as a flag.
ok(bbconfig._hostname("FozStore") == "FozStore", "a plain hostname passes")
ok(bbconfig._hostname("  Foz Store 2  ") == "Foz Store 2", "surrounding space is trimmed")
ok(raises(bbconfig._hostname, "-rf"), "a value starting with a dash is refused")
ok(raises(bbconfig._hostname, "--print"), "so is a long option")
ok(raises(bbconfig._hostname, "") and raises(bbconfig._hostname, "x" * 65),
   "an empty or over-long hostname is refused")
ok(raises(bbconfig._hostname, "a;b") and raises(bbconfig._hostname, "a$(id)")
   and raises(bbconfig._hostname, "a\nb"), "shell and newline characters are refused")

_argv_seen[:] = []
okw, _ = bbconfig.write("num_backup_threads", "4")
ok(okw and _argv_seen == [["configure", "-v", "num_backup_threads=4"]],
   "a whitelisted key reaches bzcli as one validated argv element")
_argv_seen[:] = []
bbconfig.write("net_auto_throttle", "1")
ok(_argv_seen[0][-1] == "net_auto_throttle=true",
   "a bool is normalised to the client's own spelling before it is sent")

sel = bbconfig.drive_selection({"drive_filters": [
    {"dir": "d:\\", "whichfiles": "all"},
    {"dir": "E:\\", "whichfiles": "none"},
    {"dir": "d:\\photos\\raw\\", "whichfiles": "none"},
    {"dir": "", "whichfiles": "all"},
    "not a dict",
]})
ok(sorted(sel) == ["D:\\", "E:\\"], "drive_selection reads root entries only")
ok(sel["D:\\"]["backed_up"] is True, "a root set to all is backed up")
ok(sel["E:\\"]["backed_up"] is False,
   "a root set to none is not, which is the state behind 'No files are selected'")
ok(bbconfig.drive_selection({"drive_filters": []}) is None, "no filters means no answer")
ok(bbconfig.excluded_dirs({"drive_filters": [
    {"dir": "E:\\", "whichfiles": "none"},
    {"dir": "d:\\photos\\raw\\", "whichfiles": "none"},
    {"dir": "d:\\", "whichfiles": "all"},
]}) == ["E:\\", "d:\\photos\\raw\\"], "excluded_dirs is every entry set to none")

# One sweep at a time. Each path is a Wine start, so four callers arriving on a
# cold cache used to run twenty of them at once on a container short of memory.
_calls = []
_calls_lock = threading.Lock()


def _slow_run(args):
    with _calls_lock:
        _calls.append(args[-1])
    time.sleep(0.2)                       # a Wine start, compressed
    return True, '{"status": "billing_active"}', ""


bbconfig._run = _slow_run
bbconfig.available = lambda: True
bbconfig._cache = {"at": 0, "data": None, "error": None, "started": 0}
_results = []
_threads = [threading.Thread(target=lambda: _results.append(bbconfig.read())) for _ in range(4)]
for t in _threads:
    t.start()
for t in _threads:
    t.join()
ok(len(_calls) == len(bbconfig.PATHS),
   "four concurrent read()s run one sweep, not four (%d bzcli starts for %d paths)"
   % (len(_calls), len(bbconfig.PATHS)))
ok(len({id(r) for r in _results}) == 1, "all four callers get the sweep's own reading")
_before = len(_calls)
bbconfig.read()
ok(len(_calls) == _before, "a read inside the TTL costs no bzcli start")
bbconfig.read(force=True)
ok(len(_calls) == _before + len(bbconfig.PATHS), "force=True runs a full sweep")


# ---- the log parsers -----------------------------------------------------------------
import bbdata

# The row's own separator is " - " and so is part of this file's name, so the
# name has to come off the fixed prefix rather than off the last separator.
bbdata.RPTLOG = os.path.join(FIX, "rptlog")
os.makedirs(bbdata.RPTLOG, exist_ok=True)
ROW = ("2026-09-06 01:02:56 -  small  - throttle x  -  -  dedup - 0 bytes - "
       "D:\\Music\\Artist - Track.mp3\n")
# Both the UTC day file and the local day file, because the rows are UTC-stamped
# and the client's own naming convention for the file is not established.
for mday in {time.gmtime().tm_mday, time.localtime().tm_mday}:
    with open(os.path.join(bbdata.RPTLOG, "%02d.log" % mday), "w", encoding="utf-8") as fh:
        fh.write(ROW)
bbdata._dedup_cache = {"key": None, "names": {}}
names = bbdata.dedup_names()
ok(list(names) == ["Artist - Track.mp3"],
   "a dedup row keeps a file name that contains the row's own separator")
ok(names.get("Artist - Track.mp3") == "01:02:56", "the row's time comes back with it")
rows = [{"small": True, "name": "Artist - Track.mp3"}, {"small": True, "name": "other.mp3"}]
bbdata.mark_dedup(rows)
ok(rows[0].get("dedup") is True and not rows[1].get("dedup"),
   "mark_dedup flags only the file the report names")

# The chunk counter is measured against now. Measured against the newest line in
# the tail it froze at whatever the last burst reached and stayed there for hours
# after uploads stopped, and the modular arithmetic let a line a day old count.
_saved = {n: getattr(bbdata, n) for n in
          ("tail_log", "read", "scan_procs", "client_state", "activity",
           "mem_info", "memory_by_process", "backup_totals", "time")}


class _PinnedClock:
    """The time module with time() pinned, so gather()'s idea of now can be put
    where a test needs it. Everything else is the real module: gather formats
    stamps with it as well as reading the clock."""

    def __init__(self, at):
        self._at = at

    def time(self):
        return self._at

    def __getattr__(self, name):
        return getattr(time, name)


def _push(line):
    return ("2026-09-09 %s - Leaving bztrans_thread_push which_threadStr=00 "
            "elapsedSec=1 numBytes=1048576 bytes" % line)


def _hms(sod):
    sod %= 86400
    return "%02d:%02d:%02d" % (sod // 3600, (sod % 3600) // 60, sod % 60)


def _chunks(lines, at):
    """gather()'s chunks-a-minute figure for a fixed log tail and a fixed now."""
    bbdata.tail_log = lambda n=800: "\n".join(lines) + "\n"
    bbdata.read = lambda p: ""
    bbdata.scan_procs = lambda: (0, [], False, False)
    bbdata.client_state = lambda: (None, None)
    bbdata.activity = lambda: None
    bbdata.mem_info = lambda t: {}
    bbdata.memory_by_process = lambda limit=5: []
    bbdata.backup_totals = lambda *a, **k: None
    bbdata.time = _PinnedClock(at)
    try:
        return bbdata.gather(None)["chunks"]
    finally:
        bbdata.time = _saved["time"]


NOON = calendar.timegm((2026, 9, 9, 12, 0, 0, 0, 0, 0))     # 12:00:00 UTC
noon_sod = 12 * 3600
ok(_chunks([_push(_hms(noon_sod - i)) for i in (50, 40, 30, 20, 10)], NOON) == 5,
   "five completions inside the last minute count five")
ok(_chunks([_push(_hms(noon_sod - 3600 - i)) for i in (50, 40, 30, 20, 10)], NOON) == 0,
   "the same five an hour later count nothing")
ok(_chunks([_push(_hms(noon_sod - (23 * 3600 + 59 * 60)))], NOON) == 0,
   "a line most of a day old does not count as a line a minute old")
ok(_chunks([_push(_hms(noon_sod - 61)), _push(_hms(noon_sod - 59))], NOON) == 1,
   "the window is the last sixty seconds and no more")

# Just past UTC midnight the recent lines are stamped on the other side of it.
MIDNIGHT = calendar.timegm((2026, 9, 9, 0, 0, 20, 0, 0, 0))  # 00:00:20 UTC
ok(_chunks([_push("23:59:50"), _push("23:59:55"), _push("00:00:05")], MIDNIGHT) == 3,
   "completions either side of midnight all count as recent")
ok(_chunks([_push("23:58:00")], MIDNIGHT) == 0,
   "a line two minutes before midnight does not")
for _n, _v in _saved.items():
    setattr(bbdata, _n, _v)

# The banner sits beside a gauge drawn by human(), which renders TB at 1024^4.
# At a decimal 10^12 it announced the first terabyte over a gauge reading
# 931.3 GB, and the mark is latched to disk, so waiting could not correct it.
bbdata.MILESTONES_MARK = os.path.join(FIX, "milestones")
TIB = 1099511627776


def _tb1(done):
    try:
        os.unlink(bbdata.MILESTONES_MARK)
    except OSError:
        pass
    got = bbdata.milestones({"total": TIB * 4, "done": done, "pct": 10.0}) or []
    return "tb1" in [m["key"] for m in got]


ok(_tb1(TIB) is True, "the first-terabyte milestone fires at a tebibyte")
ok(_tb1(TIB - 1) is False, "and not one byte earlier")
ok(_tb1(10 ** 12) is False,
   "a decimal terabyte does not fire it, which is the gauge reading 931.3 GB")
ok(bbdata.human(TIB) == "1.0 TB", "human() agrees on where a terabyte starts")
ok(bbdata.milestones({"total": 0, "done": 0}) is None,
   "no totals means no judgement, as completion() also demands")

# One physical rate, two branches: the measured one divides bytes by seconds, the
# fallback is handed the client's own kbit figure. Dividing by 8192 rather than
# 8388.608 mixed 1000-bit kbits with 1024-based mebibytes and read 2.4% high.
MEASURED = bbdata.rate_str(10 * 1048576, 1)
FALLBACK = bbdata.rate_str(0, 0, 10 * 1048576 * 8 / 1000.0)
ok(MEASURED == "10.00 MB/s", "the measured branch reads 10 MiB/s as 10.00 MB/s")
ok(FALLBACK == MEASURED, "the fallback branch agrees with it exactly (%s)" % FALLBACK)
ok(bbdata.rate_str(0, 0, 0) == "", "no rate and no fallback prints nothing")
ok(bbdata.rate_str(1024, 1) == "8 kbit/s", "a slow rate stays in kbit/s")

# A bundle that spans midnight has a negative span until the day is added back.
r = {"small": False, "chunked": True, "done": 3, "total": 4,
     "bytes": 10 * 1048576, "first": 86390, "last": 10}
ok(bbdata.rec_cols(r) == ("3/4", "10 MB", "0.50 MB/s"),
   "a bundle spanning midnight is rated over 20 seconds, not a negative span")
r_over = dict(r, done=9, total=4)
ok(bbdata.rec_cols(r_over)[0] == "4/4",
   "a bundle whose total went wrong once is clamped rather than counting past it")
ok(bbdata.rec_cols({"small": True, "dedup": True})[1] == "already backed up",
   "a deduplicated small file says so in the web page's own words")
ok(bbdata.rec_cols({"small": True, "dedup": True}, wide=False)[1] == "dedup",
   "and in nine characters for the terminal")


# ---- quiet hours ----------------------------------------------------------------------
import bbquiet

bbquiet.CONF = os.path.join(FIX, "bb-api", "quiet.json")
DAILY = {"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "07:00"}
CONF = {"enabled": True, "windows": [DAILY]}


def _local(y, mo, d, h, mi):
    """Local epoch for a wall clock, with the C library picking the offset. The
    boundaries under test are built the same way, so a test that agreed with the
    code by accident would have to agree with the platform's own zone data too."""
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def _wall(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


# The two days a year the local day is 23 or 25 hours long are exactly the days
# an end built by adding seconds to a start lands an hour off the wall clock the
# user typed. Europe/London because the container's TZ is the user's, and this
# fork's are in the UK.
_tz_before = os.environ.get("TZ")
os.environ["TZ"] = "Europe/London"
time.tzset()
try:
    w, start, end = bbquiet.in_window(CONF, _local(2026, 3, 29, 5, 0))
    ok(w is not None, "05:00 on the spring-forward Sunday is inside a 22:00-07:00 window")
    ok(_wall(start) == "2026-03-28 22:00", "the window started at the 22:00 the user typed")
    ok(_wall(end) == "2026-03-29 07:00",
       "and ends at the 07:00 they typed, not 08:00, across the hour that vanished")
    w, _, nxt = bbquiet.in_window(CONF, _local(2026, 3, 29, 12, 0))
    ok(w is None and _wall(nxt) == "2026-03-29 22:00",
       "the next start that evening is 22:00 local")
    w, _, nxt = bbquiet.in_window(CONF, _local(2026, 10, 25, 12, 0))
    ok(w is None and _wall(nxt) == "2026-10-25 22:00",
       "and 22:00 local on the autumn-back Sunday, across the hour that happened twice")
    w, start, end = bbquiet.in_window(CONF, _local(2026, 10, 25, 5, 0))
    ok(w is not None and _wall(end) == "2026-10-25 07:00",
       "a window running through the repeated hour still ends at 07:00")
finally:
    if _tz_before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _tz_before
    time.tzset()

# A weekday-limited window must not be found on a day it does not list.
MON = {"days": [0], "start": "01:00", "end": "02:00"}
w, _, nxt = bbquiet.in_window({"windows": [MON]}, _local(2026, 6, 10, 1, 30))  # a Wednesday
ok(w is None, "a Monday-only window is not open on a Wednesday")
ok(_wall(nxt) == "2026-06-15 01:00", "and the next start is the following Monday")

sch = bbquiet.Scheduler(lambda name: (True, "ok"), log=lambda m: None)
st = sch.state({"enabled": True, "windows": [DAILY]}, _local(2026, 6, 10, 12, 0))
ok(st["in_window"] is False, "state() says it is outside a window at noon")
ok(st["window_end"] is None,
   "window_end is null outside a window, rather than the next start wearing that name")
ok(st["next_start"] == int(_local(2026, 6, 10, 22, 0)), "next_start carries it instead")
st = sch.state({"enabled": True, "windows": [DAILY]}, _local(2026, 6, 10, 23, 0))
ok(st["in_window"] is True and st["next_start"] is None,
   "inside a window it is the other way round")
ok(st["window_end"] == int(_local(2026, 6, 11, 7, 0)), "and window_end is the window's end")


def _scheduler():
    """A scheduler with the action recorded rather than run. The action itself
    goes to a thread of its own, so the test waits on it: the poll loop must not
    block on a cold Wine start, which is why it is threaded at all."""
    seen = []
    rang = threading.Event()

    def runner(name):
        seen.append(name)
        rang.set()
        return True, "ok"

    return bbquiet.Scheduler(runner, log=lambda m: None), seen, rang


API_RUNNING = {"ok": True, "paused": False}
IN = _local(2026, 6, 10, 22, 5)          # a Wednesday, inside 22:00-07:00
OUT = _local(2026, 6, 11, 8, 0)          # the next morning, outside it

sch, seen, rang = _scheduler()
ok(sch.observe(API_RUNNING, CONF, _local(2026, 6, 10, 21, 0)) is None,
   "an hour before the window nothing happens")
ok(sch.observe(API_RUNNING, CONF, IN) == "paused-now", "entering the window pauses")
ok(rang.wait(5) and seen == ["pause"], "and the action it ran was the pause verb")
ok(sch.last["action"] == "pause" and sch.active,
   "the record for the page is filled in before the action finishes")

sch, seen, rang = _scheduler()
sch.observe(API_RUNNING, CONF, IN)
ok(rang.wait(5), "the pause ran")
rang.clear()
sch.observe(API_RUNNING, CONF, OUT)
ok(rang.wait(5) and seen == ["pause", "backup-now"], "leaving the window resumes")
ok(not sch.active, "and the scheduler stops holding the pause")

# The client's own pause expires after about two hours and it resumes itself;
# inside a window that is re-paused. A person resuming by hand is told apart by
# the deadline the client recorded: before it, somebody did this deliberately,
# and the scheduler holds off until the window ends rather than fighting them.
sch, seen, rang = _scheduler()
sch.observe(API_RUNNING, CONF, IN)
ok(rang.wait(5) and seen == ["pause"], "the window pauses the backup")
until = int(IN + 7200)
paused = {"ok": True, "paused": True, "pause": {"until": until}}
ok(sch.observe(paused, CONF, IN + 120) == "paused", "while it holds, nothing more is done")
ok(sch.paused_until == until, "the client's own deadline is remembered")
ok(sch.observe(API_RUNNING, CONF, IN + 300) == "override",
   "a resume well before that deadline reads as a person, not the deadline passing")
ok(sch.override_until == _local(2026, 6, 11, 7, 0), "the override runs to the window's end")
ok(sch.observe(API_RUNNING, CONF, IN + 400) == "override",
   "and a later poll inside the window does not pause again")
ok(seen == ["pause"], "so the pause verb was issued once, not twice")
ok(sch.observe(API_RUNNING, CONF, _local(2026, 6, 11, 7, 30)) is None,
   "the override lapses with the window")

# Past the deadline the client resumed itself, which is the case that is re-paused.
sch, seen, rang = _scheduler()
sch.observe(API_RUNNING, CONF, IN)
rang.wait(5)
sch.observe({"ok": True, "paused": True, "pause": {"until": int(IN + 120)}}, CONF, IN + 60)
rang.clear()
ok(sch.observe(API_RUNNING, CONF, IN + 300) == "repaused",
   "a resume after the client's own deadline is the client, so the pause is re-issued")
ok(rang.wait(5) and seen == ["pause", "pause"], "and the pause verb runs a second time")

sch, seen, rang = _scheduler()
sch.observe(API_RUNNING, CONF, IN)
rang.wait(5)
rang.clear()
ok(sch.observe(API_RUNNING, {"enabled": False, "windows": [DAILY]}, IN + 200) is None,
   "turning quiet hours off inside a window resumes")
ok(rang.wait(5) and seen == ["pause", "backup-now"], "with the backup-now verb")
ok(sch.observe({"ok": False}, CONF, IN) is None, "a payload that is not ok is not acted on")
ok(sch.observe(None, CONF, IN) is None, "nor is no payload at all")


# ---- the timeline summary line --------------------------------------------------------
# A multi-part row's time stamp moves with every part and its bytes accumulate, so a
# summary that keyed rows on (name, time) counted each part as a new file carrying the
# running total. A live container reported 11.5 TB for a spell that sent about 1 TB.
import bbtimeline
_PART = 35 * 1048576
def _tx(rows):
    return {"ok": True, "state": "Transmitting", "rate_bytes_per_sec": 4e6, "threads": 4,
            "files": {"recent": rows}}
def _bundle(i, total=6):
    return {"name": "D:\\big.mkv", "time": "12:%02d:00" % i, "chunked": True,
            "parts": {"done": i, "total": total}, "bytes": _PART * i, "seconds": 60 * i}
tl = bbtimeline.Timeline()
# The state turns to Transmitting seconds before the first part lands, so the
# poll that opens a spell carries no fresh rows.
tl.observe(_tx([]), now=1000)
for i in range(1, 7):
    tl.observe(_tx([_bundle(i)]), now=1000 + 60 * i)
tl.observe(_tx([_bundle(6), {"name": "D:\\small.jpg", "time": "12:07:00", "chunked": False,
                             "bytes": 2 * 1048576, "seconds": 1, "kbit_per_sec": 1,
                             "measured": True}]), now=1420)
tl.observe({"ok": True, "state": "Idle"}, now=1600)
line = [e["note"] for e in tl.listing() if e["note"]][-1]
ok(line.startswith("Uploaded 2 files (212.0 MB)"),
   "a six-part file is one file at its final size, not six at a running total: %r" % line)
# A bundle already landing parts when the spell starts contributes only its growth.
tl = bbtimeline.Timeline()
tl.observe(_tx([_bundle(2)]), now=2000)                 # spell opens with 2 parts down
tl.observe(_tx([_bundle(5)]), now=2060)
tl.observe({"ok": True, "state": "Idle"}, now=2200)
line = [e["note"] for e in tl.listing() if e["note"]][-1]
ok(line.startswith("Uploaded 1 file (105.0 MB)"),
   "a bundle in flight at spell start counts only the parts landed since: %r" % line)
tl = bbtimeline.Timeline()
tl.observe(_tx([_bundle(6)]), now=3000)                 # already complete: earlier spell's
tl.observe(_tx([_bundle(6)]), now=3060)
tl.observe({"ok": True, "state": "Idle"}, now=3200)
line = [e["note"] for e in tl.listing() if e["note"]][-1]
ok(line.startswith("Uploaded 0 files in"),
   "a bundle finished before the spell is not counted: %r" % line)

# ---- notifications ----------------------------------------------------------------------
import bbnotify

bbnotify.CONF = os.path.join(FIX, "bb-api", "notify.json")
bbnotify.STATE = os.path.join(FIX, "bb-api", "notify-state.json")
bbnotify.BB_HEALTH = os.path.join(FIX, "no-such-bb-health")

# An endpoint is the one place a person can make the container issue an outbound
# request with a body and headers of their choosing, so the addresses that mean
# "me" are closed before a socket is ever opened. Numeric hosts throughout: this
# test resolves nothing and connects to nothing.
_opened = []
_real_urlopen = bbnotify.urllib.request.urlopen


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_stub(answer):
    def stub(req, timeout=None):
        _opened.append(req.full_url)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)
    return stub


def _send(url, answer=204, kind="webhook"):
    ep = {"id": "e1", "label": "test", "kind": kind, "url": url, "auth": "none"}
    bbnotify.urllib.request.urlopen = _urlopen_stub(answer)
    log = io.StringIO()
    try:
        with contextlib.redirect_stderr(log):
            res = bbnotify.send_once(ep, "frozen", "Title", "Message", {"event": "frozen"})
    finally:
        bbnotify.urllib.request.urlopen = _real_urlopen
    return res, log.getvalue()


VOCABULARY = ("delivered", "rejected by the endpoint", "not reachable")

for url, what in [("http://127.0.0.1:8099/hook", "loopback"),
                  ("http://[::1]:8099/hook", "IPv6 loopback"),
                  ("http://[::ffff:127.0.0.1]/hook", "loopback wearing an IPv6 shape"),
                  ("http://169.254.169.254/latest/meta-data/", "the metadata service"),
                  ("http://0.0.0.0/hook", "the unspecified address")]:
    _opened[:] = []
    (sent, detail), _log = _send(url)
    ok(sent is False, "send_once refuses %s" % what)
    ok(not _opened, "and opens no connection to %s" % what)
    ok(detail.startswith("refused: "), "saying it refused rather than what answered")

_opened[:] = []
(sent, detail), _log = _send("http://192.168.1.50:8080/hook")
ok(sent and detail == "delivered", "a LAN endpoint is allowed, because ntfy is usually on one")
ok(_opened == ["http://192.168.1.50:8080/hook"], "and it is the URL that was checked")

# The detail is read back through GET /manage/notify, so a status code per host
# would be a probe of whatever the URL points at. The number goes to the log.
(sent, detail), log = _send("http://192.0.2.10/hook", answer=403)
ok(sent is False and detail == "rejected by the endpoint", "a refusal says only that")
ok("403" not in detail and "HTTP 403" in log, "the code goes to the container log instead")
(sent, detail), log = _send("http://192.0.2.10/hook",
                            answer=urllib.error.URLError("Connection refused"))
ok(sent is False and detail == "not reachable", "an unreachable endpoint says only that")
ok("Connection refused" in log, "with the reason in the log")
(sent, detail), log = _send("http://192.0.2.10/hook",
                            answer=urllib.error.HTTPError("u", 401, "no", {}, None))
ok(sent is False and detail == "rejected by the endpoint", "an HTTP error is the same word")
_details = [_send("http://192.0.2.10/hook", answer=a)[0][1] for a in (204, 200, 403)]
ok(all(d in VOCABULARY for d in _details),
   "every detail from a reachable endpoint is one of the three fixed words")
ok(_send("http://127.0.0.1/x")[0][1].split(":")[0] == "refused",
   "and a blocked one is a refused: string")

# The store is on the user's own appdata share and can be unwritable. observe()
# rebuilds its baseline from disk on every poll, so a store that cannot be
# written at all used to mean prev was always None and nothing ever fired again,
# silently. A directory whose parent is a file cannot be created by anyone,
# including root, which is what makes this reproduce wherever the tests run.
_broken = os.path.join(FIX, "a-file-not-a-directory")
with open(_broken, "w", encoding="utf-8") as fh:
    fh.write("x")
_dir_before = bbapi.DIR
bbapi.DIR = os.path.join(_broken, "bb-api")
bbnotify.STATE = os.path.join(bbapi.DIR, "notify-state.json")
bbnotify._state_mem.clear()
bbnotify._state_warned = False
NCONF = {"endpoints": [], "events": {k: True for k, _, _, _ in bbnotify.EVENTS},
         "skipped_threshold": 1}


def _api(frozen):
    return {"ok": True, "health": [{"kind": "frozen"}] if frozen else [],
            "skipped_files": {"total": 0}, "paused": False, "build": "1"}


_delivered = []
_log = io.StringIO()
with contextlib.redirect_stderr(_log):
    first = bbnotify.observe(_api(False), NCONF, deliver=lambda *a: _delivered.append(a))
    second = bbnotify.observe(_api(True), NCONF, deliver=lambda *a: _delivered.append(a))
    third = bbnotify.observe(_api(True), NCONF, deliver=lambda *a: _delivered.append(a))
    fourth = bbnotify.observe(_api(False), NCONF, deliver=lambda *a: _delivered.append(a))
ok(not os.path.exists(bbnotify.STATE), "the state file really could not be written")
ok(first == [], "the first observation records a baseline and fires nothing")
ok([f[0] for f in second] == ["frozen"], "a freeze still fires with the store unwritable")
ok(third == [], "and does not fire again while it holds")
ok([f[0] for f in fourth] == ["frozen"], "the clearing event fires too")
ok(_log.getvalue().count("notification state cannot be saved") == 1,
   "the unwritable store is reported once, not once per poll")
ok("frozen" in _delivered[0][1], "the fired event reaches the delivery function")
bbapi.DIR = _dir_before

# The same sequence with a working store, so the disk path is covered as well.
bbnotify.STATE = os.path.join(FIX, "bb-api", "notify-state.json")
bbnotify._state_mem.clear()
try:
    os.unlink(bbnotify.STATE)
except OSError:
    pass
ok(bbnotify.observe(_api(False), NCONF, deliver=lambda *a: None) == [],
   "a writable store also fires nothing on the first observation")
ok(os.path.exists(bbnotify.STATE), "and the baseline is on disk")
_mtime = os.stat(bbnotify.STATE).st_mtime_ns
ok(bbnotify.observe(_api(False), NCONF, deliver=lambda *a: None) == [],
   "an identical poll fires nothing")
ok(os.stat(bbnotify.STATE).st_mtime_ns == _mtime,
   "and does not rewrite the store, which was 43,000 atomic replaces a day")

# Nothing that names a file may leave the container, whatever the payload holds.
msg = bbnotify._message("skipped", {"skipped_total": 120},
                        {"skipped_files": {"total": 120, "top_reason": "CANNOT_READ",
                                           "sample": "C:\\Jane\\payslip.pdf"}})
ok("payslip" not in msg and "120 files skipped" in msg,
   "the skipped message carries a count and a reason, never a path")

print()
if XPASS:
    print("%d expected failures now pass and should become plain assertions:" % len(XPASS))
    for m in XPASS:
        print("  " + m)
print("%d failures" % (len(FAIL) + len(XPASS)))
sys.exit(1 if (FAIL or XPASS) else 0)
