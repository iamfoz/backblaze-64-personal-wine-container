#!/bin/sh
# A parse gate over both rootfs trees.
#
# Nothing in this repo is compiled, so a syntax error in bb-monitor-web builds
# cleanly, publishes, and only shows up when a person opens the page and the
# service will not start. The same goes for a stray `[[ ]]` in a shell script
# that dash refuses, and for the inline page scripts, which have to stay ES5-ish
# and are never parsed by anything until a browser tries.
#
# So: py_compile every Python file, `sh -n` every POSIX shell file, and
# `node --check` every <script> block lifted out of the web page. All of it
# passes today; this is what keeps it passing.
#
# Run:  sh tests/test-parse.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FAILED=0
CHECKED=0

ok() {
    CHECKED=$((CHECKED + 1))
    printf 'PASS %s\n' "$1"
}

bad() {
    CHECKED=$((CHECKED + 1))
    FAILED=$((FAILED + 1))
    printf 'FAIL %s\n' "$1"
}

# ---- Python -------------------------------------------------------------------
# py_compile rather than a bare compile() so the error text is the one the
# container's own start-up would print. The output goes to the temporary
# directory rather than the default __pycache__: `python3 -m py_compile` writes
# the .pyc whatever PYTHONDONTWRITEBYTECODE says, and these files ship in the
# image, so a stray .pyc beside them is noise in the build context.
check_py() {
    # The error is printed on its own rather than as a traceback through
    # py_compile: what matters here is the file, the line and the message.
    if out="$(python3 -c 'import py_compile, sys
try:
    py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)
except py_compile.PyCompileError as exc:
    sys.exit(str(exc).rstrip())' "$1" "$TMP/check.pyc" 2>&1)"; then
        ok "python  $(printf '%s' "$1" | sed "s#^$ROOT/##")"
    else
        bad "python  $(printf '%s' "$1" | sed "s#^$ROOT/##")"
        printf '%s\n' "$out" | sed 's/^/       /'
    fi
}

for f in "$ROOT"/rootfs-beta/usr/local/lib/bb-monitor/*.py; do
    [ -f "$f" ] && check_py "$f"
done

# The command-line tools have no .py suffix, so they are found by shebang. Both
# trees, because rootfs-beta overlays rootfs and a broken file in either ships.
for f in "$ROOT"/rootfs/usr/local/bin/* "$ROOT"/rootfs-beta/usr/local/bin/*; do
    [ -f "$f" ] || continue
    case "$(sed -n 1p "$f")" in
        '#!'*python*) check_py "$f" ;;
    esac
done

# ---- POSIX shell ---------------------------------------------------------------
# The image's /bin/sh is dash, so this is `sh -n` and not `bash -n`: bash accepts
# arrays and [[ ]] that dash refuses at run time, in a service that is meant to
# be starting the container.
check_sh() {
    if out="$(sh -n "$1" 2>&1)"; then
        ok "sh      $(printf '%s' "$1" | sed "s#^$ROOT/##")"
    else
        bad "sh      $(printf '%s' "$1" | sed "s#^$ROOT/##")"
        printf '%s\n' "$out" | sed 's/^/       /'
    fi
}

# __pycache__ is pruned rather than filtered: a stale .pyc left by an earlier run
# of another suite is a binary file whose first "line" is not worth reading.
# Unquoted on purpose, to split the list; no path in either tree has a space.
for f in $(find "$ROOT/rootfs" "$ROOT/rootfs-beta" \
                -name __pycache__ -prune -o -type f -print | sort); do
    case "$(sed -n 1p "$f")" in
        '#!/bin/sh'|'#!/usr/bin/env sh')
            check_sh "$f"
            continue
            ;;
        '#!/bin/bash'|'#!/usr/bin/env bash')
            # startapp.sh is the one deliberate bash script; check it with bash
            # when there is one, and say so rather than silently skipping.
            if command -v bash >/dev/null 2>&1; then
                if out="$(bash -n "$f" 2>&1)"; then
                    ok "bash    $(printf '%s' "$f" | sed "s#^$ROOT/##")"
                else
                    bad "bash    $(printf '%s' "$f" | sed "s#^$ROOT/##")"
                    printf '%s\n' "$out" | sed 's/^/       /'
                fi
            else
                printf 'SKIP bash    %s (no bash on this machine)\n' \
                    "$(printf '%s' "$f" | sed "s#^$ROOT/##")"
            fi
            continue
            ;;
    esac
    # The bb-doctor drop-ins are sourced fragments with no shebang of their own.
    # Dockerfile.beta runs sh -n over exactly these at build time; this is the
    # same check, before a build is spent finding out.
    case "$f" in
        */usr/local/lib/*.sh) check_sh "$f" ;;
    esac
done

# ---- the inline page scripts ------------------------------------------------------
# The pages are built as Python strings, so a broken script block is a valid
# Python file that serves a page which does nothing. The blocks must also stay
# ES5-ish (no arrow functions, no let/const) for older browsers, which node will
# not tell us, but a parse error it will.
JSDIR="$TMP/js"
mkdir -p "$JSDIR"
BLOCKS="$(python3 - "$ROOT/rootfs-beta/usr/local/bin/bb-monitor-web" "$JSDIR" <<'PY'
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
n = 0
for m in re.finditer(r"<script>(.*?)</script>", src, re.S):
    n += 1
    line = src.count("\n", 0, m.start()) + 1
    with open("%s/block%02d_line%d.js" % (sys.argv[2], n, line), "w", encoding="utf-8") as fh:
        fh.write(m.group(1))
print(n)
PY
)"
if [ "$BLOCKS" = "0" ]; then
    bad "no <script> blocks were extracted from bb-monitor-web, which cannot be right"
elif command -v node >/dev/null 2>&1; then
    for f in "$JSDIR"/*.js; do
        if out="$(node --check "$f" 2>&1)"; then
            ok "node    $(basename "$f")"
        else
            bad "node    $(basename "$f")"
            printf '%s\n' "$out" | sed 's/^/       /'
        fi
    done
else
    printf 'SKIP node    %s script blocks from bb-monitor-web (node is not installed)\n' "$BLOCKS"
fi

printf '\n%d checked, %d failures\n' "$CHECKED" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
