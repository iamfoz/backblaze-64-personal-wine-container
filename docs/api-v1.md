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
| `control:pause` | Ask a running backup to pause. |
| `report` | Reserved. Refused until the diagnostic bundle flow exists. |

`control` is accepted as shorthand for both control operations and expands when the key is
created, so what is stored is always the explicit list.

Grant the least that works. A display showing rate and progress needs `read` alone, and
withholding `read:files` from it means a compromised key does not enumerate everything on
the array.

## Endpoints

### `GET /api/v1/status`

Requires `read`. The whole monitor payload. See the field reference below.

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
| `state` | string | What the client is doing, in its own words. |
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

### `backup`

Overall progress. `null` before the client has reported totals.

| Field | Type | Meaning |
|---|---|---|
| `done_bytes` / `total_bytes` | int | Uploaded, and selected for backup. |
| `pct` | number | Percentage complete. |
| `done_files` / `total_files` / `remaining_files` | int, null | File counts. |
| `eta_seconds` | int, null | Estimate, weighted by completed transfers. |
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

`success` and `failures` counts for the current day, or `null` if not yet reported.

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

## What is recorded

A successful control action is written to the container log with the public id of the key
that asked for it, so there is a trace of anything that changed the system. Reads are not
logged: a consumer polling every few seconds would bury everything else.

Secrets never appear in a log. A failed authentication records the key's public id where one
could be parsed, and nothing otherwise.

`bb-apikey list` shows when each key was last used, to the nearest minute. It is deliberately
coarse: recording every request would mean rewriting the key store on each poll.

## Notes for consumers

Poll no faster than `poll_interval_seconds`. The container refreshes on its own schedule and
a faster poll returns the same snapshot.

Handle `null` everywhere. Every optional field above is genuinely absent in ordinary
conditions, not only in error.

Do not parse `state` or `activity.phase` for control flow beyond display. They are the
client's own words and can gain new values without a schema change.

Use `build` when reporting a problem. It identifies exactly what produced a payload.
