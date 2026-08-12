# Diagnostic bundles over HTTP, wrapping bb-report.
#
# Two things shape this.
#
# Generation is not instant, and a request that blocks for a minute is a request
# that times out somewhere in between. So a POST starts a job and returns at once,
# and the caller polls.
#
# The download suits a browser, where the natural thing is a plain link, and a key
# in a query string ends up in history, in logs and in a Referer header. So the
# finished job hands back a single-use, short-lived URL carrying its own
# unguessable token. The key never appears in a URL, and a link that leaks stops
# working almost immediately.

import os, re, secrets, subprocess, threading, time

BB_REPORT = "/usr/local/bin/bb-report"
TOKEN_TTL = 300           # seconds a download link stays good
JOB_TTL = 3600            # seconds a finished job is remembered
TIMEOUT = 600             # bundling a large config is not quick

_lock = threading.Lock()
_jobs = {}                # id -> {state, started, path, error, token, token_expires}


def available():
    return os.path.exists(BB_REPORT)


def _sweep(now):
    for jid, j in list(_jobs.items()):
        if j["token"] and j["token_expires"] and now >= j["token_expires"]:
            j["token"] = None
        if now - j["started"] > JOB_TTL:
            _jobs.pop(jid, None)


def _run(jid):
    try:
        p = subprocess.run([BB_REPORT], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=TIMEOUT)
        out = (p.stdout or b"").decode("utf-8", "replace")
        # bb-report prints the path it wrote. Parsed rather than guessed at from
        # the newest file in the directory, which would race a second job.
        m = re.search(r"^\s*(\S+bb-report-[\w-]+\.zip)\b", out, re.M)
        with _lock:
            j = _jobs.get(jid)
            if not j:
                return
            if p.returncode == 0 and m and os.path.exists(m.group(1)):
                j.update(state="done", path=m.group(1),
                         token=secrets.token_urlsafe(32),
                         token_expires=time.time() + TOKEN_TTL)
            else:
                err = (p.stderr or b"").decode("utf-8", "replace").strip()
                j.update(state="failed",
                         error=err or "bb-report exited %d" % p.returncode)
    except subprocess.TimeoutExpired:
        with _lock:
            if jid in _jobs:
                _jobs[jid].update(state="failed",
                                  error="bb-report did not finish within %ds" % TIMEOUT)
    except OSError as exc:
        with _lock:
            if jid in _jobs:
                _jobs[jid].update(state="failed", error=str(exc))


def start():
    """(job_id, already_running). One bundle at a time: a second request while one
    is in flight joins the existing job rather than running bb-report twice over
    the same config."""
    now = time.time()
    with _lock:
        _sweep(now)
        for jid, j in _jobs.items():
            if j["state"] == "running":
                return jid, True
        jid = secrets.token_hex(6)
        _jobs[jid] = {"state": "running", "started": now, "path": None,
                      "error": None, "token": None, "token_expires": None}
    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return jid, False


def status(jid):
    now = time.time()
    with _lock:
        _sweep(now)
        j = _jobs.get(jid)
        if not j:
            return None
        out = {"job": jid, "state": j["state"],
               "started": int(j["started"]), "error": j["error"]}
        if j["state"] == "done":
            out["size_bytes"] = (os.path.getsize(j["path"])
                                 if j["path"] and os.path.exists(j["path"]) else None)
            if j["token"]:
                out["download"] = "report/download/" + j["token"]
                out["download_expires_in"] = int(j["token_expires"] - now)
        return out


def claim(token):
    """The file this token unlocks, and burn the token. None if unknown, expired
    or already used."""
    now = time.time()
    with _lock:
        _sweep(now)
        for j in _jobs.values():
            if j["token"] and secrets.compare_digest(j["token"], token):
                j["token"] = None          # single use, spent on the way out
                path = j["path"]
                return path if path and os.path.exists(path) else None
    return None
