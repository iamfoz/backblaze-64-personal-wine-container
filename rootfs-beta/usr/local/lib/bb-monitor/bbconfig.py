# What the client will tell us about itself, through its own bzcli.
#
# bzcli report answers a query path with that subtree as JSON. That matters more
# than it sounds: the whole document carries the account email, the login and the
# host guid, and this container has no business holding any of them. Querying by
# path means they never enter this process. PATHS below is the complete list of
# what is read, and nothing from a request reaches a query string.
#
# Every call starts a Wine process, so this is not a two-second poll. Readings
# are cached for TTL seconds and refreshed after a write.
#
# The writable side is a separate whitelist again, with a validator per key,
# because bzcli's own --print preview renders the resulting file without checking
# it: a preview of backup_schedule_type=nonsense came back with "nonsense" in it.
# The client does validate on apply, but a control that offers a bad value and
# lets the client reject it is a worse control than one that knows the answer.

import json, os, re, signal, subprocess, threading, time

BZCLI = "/config/wine/drive_c/Program Files/Backblaze/bzcli.exe"
PREFIX = os.environ.get("WINEPREFIX", "/config/wine")
TIMEOUT = 90              # a cold Wine start is slow; a hung call must not hold a worker
TTL = 300                 # seconds a reading stays good

# The subtrees worth reading, and the only ones this container ever asks for.
# /backup/account is deliberately absent: it holds the email and the login.
# /backup/installation is read whole for has_pek and version, which also brings
# hguid and the install directory; both are dropped in _shape() rather than
# queried, because one call for the subtree beats three for its leaves.
PATHS = ("/backup/license", "/backup/installation", "/backup/datacenter",
         "/backup/status", "/settings")

# Keys this container will write, with what each means in the client's own words
# (from bzcli's help text, so the wording matches what the settings window says)
# and a validator. A key absent from here cannot be written, whatever a request
# asks for.
#
# Deliberately absent: bzdirfilter_* and excludefiletypes_*, which decide what is
# backed up and where a wrong edit stops a backup; backup_schedule_*, because the
# container's own quiet hours already cover that ground and two mechanisms
# fighting over one behaviour is a fault waiting to happen; and lock_PEK,
# lock_schedule and lock_exclusion, which can leave someone unable to change
# their own settings from either interface.


def _int_in(lo, hi):
    def check(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number")
        if not (lo <= n <= hi):
            raise ValueError("must be between %d and %d" % (lo, hi))
        return n
    return check


def _bool(v):
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("true", "1", "yes", "on"):
        return True
    if str(v).lower() in ("false", "0", "no", "off"):
        return False
    # The message names what is accepted, so a caller typing "maybe" is told
    # the whole vocabulary rather than a subset of it.
    raise ValueError("must be true or false (1, 0, yes, no, on and off are read the same way)")


def _hostname(v):
    v = str(v).strip()
    if not v or len(v) > 64 or not re.match(r'^[A-Za-z0-9][A-Za-z0-9 ._-]*$', v):
        raise ValueError("must be 1 to 64 characters, letters, digits, space, dot, dash or underscore")
    return v


WRITABLE = {
    "num_backup_threads": {
        "label": "Backup threads",
        "does": "Use up to this many threads (background processes) for backup.",
        "check": _int_in(1, 100),
        # The client opens what it has work for, so this is a ceiling. Under Wine
        # each thread is also bounded by the send buffer divided by the round
        # trip, which is usually the smaller of the two limits.
        "note": "A ceiling rather than a target. Each thread is also limited by "
                "the round trip; see the rate on the Monitor.",
    },
    "net_auto_throttle": {
        "label": "Automatic throttle",
        "does": "'true' lets the backup software manage its network bandwidth.",
        "check": _bool,
    },
    "net_throttle": {
        "label": "Manual throttle",
        "does": "For manual throttle, each thread will use up to this many Mbps.",
        "check": _int_in(1, 10000),
        "unit": "Mbps per thread",
        "note": "Only applies with the automatic throttle off.",
    },
    "max_filesize_mb": {
        "label": "Largest file to back up",
        "does": "Don't back up files larger than this many MB.",
        "check": _int_in(1, 1000000000),
        "unit": "MB",
    },
    "backup_on_battery": {
        "label": "Back up on battery",
        "does": "Whether to back up when on battery power.",
        "check": _bool,
        "note": "Of little consequence in a container, which has no battery.",
    },
    "numdays_warn_if_no_backup": {
        "label": "Warn after this many days",
        "does": "Days without a completed backup before the client warns.",
        "check": _int_in(1, 365),
        "unit": "days",
        "note": "The monitor's own staleness warning reads the same figure.",
    },
    "online_hostname": {
        "label": "Name shown in your Backblaze account",
        "does": "The name this computer appears under at backblaze.com.",
        "check": _hostname,
    },
    "allow_network_tests": {
        "label": "Allow network speed tests",
        "does": "Whether the client may measure the connection.",
        "check": _bool,
        "note": "The client's measured throughput on the Monitor comes from these.",
    },
}

_lock = threading.Lock()
# Held across the whole query loop, so only one sweep of PATHS is ever in flight.
# _lock guards the cache dictionary itself and is never held while wine runs.
# "started" is when the sweep in flight began, which is what a forced caller has
# to compare against: a sweep that began before the caller's write cannot be
# carrying the value the write just set.
_sweep_lock = threading.Lock()
_cache = {"at": 0, "data": None, "error": None, "started": 0}


def available():
    return os.path.exists(BZCLI)


def _env():
    # Matched to bbctl.run: s6 hands the service a minimal environment, and wine
    # without HOME tries to build a fresh prefix somewhere it cannot write.
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    env.setdefault("HOME", "/config")
    if "/opt/wine/bin" not in env.get("PATH", ""):
        env["PATH"] = "/opt/wine/bin:" + env.get("PATH", "/usr/bin:/bin")
    return env


def _kill_group(p):
    """SIGKILL the whole process group `p` leads, and never raise.

    Killing the Popen kills wine and nothing wine started. A wedged client (the
    OOM wedge in the project's notes) leaves a child behind holding the pipes it
    inherited, and that child then runs for the life of the container. The
    caller starts the process with start_new_session=True, so the group holds
    this one call and nothing else: the client's own long-running wineserver was
    started elsewhere and is in another session, out of reach of this.
    """
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass


def _drain_after_kill(p):
    """Collect what a killed process left, without waiting on it for ever.

    A grandchild that made a session of its own escapes _kill_group and keeps the
    write end open, so even this second read has to be bounded. The pipes are
    closed by hand in that case: the caller is giving up on them, and a hung
    call an hour ago should not still be holding two file descriptors.
    """
    try:
        p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        for pipe in (p.stdout, p.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


def _run(args):
    """(ok, stdout, detail). Never raises."""
    try:
        # Popen and an explicit drain rather than subprocess.run: run() kills
        # only the direct child when its timeout fires, so every timed-out call
        # used to leave a wine tree behind on a container that is short of
        # memory to begin with.
        p = subprocess.Popen(["wine", BZCLI] + args, env=_env(),
                             cwd=os.path.dirname(BZCLI), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    except FileNotFoundError:
        # Either wine or the install directory. Saying "wine is not on PATH" for
        # a missing Backblaze directory sends the reader after the wrong thing.
        if not os.path.isdir(os.path.dirname(BZCLI)):
            return False, "", "the Backblaze install directory is not there: %s" % os.path.dirname(BZCLI)
        return False, "", "wine is not on PATH"
    except OSError as exc:
        return False, "", "could not run bzcli: %s" % exc
    try:
        out_b, err_b = p.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        _drain_after_kill(p)
        return False, "", "bzcli did not finish within %ds" % TIMEOUT
    out = (out_b or b"").decode("utf-8", "replace").strip()
    err = (err_b or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0:
        return False, out, err or ("bzcli exited %d" % p.returncode)
    return True, out, ""


def _query(path):
    """One subtree, parsed. Scalars come back bare rather than as JSON, so a
    value that will not parse is returned as the string it is."""
    ok, out, detail = _run(["report", "-v", path])
    if not ok:
        return None, detail
    try:
        return json.loads(out), ""
    except ValueError:
        return out, ""


def _shape(raw):
    """Only the fields this container has a use for. Anything that identifies
    the account or the machine is dropped here rather than carried further."""
    lic = raw.get("/backup/license") or {}
    inst = raw.get("/backup/installation") or {}
    dc = raw.get("/backup/datacenter") or {}
    st = raw.get("/backup/status") or {}
    settings = raw.get("/settings") or {}
    fail = (lic.get("renewal_fail") or {}).get("gmt_time")
    return {
        "license": {"status": lic.get("status"), "type": lic.get("type"),
                    "renewal_failure": lic.get("renewal_failure"),
                    "renewal_failed_at": None if fail in (None, "none") else fail},
        "encrypted": inst.get("has_pek"),
        "client_version": inst.get("version"),
        "cluster": dc.get("cluster"),
        "cluster_url": dc.get("url"),
        "safety_freeze": st.get("safety_freeze"),
        "summary": st.get("summary"),
        "transmit": st.get("bztransmit"),
        "installed": st.get("installed"),
        "settings": {k: settings.get(k) for k in WRITABLE if k in settings},
        # The selection tree, which decides whether a drive is backed up at all.
        # Kept separate from the writable settings: it is read here and never
        # written, and it names directories on the user's own share, so it is
        # withheld from a key without read:files the way the skipped list is.
        "drive_filters": settings.get("bzdirfilter_add"),
        "schedule": {k: settings.get(k) for k in
                     ("backup_schedule_type", "backup_schedule_detail",
                      "backup_schedule_end_hour")},
    }


def read(force=False):
    """The cached reading, refreshed when stale. {ok, at, error, ...} or None
    when bzcli is not in this image."""
    if not available():
        return None
    entered = time.time()
    with _lock:
        fresh = _cache["data"] is not None and entered - _cache["at"] < TTL
        if fresh and not force:
            return _cache["data"]
    # One sweep at a time. Each path is a wine start, so four callers arriving
    # on a stale cache used to run twenty of them at once on a container with
    # little memory to spare, and each could block for len(PATHS) * TIMEOUT.
    # write() calls read(force=True) from a request thread, which the poll
    # thread's own guard does not cover, so the serialising has to live here.
    with _sweep_lock:
        # The sweep we queued behind may already have fetched what we came for.
        with _lock:
            data = _cache["data"]
            if data is not None:
                if force:
                    # Good enough only if it started after we asked, and so
                    # after whatever change prompted the forced read.
                    if _cache["started"] >= entered:
                        return data
                elif time.time() - _cache["at"] < TTL:
                    return data
            _cache["started"] = time.time()
        now = time.time()
        raw, errors = {}, []
        for path in PATHS:
            val, detail = _query(path)
            if detail:
                errors.append("%s: %s" % (path, detail))
            raw[path] = val
        data = _shape(raw)
        data["ok"] = not errors
        data["at"] = int(now)
        data["error"] = "; ".join(errors) or None
        with _lock:
            _cache.update(at=now, data=data, error=data["error"])
        return data


def cached():
    """Whatever is in hand, without starting a Wine process."""
    with _lock:
        return _cache["data"]


def describe():
    """The writable keys for display, with the current value beside each."""
    data = cached() or {}
    cur = (data.get("settings") or {})
    out = []
    for key in ("num_backup_threads", "net_auto_throttle", "net_throttle",
                "max_filesize_mb", "numdays_warn_if_no_backup",
                "allow_network_tests", "backup_on_battery", "online_hostname"):
        spec = WRITABLE[key]
        out.append({"key": key, "label": spec["label"], "does": spec["does"],
                    "unit": spec.get("unit"), "note": spec.get("note"),
                    "value": cur.get(key),
                    "type": "bool" if spec["check"] is _bool
                            else ("text" if key == "online_hostname" else "int")})
    return out


def preview(key, value):
    """What the configuration would become, without changing it. Confirmed inert
    on a live container: a preview setting a value left the real one alone."""
    key, value = _validate(key, value)
    ok, out, detail = _run(["configure", "-p", "-v", "%s=%s" % (key, _fmt(value))])
    return ok, (out if ok else detail)


def write(key, value):
    """Apply one setting. (ok, detail). Refreshes the cache on success, so a
    caller reading straight afterwards sees the new value rather than the old."""
    key, value = _validate(key, value)
    ok, out, detail = _run(["configure", "-v", "%s=%s" % (key, _fmt(value))])
    if ok:
        read(force=True)
        return True, out or "set"
    return False, detail or "bzcli refused the change"


def _validate(key, value):
    spec = WRITABLE.get(key)
    if not spec:
        raise ValueError("not a setting this container will change")
    return key, spec["check"](value)


def _fmt(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---- what the readings mean --------------------------------------------------

def health(data=None):
    """Warnings drawn from what the client reports about itself, in the same
    (kind, text) shape as bbdata.health()."""
    d = data if data is not None else cached()
    if not d:
        return []
    out = []
    lic = d.get("license") or {}
    status = (lic.get("status") or "").lower()
    # billing_active is the healthy reading seen on a live container. Anything
    # else is reported rather than interpreted: the set of values is not
    # established, and a wrong guess about someone's billing is worse than a
    # plain statement of what the client said.
    if status and status not in ("billing_active", "active", "trial"):
        out.append(("licence", "Backblaze reports the licence as %s" % lic["status"]))
    if lic.get("renewal_failure") and lic["renewal_failure"] != "none":
        out.append(("renewal", "The last licence renewal failed (%s)" % lic["renewal_failure"]))
    if d.get("safety_freeze") not in (None, "not_frozen"):
        out.append(("frozen", "Backblaze has safety-frozen this backup"))
    return out


def drive_selection(data=None):
    """Per drive, whether the client is set to back it up at all.

    The filter list carries a root entry per drive, and that entry decides it:
    a drive whose root reads "none" shows as ticked in the settings window and
    backs up nothing, which is the state behind "No files are selected".
    Returns {"D:\\\\": {"whichfiles": "all", "backed_up": True, "system": False},
    ...} or None. "system" says the drive is the container rather than the
    user's data; see _is_system_drive().
    """
    d = data if data is not None else cached()
    filters = (d or {}).get("drive_filters")
    if not filters:
        return None
    out = {}
    for f in filters:
        if not isinstance(f, dict):
            continue
        path = (f.get("dir") or "").strip()
        # A root entry is a bare drive letter: "d:\". Anything deeper is an
        # exception within a drive rather than a statement about the drive.
        if re.match(r'^[A-Za-z]:\\$', path):
            out[path.upper()] = {"whichfiles": f.get("whichfiles"),
                                 "backed_up": f.get("whichfiles") == "all",
                                 "system": _is_system_drive(path[0])}
    return out or None


def _is_system_drive(letter):
    """Whether this drive letter is the container rather than the user's data.

    Two of them are, on every container. C: is the Wine prefix: the client's own
    install and the Windows layer it runs on, which nobody should be backing up
    and which the client is right not to be selecting. Z: is Wine's own mapping
    of the container root, and is the same story by a different route, so it is
    found rather than named: the letter Wine maps to "/" is a convention and not
    a promise, and a prefix that maps a different one should be read the same
    way. Anything else is a mounted share, where a root set to "none" really is
    the fault the warning describes.
    """
    if letter.upper() == "C":
        return True
    link = os.path.join(PREFIX, "dosdevices", letter.lower() + ":")
    try:
        return os.path.realpath(link) == "/"
    except OSError:
        return False


def excluded_dirs(data=None):
    """The directories set to "none", lowercased as the client stores them."""
    d = data if data is not None else cached()
    filters = (d or {}).get("drive_filters") or []
    return [f.get("dir") for f in filters
            if isinstance(f, dict) and f.get("whichfiles") == "none" and f.get("dir")]
