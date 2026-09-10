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

import base64, ipaddress, json, os, secrets, socket, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request

import bbapi

CONF = bbapi.DIR + "/notify.json"
STATE = bbapi.DIR + "/notify-state.json"

# Each service wants its own shape. ntfy takes the text as the body with the title
# in a header; the generic webhook gets everything as JSON; the named services get
# the fields they document; "custom" renders a JSON template the user writes, with
# placeholders, for anything not listed. A Pushbullet user found the gap: it needs
# {"type": "note", ...} and answers 400 to anything else.
KINDS = ("ntfy", "webhook", "pushbullet", "discord", "slack", "gotify", "custom")
TEMPLATES = {
    "pushbullet": '{"type": "note", "title": "{title}", "body": "{message}"}',
    "discord":    '{"content": "**{title}**\\n{message}"}',
    "slack":      '{"text": "*{title}*\\n{message}"}',
    "gotify":     '{"title": "{title}", "message": "{message}", "priority": {priority}}',
}
PLACEHOLDERS = ("title", "message", "event", "container", "build", "state", "time", "priority")
DEFAULT_URLS = {"pushbullet": "https://api.pushbullet.com/v2/pushes"}


def clean_label(s):
    """One bounded line. The label is written to the container log, which is the
    audit trail for the control routes, so an embedded newline in it could forge
    a line there."""
    return (s or "").replace("\r", " ").replace("\n", " ").strip()[:60]


def blocked_address(url):
    """The address this URL resolves to that the container will not send to, or
    None.

    An endpoint is the one place a person can make the container issue an
    outbound request with a body and headers they choose, so the addresses that
    mean "me" are closed: loopback, the link-local range that carries a cloud
    host's metadata service, and the unspecified address. Private LAN ranges
    stay open, because ntfy and Gotify are usually run on the LAN.

    A name that will not resolve is not a refusal. The send fails on its own,
    and refusing at save time would stop a person configuring an endpoint while
    the network is down.
    """
    host = ""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return None
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        # ::ffff:127.0.0.1 is loopback wearing an IPv6 shape.
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return str(ip)
    return None


def render(template, values):
    """Fill {name} placeholders inside a JSON template. Strings are escaped as
    JSON string content, so a title with a quote in it stays valid JSON; numbers
    go in bare, so {priority} can sit outside quotes."""
    out = template
    for k in PLACEHOLDERS:
        v = values.get(k)
        if isinstance(v, bool) or v is None:
            rep_ = "null" if v is None else ("true" if v else "false")
        elif isinstance(v, (int, float)):
            rep_ = str(v)
        else:
            rep_ = json.dumps(str(v))[1:-1]
        out = out.replace("{" + k + "}", rep_)
    return out

# (key, label, what it means, fires when it clears too)
EVENTS = (
    ("frozen",        "Safety freeze",
     "Backblaze has frozen the backup. Nothing is deleted.", True),
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

# How each endpoint's last delivery went. A notification failing at three in the
# morning went to the container log, where nobody looks; the Pushbullet endpoint
# that could not work was only found by pressing Test. Kept in memory rather than
# on disk: it describes this run of the service, and a stale verdict from before
# a restart would be worse than none.
_delivery = {}      # endpoint id -> {ok, detail, at, event}


def delivery_state():
    with _lock:
        return dict(_delivery)


def _record(ep, ok, detail, event):
    if not ep.get("id"):
        return
    with _lock:
        _delivery[ep["id"]] = {"ok": bool(ok), "detail": detail,
                               "at": int(time.time()), "event": event}


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
        bad = blocked_address(url)
        if bad:
            raise ValueError("that URL resolves to %s, which is the container itself "
                             "or a link-local address" % bad)
        auth = (e.get("auth") or "none").strip()
        if auth not in ("none", "bearer", "basic"):
            raise ValueError("auth must be none, bearer or basic")
        template = (e.get("template") or "").strip() if kind == "custom" else ""
        if kind == "custom":
            if not template:
                raise ValueError("a custom endpoint needs a body template")
            sample = {k: (3 if k in ("priority", "time") else 'x"y') for k in PLACEHOLDERS}
            try:
                json.loads(render(template, sample))
            except ValueError:
                raise ValueError("the body template is not valid JSON once the placeholders are filled")
        rec = {"id": e.get("id") or secrets.token_hex(4),
               "label": clean_label(e.get("label")) or kind,
               "kind": kind, "url": url, "auth": auth,
               "token": (e.get("token") or "").strip() or kept.get(e.get("id") or "", ""),
               "user": (e.get("user") or "").strip(), "template": template}
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
    state = delivery_state()
    for e in c["endpoints"]:
        e["has_token"] = bool(e.get("token"))
        e["token"] = ""
        e["last_delivery"] = state.get(e["id"])
    c["event_list"] = [{"key": k, "label": l, "does": d, "clears": c_}
                       for k, l, d, c_ in EVENTS]
    c["kinds"] = list(KINDS)
    c["placeholders"] = list(PLACEHOLDERS)
    c["default_urls"] = dict(DEFAULT_URLS)
    return c


# ---- what is true right now ------------------------------------------------------

# The same state as the file holds, kept here as well. observe() rebuilds its
# baseline from disk on every poll, so a store that cannot be written at all
# used to mean prev was always None and no event ever fired again, silently.
# Held in memory the failure degrades to "forgotten across a restart".
_state_mem = {}
_state_warned = False


def _state_load():
    try:
        with open(STATE, encoding="utf-8") as fh:
            st = json.load(fh)
            if isinstance(st, dict):
                return st
    except (OSError, ValueError):
        pass
    return dict(_state_mem)


def _state_save(st):
    global _state_warned
    _state_mem.clear()
    _state_mem.update(st)
    try:
        with bbapi._mutate():
            _write(STATE, st)
    except OSError as exc:
        # Once only. This runs on the poll loop, so a line per poll would be
        # 43,000 a day in the container log.
        if not _state_warned:
            _state_warned = True
            sys.stderr.write("bb-monitor-web: notification state cannot be saved (%s); "
                             "events still fire but are forgotten on restart\n" % exc)


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
    now_conditions = dict(cur_flags, milestones=cur["milestones"], build=cur["build"])
    # Only when something moved. Written on every poll this was 43,000 atomic
    # replaces a day on the user's appdata share, each one taking the store lock
    # that key creation and use also want, for a record that had not changed.
    if now_conditions != prev:
        st["conditions"] = now_conditions
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
            _record(ep, True, detail, key)
            return True
        last = detail
    sys.stderr.write("bb-monitor-web: notification to %s failed after %d attempts: %s\n"
                     % (ep.get("label"), len(RETRY_AFTER), last))
    _record(ep, False, last, key)
    return False


def _quiet_detail(ep, reason, said):
    """What a delivery record says, with the reason kept to the container log.

    The record is read back from the page, so it is the one place the remote's
    answer could be read by whoever can reach /manage/. A person debugging their
    own endpoint gets the full reason from the log, where it was always going.
    """
    sys.stderr.write("bb-monitor-web: notify %s (%s): %s\n"
                     % (ep.get("label"), ep.get("kind"), reason))
    return said


def send_once(ep, key, title, message, payload):
    """One attempt. (ok, detail). Never raises."""
    try:
        # Again here and not only at save time: a name that resolved to the LAN
        # when the endpoint was stored can resolve to loopback later, and a
        # store written by hand never passed save() at all.
        bad = blocked_address(ep.get("url") or "")
        if bad:
            return False, "refused: %s is not a permitted destination" % bad
        headers = {"User-Agent": "bb-monitor-web"}
        if ep.get("auth") == "bearer" and ep.get("token"):
            headers["Authorization"] = "Bearer " + ep["token"]
        elif ep.get("auth") == "basic" and ep.get("token"):
            cred = "%s:%s" % (ep.get("user", ""), ep["token"])
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        kind = ep.get("kind") or "webhook"
        priority = 5 if key in URGENT else 3
        if kind == "ntfy":
            headers.update({"Title": title.encode("ascii", "replace").decode(),
                            "Priority": str(priority), "Tags": "backblaze"})
            body = message.encode("utf-8")
        else:
            headers["Content-Type"] = "application/json"
            if kind == "gotify" and ep.get("token"):
                headers["X-Gotify-Key"] = ep["token"]     # Gotify's own header
            template = TEMPLATES.get(kind) or (ep.get("template") if kind == "custom" else None)
            if template:
                values = dict(payload, title=title, message=message, priority=priority)
                body = render(template, values).encode("utf-8")
            else:
                # The generic shape, with "body" alongside "message" because that
                # is the other name a receiver tends to look for.
                body = json.dumps(dict(payload, body=message)).encode("utf-8")
        req = urllib.request.Request(ep["url"], data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            # The status code stays out of the detail: it is recorded and read
            # back through GET /manage/notify, and a code per host is a probe of
            # whatever the URL points at. The container log keeps the number.
            if 200 <= resp.status < 300:
                return True, "delivered"
            return False, _quiet_detail(ep, "HTTP %d" % resp.status, "rejected by the endpoint")
    except urllib.error.HTTPError as exc:
        return False, _quiet_detail(ep, "HTTP %d" % exc.code, "rejected by the endpoint")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, _quiet_detail(ep, str(exc), "not reachable")


def test(ep_id, conf=None):
    """A test message to one endpoint, synchronously. (ok, detail)."""
    conf = conf or load()
    for ep in conf["endpoints"]:
        if ep["id"] == ep_id:
            ok, detail = send_once(ep, "test", "Backblaze 64 test",
                             "If you can read this, notifications work.",
                             {"container": socket.gethostname(), "event": "test",
                              "title": "Backblaze 64 test", "time": int(time.time()),
                              "message": "If you can read this, notifications work."})
            _record(ep, ok, detail, "test")
            return ok, detail
    return False, "no such endpoint"
