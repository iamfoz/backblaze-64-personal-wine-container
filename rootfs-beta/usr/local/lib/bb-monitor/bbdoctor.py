# Settings for bb-doctor that the Settings tab can change: how deep the
# read-speed sample looks for a large file, and whether to keep going until it
# finds one. One line of JSON, read by the shell drop-in with grep, the same
# way the watchdog switch is. Absent, the drop-in's own defaults apply (three
# levels, stop there), which is what every container did before the setting
# existed.

import json, os, tempfile

import bbapi

STORE = bbapi.DIR + "/doctor.json"
DEFAULTS = {"read_depth": 3, "read_until_found": False}
MAX_DEPTH = 32


def load():
    """The stored settings over the defaults."""
    out = dict(DEFAULTS)
    try:
        with open(STORE, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return out
    if isinstance(c, dict):
        d = c.get("read_depth")
        if isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= MAX_DEPTH:
            out["read_depth"] = d
        if isinstance(c.get("read_until_found"), bool):
            out["read_until_found"] = c["read_until_found"]
    return out


def save(conf):
    """Validate and store. Raises ValueError with a message for the page."""
    try:
        depth = int(conf.get("read_depth", DEFAULTS["read_depth"]))
    except (TypeError, ValueError):
        raise ValueError("read depth must be a whole number")
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError("read depth must be between 1 and %d" % MAX_DEPTH)
    rec = {"read_depth": depth, "read_until_found": bool(conf.get("read_until_found"))}
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
