![GitHub License](https://img.shields.io/github/license/iamfoz/backblaze-64-personal-wine-container?style=flat-square)
![Maintenance](https://img.shields.io/maintenance/yes/2026?style=flat-square)
![GitHub last commit](https://img.shields.io/github/last-commit/iamfoz/backblaze-64-personal-wine-container?style=flat-square)
![GitHub contributors](https://img.shields.io/github/contributors/iamfoz/backblaze-64-personal-wine-container?style=flat-square)
[![Stand With Ukraine](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/badges/StandWithUkraine.svg)](https://stand-with-ukraine.pp.ua)

# Backblaze 64 Personal Wine Community Container

This Docker container runs the Backblaze personal backup client via [WINE](https://www.winehq.org), so that you can back up your files with the separation and portability capabilities of Docker on Linux.

It runs the Backblaze client and starts a virtual X server and a VNC server with Web GUI, so that you can interact with it.

⚠️ This project is not affiliated with Backblaze Inc. ⚠️

## Table of Content

   * **[Backblaze 64 Personal Wine Community Container](#backblaze-64-personal-wine-community-container)**
      * [Table of Content](#table-of-content)
      * [Project Status](#project-status)
      * [Known Limitations](#known-limitations)
      * [Docker Images](#docker-images)
         * [Content](#content)
         * [Tags](#tags)
         * [Platforms](#platforms)
      * [Environment Variables](#environment-variables)
      * [Config Directory](#config-directory)
      * [Ports](#ports)
      * [Volumes](#volumes)
      * [Accessing the GUI](#accessing-the-gui)
      * [Sizing Your Host](#sizing-your-host)
      * [Health and Auto-Recovery](#health-and-auto-recovery)
      * [Upload Monitor](#upload-monitor)
      * [Checking Versions](#checking-versions)
      * [Fixing Problems](#fixing-problems)
      * [Reporting a Problem](#reporting-a-problem)
      * [Beta Image](#beta-image)
      * [Optional: Wine Upload-Speed Patch](#optional-wine-upload-speed-patch)
      * [Security](#security)
         * [SSVNC](#ssvnc)
         * [Certificates](#certificates)
         * [VNC Password](#vnc-password)
         * [DH Parameters](#dh-parameters)
      * **[Installation Guide](#installation-guide)**
      * [Troubleshooting](#troubleshooting)
      * [Additional Information](#additional-information)
      * [Credits](#credits)

## Project Status

This docker should just work for most people. But if you for example have a complex permissions setup in the filesystem you are trying to back up you will need good knowledge of docker to get it set up.

Still please be attentive during the install process: The docker by design has read/write access to all the data you are trying to back up and if you make a grave mistake you could delete stuff.

## Known Limitations

Backblaze 10.x (64-bit, Windows 10-only) installs, signs in, and backs up reliably under Wine. Two caveats are worth knowing — neither corrupts or blocks your backups:

- **Upload speed is throttled by a bug in Wine, not by the container or your network.** An `iperf3` test from inside the container reaches full line speed. The cause has since been traced: Wine reports a socket as "not writable" while its send buffer still has room, so the client's sending loop waits out a full one-second timeout instead of sending, capping a single stream at roughly 140 KB/s. This is filed as [WineHQ bug 59893](https://bugs.winehq.org/show_bug.cgi?id=59893) with a fix submitted upstream, and the standard images will pick it up automatically once it ships in a Wine release — see [Optional: Wine Upload-Speed Patch](#optional-wine-upload-speed-patch) if you would rather have it now.

  In the meantime, set the thread count under Settings → Performance to **Manual, 4–8 threads**. More threads is the obvious workaround, and it does raise throughput, but leave it on Automatic and Backblaze can spin up dozens: enough concurrent uploads to deadlock Wine's pipe handling and stall the transfer completely. Throughput is also far lower while grinding through many small files than on large ones, so expect it to climb as the backup progresses.

- **"Permission Issue … `bzdata\bzreports`" warning.** A false positive: Backblaze's permission self-check misbehaves under Wine, but it writes to that directory fine and backups run normally. Safe to ignore.

> The control panel previously rendered unstyled (black background, blank dialog text). That turned out **not** to be an unfixable GDI+ incompatibility: `bzbui.exe` references its hi-DPI skin assets with hyphenated names (`*-4x.gif`) while the bypass install ships them underscored (`*_4x.gif`), so the skin failed to load and the main window couldn't build. The container now creates the hyphen-named aliases at startup, so the panel renders correctly on first launch.

> Backblaze's installer — and, more importantly, its in-app **self-update** — runs a .NET MSI custom action (`CheckVersions`) inside `rundll32.exe`. Under Wine's Windows 8.1+ "version lie", an *unmanifested* process is told it is running Windows 8 (6.2) regardless of what the registry reports, so that check aborts with `MajorVerTooOld` / "unsupported OS" even though the prefix is forced to Windows 10. The first install sidesteps this by bypassing the MSI (it drives `bzdoinstall.exe` directly), but a self-update runs the MSI itself. The container therefore writes an external `rundll32.exe.manifest` declaring a Windows 10/11 `supportedOS` into `system32` and `syswow64` at startup and enables `PreferExternalManifest`, so `GetVersionEx` reports the real version and self-updates do not break on the OS gate.

## Docker Images
### Content
Here are the main components of this image:
  * [S6-overlay], a process supervisor for containers.
  * [x11vnc], a X11 VNC server.
  * [xvfb], a X virtual framebuffer display server.
  * [openbox], a windows manager.
  * [noVNC], a HTML5 VNC client.
  * [NGINX], a high-performance HTTP server.
  * [stunnel], a proxy encrypting arbitrary TCP connections with SSL/TLS.
  * [WINE], a compatibility layer for windows applications on Linux
  * [Winetricks] is a helper script to download and install various redistributable runtime libraries needed to run some programs in Wine
  * [Backblaze Personal Backup]
  * `bb-monitor`, a built-in terminal dashboard for watching uploads live — see [Upload Monitor](#upload-monitor).
  * `bb-health`, which reports the state of the backup as the container's health status, with optional automatic recovery — see [Health and Auto-Recovery](#health-and-auto-recovery).
  * `bb-version`, which reports installed and available client versions — see [Checking Versions](#checking-versions).
  * `bb-doctor`, which checks the installation for known problems and can repair many of them — see [Fixing Problems](#fixing-problems).
  * `bb-report`, which builds a sanitised diagnostic bundle for a bug report — see [Reporting a Problem](#reporting-a-problem).

[S6-overlay]: https://github.com/just-containers/s6-overlay
[x11vnc]: http://www.karlrunge.com/x11vnc/
[xvfb]: http://www.x.org/releases/X11R7.6/doc/man/man1/Xvfb.1.xhtml
[openbox]: http://openbox.org
[noVNC]: https://github.com/novnc/noVNC
[NGINX]: https://www.nginx.com
[stunnel]: https://www.stunnel.org
[WINE]: https://www.winehq.org/
[Winetricks]: https://wiki.winehq.org/Winetricks
[Backblaze Personal Backup]: https://www.backblaze.com/cloud-backup.html

### Tags

| Tag | Description |
|-----|-------------|
| latest | Recommended stable image — the current default LTS (Ubuntu 24.04) |
| ubuntu24 | Ubuntu 24.04 LTS build (same image as `latest`) |
| ubuntu26 | Ubuntu 26.04 LTS build — early-access, for hardening before it becomes the default |
| main | Automatic build of the `main` branch (may be unstable) |
| beta | Ubuntu 26.04 with the Wine upload-speed fix built in.  Not the supported path; see below |
| vX.Y.Z | A specific release (Ubuntu 24.04); `vX.Y.Z-ubuntu26` for the 26.04 variant |

**LTS policy.** The image tracks the **two most recent Ubuntu LTS releases** at a
time. The **older** of the two is the default (`latest`), chosen for stability;
the **newer** ships alongside (currently `ubuntu26`) so problems can be found and
fixed before it ever becomes the default. Interim bugfixes and base/runtime
uplifts are released against both as they land. When an LTS reaches end of
support it is retired — the newer LTS becomes the new default and the next LTS is
added as the early-access variant. So `latest` always points at a mature,
well-supported LTS, while the newer-LTS tag lets you opt in early if you want it.

The older `ubuntu22` / `ubuntu20` / `ubuntu18` variants are no longer published.

### Platforms

| Platform | Support |
|-----|-------------|
| linux/amd64 | Fully supported |
| linux/arm64 | Not supported |
| linux/arm/v7 | Not supported |
| linux/arm/v6 | Not supported |
| linux/riscv64 | Not supported |
| linux/s390x | Not supported |
| linux/ppc64le | Not supported |
| linux/386 | Not supported |

Only `linux/amd64` is realistic. Backblaze Personal Backup ships as an x86-64 Windows
binary, so a non-x86 host would have to emulate the instruction set underneath Wine as
well as translating the Windows API — slow enough to be useless for a backup client that
is already working hard to keep uploads saturated. `linux/386` is out for a different
reason: Backblaze 10.x dropped 32-bit entirely. Neither is a packaging gap that a future
release will close.

## Environment Variables

Environment variables can be set by adding one or more arguments `-e "<VAR>=<VALUE>"` to the `docker run` command.

| Variable       | Description                                  | Default |
|----------------|----------------------------------------------|---------|
|`DISABLE_VIRTUAL_DESKTOP` | Disables Wine's Virtual Desktop Mode | false |
|`ENABLE_WATCHDOG`| When `true`, the container recovers automatically from the two known stall conditions: it deletes a stale four-hour lock left behind by an out-of-memory kill, and kills deadlocked upload threads so they respawn. Every action is logged. Off by default because it deletes a file and kills processes. See [Health and Auto-Recovery](#health-and-auto-recovery). | false |
|`DISABLE_AUTOUPDATE` | When set to true, skip the startup update check and just launch the installed client. When false (the default), the container checks Backblaze for a newer client on each start and updates if one is available. | false |
|`FORCE_LATEST_UPDATE`| When `true` (the default), the updater downloads the newest Backblaze client from Backblaze's servers on each start. When `false`, the installed version is kept and the update check is skipped. | true |
|`UMASK`| Mask that controls how file permissions are set for newly created files. The value of the mask is in octal notation.  By default, this variable is not set and the default umask of `022` is used, meaning that newly created files are readable by everyone, but only writable by the owner. See the following online umask calculator: http://wintelguy.com/umask-calc.pl | (unset) |
|`TZ`| [TimeZone] of the container.  Timezone can also be set by mapping `/etc/localtime` between the host and the container. | `Etc/UTC` |
|`APP_NICENESS`| Priority at which the application should run.  A niceness value of -20 is the highest priority and 19 is the lowest priority.  By default, niceness is not set, meaning that the default niceness of 0 is used.  **NOTE**: A negative niceness (priority increase) requires additional permissions.  In this case, the container should be run with the docker option `--cap-add=SYS_NICE`. | (unset) |
|`USER_ID`| When mounting docker-volumes, permission issues can arise between the docker host and the container. You can pass the User_ID permissions to the container with this variable. | `1000` |
|`GROUP_ID`| When mounting docker-volumes, permission issues can arise between the docker host and the container. You can pass the Group_ID permissions to the container with this variable. | `1000` |
|`CLEAN_TMP_DIR`| When set to `1`, all files in the `/tmp` directory are deleted during the container startup. | `1` |
|`DISPLAY_WIDTH`| Width (in pixels) of the virtual screen's window. (Has to be divisible by 4) | `900` |
|`DISPLAY_HEIGHT`| Height (in pixels) of the virtual screen's window. (Has to be divisible by 4) | `700` |
|`SECURE_CONNECTION`| When set to `1`, an encrypted connection is used to access the application's GUI (either via a web browser or VNC client).  See the [Security](#security) section for more details. | `0` |
|`VNC_PASSWORD`| Password needed to connect to the application's GUI.  See the [VNC Password](#vnc-password) section for more details. | (unset) |
|`X11VNC_EXTRA_OPTS`| Extra options to pass to the x11vnc server running in the Docker container.  **WARNING**: For advanced users. Do not use unless you know what you are doing. | (unset) |
|`ENABLE_CJK_FONT`| When set to `1`, open-source computer font `WenQuanYi Zen Hei` is installed.  This font contains a large range of Chinese/Japanese/Korean characters. | `0` |
|`STARTUP_LOGFILE`| The location for writing logs of the startup script, responsible for installing and starting the Backblaze app.  The default path is also backed up to Backblaze. | `/config/wine/dosdevices/c:/backblaze-wine-startapp.log` |

## Config Directory
Inside the container, wine's configuration and with it Backblaze's configuration is stored in the
`/config/wine/` directory.

This directory is also used to store the VNC password.  See the
[VNC Pasword](#vnc-password) section for more details.

## Ports

Here is the list of ports used by container.  They can be mapped to the host
via the `-p <HOST_PORT>:<CONTAINER_PORT>` parameter.  The port number inside the
container cannot be changed, but you are free to use any port on the host side.

| Port | Mapping to host | Description |
|------|-----------------|-------------|
| 5800 | Mandatory | Port used to access the application's GUI via the web interface. |
| 5900 | Optional | Port used to access the application's GUI via the VNC protocol.  Optional if no VNC client is used. |

## Volumes

A minimum of 2 volumes need to be mounted to the container

  * /config - This is where Wine and Backblaze will be installed
  * Backup drives - these are the locations you wish to backup, any volume that is mounted as /drive_**driveletter** (from d up to z) will be mounted automatically for use in Backblaze with their equivalent letter, for example /drive_d will be mounted as D:. Mount these **read-write** - Backblaze creates a `.bzvol` folder in each drive's root, so a read-only mount will fail (the volume can't be tracked or its backup state inherited).

You can mount drives with different paths, but these will need to be mounted manually within wine using the following method

1. Add your storage path as a wine drive, so Backblaze can access it

    ````shell
    docker exec --user app Backblaze64 ln -s /backup_volume/ /config/wine/dosdevices/d:
    ````

1. Restart the docker to get Backblaze to recognize the new drive

    ````shell
    docker restart Backblaze64
    ````

1. Reload the Web Interface

    ![Bildschirmfoto von 2022-01-16 14-49-45](https://user-images.githubusercontent.com/28999431/149662817-27f3c9e8-12ba-494c-898d-d9492541a5fb.png)

## Accessing the GUI

Assuming that container's ports are mapped to the same host's ports, the
graphical interface of the application can be accessed via:

  * A web browser:
```
http://<HOST IP ADDR>:5800
```

  * Any VNC client:
```
<HOST IP ADDR>:5900
```

## Sizing Your Host

Backblaze's memory use scales with the **number of files** you back up, not their total
size, and the peak comes from `bztransmit` building its index of everything already
backed up. At roughly 2.6 million files that peak has been measured at **4–5 GB**, most of
it a single large allocation. Rough guidance:

| Files backed up | Peak `bztransmit` memory | Comfortable host RAM |
|---|---|---|
| Up to ~500,000 | ~1–2 GB | 4 GB |
| ~1 million | ~2–3 GB | 8 GB |
| ~2.5 million and up | ~4–5 GB | 12 GB, or 8 GB plus swap |

Two things matter more than the raw numbers:

- **Have swap, or headroom.** The failure mode on a tight host is the kernel's
  out-of-memory killer terminating `bztransmit` mid-pass. That leaves a stale lock behind
  and every following pass fails to start, so the backup silently stops making progress —
  see [Health and Auto-Recovery](#health-and-auto-recovery). A modest swap file absorbs
  the peak and avoids this entirely.
- **Watch file count, not bytes.** A few large media files cost almost nothing. Hundreds
  of thousands of small ones — bundled downloads, package caches, generated thumbnails —
  are what pushes memory up. Excluding directories of regenerable junk is the cheapest fix
  available, though note that excluding a path that was previously backed up starts its
  retention clock, so only exclude things you would not want to restore.

## Health and Auto-Recovery

The container reports the state of the **backup** as its Docker health status, not merely
whether a process is alive. On Unraid the container shows as healthy or unhealthy on the
Docker page; `docker inspect` and `docker ps` show it anywhere else. You can also ask
directly at any time:

```
docker exec <container> bb-health
```

It reports one of:

- `OK` — nothing is wrong. An idle container, a fresh install, or one that is signed out
  is healthy: a backup tool with nothing to do right now is not broken.
- `HANG` — an upload thread is alive but the transmit log has not advanced for 20 minutes.
  Backblaze's automatic thread setting can spin up enough upload threads to deadlock
  Wine's pipe handling, which leaves the transfer stuck forever.
- `WEDGE` — a stale four-hour lock is blocking every pass. This is what an out-of-memory
  kill leaves behind: the lock file outlives the process that owned it, and every
  subsequent pass fails to acquire it.

Both states are reported only on corroborated evidence — for `WEDGE`, the lock must be
present *and* stale *and* accompanied by repeated failures in the log *and* no
`bztransmit` process alive to legitimately own it — so a healthy backup is never flagged.

### Automatic recovery

Set `ENABLE_WATCHDOG=true` to have the container fix both conditions itself. It checks
every five minutes and takes the smallest action that clears the fault: deleting the stale
lock for `WEDGE`, or killing the deadlocked upload threads for `HANG` so they respawn.
Every action is logged. After detecting a fault it waits 30 minutes before acting
again - whether or not the recovery succeeded - so a fault it cannot fix produces one
log line per cooldown rather than a retry storm. The cooldown always stays longer
than the stall threshold, so the watchdog can never re-kill the healthy pass it just
restarted before that pass has had time to prove itself in the log.

The thresholds can be tuned if the defaults do not fit your setup - for instance a
very slow uplink where more than 20 minutes between transmit-log writes is normal:

| Variable | Meaning | Default |
|---|---|---|
|`STALL_MIN`| Minutes the transmit log may be silent (with an upload thread alive) before `HANG` is reported | `20` |
|`LOCK_AGE_MIN`| Minimum age in minutes of the four-hour lock before it can be considered stale | `245` |
|`LOCK_FAILS`| Recent "Failed to grab fourHourLock" log lines required to corroborate a `WEDGE` | `5` |
|`WATCHDOG_INTERVAL`| Seconds between watchdog health checks | `300` |
|`COOLDOWN_MIN`| Minutes the watchdog waits after acting before it may act again (raised automatically if set at or below `STALL_MIN`) | `30` |

All five take plain whole numbers; anything else falls back to the default.

It is **opt-in** because it deletes a lock file and kills processes, which should be a
deliberate choice rather than a surprise. Leaving it off costs nothing: the health status
still tells you when something is wrong.

## Upload Monitor

The GUI shows little while a large file uploads — no percentage, no live speed. The
container therefore ships `bb-monitor`, a terminal dashboard that reads Backblaze's own
transmit state directly.

On Unraid, click the container's icon on the Docker page, choose **Console**, and run:

```
bb-monitor
```

From a shell on any Docker host:

```
docker exec -it backblaze-personal-wine bb-monitor
```

It shows live upload speed, the files each thread is sending right now with estimated
progress bars, recently completed files with size and speed, active thread count,
chunks per minute, the session total, and container memory plus host swap gauges.
Files larger than ~100 MB are split into parts by Backblaze: the parts appear
individually while uploading, then bundle into a single row once completed, with the
thread column showing parts done out of total. Scroll with the arrow keys or
PgUp/PgDn when many files are in flight, and quit with `q`.

## Checking Versions

```
docker exec <container> bb-version
```

Reports the container image and Wine versions, the Backblaze client version you have
installed, and the version Backblaze is currently serving — plus whether an update is
pending and, if one is being held back, which setting is holding it.

Backblaze publishes release notes **ahead of** actually serving a build, so a version
number you read about there is often not yet installable. `bb-version` queries the same
API the updater polls, so it answers the question that actually matters: what will happen
on the next container restart. With the default `FORCE_LATEST_UPDATE=true`, a newer client
is picked up automatically once Backblaze serves it — there is nothing to do by hand.

Every `bb-*` tool also accepts `--version`, which reports the image version, git
revision, LTS variant and build date it was built from:

```
docker exec <container> bb-monitor --version
```

The tools always ship together inside an image, so that build stamp — rather than a
separate version per tool — is what identifies exactly what you are running.

This output is also the most useful thing to include when reporting a problem.

## Fixing Problems

```
docker exec <container> bb-doctor
docker exec <container> bb-doctor --fix
```

Checks the installation against the problems this project has actually run into — the
Wine prefix and reported Windows version, the manifest that lets client self-updates
past the OS check, drive mappings and their readability, control panel skin files,
permissions and free space, RAM and swap against your file count, zombie processes,
thread count, stalls, and whether Backblaze is reachable.

With `--fix` it repairs what can be repaired safely: the reported Windows version,
missing drive links, missing skin aliases, and a stale lock left behind by an
out-of-memory kill. Repairs are idempotent, never touch backup state, and are skipped
whenever the diagnosis is ambiguous — a tool that "fixes" a misdiagnosis is worse than
one that just reports. Anything it will not fix on its own (too little RAM, no swap,
a full disk, a wedged transfer) is reported with what to do about it.

## Reporting a Problem

```
docker exec <container> bb-report
```

Builds a sanitised diagnostic bundle as a `.zip` in your config/appdata folder, ready
to attach to a forum post or GitHub issue. Run `bb-report --list` first if you want to
see exactly what it would collect.

**What is never collected.** Backblaze's working files contain material that must not be
posted publicly: the per-thread XMLs carry a live authentication token, the AES key and
IV, and the wrapped file encryption key; the `bz_done` files are a complete listing of
everything on your machine. None of these are collected, ever. The bundle is built from
an explicit list of safe sources rather than by scrubbing whatever is lying around — a
list of what to exclude only has to be wrong once.

**How file names are handled.** Names are replaced with keyed hashes, one per path
component:

```
D:\MediaStore\Photos\Holiday\IMG_0042.CR2   ->   D:\9f2c1a4b7e88\3d5a1c9b2e77\...\a71c….CR2
```

Because each component is hashed separately, files in the same folder share a folder
hash. That is enough to see that everything failing sits in one directory, or that the
same file keeps failing, without revealing what any of them are called. Drive letters
and the conventional mount roots (`/drive_d`, `/mnt`, ...) are kept, and recognised
file extensions survive on file names - they say what kind of file was involved and
identify nobody. A mount root you named yourself is treated as part of the data and
hashed like everything beneath it, and container-internal paths (`/usr`, `/config`,
...) stay readable so the diagnostics remain legible.

The hashes cannot be decoded back into names. They are HMACs under a random secret
salt that is generated once, stored in your config folder readable only by you, and
**never included in a bundle**.

**Linking bundles, and unlinking them.** The same name always produces the same hash, so
if you send two bundles while chasing one problem, they can be compared — the same
folder or file is recognisable across both. That also means the two bundles are
identifiable as coming from the same machine. When you would rather they were not:

```
docker exec <container> bb-report --regenerate-hashes
```

This rotates the salt, so future bundles share nothing with earlier ones. It asks for
confirmation first, because it permanently breaks the connection with anything you have
already sent — including bundles attached to an issue that is still open. Each bundle
notes a short *hash epoch* identifier so it is clear which bundles can be compared with
each other; the identifier reveals nothing about your files.

Have a look through the bundle before you post it. It is your machine, and you should be
comfortable with what is in it.

## Beta Image

```
ghcr.io/iamfoz/backblaze-personal-wine:beta
```

The beta carries the Wine upload-speed fix already built in, so uploads run at full
speed without building anything yourself. Point your container's Repository field at
the tag above to switch, and back to `latest` to switch away. Your `/config` volume
carries over either way.

It differs from the stable images in three ways worth knowing:

- The Wine in it is **built from source with a patch that WineHQ has not yet
  reviewed**. The fix is filed as [WineHQ bug 59893](https://bugs.winehq.org/show_bug.cgi?id=59893)
  and submitted upstream; until it is accepted, this is a change no one else has
  vetted.
- It tracks **Ubuntu 26.04**, the newer LTS, rather than the stable default.
- It is rebuilt on a schedule rather than pinned to a release, so it moves.

Use the stable tags unless upload speed is the reason you are here. When the fix
reaches a Wine release the stable images pick it up on their own and the beta stops
being necessary.

`bb-version` reports `beta-ubuntu26` as its variant, so a bug report always says
which image it came from.

Wine is licensed under the LGPL. This image contains a modified Wine built from the
public source at [gitlab.winehq.org](https://gitlab.winehq.org/wine/wine) with the
patch in [`patches/`](https://github.com/iamfoz/backblaze-64-personal-wine-container/tree/main/patches)
applied; both are available at those locations.

## Optional: Wine Upload-Speed Patch

Single-stream uploads run far slower under Wine than they should. The cause is a bug in
Wine's `select()` writability reporting, not in Backblaze or this container: Wine reports
a socket as "not writable" while its send buffer still has room, so the sending loop waits
out a full timeout instead of sending. This is filed as
[WineHQ bug 59893](https://bugs.winehq.org/show_bug.cgi?id=59893) with a fix submitted
upstream ([merge request 11272](https://gitlab.winehq.org/wine/wine/-/merge_requests/11272)),
and once it ships in a Wine release the standard images will pick it up automatically with
no action from you.

Until then, the fix is available as an **opt-in build you run yourself**. The standard
images do not include it. Building it applies a small patch to Wine's source and compiles
Wine inside the image, which takes a while and needs an `x86-64` machine:

```
git clone https://github.com/iamfoz/backblaze-64-personal-wine-container.git
cd backblaze-64-personal-wine-container
git checkout feat/bundle-patched-wine
docker build -f Dockerfile.ubuntu24 -t backblaze-personal-wine:patched .
```

Then point your container at the `backblaze-personal-wine:patched` image instead of the
published one, keeping your existing `/config` volume so the backup state carries over.
Use `Dockerfile.ubuntu26` instead for the Ubuntu 26.04 variant.

The patch itself is [`patches/wine-sock-upload-fix.py`](https://github.com/iamfoz/backblaze-64-personal-wine-container/blob/feat/bundle-patched-wine/patches/wine-sock-upload-fix.py),
on the same branch as the build above (it is not part of the standard images).
It is anchor-based and deliberately fails loudly if Wine's source has moved, so a mismatch
stops the build rather than silently producing a mispatched Wine. Wine is licensed under
the LGPL; the patched build is produced from Wine's public source with the patch in this
repository applied, both of which are available at the links above.

## Security

By default, access to the application's GUI is done over an unencrypted
connection (HTTP or VNC).

Secure connection can be enabled via the `SECURE_CONNECTION` environment
variable.  See the [Environment Variables](#environment-variables) section for
more details on how to set an environment variable.

When enabled, application's GUI is performed over an HTTPs connection when
accessed with a browser.  All HTTP accesses are automatically redirected to
HTTPs.

When using a VNC client, the VNC connection is performed over SSL.  Note that
few VNC clients support this method.  [SSVNC] is one of them.

### SSVNC

[SSVNC] is a VNC viewer that adds encryption security to VNC connections.

While the Linux version of [SSVNC] works well, the Windows version has some
issues.  At the time of writing, the latest version `1.0.30` is not functional,
as a connection fails with the following error:
```
ReadExact: Socket error while reading
```
However, for your convienence, an unoffical and working version is provided
here:

https://github.com/jlesage/docker-baseimage-gui/raw/master/tools/ssvnc_windows_only-1.0.30-r1.zip

The only difference with the offical package is that the bundled version of
`stunnel` has been upgraded to version `5.49`, which fixes the connection
problems.

### Certificates

Here are the certificate files needed by the container.  By default, when they
are missing, self-signed certificates are generated and used.  All files have
PEM encoded, x509 certificates.

| Container Path                  | Purpose                    | Content |
|---------------------------------|----------------------------|---------|
|`/config/certs/vnc-server.pem`   |VNC connection encryption.  |VNC server's private key and certificate, bundled with any root and intermediate certificates.|
|`/config/certs/web-privkey.pem`  |HTTPs connection encryption.|Web server's private key.|
|`/config/certs/web-fullchain.pem`|HTTPs connection encryption.|Web server's certificate, bundled with any root and intermediate certificates.|

**NOTE**: To prevent any certificate validity warnings/errors from the browser
or VNC client, make sure to supply your own valid certificates.

**NOTE**: Certificate files are monitored and relevant daemons are automatically
restarted when changes are detected.

### VNC Password

To restrict access to your application, a password can be specified.  This can
be done via two methods:
  * By using the `VNC_PASSWORD` environment variable.
  * By creating a `.vncpass_clear` file at the root of the `/config` volume.
    This file should contains the password in clear-text.  During the container
    startup, content of the file is obfuscated and moved to `.vncpass`.

The level of security provided by the VNC password depends on two things:
  * The type of communication channel (encrypted/unencrypted).
  * How secure access to the host is.

When using a VNC password, it is highly desirable to enable the secure
connection to prevent sending the password in clear over an unencrypted channel.

Access to the host by unexpected users with sufficient privileges can be
dangerous as they can retrieve the password with the following methods:
  * By looking at the `VNC_PASSWORD` environment variable value via the
    `docker inspect` command.  By defaut, the `docker` command can be run only
    by the root user.  However, it is possible to configure the system to allow
    the `docker` command to be run by any users part of a specific group.
  * By decrypting the `/config/.vncpass` file.  This requires the user to have
    the appropriate permission to read the file:  it has to be root or be the
    user defined by the `USER_ID` environment variable.  Also, to be able to
    retrieve the correct decryption key, one needs to know that the content of
    the file was generated by `x11vnc`.

### DH Parameters

Diffie-Hellman (DH) parameters define how the [DH key-exchange] is performed.
More details about this algorithm can be found on the [OpenSSL Wiki].

DH Parameters are saved into the PEM encoded file located inside the container
at `/config/certs/dhparam.pem`.  By default, when this file is missing, 2048
bits DH parameters are automatically generated.  Note that this one-time
operation takes some time to perform and increases the startup time of the
container.

[SSVNC]: http://www.karlrunge.com/x11vnc/ssvnc.html
[DH key-exchange]: https://en.wikipedia.org/wiki/Diffie%E2%80%93Hellman_key_exchange
[OpenSSL Wiki]: https://wiki.openssl.org/index.php/Diffie_Hellman

## Installation Guide:
1. Understand, that this docker is a volunteer project, not a commercial product. Some thinkering is to be expected, community based solution finding is encouraged in the issues. If something does not work: look for an open issue about the topic, if there isn't create one. If there is one read through it to see if somebody has found a workaround/fix. If you are a developer I highly encourage you to turn your fix into a Pull Request to allow others to benefit from it.
1. Check for yourself if using this docker complies with the Backblaze [terms of service](https://www.backblaze.com/company/terms.html)
1. Modify the following for your setup (in terms of [ports](#ports), [volumes](#volumes) and [environment variables](#environment-variables)) and run it
   
    **(for Unraid users, instead of running this command navigate to the Apps tab, search for this docker and install it)**
   
    **NOTE**: root priviliges may be needed
    ````shell
    docker run \
        -p 8080:5800 \
        --init \
        --name Backblaze64 \
        -v "[backup folder]/:/drive_d/" \
        -v "[config folder]/:/config/" \
        ghcr.io/iamfoz/backblaze-personal-wine:latest
    ````

1. Open the Web Interface (on the port you specified in the docker run command, in this example 8080):
2. You may see wine being updated, this will take a couple of minutes
   
   ![image](https://github.com/xela1/backblaze-personal-wine-container/assets/357319/4f401b31-8d1d-40fe-85a3-ec4637c23bf5)

1. The UI of the first step of the Backblaze installer is broken on wine, but it doesn't matter, just insert the email to your backblaze account into the input field. (If the UI does not load for you, look in the top left corner for a white pixel. Move your mouse pointer over that pixel, the pixel will go away, and the UI should load.)

    ![Bildschirmfoto von 2022-01-16 14-51-16](https://user-images.githubusercontent.com/28999431/149662881-b8527b31-e837-4982-91db-b0a3df6cc379.png)

1. Press Enter

    ![Bildschirmfoto von 2022-01-16 14-52-27](https://user-images.githubusercontent.com/28999431/149662922-b637e0e5-7932-4e5e-bf14-1e7a6678311c.png)

1. Insert your password (important: keyboard locale mismatches can mess up your inputs)

    - **TIP**: You can use the clipboard function of the web interface, but some passwords will still not get transferred correctly, i would reccommend setting your backblaze password to a long string without special characters

    ![Bildschirmfoto von 2022-01-16 14-57-31](https://user-images.githubusercontent.com/28999431/149663068-80b17726-860a-4614-abc3-e1dba7b1674e.png)

1. Press Enter

    ![Bildschirmfoto von 2022-01-16 15-00-44](https://user-images.githubusercontent.com/28999431/149663220-625a74f7-f59c-40a4-83fc-992d039896b8.png)

1. Wait for Backblaze to analyze your drives

    ![Bildschirmfoto von 2022-01-16 15-00-49](https://user-images.githubusercontent.com/28999431/149663225-dc2f7209-2c57-4c3a-8f87-50750957cd69.png)

1. Click Ok

    ![Bildschirmfoto von 2022-01-16 15-01-00](https://user-images.githubusercontent.com/28999431/149663289-d53c7241-5856-4032-af41-66a3fa513b36.png)

1. If your [config folder] is somewehere inside the [backup folder] on the docker host side (which is the case for the Unraid template) in order to prevent an infinite loop of config file uploads, because those uploads change bz_done* files in [config folder]/wine/drive_c/ProgramData/Backblaze/bzdata/bzbackup/bzdatacenter open the web interface, open the Backblaze settings, open the "Exclusions" tab, click on "Add Folder" and in the popup navigate to My Computer -> (D:) and naviagate to the config folder inside. For unraid template installs this is My Computer -> (D:) -> appdata -> Backblaze64. Click on OK and close the Backblaze Settings.

1. The Installation is done 🎉

1. Buy a license for your Computer in the Backblaze Dashboard, just like for a normal Windows/Mac installation

## Troubleshooting

- The Backblaze Installer says it recognized a server operating system

  ![Bildschirmfoto von 2022-01-16 14-41-04](https://user-images.githubusercontent.com/28999431/149662713-b7b27862-59b6-432a-a3c3-327f939a7292.png)

  - **Explanation**: I don't know what can cause this, it seems to randomly occur on some installations

  - **Solution**: Stop the docker, delete the config directory, restart installation from beginning

  - (**Speculation**: I think this only happens, when no volume is mounted at /config/ and docker manages the folder instead of the volume)

- The backup folder mounted as drive D is not being backed up

  - **Explanation**: Depending on when you added drive D to your wine configuration, the Backblaze installer might not recognize it

  - **Solution**:
    - Open the Backblaze settings
    - In the section "Hard Drives" in the first tab "Settings" enable the checkbox for next to the drive D:\ 

  - **Still not working**:
    - Run
      ````shell
      docker exec --user app Backblaze64 ls -la /config/wine/dosdevices/
      ````

    - The output should look like this:
      ````
        drwxr-xr-x 2 app app 4096 Jan 16 13:43 .
        drwxr-xr-x 4 app app 4096 Jan 16 14:08 ..
        lrwxrwxrwx 1 app app   10 Jan 16 13:43 c: -> ../drive_c
        lrwxrwxrwx 1 app app   10 Jan 16 13:43 d: -> /drive_d/
        lrwxrwxrwx 1 app app    1 Jan 16 13:43 z: -> /
      ````

     - If it doesn't confirm you've mounted the volume in the container correctly for automatic attachment or followed the manual instructions in [volumes](#volumes)
	 
- I can only see a black screen when I start the container

  - **Explanation**: The Docker container may have insufficient permissions to download and install Backblaze.

  - **Solution**:
    - Try a different run command where you explicitly pass the root ID 0 to the container:

    ````shell
    docker run \
        -p 8080:5800 \
        --init \
        -e USER_ID=0 \
        -e GROUP_ID=0 \
        --name Backblaze64 \
        -v "[backup folder]/:/drive_d/" \
        -v "[config folder]/:/config/" \
        ghcr.io/iamfoz/backblaze-personal-wine:latest
    ````

  - **Additional 'black screen' troubleshooting for Synology devices**:
    - It may be necessary to run the container with even higher permissions (--privileged)

    ````shell
    docker run \
        -p 8080:5800 \
        --init \
        --privileged \
        -e USER_ID=0 \
        -e GROUP_ID=0 \
        --name Backblaze64 \
        -v "[backup folder]/:/drive_d/" \
        -v "[config folder]/:/config/" \
        ghcr.io/iamfoz/backblaze-personal-wine:latest
    ````

  - **Still stuck?** Open an issue on [this project's tracker](https://github.com/iamfoz/backblaze-64-personal-wine-container/issues), including the container log and the output of `docker exec <container> bb-health` and
    `docker exec <container> bb-version`.
  
## Additional Information

1. Warning: The Backblaze client is not an init system (who knew) and doesn't clean up its zombie children. This will cause it to fill up your system's PID limit within a few hours which prevents new processes from being created system-wide, would not recommend.  
The `--init` flag installs a tiny process that can actually do a few init things like wait()ing children in place of the backblaze client as PID 1.  
2. Backblaze will create a `.bzvol` directory in the root of every hard drive it's configured to back up in which it'll store a full copy of files >100M split into 10M parts. Mount accordingly if you want to preserve SSD erase cycles.
3. You can browse the files accessible to Backblaze using:
    ````shell
    docker exec --user app Backblaze64 wine explorer
    ````
4. You can open the Wine Config using:
    ````shell
    docker exec --user app Backblaze64 winecfg
    ````
5. We are using Wine's virtual desktop mode as default and are using a default screen resoluzion of 900x700 pixels. It's larger than the Backblaze UI window itself to make room for the Backblaze restore app. You can always modify the resolution as you like with DISPLAY_WIDTH and DISPLAY_HEIGHT:
    ````shell
    docker run ... -e "DISPLAY_WIDTH=1280" -e "DISPLAY_HEIGHT=800" ...
    ````

# Credits

**Backblaze 64** is a 64-bit / Windows 10 fork maintained by [@iamfoz](https://github.com/iamfoz/backblaze-64-personal-wine-container), re-engineered to install and run Backblaze Personal Backup 10.x (which dropped 32-bit and pre-Windows 10 support).

It builds directly on [@JonathanTreffler](https://github.com/JonathanTreffler/backblaze-personal-wine-container)'s Backblaze Personal Wine Community Container — huge thanks to Jonathan, whose project this is forked from. That project was originally developed by [@Atemu](https://github.com/Atemu/backblaze-personal-wine-container) and is built on [@jlesage](https://github.com/jlesage/docker-baseimage-gui)'s excellent GUI base image.

The Backblaze name, logo and application are the property of Backblaze, Inc. This image does not redistribute the Backblaze application; it is downloaded from the official Backblaze servers during installation.

## Contributors:

Maintained by [@iamfoz](https://github.com/iamfoz), standing on the work of everyone who
contributed to the projects it grew out of:

<a href="https://github.com/iamfoz">
  <img src="https://github.com/iamfoz.png?size=64" width="64" height="64" alt="@iamfoz" />
</a>
<a href="https://github.com/JonathanTreffler/backblaze-personal-wine-container/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=JonathanTreffler/backblaze-personal-wine-container" />
</a>
