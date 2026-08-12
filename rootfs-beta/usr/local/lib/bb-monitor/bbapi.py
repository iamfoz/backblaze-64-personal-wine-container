# Key store for the /api/v1 surface, shared by bb-monitor-web and bb-apikey.
#
# A consumer outside the browser cannot reach the monitor's data any other way:
# it runs on the host or elsewhere on the network, the data is inside the
# container, and /monitor/ sits behind nginx's auth_request, which expects a
# browser session. So /api/v1 is exempted from that and defends itself with a key.
#
# There is no separate on/off setting. The surface is live exactly when an
# unrevoked key exists, which is one less thing to get out of step, and it means
# a fresh container answers 404 rather than 403 on every /api/v1 path. A 403
# would confirm the endpoint is there; with no key there is nothing to confirm.

import base64, hashlib, hmac, json, os, re, tempfile, time

DIR = "/config/bb-api"
KEYS = DIR + "/keys.json"

SCHEMA = 1                      # payload contract version, sent on every response

# Permissions are per operation, not per group. A key issued so that something can
# kick off a backup has no business also being able to pause one, and the two are
# not a ladder: neither implies the other. Groups exist only so a person can say
# "all of control" without ticking every box, and are expanded on the way in, so
# what gets stored is always the explicit list.
PERMISSIONS = {
    "read":               "Read status: rates, progress, memory, latency, health.",
    "read:files":         "Also see the names of files being backed up.",
    "control:backup-now": "Start a backup if one is not already running.",
    "control:pause":      "Ask a running backup to pause, cooperatively.",
    "report":             "Generate and download a diagnostic bundle.",
}

# Ordered for display, and it is the order the settings tab renders in.
ORDER = ("read", "read:files", "control:backup-now", "control:pause", "report")

# "read" is a permission in its own right, so it is not also a group name. A key
# that should see file names is granted read:files alongside it.
GROUPS = {"control": ("control:backup-now", "control:pause")}

# Nothing is held back at present. An entry here is refused at creation as well as
# being greyed in the settings tab, so a permission cannot be granted before the
# thing it governs exists.
RESERVED = ()


def expand(names):
    """Group names to their members, everything else through unchanged."""
    out = []
    for n in names:
        out.extend(GROUPS.get(n, (n,)))
    return sorted(set(out))

# bb64_<id>_<secret>: the id is public so a key can be named, listed and revoked
# without the secret ever being recoverable or shown twice.
_KEY_RE = re.compile(r"^bb64_([0-9a-f]{8})_([A-Za-z0-9_-]{43})$")


def _now():
    return int(time.time())


def _read():
    try:
        with open(KEYS, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write(records):
    os.makedirs(DIR, exist_ok=True)
    os.chmod(DIR, 0o700)
    # Written through a temporary file in the same directory so a crash mid-write
    # cannot leave a truncated key store, which would lock the owner out.
    fd, tmp = tempfile.mkstemp(dir=DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, KEYS)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _hash(secret):
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def create(label, scopes, expires_in_days=None):
    """Mint a key. Returns (record, secret_once) — the secret is never stored.

    `expires_in_days` of None means it never expires, which is the right answer
    for something wired into a dashboard that should keep working. A key handed
    to someone for a support thread is the case that wants a date on it.
    """
    scopes = expand(scopes)
    bad = [s for s in scopes if s not in PERMISSIONS]
    if bad:
        raise ValueError("unknown permission: %s" % ", ".join(bad))
    held = [s for s in scopes if s in RESERVED]
    if held:
        raise ValueError("not available yet: %s" % ", ".join(held))
    if not scopes:
        raise ValueError("a key needs at least one permission")
    records = _read()
    while True:
        kid = os.urandom(4).hex()
        if not any(r["id"] == kid for r in records):
            break
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    if expires_in_days is not None:
        try:
            days = float(expires_in_days)
        except (TypeError, ValueError):
            raise ValueError("expiry must be a number of days, or none")
        if days <= 0:
            raise ValueError("expiry must be more than zero days")
        expires = _now() + int(days * 86400)
    else:
        expires = None
    rec = {"id": kid, "label": label, "scopes": sorted(scopes),
           "hash": _hash(secret), "created": _now(),
           "last_used": None, "revoked": None, "expires": expires}
    records.append(rec)
    _write(records)
    return rec, "bb64_%s_%s" % (kid, secret)


def revoke(kid):
    records = _read()
    for r in records:
        if r["id"] == kid and not r["revoked"]:
            r["revoked"] = _now()
            _write(records)
            return True
    return False


def listing():
    """Records without the hash, for display. `state` saves every caller from
    working out the same three-way answer."""
    now = _now()
    out = []
    for r in _read():
        row = {k: v for k, v in r.items() if k != "hash"}
        row.setdefault("expires", None)      # keys minted before expiry existed
        row["state"] = ("revoked" if r["revoked"]
                        else "expired" if expired(r, now) else "active")
        out.append(row)
    return out


def expired(rec, now=None):
    exp = rec.get("expires")
    return bool(exp) and (now or _now()) >= exp


def active():
    """Keys that would authenticate right now. An expired key does not keep the
    surface alive any more than a revoked one does."""
    now = _now()
    return [r for r in _read() if not r["revoked"] and not expired(r, now)]


def verify(presented, scope):
    """(ok, key_id_or_None). key_id is returned on a parseable id even when the
    secret is wrong, so a failure can be logged without ever touching the secret.

    `scope` is one permission, a tuple meaning any one of them, or None meaning
    any valid key.
    """
    if not presented:
        return False, None
    m = _KEY_RE.match(presented.strip())
    if not m:
        return False, None
    kid, secret = m.group(1), m.group(2)
    want = _hash(secret)
    for r in _read():
        if r["id"] != kid:
            continue
        # compare_digest even though both sides are hex of a 256-bit random value:
        # the cost is nothing and it keeps the comparison free of timing shape.
        if not hmac.compare_digest(r["hash"], want):
            return False, kid
        if r["revoked"] or expired(r):
            return False, kid
        if scope is not None:
            want = scope if isinstance(scope, (tuple, list)) else (scope,)
            if not any(w in r["scopes"] for w in want):
                return False, kid
        return True, kid
    return False, kid


def perms(kid):
    """What a key holds, so a caller can be told what it may do rather than
    having to probe each endpoint and collect 401s."""
    for r in _read():
        if r["id"] == kid and not r["revoked"] and not expired(r):
            return list(r["scopes"])
    return []


TOUCH_RESOLUTION = 60     # seconds; see touch()


def touch(kid):
    """Record use. Best effort: a read must not fail because this could not write.

    Rewriting the key store on every request would mean a full read and an atomic
    replace per poll, which for a consumer polling every couple of seconds is a
    steady stream of writes to /config for a timestamp nobody reads at that
    resolution. Coarse to the minute instead.
    """
    try:
        records = _read()
        for r in records:
            if r["id"] == kid:
                now = _now()
                if r["last_used"] and now - r["last_used"] < TOUCH_RESOLUTION:
                    return
                r["last_used"] = now
                _write(records)
                return
    except OSError:
        pass
