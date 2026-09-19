# Backup passes for bb-doctor: whether they complete. Beta only, to be folded
# into bb-doctor at the next stable release.
#
# A pass that loses the client's four-hour lock aborts at the transmit step,
# and bzserv starts another a few minutes later that loses it the same way.
# The chunk path keeps uploading large files meanwhile, so bb-health reads OK
# and the transmit log never goes quiet: the one place this shows is the log's
# own error line. Seen with client 10.0.3.1075 under Wine, where the lock file
# is on disk and the client's own check says it is not.
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
    _pl_lost="$(tail -4000 "$_pl_log" 2>/dev/null | grep -c "Lost four hour lock")"
    _pl_ver="$(grep -o "my_bztransmit_version=[0-9.]*" "$_pl_log" 2>/dev/null | tail -1 | sed 's/.*=//')"
    if [ "${_pl_lost:-0}" -ge 2 ]; then
        BAD "the client lost its four-hour lock ${_pl_lost} times in the current log; no backup pass completes"
        NOTE "the lock file is on disk and the client's own check says it is not. Client ${_pl_ver:-unknown}; seen with 10.0.3.1075 under Wine."
        NOTE "set BACKBLAZE_VERSION=10.0.1.1069 on the container and restart: the pinned version is installed over the newer one."
    else
        OK "no pass has lost the four-hour lock in the current log"
    fi
fi
echo
