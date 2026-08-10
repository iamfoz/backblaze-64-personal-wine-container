# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [beta] - 2026-08-09

Beta channel only (`:beta` tag), carrying everything in 10.2.1 plus the additions below. The
`:beta` tag is mutable, so each published build stamps a number: `bb-version` reports it
and the monitors show it as `beta+<n>`. Quote that number in a bug report.

Everything below is beta only. The stable images are unchanged from 10.2.1 and will stay
that way until this has run without problems for a while, at which point it moves into a
stable release.

### Added
- A second Wine patch, `patches/wine-fdwrite-rearm.patch`, re-arming FD_WRITE after a
  poll observes that a socket cannot accept a send. Once FD_WRITE has been reported,
  Wine masks POLLOUT for that socket and suppresses further notification, so an
  application waiting on the event sleeps until its own timeout even though the socket
  drained milliseconds later. Measured against the existing writability patch alone on a
  live backup: aggregate upload rose from 13.0-13.9 Mbit/s to 40.9, at an unchanged
  per-connection ceiling, so the gain is recovered idle time rather than a faster pipe.
  **This is a workaround, not a fix.** It diverges from Windows, which does not re-signal
  FD_WRITE on a poll-observed not-writable, and it is deliberately not being submitted
  upstream. The upstream work is a larger redesign being discussed with the Wine
  maintainers; this patch will be dropped as soon as that lands. Only `Dockerfile.beta`
  applies it, and CI fails a stable build if a patched `wineserver` ever appears in one.
- `bb-monitor` gains what the web dashboard had first: overall backup progress with its ETA,
  files remaining, round-trip time, uptime, and the assigned upload server.
- A settings dialog in `bb-monitor` too: `s` opens it, `Tab` switches between Preferences
  and About, `Enter` opens the theme chooser, the arrows move through it with a live
  preview. The theme choice is remembered in `/config/bb-monitor.conf`. All thirteen themes
  are offered on any terminal, each with its own low-colour rendering.
- The upload sparkline in `bb-monitor`, over the same forty-sample window the web dashboard
  uses.
- `bb-monitor-web`, the upload dashboard served over HTTP instead of the terminal, so a
  session survives the console window closing. Shows overall backup progress with an ETA
  weighted by completed transfers, files remaining, a live rate sparkline and uptime.
  Contributed by rogman.
- The dashboard is served through the existing web interface at `/monitor/` rather than on
  a second port, so it inherits whatever `WEB_AUTHENTICATION` and `SECURE_CONNECTION` are
  configured for the GUI. The service itself binds loopback only.
- The web interface opens on a tabbed shell offering the Wine desktop and the upload
  monitor, so the WebUI button reaches both rather than only the desktop. Switching tabs
  hides the desktop rather than unloading it, so the VNC session survives a look at the
  monitor. The monitor frame loads on first use, so its polling never starts for someone
  who does not open it. The desktop stays directly reachable at `/desktop/` if the shell
  itself is ever a problem.
- Round-trip time to the storage pod, in both monitors. The figure is read from the kernel
  rather than measured: every established connection already carries a smoothed round-trip
  time, and `NETLINK_SOCK_DIAG` exposes it. There is no traffic when idle, and the number
  describes the upload connections themselves. Sockets are matched on the owning uid of the
  `-threadpush` processes rather than by walking another process's file descriptors, which
  would need `CAP_SYS_PTRACE`. It reads n/a rather than guessing, and says which reason
  applies: nothing uploading, no kernel socket table, or a connection that terminates locally
  instead of at the pod. That last case covers Docker Desktop on Mac and Windows, where
  container traffic passes through a proxy on the host that can end the connection before it
  leaves the machine. `bb-monitor-web --dump-rtt` prints what the kernel reports, for checking
  it inside a container. Original concept contributed by rogman.

  Round-trip time is useful to read with transfer rate because per-connection
  throughput is bounded by send buffer divided by RTT, so the two together give the
  per-thread ceiling to expect.
- A settings dialog in the web dashboard, holding the thirteen colour themes and an About
  tab reporting the running build, licence and credits.
- The state now comes from the client rather than being inferred. `overviewstatus.xml`
  carries Backblaze's own `cur_state` and names the part in flight, which replaces guessing
  from which processes are running.
- Progress for file-list scans, in both monitors: directories indexed out of the total from
  `topdirs.xml.future`, alongside a running count of files and bytes found. The client
  exposes no other real percentage.
- Chunk positions for the large file being split. Each chunk's index, byte offset and SHA-1
  come from `bzcurrentlargefile/onechunk_seq*.dat`, and that SHA-1 also appears in each
  transfer's own record, so a thread can be matched to the exact chunk it is carrying.
  Chunks are shown in their real positions, filling out of order as threads finish, with
  those in flight marked differently. Chunks that completed before the monitor was opened
  stay unmarked, since they cannot be told apart from pending ones.
- Warnings drawn from the client's own records: a safety freeze, a failed file check, or no
  completed backup within the number of days set in the user's own settings. The staleness
  warning applies only once the backup has caught up, because
  `bzstat_lastbackupcompleted.xml` marks a pass finishing rather than the whole set: on the
  machine this was developed against it read four days old while 87% of 85 TB was still
  unsent, and warning about that would be warning about a first upload behaving normally.
- A first upload still working through the set shows how long it has been running and how
  far it has got, rather than looking like something is wrong. The client exposes no
  "initial backup finished" flag, so this is inferred from how much of the set has never
  been sent. They appear as
  a banner in the web dashboard and in the terminal title bar.
- Upload counts for the most recent day, with failures broken out by the client's own
  categories, and the bytes compression has saved.
- Backblaze's own measured throughput, from `bzperf_measured_upload.xml`. It reports
  3578 kbit/s for files over a megabyte on the machine this was developed against, which
  matches the ceiling calculated from the send buffer and round-trip time to within two
  percent.
- A compact view for multi-part uploads, in both monitors. One row per file with the bar
  drawn as a block per part, filling as parts complete. Off by default, toggled beside the
  theme picker.

### Changed
- The dark theme is properly black rather than dark grey, using rogman's values.
- The web dashboard is mobile-friendly.
- `bb-monitor` and `bb-monitor-web` share one data layer, `/usr/local/lib/bb-monitor/bbdata.py`,
  so a feature appears in both or neither.

### Fixed
- Completed multi-part files showed nonsense part counts such as "21/6594". The count was
  derived from the thread instruction's `numBytes_to_send_in_shm`, which describes the part
  a thread is carrying rather than the file's part size, and readings well below the part
  size turned `filesize / part` into tens of thousands. The configured part size is now read
  from the `bz_done` line, which is constant for a file. A total that large also meant the
  record never reached completion, so every later upload of the same file kept accumulating
  into it. Multi-part files also produce one push beyond their part count, which would
  otherwise open a second row for the same file once the count was corrected; that trailing
  push is now absorbed. Reported by gandalf15.
- Sizes above a terabyte were rendered in gigabytes, so a 250 TB backup showed as
  "257524.9 GB" and overflowed the gauge label. Sizes now run to petabytes. Reported by
  gandalf15.
- File names were written into the page without HTML escaping, so a backed-up file whose
  name contained markup could inject it into the dashboard. Found by rogman.
- The dashboard had no viewport meta, so a mobile browser laid it out at around 980px and
  scaled it down to something unreadable. An iframe does not inherit its parent's.
- Opening the monitor shortly after a container start gave a bare 502 that stayed until the
  page was reloaded by hand, because the shell loads the frame once and nothing retried.
  nginx now serves a holding page for the few seconds before the service is listening.
- A long file path made the whole page scroll sideways at any window width. Flex items
  default to `min-width:auto` and so refuse to shrink below their content, which let one
  path in the in-flight list widen everything around it.


## [10.2.1] - 2026-08-07

### Added
- Build numbers: every image stamps `build=` into `/etc/bb-build` from the CI run
  number, so a mutable tag can be pinned down in a bug report. `bb-version` prints it,
  and `bb-monitor` shows it in the status bar as `v10.2.1+<n>`.

### Fixed
- `bb-monitor` showed completion times in UTC while its own clock showed local time, so
  the two disagreed by an hour in the same panel wherever the container's `TZ` is not
  UTC. Backblaze stamps its logs in UTC regardless of `TZ`; the completion times are now
  converted for display while the internal duration arithmetic stays in UTC.

## [10.2.0] - 2026-08-05

### Added
- A Docker `HEALTHCHECK` that reports the state of the backup rather than just whether
  a process is alive, so a stalled backup shows as unhealthy on the Unraid Docker page.
  Run `docker exec <container> bb-health` to query it directly. It reports unhealthy only
  on corroborated evidence, so idle, freshly installed and signed-out containers stay
  healthy.
- Optional auto-recovery, enabled with `ENABLE_WATCHDOG=true`. It clears the stale
  four-hour lock left behind by an out-of-memory kill, and kills deadlocked upload
  threads so they respawn. Actions are logged, with a cooldown so an unfixable fault
  cannot cause a loop.
- `bb-version`, reporting the installed Backblaze client version alongside the one
  Backblaze is currently serving, plus the container and Wine versions. Backblaze
  publishes release notes ahead of serving a build, so a version in the notes is often
  not yet installable; this queries the same API the updater polls and says whether an
  update is pending or which setting is holding it back.
- `bb-doctor`, which checks an installation against the problems this project has run
  into and, with `--fix`, repairs the ones that can be repaired safely (reported Windows
  version, drive links, control panel skin aliases, a stale four-hour lock). Repairs are
  idempotent, never touch backup state, and are skipped when the diagnosis is ambiguous.
- Detection of the stale-lock respawn loop. A four-hour lock whose holder is killed by
  a container restart (or an out-of-memory kill) puts `bztransmit` in a relaunch loop:
  every few seconds a new pass exits with "Failed to grab fourHourLock", and the
  constant respawns keep every log and file mtime fresh, so the previous stall
  heuristics reported a wedged container as healthy. `bb-health` now corroborates by
  process age instead, since a lock older than a small grace window cannot belong to a
  pass younger than it, and reports the loop as `WEDGE`, so the health status goes red and
  the watchdog clears it. `bb-doctor` diagnoses the same signature independently of
  the tunable thresholds, and `bb-doctor --fix` removes the lock only while the full
  signature holds: a `bztransmit` past the grace window, or one whose age cannot be
  read, always keeps its lock. CI covers both the detection and the repair gate.
- `bb-report`, which builds a sanitised diagnostic bundle for a forum post or issue.
  Collection is allowlist-based, so the per-thread XMLs (live auth token, AES key and IV,
  wrapped file encryption key) and the `bz_done` file listings are never included. File
  names become per-component keyed hashes, so a problem can be traced to a directory or
  followed across bundles without any name being recoverable. `--regenerate-hashes`
  rotates the salt to break that link when a user wants to.
- Every `bb-*` tool accepts `--version`, reporting the image version, git revision,
  LTS variant and build date from a stamp written at build time. The tools only ever
  ship together inside an image, so the build is the identifier to quote in a bug
  report, and a single stamp cannot drift out of step with the tools it describes.
- CI tests `bb-report`'s sanitiser on every change, using the real data shapes found
  in this container. A sanitiser bug does not crash anything; it quietly publishes
  private data in a bundle meant for a public issue tracker, so the check is gated
  rather than left to be run by hand.
- A CI smoke test that boots each built image and verifies the Wine prefix builds, the
  drive mapping reaches into the prefix, and the bundled tools run, all before anything is
  published. Images are now pushed only if that passes.
- Host sizing guidance in the README: Backblaze's memory use tracks file count rather
  than data volume, with measured figures and the reason swap matters.
- `bb-monitor`, a terminal upload dashboard built into the image. Run it from the
  container console (Unraid: container icon → Console) or with
  `docker exec -it <container> bb-monitor`. Shows live upload speed, per-thread file
  progress, recently completed files, thread count, chunks per minute, session total,
  and container memory plus host swap gauges. Files Backblaze splits into parts are
  bundled into a single completed row showing parts done out of total, cumulative
  size, and the file's aggregate transfer rate.
- Documentation for the optional Wine upload-speed patch, an opt-in self-built image
  carrying the fix for [WineHQ bug 59893](https://bugs.winehq.org/show_bug.cgi?id=59893)
  while it is under review upstream.
- Guidance to keep the Backblaze thread count manual and modest (4–8); the automatic
  setting can spin up enough threads to deadlock Wine's pipe handling and stall
  transmits.

- A `beta` image (`ghcr.io/iamfoz/backblaze-personal-wine:beta`): Ubuntu 26.04 with
  Wine built from source and the upload-speed fix applied, so the fix can be used
  without building it yourself. It is not the supported path, since it carries a
  Wine change WineHQ has not yet reviewed and tracks the newer LTS. It is built on
  the weekly schedule and publishes only the `beta` tag; the stable tags are
  produced by a separate job and CI checks the beta cannot write them.
- The beta's Wine fix was reworked after extended testing. The original version
  measured send-buffer room by payload bytes, which near the blocking boundary
  could report a socket writable when a send would block; Wine's full socket
  test suite deadlocked on that state. The fix now reports writability from the
  kernel's own send-accept accounting and applies the same condition to Wine's
  blocking-send path, which previously parked sends the kernel would accept.
  Verified against Wine's full `ws2_32:sock` suite (which now passes cleaner
  than stock Wine), against real Windows Server 2022 and 2025, and against a
  live backup at full uplink speed. The patch in `patches/` is byte-identical
  to the series submitted upstream.

### Changed
- A pre-release review of the whole release surface raised 22 issues, of which 16
  were confirmed against the code and fixed:
  - `bb-report` hardening: a stored hash salt is now trusted only if intact
    (a zero-length salt from an interrupted first run would have made every
    published hash fall to a wordlist), the salt is written atomically and its
    0600 mode re-asserted on every load; user-named mount roots are hashed rather
    than allowlisted (a `-v /mnt/user/Photos:/Photos` style mount previously
    published its name via `df`/`ls` output); dotted directory names like
    `Jane.Doe` are no longer mistaken for extensions, and only recognised
    extensions survive on final components.
  - `bb-health` stall detection now fails SAFE: file ages come from `stat`
    arithmetic instead of `find -newermt`, whose any-error-means-empty output
    read as "stalled" and could fabricate a `HANG` (whose recovery kills upload
    threads); thresholds are validated as integers with fallback to defaults.
  - `bb-watchdog`: the SIGKILL escalation now targets only the original stuck
    PIDs (still alive and still push threads) instead of re-scanning by name,
    which could destroy the healthy replacement thread bzserv had just respawned;
    the cooldown starts at detection rather than on success, so a failing
    recovery backs off instead of retrying every interval; the default cooldown
    is 30 minutes and always exceeds the stall threshold, preventing a re-kill
    loop; interval and cooldown values are validated.
  - `bb-doctor`: `--fix` no longer reports skin aliases as fixed unless every
    link was actually created; drive relinking uses `ln -sfn` so a dangling
    symlink can be repaired; connectivity probes all six Backblaze mirrors
    before declaring the API unreachable; thread counting uses live processes
    rather than the accumulated instruction files.
  - `bb-version` no longer claims "restart the container to install it" when
    `FORCE_LATEST_UPDATE` is unset - the updater only runs when it is exactly
    `true`, and the report now matches that.
  - Release images now stamp their real version: the stock Dockerfiles were
    missing the `ARG` for `DOCKER_IMAGE_VERSION`, so Docker silently dropped
    the value CI passes and published images would have identified as "dev".
  - The CI smoke test asserts the stamp file exists and anchors on a field the
    no-stamp fallback text cannot produce, and the stall-detection tests now
    gate the build alongside the sanitiser tests.
  - `FORCE_LATEST_UPDATE` is exposed in the Unraid template, and the health
    tuning variables are documented.
- Base image updated to `v4.12.6` for both variants, which fixes a startup regression
  when the container engine auto-mounts files under `/run`.
- `python3` added to the runtime image so `bb-monitor` can run.
- CI now uses `actions/checkout@v7`.

## [10.1.0] - 2026-06-20

### Added
- Ubuntu 26.04 LTS ("Resolute") image, published as the `ubuntu26` tag (and
  `vX.Y.Z-ubuntu26` on releases). It ships alongside the default Ubuntu 24.04
  image as an early-access variant so problems can be found before it becomes
  the default. The project now tracks the two most recent Ubuntu LTS releases:
  the older is the default (`latest`) for stability, the newer is offered early,
  and the oldest is retired when it reaches end of support.

### Changed
- Updated the jlesage GUI base image to `v4.12.5` on both LTS variants.
- The WineHQ signing key is now stored as an armored `.asc` keyring referenced by
  an inline deb822 source, so the repository verifies under the stricter apt in
  Ubuntu 26.04 (which ignores a keyring saved with the old `.key` extension).
- CI builds both LTS variants in a matrix. The shared `latest` / `main` / version
  tags track the default (oldest supported) LTS; the newer LTS is published under
  its own `ubuntuNN` tag.

## [10.0.0] - 2026-06-05

### Changed
- Re-engineered for Backblaze 10.x, which is 64-bit only and requires Windows 10.
  - 64-bit WineHQ install (`winehq-stable`) via the modern deb822 `.sources`
    repository method, replacing the brittle `apt-key` / `add-apt-repository`
    setup that silently fell back to Ubuntu's old system Wine.
  - The Wine prefix is forced to report Windows 10 on every start (via the
    registry), fixing the installer's "unsupported operating system / Windows XP"
    error.
  - Install/run path moved to the 64-bit `C:\Program Files\Backblaze`.
  - Legacy 32-bit prefixes are detected and rebuilt as `win64` automatically.
  - The v10 MSI wrapper's WiX OS-version check rejects Wine (`GetVersionEx`
    reports Windows 8 to unmanifested processes), so installation now bypasses
    it: the installer's CAB payload is extracted, the program binaries are
    copied into place, and Backblaze's native `bzdoinstall.exe` is run directly
    (its only OS gate rejects server editions, which a workstation prefix passes).
  - Backblaze's in-app self-update runs a .NET MSI custom action
    (`CheckVersions`) inside `rundll32.exe`, which the Windows 8.1+ "version
    lie" reports as Windows 8 (6.2) to unmanifested processes regardless of the
    registry, aborting the update with "unsupported OS" / `MajorVerTooOld`. The
    container now writes an external `rundll32.exe.manifest` declaring a Windows
    10/11 `supportedOS` into `system32` and `syswow64` and enables
    `PreferExternalManifest`, so `GetVersionEx` reports the real Windows 10 and
    self-updates no longer break on the OS gate (#5).
- Base image moved to Ubuntu 24.04 LTS (`jlesage/baseimage-gui:ubuntu-24.04-v4`),
  with WineHQ packages installed from the `noble` repository, for a longer
  security-support window and an up-to-date userspace.
- CI builds only the `ubuntu24` image; the older `ubuntu22`, `ubuntu20`, and
  `ubuntu18` variants are no longer published.
- Removed the dead "pinned version" update path (its archive.org URL 404s and
  it was already disabled); `FORCE_LATEST_UPDATE=false` now simply keeps the
  installed client and skips the update check.
- Added a Community Applications profile (`ca_profile.xml`) and a `<TemplateURL>`
  for the Unraid CA submission.

## 1.11

### Changed
- It seems that Backblaze has disabled our source of the known-good Backblaze installer on archive.org
  Currently, all new installs will get the latest Backblaze version installed
  Also, the autoupdate functionality is now disabled by default because of this change.

## 1.10

### Changed
- Update known-good Backblaze version to 9.0.1.777
- Ubuntu 22 is now the default versioned image

## 1.9

### Changed
- Try to prevent forced Backblaze client updates

## 1.8.1

### Changed
- Optimize Dockerfiles to reduce layer count

## 1.8 - 2024-03-15

### Changed
- Update Backblaze automatically in the background
- Make startapp log file location configurable by an env var (#129, thanks @brokeh)

## 1.7.2 - 2024-02-24

### Changed
- Update known-good Backblaze version to 9.0.1.767
- Update Backblaze in the background
- Mark ubuntu18 tag as "End of Life" and remove ubuntu18 specific troubleshooting from readme


## 1.7.1 - 2024-02-15

### Changed
- Set lower default values for DISPLAY_WIDTH and DISPLAY_HEIGHT

## 1.7 - 2024-02-07

### Added
- Automatically create symlinks for mounts (#110, thanks @xela1)
- Enable Wine Virtual Desktop mode by default

### Changed
- Updated known-good Backblaze version to 9.0.1.763
> [!NOTE]
> Backblaze will automatically be updated to a known-good version mentioned above, if your installed version is older.
> This download of the new version may take some time, so you will only see a black screen until the download is finished. After that, the installer appears and you can update Backblaze by clicking on "install".
- Fix error `Make sure that your X server is running and that $DISPLAY is set correctly` when running basic CLI commands like `winecfg` by adding the DISPLAY environment variable to the Dockerfiles

## 1.6 - 2024-01-22

### Added
- Added backblaze client auto-update functionality to the docker (#88, thanks @traktuner)

### Changed
- By default a known-good version of the backblaze client will now be used
  - Can be overridden by adding the environment variable "FORCE_LATEST_UPDATE=true"
- The wine version in the Dockerfiles is now pinned to get more control over stability

## 1.5 - 2023-10-13
### Changed
- Dependency updates (see #18 (comment))

## 1.4 - 2023-03-22
### Changed
- Dependency updates

## 1.3 - 2023-01-11
### Changed
- Update README.md

## 1.2 - 2022-03-21
### Changed
- Fixed automated build

## 1.1 - 2022-03-21
### Added
- Ubuntu 18 based version to broaden compatibility

## 1.0 - 2022-03-05
### Added
- First versioned release
- Automatic docker build using Github Actions
- Initial platform support for linux/arm64
- Initial platform support for linux/arm/v7
- Initial platform support for linux/arm/v6

### Changed
- Updated Dependencies

[10.2.0]: https://github.com/iamfoz/backblaze-64-personal-wine-container/compare/v10.1.0...v10.2.0
[10.1.0]: https://github.com/iamfoz/backblaze-64-personal-wine-container/compare/v10.0.0...v10.1.0
[10.0.0]: https://github.com/iamfoz/backblaze-64-personal-wine-container/releases/tag/v10.0.0
