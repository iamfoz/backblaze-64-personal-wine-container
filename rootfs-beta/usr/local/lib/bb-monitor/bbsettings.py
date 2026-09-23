# Settings export and import: one JSON file holding everything this container
# keeps for itself, so a rebuild or a move to another host is a file rather
# than a copy of the whole appdata directory.
#
# What goes in, by section:
#   api_keys        the key records (hashes, labels, scopes, expiry) and the
#                   API switch. A restored hash is the same key, so a client
#                   that held bb64_... before keeps working.
#   notifications   endpoints, events, the skipped-files threshold
#   quiet_hours     the windows and the switch
#   warnings        dismissed warnings and hidden kinds
#   recovery        the automatic-recovery switch, when set on the page
#   client          the Backblaze client's own writable settings, as values;
#                   import writes them back through bzcli one at a time
#
# Never in the file: the computer identity (hguid), the drive stamps under
# .bzvol, the volume list, the drive selections and exclusions, or any backup
# state. A file that could stamp a second container with the same identity
# would set two machines on one backup, and Backblaze removes files when the
# stamps stop agreeing. Moving a backup is a copy of appdata or an inherit.
#
# Secrets. The key hashes, and the notification endpoints whole (a webhook
# URL is itself a secret), are the sensitive part. With a passphrase they go
# into the file encrypted; without one they are left out and the file records
# which sections are missing, so a file made without a passphrase is safe to
# keep anywhere and still restores everything else.
#
# The encryption uses nothing outside the standard library, which is all the
# image has: scrypt for the key, then HMAC-SHA256 as a keystream in counter
# mode and HMAC-SHA256 over nonce and ciphertext as the tag. The tag is checked
# before anything is decrypted, so a wrong passphrase and a damaged file are
# the same refusal. Every parameter is written into the file, so a later
# version can read an older file without guessing.
#
# Versioning. `version` is the file format. An import migrates an older file
# forward through MIGRATIONS before reading it, and refuses a newer one
# rather than guessing at fields it does not know.

import base64, hashlib, hmac, json, os, time

import bbapi, bbconfig, bbdismiss, bbnotify, bbquiet, bbrecover

FORMAT = "bb64-settings"
VERSION = 1
SECTIONS = ("api_keys", "notifications", "quiet_hours", "warnings", "recovery", "client")
SECRET_SECTIONS = ("api_keys", "notifications")

KDF = {"name": "scrypt", "n": 1 << 15, "r": 8, "p": 1}
CIPHER = "hmac-sha256-ctr"
MAX_FILE = 1 << 20          # a settings file is kilobytes; anything near this is not one

# Older formats are brought up to date one step at a time. None yet: the
# table is here so the first change to the format has a place to go and a
# test to prove it lands.
MIGRATIONS = {}


# ---- encryption --------------------------------------------------------------

def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    try:
        return base64.b64decode(s, validate=True)
    except (ValueError, TypeError):
        raise ValueError("the encrypted part of the file is not intact")


def _keys(passphrase, salt, n, r, p):
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("a passphrase is needed")
    dk = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=64,
                        maxmem=256 * 1024 * 1024)
    return dk[:32], dk[32:]


def _stream(key, nonce, length):
    out, counter = bytearray(), 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(passphrase, obj):
    """Encrypt a JSON-able object under a passphrase. Returns the box that
    goes into the file, with every parameter needed to open it."""
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _keys(passphrase, salt, KDF["n"], KDF["r"], KDF["p"])
    plain = json.dumps(obj, sort_keys=True).encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(plain, _stream(enc_key, nonce, len(plain))))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).hexdigest()
    return {"kdf": KDF["name"], "salt": _b64(salt), "n": KDF["n"], "r": KDF["r"], "p": KDF["p"],
            "cipher": CIPHER, "nonce": _b64(nonce), "data": _b64(cipher), "tag": tag}


def open_(passphrase, box):
    """The object seal() put in the box, or ValueError when the passphrase is
    wrong or the box has been altered; the two are not told apart."""
    if not isinstance(box, dict):
        raise ValueError("the encrypted part of the file is not intact")
    if box.get("kdf") != KDF["name"] or box.get("cipher") != CIPHER:
        raise ValueError("the file's encryption (%s, %s) is not one this build can open"
                         % (box.get("kdf"), box.get("cipher")))
    try:
        n, r, p = int(box["n"]), int(box["r"]), int(box["p"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("the encrypted part of the file is not intact")
    if not (1024 <= n <= 1 << 20 and 1 <= r <= 32 and 1 <= p <= 16):
        raise ValueError("the file's key derivation parameters are out of range")
    salt, nonce, cipher = _unb64(box.get("salt", "")), _unb64(box.get("nonce", "")), _unb64(box.get("data", ""))
    if len(salt) < 8 or len(nonce) < 8:
        raise ValueError("the encrypted part of the file is not intact")
    enc_key, mac_key = _keys(passphrase, salt, n, r, p)
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(tag, str(box.get("tag", ""))):
        raise ValueError("wrong passphrase, or the file has been altered")
    plain = bytes(a ^ b for a, b in zip(cipher, _stream(enc_key, nonce, len(cipher))))
    try:
        return json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("wrong passphrase, or the file has been altered")


# ---- export ------------------------------------------------------------------

def _client_settings():
    d = bbconfig.cached() if bbconfig.available() else None
    if not d:
        return None
    cur = d.get("settings") or {}
    return {k: cur.get(k) for k in bbconfig.WRITABLE if cur.get(k) is not None}


def export(passphrase=None, build=None):
    """The settings file as a dict. With a passphrase the secrets are in it,
    encrypted; without one they are left out and `omitted` names them."""
    notify = bbnotify.load()
    doc = {
        "format": FORMAT, "version": VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build": build or "",
        "sections": {
            "notifications": {"endpoints": [], "events": notify["events"],
                              "skipped_threshold": notify["skipped_threshold"]},
            "quiet_hours": bbquiet.load(),
            "warnings": {"dismissed": bbdismiss.load()["keys"],
                         "hidden": sorted(bbdismiss.hidden())},
        },
    }
    stored = bbrecover.load()
    if stored:
        doc["sections"]["recovery"] = {"watchdog": stored["enabled"]}
    client = _client_settings()
    if client:
        doc["sections"]["client"] = {"settings": client}
    else:
        doc.setdefault("notes", []).append("client settings not included: no reading was in hand")
    secrets = {"api_keys": {"records": bbapi._read(), "enabled": bbapi.enabled()},
               "endpoints": notify["endpoints"]}
    if passphrase:
        doc["secrets"] = seal(passphrase, secrets)
        doc["secret_sections"] = list(SECRET_SECTIONS)
    else:
        # The endpoints minus everything that identifies or authenticates: a
        # reminder of what was set up, not enough to send to it.
        doc["sections"]["notifications"]["endpoints"] = [
            {"id": e.get("id"), "label": e.get("label"), "kind": e.get("kind"),
             "url": "", "auth": e.get("auth", "none"), "user": "", "token": "",
             "template": e.get("template", "")}
            for e in notify["endpoints"]]
        doc["omitted"] = list(SECRET_SECTIONS)
    return doc


# ---- import ------------------------------------------------------------------

def _check_doc(doc):
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("not a Backblaze 64 settings file")
    try:
        ver = int(doc.get("version"))
    except (TypeError, ValueError):
        raise ValueError("the file has no version")
    if ver > VERSION:
        raise ValueError("the file is version %d and this build reads up to %d; update the container first"
                         % (ver, VERSION))
    while ver < VERSION:
        doc = MIGRATIONS[ver](doc)
        ver += 1
        doc["version"] = ver
    if not isinstance(doc.get("sections"), dict):
        raise ValueError("the file has no sections")
    return doc


def _check_key_records(records):
    if not isinstance(records, list):
        raise ValueError("api_keys must be a list of key records")
    out = []
    for r in records:
        if not isinstance(r, dict):
            raise ValueError("each key record must be an object")
        kid, h = str(r.get("id") or ""), str(r.get("hash") or "")
        if len(kid) != 8 or any(c not in "0123456789abcdef" for c in kid):
            raise ValueError("a key record has a malformed id")
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError("a key record has a malformed hash")
        scopes = r.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
            raise ValueError("a key record has malformed scopes")
        rec = {"id": kid, "label": bbnotify.clean_label(r.get("label")) or kid,
               "scopes": sorted(set(scopes))[:32], "hash": h,
               "created": int(r.get("created") or 0),
               "last_used": r.get("last_used") if isinstance(r.get("last_used"), int) else None,
               "revoked": r.get("revoked") if isinstance(r.get("revoked"), int) else None,
               "expires": r.get("expires") if isinstance(r.get("expires"), int) else None}
        out.append(rec)
    if len({r["id"] for r in out}) != len(out):
        raise ValueError("two key records share an id")
    return out


def _check_warnings(sec):
    if not isinstance(sec, dict):
        raise ValueError("warnings must be an object")
    keys = {}
    for key, rec in (sec.get("dismissed") or {}).items():
        if not bbdismiss.KEY.match(str(key)) or not isinstance(rec, dict):
            raise ValueError("a dismissed warning has a malformed key")
        keys[str(key)] = {"at": int(rec.get("at") or 0),
                          "text": str(rec.get("text") or "")[:200]}
    hidden = sec.get("hidden") or []
    if not isinstance(hidden, list) or not all(bbdismiss.KIND.match(str(k)) for k in hidden):
        raise ValueError("hidden warning kinds are malformed")
    return {"keys": keys, "hidden": sorted({str(k) for k in hidden})}


def import_(doc, passphrase=None, sections=None, write_client=None):
    """Apply a settings file. Returns a report {applied, skipped, notes}, or
    raises ValueError with a message for the page before anything is written:
    every section is checked first, so a bad file changes nothing.

    `sections` limits what is applied; None means everything in the file.
    `write_client` is the function that writes one client setting, for tests;
    bbconfig.write otherwise.
    """
    doc = _check_doc(doc)
    want = set(sections) if sections else set(SECTIONS)
    unknown = want - set(SECTIONS)
    if unknown:
        raise ValueError("unknown sections: %s" % ", ".join(sorted(unknown)))
    secs = doc["sections"]
    secrets = None
    if doc.get("secrets") is not None:
        if not passphrase:
            raise ValueError("this file's API keys and notification endpoints are encrypted; the passphrase is needed")
        secrets = open_(passphrase, doc["secrets"])
        if not isinstance(secrets, dict):
            raise ValueError("the encrypted part of the file is not intact")
    report = {"applied": [], "skipped": [], "notes": []}
    plan = []

    # Check everything before writing anything.
    if "notifications" in want and isinstance(secs.get("notifications"), dict):
        conf = dict(secs["notifications"])
        if secrets is not None and "endpoints" in secrets:
            conf["endpoints"] = secrets["endpoints"]
        elif doc.get("omitted"):
            conf["endpoints"] = []
            report["notes"].append("notification endpoints were not in the file (exported without a passphrase); existing ones kept")
        # save() validates as it writes; there is no dry pass through the
        # same checks, so a bad endpoint is reported from the write step, with
        # the sections before it already applied. It runs first for that reason.
        plan.append(("notifications", lambda c=conf, keep=bool(doc.get("omitted")): _save_notify(c, keep)))
    if "quiet_hours" in want and isinstance(secs.get("quiet_hours"), dict):
        plan.append(("quiet_hours", lambda c=secs["quiet_hours"]: (bbquiet.save(c), None)[1]))
    if "warnings" in want and isinstance(secs.get("warnings"), dict):
        rec = _check_warnings(secs["warnings"])
        plan.append(("warnings", lambda r=rec: _save_warnings(r)))
    if "api_keys" in want:
        if secrets is not None and isinstance(secrets.get("api_keys"), dict):
            records = _check_key_records(secrets["api_keys"].get("records"))
            enabled = bool(secrets["api_keys"].get("enabled", True))
            plan.append(("api_keys", lambda r=records, e=enabled: _save_keys(r, e)))
        elif doc.get("omitted"):
            report["skipped"].append("api_keys: not in the file (exported without a passphrase)")
    if "recovery" in want and isinstance(secs.get("recovery"), dict):
        wd = secs["recovery"].get("watchdog")
        if not isinstance(wd, bool):
            raise ValueError("recovery.watchdog must be true or false")
        plan.append(("recovery", lambda v=wd: (bbrecover.save(v), None)[1]))
    if "client" in want and isinstance(secs.get("client"), dict):
        values = secs["client"].get("settings") or {}
        if not isinstance(values, dict):
            raise ValueError("client settings must be an object")
        checked = {}
        for key, value in values.items():
            if key not in bbconfig.WRITABLE:
                raise ValueError("client setting %r is not one this container changes" % key)
            checked[key] = bbconfig._validate(key, value)[1]
        values = checked
        if values:
            if bbconfig.available():
                plan.append(("client", lambda v=values: _write_client(v, write_client or bbconfig.write)))
            else:
                report["skipped"].append("client: no bzcli in this image")

    for name, step in plan:
        try:
            detail = step()
        except ValueError as exc:
            raise ValueError("%s: %s" % (name, exc))
        report["applied"].append(name if not detail else "%s: %s" % (name, detail))
    return report


def _save_notify(conf, keep_endpoints):
    if keep_endpoints:
        conf = dict(conf, endpoints=bbnotify.load()["endpoints"])
    bbnotify.save(conf)
    return None


def _save_warnings(rec):
    with bbapi._mutate():
        bbdismiss._write(rec)
    return None


def _save_keys(records, enabled):
    with bbapi._mutate():
        bbapi._write(records)
    bbapi.set_enabled(enabled)
    return "%d key%s" % (len(records), "" if len(records) == 1 else "s")


def _write_client(values, write):
    done, failed = [], []
    for key, value in values.items():
        ok, detail = write(key, value)
        (done if ok else failed).append(key if ok else "%s (%s)" % (key, detail))
    out = "%d written" % len(done)
    if failed:
        out += "; not written: " + ", ".join(failed)
    return out
