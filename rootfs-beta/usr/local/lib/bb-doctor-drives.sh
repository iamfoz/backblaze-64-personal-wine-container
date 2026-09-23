# Two checks on the mapped drives, for bb-doctor. Sourced by a beta-only patch
# after the drive-mapping section; fold into that script at the next stable release.
#
# Both come from one support thread. A user moved to this container, inherited the
# backup, and got "No files are selected" followed by permission skips and then a
# safety freeze. Two container-shaped faults sat under that: a share the container
# user could not read, which the skipped-file check finds only after the client has
# given up on files, and a drive whose identity the client no longer recognised.
# Neither was named by anything, and both can be, cheaply, before the client has
# to fail first.
#
# Nothing here is repaired. The first is the user's files on their share; the
# second is the identity of their backup. The wrong repair to either costs more
# than the fault.

echo "Source drives"
_drives=0

# Set one attribute of a drive stamp to a value the client itself wrote
# elsewhere, under --fix. The file must be laid out the way the client writes
# it, one <bzvolume vguid=... associated_hguid=... /> line, or nothing is
# touched: a stamp in any other shape is not one this container understands
# well enough to edit. The previous stamp is kept beside it, and the file is
# rewritten in place so its owner and mode on the share stay as they were.
_stamp_repair() {
    _sr_file="$1"; _sr_attr="$2"; _sr_val="$3"
    [ -f "$_sr_file" ] && [ -w "$_sr_file" ] || return 1
    [ "$(grep -c '<bzvolume ' "$_sr_file" 2>/dev/null)" = 1 ] || return 1
    grep -q 'vguid="[^"]*"' "$_sr_file" && grep -q 'associated_hguid="[^"]*"' "$_sr_file" || return 1
    _sr_bak="${_sr_file}.bak-$(date +%Y%m%d%H%M%S)"
    cp -p "$_sr_file" "$_sr_bak" 2>/dev/null || return 1
    _sr_new="$(sed "s/${_sr_attr}=\"[^\"]*\"/${_sr_attr}=\"${_sr_val}\"/" "$_sr_file")" || return 1
    printf '%s' "$_sr_new" | grep -q "${_sr_attr}=\"${_sr_val}\"" || return 1
    printf '%s\n' "$_sr_new" > "$_sr_file" || return 1
    grep -q "${_sr_attr}=\"${_sr_val}\"" "$_sr_file"
}
for _link in "${PREFIX}dosdevices"/[d-z]:; do
    [ -L "$_link" ] || continue
    _root="$(readlink -f "$_link" 2>/dev/null)"
    # Wine's own z: -> / is the container, not a drive the user mapped, and a
    # note that the client has not taken ownership of it reads as a fault.
    [ "$_root" = "/" ] && continue
    _drives=$((_drives+1))
    _letter="$(basename "$_link" | cut -c1 | tr 'a-z' 'A-Z')"
    if [ -z "$_root" ] || [ ! -d "$_root" ]; then
        BAD "${_letter}: is mapped but its target is missing"
        continue
    fi

    # -- Ownership, before the client has to discover it --------------------------
    # As the user this script runs as, which after the root drop is the client's.
    # The root and a sample of what is directly under it: a wrong owner on a
    # share is usually the whole share or a whole top-level folder, so a shallow
    # sample finds it without walking a NAS.
    if [ ! -r "$_root" ] || [ ! -x "$_root" ]; then
        BAD "${_letter}: root ${_root} cannot be read by $(id -un 2>/dev/null || id -u) ($(id -u):$(id -g))"
        NOTE "owner and mode: $(stat -c '%U:%G %a' "$_root" 2>/dev/null || echo unknown). Correct USER_ID/GROUP_ID, or the owner on the host."
        continue
    fi
    _unreadable=""; _seen=0
    for _e in "$_root"/* "$_root"/.[!.]*; do
        [ -e "$_e" ] || continue
        case "$(basename "$_e")" in .bzvol) continue ;; esac
        _seen=$((_seen+1))
        [ "$_seen" -gt 40 ] && break
        if [ -d "$_e" ]; then
            { [ -r "$_e" ] && [ -x "$_e" ]; } || _unreadable="${_unreadable}${_e}
"
        else
            [ -r "$_e" ] || _unreadable="${_unreadable}${_e}
"
        fi
    done
    if [ -n "$_unreadable" ]; then
        _n="$(printf '%s' "$_unreadable" | grep -c .)"
        WARN "${_letter}: ${_n} of the first ${_seen} entries under ${_root} cannot be read by this container"
        printf '%s' "$_unreadable" | head -5 | while read -r _p; do
            [ -n "$_p" ] || continue
            NOTE "  ${_p}  ($(stat -c '%U:%G %a' "$_p" 2>/dev/null || echo unknown))"
        done
        NOTE "the client will skip everything under these. Correct the owner on the host:"
        NOTE "  chown -R $(id -u):$(id -g) '<that folder>'"
    else
        OK "${_letter}: ${_root} readable, ${_seen} top-level entr$([ "$_seen" = 1 ] && echo y || echo ies) sampled"
    fi

    # -- Read speed: how fast the client's parent can stage chunks -----------------
    # Before each 10 MB chunk upload the pass reads the chunk from the source,
    # hashes it and stages it, and only then launches the child that sends it.
    # The read is the slow part on a FUSE share: one user's parent took two
    # seconds per chunk from /mnt/user, so however many threads the client had,
    # one or two were ever busy (2026-09-22). 64 MB from a quarter of the way
    # into one large file, so a file the client just read is less likely to
    # come from cache; the figure is an upper bound on what the pass will see.
    _big="$(find "$_root" -maxdepth 3 -type f -size +67108864c 2>/dev/null | head -1)"
    _fs="$(df -T "$_root" 2>/dev/null | awk 'NR==2{print $2}')"
    if [ -z "$_big" ]; then
        NOTE "${_letter}: read speed not measured: no file over 64 MB within three levels of ${_root}"
    else
        _sz="$(stat -c %s "$_big" 2>/dev/null || echo 0)"
        _skip=$(( _sz / 4 / 1048576 ))
        _t0="$(date +%s%N 2>/dev/null)"
        case "$_t0" in *[!0-9]*|'') _t0="" ;; esac
        if dd if="$_big" of=/dev/null bs=1M skip="$_skip" count=64 2>/dev/null && [ -n "$_t0" ]; then
            _t1="$(date +%s%N)"
            _ms=$(( (_t1 - _t0) / 1000000 ))
            [ "$_ms" -lt 1 ] && _ms=1
            _mbs=$(( 64000 / _ms ))
            # A chunk is 10 MB; the parent's other work per chunk is well under a second.
            _cpm=$(( _mbs * 6 ))
            if [ "$_mbs" -ge 40 ]; then
                OK "${_letter}: reads at about ${_mbs} MB/s (64 MB sample), enough for ${_cpm}+ chunks a minute"
            elif [ "$_mbs" -ge 20 ]; then
                NOTE "${_letter}: reads at about ${_mbs} MB/s (64 MB sample): the pass can stage at most about ${_cpm} chunks a minute from here"
            else
                WARN "${_letter}: reads at about ${_mbs} MB/s (64 MB sample), so the pass can stage at most about ${_cpm} chunks (10 MB) a minute from here, whatever the thread setting"
                case "$_fs" in
                    shfs|fuse*) NOTE "this drive is a ${_fs} mount, Unraid's user-share layer. Map the disk or pool path underneath (/mnt/cache/... or /mnt/diskN/...) instead of /mnt/user/... and the reads skip that layer." ;;
                    *) NOTE "the client reads each 10 MB chunk from here before it can upload it; a faster path to these files is the only lever." ;;
                esac
            fi
        else
            NOTE "${_letter}: read speed not measured (could not read ${_big}, or no high-resolution clock)"
        fi
    fi

    # -- Identity: does the client still know this drive? ---------------------------
    # The client stamps each drive it owns in <root>/.bzvol/bzvol_id.xml:
    # <bzvolume vguid="v00..." associated_hguid="..." />, the volume id being
    # "v00" followed by 25 hex characters (captured 2026-09-22). The hguid ties
    # the drive to the computer identity and is never printed here. Backblaze's
    # own README in that directory says deleting it removes the drive's files
    # from the datacenter, so no note below ever suggests removing it.
    # and lists the drives it knows in bzvolumes.xml, each with the mount point
    # as hex: 443a5c is D:\. When the stamp and the list disagree the client
    # shows the drive as ticked and backs nothing up. Three cases, told apart
    # by the list: the id is known under another letter (the mapping moved), the
    # list has a different id for this letter (another install stamped the
    # drive, or an inherit brought a different record), or the list has nothing
    # for this letter at all (the client never owned it). Not repaired here: the
    # id is the backup's identity, and the wrong change starts the drive over.
    _vol="${_root}/.bzvol"
    _idf="${_vol}/bzvol_id.xml"
    _vols="${BZ}/bzvolumes.xml"
    _hex="$(printf '%s:\\' "$_letter" | od -An -tx1 | tr -d ' \n')"
    if [ ! -d "$_vol" ]; then
        NOTE "${_letter}: no .bzvol yet; the client has not taken ownership of this drive"
    elif [ ! -r "$_vols" ]; then
        NOTE "${_letter}: .bzvol present; bzvolumes.xml not readable, identity not checked"
    else
        _id="$(grep -oE 'vguid="v[0-9a-f]{27}"' "$_idf" 2>/dev/null | grep -oE 'v[0-9a-f]{27}' | head -1)"
        # The list can hold several records for one letter, one per drive that
        # has ever sat there, so a stamp is recognised if it matches any of
        # them, and the value to restore is the record most recently seen
        # attached. Taking the first record read a user's healthy drive as
        # "known as D:, not D:" on 2026-09-22.
        _mine_lines="$(grep "mountPointPathHex=\"${_hex}\"" "$_vols" 2>/dev/null)"
        _ids_here="$(printf '%s\n' "$_mine_lines" | grep -oE 'bzVolumeGuid="v[0-9a-f]{27}"' | grep -oE 'v[0-9a-f]{27}')"
        _known="$(printf '%s\n' "$_mine_lines" | sed -n 's/.*bzVolumeGuid="\(v[0-9a-f]\{27\}\)".*lastTimeVolumeWasSeenAttachedGmtMillis="\([0-9]*\)".*/\2 \1/p' | sort -n | tail -1 | cut -d' ' -f2)"
        [ -n "$_known" ] || _known="$(printf '%s\n' "$_ids_here" | head -1)"
        if [ -z "$_id" ]; then
            if [ -f "$_idf" ]; then
                WARN "${_letter}: .bzvol/bzvol_id.xml carries no volume id, so the client cannot match this drive to its backup"
                if [ -n "$_known" ]; then
                    NOTE "the client's own record for ${_letter}: is ${_known}. With the container stopped, set vguid in"
                    NOTE "${_idf} to that id, and the existing backup of this drive carries on. Not done by --fix: an"
                    NOTE "empty stamp is not laid out the way the client writes it. Never delete .bzvol: Backblaze"
                    NOTE "removes the drive's backed-up files when it goes."
                else
                    NOTE "the client has no record of a drive at ${_letter}: either. Untick and re-tick it in the client's"
                    NOTE "settings; the client stamps it afresh and its upload starts over."
                fi
            else
                NOTE "${_letter}: .bzvol present but no bzvol_id.xml yet; the client has not taken ownership of this drive"
            fi
        elif printf '%s\n' "$_ids_here" | grep -qx -- "$_id"; then
            OK "${_letter}: the client recognises this drive (id ${_id})"
            # The stamp also names the computer it belongs to. An inherit or a
            # reinstall gives this install a new hguid, and a drive stamped for
            # the old one is refused however right its volume id is. Compared,
            # never printed: the hguid is the machine's identity.
            _hg="$(grep -oE 'associated_hguid="[0-9a-f]+"' "$_idf" 2>/dev/null | grep -oE '[0-9a-f]{16,}' | head -1)"
            _inst="${PREFIX}drive_c/Program Files/Backblaze/bzinstall.xml"
            _mine="$(grep -oE ' hguid="[0-9a-f]+"' "$_inst" 2>/dev/null | grep -oE '[0-9a-f]{16,}' | head -1)"
            if [ -n "$_hg" ] && [ -n "$_mine" ] && [ "$_hg" != "$_mine" ]; then
                if [ "$FIX" = 1 ]; then
                    if _stamp_repair "$_idf" associated_hguid "$_mine"; then
                        OK "${_letter}: repaired: the stamp now carries this install's computer identity; the previous stamp is kept beside it"
                        NOTE "restart the container so the client re-reads it."
                    else
                        BAD "${_letter}: could not repair ${_idf}: not laid out the way the client writes it, or not writable; nothing changed"
                    fi
                else
                    WARN "${_letter}: the drive is stamped for a different computer identity than this install's"
                    NOTE "an inherit or a reinstall gave this install a new identity, and the client will not back up a drive"
                    NOTE "stamped for the old one. Re-run with --fix to set associated_hguid in ${_idf} to this install's;"
                    NOTE "the previous stamp is kept beside it. Never delete .bzvol: Backblaze removes the drive's backed-up"
                    NOTE "files when it goes."
                fi
            fi
        elif grep -q "bzVolumeGuid=\"${_id}\"" "$_vols" 2>/dev/null; then
            _ohex="$(grep "bzVolumeGuid=\"${_id}\"" "$_vols" | grep -oE 'mountPointPathHex="[0-9a-f]*"' | grep -oE '[0-9a-f]{2}' | head -1)"
            _other="$(printf "\\$(printf '%03o' $((0x${_ohex:-3f})))")"
            WARN "${_letter}: the client knows this drive as ${_other}:, not ${_letter}: (id ${_id})"
            NOTE "the drive letter changed. Map the folder back to drive_$(printf '%s' "$_other" | tr 'A-Z' 'a-z') so it is ${_other}: again, or the"
            NOTE "client treats it as a new drive and starts its upload over."
        else
            if [ -n "$_known" ] && [ "$FIX" = 1 ]; then
                if _stamp_repair "$_idf" vguid "$_known"; then
                    OK "${_letter}: repaired: vguid set to ${_known}, the client's own record for this drive; the previous stamp is kept beside it"
                    NOTE "restart the container so the client re-reads it."
                else
                    BAD "${_letter}: could not repair ${_idf}: not laid out the way the client writes it, or not writable; nothing changed"
                fi
            elif [ -n "$_known" ]; then
                WARN "${_letter}: the client does not recognise this drive's identity (${_id} is not in bzvolumes.xml)"
                NOTE "the client's own record for ${_letter}: is ${_known}: another install stamped this drive, or an"
                NOTE "inherit brought a different record. Re-run with --fix to set vguid in ${_idf} to that id and"
                NOTE "keep the existing backup of this drive; the previous stamp is kept beside it. Never delete .bzvol:"
                NOTE "Backblaze removes the drive's backed-up files when it goes."
            else
                WARN "${_letter}: the client does not recognise this drive's identity (${_id} is not in bzvolumes.xml)"
                NOTE "the client has no record of a drive at ${_letter}: at all. Untick and re-tick it in the client's"
                NOTE "settings; the client stamps it afresh and its upload starts over. If you inherited, check that"
                NOTE "the right computer was chosen."
            fi
        fi
    fi
done
[ "$_drives" = 0 ] && WARN "no drives mapped: mount your data at /drive_d, /drive_e, ..."
echo
