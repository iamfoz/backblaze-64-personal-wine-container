# HTTP API, version 1

A key-authenticated read and control surface for anything outside the browser: a status
display, an automation system, a script, a plugin of your own. It is served on the same
port as the web interface, so nothing extra needs publishing.

The web interface itself sits behind whatever `WEB_AUTHENTICATION` and `SECURE_CONNECTION`
are configured for the GUI. `/api/v1/` is the one path exempted from that login, because a
consumer that is not a browser cannot satisfy one. It defends itself with a key instead.

## Turning it on

There is no separate switch. The API is live exactly when an unrevoked key exists, and
answers `404` on every path until then, so a container nobody has configured does not
advertise it.

Create a key from the **API** tab of the web interface, or from a terminal:

```
docker exec <container> bb-apikey create --label "status display" --scope read
```

The secret is printed once and stored only as a SHA-256. If it is lost, revoke the key and
issue another. `bb-apikey list` shows what exists, `bb-apikey revoke <id>` withdraws one,
and `bb-apikey permissions` prints what can be granted.

## Authenticating

Send the key as a bearer token:

```
curl -H "Authorization: Bearer bb64_1a2b3c4d_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
     https://<host>:<port>/api/v1/status
```

A key is `bb64_<id>_<secret>`. The `<id>` is public: it identifies the key in listings and
logs, so a failed request can be traced without the secret appearing anywhere.

| Response | Meaning |
|---|---|
| `200` | Fine. |
| `401` | Missing, malformed, revoked, or lacking the permission for this endpoint. |
| `404` | No key exists at all, or the endpoint does not exist. |
| `502` | The action was attempted and the backup client reported failure. |
| `503` | The backup client's own tool is not present in the container. |

`401` covers "wrong key" and "right key, wrong permission" alike. That is deliberate: a key
learns nothing about what it does not hold.

## Permissions

Granted per operation, not in tiers, because these are not a ladder — none implies another.

| Permission | Grants |
|---|---|
| `read` | Status: rates, progress, memory, latency, health. No file names. |
| `read:files` | Adds the names of files being backed up. |
| `control:backup-now` | Start a backup if one is not already running. |
| `control:pause` | Pause a running backup. |
| `report` | Generate and download a diagnostic bundle. |

`control` is accepted as shorthand for both control operations and expands when the key is
created, so what is stored is always the explicit list.

Grant the least that works. A display showing rate and progress needs `read` alone, and
withholding `read:files` from it means a compromised key does not enumerate everything on
the array.

## Endpoints

### `GET /api/v1/status`

Requires `read`. The whole monitor payload. See the field reference below.

`?fields=` trims the response to the top-level fields named, comma-separated, plus
`schema` and `ok`, which every response carries:

```
curl -H "Authorization: Bearer <key>" "https://<host>:<port>/api/v1/status?fields=rate_bytes_per_sec,paused"
```

Names the payload does not have are ignored rather than refused, so a consumer built
against a newer container keeps working on an older one that lacks a field.

### `GET /api/v1/key`

Any valid key. Describes the key presenting it, so a consumer can discover its own
permissions rather than probing endpoints and collecting refusals.

```json
{ "schema": 1, "id": "1a2b3c4d", "permissions": ["read", "read:files"] }
```

### `GET /api/v1/control`

Requires at least one `control:` permission. Lists only the operations that key holds, and
whether the client's control tool is present.

```json
{
  "schema": 1,
  "available": true,
  "actions": [ { "name": "pause", "does": "ask the running backup to pause, cooperatively" } ]
}
```

### `POST /api/v1/control/backup-now`

Requires `control:backup-now`. Starts a backup if one is not running. No body.

### `POST /api/v1/control/pause`

Requires `control:pause`. Asks a running backup to pause. No body.

Both return:

```json
{ "schema": 1, "action": "pause", "ok": true, "detail": "..." }
```

Pausing is cooperative: the backup client is asked to stop, and its own process is left
alone. Nothing here kills a process. Starting a backup is how a pause is lifted; there is no
separate resume.

It does stop the uploads. Measured on a live backup: 8 transfers completed in the minute
before pausing, none at all in the two minutes after, and they resumed on `backup-now`.

Pausing is not instant. The client finishes the transfers already in flight before it stops,
which is the point of asking it rather than killing it, and Backblaze's own window shows the
backup as still running until that drain completes. That window is not lagging; it is
waiting for the same thing. `paused` goes true when the pause is requested and `draining`
stays true until the client has actually stopped, so a consumer wanting to show a settled
state should wait for `draining` to clear rather than treat the request as the arrival.

### `POST /api/v1/report`

Requires `report`. Starts a diagnostic bundle and returns `202` at once, because
generating one is not instant and a request that blocks is a request that times out
somewhere in between.

```json
{ "schema": 1, "job": "a0bee5df9a11", "state": "running", "joined_existing": false }
```

One bundle is built at a time. A second request while one is in flight returns the same
job with `joined_existing: true` rather than running it twice over the same config.

### `GET /api/v1/report/<job>`

Requires `report`. Poll until `state` is `done` or `failed`.

```json
{
  "schema": 1, "job": "a0bee5df9a11", "state": "done", "size_bytes": 148213,
  "download": "report/download/xh-qAnV10It3gPqS4YCfcwGGNjClD4",
  "download_expires_in": 298
}
```

`download` is relative to `/api/v1/`. A finished job is forgotten an hour after it started.

### `GET /api/v1/report/download/<token>`

**No bearer token, and none should be sent.** The link is the credential, which is the
reason it exists: this is the URL a browser follows, and a key in a URL ends up in browser
history, in server logs and in a `Referer` header.

The link is single use and lives about five minutes. Fetching it returns the zip and burns
the token; a second fetch, or one made after it expires, returns `404`. Generate another
bundle if you need it again.

The bundle contains no file names, no account details and no keys, but it does describe the
host. Look through it before sending it anywhere.

## Schema versioning

Every response carries `"schema"`. A consumer is released independently of this container,
so the payload is a contract from the moment it ships.

Within a version, fields may be **added**. Nothing is removed, renamed, or has its units or
meaning changed. If that becomes necessary the number goes up and `/api/v2/` appears
alongside. Read `schema` and refuse to render what you do not recognise, rather than
guessing.

Any field can be `null` when the underlying figure is not available — a scan is not running,
the platform offers no round-trip time, the client has not reported yet. Treat `null` as
"unknown", never as zero.

## Field reference: `GET /api/v1/status`

Everything is in raw units. Bytes are bytes, seconds are seconds, times are Unix epoch
seconds. Nothing is pre-formatted, because a consumer wants to graph a number or format it
for its own locale.

### Top level

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | Contract version. Currently `1`. |
| `ok` | bool | `false` means collection failed; an `error` string is present instead of the rest. |
| `build` | string | Running build of this container. Quote it in a bug report. |
| `time` | int | When this snapshot was taken, epoch seconds. |
| `poll_interval_seconds` | number | How often the container refreshes. Polling faster gains nothing. |
| `state` | string | What the client is doing, in its own words. Reads `Paused` when paused. |
| `paused` | bool | Whether a backup is paused. The field to render a pause button from. |
| `threads` | int | Upload threads currently running. |
| `rate_bytes_per_sec` | int | Current upload rate. |
| `session_bytes` | int | Uploaded since this container started. |
| `chunks_last_minute` | int | Chunks completed in the last 60 seconds. |
| `uptime_seconds` | int | How long the monitor has been running. |
| `skipped_files` | int, null | Files the client has given up on. Neither queued nor retried, so a non-zero value means data is unprotected. |
| `last_backup_days` | number, null | Days since a backup pass completed. |
| `upload_pod` | string, null | The storage host in use. |
| `compress_saved_bytes` | int, null | Bytes saved by compression. |

### `activity`

What the client has in hand right now. `null` when it is doing nothing.

| Field | Type | Meaning |
|---|---|---|
| `phase` | string | `Uploading`, `Preparing`, `Finishing`, `Producing file lists`, `Uploading backup records`. |
| `file` | string, null | The file. Always `null` without `read:files`. |
| `part` | int, null | Which part of a multi-part file. |
| `internal` | bool | `true` when this is the client's own bookkeeping rather than one of your files. |

### `pause`

| Field | Type | Meaning |
|---|---|---|
| `paused` | bool | Same as the top-level `paused`. True from the moment the pause is requested. |
| `draining` | bool | True while the client is still finishing transfers that were already in flight. A pause is not instant, and until this goes false the backup is on its way to stopping rather than stopped. `state` reads `Pausing` meanwhile. |
| `until` | int, null | Epoch seconds the pause runs to. `null` means it holds until a backup is started. |
| `reason` | string, null | The client's own word for why, when it paused itself. |

A pause the client set for its own reasons looks the same as one requested through the API.
There is no separate resume: starting a backup is what lifts it.

### `backup`

Overall progress. `null` before the client has reported totals.

| Field | Type | Meaning |
|---|---|---|
| `done_bytes` / `total_bytes` | int | Uploaded, and selected for backup. |
| `pct` | number | Percentage complete. |
| `done_files` / `total_files` / `remaining_files` | int, null | File counts. |
| `eta_seconds` | int, null | Estimate, weighted by completed transfers. |
| `eta_date` | string, null | The same estimate as a calendar date, e.g. `17 Feb 2027`. Day resolution on purpose: an estimate from a moving average should not pretend to know the hour. |
| `eta_samples` | int | How many completions the estimate rests on. A low number means a rough estimate. |

### `scan`

Present only while a file-list scan is running, `null` otherwise.

| Field | Type | Meaning |
|---|---|---|
| `dirs_done` / `dirs_total` | int | Top-level directories indexed. |
| `pct` | number | Percentage of directories indexed. |
| `files` / `bytes` | int, null | Found so far. |

### `memory`, `swap`

Container memory and host swap. Either can be `null` where the platform does not expose it.

| Field | Type |
|---|---|
| `used_bytes` | int |
| `total_bytes` | int |
| `pct` | number |

### `latency`

Round-trip time to the storage host, read from the kernel rather than measured, so it costs
no traffic.

| Field | Type | Meaning |
|---|---|---|
| `ms` | number, null | Smoothed round-trip time. |
| `host` | string, null | Which host it describes. |
| `note` | string, null | Why `ms` is null: nothing uploading, no kernel socket table, or a connection ending locally. |

### `health`

An array, empty when nothing is wrong. Each entry:

| Field | Type | Meaning |
|---|---|---|
| `kind` | string | Machine-readable category. |
| `text` | string | Human-readable description. |

A non-empty array is the field to alert on.

### `first_backup`

Present while a first pass is still working through the set, `null` afterwards. `days` is
how long it has been running, `pct` how far it has got. The client exposes no
"initial backup finished" flag, so this is inferred.

### `client_measured_kbit`

The backup client's own throughput measurement, not this container's: `large_kbit` for files
over a megabyte, `small_kbit` for smaller ones. Small files are much slower because each
costs a round trip.

### `uploads_today`

Counts for the client's most recent recorded day, or `null` if not yet reported.

| Field | Type | Meaning |
|---|---|---|
| `success` | int | Uploads completed. |
| `failures` | int | Failed **attempts**, not failed files. Kept under this name for the schema promise; `retried_attempts` is the same number under an honest one. |
| `retried_attempts` | int | Attempts a storage vault turned away. The client retries against another vault and the file still goes up, so these name no file and appear in no per-file log. |
| `reasons` | object | The breakdown: `vault_busy`, `vault_full`, `unknown`. |

Do not alert on `failures`. A handful per day is Backblaze's own load balancing working as
designed. The field that means data is not backed up is `skipped_files`.

### `composition`

What the backup is made of, from the client's completed file statistics, or `null` before a
scan has finished. Categories carry only nonzero counts; `other` is the remainder, so the
parts account for the whole.

| Field | Type | Meaning |
|---|---|---|
| `files` | int | Files selected for backup. |
| `bytes` | int | Their total size. |
| `categories` | object | Counts by kind: `photos`, `documents`, `music`, `video`, `other`. |

### `backing_up_since`

`YYYYMMDD` string: when this backup began. `null` if the client's records carry no date.

### `eta_trend`

Whether the estimate moved since yesterday, or `null` when there is no estimate or no
history yet. One sample is kept per day, because an estimate compared with itself an hour
ago only measures the jitter of the moving average it came from.

| Field | Type | Meaning |
|---|---|---|
| `direction` | string | `improving`, `worsening`, or `steady` (within two percent or one day, whichever is larger). |
| `delta_seconds` | int | Signed change since the previous recorded day. Negative is better. |

### `upload_history`

The client's own per-day upload record, oldest first, up to seven days, or `null` before
anything has been recorded. Each entry:

| Field | Type | Meaning |
|---|---|---|
| `day` | string, null | `YYYYMMDD`. Can be `null` if the client's record carried no recognisable date; order still holds. |
| `success` | int | Uploads completed that day. |
| `retried` | int | Attempts turned away and retried. See `uploads_today`. |

### `completion`

Present for the seven days after a first backup finishes, then `null` forever. It fires
once: the moment is latched on disk, so files added later cannot replay it.

| Field | Type | Meaning |
|---|---|---|
| `done_at` | int | When the backup first caught up, epoch seconds. |
| `days` | int | How long the first pass took. |
| `total_bytes` | int | The size of the set it worked through. |

### `files`

`null` entirely for a key without `read:files`.

`in_flight` — an array of what is uploading now:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | File name. |
| `pct` | number | Estimated progress of this transfer. |
| `size_bytes` | int, null | Whole file. |
| `part_bytes` | int, null | Size of one part. |
| `parts` | object, null | `done` and `total` for a multi-part file. |

`recent` — an array of recent completions, oldest first:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | File name. |
| `time` | string | Local time of completion. |
| `chunked` | bool | Whether it was multi-part. |
| `parts` | object | For a multi-part file, `done` and `total`. |
| `bytes` / `seconds` / `kbit_per_sec` | int | Transfer figures. |
| `thread` | int | Which thread carried it. |
| `measured` | bool | `false` for a file too small to catch in flight: the client named it and moved on, so there is no thread, size or rate, and completion is inferred rather than confirmed. |

`chunk_map` — where the parts of the large file currently being split have got to, or
`null` if none is:

| Field | Type | Meaning |
|---|---|---|
| `file` | string | The file being split. |
| `total` | int | Total chunks. |
| `sent` | array of int | Chunk indices seen to complete. |
| `in_flight` | array of int | Chunk indices a thread is carrying now. |

Chunks that finished before the container started are in neither array: they cannot be told
apart from chunks not yet started.

## Cross-origin requests

A consumer running in a browser cannot reach the API from another origin unless you say so.
Set `API_CORS_ORIGINS` on the container to a comma-separated list:

```
API_CORS_ORIGINS=https://dash.example.com,https://other.example
```

Unset, which is the default, no cross-origin request succeeds. There is deliberately no
wildcard: a key is still required either way, but with `*` any page the browser happens to
load could poll the container in the background, and the answer describes what is being
backed up.

Only `/api/v1/` is covered. The key management pages are authorised by the browser session,
so allowing another origin to call them would hand key creation to any page you have open.

## Key expiry

A key never expires unless you give it a lifetime. That is the right default for something
long-running, which should not stop working at a date nobody remembers setting.

Put a date on a key you are handing to someone for a one-off. An expired key stops
authenticating, stops appearing as active, and does not keep the API alive on its own: if it
is the only key, the surface returns to answering `404`.

## What is recorded

A successful control action is written to the container log with the public id of the key
that asked for it, so there is a trace of anything that changed the system. Reads are not
logged: a consumer polling every few seconds would bury everything else.

Secrets never appear in a log. A failed authentication records the key's public id where one
could be parsed, and nothing otherwise.

`bb-apikey list` shows when each key was last used, to the nearest minute. It is deliberately
coarse: recording every request would mean rewriting the key store on each poll.

## Notes for consumers

The service speaks HTTP/1.1 and keeps connections alive, so a polling client should reuse
its connection rather than opening one per request.

Poll no faster than `poll_interval_seconds`. The container refreshes on its own schedule and
a faster poll returns the same snapshot.

Handle `null` everywhere. Every optional field above is genuinely absent in ordinary
conditions, not only in error.

Do not parse `state` or `activity.phase` for control flow beyond display. They are the
client's own words and can gain new values without a schema change.

Use `build` when reporting a problem. It identifies exactly what produced a payload.
