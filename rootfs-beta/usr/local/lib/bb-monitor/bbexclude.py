# The client's XML exclusion rules, managed from the Settings tab.
#
# Backblaze reads bzexcluderules_editable.xml from bzdata on every pass and
# applies each rule to every attached volume, which is what a user mapping
# twenty Unraid disks wants: one rule excludes \Media\ on all of them, where the
# GUI's folder exclusions have to be added disk by disk. The client writes the
# file itself, with its own optional Windows rules, and regenerates it if it is
# removed (its documentation: "Configure custom exclusions using XML").
#
# Rules made here live between two comment markers so the client's own rules
# and anything edited by hand are kept byte for byte; only the block between
# the markers is rewritten. The file's own format is the contract:
#
#   <excludefname_rule plat="win" osVers="*" ruleIsOptional="t"
#       skipFirstCharThenStartsWith=":\Media\" contains_1="*" contains_2="*"
#       doesNotContain="*" endsWith="*" hasFileExtension="*" />
#
# A file matches a rule when every criterion matches; "*" skips one. The
# first character of the path is the drive letter, so the prefix starts with
# ":\". The client's documentation says the prefix is "*" or at least four
# characters, and that it is the one criterion that prunes cheaply, so the
# page asks for it first.

import html, os, re, tempfile

BZ = "/config/wine/dosdevices/c:/ProgramData/Backblaze/bzdata"
FILE = BZ + "/bzexcluderules_editable.xml"
BEGIN = "<!-- Backblaze 64: rules managed from the Settings tab; edit them there, not here -->"
END = "<!-- Backblaze 64: end of managed rules -->"
FIELDS = ("skipFirstCharThenStartsWith", "contains_1", "contains_2",
          "doesNotContain", "endsWith", "hasFileExtension")
FIXED = 'plat="win" osVers="*" ruleIsOptional="t"'
MAX_RULES = 200
_RULE = re.compile(r"<excludefname_rule\b([^>]*?)/>", re.S)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _parse(block):
    out = []
    for m in _RULE.finditer(block):
        attrs = {k: html.unescape(v) for k, v in _ATTR.findall(m.group(1))}
        out.append({f: attrs.get(f, "*") for f in FIELDS})
    return out


def _split(text):
    """(before, managed block, after). The markers may be absent."""
    i = text.find(BEGIN)
    j = text.find(END, i + len(BEGIN)) if i >= 0 else -1
    if i < 0 or j < 0:
        return text, "", None
    return text[:i], text[i + len(BEGIN):j], text[j + len(END):]


def _read():
    try:
        with open(FILE, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def load():
    """{present, rules, other, file}: the managed rules with their index, the
    count of rules outside the managed block, and whether the file exists."""
    text = _read()
    if text is None:
        return {"present": False, "rules": [], "other": 0, "file": FILE}
    before, block, after = _split(text)
    rules = _parse(block)
    for i, r in enumerate(rules):
        r["index"] = i
    other = len(_parse(before)) + (len(_parse(after)) if after is not None else 0)
    return {"present": True, "rules": rules, "other": other, "file": FILE}


def normalise(rule):
    """One rule as the page sends it, checked and in the client's own form.
    Raises ValueError with a message for the page."""
    if not isinstance(rule, dict):
        raise ValueError("a rule must be an object")
    out = {}
    for f in FIELDS:
        v = rule.get(f, "*")
        if v is None:
            v = "*"
        if not isinstance(v, str):
            raise ValueError("%s must be text" % f)
        v = v.strip()
        if "\n" in v or "\r" in v or '"' in v:
            raise ValueError("%s cannot contain a quote or a line break" % f)
        if len(v) > 500:
            raise ValueError("%s is too long" % f)
        out[f] = v or "*"
    p = out["skipFirstCharThenStartsWith"]
    if p != "*":
        # The first character of a path is the drive letter, which the rule
        # skips: "D:\Media\" and "\Media\" and "Media\" all mean ":\Media\".
        p = p.replace("/", "\\")
        if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
            p = p[1:]
        if not p.startswith(":"):
            p = ":" + (p if p.startswith("\\") else "\\" + p)
        if len(p) < 4:
            raise ValueError("the folder path must be at least four characters, such as :\\Media\\")
        out["skipFirstCharThenStartsWith"] = p
    ext = out["hasFileExtension"]
    if ext != "*":
        ext = ext.lstrip(".")
        if not ext or re.search(r"[\s/\\.]", ext):
            raise ValueError("the extension is the part after the last dot, with no spaces or slashes")
        out["hasFileExtension"] = ext
    if all(v == "*" for v in out.values()):
        raise ValueError("a rule with every criterion left as * would exclude every file")
    return out


def _render(rules):
    lines = [BEGIN]
    for r in rules:
        attrs = " ".join('%s=%s' % (f, _quote(r[f])) for f in FIELDS)
        lines.append("<excludefname_rule %s %s />" % (FIXED, attrs))
    lines.append(END)
    return "\n".join(lines)


def _quote(v):
    return '"' + html.escape(v, quote=True) + '"'


def save(rules):
    """Replace the managed block with these rules (already normalised) and
    write the file back with everything else untouched. Returns load()."""
    if len(rules) > MAX_RULES:
        raise ValueError("at most %d managed rules" % MAX_RULES)
    text = _read()
    if text is None:
        raise ValueError("the client has not written its exclusions file yet; "
                         "it appears after the first backup pass")
    before, block, after = _split(text)
    body = _render(rules)
    if after is None:
        # No block yet: put it just inside the closing tag, or at the end.
        k = text.rfind("</bzexclusions>")
        if k >= 0:
            new = text[:k].rstrip("\n") + "\n" + body + "\n" + text[k:]
        else:
            new = text.rstrip("\n") + "\n" + body + "\n"
    else:
        new = before + body + after
    d = os.path.dirname(FILE)
    try:
        st = os.stat(FILE)
        with open(FILE + ".bb64-bak", "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        st = None
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".bzexcluderules.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        if st is not None:
            os.chmod(tmp, st.st_mode & 0o777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        os.replace(tmp, FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return load()


def add(rule):
    rules = [{f: r[f] for f in FIELDS} for r in load()["rules"]]
    rules.append(normalise(rule))
    return save(rules)


def update(index, rule):
    rules = [{f: r[f] for f in FIELDS} for r in load()["rules"]]
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(rules):
        raise ValueError("no managed rule with that index")
    rules[index] = normalise(rule)
    return save(rules)


def delete(index):
    rules = [{f: r[f] for f in FIELDS} for r in load()["rules"]]
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(rules):
        raise ValueError("no managed rule with that index")
    del rules[index]
    return save(rules)


def replace(rules):
    """For the settings import: every rule checked before any is written."""
    if not isinstance(rules, list):
        raise ValueError("exclusions.rules must be a list")
    return save([normalise(r) for r in rules])
