# HTTP API, version 1

The API gives read and control access to any consumer outside the browser. Examples: a
status display, an automation system, a script, or a plugin of your own. A key
authenticates each request. The container serves the API on the same port as the web interface, so you do not
need to publish another port.

`WEB_AUTHENTICATION` and `SECURE_CONNECTION` control access to the web interface.
`/api/v1/` is the one path that this login does not protect, because a consumer that is not
a browser cannot complete a login. A key protects the API instead.

## Turning it on

There is no separate switch. The API is active exactly when one key exists and you have not
revoked it. Until then it answers `404` on every path. A container that nobody has
configured therefore does not show that the API is present.

Create a key from the **API** tab of the web interface, or from a terminal:

```
docker exec <container> bb-apikey create --label "status display" --scope read
```

The container prints the secret one time and stores only a SHA-256 of it. If you lose the
secret, revoke the key and create another. `bb-apikey list` shows the keys that exist,
`bb-apikey revoke <id>` revokes one key, and `bb-apikey permissions` prints the permissions
that you can grant.

## Authenticating

Send the key as a bearer token:

```
curl -H "Authorization: Bearer bb64_1a2b3c4d_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
     https://<host>:<port>/api/v1/status
```

A key has the form `bb64_<id>_<secret>`. The `<id>` is public. It identifies the key in the
listings and in the log, so you can trace a failed request and the secret does not appear
anywhere.

| Response | Meaning |
|---|---|
| `200` | The request succeeded. |
| `401` | The key is missing, malformed, or revoked, or it does not hold the permission for this endpoint. |
| `404` | No key exists at all, or the endpoint does not exist. |
| `502` | The API tried the action, and the client reported a failure. |
| `503` | The client's own tool is not present in the container. |

`401` covers a wrong key and a correct key without the necessary permission. This is
deliberate: a key learns nothing about the permissions that it does not hold.

## Permissions

You grant each permission for one operation. There are no tiers, because no permission
includes another one.

| Permission | Grants |
|---|---|
| `read` | Status: rates, progress, memory, latency, health. No file names. |
| `read:files` | Adds the names of the files that the client backs up. |
| `control:backup-now` | Start a backup if one is not already running. |
| `control:pause` | Pause a running backup. |
| `report` | Generate and download a diagnostic bundle. |

`control` is a shorthand for both control operations. The container expands the shorthand
when it creates the key, so it always stores the explicit list.

Grant the smallest set that works. A display that shows the rate and the progress needs
`read` alone. If you do not grant `read:files`, a compromised key cannot list everything on
the array.

## Endpoints

### `GET /api/v1/status`

Requires `read`. The whole monitor payload. See the field reference below.

`?fields=` limits the response to the top-level fields that you name, separated by commas.
Every response also carries `schema` and `ok`:

```
curl -H "Authorization: Bearer <key>" "https://<host>:<port>/api/v1/status?fields=rate_bytes_per_sec,paused"
```

The API ignores a name that the payload does not have, and does not refuse the request. A
consumer that you built for a newer container therefore keeps working on an older container
that does not have a field.

### `GET /api/v1/key`

Any valid key. This endpoint describes the key that you present. A consumer can therefore
find its own permissions. It does not have to try each endpoint and collect the refusals.

```json
{ "schema": 1, "id": "1a2b3c4d", "permissions": ["read", "read:files"] }
```

### `GET /api/v1/control`

Requires at least one `control:` permission. It lists only the operations that the key
holds. It also reports whether the client's control tool is present.

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

A pause is cooperative. The API asks the client to stop, and it does not touch the client's
own process. The API never stops a process by force. To end a pause, start a backup. There
is no separate resume.

A pause does stop the uploads. Measured on a live backup: 8 transfers completed in the
minute before the pause, and none at all in the two minutes after. The transfers started
again on `backup-now`.

A pause is not immediate. The client first completes the transfers that it already started.
This is the reason to ask the client to stop and not to stop it by force. Backblaze's own
window shows the backup as still running until those transfers complete. That window is not
late; it waits for the same event.

`paused` becomes true when you request the pause. `draining` stays true until the client
has actually stopped. A consumer that shows a settled state must wait for `draining` to
become false. Do not treat the request itself as the completed pause.

### `POST /api/v1/report`

Requires `report`. It starts a diagnostic bundle and returns `202` immediately. The bundle
is not instant to generate, and a request that waits for it can time out at some point in
between.

```json
{ "schema": 1, "job": "a0bee5df9a11", "state": "running", "joined_existing": false }
```

The container builds one bundle at a time. If you send a second request while a bundle is
running, the API returns the same job with `joined_existing: true`. It does not build the
bundle twice over the same config.

### `GET /api/v1/report/<job>`

Requires `report`. Poll until `state` is `done` or `failed`.

```json
{
  "schema": 1, "job": "a0bee5df9a11", "state": "done", "size_bytes": 148213,
  "download": "report/download/xh-qAnV10It3gPqS4YCfcwGGNjClD4",
  "download_expires_in": 298
}
```

`download` is relative to `/api/v1/`. The container discards a finished job one hour after
the job started.

### `GET /api/v1/report/download/<token>`

**This endpoint takes no bearer token, and you must not send one.** The link is the
credential, which is the reason it exists: this is the URL a browser follows, and a key in a
URL ends up in browser history, in server logs and in a `Referer` header.

You can use the link one time only, and it is valid for approximately five minutes. A fetch
returns the zip and cancels the token. A second fetch, or one made after the link expires,
returns `404`. Generate another bundle if you need it again.

The bundle contains no file names, no account details and no keys, but it does describe the
host. Examine it before you send it anywhere.

## Schema versioning

Every response carries `"schema"`. You release a consumer independently of this container,
so the payload is a contract from the moment it ships.

Within a version, fields may be **added**. Nothing is removed, renamed, or has its units or
meaning changed. If that becomes necessary, the number goes up and `/api/v2/` appears
alongside. Read `schema` and refuse to show what you do not recognise, rather than guessing.

Any field can be `null` when the figure behind it is not available. Examples: a scan is not
running, the platform offers no round-trip time, or the client has not reported yet. Treat
`null` as "unknown", never as zero.

## Field reference: `GET /api/v1/status`

All values are in raw units. Bytes are bytes, seconds are seconds, times are Unix epoch
seconds. The container formats nothing, because a consumer wants to graph a number or
format it for its own locale.

### Top level

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | Contract version. Currently `1`. |
| `ok` | bool | `false` means that the collection failed. An `error` string replaces the other fields. |
| `build` | string | The build of this container that is running. Give this value in a bug report. |
| `time` | int | When the container took this snapshot, epoch seconds. |
| `poll_interval_seconds` | number | How often the container refreshes. A faster poll gives nothing more. |
| `state` | string | What the client is doing, in its own words. It reads `Paused` during a pause. |
| `paused` | bool | Whether a backup is paused. Use this field to draw a pause button. |
| `threads` | int | Upload threads currently running. |
| `rate_bytes_per_sec` | int | Current upload rate. |
| `session_bytes` | int | Uploaded since this container started. |
| `chunks_last_minute` | int | Chunks completed in the last 60 seconds. |
| `uptime_seconds` | int | How long the monitor has been running. |
| `skipped_files` | int, null | Files that the client has stopped trying to back up. It does not queue them and does not retry them, so a value above zero means that data is unprotected. |
| `last_backup_days` | number, null | Days since a backup completed. |
| `upload_pod` | string, null | The storage host in use. |
| `compress_saved_bytes` | int, null | Bytes saved by compression. |

### `activity`

What the client is working on right now. `null` when it is doing nothing.

| Field | Type | Meaning |
|---|---|---|
| `phase` | string | `Uploading`, `Preparing`, `Finishing`, `Producing file lists`, `Uploading backup records`. |
| `file` | string, null | The file. Always `null` without `read:files`. |
| `part` | int, null | Which part of a multi-part file. |
| `internal` | bool | `true` when the client is working on its own records and not on one of your files. |

### `pause`

| Field | Type | Meaning |
|---|---|---|
| `paused` | bool | The same as the top-level `paused`. True from the moment that you request the pause. |
| `draining` | bool | True while the client is still completing the transfers that it already started. A pause is not immediate. Until this field goes false, the backup is stopping but has not stopped. `state` reads `Pausing` meanwhile. |
| `until` | int, null | Epoch seconds the pause runs to. `null` means that it holds until you start a backup. |
| `reason` | string, null | The client's own word for the cause, when it paused itself. |

A pause that the client set for its own reasons looks the same as one that you request
through the API. There is no separate resume: a backup that you start is what ends it.

### `backup`

Overall progress. `null` before the client has reported totals.

| Field | Type | Meaning |
|---|---|---|
| `done_bytes` / `total_bytes` | int | Uploaded, and selected for backup. |
| `pct` | number | Percentage complete. |
| `done_files` / `total_files` / `remaining_files` | int, null | File counts. |
| `eta_seconds` | int, null | Estimate, weighted by completed transfers. |
| `eta_date` | string, null | The same estimate as a calendar date, e.g. `17 Feb 2027`. The resolution is one day on purpose, because an estimate from a moving average is not accurate to the hour. |
| `eta_samples` | int | How many completed transfers the estimate rests on. A low number means a rough estimate. |

### `scan`

Present only while a file-list scan is running, `null` otherwise.

| Field | Type | Meaning |
|---|---|---|
| `dirs_done` / `dirs_total` | int | Top-level directories indexed. |
| `pct` | number | Percentage of directories indexed. |
| `files` / `bytes` | int, null | Found so far. |

### `memory_by_process`

The programs that use the most memory, largest first, or `null`. Each entry has a `name` and
`rss_bytes`.

The container memory figure alone does not say which program uses the memory. Users have
reported high memory after an update when the cause was the client, which reads a large file
list into memory during a scan. Use this field to name the program before you report a
problem.

### `memory`, `swap`

Container memory and host swap. Each one can be `null` where the platform does not report
it.

| Field | Type |
|---|---|
| `used_bytes` | int |
| `total_bytes` | int |
| `pct` | number |

### `latency`

The round-trip time to the storage host. The container reads it from the kernel and does
not measure it, so it costs no traffic.

| Field | Type | Meaning |
|---|---|---|
| `ms` | number, null | Smoothed round-trip time. |
| `host` | string, null | Which host it describes. |
| `note` | string, null | Why `ms` is null: nothing is uploading, there is no kernel socket table, or a connection ends locally. |

### `health`

An array, empty when nothing is wrong. Each entry:

| Field | Type | Meaning |
|---|---|---|
| `kind` | string | Machine-readable category. |
| `text` | string | Human-readable description. |

Alert when this array is not empty.

### `first_backup`

Present while a first backup is still working through the set, `null` afterwards. `days` is
how long it has been running, `pct` is how far it has got. The client exposes no
"initial backup finished" flag, so the container infers this.

### `client_measured_kbit`

The client's own throughput measurement, not this container's: `large_kbit` for files over
a megabyte, `small_kbit` for smaller ones. Small files are much slower, because each one
costs a round trip.

### `uploads_today`

Counts for the client's most recent recorded day, or `null` if it has not reported yet.

| Field | Type | Meaning |
|---|---|---|
| `success` | int | Uploads completed. |
| `failures` | int | Failed **attempts**, not failed files. The name stays for the schema promise; `retried_attempts` is the same number under an honest one. |
| `retried_attempts` | int | Attempts that a storage vault turned away. The client retries against another vault and the file still goes up, so these name no file and appear in no per-file log. |
| `reasons` | object | The breakdown: `vault_busy`, `vault_full`, `unknown`. |

Do not alert on `failures`. A small number each day is Backblaze's own load balancing
working as designed. `skipped_files` is the field that means data is not backed up.

### `composition`

What the backup is made of, from the client's completed file statistics, or `null` before a
scan has finished. The categories carry only nonzero counts. `other` is the remainder, so
the parts account for the whole.

| Field | Type | Meaning |
|---|---|---|
| `files` | int | Files selected for backup. |
| `bytes` | int | Their total size. |
| `categories` | object | Counts by kind: `photos`, `documents`, `music`, `video`, `other`. |

### `backing_up_since`

`YYYYMMDD` string: when this backup began. `null` if the client's records carry no date.

### `eta_trend`

Whether the estimate moved since yesterday, or `null` when there is no estimate or no
history yet. The container keeps one sample per day. The reason: an estimate compared with
itself an hour ago only measures the jitter of the moving average it came from.

| Field | Type | Meaning |
|---|---|---|
| `direction` | string | `improving`, `worsening`, or `steady` (within two percent or one day, whichever is larger). |
| `delta_seconds` | int | Signed change since the previous recorded day. Negative is better. |

### `upload_history`

The client's own per-day upload record, oldest first, up to seven days, or `null` before it
has recorded anything. Each entry:

| Field | Type | Meaning |
|---|---|---|
| `day` | string, null | `YYYYMMDD`. Can be `null` if the client's record carried no date that the container recognises. The order still holds. |
| `success` | int | Uploads completed that day. |
| `retried` | int | Attempts turned away and retried. See `uploads_today`. |

### `completion`

Present for the seven days after a first backup finishes, then `null` forever. It occurs
one time only: the container records the moment on disk, so files that you add later cannot
repeat it.

| Field | Type | Meaning |
|---|---|---|
| `done_at` | int | When the backup first caught up, epoch seconds. |
| `days` | int | How long the first backup took. |
| `total_bytes` | int | The size of the set it worked through. |

### `files`

`null` entirely for a key without `read:files`.

`in_flight` — an array of the files that are uploading now:

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
| `measured` | bool | `false` for a file too small to observe during the transfer: the client named it and continued, so there is no thread, size or rate, and the container infers the completion rather than confirms it. |

`chunk_map` — how far the parts of the large file that is currently being split have got,
or `null` if there is none:

| Field | Type | Meaning |
|---|---|---|
| `file` | string | The file being split. |
| `total` | int | Total chunks. |
| `sent` | array of int | Chunk indices seen to complete. |
| `in_flight` | array of int | Chunk indices that a thread is carrying now. |

Chunks that finished before the container started are in neither array. The container
cannot tell them apart from chunks that have not started.

## Cross-origin requests

A consumer that runs in a browser cannot reach the API from another origin unless you
permit it. Set `API_CORS_ORIGINS` on the container to a list separated by commas:

```
API_CORS_ORIGINS=https://dash.example.com,https://other.example
```

If you leave it unset, which is the default, no cross-origin request succeeds. There is
deliberately no wildcard. A key is still required either way, but with `*` any page the
browser happens to load could poll the container in the background. The answer describes
what is being backed up.

`API_CORS_ORIGINS` covers only `/api/v1/`. The web login authorises the key management
pages, so allowing another origin to call them would hand key creation to any page you have
open.

## Key expiry

A key never expires unless you give it a lifetime. That is the right default for something
long-running, which should not stop working at a date nobody remembers setting.

Put a date on a key that you hand to someone for a single task. An expired key stops
authenticating and stops appearing as active. It also does not keep the API alive on its
own: if it is the only key, the API returns to answering `404`.

## What is recorded

The container writes each successful control action to its log, with the public id of the
key that asked for it. There is therefore a trace of anything that changed the system. The
container does not log the reads, because a consumer polling every few seconds would bury
everything else.

Secrets never appear in a log. For a failed authentication, the container records the key's
public id where it can parse one, and nothing otherwise.

`bb-apikey list` shows when each key was last used, to the nearest minute. It is
deliberately coarse, because recording every request would mean rewriting the key store on
each poll.

## Notes for consumers

The service uses HTTP/1.1 and keeps connections alive. A polling consumer should reuse its
connection rather than open one per request.

Poll no faster than `poll_interval_seconds`. The container refreshes on its own schedule,
and a faster poll returns the same snapshot.

Handle `null` everywhere. Every optional field above is genuinely absent in ordinary
conditions, not only after an error.

Do not parse `state` or `activity.phase` for control flow. Use them for display only. They
are the client's own words, and they can gain new values without a schema change.

Give `build` when you report a problem. It identifies exactly what produced a payload.
