# The Wine patch switches, settable from the Settings tab.
#
# The fork's Wine has a runtime switch for each of its patches, and without
# the switch it behaves as upstream Wine. The list, with each switch's class
# and text, is wine-switches.tsv; which ones this image's Wine carries is in the
# manifest its build wrote. startapp.sh exports them before Wine starts, so a
# change takes effect at the next container restart. bb-wine-switches.sh is the
# shell side of the same rules and the two must agree:
#
#   * the store wins when it names a switch, the container variable of the same
#     name decides when it does not (the image sets it to its default);
#   * a switch that needs another one that is off is off too;
#   * with the service token off, a client from 10.0.3.1075 on completes no
#     pass, so startapp pins an unpinned client to 10.0.1.1069.
#
# The page enforces the interlocks before saving, so a choice that would break
# the backup is refused with the reason rather than stored.
#
# The store is one line of JSON, {"WINE_CASE_CACHE": "0", ...}, string values
# so the shell can read it with sed.

import json, os, tempfile

import bbapi, bbdata

STORE = bbapi.DIR + "/wine-switches.json"
REGISTRY = "/usr/local/share/bb64/wine-switches.tsv"
MANIFEST = "/opt/wine/share/bb64-patches"
APPLIED = "/tmp/.bb-wine-switches-applied"
CLIENT_VERSION = bbdata.BZ + "/bzreports/bzserv_version.txt"
SAFE_CLIENT = "10.0.1.1069"
TOKEN_CLIENT = (10, 0, 3, 1075)
CLASSES = ("fix", "performance", "experimental")
DRIVES = "WINE_MOUNTPOINTS_AS_DIRS"    # set per drive in bbmounts, listed here for the page


def registry():
    """The switches, in the order the page shows them."""
    out = []
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                f = line.rstrip("\n").split("\t")
                if len(f) != 7 or f[2] not in CLASSES:
                    continue
                out.append({"var": f[0], "patches": f[1].split(","), "class": f[2],
                            "requires": None if f[3] == "-" else f[3],
                            "title": f[4], "summary": f[5], "warning": f[6]})
    except OSError:
        pass
    return out


def manifest():
    """{"wine_ref": ..., "patches": {name: "applied" | "upstream"}}, or None
    for a Wine with no manifest (WineHQ's)."""
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    out = {"wine_ref": None, "patches": {}}
    for line in lines:
        f = line.split()
        if len(f) >= 2 and f[0] == "wine_ref":
            out["wine_ref"] = f[1]
        elif len(f) >= 2 and f[0] in ("applied", "upstream"):
            out["patches"][f[1]] = f[0]
    return out


def switch_state(row, man):
    """available, upstream or absent, as bb_ws_state decides it."""
    if man is None:
        return "absent"
    state = "available"
    for p in row["patches"]:
        got = man["patches"].get(p)
        if got == "upstream":
            state = "upstream"
        elif got != "applied":
            return "absent"
    return state


def _on(value):
    return str(value or "").strip().lower() in ("1", "on", "true", "yes")


def load():
    """{var: "1" | "0"} from the store, or {} when the page never set one."""
    try:
        with open(STORE, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(c, dict):
        return {}
    return {k: v for k, v in c.items() if isinstance(k, str) and v in ("0", "1")}


def _chosen(var, stored):
    if var in stored:
        return stored[var] == "1", "setting"
    return _on(os.environ.get(var)), "variable"


def _version(text):
    try:
        return tuple(int(x) for x in str(text).strip().split("."))
    except ValueError:
        return None


def client_needs_token(version):
    v = _version(version)
    return v is not None and v >= TOKEN_CLIENT


def installed_client():
    try:
        with open(CLIENT_VERSION, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _applied():
    try:
        with open(APPLIED, encoding="utf-8") as fh:
            return dict(l.strip().split("=", 1) for l in fh if "=" in l)
    except OSError:
        return None


def _resolve(rows, man, stored):
    """{var: (value, chosen, source, note)} for the boolean switches this Wine
    carries, after the interlocks."""
    chosen = {}
    for r in rows:
        if r["var"] == DRIVES or switch_state(r, man) != "available":
            continue
        chosen[r["var"]] = _chosen(r["var"], stored)
    out = {}
    for r in rows:
        if r["var"] not in chosen:
            continue
        on, source = chosen[r["var"]]
        value, note = on, None
        req = r["requires"]
        if on and req and req in chosen and not chosen[req][0]:
            value, note = False, "off because it needs %s, which is off" % req
        out[r["var"]] = (value, on, source, note)
    return out


def check(stored):
    """Refuse a set of choices that would break the backup. Raises ValueError
    with the reason the page shows."""
    rows = {r["var"]: r for r in registry()}
    man = manifest()
    res = _resolve(list(rows.values()), man, stored)
    for var, (value, on, source, note) in res.items():
        req = rows[var]["requires"]
        if on and req in res and not res[req][1]:
            raise ValueError("%s needs %s, so turn that on first or turn this off"
                             % (rows[var]["title"], rows[req]["title"]))
    tok = res.get("WINE_SERVICE_TOKEN")
    if tok and not tok[1]:
        pin = os.environ.get("BACKBLAZE_VERSION", "").strip()
        if pin and client_needs_token(pin):
            raise ValueError("BACKBLAZE_VERSION=%s needs the service token. Pin 10.0.1.1069 or "
                             "unset the variable before turning the token off" % pin)


def save(choices):
    """Store the page's choices: {var: bool}. Only switches this Wine carries
    are accepted, and the interlocks are checked first."""
    if not isinstance(choices, dict):
        raise ValueError("switches must be an object")
    rows = {r["var"]: r for r in registry()}
    man = manifest()
    stored = {}
    for var, on in choices.items():
        row = rows.get(var)
        if row is None or var == DRIVES:
            raise ValueError("%r is not a switch this page sets" % var)
        if switch_state(row, man) != "available":
            raise ValueError("this image's Wine has no %s switch" % row["title"])
        if not isinstance(on, bool):
            raise ValueError("%s must be true or false" % var)
        stored[var] = "1" if on else "0"
    check(stored)
    with bbapi._mutate():
        fd, tmp = tempfile.mkstemp(dir=bbapi.DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(stored, sort_keys=True) + "\n")
            os.chmod(tmp, 0o600)
            bbapi._own_like_config(tmp)
            os.replace(tmp, STORE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return stored


def clear():
    """Back to the container variables deciding."""
    with bbapi._mutate():
        try:
            os.unlink(STORE)
        except OSError:
            pass


def state():
    """What the page shows: the Wine, each switch with its class, value and
    where it comes from, and whether a restart is due."""
    rows = registry()
    man = manifest()
    stored = load()
    res = _resolve(rows, man, stored)
    applied = _applied()
    out = []
    restart = False
    for r in rows:
        st = switch_state(r, man)
        item = {k: r[k] for k in ("var", "class", "requires", "title", "summary", "warning")}
        item["state"] = st
        item["drives"] = r["var"] == DRIVES
        if r["var"] in res:
            value, on, source, note = res[r["var"]]
            item.update(value=value, chosen=on, source=source, note=note,
                        variable=os.environ.get(r["var"], ""))
            if applied is not None:
                was = applied.get(r["var"])
                item["applied"] = was
                if was is not None and was != ("1" if value else "0"):
                    restart = True
        out.append(item)
    tok = res.get("WINE_SERVICE_TOKEN")
    pin = os.environ.get("BACKBLAZE_VERSION", "").strip()
    client = {"installed": installed_client(), "pin": pin or None,
              "token_off_pins": bool(tok and not tok[0] and not pin),
              "safe": SAFE_CLIENT}
    return {"wine": "patched" if man is not None else "winehq",
            "wine_ref": man["wine_ref"] if man else None,
            "switches": out, "stored": bool(stored),
            "restart_needed": restart, "client": client}
