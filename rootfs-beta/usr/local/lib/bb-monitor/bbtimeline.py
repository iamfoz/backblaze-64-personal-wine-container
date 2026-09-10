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
# tracks. Rows present when the spell starts belong to an earlier one and are
# not counted; a multi-part file's bytes are taken as they stand at the end, not
# at first sight; and a small file the datacentre already held is counted as
# checked, not as uploaded. A multi-part row is keyed by name alone: its time
# stamp moves on with every part and its byte count is cumulative, so keying it
# by (name, time) made every part a new file carrying the whole running total,
# and a day of large files summed to ten times what was sent. Rate, threads, memory and swap are means over the
# samples taken while uploading. A long spell gets a line every hour so a day of
# uploading is not one line at the end of it.

import time

KEEP = 24 * 3600
GAP = 60             # seconds without Transmitting before a spell is over
REPORT_EVERY = 3600  # seconds between "so far" lines inside a spell
# Both spellings, matching bbdata's own list: the state is whatever the client
# wrote in cur_state, capitalised, and a client reporting "uploading" would
# otherwise leave every spell unopened and the panel with no summary line.
_TX = ("Transmitting", "Uploading")


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

    @staticmethod
    def _rows(api):
        return ((api.get("files") or {}).get("recent") or [])

    @staticmethod
    def _key(r):
        # One row per multi-part file, whatever its time stamp says this poll.
        if r.get("chunked"):
            return (r.get("name"), None)
        return (r.get("name"), r.get("time"))

    def _track(self, api, state, now):
        p = self.period
        if state in _TX:
            if p is None:
                # Rows already in the list finished before this spell: remember
                # them so they are not counted as this spell's work.
                before = {self._key(r): (r.get("bytes") or 0) for r in self._rows(api)}
                p = self.period = {"start": now, "last": now, "reported": now,
                                   "n": 0, "rate": 0.0, "threads": 0,
                                   "mem": 0.0, "mem_n": 0, "swap": 0.0, "swap_n": 0,
                                   "before": before, "rows": {}, "held": set()}
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
            for r in self._rows(api):
                key = self._key(r)
                nbytes = r.get("bytes") or 0
                if key in p["before"]:
                    # A bundle still landing parts when the spell began: only
                    # what it has gained since is this spell's work. Anything
                    # else that was already listed finished before the spell.
                    if not r.get("chunked") or nbytes <= p["before"][key]:
                        continue
                    nbytes -= p["before"][key]
                if r.get("dedup"):
                    p["held"].add(key)
                    continue
                # Latest figure wins: a multi-part row's bytes grow as parts land.
                p["rows"][key] = nbytes
            if now - p["reported"] >= REPORT_EVERY:
                p["reported"] = now
                return [self._summary(p, now, so_far=True)]
            return []
        if p is not None and now - p["last"] > GAP:
            self.period = None
            return [self._summary(p, p["last"])]
        return []

    def _summary(self, p, at, so_far=False):
        n = max(1, p["n"])
        files, held = len(p["rows"]), len(p["held"])
        total = sum(p["rows"].values())
        what = "%d file%s" % (files, "" if files == 1 else "s")
        if total:
            what += " (%s)" % human(total)
        elapsed = span(at - p["start"])
        head = ("Uploading for %s: %s so far" % (elapsed, what) if so_far
                else "Uploaded %s in %s" % (what, elapsed))
        if held:
            head += ", %d already backed up" % held
        avgs = ["%.1f Mbit/s" % (p["rate"] / n * 8 / 1e6),
                "%.1f threads" % (p["threads"] / n)]
        if p["mem_n"]:
            avgs.append("mem %s" % human(p["mem"] / p["mem_n"]))
        if p["swap_n"]:
            avgs.append("swap %s" % human(p["swap"] / p["swap_n"]))
        return {"at": int(at), "state": None, "note": head + ": average " + ", ".join(avgs)}

    def listing(self):
        return list(self.entries)
