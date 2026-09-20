# Backup passes for bb-doctor: whether they complete. Beta only, to be folded
# into bb-doctor at the next stable release.
#
# A pass that loses the client's four-hour lock aborts at the transmit step,
# and bzserv starts another a few minutes later that loses it the same way.
# The chunk path keeps uploading large files meanwhile, so bb-health reads OK
# and the transmit log never goes quiet: the one place this shows is the log's
# own error line. Seen with client 10.0.3.1075 under an unpatched Wine, where
# bzserv cannot open the pass it launched, takes it for dead and deletes the
# lock under it. The beta's Wine carries the fix.
#
# Only losses since the last pass that got past that point count. A pass that
# loses the lock aborts before it records its bz_done file; one that records
# it has cleared the step, so earlier losses in the same day's log are history.
echo "Backup passes"
# An inherit in progress explains a pass that has not started: nothing runs
# until the other identity's backup state is downloaded and merged.
_pl_ibs="${BZ}/bzinherit/bz_ibs_progress.xml"
if [ -f "$_pl_ibs" ] && grep -q "inherit_stage" "$_pl_ibs" 2>/dev/null; then
    _pl_stage="$(sed -n 's/.*inherit_stage="\([^"]*\)".*/\1/p' "$_pl_ibs" | head -1)"
    _pl_pm="$(sed -n 's/.*prog_out_of_thousand="\([0-9]*\)".*/\1/p' "$_pl_ibs" | head -1)"
    # The file outlives the inherit; a finished one is not news.
    case "$_pl_stage" in
        tbs_done_success) ;;
        tbs_done*) BAD "an inherit of the backup state finished badly: ${_pl_stage}" ;;
        *) NOTE "an inherit of the backup state is in progress: ${_pl_stage:-unknown}, $(( ${_pl_pm:-0} / 10 ))%. No pass runs until it finishes." ;;
    esac
fi
_pl_dir="${BZ}/bzlogs/bztransmit"
_pl_log="$(ls -t "$_pl_dir"/*.log 2>/dev/null | head -1)"
if [ -z "$_pl_log" ]; then
    NOTE "no transmit log yet"
else
    # Two or more: a single loss can follow a restart that cut a pass short.
    # awk resets the count each time a pass records its bz_done file, so what
    # is left is the losses since the last pass that got past that point.
    _pl_counts="$(tail -4000 "$_pl_log" 2>/dev/null | awk '
        /bz_done file recorded for upload/ { since = 0; passed = 1 }
        /Lost four hour lock/ { since++; total++ }
        END { printf "%d %d %d", since, total, passed }')"
    _pl_lost="${_pl_counts%% *}"
    _pl_total="$(printf '%s' "$_pl_counts" | cut -d' ' -f2)"
    _pl_ver="$(grep -o "my_bztransmit_version=[0-9.]*" "$_pl_log" 2>/dev/null | tail -1 | sed 's/.*=//')"
    _pl_variant="$(sed -n 's/^variant=//p' /etc/bb-build 2>/dev/null)"
    if [ "${_pl_lost:-0}" -ge 2 ]; then
        BAD "the client lost its four-hour lock ${_pl_lost} times since a pass last got past that point; no backup pass completes"
        NOTE "bzserv cannot open the pass it launched, takes it for dead and deletes the lock under it. Client ${_pl_ver:-unknown}; seen with 10.0.3.1075 under an unpatched Wine."
        case "$_pl_variant" in
            beta*) NOTE "this image's Wine carries the fix for that, so this is something else: run bb-report and open an issue with the bundle." ;;
            *) NOTE "set BACKBLAZE_VERSION=10.0.1.1069 on the container and restart: the pinned version is installed over the newer one. The beta image runs 10.0.3.1075 with a patched Wine." ;;
        esac
    elif [ "${_pl_total:-0}" -ge 2 ]; then
        OK "passes complete again: the lock was lost ${_pl_total} times earlier in the current log, and a pass has got past that point since"
    else
        OK "no pass has lost the four-hour lock in the current log"
    fi
fi
echo
