# Outbound notifications: the container tells the user, on a device they carry,
# when the backup needs them.
#
# Everything here is detected already and shown on a page nobody has open. A
# safety freeze in one support thread was found by accident days later. The fix
# is not a louder page; it is a message to somewhere the user already looks.
#
# Two rules. An event fires once when its condition becomes true and once when
# it clears, never on every poll, and the conditions are remembered on disk so a
# service restart does not replay them. And nothing that names a file ever
# leaves the container: the skipped-files event carries a count and a reason,
# not a path.
#
# Two endpoint shapes cover most of the world. An ntfy topic URL takes the
# message as the body with the title in a header, and Gotify, Pushover-style
# bridges and Apprise accept that or the other: a webhook that receives JSON,
# which Home Assistant, Discord, Slack and anything scripted can take.
#
# Delivery is best effort: three attempts, then the failure is logged. There is
# no queue on disk. A stall notification an hour late is still worth having; one
# a week late is not.

import base64, json, os, secrets, socket, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request

import bbapi

CONF = bbapi.DIR + "/notify.json"
STATE = bbapi.DIR + "/notify-state.json"

KINDS = ("ntfy", "webhook")

# (key, label, what it means, fires when it clears too)
EVENTS = (
    ("frozen",        "Safety freeze",
     "Backblaze has frozen the backup. Nothing is deleted; it needs a person.", True),
    ("skipped",       "Files skipped",
     "The client has given up on files. Fires when the count reaches the threshold.", True),
    ("stale",         "No completed backup",
     "No pass has completed within the limit set in the client's own settings.", True),
    ("stalled",       "Backup stalled",
     "bb-health reports a HANG or a WEDGE.", True),
    ("client_paused", "Paused by the client",
     "The client paused itself, for example during Backblaze maintenance.", True),
    ("completion",    "First backup complete", "The first backup has caught up.", False),
    ("milestone",     "Milestone",
     "A quarter, half, three quarters of the way, or the first terabyte.", False),
    ("build",         "Container updated",
     "The container is running a different build than it was.", False),
)
URGENT = ("frozen", "stalled")

RETRY_AFTER = (0, 5, 15)      # seconds before each attempt
TIMEOUT = 10
HEALTH_EVERY = 60             # seconds between bb-health runs for "stalled"
BB_HEALTH = "/usr/local/bin/bb-health"

_lock = threading.Lock()
_health = {"verdict": None, "at": 0}


def default():
    return {"endpoints": [], "events": {k: True for k, _, _, _ in EVENTS},
            "skipped_threshold": 1}


def load():
    try:
        with open(CONF, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return default()
    d = default()
    if isinstance(c, dict):
        d["endpoints"] = [e for e in c.get("endpoints", []) if isinstance(e, dict)]
        d["events"].update({k: bool(v) for k, v in (c.get("events") or {}).items()
                            if k in d["events"]})
        try:
            d["skipped_threshold"] = max(1, int(c.get("skipped_threshold", 1)))
        except (TypeError, ValueError):
            pass
    return d


def _write(path, obj):
    os.makedirs(bbapi.DIR, exist_ok=True)
    bbapi._own_like_config(bbapi.DIR)
    fd, tmp = tempfile.mkstemp(dir=bbapi.DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)          # tokens live here in the clear; owner only
        bbapi._own_like_config(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(conf):
    """Validate and store. Raises ValueError with a message for the page."""
    out = default()
    eps = conf.get("endpoints") or []
    if not isinstance(eps, list):
        raise ValueError("endpoints must be a list")
    # The page never sees a stored token, so a blank token on an endpoint it
    # already knows means "keep what is there", not "remove it".
    kept = {e["id"]: e.get("token", "") for e in load()["endpoints"] if e.get("id")}
    for e in eps:
        if not isinstance(e, dict):
            raise ValueError("each endpoint must be an object")
        kind = (e.get("kind") or "").strip()
        url = (e.get("url") or "").strip()
        if kind not in KINDS:
            raise ValueError("endpoint kind must be one of %s" % ", ".join(KINDS))
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("endpoint URL must start with http:// or https://")
        auth = (e.get("auth") or "none").strip()
        if auth not in ("none", "bearer", "basic"):
            raise ValueError("auth must be none, bearer or basic")
        rec = {"id": e.get("id") or secrets.token_hex(4),
               "label": (e.get("label") or "").strip()[:60] or kind,
               "kind": kind, "url": url, "auth": auth,
               "token": (e.get("token") or "").strip() or kept.get(e.get("id") or "", ""),
               "user": (e.get("user") or "").strip()}
        out["endpoints"].append(rec)
    out["events"].update({k: bool(v) for k, v in (conf.get("events") or {}).items()
                          if k in out["events"]})
    try:
        out["skipped_threshold"] = max(1, int(conf.get("skipped_threshold", 1)))
    except (TypeError, ValueError):
        raise ValueError("skipped threshold must be a whole number")
    with bbapi._mutate():
        _write(CONF, out)
    return out


def public(conf):
    """The configuration for display: tokens replaced by whether one is set."""
    c = json.loads(json.dumps(conf))
    for e in c["endpoints"]:
        e["has_token"] = bool(e.get("token"))
        e["token"] = ""
    c["event_list"] = [{"key": k, "label": l, "does": d, "clears": c_}
                       for k, l, d, c_ in EVENTS]
    return c


# ---- what is true right now ------------------------------------------------------

def _state_load():
    try:
        with open(STATE, encoding="utf-8") as fh:
            st = json.load(fh)
            if isinstance(st, dict):
                return st
    except (OSError, ValueError):
        pass
    return {}


def _state_save(st):
    try:
        with bbapi._mutate():
            _write(STATE, st)
    except OSError:
        pass


def health_verdict():
    """bb-health's first line, refreshed at most once a minute, in its own thread
    so the poll loop never waits on a shell script."""
    now = time.time()
    with _lock:
        stale = now - _health["at"] > HEALTH_EVERY
        if stale:
            _health["at"] = now
    if stale:
        threading.Thread(target=_run_health, daemon=True).start()
    return _health["verdict"]


def _run_health():
    try:
        p = subprocess.run([BB_HEALTH], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=30)
        line = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        v = line[0] if line else None
    except (OSError, subprocess.TimeoutExpired):
        v = None
    with _lock:
        _health["verdict"] = v


def conditions(api, health=None):
    """The current truth of each condition from an API payload."""
    kinds = {h.get("kind") for h in (api.get("health") or [])}
    sk = api.get("skipped_files") or {}
    pl = api.get("pause_label") or {}
    hv = health or ""
    return {
        "frozen": "frozen" in kinds,
        "skipped_total": int(sk.get("total") or 0),
        "stale": "stale" in kinds,
        "stalled": hv.startswith("HANG") or hv.startswith("WEDGE"),
        "client_paused": bool(api.get("paused")) and pl.get("who") == "client",
        "completion": bool(api.get("completion")),
        "milestones": sorted(m["key"] for m in (api.get("milestones") or [])),
        "build": api.get("build"),
        "health_line": hv or None,
    }


def observe(api, conf=None, deliver=None, now=None):
    """Compare the payload with what was last seen; fire what changed.

    Returns the list of events fired, for tests and for the log. The first
    observation records a baseline and fires nothing, except a build change
    against a build recorded before the restart: that one is the point.
    """
    if not api or not api.get("ok"):
        return []
    conf = conf or load()
    deliver = deliver or fire
    now = now or time.time()
    cur = conditions(api, health_verdict() if BB_HEALTH and os.path.exists(BB_HEALTH) else None)
    st = _state_load()
    prev = st.get("conditions")
    fired = []
    thr = conf["skipped_threshold"]
    cur_flags = {
        "frozen": cur["frozen"], "skipped": cur["skipped_total"] >= thr,
        "stale": cur["stale"], "stalled": cur["stalled"],
        "client_paused": cur["client_paused"], "completion": cur["completion"],
    }
    if prev is not None:
        labels = {k: (l, d, c) for k, l, d, c in EVENTS}
        for key, on in cur_flags.items():
            was = bool(prev.get(key))
            if not conf["events"].get(key):
                continue
            if on and not was:
                fired.append((key, labels[key][0], _message(key, cur, api)))
            elif was and not on and labels[key][2]:
                fired.append((key, labels[key][0] + " cleared", _cleared(key, cur)))
        if conf["events"].get("milestone"):
            seen = set(prev.get("milestones") or [])
            for m in (api.get("milestones") or []):
                if m["key"] not in seen:
                    fired.append(("milestone", "Milestone", m["label"] + "."))
        if conf["events"].get("build") and prev.get("build") and cur["build"] \
                and prev.get("build") != cur["build"]:
            fired.append(("build", "Container updated",
                          "Now running build %s (was %s)." % (cur["build"], prev["build"])))
    st["conditions"] = dict(cur_flags, milestones=cur["milestones"], build=cur["build"])
    st["seen"] = int(now)
    _state_save(st)
    for key, title, message in fired:
        deliver(conf, key, title, message, api)
    return fired


def _message(key, cur, api):
    if key == "frozen":
        return ("Backblaze has safety-frozen this backup. Nothing is deleted. Open the "
                "Status tab; it says what to do and what not to do.")
    if key == "skipped":
        sk = api.get("skipped_files") or {}
        reason = (sk.get("top_reason") or "").replace("_", " ").lower()
        return "%d files skipped and not backed up%s." % (
            cur["skipped_total"], (" (%s)" % reason) if reason else "")
    if key == "stale":
        d = api.get("last_backup_days")
        return "No completed backup for %s days." % (int(d) if d is not None else "several")
    if key == "stalled":
        return "bb-health: %s" % (cur["health_line"] or "stall")
    if key == "client_paused":
        pl = api.get("pause_label") or {}
        until = (" Until %s." % pl["until_str"]) if pl.get("until_str") else ""
        return "%s. %s%s" % (pl.get("title", "Paused by the client"), pl.get("detail", ""), until)
    if key == "completion":
        c = api.get("completion") or {}
        days = c.get("days")
        return "The first backup has caught up%s." % (
            (" after %d days" % days) if days else "")
    return key


def _cleared(key, cur):
    return {"frozen": "The backup is no longer frozen.",
            "skipped": "No files are skipped any more.",
            "stale": "A backup pass has completed.",
            "stalled": "bb-health reports OK again.",
            "client_paused": "The client has resumed."}.get(key, key + " cleared")


# ---- delivery --------------------------------------------------------------------

def fire(conf, key, title, message, api=None):
    """Send to every endpoint, each in its own thread, and log the outcome."""
    payload = {"container": socket.gethostname(), "build": (api or {}).get("build"),
               "event": key, "title": title, "message": message,
               "time": int(time.time()),
               "state": (api or {}).get("state")}
    for ep in conf.get("endpoints") or []:
        threading.Thread(target=_deliver, args=(ep, key, title, message, payload),
                         daemon=True).start()


def _deliver(ep, key, title, message, payload):
    last = None
    for wait in RETRY_AFTER:
        if wait:
            time.sleep(wait)
        ok, detail = send_once(ep, key, title, message, payload)
        if ok:
            sys.stderr.write("bb-monitor-web: notified %s (%s): %s\n"
                             % (ep.get("label"), ep.get("kind"), title))
            return True
        last = detail
    sys.stderr.write("bb-monitor-web: notification to %s failed after %d attempts: %s\n"
                     % (ep.get("label"), len(RETRY_AFTER), last))
    return False


def send_once(ep, key, title, message, payload):
    """One attempt. (ok, detail). Never raises."""
    try:
        headers = {"User-Agent": "bb-monitor-web"}
        if ep.get("auth") == "bearer" and ep.get("token"):
            headers["Authorization"] = "Bearer " + ep["token"]
        elif ep.get("auth") == "basic" and ep.get("token"):
            cred = "%s:%s" % (ep.get("user", ""), ep["token"])
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        if ep.get("kind") == "ntfy":
            headers.update({"Title": title.encode("ascii", "replace").decode(),
                            "Priority": "5" if key in URGENT else "3",
                            "Tags": "backblaze"})
            body = message.encode("utf-8")
        else:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(ep["url"], data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300, "HTTP %d" % resp.status
    except urllib.error.HTTPError as exc:
        return False, "HTTP %d" % exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)


def test(ep_id, conf=None):
    """A test message to one endpoint, synchronously. (ok, detail)."""
    conf = conf or load()
    for ep in conf["endpoints"]:
        if ep["id"] == ep_id:
            return send_once(ep, "test", "Backblaze 64 test",
                             "If you can read this, notifications work.",
                             {"container": socket.gethostname(), "event": "test",
                              "title": "Backblaze 64 test", "time": int(time.time()),
                              "message": "If you can read this, notifications work."})
    return False, "no such endpoint"
