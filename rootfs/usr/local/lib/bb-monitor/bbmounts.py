# Which drives show their mount points as plain folders, settable from the
# Settings tab.
#
# Wine reports a folder that is a separate filesystem (a ZFS dataset, another
# disk mounted inside a share) as a mount-point reparse point, and the client
# skips reparse points, so it never backs up what is inside. The fork's Wine
# (the beta and :latest-patched) carries a patch that reports them as plain
# folders instead, for the drives
# named in WINE_MOUNTPOINTS_AS_DIRS. startapp.sh sets that variable from this
# store when it exists and from the container's MOUNTPOINTS_AS_DIRS variable
# when it does not, so a container that never touched the page behaves exactly
# as before. Wine reads it once per process, so a change takes effect at the
# next container restart.
#
# The store is one line of JSON, {"drives": "de"}, so the shell helper
# (/usr/local/lib/bb-mountpoints.sh) can read it with sed. An empty string is
# a choice too: no drives, whatever the variable says.

import json, os, re, tempfile

import bbapi, bbwine

STORE = bbapi.DIR + "/mountpoints.json"
APPLIED = "/tmp/.bb-mountpoints-applied"   # written by startapp.sh: the value Wine was started with
DOSDEVICES = os.environ.get("WINEPREFIX", "/config/wine/").rstrip("/") + "/dosdevices"
LETTERS = "defghijklmnopqrstuvwxyz"


def letters(value):
    """The drive letters in a value, lower case, sorted, once each. C: is the
    prefix's own drive and never a candidate."""
    return "".join(sorted({c for c in str(value or "").lower() if c in LETTERS}))


def from_variable(raw):
    """The Wine value for the container variable: "1" for every drive, letters
    for some, "" for none."""
    v = (raw or "").strip().lower()
    if v in ("all", "true", "yes", "1"):
        return "1"
    if v in ("", "false", "no", "0", "none"):
        return ""
    return letters(v)


def load():
    """{"drives": "de"} from the store, or None when the page never set it."""
    try:
        with open(STORE, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict) or not isinstance(c.get("drives"), str):
        return None
    return {"drives": letters(c["drives"])}


def save(drives):
    if isinstance(drives, list):
        drives = "".join(str(d) for d in drives)
    if not isinstance(drives, str) or re.search(r"[^a-zA-Z:,; ]", drives):
        raise ValueError("drives must be drive letters")
    rec = {"drives": letters(drives)}
    with bbapi._mutate():
        fd, tmp = tempfile.mkstemp(dir=bbapi.DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            os.chmod(tmp, 0o600)
            bbapi._own_like_config(tmp)
            os.replace(tmp, STORE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return rec


def clear():
    """Back to the variable deciding."""
    with bbapi._mutate():
        try:
            os.unlink(STORE)
        except OSError:
            pass


def _mapped():
    """[(letter, target)] for the drives mapped into the prefix, D: to Z:,
    leaving out Wine's own z: -> /."""
    out = []
    for c in LETTERS:
        link = "%s/%s:" % (DOSDEVICES, c)
        if not os.path.islink(link):
            continue
        target = os.path.realpath(link)
        if target == "/":
            continue
        out.append((c, target))
    return out


def _mounts_under(root, limit=60):
    """Names of the folders directly under root that are separate filesystems,
    the same top-level sample bb-doctor takes."""
    try:
        rdev = os.stat(root).st_dev
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out, seen = [], 0
    for n in names:
        if n == ".bzvol":
            continue
        p = os.path.join(root, n)
        try:
            if not os.path.isdir(p):
                continue
            seen += 1
            if seen > limit:
                break
            if os.stat(p).st_dev != rdev:
                out.append(n)
        except OSError:
            continue
    return out


def effective():
    """The Wine value the next start will use, and where it comes from."""
    stored = load()
    if stored is not None:
        return stored["drives"], "setting"
    return from_variable(os.environ.get("MOUNTPOINTS_AS_DIRS")), "variable"


def available():
    """Whether this image's Wine has the patch. WineHQ's (:latest) does not."""
    for r in bbwine.registry():
        if r["var"] == bbwine.DRIVES:
            return bbwine.switch_state(r, bbwine.manifest()) != "absent"
    return False


def state():
    """What the page shows: every mapped drive with whether it is included and
    the separate filesystems directly under it, and whether a restart is due."""
    value, source = effective()
    try:
        with open(APPLIED, encoding="utf-8") as fh:
            applied = fh.read().strip()
    except OSError:
        applied = None
    drives = []
    for c, target in _mapped():
        drives.append({"letter": c.upper(), "target": target,
                       "enabled": value == "1" or c in value,
                       "mounts": _mounts_under(target)})
    return {"available": available(), "value": value, "source": source, "all": value == "1",
            "variable": os.environ.get("MOUNTPOINTS_AS_DIRS", ""),
            "applied": applied, "restart_needed": applied is not None and applied != value,
            "drives": drives}
