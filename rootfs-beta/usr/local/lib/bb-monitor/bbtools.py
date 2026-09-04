# The command-line tools over HTTP: bb-doctor, bb-health and bb-version.
#
# The web does not reimplement these. It runs the binary the console runs and
# shows what it printed. bb-monitor and bb-monitor-web share a data layer because
# both draw the same live state continuously; these tools are one-shot programs
# that write a text report and exit, and for that shape one program with two ways
# to start it is a stronger guarantee than two implementations kept in step. A
# change to bb-doctor is a change to both views, with nothing to drift.
#
# Two consequences follow. The output shown is the tool's output, byte for byte,
# so a paste into a support thread is the same artefact whichever way it was
# made. And the process runs as this service's user, the container's own, which
# is the user whose permissions the checks are about; a console run is root and
# passes every permission test.
#
# The argument list is fixed. A caller names a tool and may switch on options
# from that tool's own list, each of which maps to a fixed flag. Nothing from a
# request reaches argv except through those lookups. Same rule as bbctl.ACTIONS.

import os, secrets, subprocess, threading, time

BIN = "/usr/local/bin"
PREFIX = os.environ.get("WINEPREFIX", "/config/wine")

# Ordered for display.
ORDER = ("doctor", "health", "version")

TOOLS = {
    "doctor": {
        "argv": [BIN + "/bb-doctor"],
        "label": "bb-doctor",
        "does": "Checks the installation for the faults this project has met: the Wine "
                "prefix, the client, drive mappings, storage, memory, backup health, "
                "connectivity and files the client has given up on.",
        # The connectivity check alone can take a minute: six mirrors at ten
        # seconds each. The skipped-file sample stats files on a mounted share.
        "timeout": 180,
        "options": {
            "fix": {
                "flag": "--fix",
                "label": "Repair what can be repaired safely",
                "does": "Repairs the reported Windows version, missing drive links, "
                        "missing skin aliases and a stale four-hour lock. Never touches "
                        "backup state, and skips anything it is not sure of.",
                # Shown before a run with this option. It names what can change,
                # in plain words, every time. Not a generic "are you sure".
                "confirm": "This can change files in the Wine prefix and delete a stale "
                           "lock file. bb-doctor only repairs what it has already judged "
                           "safe, and only in the container's own files. Run the repair?",
            },
        },
        # Exit codes in words. bb-doctor exits 1 when it finds a problem, which
        # is a result and not a failure.
        "exit": {0: "no problems found", 1: "found problems"},
    },
    "health": {
        "argv": [BIN + "/bb-health"],
        "label": "bb-health",
        "does": "The check the container's HEALTHCHECK runs: OK, or a corroborated "
                "HANG or WEDGE with the evidence.",
        "timeout": 30,
        "options": {},
        "exit": {0: "healthy", 1: "fault reported"},
        # The output is a verdict and a few lines of evidence, so the page shows
        # it in the card itself, with a mark, rather than in an output box.
        "inline": True,
    },
    "version": {
        "argv": [BIN + "/bb-version"],
        "label": "bb-version",
        "does": "The installed client version against the one Backblaze is serving, "
                "with the container and Wine versions. What to paste into a bug report.",
        # It asks Backblaze's update API, the same call the updater makes.
        "timeout": 60,
        "options": {},
        "exit": {0: "done"},
    },
}

OUTPUT_CAP = 64 * 1024     # bytes kept per job; nothing here comes close
JOB_TTL = 3600             # seconds a finished job is remembered

_lock = threading.Lock()
_jobs = {}                 # id -> job dict
_running = {}              # tool name -> job id, while a run is in flight


def available(name):
    t = TOOLS.get(name)
    return bool(t) and os.path.exists(t["argv"][0])


def describe():
    """The registry as the page needs it, plus the most recent job per tool so a
    reload shows a run that is still going or has just finished."""
    now = time.time()
    with _lock:
        _sweep(now)
        out = []
        for name in ORDER:
            t = TOOLS[name]
            last = None
            best = None
            for j in _jobs.values():
                if j["tool"] == name and (best is None or j["started"] > best):
                    best, last = j["started"], j["id"]
            out.append({
                "name": name, "label": t["label"], "does": t["does"],
                "available": available(name), "inline": bool(t.get("inline")),
                "options": [{"key": k, "flag": o["flag"], "label": o["label"],
                             "does": o["does"], "confirm": o.get("confirm")}
                            for k, o in t["options"].items()],
                "last_job": last,
                "running": _running.get(name),
            })
    return out


def _sweep(now):
    for jid, j in list(_jobs.items()):
        if j["state"] != "running" and now - j["started"] > JOB_TTL:
            _jobs.pop(jid, None)


def _env():
    # Matched to bbctl.run: s6 hands the service a minimal environment, and
    # bb-version calls `wine --version`.
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    env.setdefault("HOME", "/config")
    if "/opt/wine/bin" not in env.get("PATH", ""):
        env["PATH"] = "/opt/wine/bin:" + env.get("PATH", "/usr/bin:/bin")
    return env


def start(name, options=()):
    """(job_id, joined). One running job per tool: a second request while one
    runs joins it, so two tabs cannot run --fix at once against the same lock.

    Raises KeyError for an unknown tool and ValueError for an unknown option.
    """
    t = TOOLS[name]
    keys = []
    for k in options:
        if k not in t["options"]:
            raise ValueError("unknown option for %s: %s" % (name, k))
        if k not in keys:
            keys.append(k)
    argv = list(t["argv"]) + [t["options"][k]["flag"] for k in keys]
    now = time.time()
    with _lock:
        _sweep(now)
        jid = _running.get(name)
        if jid and jid in _jobs and _jobs[jid]["state"] == "running":
            return jid, True
        jid = secrets.token_hex(6)
        _jobs[jid] = {"id": jid, "tool": name, "options": keys, "argv": argv,
                      "state": "running", "started": now, "finished": None,
                      "exit_code": None, "error": None,
                      "lines": [], "bytes": 0, "truncated": False}
        _running[name] = jid
    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return jid, False


def _run(jid):
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return
        argv, timeout = j["argv"], TOOLS[j["tool"]]["timeout"]
    timed_out = []

    def kill():
        timed_out.append(True)
        try:
            p.kill()
        except OSError:
            pass

    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=_env(),
                             cwd=os.path.dirname(argv[0]))
    except OSError as exc:
        _finish(jid, error="could not run %s: %s" % (os.path.basename(argv[0]), exc))
        return
    timer = threading.Timer(timeout, kill)
    timer.daemon = True
    timer.start()
    try:
        # Line by line into the buffer as it arrives, so a poll shows what has
        # been printed so far. bb-doctor's connectivity check alone can take a
        # minute; a page that showed nothing until then would look hung.
        # readline() rather than iterating the pipe: iteration reads ahead in
        # blocks and released nothing until the tool exited, which is exactly
        # the behaviour this loop exists to avoid.
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            with _lock:
                j = _jobs.get(jid)
                if not j:
                    continue
                j["bytes"] += len(raw)
                if j["bytes"] <= OUTPUT_CAP:
                    j["lines"].append(line)
                else:
                    j["truncated"] = True     # keep draining so the tool can exit
        p.wait()
    finally:
        timer.cancel()
    if timed_out:
        _finish(jid, error="%s did not finish within %ds"
                % (os.path.basename(argv[0]), timeout))
    else:
        _finish(jid, exit_code=p.returncode)


def _finish(jid, exit_code=None, error=None):
    with _lock:
        j = _jobs.get(jid)
        if j:
            j["finished"] = time.time()
            j["exit_code"] = exit_code
            j["error"] = error
            j["state"] = "failed" if error else "done"
            if _running.get(j["tool"]) == jid:
                _running.pop(j["tool"], None)


def clear(name):
    """Forget the finished jobs of one tool, so the page can drop a result the
    user no longer wants shown. A running job is left alone; it finishes and
    can be cleared afterwards. Returns how many were dropped."""
    n = 0
    with _lock:
        for jid, j in list(_jobs.items()):
            if j["tool"] == name and j["state"] != "running":
                _jobs.pop(jid, None)
                n += 1
    return n


def status(jid):
    """Everything about a job, output included, or None."""
    now = time.time()
    with _lock:
        _sweep(now)
        j = _jobs.get(jid)
        if not j:
            return None
        t = TOOLS[j["tool"]]
        out = {k: j[k] for k in ("id", "tool", "options", "state", "started",
                                 "finished", "exit_code", "error", "truncated")}
        out["lines"] = list(j["lines"])
        out["elapsed"] = int((j["finished"] or now) - j["started"])
        # The command as the console would show it, so the page can say what ran.
        out["command"] = " ".join([os.path.basename(j["argv"][0])] + j["argv"][1:])
        if j["state"] == "done":
            out["result"] = t["exit"].get(j["exit_code"], "exited %s" % j["exit_code"])
        elif j["state"] == "failed":
            out["result"] = j["error"]
        else:
            out["result"] = "running"
        return out
