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
#
# Hidden kinds are the other thing the store holds: a preference, set on the
# Settings tab, that a kind of warning is never shown. A dismissal is one
# occurrence accepted; hiding is the kind switched off. Reset forgets the
# dismissals and keeps the preference, which is why the two live in one file
# but are read out separately.

import json, os, re, tempfile, threading, time

import bbapi

STORE = bbapi.DIR + "/dismissed.json"

# Either a bare kind, as health() reports it, or "selection:<DRIVE>" where the
# drive is exactly the key drive_selection() returns: an upper-case letter, a
# colon and one backslash. Nothing else is stored, so a key can never carry a
# path separator, a dot segment or anything else that reads as a file name.
KEY = re.compile(r'^[a-z]+(:[A-Z]:\\)?$')
KIND = re.compile(r'^[a-z]+$')

_lock = threading.Lock()
_cache = None           # (signature, frozenset of keys to suppress, frozenset of hidden kinds)


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
        return {"keys": {}, "hidden": []}
    if not isinstance(c, dict) or not isinstance(c.get("keys"), dict):
        return {"keys": {}, "hidden": []}
    out = {}
    for key, rec in c["keys"].items():
        if not KEY.match(str(key)) or not isinstance(rec, dict):
            continue
        out[key] = {"at": int(rec.get("at") or 0), "text": str(rec.get("text") or "")}
    hid = c.get("hidden")
    hidden = sorted({str(k) for k in hid if KIND.match(str(k))}) if isinstance(hid, list) else []
    return {"keys": out, "hidden": hidden}


def _cached():
    """(keys to suppress, hidden kinds). Read on every poll by both monitors
    and by the metrics route, so the parse is cached against the file itself
    and a poll that changes nothing costs one stat."""
    global _cache
    sig = _signature()
    with _lock:
        cached = _cache
    if cached is not None and cached[0] == sig:
        return cached[1], cached[2]
    rec = load()
    hidden = frozenset(rec["hidden"])
    got = frozenset(rec["keys"]) | hidden
    with _lock:
        _cache = (sig, got, hidden)
    return got, hidden


def keys():
    """Every key to keep off the Status tab, the Monitor, the terminal monitor
    and the alert gauge: the dismissed keys and the hidden kinds together.
    A hidden kind is in here as its bare kind, so a consumer that checks a
    kind sees it; one that checks a subject key, such as a drive, must also
    ask hidden()."""
    return _cached()[0]


def hidden():
    """The kinds switched off on the Settings tab."""
    return _cached()[1]


def set_hidden(kinds):
    """Replace the hidden kinds. Raises ValueError with a message for the page."""
    if not isinstance(kinds, (list, tuple, set, frozenset)):
        raise ValueError("hidden must be a list of warning kinds")
    clean = set()
    for k in kinds:
        k = str(k or "").strip()
        if not KIND.match(k):
            raise ValueError("not a warning kind this container knows: %r" % k)
        clean.add(k)
    with bbapi._mutate():
        rec = load()
        rec["hidden"] = sorted(clean)
        _write(rec)
    return rec


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
    """Forget every dismissal and keep the hidden kinds: a reset is "show me
    what I accepted again", not "undo my settings". A store that was never
    written is already reset, so a missing file is the answer rather than an
    error."""
    with bbapi._mutate():
        rec = load()
        if rec["hidden"]:
            _write({"keys": {}, "hidden": rec["hidden"]})
        else:
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
