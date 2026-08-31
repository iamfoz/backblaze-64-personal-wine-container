# Backup control, through Backblaze's own bzcli rather than anything invented here.
#
# The rule this module exists to enforce: killing bztransmit mid-upload is what
# leaves the stale four-hour lock behind, and the relaunch loop after it is the
# wedge bb-health detects and bb-watchdog clears. An API that stopped a backup by
# killing the process would manufacture that fault on demand, over HTTP. bzcli
# asks bztransmit to pause instead, which is the client's own mechanism.
#
# bzcli's "action" group also carries --set-pek, --clear-pek and --change-pek.
# Clearing the private encryption key on someone's backup is unrecoverable:
# Backblaze cannot reset it and cannot retrieve the data without it. Those verbs
# do require account authentication that this container does not hold, but that
# is Backblaze's guard and not ours. Hence the whitelist below: the HTTP layer
# maps a path segment to a key in ACTIONS, and nothing from a request ever
# reaches the argument list.

import os, subprocess

BZCLI = "/config/wine/drive_c/Program Files/Backblaze/bzcli.exe"
PREFIX = os.environ.get("WINEPREFIX", "/config/wine")

# The complete set of things this container will ask the client to do. Neither
# needs account authentication, which is why they are safe to expose and the PEK
# verbs are not. Adding to this dict is a deliberate act; nothing here builds an
# argument list from anything a caller sends.
ACTIONS = {
    "backup-now": (["action", "--backup-now"],
                   "start a backup if one is not already running"),
    # Backblaze document backup-now as the way out of a pause: their own PEK
    # instructions read "pause backup, change the PEK, and then do backup now".
    "pause": (["action", "--pause-backup"],
              "ask the running backup to pause, cooperatively"),
}

TIMEOUT = 60          # wine start-up is slow; a hung call must not hold a worker


def run(name):
    """(ok, message). `name` must already be a key of ACTIONS."""
    argv = ACTIONS[name][0]
    # Matched to the invocation proven by hand: a working directory of the
    # install folder, and HOME set. This runs from an s6 service rather than a
    # shell, and s6 hands down a minimal environment, so anything Wine needs has
    # to be supplied rather than assumed. Wine without HOME tries to build a
    # fresh prefix somewhere it cannot write and fails in a way that has nothing
    # to do with the command it was asked to run.
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    env.setdefault("HOME", "/config")
    if "/opt/wine/bin" not in env.get("PATH", ""):
        env["PATH"] = "/opt/wine/bin:" + env.get("PATH", "/usr/bin:/bin")
    try:
        p = subprocess.run(["wine", BZCLI] + argv, env=env, timeout=TIMEOUT,
                           cwd=os.path.dirname(BZCLI),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return False, "wine is not on PATH"
    except OSError as exc:
        return False, "could not run wine: %s" % exc
    except subprocess.TimeoutExpired:
        return False, "bzcli did not finish within %ds" % TIMEOUT
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if p.returncode == 0:
        return True, out or "ok"
    # bzcli documents non-zero as failure with detail on stderr. Passed through
    # rather than summarised, since the caller cannot see the container's log,
    # and with the exit status kept alongside it: a Wine startup failure writes
    # to stderr and exits non-zero, and the two together say which happened.
    detail = err or out
    return False, ("bzcli exited %d: %s" % (p.returncode, detail) if detail
                   else "bzcli exited %d with no output" % p.returncode)


def report_value(query):
    """One value from `bzcli report -v <path>`.

    Deliberately never the whole document. `bzcli report -f json` carries the
    account email, login, host guid and billing status, none of which belongs in
    a response this container hands out. Callers ask for the one path they want.
    """
    if not query.startswith("/") or any(c in query for c in " ;&|$`\n"):
        return None
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    try:
        p = subprocess.run(["wine", BZCLI, "report", "-v", query], env=env,
                           timeout=TIMEOUT, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace").strip() or None


def available():
    return os.path.exists(BZCLI)
