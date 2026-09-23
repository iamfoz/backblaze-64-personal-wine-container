# The automatic-recovery switch, settable from the Settings tab.
#
# ENABLE_WATCHDOG is a container variable, so changing it means editing the
# container. This store holds a switch the page can set instead: when the file
# exists it wins, when it does not the variable decides, so a container that
# never touched the page behaves exactly as before. The watchdog's run script
# and the watchdog itself read the file with grep, every minute while parked
# and every cycle while running, so a change takes effect without a restart.
# The file is one line of JSON so a shell can read it without a JSON parser.

import json, os, tempfile

import bbapi

STORE = bbapi.DIR + "/watchdog.json"
STATE = "/tmp/.bb-watchdog-state"      # written by the run script: enabled or disabled


def load():
    """{"enabled": bool} from the store, or None when the page never set it."""
    try:
        with open(STORE, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict) or not isinstance(c.get("enabled"), bool):
        return None
    return {"enabled": c["enabled"]}


def save(enabled):
    rec = {"enabled": bool(enabled)}
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


def _running():
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    if b"bin/bb-watchdog" in fh.read():
                        return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def state():
    """What the page shows: the effective setting, where it comes from, and
    whether the watchdog process is up right now."""
    stored = load()
    env = os.environ.get("ENABLE_WATCHDOG", "false").strip().lower() == "true"
    enabled = stored["enabled"] if stored else env
    try:
        with open(STATE, encoding="utf-8") as fh:
            last = fh.read().strip()
    except OSError:
        last = None
    return {"enabled": enabled, "source": "setting" if stored else "variable",
            "variable": env, "running": _running(), "service_state": last}
