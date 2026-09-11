# Dismissed warnings: the ones the person has looked at and accepted.
#
# Every warning the monitor raises is drawn from the client's own records, so a
# state that is genuinely fine on this container still raises one for as long as
# it lasts. C: set to back up nothing is the case that prompted this; a licence
# the owner knows about is another. A warning nobody can clear is a warning
# everybody learns to read past, which costs the next real one its audience.
#
# Dismissing is per key and reversible. It hides the warning on the Status tab,
# in the terminal monitor and in the metrics alert gauge. It does not hide it
# from the API, which still reports the entry with dismissed true: a consumer
# asking what is wrong should be told, and told that somebody has accepted it.
#
# bbnotify needs no part of this. It fires on transitions rather than on a
# steady state, so a warning that has been standing long enough to be dismissed
# has already fired once and will not fire again until it clears and returns.

import json, os, re, tempfile, threading, time

import bbapi

STORE = bbapi.DIR + "/dismissed.json"

# Either a bare kind, as health() reports it, or "selection:<DRIVE>" where the
# drive is exactly the key drive_selection() returns: an upper-case letter, a
# colon and one backslash. Nothing else is stored, so a key can never carry a
# path separator, a dot segment or anything else that reads as a file name.
KEY = re.compile(r'^[a-z]+(:[A-Z]:\\)?$')

_lock = threading.Lock()
_cache = None           # (signature, frozenset of keys)


def key_for(kind, subject=None):
    """The key for a warning. `subject` is the drive for a selection notice."""
    return "%s:%s" % (kind, subject) if subject else str(kind)


def _signature():
    """What identifies the current contents of the store, or None when there is
    none. The inode is in it as well as the modification time because the file
    is replaced rather than rewritten, and a replace within one clock tick would
    otherwise read as no change at all."""
    try:
        st = os.stat(STORE)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def load():
    try:
        with open(STORE, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return {"keys": {}}
    if not isinstance(c, dict) or not isinstance(c.get("keys"), dict):
        return {"keys": {}}
    out = {}
    for key, rec in c["keys"].items():
        if not KEY.match(str(key)) or not isinstance(rec, dict):
            continue
        out[key] = {"at": int(rec.get("at") or 0), "text": str(rec.get("text") or "")}
    return {"keys": out}


def keys():
    """The dismissed keys. Read on every poll by both monitors and by the
    metrics route, so the parse is cached against the file itself and a poll
    that changes nothing costs one stat."""
    global _cache
    sig = _signature()
    with _lock:
        cached = _cache
    if cached is not None and cached[0] == sig:
        return cached[1]
    got = frozenset(load()["keys"])
    with _lock:
        _cache = (sig, got)
    return got


def listing():
    """[{"key", "text", "at"}], oldest first, for the Settings tab."""
    recs = load()["keys"]
    return sorted(({"key": k, "text": v["text"], "at": v["at"]} for k, v in recs.items()),
                  key=lambda r: (r["at"], r["key"]))


def dismiss(key, text=""):
    """Record one dismissal. Raises ValueError with a message for the page.

    Written the way bbquiet.save() writes: a temporary file in the store's own
    directory, tightened and owned before it is put in place, under the same
    lock bb-apikey takes, so a dismissal arriving while a key is being minted
    cannot write back a store read before that key existed.
    """
    key = str(key or "").strip()
    if not KEY.match(key):
        raise ValueError("not a warning this container will dismiss")
    # Bounded and on one line, for the same reason a key label is: it is read
    # back into the Settings tab and it is the client's own wording, which is
    # the one string here that this container did not write.
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()[:200]
    with bbapi._mutate():
        rec = load()
        rec["keys"][key] = {"at": int(time.time()), "text": text}
        _write(rec)
    return rec


def reset():
    """Forget every dismissal. A store that was never written is already reset,
    so a missing file is the answer rather than an error."""
    with bbapi._mutate():
        try:
            os.unlink(STORE)
        except OSError:
            pass
    return True


def _write(rec):
    os.makedirs(bbapi.DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=bbapi.DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        bbapi._own_like_config(tmp)
        os.replace(tmp, STORE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
