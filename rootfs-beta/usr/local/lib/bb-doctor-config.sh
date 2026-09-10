# Client-settings checks for bb-doctor, read straight from the client via bzcli.
# Sourced by a beta-only patch after the drive-mapping section; fold into that
# script at the next stable release.
#
# Everything above this file in bb-doctor infers the client's state from files on
# disk: registries, logs, a lock file's mtime. That is guesswork whenever the
# client itself disagrees, and the fault behind "No files are selected" is
# exactly that disagreement: bzdirfilter_add can mark a drive's root entry
# "none" while the desktop client still shows it ticked, because the tick
# reflects a different setting than the one that decides whether anything is
# sent. bzcli's own report command answers the question the client would give,
# not what the files on disk suggest it might give.
#
# Only four subtrees are queried, and /backup/account is never one of them: it
# holds the account email and the login state, and nothing here needs either.
# Getting that wrong once would put a diagnostic tool in the business of leaking
# what it was built to protect.
#
# Each subtree is queried once and cached, because every call starts a Wine
# process and costs real seconds; a check written naively per drive would run
# the settings query once per drive letter instead of once in total.

echo "Backblaze client settings"

_cfg_bzcli="${BBDIR}/bzcli.exe"
if [ ! -f "$_cfg_bzcli" ]; then
    NOTE "bzcli is not in this image"
    echo
    return
fi

# Run bzcli against one report subtree and return its stdout. In a subshell so
# the cd cannot leak into the rest of bb-doctor, and with PATH set explicitly
# rather than trusted from the caller: bzcli.exe will not run at all unless it
# is launched from its own install directory under this exact environment.
_cfg_report() {
    ( cd "$BBDIR" 2>/dev/null \
      && env PATH="/opt/wine/bin:${PATH}" WINEPREFIX="$PREFIX" WINEDEBUG=-all HOME=/config \
             wine bzcli.exe report -v "$1" ) 2>/dev/null
}

# A quoted string value for $2 out of the pretty-printed JSON in $1, or empty if
# the key is absent or its value is not a string.
_cfg_str() {
    printf '%s\n' "$1" | grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 \
        | sed 's/.*"\([^"]*\)"$/\1/'
}

# A bare (unquoted) scalar value for $2, such as the true/false on has_pek.
_cfg_bool() {
    printf '%s\n' "$1" | grep -o "\"$2\"[[:space:]]*:[[:space:]]*[a-z]*" | head -1 \
        | sed 's/.*:[[:space:]]*//'
}

_cfg_license="$(_cfg_report /backup/license)"
_cfg_status="$(_cfg_report /backup/status)"
_cfg_install="$(_cfg_report /backup/installation)"
_cfg_settings="$(_cfg_report /settings)"

# -- Licence --------------------------------------------------------------------
if [ -z "$_cfg_license" ]; then
    WARN "could not query bzcli for /backup/license"
else
    _cfg_lstatus="$(_cfg_str "$_cfg_license" status)"
    _cfg_lrenew="$(_cfg_str "$_cfg_license" renewal_failure)"
    if [ "$_cfg_lstatus" = "billing_active" ]; then
        OK "the licence is active"
    else
        BAD "Backblaze reports the licence as ${_cfg_lstatus:-unknown}"
        NOTE "a lapsed licence stops backups."
    fi
    if [ -n "$_cfg_lrenew" ] && [ "$_cfg_lrenew" != "none" ]; then
        BAD "the client reports a licence renewal failure: ${_cfg_lrenew}"
    fi
fi

# -- Safety freeze ----------------------------------------------------------------
if [ -z "$_cfg_status" ]; then
    WARN "could not query bzcli for /backup/status"
else
    _cfg_freeze="$(_cfg_str "$_cfg_status" safety_freeze)"
    if [ "$_cfg_freeze" = "not_frozen" ]; then
        OK "backup is not safety-frozen"
    else
        BAD "Backblaze has safety-frozen this backup"
        NOTE "see https://www.backblaze.com/computer-backup/docs/resolve-a-safety-freeze"
    fi
fi

# -- Encryption ---------------------------------------------------------------------
if [ -z "$_cfg_install" ]; then
    WARN "could not query bzcli for /backup/installation"
else
    _cfg_pek="$(_cfg_bool "$_cfg_install" has_pek)"
    if [ "$_cfg_pek" = "true" ]; then
        OK "a private encryption key is set"
        NOTE "Backblaze cannot recover a forgotten private key."
    else
        NOTE "no private encryption key is set"
    fi
fi

# -- Drive selection, and the config-loop check that reuses its parse -------------
if [ -z "$_cfg_settings" ]; then
    WARN "could not query bzcli for /settings"
else
    # bzdirfilter_add is a flat array of {dir, whichfiles} objects, one entry per
    # line pair in the pretty-printed output. Paired positionally rather than by
    # locating the array's brackets: "dir" immediately followed by "whichfiles" is
    # specific to this array and does not occur elsewhere in the document.
    #
    # The JSON escapes each backslash as two characters ("d:\\" on disk means the
    # path d:\), so the doubled backslashes are collapsed after extraction, once,
    # rather than in every comparison that follows.
    _cfg_pairs="$(printf '%s\n' "$_cfg_settings" | awk '
        /"dir"[[:space:]]*:/ {
            d = $0
            sub(/.*"dir"[[:space:]]*:[[:space:]]*"/, "", d)
            sub(/",?[[:space:]]*$/, "", d)
            dir = d
            next
        }
        /"whichfiles"[[:space:]]*:/ {
            w = $0
            sub(/.*"whichfiles"[[:space:]]*:[[:space:]]*"/, "", w)
            sub(/",?[[:space:]]*$/, "", w)
            print dir "\t" w
            next
        }
    ' | sed 's/\\\\/\\/g')"

    # Where /config really sits, so the loop check below can ask whether a mapped
    # drive covers it. Resolved once: readlink -f is not free and every drive in
    # the loop is compared against the same target.
    _cfg_config_real="$(readlink -f /config 2>/dev/null)"
    _cfg_config_winpath=""

    _cfg_drives=0
    for _cfg_link in "${PREFIX}dosdevices"/[d-z]:; do
        [ -L "$_cfg_link" ] || continue
        _cfg_drives=$((_cfg_drives+1))
        _cfg_letter="$(basename "$_cfg_link" | cut -c1)"
        _cfg_disp="$(printf '%s' "$_cfg_letter" | tr 'a-z' 'A-Z')"
        _cfg_root="$(readlink -f "$_cfg_link" 2>/dev/null)"

        # tolower() on both sides: bzcli's JSON is not guaranteed to give the
        # drive letter back in the same case the dosdevices symlink uses (the
        # config-exclusion check below already treats the JSON side this way).
        _cfg_which="$(printf '%s\n' "$_cfg_pairs" \
            | awk -F'\t' -v want="${_cfg_letter}:\\" 'tolower($1)==tolower(want){print $2; exit}')"
        case "$_cfg_which" in
            all)  OK "${_cfg_disp}: the client is set to back this drive up" ;;
            none) BAD "${_cfg_disp}: the client is set NOT to back this drive up"
                  NOTE "this is what produces \"No files are selected\". Check the drive in the"
                  NOTE "client's own Settings window." ;;
            *)    WARN "${_cfg_disp}: the client has no selection entry for this drive" ;;
        esac

        # Does this drive's mount hold /config? Found by comparing resolved real
        # paths rather than the container mount points, since a symlinked /config
        # would otherwise read as living nowhere.
        if [ -z "$_cfg_config_winpath" ] && [ -n "$_cfg_config_real" ] && [ -n "$_cfg_root" ]; then
            case "$_cfg_config_real" in
                "$_cfg_root")
                    _cfg_config_winpath="${_cfg_letter}:\\"
                    ;;
                "$_cfg_root"/*)
                    _cfg_rel="${_cfg_config_real#"$_cfg_root"/}"
                    _cfg_config_winpath="${_cfg_letter}:\\$(printf '%s' "$_cfg_rel" | tr '/' '\\')\\"
                    ;;
            esac
        fi
    done
    [ "$_cfg_drives" = 0 ] && WARN "no drives mapped: the drive selection check has nothing to look at"

    # -- The container's own config directory must not be a backup target ---------
    if [ -z "$_cfg_config_winpath" ]; then
        OK "the container's own config directory is not inside any mapped drive"
    else
        _cfg_config_lc="$(printf '%s' "$_cfg_config_winpath" | tr 'A-Z' 'a-z')"
        _cfg_excluded=0
        while IFS="$(printf '\t')" read -r _cfg_d _cfg_w; do
            [ -n "$_cfg_d" ] || continue
            [ "$_cfg_w" = "none" ] || continue
            _cfg_d_lc="$(printf '%s' "$_cfg_d" | tr 'A-Z' 'a-z')"
            case "$_cfg_config_lc" in
                "$_cfg_d_lc"*) _cfg_excluded=1 ;;
            esac
        done <<CFGEOF
$_cfg_pairs
CFGEOF
        if [ "$_cfg_excluded" = 1 ]; then
            OK "the container's own config directory is excluded from backup"
        else
            BAD "the container's own config directory is inside a backed-up drive and is not excluded"
            # bb-doctor's OK/BAD/WARN/NOTE all print via echo, and dash's echo treats a
            # bare backslash as the start of an escape (a stray "\c" here would even
            # swallow the rest of the line). Doubled, each pair round-trips back to the
            # single backslash the Windows path actually needs.
            _cfg_winpath_disp="$(printf '%s' "$_cfg_config_winpath" | sed 's/\\/\\\\/g')"
            NOTE "exclude ${_cfg_winpath_disp} in the client's Settings > Exclusions."
            NOTE "left as it is, the client backs up its own bookkeeping, which changes what it just"
            NOTE "backed up, which the next pass then backs up again: an endless loop."
        fi
    fi
fi

echo
