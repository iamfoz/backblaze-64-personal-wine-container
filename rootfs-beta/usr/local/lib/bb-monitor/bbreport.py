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

# Where bundles made from the web live, and what they are called. bb-report
# itself writes bb-report-<stamp>.zip into /config, which is where a console run
# leaves them. A bundle made from the web is moved here under a name that says
# what it is and when it was made, so the Tools tab can list, hand out and
# delete them without ever touching anything else in /config. The name is
# checked against this pattern before any path is built from it, so a request
# cannot name a file outside this directory.
DIAG_DIR = "/config/bb-diag"
NAME_RE = re.compile(r"^backblaze64-diag-\d{12}(-\d+)?\.zip$")
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
                j.update(state="done", path=_store(m.group(1)),
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


def _store(src):
    """Move a finished bundle into DIAG_DIR under its dated name. Returns the
    path it ends up at, which is the original if the move cannot be made: a
    bundle that could not be renamed is still a bundle."""
    try:
        os.makedirs(DIAG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M")
        dest = os.path.join(DIAG_DIR, "backblaze64-diag-%s.zip" % stamp)
        n = 2
        while os.path.exists(dest):          # two in one minute
            dest = os.path.join(DIAG_DIR, "backblaze64-diag-%s-%d.zip" % (stamp, n))
            n += 1
        os.replace(src, dest)                # same filesystem, so atomic
        return dest
    except OSError:
        return src


def listing():
    """Bundles in DIAG_DIR, newest first: [{name, size_bytes, modified}]."""
    out = []
    try:
        names = os.listdir(DIAG_DIR)
    except OSError:
        return out
    for n in names:
        if not NAME_RE.match(n):
            continue
        try:
            st = os.stat(os.path.join(DIAG_DIR, n))
        except OSError:
            continue
        out.append({"name": n, "size_bytes": st.st_size, "modified": int(st.st_mtime)})
    out.sort(key=lambda r: r["modified"], reverse=True)
    return out


def path_for(name):
    """The bundle a name refers to, or None. The name must match NAME_RE, which
    admits no separator, so the result is always inside DIAG_DIR."""
    if not name or not NAME_RE.match(name):
        return None
    p = os.path.join(DIAG_DIR, name)
    return p if os.path.isfile(p) else None


def delete(name):
    p = path_for(name)
    if not p:
        return False
    try:
        os.unlink(p)
    except OSError:
        return False
    with _lock:
        for j in _jobs.values():
            if j["path"] == p:
                j["path"] = None
                j["token"] = None
    return True


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


def current():
    """The id of the job in flight, or None, so a page can pick up a run that
    started before it loaded."""
    with _lock:
        for jid, j in _jobs.items():
            if j["state"] == "running":
                return jid
    return None


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
            out["name"] = os.path.basename(j["path"]) if j["path"] else None
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
