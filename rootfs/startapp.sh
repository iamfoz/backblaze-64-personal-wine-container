#!/bin/bash
set -x

# Define globals
local_version_file="${WINEPREFIX}dosdevices/c:/ProgramData/Backblaze/bzdata/bzreports/bzserv_version.txt"
install_exe_path="${WINEPREFIX}dosdevices/c:/"
log_file="${STARTUP_LOGFILE:-${WINEPREFIX}dosdevices/c:/backblaze-wine-startapp.log}"

export WINEARCH="win64"
export WINEDLLOVERRIDES="mscoree=" # Disable Mono installation

log_message() {
    # The log file lives inside the Wine prefix, which does not exist on the very
    # first run. Skip logging until its directory exists - a trailing 2>/dev/null
    # does NOT suppress the error, because bash reports the failed >> redirection
    # before it applies the stderr redirect.
    [ -d "$(dirname "$log_file")" ] || return 0
    echo "$(date): $1" >> "$log_file" 2>/dev/null
}

# --- Wine prefix preparation -------------------------------------------------
# Backblaze 10.x is a native 64-bit application that refuses to install on
# anything older than Windows 10. The prefix therefore MUST be win64 and MUST
# report Windows 10 - getting the OS version right is the fix for the installer's
# "unsupported operating system / Windows XP" error.

# A leftover 32-bit prefix (from the pre-10.x era) can never run the 64-bit
# client, so remove it and let it be rebuilt as win64.
if [ -f "${WINEPREFIX}system.reg" ] && grep -q '#arch=win32' "${WINEPREFIX}system.reg" 2>/dev/null; then
    echo "WINE: legacy 32-bit prefix detected - removing so it is rebuilt as win64"
    log_message "WINE: removed legacy 32-bit prefix"
    rm -rf "${WINEPREFIX}"
fi

# Initialise the prefix on first run.
if [ ! -f "${WINEPREFIX}system.reg" ]; then
    echo "WINE: no prefix found - initialising a fresh win64 prefix"
    log_message "WINE: initialising fresh win64 prefix"
    wineboot -i
    wineserver -w
fi

# Force the reported Windows version to Windows 10 on EVERY start. We set both
# Wine's own version key (exactly what winecfg writes) and the raw NT
# CurrentVersion keys, so the check passes no matter how Backblaze probes the OS.
force_windows_10() {
    wine reg add 'HKCU\Software\Wine' /v Version /t REG_SZ /d win10 /f
    nt_key='HKLM\Software\Microsoft\Windows NT\CurrentVersion'
    wine reg add "$nt_key" /v CurrentVersion            /t REG_SZ    /d '10.0'  /f
    wine reg add "$nt_key" /v CurrentBuild              /t REG_SZ    /d '19045' /f
    wine reg add "$nt_key" /v CurrentBuildNumber        /t REG_SZ    /d '19045' /f
    wine reg add "$nt_key" /v ProductName               /t REG_SZ    /d 'Microsoft Windows 10' /f
    wine reg add "$nt_key" /v CurrentMajorVersionNumber /t REG_DWORD /d 10 /f
    wine reg add "$nt_key" /v CurrentMinorVersionNumber /t REG_DWORD /d 0  /f
    # Do NOT "wineserver -w" here. reg writes are synchronous, and once Backblaze
    # is installed the first wine call auto-starts its persistent bzserv service -
    # waiting for the server to terminate would then block forever and the GUI
    # (bzbui.exe) would never launch.
}

echo "WINE: binary=$(command -v wine) version=$(wine --version 2>/dev/null)"
echo "WINE: WINEARCH=${WINEARCH} WINEPREFIX=${WINEPREFIX}"
echo "WINE: forcing reported Windows version to Windows 10"
force_windows_10
echo "WINE: OS version now reported by the prefix:"
wine cmd /c ver 2>/dev/null | tr -d '\r'
log_message "WINE: prefix ready and reporting Windows 10"

#Configure Extra Mounts
for x in {d..z}
do
    if test -d "/drive_${x}" && ! test -d "${WINEPREFIX}dosdevices/${x}:"; then
        log_message "DRIVE: drive_${x} found but not mounted, mounting..."
        ln -s "/drive_${x}/" "${WINEPREFIX}dosdevices/${x}:"
    fi
done

# Set the Wine "virtual desktop" by writing the registry directly. We must NOT
# use "winetricks vd=..." here: winetricks runs "wineserver -w" internally, which
# hangs forever once Backblaze's persistent bzserv service is running (it never
# lets the wineserver terminate). These are exactly the keys winetricks writes.
cd "$WINEPREFIX"
explorer_key='HKCU\Software\Wine\Explorer'
desktops_key='HKCU\Software\Wine\Explorer\Desktops'
if [ "$DISABLE_VIRTUAL_DESKTOP" = "true" ]; then
    log_message "WINE: DISABLE_VIRTUAL_DESKTOP=true - disabling Virtual Desktop mode"
    wine reg delete "$explorer_key" /v Desktop /f 2>/dev/null
    wine reg delete "$desktops_key" /v Default /f 2>/dev/null
else
    desktop_size="${DISPLAY_WIDTH:-900}x${DISPLAY_HEIGHT:-700}"
    log_message "WINE: Enabling Virtual Desktop mode at $desktop_size"
    wine reg add "$explorer_key" /v Desktop /t REG_SZ /d Default /f
    wine reg add "$desktops_key" /v Default /t REG_SZ /d "$desktop_size" /f
fi

# Disclaimer
    # Check if auto-updates are disabled
if [ "$DISABLE_AUTOUPDATE" = "true" ]; then
    echo "Auto-updates are disabled. Backblaze won't be updated."
else
    # Check the status of FORCE_LATEST_UPDATE
    if [ "$FORCE_LATEST_UPDATE" = "true" ]; then
        echo "FORCE_LATEST_UPDATE is enabled which may brick your installation."
    else
        echo "FORCE_LATEST_UPDATE is disabled. Keeping the installed version without checking for updates."
    fi
fi

# Function to handle errors
handle_error() {
    echo "Error: $1" >> "$log_file"
    start_app # Start app even if there is a problem with the updater
}

fetch_and_install() {
    cd "$install_exe_path" || handle_error "INSTALLER: can't navigate to $install_exe_path"
    log_message "INSTALLER: downloading the latest Backblaze installer"
    curl -fL "https://www.backblaze.com/win32/install_backblaze.exe" --output "install_backblaze.exe" || handle_error "INSTALLER: failed to download installer"
    # Backblaze 10.x ships an MSI whose WiX OS-version action rejects Wine
    # (GetVersionEx reports Windows 8 to unmanifested processes, and there is no
    # way to manifest the builtin msiexec). The installer is just a CAB wrapper,
    # so we bypass the MSI: extract the payload and drive Backblaze's native
    # installer (bzdoinstall.exe) directly - its only OS gate rejects *server*
    # editions, which a normal win64 workstation prefix passes.
    log_message "INSTALLER: extracting installer payload"
    extract_dir="${install_exe_path}bzextract"
    rm -rf "$extract_dir"
    mkdir -p "$extract_dir"
    7z x -y -o"$extract_dir" "install_backblaze.exe" >/dev/null 2>&1
    find "$extract_dir" -type f -iname '*.cab' -exec 7z x -y -o"$extract_dir" {} \; >/dev/null 2>&1

    src_dir="$(dirname "$(find "$extract_dir" -iname 'bzdoinstall.exe' | head -1)")"
    if [ ! -f "$src_dir/bzdoinstall.exe" ]; then
        handle_error "INSTALLER: bzdoinstall.exe not found after extracting install_backblaze.exe"
        return
    fi

    # Place the program files into the install directory. bzdoinstall.exe does
    # NOT copy them itself - in the normal flow the MSI does this first - so we
    # perform that step, otherwise service registration fails with "not on disk".
    bb_dir="${WINEPREFIX}drive_c/Program Files/Backblaze"
    mkdir -p "$bb_dir"
    cp "$src_dir"/bz*.exe "$src_dir"/*.dll "$src_dir"/*.xml "$src_dir"/*.gif "$src_dir"/*.ico "$src_dir"/*.txt "$bb_dir/" 2>/dev/null

    # Run the native installer. On a first install this shows Backblaze's account
    # sign-in in the GUI (sign in via the web UI). On later runs it detects the
    # existing account and finalises (registers and starts bzserv).
    log_message "INSTALLER: launching bzdoinstall.exe"
    cd "$src_dir" || handle_error "INSTALLER: can't navigate to $src_dir"
    wine bzdoinstall.exe -doinstall "$(winepath -w "$src_dir")"
    install_exit=$?
    log_message "INSTALLER: bzdoinstall.exe exited with code $install_exit"
    if [ ! -f "$bb_dir/bzbui.exe" ]; then
        handle_error "INSTALLER: bzbui.exe missing after install (bzdoinstall exit code: $install_exit)"
    fi

}

# Backblaze's bzbui.exe references its high-DPI skin assets with hyphenated names
# (e.g. "bzbui_skin_bg-4x.gif", "windows-computer-4x_dm.gif"), but the installer
# payload ships them with underscores ("bzbui_skin_bg_4x.gif"). Without the
# hyphen-named copies bzbui cannot build its main control-panel window - it
# renders unstyled and logs "could not CreateDialog for main white window". The
# naming differs across asset families (some flip every "_", some only the one
# before "4x"), so rather than guess we read the exact names the binaries
# reference and alias each from its underscore twin - the rule is reliable: the
# wanted name with every "-" turned back into "_" is the file already on disk.
# Deriving the list from the binaries keeps it complete across client versions,
# so the GUI renders correctly on the first launch with no per-file chasing.
create_skin_aliases() {
    bb_dir="${WINEPREFIX}drive_c/Program Files/Backblaze"
    [ -d "$bb_dir" ] || return 0
    grep -aohE '[A-Za-z0-9_-]+-4x[A-Za-z0-9_]*\.gif' "$bb_dir"/*.exe "$bb_dir"/*.dll 2>/dev/null \
        | sort -u | while read -r want; do
            have="$(printf '%s' "$want" | tr '-' '_')"
            [ -f "$bb_dir/$have" ] && [ ! -e "$bb_dir/$want" ] && cp -- "$bb_dir/$have" "$bb_dir/$want"
        done
}

start_app() {
    create_skin_aliases
    log_message "STARTAPP: Starting Backblaze version $(cat "$local_version_file")"
    wine "${WINEPREFIX}drive_c/Program Files/Backblaze/bzbui.exe" -noquiet &
    sleep infinity
}

if [ -f "${WINEPREFIX}drive_c/Program Files/Backblaze/bzbui.exe" ]; then
    check_url_validity() {
        url="$1"
        if http_code=$(curl -s -o /dev/null -w "%{http_code}" "$url"); then
            if [ "$http_code" -eq 200 ]; then
                content_type=$(curl -s -I "$url" | grep -i content-type | cut -d ':' -f2)
                if echo "$content_type" | grep -q "xml"; then
                    return 0 # Valid XML content found
                fi
            fi
        fi
        return 1 # Invalid or unavailable content
    }

    compare_versions() {
        local_version="$1"
        compare_version="$2"

        if dpkg --compare-versions "$local_version" lt "$compare_version"; then
            return 0 # The compare_version is higher
        else
            return 1 # The local version is higher or equal
        fi
    }



    # Check if auto-updates are disabled
    if [ "$DISABLE_AUTOUPDATE" = "true" ]; then
        log_message "UPDATER: DISABLE_AUTOUPDATE=true, Auto-updates are disabled. Starting Backblaze without updating."
        start_app
    fi

    # Update process for force_latest_update set to true or not set
    if [ "$FORCE_LATEST_UPDATE" = "true" ]; then
        # Main auto update logic
        if [ -f "$local_version_file" ]; then
            log_message "UPDATER: FORCE_LATEST_UPDATE=true, checking for a new version"
            urls="
                https://ca000.backblaze.com/api/clientversion.xml
                https://ca001.backblaze.com/api/clientversion.xml
                https://ca002.backblaze.com/api/clientversion.xml
                https://ca003.backblaze.com/api/clientversion.xml
                https://ca004.backblaze.com/api/clientversion.xml
                https://ca005.backblaze.com/api/clientversion.xml
            "

            for url in $urls; do
                if check_url_validity "$url"; then
                    xml_content=$(curl -s "$url") || handle_error "UPDATER: Failed to fetch XML content"
                    xml_version=$(echo "$xml_content" | grep -o '<update win32_version="[0-9.]*"' | cut -d'"' -f2)
                    local_version=$(cat "$local_version_file") || handle_error "UPDATER: Failed to read local version from $local_version_file"
                    log_message "UPDATER: Installed Version=$local_version"
                    log_message "UPDATER: Latest Version=$xml_version"
                    if [ -n "$local_version" ] && [ -n "$xml_version" ] && compare_versions "$local_version" "$xml_version"; then
                        log_message "UPDATER: Newer version $xml_version found (installed $local_version) - downloading and installing"
                        fetch_and_install
                        start_app # Exit after successful download+installation and start app
                    else
                        log_message "UPDATER: Installed version ($local_version) is up to date - not reinstalling"
                        start_app # Exit autoupdate and start app
                    fi
                fi
            done

            handle_error "No valid XML content found or all URLs are unavailable."
        else
            handle_error "Local version file not found. Exiting."
        fi
    else
        # FORCE_LATEST_UPDATE=false: keep the installed client, skip the update check.
        log_message "UPDATER: FORCE_LATEST_UPDATE=false, keeping the installed version without checking for updates"
        start_app
    fi
else # Client currently not installed
    fetch_and_install &&
    start_app
fi
