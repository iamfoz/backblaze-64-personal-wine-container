# Key store for the /api/v1 surface, shared by bb-monitor-web and bb-apikey.
#
# The dashboard plugin lives in its own repository and cannot reach the monitor's
# data any other way: the plugin is PHP on the host, the data is inside the
# container, and /monitor/ sits behind nginx's auth_request. So /api/v1 is
# exempted from that and defends itself with a key.
#
# There is no separate on/off setting. The surface is live exactly when an
# unrevoked key exists, which is one less thing to get out of step, and it means
# a fresh container answers 404 rather than 403 on every /api/v1 path. A 403
# would confirm the endpoint is there; with no key there is nothing to confirm.

import base64, hashlib, hmac, json, os, re, tempfile, time

DIR = "/config/bb-api"
KEYS = DIR + "/keys.json"

SCHEMA = 1                      # payload contract version, sent on every response
SCOPES = ("read", "control", "report")

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


def create(label, scopes):
    """Mint a key. Returns (record, secret_once) — the secret is never stored."""
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError("unknown scope: %s" % ", ".join(bad))
    if not scopes:
        raise ValueError("a key needs at least one scope")
    records = _read()
    while True:
        kid = os.urandom(4).hex()
        if not any(r["id"] == kid for r in records):
            break
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    rec = {"id": kid, "label": label, "scopes": sorted(scopes),
           "hash": _hash(secret), "created": _now(),
           "last_used": None, "revoked": None}
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
    """Records without the hash, for display."""
    return [{k: v for k, v in r.items() if k != "hash"} for r in _read()]


def active():
    return [r for r in _read() if not r["revoked"]]


def verify(presented, scope):
    """(ok, key_id_or_None). key_id is returned on a parseable id even when the
    secret is wrong, so a failure can be logged without ever touching the secret."""
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
        if r["revoked"]:
            return False, kid
        if scope not in r["scopes"]:
            return False, kid
        return True, kid
    return False, kid


def touch(kid):
    """Record use. Best effort: a read must not fail because this could not write."""
    try:
        records = _read()
        for r in records:
            if r["id"] == kid:
                r["last_used"] = _now()
                _write(records)
                return
    except OSError:
        pass
