# Quiet hours: pause windows on a weekly schedule.
#
# The client has a schedule, but it is a backup schedule and not a pause
# schedule, and it lives in a window that renders badly under Wine. This is the
# one control the API already has, given a clock. It uses only the two
# whitelisted verbs: pause at the start of a window, backup-now at the end.
#
# The client's own pause carries a deadline of about two hours and then resumes
# by itself. Inside a window the scheduler re-issues the pause when that happens,
# and says so in the log. A resume the user asked for is different: it holds
# until the window ends. The two are told apart by the deadline the client
# recorded: past it, the client resumed itself; before it, someone did.

import json, os, sys, tempfile, threading, time

import bbapi

CONF = bbapi.DIR + "/quiet.json"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")   # Python weekday(): Mon=0
SETTLE = 90        # seconds to let a pause take before judging whether it held
_lock = threading.Lock()


def default():
    return {"enabled": False, "windows": []}


def load():
    try:
        with open(CONF, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return default()
    if not isinstance(c, dict):
        return default()
    return {"enabled": bool(c.get("enabled")),
            "windows": [w for w in (c.get("windows") or []) if _valid(w)]}


def _hm(s):
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError
    return h * 60 + m


def _valid(w):
    try:
        days = sorted({int(d) for d in w.get("days", [])})
        return bool(days) and all(0 <= d <= 6 for d in days) \
            and _hm(w["start"]) != _hm(w["end"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def save(conf):
    """Validate and store. Raises ValueError with a message for the page."""
    ws = conf.get("windows") or []
    if not isinstance(ws, list):
        raise ValueError("windows must be a list")
    out = []
    for w in ws:
        if not isinstance(w, dict):
            raise ValueError("each window must be an object")
        try:
            days = sorted({int(d) for d in w.get("days", [])})
            start, end = w["start"].strip(), w["end"].strip()
            _hm(start); _hm(end)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("a window needs days and start/end times as HH:MM")
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("days must be 0 (Monday) to 6 (Sunday)")
        if _hm(start) == _hm(end):
            raise ValueError("a window cannot start and end at the same time")
        out.append({"days": days, "start": start, "end": end})
    rec = {"enabled": bool(conf.get("enabled")), "windows": out}
    with bbapi._mutate():
        os.makedirs(bbapi.DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=bbapi.DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
            os.chmod(tmp, 0o600)
            bbapi._own_like_config(tmp)
            os.replace(tmp, CONF)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return rec


def in_window(conf, now=None):
    """(window, start_epoch, end_epoch) for the window containing `now`, else
    (None, None, next_start_epoch or None). Local time, which is the container's
    TZ, which is what the user set the times in."""
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    today_midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    best_next = None
    for w in conf.get("windows") or []:
        s, e = _hm(w["start"]), _hm(w["end"])
        dur = (e - s) if e > s else (e - s + 1440)
        # The occurrence that started most recently on each listed weekday,
        # looking back a week, plus the next one ahead for the "next change".
        for back in range(0, 8):
            day_mid = today_midnight - back * 86400
            wd = time.localtime(day_mid).tm_wday
            if wd not in w["days"]:
                continue
            start = day_mid + s * 60
            end = start + dur * 60
            if start <= now < end:
                return w, start, end
        for ahead in range(0, 8):
            day_mid = today_midnight + ahead * 86400
            wd = time.localtime(day_mid).tm_wday
            if wd not in w["days"]:
                continue
            start = day_mid + s * 60
            if start > now and (best_next is None or start < best_next):
                best_next = start
    return None, None, best_next


class Scheduler:
    """Drives the pause from the poll loop. observe() is called with each API
    payload; `runner(name)` issues a whitelisted action and returns (ok, msg)."""

    def __init__(self, runner, log=None):
        self.runner = runner
        self.log = log or (lambda m: sys.stderr.write("bb-monitor-web: quiet hours: %s\n" % m))
        self.active = False          # we set the current pause
        self.override_until = None   # a person resumed inside a window
        self.paused_until = None     # the client's deadline for the pause we set
        self.acted_at = 0
        self.last = None             # last action, for the page

    def observe(self, api, conf=None, now=None):
        conf = conf or load()
        now = now if now is not None else time.time()
        if not api or not api.get("ok"):
            return None
        paused = bool(api.get("paused"))
        w, start, end = in_window(conf, now)
        if not conf.get("enabled"):
            if self.active:
                self._act("backup-now", "disabled, resuming", now)
                self.active = False
            self.override_until = None
            return None
        if w is None:
            self.override_until = None
            if self.active:
                self._act("backup-now", "window ended, resuming", now)
                self.active = False
                self.paused_until = None
            return None
        # Inside a window.
        if self.override_until and now < self.override_until:
            return "override"
        self.override_until = None
        if paused:
            if self.active:
                self.paused_until = (api.get("pause") or {}).get("until") or self.paused_until
            return "paused"
        if not self.active:
            if now - self.acted_at < SETTLE:
                return "settling"
            self._act("pause", "window %s-%s started, pausing" % (w["start"], w["end"]), now)
            self.active = True
            return "paused-now"
        # We set a pause and it is no longer in force.
        if now - self.acted_at < SETTLE:
            return "settling"
        if self.paused_until and now < self.paused_until - 60:
            # Before the client's own deadline: a person resumed it. Hold off
            # until the window ends rather than fighting them.
            self.override_until = end
            self.active = False
            self.log("resumed by hand inside a window; not pausing again until %s"
                     % time.strftime("%H:%M", time.localtime(end)))
            return "override"
        self._act("pause", "the client resumed at its deadline, pausing again", now)
        return "repaused"

    def _act(self, name, why, now):
        ok, msg = self.runner(name)
        self.acted_at = now
        self.last = {"action": name, "ok": ok, "at": int(now), "why": why}
        self.log("%s: %s -> %s" % (why, name, msg if ok else "FAILED: " + msg))

    def state(self, conf=None, now=None):
        conf = conf or load()
        now = now if now is not None else time.time()
        w, start, end = in_window(conf, now)
        return {"enabled": conf.get("enabled", False), "in_window": w is not None,
                "window_end": int(end) if end else None,
                "next_start": None if w else (int(end) if end else None),
                "active": self.active,
                "override_until": int(self.override_until) if self.override_until else None,
                "last": self.last}
