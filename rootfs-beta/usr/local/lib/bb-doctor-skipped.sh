# Skipped-file diagnosis for bb-doctor. Sourced by a beta-only patch, so the
# stable bb-doctor is untouched; fold this into it at the next stable release.
#
# The client keeps a list of files it has given up on, with a reason each. They
# are neither queued nor retried, so a file on that list is simply not backed up
# and nothing in the desktop GUI says so. Under this container the usual cause is
# a file the container user cannot read, which points at ownership on the mounted
# source rather than at Backblaze.
#
# Nothing here is repaired, even with --fix. These are the user's own files on a
# mounted share, often thousands of them, and other software on the host may
# depend on their ownership. Changing that from inside a container is exactly the
# "wrong repair costs more than a missed one" case this tool is built around. So
# it works out precisely what is wrong and prints the command to run, and the
# person whose files they are decides.

SKIPLIST="${BZ}/bzreports/bzlist_skipped_files.txt"

# The nearest ancestor of a path that actually exists. When a directory cannot be
# entered, everything below it reports as absent, so "does it exist" cannot be
# asked directly: the answer has to come from the deepest part that can be seen.
nearest_existing() {
    _p="$1"
    while [ -n "$_p" ] && [ "$_p" != "/" ] && [ "$_p" != "." ]; do
        [ -e "$_p" ] && { printf '%s\n' "$_p"; return 0; }
        _p="$(dirname "$_p")"
    done
    printf '%s\n' "$_p"
}

# A Windows path from the list to the path this container sees.
win_to_container() {
    _w="$1"
    case "$_w" in
        ?:\\*) printf '%sdosdevices/%s:%s\n' "$PREFIX" \
                 "$(printf '%s' "$_w" | cut -c1 | tr 'A-Z' 'a-z')" \
                 "$(printf '%s' "$_w" | cut -c3- | tr '\\' '/')" ;;
        *)     printf '%s\n' "$_w" ;;
    esac
}

echo "Files the client has given up on"

if [ ! -r "$SKIPLIST" ]; then
    OK "no skipped-file list yet (nothing has been given up on)"
else
    # A record has a tab-separated reason; the file also carries comment lines
    # ("# SkippedFilesReportStarted: ...") and other non-record lines, which a
    # bare non-empty count read as skipped files. Seen live: a clean list with
    # only its header reported "2 file(s) skipped". The same rule bbdata's
    # counter always applied — real fields, non-empty reason — applies here.
    # awk alone does the counting: grep -c prints 0 AND exits nonzero on no
    # matches, so a trailing "|| echo 0" produced the two-line string "0\n0",
    # which is not equal to "0" and sailed past the no-files branch.
    total="$(grep -v '^#' "$SKIPLIST" 2>/dev/null | awk -F'\t' 'NF>=3 && $2!="" {n++} END{print n+0}')"
    if [ "$total" = 0 ]; then
        OK "no files skipped"
    else
        WARN "${total} file(s) skipped and not backed up"
        NOTE "these are not queued and not retried, and the desktop Issues tab does not list them"
        echo
        NOTE "by reason:"
        grep -v '^#' "$SKIPLIST" 2>/dev/null | awk -F'\t' 'NF>=3 && $2!=""' \
            | cut -f2 | sort | uniq -c | sort -rn | while read -r n reason; do
            NOTE "  ${n}  ${reason}"
        done

        # Work out what is actually wrong with each one, up to a sample. Reading
        # a path is the only way to tell an unreadable file from a deleted one.
        echo
        NOTE "checking the first few:"
        unreadable=""; missing=0; readable=0; checked=0; nopath=0
        # The path is found rather than taken from a fixed column. Only the reason
        # column is established (the monitor has counted by it in production); the
        # rest of the layout is not, and assuming it would report every file as
        # missing if it were wrong. A field starting with a drive letter is the
        # path, whichever column it lands in.
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            path="$(printf '%s' "$line" | tr '\t' '\n' \
                    | grep -m1 -E '^[A-Za-z]:\\' || true)"
            if [ -z "$path" ]; then
                nopath=$((nopath+1))
                continue
            fi
            checked=$((checked+1))
            [ "$checked" -gt 20 ] && break
            cpath="$(win_to_container "$path")"
            if [ -r "$cpath" ]; then
                readable=$((readable+1))
            elif [ -e "$cpath" ]; then
                unreadable="${unreadable}$(dirname "$cpath")
"
            else
                # Absent, or sitting under a directory this container cannot
                # enter. Those look identical from here and mean opposite things:
                # one needs nothing done, the other is the whole problem. The
                # deepest visible ancestor tells them apart.
                anc="$(nearest_existing "$(dirname "$cpath")")"
                if [ -d "$anc" ] && { [ ! -x "$anc" ] || [ ! -r "$anc" ]; }; then
                    unreadable="${unreadable}${anc}
"
                else
                    missing=$((missing+1))
                fi
            fi
        done < "$SKIPLIST"

        [ "$nopath"   -gt 0 ] && NOTE "  ${nopath} line(s) carried no path this could recognise"
        [ "$missing"  -gt 0 ] && NOTE "  ${missing} no longer exist, so nothing to fix"
        [ "$readable" -gt 0 ] && NOTE "  ${readable} readable now, and should clear on the next scan"

        if [ -n "$unreadable" ]; then
            # One bad directory usually explains the lot: mine was 770 files in a
            # single folder that had ended up root-owned and 0700.
            dirs="$(printf '%s' "$unreadable" | sort -u)"
            ndirs="$(printf '%s\n' "$dirs" | grep -c .)"
            NOTE "  unreadable by this container, in ${ndirs} director$([ "$ndirs" = 1 ] && echo y || echo ies):"
            me="$(id -un 2>/dev/null || echo app)"
            printf '%s\n' "$dirs" | while read -r d; do
                [ -n "$d" ] || continue
                own="$(stat -c '%U:%G %a' "$d" 2>/dev/null || echo 'unknown')"
                NOTE "    ${d}"
                NOTE "      owner/mode ${own}, and this container runs as ${me} ($(id -u):$(id -g))"
            done
            echo
            NOTE "This is ownership on the mounted source rather than anything Backblaze has done."
            NOTE "Fix it on the host, not in here, since these are your files and other"
            NOTE "software may rely on who owns them. On Unraid something like:"
            NOTE ""
            NOTE "  chown -R $(id -u):$(id -g) '/mnt/user/<share>/<the folder above>'"
            NOTE ""
            NOTE "They stay on this list until the client rescans, so the count will not"
            NOTE "drop the moment you fix it."
        fi
        [ "$checked" -ge 20 ] && NOTE "  (checked the first 20 of ${total})"
    fi
fi
echo
