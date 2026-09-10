# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [beta] - 2026-08-09

This release is for the beta channel only (the `:beta` tag). It has everything in 10.2.1, plus
the additions below. The `:beta` tag is mutable, so each published build has its own number.
`bb-version` reports that number, and the monitors show it as `beta+<n>`. Give that number in a
bug report.

Everything below is in the beta images only. The stable images are unchanged from 10.2.1. They
stay unchanged until this beta runs without problems for some time. The changes then move into
a stable release.

### Added
- A second Wine patch, `patches/wine-fdwrite-rearm.patch`. It re-arms FD_WRITE after a poll
  shows that a socket cannot accept a send. After Wine reports FD_WRITE one time, it masks
  POLLOUT for that socket and gives no further notification. An application that waits on the
  event thus sleeps until its own timeout, although the socket drained milliseconds later.
  Measured against the existing writability patch alone, on a live backup: the aggregate upload
  rate increased from 13.0-13.9 Mbit/s to 40.9. The per-connection ceiling did not change, so
  the increase is recovered idle time and not a faster connection. **This is a workaround, not
  a fix.** It differs from Windows, which does not signal FD_WRITE again when a poll shows that
  a socket is not writable. It is deliberately not submitted upstream. The upstream work is a
  larger redesign, which is in discussion with the Wine maintainers. This patch will be removed
  as soon as that redesign is available. Only `Dockerfile.beta` applies the patch, and CI makes
  a stable build fail if a patched `wineserver` ever occurs in one.
- `bb-monitor` now shows what the web dashboard showed first. It gives the overall backup
  progress with its ETA, the files remaining, the round-trip time, the uptime, and the assigned
  upload server.
- `bb-monitor` also has a settings dialog. `s` opens it. `Tab` moves between the Preferences
  tab and the About tab. `Enter` opens the theme chooser, and the arrow keys move through it
  with a live preview. `bb-monitor` keeps the theme choice in `/config/bb-monitor.conf`. All
  thirteen themes are available on any terminal, each with its own low-colour version.
- An upload sparkline in `bb-monitor`. It uses the same forty-sample window as the web
  dashboard.
- `bb-monitor-web`, the upload dashboard over HTTP instead of the terminal. The session thus
  continues when you close the console window. It shows the overall backup progress with an
  ETA, which is weighted by the completed transfers. It also shows the files remaining, a live
  rate sparkline and the uptime. Contributed by rogman.
- The container serves the dashboard through the existing web interface at `/monitor/`, and not
  on a second port. The dashboard thus uses the `WEB_AUTHENTICATION` and `SECURE_CONNECTION`
  settings you configured for the GUI. The service itself binds to loopback only.
- The web interface opens a tabbed shell with the Wine desktop and the upload monitor. The
  WebUI button thus gives you both, and not only the desktop. When you change tabs, the shell
  hides the desktop but does not unload it, so the VNC session continues while you look at the
  monitor. The shell loads the monitor frame when you first use it, so the monitor never polls
  for a user who does not open it. The desktop stays directly available at `/desktop/`, if the
  shell itself ever causes a problem.
- The round-trip time to the storage pod, in both monitors. The monitor reads the figure from
  the kernel and does not measure it. Every established connection already carries a smoothed
  round-trip time, and `NETLINK_SOCK_DIAG` gives access to it. There is no traffic when the
  container is idle, and the figure describes the upload connections themselves. The monitor
  matches the sockets on the owning uid of the `-threadpush` processes. It does not walk the
  file descriptors of another process, because that would need `CAP_SYS_PTRACE`. The monitor
  reads n/a instead of a guess, and says which reason applies. There are three reasons: nothing
  uploads, there is no kernel socket table, or the connection ends locally instead of at the
  pod. That last case covers Docker Desktop on Mac and on Windows. There, the container traffic
  goes through a proxy on the host. That proxy can end the connection before it leaves the
  machine. `bb-monitor-web --dump-rtt` prints what the kernel reports, so you can check this
  inside a container. Original concept contributed by rogman.

  Read the round-trip time together with the transfer rate. The per-connection throughput is
  limited to the send buffer divided by the RTT, so the two figures together give the
  per-thread ceiling to expect.
- A settings dialog in the web dashboard. It holds the thirteen colour themes and an About tab.
  The About tab reports the running build, the licence and the credits.
- The state now comes from the client, in `overviewstatus.xml`, instead of being inferred. The
  `cur_state` field is coarse: it reads `transmitting` through work that is nothing of the
  sort. The monitors thus take the activity from `current_file`. When there is no file,
  `current_file` holds a phrase and not a name: a scan reports "Producing file lists" instead
  of the part in flight.
- An API tab in the web interface, and a key-authenticated read feed at `/api/v1/status`. The
  feed is for anything outside the browser: a dashboard, an automation system, or a script. It
  carries raw numbers, and not the formatted strings the dashboard draws, so a consumer can
  graph or format the data for itself. Every response has a schema version, because a consumer
  is released independently of this container.

  The feed is the one path that does not use the web login, so it defends itself with a bearer
  key instead. It answers 404, and not 403, until a key exists. Key management stays behind the
  web login. Keys are 256-bit random values. The API shows a key one time and stores only its
  SHA-256. `bb-apikey` does the same job from a terminal.

  The feed carries everything the monitors know. It gives the rate, the backup progress and
  ETA, the scan progress, the container memory and swap, and the round-trip time. It also gives
  the health warnings, the skipped files, the compression saved, the uploads and failures for
  the day, and Backblaze's own measured throughput. Last, it gives the in-flight and recently
  completed files with their chunk positions. A key granted `read` alone gets none of the file
  names, which is all a status display needs. `read:files` adds them.

  The dashboard's own service speaks HTTP/1.1. A consumer that polls every couple of seconds
  thus reuses one connection, and does not open a fresh one each time. Every response carries a
  length, including the responses with no body. Without that length, a kept-alive connection
  stalls and waits for a body that never arrives.

  The API tab listed an empty "read (0)" group. The code read a colon in a permission name as a
  group prefix, so `read:files` invented a group that does not exist. Only `control` is a
  group. The other permissions sit at the top level.

  The state read as the literal string "None" when the client had nothing to report. The client
  writes `cur_state="none"`. The code capitalised that value instead of treating it as no
  answer, so a payload looked as though a null had leaked into it. The API now falls back to
  what the running processes show, as it did before the client's own word was preferred.

  The key store takes its ownership from `/config`. You normally run `bb-apikey` through
  `docker exec`, which is root, while the service runs as the container's own user. A key
  created on the command line thus landed in a root-owned directory that the service could not
  open. The key never appeared in the settings tab, and the API answered 404 as though no key
  existed.

  The container serialises changes to the key store against a lock. To record a key's last use
  is a read-modify-write, and to create a key is one as well. Without the lock, a key created
  while anything was polling could be written straight back out of existence. Forty of
  forty-one were lost in a test of it. The lock covers this container's own threads and
  `bb-apikey` in its separate process.

  Both monitors and the feed now report whether a backup is paused. They read this from
  `bzdata/pauseinfo.xml`, which the client writes only while a pause is set. Without the pause
  file, a pause read as "Uploading" with nothing moving, because the client stays running and
  keeps naming the last file it had. A pause the client set for its own reasons looks the same
  as one asked for over the API.

  A `report` permission generates a diagnostic bundle and hands back a single-use link. The
  link expires in minutes, so the key never appears in a URL, where a browser would keep it in
  the history. The container builds one bundle at a time.

  A key can carry an expiry, or none. No expiry is the default, and it is the right answer for
  something long-running. An expired key stops working and stops keeping the API alive.

  A consumer that runs in a browser needs its origin listed in `API_CORS_ORIGINS`, which is
  unset by default. There is no wildcard. A key is still required either way, but with a
  wildcard, any page the browser loaded could poll the container in the background.

  The API grants permissions per operation and not per group, so a key that starts backups need
  not also be able to pause them. If you tick a group name, you take all of its operations. The
  API expands the group when the key is created, so what is stored is always the explicit list.
  The API exposes only what the key presenting it holds. It does not tell a read-only key which
  control operations exist, and it does not tell a key granted one of them about the other. A
  key can ask what it holds. A client thus offers the buttons it can use, and does not discover
  its own limits from a run of refusals.

  The API starts a backup, or pauses a running one, through `bzcli`, which Backblaze ship with
  the client for this purpose. The pause is cooperative: the client asks its transmit process
  to stop, and no process is killed. A pause thus cannot leave the stale four-hour lock that
  `bb-watchdog` exists to clear. Backblaze document `--backup-now` as the way out of a pause.
  The same `bzcli` command group can also clear the private encryption key, which is
  unrecoverable. The API thus whitelists the two safe verbs by name, and nothing from a request
  reaches their arguments. A `report` permission is defined, but the API refuses it until the
  bundle flow exists.
- Both monitors name the file in hand. For most files this is the only way they appear at all.
  A small file is usually gone before the next poll, and never gets a row of its own. The
  monitors also show what the client says it is doing with that file. The words are
  "Preparing", "Part N of", and "Finishing" for a multi-part file. When the client uploads its
  own bookkeeping (`caNNN/bz_done_*.bzff`), the monitors label it as such, and do not pass it
  off as one of your files.
- Progress for file-list scans, in both monitors. The monitors show the directories indexed out
  of the total, which they read from `topdirs.xml.future`. They also show a running count of
  the files and bytes found. The client exposes no other real percentage.
- Chunk positions for the large file being split. The index, byte offset and SHA-1 of each
  chunk come from `bzcurrentlargefile/onechunk_seq*.dat`. That SHA-1 also appears in each
  transfer's own record, so you can match a thread to the exact chunk it is carrying. The
  monitors show the chunks in their real positions. The chunks fill out of order as the threads
  finish, and the monitors mark the ones in flight differently. Chunks that completed before
  you opened the monitor stay unmarked, because the monitor cannot tell them apart from pending
  ones.
- Warnings drawn from the client's own records: a safety freeze, a failed file check, or no
  recent completed backup. The number of days comes from your own settings. The staleness
  warning applies only once the backup has caught up, because `bzstat_lastbackupcompleted.xml`
  marks a pass finishing rather than the whole set. On the machine this was developed against,
  it read four days old while 87% of 85 TB was still unsent. A warning about that would be a
  warning about a first upload behaving normally.
- A pause button in the top bar of the web dashboard. While a pause is set, the same button
  starts the backup again. It uses the browser session you are already logged in with, so no
  API key is involved. It goes through the same fixed whitelist of client actions as everything
  else. The page never guesses: the button reflects what the next poll reports, and not what
  the click hoped for.
- The status panel says what a backup is made of: "2.4M files: 1.0M photos, 401k docs, 75k
  music, 53k video". The client has counted by category all along, but nothing showed the
  counts. The tooltip gives the exact counts. The categories always account for the whole, and
  a remainder category holds whatever the client counts that these categories do not.
- "Backing up since 1 Jun 2026", in the About tab of both monitors and in the feed.
- The ETA says whether it moved: "(17 Feb 2027, 9d better)", against yesterday's estimate. The
  monitors keep one sample per day. An estimate compared with itself an hour ago only measures
  the jitter of the moving average it came from. Within two percent, or a day, the ETA stays
  quiet.
- The API takes `?fields=rate_bytes_per_sec,paused` to trim the status payload to what a
  consumer wants. It ignores unknown names instead of refusing them, so something built against
  a newer container keeps working on an older one.
- The backup ETA is also given as a date. "171 days" is a number, but "17 Feb 2027" is a day
  you can picture. The resolution is one day on purpose, because an estimate from a moving
  average cannot know the hour. This is in both monitors and in the feed.
- A seven-day upload chart, in both monitors and in the feed. The client has kept one row per
  day all along, but the monitors read only the newest row. The chart answers the question
  anyone running a long backup actually has: did the backup do anything while they were not
  looking?
- On the day a first backup catches up, both monitors say so, plainly, for a week. They give
  how much was uploaded and how many days it took. The monitors latch the moment on disk, so it
  fires once, and files added later cannot replay it. After months of watching a progress bar,
  this is better than a banner that quietly disappears.
- The upload counter no longer calls retried attempts "failed". The figure it shows is the
  attempts a storage vault turned away, because the vault was too busy or full. The client
  retries such an attempt against another vault, and the file still goes up. The red "5 failed"
  sent people hunting through logs for five files that never existed. The counter now reads
  "retried", without the alarm colour. The tooltip says what the figure is, and the API carries
  the breakdown per reason. The number that means data is not backed up is the skipped-file
  count, which the monitors report separately and loudly.
- Both monitors and the API now name the program that uses the most memory. The memory figure
  alone does not say which program uses it. A user reported high memory after an update, and
  the cause was the client, which reads a large file list into memory during a scan. The
  figure now reads "Mem 8.1/16 GB (bzfilelist)", which answers the question that the number
  asks.
- The skipped-file check misread every line of the client's list. Backblaze is a Windows
  program and writes the file with CRLF, so the shell `read` command leaves a carriage return
  on each line. A path built from such a line names a file that cannot exist. The check thus
  reported a file sitting right there as "no longer exists, so nothing to fix". That is the
  opposite of the truth, on the one check whose job is finding what is wrong. The same loop
  also examined the report's header and reported it as a line it could not understand. The
  monitor was never affected, because Python strips the line ending and the shell does not.
- A container 21.9% through its first backup announced that the backup was complete, and
  latched that to disk for a week. The completion check reused the same helper as the staleness
  warning. That helper answers "yes" when there are no totals to judge against, so that a
  missing figure cannot raise a false warning. That default is right for a warning but exactly
  wrong for declaring something finished, and during a file-list scan the totals are absent.
  The check now requires positive evidence, and says nothing when it cannot tell.

  The same check took its day count from a figure that is only available while a backup is in
  progress. Every completion would thus have read "in 0 days". It now reads the client's own
  record of when the first file went up.
- A pause now reads as "Pausing" until it has actually taken. The client finishes the transfers
  already in flight before it stops. To say "Paused" the moment the request landed was thus
  ahead of the truth, because uploads were still completing. Backblaze's own window appeared
  not to notice a pause, but it was right all along and ours was early. The feed carries a
  `draining` flag for the same distinction.
- While a pause was set, the monitors said "Preparing <file>" beside a state of Paused. That
  reads as stalled, and says the opposite of what is happening. The client parks on the file it
  was about to take. While a pause is set, this is thus the next file and not the current one.
  Both monitors now say so.
- A Skipped Files tab in the web interface. It lists the files Backblaze has given up on, with
  the reason for each one. It has a filter, and a breakdown you can click to narrow the list by
  reason. The reasons read as words, and the tooltip keeps the client's own constant. Where the
  reason is a permissions problem, the page says what that means under this container. It
  points at `bb-doctor`, which diagnoses the problem and prints the command. The tab sits
  behind the web login, like the rest of the dashboard, because a list of paths is worth
  protecting. On the API, the same list is withheld from any key without `read:files`.
- Buttons and chips drawn on the border colour had near-black text hardcoded on them. In the
  dark theme this text was 1.5:1 against a dark grey, and thus effectively invisible. They now
  use the accent and background of the palette, and every theme guarantees contrast between
  those two.
- The ETA no longer reports absurd figures while a backup gets going. A backup starts on its
  small files. A handful of those gives a per-byte rate low enough to extrapolate into
  thousands of years. One run read "4259966d", which is about 11,600 years. The trend feature
  then dutifully compared today's nonsense against yesterday's. The monitors now withhold an
  estimate beyond a century instead of showing it. The trend ignores estimates resting on fewer
  than three completed transfers, and does not record them. A bad reading thus cannot spoil the
  next day's comparison either. A dismal estimate still shows: 100 TB on a 1 Mbit/s uplink is
  about 25 years, and that is a true answer.
- With no usable estimate, the ETA now reads "not yet" rather than "stalled". Stalled was true
  when the only cause was a dead rate. It is wrong when the backup is uploading briskly and
  only the projection is unusable.
- `bb-monitor` run without a terminal says so and names the fix, instead of dying in a two-deep
  curses traceback. The usual way to have no terminal is `docker exec` without `-it`, which is
  exactly what the message suggests.
- The skipped-file check counted the list's own header as a skipped file: a clean list holding
  only "# SkippedFilesReportStarted" reported two files skipped. The check now counts only
  lines with a tab-separated reason, the same rule the monitor's counter always applied. This
  was found on the first run against a real machine. That run also confirmed that a directory
  ownership fix clears the list on the next scan.
- `bb-doctor` works out why files were skipped. It groups them by the client's own reason. It
  then reads a sample, to tell the files apart. Some files no longer exist. Some are readable
  again and will clear on the next scan. Some this container still cannot read. For that last
  group it names the directory, its ownership and mode, and the command to run on the host.

  `bb-doctor` repairs nothing, even with `--fix`. These are your own files on a mounted share,
  often thousands of them, and other software on the host may depend on who owns them. To
  change that from inside a container is the case this tool's own rule was written for.
- Files Backblaze has given up on are now reported. `bzlist_skipped_files.txt` records them
  with a reason. The client neither queues nor retries these files, so nothing else tells you
  that they are unprotected. Under this container the usual cause is a file the container user
  cannot read. That points at ownership on the mounted source, and not at Backblaze.
- A first upload still working through the set now shows how long it has been running and how
  far it has got. It thus does not look like something is wrong. The client exposes no "initial
  backup finished" flag, so the monitors infer this from how much of the set has never been
  sent. It appears as a banner in the web dashboard and in the terminal title bar.
- Upload counts for the most recent day. The monitors break the failures out by the client's
  own categories, and show the bytes compression has saved.
- Backblaze's own measured throughput, from `bzperf_measured_upload.xml`. On the machine this
  was developed against, it reports 3578 kbit/s for files over a megabyte. That matches, to
  within two percent, the ceiling calculated from the send buffer and the round-trip time.
- A compact view for multi-part uploads, in both monitors. It gives one row per file, with the
  bar drawn as a block per part. The blocks fill as the parts complete. The view is off by
  default, and you toggle it beside the theme picker.

- A Tools tab in the web interface. It runs `bb-doctor`, `bb-health` and `bb-version`
  from the page and shows their output there. The page does not have its own copy of the
  tools. It runs the same programs that the console runs, and shows what they print, so a
  change to a tool is a change to both views. A paste of the output into a forum post is
  the same text whichever way it was made. The page colours the lines by the prefixes that
  `bb-doctor` prints. A line with an unknown prefix is shown as plain text, so a change of
  wording in a tool cannot hide output. Each tool has a Run button, a Copy button and a
  Download button. `bb-doctor` has a checkbox for `--fix`. The page shows what `--fix` can
  change and asks for confirmation before it runs. One run per tool at a time: a second
  click while a run is in progress joins the run. Every run is written to the container
  log with its command. The page runs as the container user, so the permission checks test
  the right user. The tab sits behind the web login, like the rest of the dashboard.

  `bb-health` shows its answer in its own card, with a tick, a warning sign or a cross,
  and the tool's lines under it. Its output is a verdict, so it has no output box and no
  Download button. The output boxes of `bb-doctor` and `bb-version`
  have a Hide button and a Show button. Every card has a Clear result button, which
  removes the result from the page and from the service, so a reload does not bring it
  back. A run in progress cannot be cleared until it finishes.
- `bb-doctor` no longer warns about low RAM when the host has swap. Swap absorbs the peaks
  that would otherwise end in an out-of-memory kill, and not everyone can add memory. With less than 12 GB and swap present, the line reads OK and names the swap.
  With less than 12 GB and no swap, the warning stands. Beta only, through the same
  build-time patch as the other `bb-doctor` additions, so the console and the Tools tab
  give the same answer.
- The Tools tab builds diagnostic bundles and keeps them. A bundle made from the page is
  stored in `/config/bb-diag` as `backblaze64-diag-YYYYMMDDHHMM.zip`. The page lists the
  bundles with their size and date, and each has a Download button and a Delete button.
  A bundle from the API gets the same name and the same place. A bundle made with
  `bb-report` on the console stays in `/config` with its old name, because `bb-report` is
  a stable file.
- The Status tab. The Skipped Files tab now leads with everything the client says needs
  attention: the health warnings, a pause and whether it has taken yet, a first backup in
  progress or just finished, and then the skipped files under them. A warning that
  `bb-doctor` can diagnose links to the Tools tab. When there is nothing to report, the
  page says so. The red band in the Monitor tab links to the Status tab.

- The pause says why, and until when. The client writes a reason code and a deadline with
  every pause, and the data layer read both, but the pages showed a flag. Now the Monitor's
  state line, the Status tab, the terminal monitor and the API carry the reason as plain text,
  with the client's code in a tooltip: "Paused from here" for a pause set from the Monitor,
  the API or bzcli; "Paused by the client" with the cause when the client chose it, for
  example when Backblaze's cluster authority is not answering, which is usually their
  maintenance and resumes on its own. A pause set from here is ended from the Monitor; one
  the client chose ends when the client decides. The page says which.
- Notifications. The Settings tab has a Notifications section. Add an ntfy topic, a Pushbullet,
  Discord, Slack or Gotify endpoint, a webhook that receives JSON, or a custom JSON body with
  placeholders for anything else, with an optional bearer token or basic auth, and choose the
  events:
  safety freeze, files skipped from a threshold, no completed backup within the client's
  own limit, a stall that `bb-health` reports, a pause the client chose, first backup
  complete, a milestone, and a container build change. Each event is sent once when it
  starts and once when it clears, and the conditions are remembered on disk so a restart
  does not send them again. Delivery makes three attempts and then logs the failure; there
  is no queue. No file names are sent. A Test button
  sends a message to one endpoint. Tokens are stored in `/config/bb-api`, readable by the
  container user only, because they have to be sent and so cannot be hashed.
- Quiet hours. The Settings tab has a schedule of pause windows: days of the week, a start and
  an end, in the container's time zone. At the start of a window the container asks the
  client to pause; at the end it starts the backup again. The client's own pause lasts
  about two hours, so inside a window the container renews it and says so in the log. A
  backup started by hand inside a window stays running until the window ends. The Status
  tab reads "Paused for quiet hours" with the time it resumes.
- Copy status summary. A button in the Monitor's settings and on the Status tab copies
  five lines of plain text: build and uptime, state and rate, progress and ETA, health,
  today's uploads. It is what a maintainer asks for first. No file names. The same text is
  at `GET /api/v1/summary`.
- `bb-doctor` checks the source drives. For each mapped drive it reads the root and a
  sample of the first-level entries as the container user and names anything it cannot
  read, with the owner and the `chown` line. This finds a wrong owner before the client has
  to give up on files. It also compares the volume id the client wrote under `.bzvol` with
  the volumes the client lists in `bzvolumes.xml`. A drive the client no longer recognises
  is the "No files are selected" state after an inherit onto a fresh install, and nothing
  reported it before. No repair for either: these are the user's files and the backup's
  identity. Beta only, through the same build-time patch as the other additions.
- `bb-health` reports FROZEN, exit 1, when Backblaze has safety-frozen the backup, so a
  frozen backup shows unhealthy in the Docker tab instead of healthy. `bb-watchdog` acts
  only on HANG and WEDGE, so this cannot start a recovery. The Tools tab shows it as a red
  cross. Beta only, as an anchored patch on the stable `bb-health`.
- The API has a switch. When a live key exists, the API tab shows a toggle that turns the
  API off and on without revoking anything. Off, every path answers 404 and the keys are
  kept; on, they work again at once. A revoked or expired key has a Delete button in the
  table, which removes the row. An active key cannot be deleted, only revoked.
- More on the API. `GET /api/v1/metrics` gives the numbers in Prometheus text format with
  a `bb64_` prefix. `GET /api/v1/events` is a server-sent event stream with one message
  per change to the state, the pause, the health warnings, the skipped count, completion or
  milestones, and a keep-alive every fifteen seconds. `GET /api/v1/openapi.json` is the
  OpenAPI 3.1 document, also in the repository at `docs/openapi.json`. The command-line
  tools are reachable at `/api/v1/tools` behind two new permissions: `diagnose` runs the
  checks and reads their output; `diagnose:repair` is needed as well to switch on
  `bb-doctor --fix`. The status payload gains `pause_label`, `milestones` and
  `progress_history`.
- The Status tab shows more. Milestones, each once for a week: a quarter, half, three
  quarters of the way, and the first terabyte. The last 24 hours as a list in a box that
- The timeline summary line counted a multi-part file once per part, each time at its running total, so a day of large files reported around ten times the bytes sent (11.5 TB for about 1 TB on a live container). A bundle is now one file at its final size, and one already landing parts when a spell starts counts only what it gains during the spell.
  scrolls, newest first: every change of state, and after each spell of uploading one line
  with what it amounted to, for example "Uploaded 28 files (1.4 GB) in 9m, 40 already backed
  up: average 40.0 Mbit/s, 8.0 threads, mem 1.9 GB, swap 95 MB". A spell that runs for hours
  gets a line every hour with the totals so far. A spell allows gaps of up to a minute, so
  the client's flicker between Transmitting and Preparing on small files does not split it.
  Files that finished before the spell began are not counted in it; a multi-part file counts
  its final size; a small file the datacentre already held counts as already backed up, not
  as uploaded, so the line after a restart says what the restart did. The safety-freeze
  notice links to the Backblaze page on resolving one and says what the uninstall step means
  in this container: delete the client's program
  directory and restart, and never recreate the Wine prefix, which would destroy the backup
  state.
- The status panel is arranged in named rows: Now, Progress, Today and System. Twenty
  readings in one wrapped line had become a wall of text. A row with nothing to show is left
  out rather than drawn as a label with nothing after it.
- The status panel can be trimmed. The Monitor's settings dialog has a Status panel section
  with a checkbox per reading, grouped the same way as the rows, and three presets to start
  from: Minimal, which leaves the state, the rate and the backup total; Normal; and
  Everything. A preset ticks its boxes and leaves them free to change, so anything in
  between takes no extra step. The choice is kept in the browser, like the theme, because a
  phone wants less than a desk.
- The terminal monitor has the same arrangement and the same choice. Its status area is in
  the same four named rows, and its settings dialog has a Status panel tab listing every
  reading with the same three presets, moved through with the arrow keys and toggled with
  Enter. It keeps its own choice in `/config/bb-monitor.conf` beside the theme, because a
  browser setting cannot reach a terminal.
- The Settings tab says where the Monitor's own settings are. Two places to change settings
  is confusing, so there is now a Monitor display section explaining that the theme, the
  compact view and the status panel belong to each browser, with a button that shows the
  Monitor and opens its settings at the status panel. The request travels to the frame as a
  message rather than as a URL, because reloading the frame would drop its poll.
- A percentage keeps its two decimal places as it moves. The payload carries two, but a
  value of 23.40 prints as "23.4" from the number itself, so the figure changed width from
  one poll to the next.
- The Monitor shows more. "Today:" under the seven-day chart, with the client's counts for
  today: uploads, retried attempts, files skipped. A progress-over-time line in the About
  tab, one sample a day of percent complete, kept for a year. The exact bytes per second in
  the rate's tooltip.
- Keys. `1` to `6` switch tabs from any page, `s` opens the Monitor's settings, `p` pauses
  or starts the backup. The list is in the settings dialog.
- The browser tab's title carries the state: a dot while uploading, a pause mark while
  paused, an exclamation mark while a warning is raised.
- The desktop pane can reconnect. When noVNC reports the connection is gone, a notice with a
  Reconnect button appears above the pane and reloads the frame.
- Every page honours every theme. The thirteen palettes were defined in the Monitor's page
  only; the Status, Tools and API pages knew one of them. The palettes now live in one
  block spliced into every page, so a theme chosen in the Monitor applies everywhere. The
  dim text colour is derived from whichever palette is in force and now lives with the
  palettes: it was defined for the plain pages only, so the Monitor's progress chart had no
  value to resolve and its axis labels fell back to the SVG default, which is black on a
  dark theme.
- Text uses the width of the window. The Status, Tools and API pages capped prose at 70
  characters and the permission list at 52, which is about 375 pixels; the pages now use
  the same 1100-pixel measure as the Monitor, with prose at 100 characters inside it.
- Accessibility. The health band, the completion banner and the Status notices are
  announced to a screen reader when they change. Animations stop when the browser asks for
  reduced motion.
- `bb-health`'s description on the Tools tab reads "with diagnostic information".
- The container asks the client about itself. Backblaze ship `bzcli` with the client, and
  its report command answers a query path with that part of its own account of itself. The
  container now reads five of those paths on a slow cycle. It never reads the whole
  document and never asks for `/backup/account`: both carry the account email and the
  login, and querying by path keeps them out of this container altogether.
- A warning when the licence lapses or a renewal fails. Backblaze report both, and until now
  the container had no way of knowing. A card that expires stops the backup quietly, with
  nothing on the dashboard to say why. It now appears in both monitors, in the API, in the
  Prometheus gauges and as a notification event, the same as a safety freeze.
- Per drive, whether the client is set to back it up. The client keeps a filter list with one
  root entry per drive, and that entry decides whether the drive backs up at all. A drive
  whose root reads "none" shows as ticked in the client's own settings window and backs up
  nothing, which is the state behind "No files are selected". A user hit exactly this after
  moving containers and it took several rounds to work out. It now appears on the Status tab,
  in the terminal monitor and in `bb-doctor`, named for what it is.
- Whether a private encryption key is set, on the Status tab and in the terminal monitor.
  Backblaze cannot recover a forgotten one, which changes what recovery advice is safe.
- The assigned datacentre cluster, read from the client rather than inferred. The monitor
  worked it out by matching sockets on the owning process, which cannot answer while nothing
  is uploading. The client states it. The socket reading stays as the fallback.
- `bb-doctor` checks the client's own settings. The licence, the safety freeze, the
  encryption key, whether each mapped drive is set to be backed up, and whether the
  container's own config directory sits inside a backed-up drive without being excluded,
  which is the loop where the client backs up its own bookkeeping for ever.
- The Settings tab can change some of the client's settings: the thread count, the automatic
  and manual throttle, the largest file to back up, the staleness threshold, the name shown
  in your Backblaze account, network tests and backing up on battery. The description under
  each is the client's own wording, so it says what the Backblaze settings window says.
  The manual throttle is megabits per second per thread rather than for the backup as a
  whole, which is easy to misread, so the page multiplies it out by your thread count. The
  thread count is shown next to the per-thread ceiling the round trip imposes, since raising
  it only helps while that ceiling is not already what is holding the rate down.

  What cannot be changed from here: anything deciding what is backed up. The file selection,
  the exclusions and the schedule are absent by design, because a wrong edit there stops a
  backup rather than slowing one, and the private encryption key verbs are unreachable as
  they always were. Backblaze cannot recover a forgotten key.

  Changing a setting is written to the client's configuration at once. Whether a running
  client honours it without the container being restarted has not been established, and the
  page says so rather than claiming either way.
- `GET /api/v1/client` gives the same reading under `read`. `POST /api/v1/client` changes a
  setting under a new `configure` permission, which no existing key holds. The drive list
  and the exclusions name directories on your own share, so a key without `read:files` does
  not get them, the same rule the skipped list follows.
- The diagnostic bundle carries the client's settings. "What are your threads set to" is a
  question that gets asked every time. The name shown in your Backblaze account is removed
  from the capture first: the bundle hashes file and directory names, but a bare value is
  not a path and would have reached the bundle whole, and a machine name is often a person's.
- The backup total is shown to two decimal places. At one place a TB reading only moves
  every 102.4 GB, which on an 87 TB backup is most of a day of uploading, and a whole
  percent is several days; the terminal monitor showed a whole percent, which is worse. A
  correct figure sat still long enough to be reported as a stuck counter. It now reads
  20.31 TB and 23.37%, both of which move about hourly. Other figures keep one place: a
  file size in the completed table gains nothing from a second. The Backup gauge's tooltip
  also gives what this session has sent, which moves continuously. Two places brings the
  figure from a day to about two hours, so the session figure beside it is still the one
  that moves while you watch. Every other displayed figure was checked for the same fault
  and moves within about ninety seconds. The first-backup percentage had it and now reads
  two places as well.
- Progress per drive, when more than one is mapped. Both files the totals come from carry a
  figure per volume that nothing read until now, so each drive gets its own bar with the
  files it has left. A single-share container shows nothing new, because the one bar would
  repeat the total.
- What is left, against what has gone. The client gives no size breakdown of the files
  still to send, but the average size of the remainder against the average of what has
  already gone is available, and it is the figure that explains a long estimate. On a live
  container what remained averaged 602 MB a file against 8 MB for what had gone, which is
  why 96% of the files was 23% of the bytes. It sits beside Files Remaining.
- The rate's tooltip says what bounds it. Wine answers the ideal-send-backlog query with a
  fixed 64 KB, so an upload connection holds at most that much in flight and its rate is
  limited by that divided by the round trip. The monitor knows the round trip and the thread
  count, so it can state the modelled limit and whether the observed rate is at it, below it
  or above it. Stated as a measurement rather than as advice about thread counts: on a live
  container the observed rate was above what the model predicts, so the figure is a floor of
  unknown tightness and the container does not pretend otherwise.
- The state reads "Checking" while the client is checking files it has already sent. After a
  restart the client re-checks the small files between its checkpoint and where it had got
  to; nothing is sent and no thread runs, so the state read "Transmitting" with everything
  at zero, which looks like a stall and was reported as one.
- Each notification endpoint shows how its last delivery went, on the Settings page. A
  delivery that fails at three in the morning went to the container log, where nobody looks.
  The state covers this run of the service and is not kept across a restart, because a stale
  verdict would be worse than none.
- Notifications send the shape each service wants. A Pushbullet user found that the generic
  webhook did not work: Pushbullet requires `{"type": "note", "title", "body"}` and answers
  400 to anything else, and Discord (`content`) and Slack (`text`) have requirements of their
  own. There are now named kinds for Pushbullet, Discord, Slack and Gotify, each sending the fields that
  service documents and, for Gotify, its `X-Gotify-Key` header. A custom kind takes a JSON
  body you write with `{title}`, `{message}`, `{event}`, `{container}`, `{build}`, `{state}`,
  `{time}` and `{priority}` placeholders, escaped for you, and it is checked as JSON when you
  save. The generic webhook now carries `body` alongside `message`, a name some receivers
  expect. The Settings page shows a hint and the right URL for each kind.
- Recently Completed says "already backed up" for a small file the client checked and did
  not send. After a restart the client re-checks the small files between its checkpoint and
  where it had got to, one round trip each, and names each one as it goes; those rows showed
  dashes in every column. The client's
  transmission report records each such check as "dedup - 0 bytes", and the monitors now
  read those rows and say so. The API row carries `dedup: true`. Confirmed on a live
  container: the report row for a file appeared three seconds before the monitor saw the
  client move on from it.

### Changed
- The tabs in the web interface are Desktop, Monitor, Status, Tools, API and Settings.
  "Upload Monitor" is now "Monitor" and "Skipped Files" is now "Status". Settings holds
  notifications and quiet hours; API holds the keys.
- The tab row scrolls sideways when it does not fit the screen. A phone showed five tabs
  and no sign of the sixth. Now a finger swipes the row, a mouse drags it, a fade with a
  chevron at either edge says there is more in that direction, and the chosen tab is
  scrolled into view when it is selected or linked to. A saved tab or an old link
  that names `skipped` opens the Status tab.
- `bb-doctor` runs as the container user when you start it as root. `docker exec` enters
  the container as root, and root passes every permission test. `bb-doctor` uses
  permission tests to decide whether `/config` is writable and whether the client can read
  a skipped file, so from the console both always said yes. Now, when it starts as root, it
  restarts itself as `USER_ID:GROUP_ID`, or as the owner of `/config` when those are not
  set, with the same arguments. The output is then the answer for the user the client
  runs as. When the image has no way to drop privileges, the tool says so at the top of
  its output and gives the `docker exec -u` command to run instead. Beta only, through the
  same build-time patch as the skipped-file check.
- The dark theme is properly black rather than dark grey. It uses rogman's values.
- The web dashboard now works on a mobile browser.
- `bb-monitor` and `bb-monitor-web` share one data layer,
  `/usr/local/lib/bb-monitor/bbdata.py`, so a feature appears in both or in neither.

### Fixed
- `bb-doctor` reported a share with the wrong owner as readable, when it was run from the
  console. The readability test is `[ -r ]`, and the console is root, so the test could not
  fail. A user with the most common fault this container has ran the tool as the README
  said to and was told the files could be read. The tool now runs as the container user,
  see Changed above, and the same test gives the true answer.
- Small files never reached Recently Completed. The table is fed from the files caught in
  flight, and a small file is gone before a poll can catch a thread carrying one. Backblaze
  also pushes small files in bundles rather than singly, so the log holds no per-file record of
  them either. The monitors now take them from the client, which names each one in turn. They
  are listed without a thread, size or rate, because none of those exist for them.
- The compact multi-part view drew one row per thread. Nine threads on one film thus gave nine
  identical "0/21" rows, plus the chunk strip above them. The compact row now counts the file's
  parts rather than the thread's own progress. There is thus one row per file, and no row at
  all for the file the strip is already showing in full.
- The compact multi-part view showed a file that was not being uploaded. One example is "0/21
  chunks" against a film while the client was producing file lists. The container does not
  clear `bzcurrentlargefile/` when a file finishes, so it still described the last file split.
  The view now appears only while that file is the one being worked on.
- Multi-part totals were wrong again, in a different way: a 221 MB file with 10 MB parts listed
  as "22/236". The part size is constant for a file, but the code remembered a reading only
  when it came from the fallback path. A line that carried the size thus returned it without
  recording it. A later line without the size then fell back to the live counter, which
  describes the part a thread happens to be carrying. One short reading thus fixed a row's
  total for good. The code now remembers the largest credible reading for a file, whichever
  path it came from, and a short one cannot undo it. It also no longer sets a total at all from
  a part size below a mebibyte, which cannot be the configured one.
- Scan progress stuck at a figure like "50% 14/28 directories" and stayed there while files
  uploaded. A scan writes `.future` files, but it does not remove them when it ends, so their
  presence proved nothing. The monitors now show the progress only while `bzfilelist` is
  running and the files are still being written to.
- The scan bar took over the Uploading Now panel in the web dashboard, hiding the transfers for
  as long as it was up. A scan runs alongside uploads, so the scan bar now heads the panel
  instead of replacing it.
- File names carrying an apostrophe or an ampersand were shown as XML: "Mike Judge&apos;s" and
  "Colbert &amp; Fallon". The monitors now decode names taken from the client's XML.
- The compact multi-part setting governed only the per-file view. Both monitors drew the chunk
  strip regardless, although the setting was meant to control it. The setting now covers the
  strip, and the web dashboard redraws on the click rather than at the next poll.
- Completed multi-part files showed nonsense part counts such as "21/6594". The count was
  derived from `numBytes_to_send_in_shm` in the thread instruction. That value describes the
  part a thread is carrying, and not the file's part size. Readings well below the part size
  thus turned `filesize / part` into tens of thousands. The monitors now read the configured
  part size from the `bz_done` line, which is constant for a file. A total that large also
  meant the record never reached completion, so every later upload of the same file kept
  accumulating into it. Multi-part files also produce one push beyond their part count. After
  the correction, that push would otherwise open a second row for the same file. The monitors
  now absorb that trailing push. Reported by gandalf15.
- Sizes above a terabyte were rendered in gigabytes, so a 250 TB backup showed as "257524.9 GB"
  and overflowed the gauge label. Sizes now run to petabytes. Reported by gandalf15.
- File names were written into the page without HTML escaping, so a backed-up file whose name
  contained markup could inject it into the dashboard. Found by rogman.
- The dashboard had no viewport meta tag, so a mobile browser laid it out at around 980px and
  scaled it down to something unreadable. An iframe does not inherit its parent's.
- Opening the monitor shortly after a container start gave a bare 502. The error stayed until
  you reloaded the page by hand, because the shell loads the frame once and nothing retried.
  nginx now serves a holding page for the few seconds before the service is listening.
- A long file path made the whole page scroll sideways at any window width. Flex items default
  to `min-width:auto` and so refuse to shrink below their content, which let one path in the
  in-flight list widen everything around it.
- The HTTP API's tools routes returned `bb-doctor`'s full output to any key holding
  `diagnose`, although that output can name drives and directories the way `read:files`
  gates everywhere else in the API. A key without `read:files` now gets the job back with
  `lines` set to `null` and `withheld` naming the permission it is missing, instead of the
  tool's output.
- `per_volume` reached a key without `read:files` with the mount path and a stable
  identifier for the machine, although every other field naming a directory is withheld
  from that key. `per_volume` now drops the identifier and reduces the path to a bare drive
  letter, or `null` when the client's own mount path is longer than that.
- A notification endpoint could point at the container's own loopback address, a link-local
  address, or anything else reachable on the Docker network, and testing it echoed back the
  remote's own status code or connection error. The container now resolves the host first
  and refuses loopback, link-local and the unspecified address; a test now only ever reports
  "delivered" or "not delivered", and the endpoint's actual reply goes to the container log
  instead.
- The web routes carried no `X-Frame-Options`, `X-Content-Type-Options` or
  `Referrer-Policy` header, and nothing capped the size of a request body. Both are set on
  `/monitor/` now, and a body over 1 MB is rejected before it is read, at nginx and inside
  the service.
- `GET /api/v1/report/download/<token>` answered before the API's own "no key means no
  surface" check, so a container with no key configured still gave a real 404 with a schema
  field, unlike every other unconfigured path. The route now sits behind that same check.
- The three routes that read a request body had no socket timeout and no check on
  `Content-Length` before reading it, so a slow or oversized POST could hold a handler open
  indefinitely. The socket now times out after 30 seconds, and a body outside 0 to 1 MB is
  rejected with 413 before it is read.
- An unwritable notification state file meant a state change could not be remembered, so the
  same event could fire on every poll instead of once; the file was also rewritten on every
  poll regardless of whether anything had changed. The state is now kept in memory as well,
  so a write failure does not lose it, one line goes to the container log the first time a
  write fails, and a write happens only when the state has actually changed.
- Pausing or resuming for a quiet hours window ran `bzcli` directly on the thread that also
  collects the monitor's own data, so the whole monitor stalled for as long as a slow or
  wedged client took to answer. The action now runs on its own thread.
- `quiet.state()` published `window_end` even outside the window it describes, which read as
  though a window not yet reached had already ended. `window_end` is now `null` outside a
  window.
- Quiet hours boundaries were computed by adding a wall-clock duration in seconds to the
  day's start, which is wrong on a clock-change day: a window computed this way could open
  or close an hour early or late in spring and autumn. Each boundary is now built from its
  own date and wall time instead.
- Chunks completed in the last minute was measured against the newest line in the log tail
  rather than against the current time, so the figure froze at whatever the last burst
  reached and stayed there for hours after uploads had actually stopped. It is now measured
  against now.
- A file the datacentre already held, and so did not need sending again, was matched by
  splitting its log line on its last " - ", so a file whose own name contained that sequence
  ("Artist - Track.mp3") lost everything before it and was never recognised as already
  backed up. The match is now taken from the line's fixed prefix instead.
- The first-terabyte milestone fired at a decimal terabyte while the gauge beside it renders
  in binary units, so the banner could appear well before the gauge itself read 1 TB. The
  milestone now uses the same tebibyte threshold the gauge does.
- The improving/worsening ETA trend shown on the Status tab and in the terminal monitor did
  not appear in the plain-text summary used for support threads. `summary_text` now includes
  it, worded the way the page already is ("better"/"slower" rather than bbdata's own
  "improving"/"worsening").
- Several callers arriving on a stale client-settings cache each started their own sweep of
  Wine, so four callers used to run twenty separate `bzcli` calls at once on a container
  with little memory to spare. A caller arriving while a sweep is already running now waits
  for it and shares its result.
- A `bzcli` call that hung left its Wine process running for the life of the container,
  because a timed-out call killed only the direct child and nothing that child had started.
  A timeout now kills the whole process group instead.
- An unexpected shape in one log line or one XML file could take the whole terminal monitor
  down with an unhandled traceback. It now keeps the last good screen, names the fault in
  the footer, and tries again on the next tick.
- The diagnostic bundle's client-settings capture redacted `online_hostname` by replacing
  the whole matching line, which is only correct for pretty-printed output; against the
  compact single-line JSON `bzcli` can also produce, it erased every other setting on the
  line along with it. The redaction is now by value, and the same by-value redaction now
  also covers every name in `bb-report`'s own list of secret keys, which that capture's JSON
  shape had been passing straight through.


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
- Guidance to keep the Backblaze thread count manual and modest (4 to 8 threads); the automatic
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
