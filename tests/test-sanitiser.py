#!/usr/bin/env python3
"""Adversarial tests for bb-report's sanitiser.

bb-report produces a bundle intended to be posted publicly on a forum or issue
tracker, built from files that contain a live authentication token, the AES key
and IV, the wrapped file encryption key, and the full path of whatever is being
uploaded. A sanitiser bug here does not cause a crash - it quietly publishes
someone's private data - so this runs in CI rather than depending on anyone
remembering to check.

The fixtures use the real shapes seen in this container, not invented ones. Two
genuine leaks were caught this way during development: path matching stopped at
the first space, and then at the first apostrophe, leaving names like
"Mum's Old Hard Disk\\tax return 2019.pdf" sitting in the output in full.

Run:  python3 tests/test-sanitiser.py
"""
import importlib.util, os, re, sys, tempfile, zipfile
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "rootfs", "usr", "local", "bin", "bb-report")
spec = importlib.util.spec_from_loader("bbreport", SourceFileLoader("bbreport", TOOL))
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)

FAIL = []
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)

SALT = b"\x01" * 16
h = bb.hasher(SALT)
S = lambda t: bb.sanitise(t, h)

# --- secrets, in the exact shapes the per-thread XML carries ----------------------
AUTH = "1_20260628184950_9879b09491950b9608b19db6_f31f31634e85300804bf04edea23e99030d3341c"
AESKEY = "d0cb18f015b39363b2d14c333a13ae8e"
AESIV = "c0dca308a999ea7c9f7dc18de0856030"
FEK = "16388056dee3775d3ec0ee271de4aadb1aee20e2873fd1fa3946395e5903f6f9642bcc959b4fff19"
DONEHEX = "35092b092d2d2d09323032363036323832303338313209345f6861"
XML = f'''<bztran_thread_instruction which_thread="003"
 bz_auth_token="{AUTH}"
 hex_encoded_aesKey128="{AESKEY}"
 hex_encoded_aesIV128="{AESIV}"
 hex_encoded_encryptedFEK="{FEK}"
 hex_encoded_bz_done_line="{DONEHEX}" />'''

out = S(XML)
for name, secret in [("auth token", AUTH), ("AES key", AESKEY), ("AES IV", AESIV),
                     ("wrapped file encryption key", FEK), ("hex bz_done line", DONEHEX)]:
    ok(secret not in out, f"{name} does not survive sanitising")

# --- personal data in log lines ----------------------------------------------------
LOG = r"""2026-07-05 10:00:01 uploading D:\FozStore\Plex [Tertiary]\Mum's Old Hard Disk\tax return 2019.pdf
2026-07-05 10:00:02 reading /drive_d/FozStore/Photos/Wedding/IMG_8108.CR2
2026-07-05 10:00:03 account jane.doe@example.com from 192.168.1.42 mac 00:1b:44:11:3a:b7
2026-07-05 10:00:04 serial ABCDEF0123456789ABCDEF0123456789
"""
out = S(LOG)
for term in ["FozStore", "Mum's Old Hard Disk", "tax return 2019", "Wedding", "IMG_8108",
             "Plex", "jane.doe@example.com", "192.168.1.42", "00:1b:44:11:3a:b7"]:
    ok(term not in out, f"'{term}' does not survive sanitising")
for marker in ["<email>", "<ip>", "<mac>"]:
    ok(marker in out, f"replacement marker {marker} is present")
ok("<redacted-hex>" in S("blob " + "a" * 64), "long hex blobs are redacted wholesale")

# --- what must survive, because it locates a problem without identifying anyone -----
tail = S(r"uploading D:\FozStore\Photos\IMG_1.CR2")
ok(".CR2" in tail.upper(), "file extensions survive")
ok("D:" in tail, "drive letter survives")
ok("/drive_d/" in S(r"reading /drive_d/a/b.jpg"), "mount root survives")

# --- hashing behaviour the documentation promises -----------------------------------
a = S(r"reading /drive_d/Photos/one.jpg")
b = S(r"reading /drive_d/Photos/two.jpg")
ok(a.split("/")[2] == b.split("/")[2], "files in one directory share a directory hash")
ok(a != b, "different files hash differently")
ok(a == S(r"reading /drive_d/Photos/one.jpg"), "hashing is stable, so bundles are comparable")
ok(a != bb.sanitise(r"reading /drive_d/Photos/one.jpg", bb.hasher(b"\x02" * 16)),
   "a rotated salt breaks the link with earlier bundles")
ok(not re.search(r"Photos", a), "the directory name is absent from hashed output")

# --- arbitrary mount roots must not leak (the README documents user-named mounts) ----
leak = S(r"lrwxrwxrwx 1 app app 10 Jan 16 13:43 e: -> /Kids_Photos_2019/")
ok("Kids_Photos_2019" not in leak, "user-named mount target is hashed in ls output")
ok("volume1/homes/jane" not in S(r"df: /volume1/homes/jane 20T"), "Synology-style path is hashed")
ok("kids_photos" not in S(r"shfs 20T 8T 12T 40% /kids_photos"), "df mount point is hashed")
ok("Jane Private" not in S(r"reading /backup_volume/Jane Private/x.pdf"), "custom mount contents hashed")

# --- container-internal paths stay readable (diagnostics must stay legible) ----------
ok("/usr/local/bin/bb-monitor" in S("app 1 /usr/local/bin/bb-monitor"), "system tool path verbatim")
ok("/config/wine/dosdevices" in S("ls /config/wine/dosdevices"), "config path verbatim")
url = "https://ca000.backblaze.com/api/clientversion.xml"
ok(url in S("fetching " + url), "URLs are not mangled as paths")

# --- dotted directory names are names, not extensions --------------------------------
dotted = S(r"uploading D:\Shared\Jane.Doe\cv.docx")
ok("doe" not in dotted.lower().replace("docx", ""), "per-person folder Jane.Doe fully hashed")
ok(".docx" in dotted, "real extension on the final component kept")
ok("2019" not in S(r"reading /drive_d/Backup.2019/x.jpg"), "unknown dot-suffix on a directory hashed")
ok("2019" not in S(r"reading /drive_d/photos/Backup.2019"), "unknown dot-suffix on final component hashed")

# --- salt integrity ------------------------------------------------------------------
import tempfile as _tf
_sd = _tf.mkdtemp()
bb.SALT_FILE = os.path.join(_sd, ".bb-report-salt")
open(bb.SALT_FILE, "wb").close()                                   # 0-byte salt
s1, created = bb.load_salt()
ok(created and len(s1) == 16, "zero-length salt is rejected and regenerated")
os.chmod(bb.SALT_FILE, 0o666)                                      # flattened perms
s2, created = bb.load_salt()
ok(s2 == s1 and not created, "valid salt is kept across loads")
ok(oct(os.stat(bb.SALT_FILE).st_mode & 0o777) == "0o600", "mode re-tightened to 0600 on load")

# --- the salt must never escape ------------------------------------------------------
ok(bb.SALT_FILE not in " ".join(n for n, _, _ in bb.build_items()),
   "the salt file is not in the collection list")
ok(SALT.hex() not in S("salt=%s" % SALT.hex()), "a salt-shaped key/value is redacted")

# --- environment handling -------------------------------------------------------------
os.environ["VNC_PASSWORD"] = "supersecret"
os.environ["ENABLE_WATCHDOG"] = "true"
env = bb.collect_env()
ok("supersecret" not in env, "unknown environment variables are hidden")
ok("ENABLE_WATCHDOG=true" in env, "known settings are shown, being diagnostic")

# --- end to end: a real bundle must not contain the salt --------------------------------
tmp = tempfile.mkdtemp()
bb.CONFIG = tmp
bb.SALT_FILE = os.path.join(tmp, ".bb-report-salt")
bb.OUT_DIR = tmp
rc = bb.main()
zips = [f for f in os.listdir(tmp) if f.endswith(".zip")]
ok(rc == 0 and zips, "a bundle is produced")
if zips:
    z = zipfile.ZipFile(os.path.join(tmp, zips[0]))
    blob = b"".join(z.read(n) for n in z.namelist())
    salt = open(bb.SALT_FILE, "rb").read()
    ok(salt not in blob and salt.hex().encode() not in blob,
       "the salt is absent from the produced bundle")
    ok(oct(os.stat(bb.SALT_FILE).st_mode & 0o777) == "0o600", "the salt file is not world readable")
    ok(b"Hash epoch" in z.read("README.txt"), "the bundle records its hash epoch")

print()
print("%d failures" % len(FAIL))
sys.exit(1 if FAIL else 0)
