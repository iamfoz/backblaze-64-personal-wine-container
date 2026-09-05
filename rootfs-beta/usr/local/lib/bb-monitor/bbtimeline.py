# The last 24 hours, as a list a person can read: every change of state, and
# after each spell of uploading, one line saying what it amounted to.
#
# The state changes come free from the poll loop. The summary line is the part
# that needs a little care: the client's state flickers between Transmitting and
# its neighbours while it works through small files, so a spell of uploading is
# not "state == Transmitting" but "Transmitting, allowing gaps of up to a
# minute". Splitting on every flicker would give a log of one-minute spells that
# says nothing; one line per real spell says how the backup is doing.
#
# Files and bytes are counted from the completed transfers the monitor already
# tracks, each seen once. Rate, threads, memory and swap are means over the
# samples taken while uploading.

import time

KEEP = 24 * 3600
GAP = 60             # seconds without Transmitting before a spell is over
_TX = ("Transmitting",)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0


def span(seconds):
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m if m else "%ds" % seconds


class Timeline:
    def __init__(self):
        self.entries = []      # [{at, state, note}], oldest first
        self.last_state = None
        self.period = None

    def observe(self, api, now=None):
        """Feed one API payload. Returns the entries added."""
        if not api or not api.get("ok"):
            return []
        now = now if now is not None else time.time()
        added = []
        state = api.get("state")
        if state != self.last_state:
            added.append({"at": int(now), "state": state, "note": None})
            self.last_state = state
        added += self._track(api, state, now)
        self.entries += added
        while self.entries and now - self.entries[0]["at"] > KEEP:
            self.entries.pop(0)
        return added

    def _track(self, api, state, now):
        p = self.period
        if state in _TX:
            if p is None:
                p = self.period = {"start": now, "last": now, "n": 0, "rate": 0.0,
                                   "threads": 0, "mem": 0.0, "mem_n": 0,
                                   "swap": 0.0, "swap_n": 0, "seen": set(),
                                   "files": 0, "bytes": 0}
            p["last"] = now
            p["n"] += 1
            p["rate"] += api.get("rate_bytes_per_sec") or 0
            p["threads"] += api.get("threads") or 0
            mem = api.get("memory") or {}
            if mem.get("used_bytes") is not None:
                p["mem"] += mem["used_bytes"]
                p["mem_n"] += 1
            sw = api.get("swap") or {}
            if sw.get("total_bytes"):
                p["swap"] += sw.get("used_bytes") or 0
                p["swap_n"] += 1
            for r in ((api.get("files") or {}).get("recent") or []):
                key = (r.get("name"), r.get("time"))
                if key not in p["seen"]:
                    p["seen"].add(key)
                    p["files"] += 1
                    p["bytes"] += r.get("bytes") or 0
            return []
        if p is not None and now - p["last"] > GAP:
            self.period = None
            return [self._summary(p)]
        return []

    def _summary(self, p):
        n = max(1, p["n"])
        parts = ["Uploaded %d file%s" % (p["files"], "" if p["files"] == 1 else "s")]
        if p["bytes"]:
            parts[0] += " (%s)" % human(p["bytes"])
        parts[0] += " in %s" % span(p["last"] - p["start"])
        avgs = ["%.1f Mbit/s" % (p["rate"] / n * 8 / 1e6),
                "%.1f threads" % (p["threads"] / n)]
        if p["mem_n"]:
            avgs.append("mem %s" % human(p["mem"] / p["mem_n"]))
        if p["swap_n"]:
            avgs.append("swap %s" % human(p["swap"] / p["swap_n"]))
        return {"at": int(p["last"]), "state": None,
                "note": parts[0] + ": average " + ", ".join(avgs)}

    def listing(self):
        return list(self.entries)
