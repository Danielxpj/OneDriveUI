# rclone remote-control (rc) HTTP JSON‑RPC API — v1.75.0

**Authoritative reference for OneDriveUI.** Every JSON body below was captured empirically on this
machine (rclone v1.75.0, linux/amd64, go1.26.5, `/usr/bin/rclone`, remote `onedrive:` = OneDrive
Personal, drive_id `1A2B3C4D5E6F7890`) against a daemon started with:

```sh
rclone rcd --rc-addr 127.0.0.1:5573 --rc-no-auth
```

Where a response is marked *(verbatim)* it is a literal capture. Where the doc text disagrees with the
observed behaviour, the observed behaviour wins and the discrepancy is called out under **Gotcha**.

---

## 1. Transport

### 1.1 Wire format

* **POST only.** `GET`/`HEAD` on an rc path returns **404 `Not Found`** (plain text). `PUT` returns
  **405 Method Not Allowed**. GET is reserved for the file server (`--rc-files` / `--rc-serve` /
  `--rc-web-gui`).
* URL = `http://HOST:PORT/<command/path>`, e.g. `POST /core/stats`. With `--rc-baseurl /rclone` it
  becomes `POST /rclone/core/stats`.
* Three interchangeable ways to pass parameters:

  | Method | Content-Type | Value typing |
  |---|---|---|
  | JSON body (**use this**) | `application/json` | full JSON types (numbers, bools, arrays, objects) |
  | Form body | `application/x-www-form-urlencoded` | **all values become strings** |
  | Query string | any | **all values become strings** |

  ```sh
  curl -X POST 127.0.0.1:5573/rc/noop -H 'Content-Type: application/json' -d '{"a":1,"d":{"e":true}}'
  # -> {"a":1,"d":{"e":true}}
  curl -X POST '127.0.0.1:5573/rc/noop?x=1&y=two'   # -> {"x":"1","y":"two"}   <-- strings!
  curl -X POST  127.0.0.1:5573/rc/noop -d 'a=1&b=hello' # -> {"a":"1","b":"hello"}
  ```

  Query/form params are the only way to pass parameters to `operations/uploadfile`, whose body is the
  multipart payload.

* Every response is JSON. Success = HTTP **200** (or **202** with `Prefer: respond-async`).

### 1.2 Response headers (verbatim)

```
HTTP/1.1 200 OK
Accept-Ranges: bytes
Content-Type: application/json
Server: rclone/v1.75.0
X-Rclone-Jobid: 176
Date: Mon, 31 Aug 2026 03:35:41 GMT
```

`X-Rclone-Jobid` is present on **every** rc response, sync or async. With `--rc-allow-origin` set you
also get `Access-Control-Allow-Origin`, `Access-Control-Allow-Methods`, `Access-Control-Allow-Headers`,
`Access-Control-Max-Age: 86400`.

### 1.3 Error shape and status codes

Errors are JSON with a stable 4-key shape; the request `input` is echoed back:

```json
{
	"error": "job not found",
	"input": { "jobid": 99999 },
	"path": "job/status",
	"status": 500
}
```

| HTTP status | Meaning | Example message |
|---|---|---|
| **400** | required parameter missing / wrong type | `Didn't find key "remote" in input`, `value must be string "recursive"=true` |
| **404** | path/dir/object not found; unknown rc method | `error in ListJSON: directory not found`; `couldn't find method "does/not/exist"` |
| **401** | auth required | body is plain text `401 Unauthorized`, header `Www-Authenticate: Basic realm=""` |
| **405** | wrong HTTP verb | |
| **500** | everything else | `didn't find section in config file ("nosuchremote")` |
| **503** | (produced by the `rclone rc` *client*, not the server) daemon unreachable | `connection failed: Post ...: connect: no such file or directory` |

`"status"` inside the body always equals the HTTP status. Parse the body, don't rely on status alone.

### 1.4 Addresses

* `--rc-addr 127.0.0.1:5573` — TCP. Default when only `--rc` is given: `localhost:5572`.
* `--rc-addr 127.0.0.1:0` — OS-chosen port. The port is only announced on **stderr**:
  `2026/08/30 23:35:52 NOTICE: Serving remote control on http://127.0.0.1:44021/`. Parse it, or use
  `ss -lntp | grep pid=<pid>`.
* `--rc-addr unix:///path/to/rc.sock` — unix domain socket, mode `srwxr-xr-x`, owned by the launching
  user. Client: `curl --unix-socket /path/rc.sock -X POST http://localhost/core/pid -d '{}'`, or
  `rclone rc --unix-socket /path/rc.sock core/version`.
* `--rc-addr` may be **repeated** to listen on several addresses.
* Socket activation: if the service manager passes FDs, rclone listens on those and **ignores
  `--rc-addr`**. Works with systemd `.socket` units — relevant if OneDriveUI ships a user unit.

**Gotcha:** `core/quit` does **not** unlink the unix socket file. A stale `rc.sock` remains on disk after
a clean quit and after a crash. Always `os.unlink()` the socket path before spawning the daemon, and
never treat "socket file exists" as "daemon is alive".

### 1.5 Authentication

| Flag | Effect |
|---|---|
| `--rc-no-auth` | no auth at all (what we used for probing). Also relaxes `--rc-serve` path restrictions. |
| `--rc-user U --rc-pass P` | HTTP Basic, single user |
| `--rc-htpasswd /path` | Apache htpasswd file, MD5/SHA1/**bcrypt** (recommended); re-read live while running |
| `--rc-realm R` | Basic realm string (default `""`) |
| `--rc-salt S` | password hashing salt (default `dlPL2MqE`) |
| `--rc-user-from-header X-Remote-User` | trust a reverse proxy header |
| `--rc-cert/--rc-key/--rc-client-ca/--rc-min-tls-version` | TLS; prefix an `--rc-addr` with `http://` to keep that one listener plaintext |

Observed with `--rc-user gui --rc-pass s3cret`:

```
$ curl -i -X POST 127.0.0.1:5574/core/version -d '{}'
HTTP/1.1 401 Unauthorized
Www-Authenticate: Basic realm=""

$ curl -u gui:s3cret -X POST 127.0.0.1:5574/core/version -d '{}'
{ "arch": "amd64", ... }
```

`rc/noopauth` exists purely to test that auth is enforced. `--rc-no-auth` only exempts commands whose
registration sets `NoAuth: true`; in v1.75.0 **every** command in `rc/list` reports `"NoAuth": false`,
so with auth configured, *everything* needs credentials — including `/metrics`.

> **Security:** rc access == shell access as the rclone user (`core/command` runs arbitrary rclone
> command lines; `config/*` reads the OAuth token). For OneDriveUI: bind to a unix socket under
> `$XDG_RUNTIME_DIR` (mode 0700 dir) **or** `127.0.0.1` with a random per-launch `--rc-user/--rc-pass`.
> Never `--rc-addr :port`.

### 1.6 Other server flags

| Flag | Notes |
|---|---|
| `--rc-serve` | GET on `/` serves an HTML "List of all rclone remotes", and `/[remote:path]/file` streams objects. Unauthenticated requests reject inline remotes and bare local paths unless `--rc-no-auth`. |
| `--rc-serve-no-modtime` | skip modtime reads when serving |
| `--rc-files /dir` | serve a local directory over GET instead |
| `--rc-web-gui` | download+serve the React web GUI on the same port (fetches from `--rc-web-fetch-url`, default the rclone-webui-react GitHub releases API). Also `--rc-web-gui-update`, `--rc-web-gui-force-update`, `--rc-web-gui-no-open-browser`. **We do not want this** — it opens a browser and pulls code off the network. Leave off. |
| `--rc-enable-metrics` | Prometheus/OpenMetrics on `/metrics` on the *same* listener |
| `--rc-allow-origin '*'` | CORS. Logs `NOTICE: Warning: Allow origin set to *.` |
| `--rc-job-expire-duration` | default **1m0s** — see §4.4 |
| `--rc-job-expire-interval` | default **10s** |
| `--rc-server-read-timeout` / `--rc-server-write-timeout` | default **1h0m0s** each; this is the *total* time for a request, so long synchronous jobs are fine up to 1h |
| `--rc-max-header-bytes` | 4096 |
| `--rc-baseurl`, `--rc-template`, `--rc-response-header Name: value` | |

### 1.7 `/metrics`

`--rc-enable-metrics` exposes the Go collector plus exactly 11 rclone gauges/counters (verbatim names):

```
rclone_bytes_transferred_total 0
rclone_checked_files_total 0
rclone_dirs_deleted_total 0
rclone_entries_listed_total 0
rclone_errors_total 0
rclone_fatal_error 0
rclone_files_deleted_total 0
rclone_files_renamed_total 0
rclone_files_transferred_total 1
rclone_retry_error 0
rclone_speed 0
```

These are **global, not per-group**. For the OneDriveUI status UI, `core/stats` is strictly richer —
`/metrics` is only worth enabling if you want an external Prometheus scraper. Note it is behind the
same auth as the rc.

### 1.8 pprof

`/debug/pprof/` is always mounted (200 OK) on the rc listener, even without `--rc-enable-metrics`.
Combine with `debug/set-block-profile-rate` and `debug/set-mutex-profile-fraction`.

---

## 2. Special parameters (`_async`, `_group`, `_config`, `_filter`)

These may be added to **any** rc call. This is the most important section of this document.

### 2.1 `_async: true` — run as a background job

```sh
curl -X POST 127.0.0.1:5573/sync/copy -H 'Content-Type: application/json' -d '{
  "srcFs":"/src", "dstFs":"/dst", "_async": true
}'
```
```json
{ "executeId": "b16db48a-a4b6-4439-ab02-e8b6d66c7022", "jobid": 37 }
```

* `jobid` is a **plain integer**, starting at 1 and incrementing for the life of the process.
* `executeId` is a **UUIDv4 string, fixed for the daemon process**. `(executeId, jobid)` is globally
  unique; a change in `executeId` means the daemon restarted and all your job ids are stale.
* Equivalent to `_async` without touching the body: send header **`Prefer: respond-async`** (RFC 7240).
  Verbatim:

  ```
  HTTP/1.1 202 Accepted
  Preference-Applied: respond-async
  X-Rclone-Jobid: 141

  { "executeId": "b16db48a-...", "jobid": 141 }
  ```

* `_async` works on **every** endpoint, including trivially fast ones such as `operations/list`.
* Without `_async` the HTTP request blocks until the operation completes (up to
  `--rc-server-write-timeout`, default 1h).

### 2.2 `_group: "name"` — stats grouping

Every rc call gets a stats group. Default group name is `job/<jobid>`. Pass `_group` to name it
yourself, then read progress with `core/stats {"group": "..."}`.

```json
{"srcFs":"onedrive:Docs","dstFs":"/home/u/OneDrive/Docs","_async":true,"_group":"sync/Docs"}
```

* Groups are created lazily and survive job completion until `core/stats-delete`.
* `job/stopgroup {"group":"sync/Docs"}` cancels **all** jobs in the group.
* `core/group-list` returns every live group, including the auto-generated `job/N` ones.
* `MaxStatsGroups` (main options) caps this at **1000**; oldest are evicted. Use a small, stable set of
  group names (e.g. one per configured sync folder), not one per file.

### 2.3 `_config: {...}` — per-call global-flag overrides

Keys are the **internal Go field names** as returned by `options/get` → `"main"` block (see §10 for the
full list of 110). Verified: `_config` is genuinely scoped to the one call and does not mutate globals.

```sh
curl -X POST 127.0.0.1:5573/options/local -H 'Content-Type: application/json' -d '{
 "_config":{"Transfers":16,"DryRun":true,"BwLimit":"1M","CheckSum":true,"MaxDepth":2}
}'
```
```json
{ "config": { ..., "DryRun": true, "CheckSum": true, "Transfers": 16,
              "MaxDepth": 2, "BwLimit": "1Mi", ... }, "filter": {...} }
```

**Flat form (equally valid):** put CLI-style names at the top level of the params — drop the `--`,
replace `-` with `_`. Verified equivalent:

```json
{"checksum": true, "transfers": 9, "max_depth": 3, "dry_run": true}
```
→ `CheckSum=True Transfers=9 MaxDepth=3 DryRun=True`. If both flat and nested are given, the nested
`_config` block wins. **Recommendation: always use the explicit nested `_config` with internal names**
— the flat form silently collides with a command's own parameter names.

Value coercion observed:
* `Duration` fields accept `"1h"`, `"30s"`, `0`, or raw nanosecond integers; they read back as **integer
  nanoseconds** (`ConnectTimeout: 60000000000`).
* `SizeSuffix` fields accept `"10M"`, `"1Mi"`, `-1`, or bytes; read back as **bytes** (`MaxSize: 10485760`).
* `LogLevel` accepts either `"DEBUG"`/`"INFO"`/`"NOTICE"` or the numeric level (8 = DEBUG).

**Gotcha — `_config.BwLimit` does not throttle.** `--bwlimit` is a single process-wide token bucket
installed at startup. Setting `BwLimit` inside `_config` is accepted and reflected by `options/local`
but has **no effect on transfer speed** (measured: 8 MiB copied in <3 s with `_config.BwLimit=300k`;
the same copy ran at 1.06 MB/s once `core/bwlimit rate=1M` was set globally). **To throttle, use
`core/bwlimit` (§3.6) — it is global and affects every in-flight job.**

### 2.4 `_filter: {...}` — per-call filter rules

Keys are the internal names from `options/get` → `"filter"` block. **This is the only way to scope a
sync/copy to a subset of files from the rc.** Verified end-to-end: a `sync/copy` carrying
`{"_filter":{"IncludeRule":["*.txt"]}}` copied only `a.txt` and `sub/b.txt` out of a 6-file tree.

Full filter block (defaults on the right), all settable:

```json
{"_filter": {
  "FilterRule":   ["+ *.docx", "- *"],   "FilterFrom":  ["/path/rules.txt"],
  "ExcludeRule":  ["node_modules/**"],   "ExcludeFrom": [],
  "IncludeRule":  ["*.jpg","*.png"],     "IncludeFrom": [],
  "ExcludeFile":  [".rcloneignore"],
  "FilesFrom":    [], "FilesFromRaw": [], "FilesFrom0": [],
  "MetaRules":    {"FilterRule":[],"FilterFrom":[],"ExcludeRule":[],
                   "ExcludeFrom":[],"IncludeRule":[],"IncludeFrom":[]},
  "MinAge":  "1h",      // default 9223372036854775807 (off)
  "MaxAge":  "30d",     // default 9223372036854775807 (off)
  "MinSize": 1024,      // default -1 (off); accepts "1M"
  "MaxSize": "10M",     // default -1 (off)
  "IgnoreCase": true,   // default false
  "DeleteExcluded": true,  // default false
  "HashFilter": ""
}}
```

Read back: `MinAge -> 3600000000000`, `MaxSize -> 10485760`, arrays preserved verbatim.

Flat CLI-style form also works (`{"max_size":"5M","include":["*.doc"],"exclude":["x/**"],
"min_age":"2h","ignore_case":true}` → identical result), with nested taking precedence.

**Verify a call's effective settings** with `options/local` — pass the exact same `_config`/`_filter` you
intend to send and inspect `{"config":…, "filter":…}`. Invaluable when debugging why a sync copied the
wrong set of files.

### 2.5 `_path` — only inside `job/batch`

See §4.5.

---

## 3. `rc/*` and `core/*`

### 3.1 `rc/list` — sync

Returns the full command registry. **101 commands in v1.75.0.** Each entry:

```json
{ "Path": "core/stats", "Title": "Returns stats about current transfers.",
  "Help": "<markdown, often with a full JSON example>",
  "AuthRequired": false, "NeedsRequest": false, "NeedsResponse": false, "NoAuth": false }
```

Only two commands set `NeedsRequest: true` (`core/command`, `operations/uploadfile`); only
`core/command` sets `NeedsResponse: true`. Use `rc/list` at startup to feature-detect the daemon.

Full path list (v1.75.0):

```
backend/command
config/create config/delete config/dump config/get config/listremotes config/oauthstatus
config/oauthstop config/password config/paths config/providers config/setpath config/unlock
config/unset config/update
core/bwlimit core/command core/disks core/du core/gc core/group-list core/memstats core/obscure
core/pid core/quit core/stats core/stats-delete core/stats-reset core/transferred core/version
debug/set-block-profile-rate debug/set-gc-percent debug/set-mutex-profile-fraction
debug/set-soft-memory-limit
fscache/clear fscache/entries
job/batch job/list job/status job/stop job/stopgroup
mount/listmounts mount/mount mount/types mount/unmount mount/unmountall
operations/about operations/check operations/cleanup operations/copyfile operations/copyurl
operations/delete operations/deletefile operations/fsinfo operations/hashsum operations/hashsumfile
operations/list operations/mkdir operations/movefile operations/publiclink operations/purge
operations/rmdir operations/rmdirs operations/settier operations/settierfile operations/size
operations/stat operations/uploadfile
options/blocks options/get options/info options/local options/set
pluginsctl/addPlugin pluginsctl/getPluginsForType pluginsctl/listPlugins pluginsctl/listTestPlugins
pluginsctl/removePlugin pluginsctl/removeTestPlugin
rc/error rc/fatal rc/list rc/noop rc/noopauth rc/panic
serve/list serve/start serve/stop serve/stopall serve/types
sync/bisync sync/copy sync/move sync/sync
vfs/forget vfs/list vfs/poll-interval vfs/queue vfs/queue-set-expiry vfs/refresh vfs/stats
```

### 3.2 `rc/noop`, `rc/noopauth`, `rc/error`, `rc/fatal`, `rc/panic` — sync

`rc/noop` echoes the input verbatim; the canonical **liveness probe**.
`rc/error`/`rc/fatal`/`rc/panic` deliberately fail — for testing your error path. Never call `rc/panic`
against the production daemon.

### 3.3 `core/version` — sync (verbatim)

```json
{
  "arch": "amd64",
  "decomposed": [1, 75, 0],
  "goTags": "none",
  "goVersion": "go1.26.5-X:nodwarf5",
  "isBeta": false,
  "isGit": false,
  "linking": "dynamic",
  "os": "linux",
  "osArch": "amd64",
  "osKernel": "6.18.42-1-cachyos-lts (x86_64)",
  "osVersion": "cachyos (64 bit)",
  "version": "v1.75.0"
}
```

Gate features on `decomposed >= [1,75,0]`, not on the string.

### 3.4 `core/pid` — sync

```json
{ "pid": 8818 }
```

### 3.5 `core/quit` — sync. Params: `exitCode` (int, optional).

Returns `{}` **then** exits. The HTTP response does arrive before shutdown. A unix socket file is left
behind (§1.4). After this, `job/list` etc. fail with connection refused.

### 3.6 `core/bwlimit` — sync. **The only working runtime throttle.**

Params: `rate` (string, optional — omit to query).

Format: a single limit, or `upload:download`. Suffixes `B K M G T P` (binary), `off`/`0` = unlimited.

| Request | Response (verbatim) |
|---|---|
| `{}` (query, unlimited) | `{"bytesPerSecond":-1,"bytesPerSecondRx":-1,"bytesPerSecondTx":-1,"rate":"off"}` |
| `{"rate":"off"}` | same as above |
| `{"rate":"1M:100k"}` | `{"bytesPerSecond":1048576,"bytesPerSecondRx":102400,"bytesPerSecondTx":1048576,"rate":"1Mi:100Ki"}` |
| `{}` after that | identical (the setting persists) |

* **`Tx` = upload, `Rx` = download.** With a single rate both are set to it.
* `bytesPerSecond` mirrors `bytesPerSecondTx`.
* The echoed `rate` is normalised to binary units (`1M` → `1Mi`), so don't string-compare against what
  you sent.
* Note the docs' example for `1M:100k` shows `"rate":"1M"`; the real v1.75.0 returns the full
  `"1Mi:100Ki"`.

OneDriveUI's Windows-parity "Limit upload rate / Limit download rate" settings map directly onto
`core/bwlimit {"rate": "<up>:<down>"}`, applied live with no restart.

### 3.7 `core/stats` — sync. **The progress engine.**

Params: `group` (string, optional — omit for the sum of all groups), `short` (bool, optional).

Idle response (verbatim; note `transferring`, `checking`, `lastError` are **absent** when empty):

```json
{
  "bytes": 0, "checks": 0, "deletedDirs": 0, "deletes": 0,
  "elapsedTime": 7.925e-06, "errors": 0, "eta": null, "fatalError": false,
  "listed": 0, "renames": 0, "retryError": false,
  "serverSideCopies": 0, "serverSideCopyBytes": 0,
  "serverSideMoveBytes": 0, "serverSideMoves": 0,
  "speed": 0, "totalBytes": 0, "totalChecks": 0, "totalTransfers": 0,
  "transferTime": 0, "transfers": 0
}
```

Active response, `{"group":"bigjob"}` (verbatim, mid-transfer at 1 MiB/s):

```json
{
  "bytes": 5332998,
  "checks": 0, "deletedDirs": 0, "deletes": 0,
  "elapsedTime": 5.03430132,
  "errors": 0,
  "eta": 42,
  "fatalError": false,
  "listed": 6,
  "renames": 0,
  "retryError": false,
  "serverSideCopies": 0, "serverSideCopyBytes": 0,
  "serverSideMoveBytes": 0, "serverSideMoves": 0,
  "speed": 1060029.116061751,
  "totalBytes": 50331660,
  "totalChecks": 0,
  "totalTransfers": 5,
  "transferTime": 5.034144617,
  "transferring": [
    {
      "bytes": 2682880,
      "dstFs": "/tmp/.../dst3",
      "eta": 10,
      "group": "bigjob",
      "name": "big.bin",
      "percentage": 31,
      "size": 8388608,
      "speed": 533074.320824698,
      "speedAvg": 536569.9085890673,
      "srcFs": "/tmp/.../t"
    },
    { "...second concurrent transfer..." }
  ],
  "transfers": 1
}
```

Field reference:

| Field | Type | Meaning |
|---|---|---|
| `bytes` | int | bytes transferred so far in this group |
| `totalBytes` | int | expected total bytes for the group |
| `checks` / `totalChecks` | int | files checked / expected to check |
| `transfers` / `totalTransfers` | int | files **completed** / expected |
| `deletes`, `deletedDirs`, `renames`, `listed` | int | counters |
| `errors` | int | error count |
| `lastError` | string | **only present when `errors > 0`** — e.g. `"context canceled"` |
| `fatalError` | bool | at least one non-retryable fatal error |
| `retryError` | bool | at least one retryable error |
| `eta` | int seconds **or `null`** | `null` when indeterminate — always null-check |
| `speed` | float B/s | group average since start |
| `elapsedTime` | float s | wall clock since process start (**not** since group start) |
| `transferTime` | float s | seconds spent actually running jobs |
| `serverSideCopies` / `serverSideCopyBytes` / `serverSideMoves` / `serverSideMoveBytes` | int | server-side ops (OneDrive→OneDrive copy/move) |
| `transferring` | array of objects | **absent when empty**, absent when `short:true` |
| `checking` | array of **plain strings** | **absent when empty**; e.g. `["f00114.txt"]` |

`transferring[]` entry:

| Field | Type | Notes |
|---|---|---|
| `name` | string | path relative to the fs |
| `size` | int | total bytes (may be `-1` if unknown) |
| `bytes` | int | bytes done |
| `percentage` | int | 0–100, integer |
| `speed` | float | **instantaneous** B/s for this file |
| `speedAvg` | float | exponentially-weighted moving average B/s |
| `eta` | int or null | seconds |
| `group` | string | the stats group |
| `srcFs`, `dstFs` | string | undocumented but always present — great for the UI's "from → to" line |

**Gotchas**
* `checking` is an **array of strings**, `transferring` an **array of objects**. Don't assume symmetry.
* Absent keys mean empty. Use `d.get("transferring", [])`.
* Global `core/stats` (no `group`) sums *all* groups over the process lifetime, so `bytes` keeps
  growing across unrelated jobs. **Always pass `group` for per-sync UI.**
* `short: true` drops `transferring` and `checking` — use it for a cheap 1 Hz global poll.

### 3.8 `core/stats-reset` / `core/stats-delete` — sync

`core/stats-reset {"group": "..."}` clears counters/errors/finished transfers (omit `group` → all
groups). `core/stats-delete {"group": "..."}` removes the group entirely (it disappears from
`core/group-list`). Both return `{}`.

### 3.9 `core/transferred` — sync. Params: `group` (optional).

Last **100** completed transfers (successes *and* failures). Verbatim:

```json
{ "transferred": [
  { "error": "", "name": "a.txt", "size": 6, "bytes": 6, "checked": false,
    "what": "transferring",
    "started_at":   "2026-08-30T23:26:05.895290105-04:00",
    "completed_at": "2026-08-30T23:26:05.895521017-04:00",
    "group": "bigjob",
    "srcFs": "/tmp/.../t", "dstFs": "/tmp/.../dst3" },
  { "error": "context canceled", "name": "big.bin", "size": 8388608, "bytes": 7008256,
    "checked": false, "what": "transferring",
    "started_at": "...", "completed_at": "...", "group": "bigjob",
    "srcFs": "...", "dstFs": "..." }
]}
```

`what` ∈ `transferring | deleting | checking | importing | hashing | merging | listing | moving |
renaming`.

**Gotcha — the built-in help is stale.** It documents `"timestamp"` (ms epoch int) and `"jobid"`.
v1.75.0 actually returns **`started_at` / `completed_at`** as RFC3339 strings plus `group`, `srcFs`,
`dstFs`, and **no `jobid`**. Code against the observed shape.

This is the data source for OneDriveUI's "Recent activity" list. Poll it after each job and merge into
your own persistent history — only 100 entries are retained and they are lost on daemon restart.

### 3.10 `core/group-list` — sync

`{"groups": ["job/30","job/31","upload-1","slowjob","bigjob"]}`. **Returns `{"groups": null}` (JSON
null, not `[]`) when there are no groups.** Null-check.

### 3.11 `core/memstats` — sync

Go `runtime.MemStats` subset (verbatim keys): `Alloc, BuckHashSys, Frees, GCSys, HeapAlloc, HeapIdle,
HeapInuse, HeapObjects, HeapReleased, HeapSys, MCacheInuse, MCacheSys, MSpanInuse, MSpanSys, Mallocs,
OtherSys, StackInuse, StackSys, Sys, TotalAlloc`. `HeapAlloc` ≈ live memory; `Sys` ≈ requested from OS.

### 3.12 `core/obscure` — sync

`{"clear":"hunter2"}` → `{"obscured":"URnbSiLBL00tUaMC1aZyo15gHBx7wEQ"}`. Output is **not
deterministic** (random nonce). Needed before writing a password into `config/create`/`config/update`
unless you pass `opt.obscure`.

### 3.13 `core/du` / `core/disks` — sync

```json
// core/du {"dir":"/home"}   (dir optional, defaults to --cache-dir)
{ "dir": "/home", "info": { "Free": 108777537536, "Available": 106200920064, "Total": 263064326144 } }

// core/disks {}
{ "disks": ["/home/user","/","/mnt/Bulk","/home/user/Desktop",
            "/home/user/Downloads","/home/user/Documents",
            "/home/user/Music","/home/user/Pictures","/home/user/Videos"] }
```

`core/disks` returns XDG user dirs + mounted volumes as ready-to-use local remote paths — exactly what
a "choose a folder to sync" picker needs as shortcuts.

### 3.14 `core/gc` — sync. `{}` → `{}`. Forces a Go GC.

### 3.15 `core/command` — sync or streaming. `NeedsRequest`+`NeedsResponse`.

```json
{ "command": "ls", "arg": ["onedrive:"], "opt": {"max-depth": "1"},
  "returnType": "COMBINED_OUTPUT" }
```
`returnType` ∈ `COMBINED_OUTPUT` (default; text in `result`), `STREAM`, `STREAM_ONLY_STDOUT`,
`STREAM_ONLY_STDERR` (raw text streamed into the HTTP body). Returns
`{"error": false, "result": "<raw output>", "returnType": "..."}`.

**Avoid this in OneDriveUI** — it is an arbitrary-command escape hatch. Everything we need has a real
endpoint.

---

## 4. Jobs

### 4.1 Every rc call is a job

Confirmed empirically: a fresh daemon that had served 21 curl requests reported `jobids` 1..22 from
`job/list` — including the `job/list` call itself, which appeared in `runningIds`. Synchronous calls
create and immediately finish a job whose `output` is the response body.

### 4.2 `job/list` — sync (verbatim)

```json
{
  "executeId": "b16db48a-a4b6-4439-ab02-e8b6d66c7022",
  "jobids":      [7,12,13,14,15,18,22,1,2,5,6,8,11,16,17,9,10,20,3,4,19,21],
  "runningIds":  [22],
  "finishedIds": [19,21,7,12,13,14,15,18,1,2,5,6,8,11,16,17,9,10,20,3,4]
}
```

**The arrays are unordered** (Go map iteration). Sort them yourself. `jobids` = running ∪ finished.

### 4.3 `job/status` — sync. Params: `jobid` (int).

Running job:
```json
{ "duration": 0, "endTime": "0001-01-01T00:00:00Z", "error": "",
  "executeId": "b16db48a-...", "finished": false, "group": "bigjob",
  "id": 49, "output": null, "startTime": "2026-08-30T23:26:05.894673612-04:00",
  "success": false }
```

Finished OK (`operations/list` run with `_async`):
```json
{ "duration": 0.000496555, "endTime": "2026-08-30T23:34:10.355022258-04:00",
  "error": "", "executeId": "b16db48a-...", "finished": true, "group": "job/174",
  "id": 174,
  "output": { "list": [ { "IsDir": false, "MimeType": "text/plain; charset=utf-8",
                          "ModTime": "...", "Name": "a.txt", "Path": "a.txt", "Size": 6 } ] },
  "startTime": "...", "success": true }
```

Cancelled by `job/stop`:
```json
{ "duration": 13.334482387, "endTime": "...", "error": "context canceled",
  "executeId": "...", "finished": true, "group": "bigjob", "id": 49,
  "output": {}, "startTime": "...", "success": false }
```

* `output` is exactly what the synchronous call would have returned (`{}` for `sync/copy`).
* `endTime` for a running job is the Go zero time `"0001-01-01T00:00:00Z"` — check `finished`, not
  `endTime`.
* **Gotcha:** the built-in help lists a `progress` field. **It is not emitted in v1.75.0.** Get progress
  from `core/stats` keyed by `_group`, not from `job/status`.
* Unknown id → **HTTP 500 `{"error":"job not found", ...}`**.

### 4.4 Job expiry — **critical**

Finished jobs are garbage-collected after `--rc-job-expire-duration` (**default 60 s**, swept every
`--rc-job-expire-interval` = 10 s). Verified: `job/status` on a job that finished 75 s earlier returned
500 `job not found`, and `job/list` had shrunk from ~180 ids to 2.

**Implications for OneDriveUI**
1. Poll `job/status` at ≤ 5 s intervals while a job is outstanding, and **persist `output`/`error` the
   moment `finished == true`** — do not go back for it later.
2. If a poll returns `job not found`, the job either expired (it *did* finish; outcome unknown) or the
   daemon restarted (check `executeId`). Treat "unknown outcome" distinctly from "failed".
3. Consider raising `--rc-job-expire-duration 10m` on our daemon for slack.

### 4.5 `job/batch` — sync. Params: `concurrency` (int, defaults to `--transfers`), `inputs` (array).

Each input is a normal rc parameter map plus `"_path": "<rc/path>"`, and may itself carry `_async`,
`_group`, `_config`, `_filter`.

```sh
curl -X POST 127.0.0.1:5573/job/batch -H 'Content-Type: application/json' -d '{
 "concurrency":2,
 "inputs":[
   {"_path":"rc/noop","parameter":"OK"},
   {"_path":"rc/error","parameter":"BAD"},
   {"_path":"operations/stat","fs":"/tmp/t","remote":"a.txt"}
 ]}'
```
```json
{ "results": [
  { "parameter": "OK" },
  { "error": "arbitrary error on input map[parameter:BAD]",
    "input": { "_group": "job/133", "parameter": "BAD" },
    "path": "rc/error", "status": 500 },
  { "item": { "Path": "a.txt", "Name": "a.txt", "Size": 6,
              "MimeType": "text/plain; charset=utf-8", "ModTime": "...", "IsDir": false } }
]}
```

`results` is **positionally aligned** with `inputs`; a per-item failure yields the standard error
object in that slot and does **not** fail the batch (outer HTTP status stays 200). Note rclone injects
`_group: "job/<batch-jobid>"` into each input. Excellent for the file-browser: one round trip to
`operations/list` many directories.

### 4.6 `job/stop` — sync. Params: `jobid` (int). Returns `{}`.

Cancels the job's context. In-flight transfers abort with `error: "context canceled"`, partial files
are recorded in `core/transferred` with that error, and `core/stats` shows `errors: 3,
lastError: "context canceled"`. Stopping an already-finished job is a no-op returning `{}`.

### 4.7 `job/stopgroup` — sync. Params: `group` (string). Returns `{}`.

Cancels every running job whose `_group` matches. This is the "Pause sync" button.

---

## 5. `operations/*` (all synchronous unless you add `_async`)

### 5.1 Path semantics — read this first

`fs` is a remote (`"onedrive:"`, `"onedrive:Documents"`, `"/home/u/OneDrive"`, or a connection string
like `":local,nounc:/tmp"`). `remote` is a path **inside** `fs`.

**In the results, `Path` is relative to `fs`, not to `fs`+`remote`:**

| Call | Returned `Path` |
|---|---|
| `{"fs":"/tmp/t","remote":""}` | `a.txt`, `sub` |
| `{"fs":"/tmp/t","remote":"","opt":{"recurse":true}}` | `a.txt`, `sub`, `sub/b.txt` |
| `{"fs":"/tmp/t","remote":"sub"}` | **`sub/b.txt`** |
| `{"fs":"/tmp/t/sub","remote":""}` | `b.txt` |
| `{"fs":"onedrive:","remote":"OneDriveUI-rctest"}` | **`OneDriveUI-rctest/a.txt`** |

`Name` is always the leaf. **Build UI rows from `Name`; build the next request's `remote` by joining
your own current path.** Never assume `Path` is relative to `remote`.

### 5.2 `operations/list` — the workhorse

Params: `fs`, `remote`, `opt` (object, optional).

`opt` keys (all boolean unless noted):

| Key | Effect |
|---|---|
| `recurse` | walk the whole subtree |
| `noModTime` | skip modtime lookup (`ModTime` returned as `""`) |
| `noMimeType` | omit `MimeType` |
| `showEncrypted` | show decrypted names on crypt remotes |
| `showOrigIDs` | include backend IDs |
| `showHash` | include a `Hashes` object |
| `hashTypes` | **array of strings**, e.g. `["md5"]`, `["quickxor"]`; only with `showHash` |
| `dirsOnly` / `filesOnly` | filter entry kind |
| `metadata` | include a `Metadata` object |

Local example (verbatim, `recurse+showHash+metadata`):

```json
{ "list": [
  { "Path": "a.txt", "Name": "a.txt", "Size": 6,
    "MimeType": "text/plain; charset=utf-8",
    "ModTime": "2026-08-30T23:25:08.693845105-04:00", "IsDir": false,
    "Hashes": { "md5": "b1946ac92492d2347c6235b4d2611184" },
    "Metadata": { "atime":"...","btime":"...","gid":"1000","mode":"100644",
                  "mtime":"...","uid":"1000" } },
  { "Path": "sub", "Name": "sub", "Size": 60, "MimeType": "inode/directory",
    "ModTime": "...", "IsDir": true, "Metadata": {"mode":"40755", ...} }
]}
```

OneDrive example (verbatim — note `ID`, `Size: -1` for directories, and the rich `Metadata`):

```json
{ "list": [
  { "Path": "AFC", "Name": "AFC", "Size": -1, "MimeType": "inode/directory",
    "ModTime": "2023-03-27T18:41:03Z", "IsDir": true,
    "ID": "1A2B3C4D5E6F7890#1A2B3C4D5E6F7890!4252",
    "Metadata": {
      "btime": "2023-03-27T18:41:02Z",
      "content-type": "inode/directory",
      "created-by-display-name": "Daniel Dughman Manzur",
      "created-by-id": "1A2B3C4D5E6F7890",
      "id": "1A2B3C4D5E6F7890#1A2B3C4D5E6F7890!4252",
      "last-modified-by-display-name": "Daniel Dughman Manzur",
      "last-modified-by-id": "1A2B3C4D5E6F7890",
      "malware-detected": "false",
      "mtime": "2023-03-27T18:41:02Z",
      "utime": "2023-03-27T18:41:02Z" } },
  { "Path": "photo.jpg", "Name": "photo.jpg", "Size": 143324, "MimeType": "image/jpeg",
    "ModTime": "2022-09-27T12:29:49Z", "IsDir": false,
    "ID": "1A2B3C4D5E6F7890#1A2B3C4D5E6F7890!4122",
    "Metadata": { "...same shape..." } }
]}
```

Notes for the OneDriveUI file browser:
* **Directories on OneDrive report `Size: -1`.** Render blank, not "-1 bytes".
* `Metadata` gives you `created-by-display-name` / `last-modified-by-display-name` — exactly the
  "Sharing" / "Modified by" columns the Windows client shows. It costs no extra API call.
* `malware-detected` maps to the Windows "blocked file" state.
* `MimeType` drives the icon; `inode/directory` for folders.
* `ModTime` is RFC3339 with timezone. Local is nanosecond precision (`Precision: 1`), OneDrive is
  second precision (`Precision: 1000000000`) — never compare exact equality across the two.
* Non-existent dir → **HTTP 404** `{"error":"error in ListJSON: directory not found", ...}`.

### 5.3 `operations/stat`

Params: `fs`, `remote`, `opt` (same as `operations/list`).

```json
{ "item": { "Path": "a.txt", "Name": "a.txt", "Size": 6,
            "MimeType": "text/plain; charset=utf-8",
            "ModTime": "2026-08-30T23:25:08.693845105-04:00", "IsDir": false } }
```
Not found returns **HTTP 200** with `{"item": null}` — not an error. Set `opt.filesOnly` when you only
care about files; it is much faster.

### 5.4 `operations/about`

Params: `fs`. OneDrive (verbatim): `{"free":852336259891,"total":1104880336896,"trashed":0,
"used":252544077005}`. Local: `{"free":…,"total":…,"used":…}` (no `trashed`). Keys are optional per
backend — always `.get()`. This drives the Windows-style storage bar.

### 5.5 `operations/fsinfo`

Params: `fs`. Returns `Name`, `Root`, `String`, `Precision` (ns), `Hashes[]`, `Features{}` (52 bools),
`MetadataInfo{}`.

`onedrive:` (verbatim highlights): `Name "onedrive"`, `Root ""`, `Precision 1000000000`,
`String "OneDrive root ''"`, `Hashes ["quickxor"]`, and true features:

```
About, CanHaveEmptyDirectories, CaseInsensitive, ChangeNotify, CleanUp, Copy, DirCacheFlush,
DirMove, DirSetModTime, ListP, MkdirMetadata, Move, PublicLink, Purge, ReadDirMetadata,
ReadMetadata, ReadMimeType, Shutdown, WriteDirMetadata, WriteDirSetModTime, WriteMetadata
```

Everything else is false — notably **`ListR: false`** (no fast recursive list; `--fast-list` is a
no-op), **`SetTier: false`**, **`Command: false`** (see §9), `IsLocal: false`, `BucketBased: false`.

**Call `operations/fsinfo` once at startup and drive the UI off it**: `PublicLink` gates the "Share"
menu item, `CleanUp` gates "Empty recycle bin", `ChangeNotify` gates live-refresh, `CaseInsensitive`
gates the rename-collision warning, `Hashes` tells you which hash column to show (`quickxor`).

### 5.6 Directory and file mutations

| Endpoint | Params | Result |
|---|---|---|
| `operations/mkdir` | `fs`, `remote` | `{}` |
| `operations/rmdir` | `fs`, `remote` | `{}` — empty dir only |
| `operations/rmdirs` | `fs`, `remote`, `leaveRoot` (bool) | `{}` — all empty dirs under path |
| `operations/purge` | `fs`, `remote` | `{}` — dir **and all contents** |
| `operations/delete` | `fs` (+`_filter`) | `{}` — deletes files in the fs, honours filters |
| `operations/deletefile` | `fs`, `remote` | `{}` |
| `operations/cleanup` | `fs` | empties the backend trash (OneDrive: `CleanUp: true`) |
| `operations/copyfile` | `srcFs`,`srcRemote`,`dstFs`,`dstRemote` | `{}` |
| `operations/movefile` | `srcFs`,`srcRemote`,`dstFs`,`dstRemote` | `{}` |
| `operations/copyurl` | `fs`,`remote`,`url`,`autoFilename` (bool) | `{}` |
| `operations/settier` | `fs` | **OneDrive: 500 `remote onedrive does not support settier`** |
| `operations/settierfile` | `fs`,`remote` | same |

All verified against `onedrive:` (mkdir → uploadfile → copyfile → movefile → deletefile → purge, all
`{}`). Rename = `operations/movefile` with the same `srcFs`/`dstFs`. On OneDrive this is server-side
(`Move: true`) and instant.

`settier` is dead on OneDrive — hide any "storage class" UI when `Features.SetTier == false`.

### 5.7 `operations/publiclink`

Params: `fs`, `remote`, `unlink` (bool, optional), `expire` (string duration, optional).

```json
{ "url": "https://1drv.ms/t/c/1a2b3c4d5e6f7890/REDACTED-SHARE-TOKEN" }
```

**Gotcha:** on OneDrive, `expire` and `unlink:true` both returned **the same URL** with HTTP 200 —
`unlink` did not error and did not obviously revoke. Do not present "Remove link" as reliable; treat
`publiclink` as create-or-fetch only. This is the "Share → Copy link" action.

### 5.8 `operations/size`

Params: `fs` (may include a path: `"onedrive:Documents"`). → `{"bytes":8388620,"count":3,"sizeless":0}`.
Walks the whole tree — **slow on OneDrive** (no `ListR`). Run it with `_async` and a `_group`, and show
`core/stats.listed` as progress.

### 5.9 `operations/check`

Params: `srcFs`, `dstFs`, `download` (bool), `oneWay` (bool), `combined` (bool, default false),
`missingOnSrc` (default true), `missingOnDst` (default true), `match` (default false),
`differ` (default true), `error` (default true), `checkFileHash`/`checkFileFs`/`checkFileRemote` (SUM
file mode).

```json
{ "success": false, "status": "6 differences found", "hashType": "md5",
  "combined": ["+ big.bin","+ sub/b.txt","= a.txt"],
  "match": ["a.txt"], "differ": [], "error": [],
  "missingOnDst": ["big.bin","huge1.bin","sub/b.txt"], "missingOnSrc": [] }
```
`combined` prefixes: `=` match, `+` missing on dst, `-` missing on src, `*` differ, `!` error.
This is the engine for a Windows-style "sync status / what's out of date" panel. Run `_async`.

### 5.10 `operations/hashsum` / `operations/hashsumfile`

`{fs, hashType, download, base64}` → `{"hashType":"md5","hashsum":["<hash>  <name>", ...]}`
`{fs, remote, hashType, download, base64}` → `{"hashType":"md5","hash":"..."}`
On OneDrive `hashType` must be `"quickxor"` (the only supported hash).

### 5.11 `operations/uploadfile` — multipart/form-data. `NeedsRequest: true`.

Parameters **must go in the query string** (the body is the multipart payload):

```sh
curl -X POST "127.0.0.1:5573/operations/uploadfile?fs=onedrive:&remote=Docs" \
     -F "file=@/home/u/a.txt"
# HTTP/1.1 200 OK, body: {}
```

* Multiple parts in one request are supported and each becomes a file:
  `-F "f1=@a.txt" -F "f2=@one.txt"` created **`up/a.txt`** and **`up/one.txt`**.
* **The destination filename comes from the part's `filename=` attribute, not the field name.**
* Response is `{}` — no per-file result, no size confirmation. Verify with `operations/stat`.
* Progress is visible through `core/stats` if you attach `_group` (as a query param).
* For anything large or for whole trees, prefer `sync/copy` — it gives you a job id, retries,
  multi-threaded upload, and real progress.

---

## 6. `sync/*`

All four are synchronous by default and **should always be run with `_async: true` + `_group`** from a
GUI. The async response is `{"jobid": N, "executeId": "..."}`; the eventual `job/status.output` is
`{}` for copy/move/sync and an object for bisync.

### 6.1 `sync/copy` / `sync/move` / `sync/sync`

| Param | Type | copy | move | sync |
|---|---|---|---|---|
| `srcFs` | string (required) | ✓ | ✓ | ✓ |
| `dstFs` | string (required) | ✓ | ✓ | ✓ |
| `createEmptySrcDirs` | bool | ✓ | ✓ | ✓ |
| `deleteEmptySrcDirs` | bool | | ✓ | |

That is the **entire** documented parameter set. **Everything else — dry-run, checksum comparison,
transfers, backup-dir, max-delete, track-renames, bandwidth, ordering, include/exclude — is passed via
`_config` and `_filter`.** This is the single most important fact about driving syncs from the rc.

```json
{
  "srcFs": "/home/user/OneDrive",
  "dstFs": "onedrive:",
  "createEmptySrcDirs": true,
  "_async": true,
  "_group": "sync/OneDrive",
  "_config": {
    "Transfers": 4, "Checkers": 8,
    "CheckSum": false, "UpdateOlder": true,
    "TrackRenames": true, "TrackRenamesStrategy": "hash",
    "MaxDelete": 100,
    "BackupDir": "onedrive:.onedriveui-trash",
    "Inplace": false, "PartialSuffix": ".partial",
    "MultiThreadStreams": 4, "MultiThreadCutoff": 268435456,
    "Retries": 3, "LowLevelRetries": 10,
    "OrderBy": "modtime,mixed",
    "DryRun": false
  },
  "_filter": {
    "ExcludeRule": ["**/.git/**", "*.tmp", "~$*", ".DS_Store", "desktop.ini"],
    "MaxSize": "250G"
  }
}
```

Semantics: `sync/copy` = additive; `sync/sync` = make `dstFs` identical to `srcFs` (**deletes** on the
destination — always set `MaxDelete` and/or `BackupDir`); `sync/move` = copy then delete source.

`operations/copyfile`/`movefile` are the single-file equivalents and are far cheaper for one file.

### 6.2 `sync/bisync` — true two-way sync

Synchronous by default; returns immediately-usable session paths. Verbatim (resync run):

```json
{
  "basePath": "/home/user/.cache/rclone/bisync/tmp_..._b1..tmp_..._b2",
  "listing1": "/home/user/.cache/rclone/bisync/<session>.path1.lst",
  "listing2": "/home/user/.cache/rclone/bisync/<session>.path2.lst",
  "logFile":  "",
  "output":   "",
  "session":  "tmp_..._b1..tmp_..._b2",
  "workDir":  "/home/user/.cache/rclone/bisync"
}
```

Full parameter list (from `rc/list`, v1.75.0):

| Param | Type | Default | Meaning |
|---|---|---|---|
| `path1` | string **required** | | e.g. `/home/u/OneDrive` |
| `path2` | string **required** | | e.g. `onedrive:` |
| `resync` | bool | false | first-run / recovery; equivalent to `resyncMode: path1` |
| `resyncMode` | string | `path1` if `resync` else `none` | `path1\|path2\|newer\|older\|larger\|smaller` |
| `dryRun` | bool | false | |
| `checkAccess` | bool | false | require `RCLONE_TEST` marker files on both sides |
| `checkFilename` | string | `RCLONE_TEST` | |
| `checkSync` | string | `true` | `true\|false\|only` — compare final listings |
| `compare` | string | `size,modtime` | comma list of `size,modtime,checksum` |
| `conflictResolve` | string | `none` | `none\|path1\|path2\|newer\|older\|larger\|smaller` |
| `conflictLoser` | string | `num` | `num\|pathname\|delete` |
| `conflictSuffix` | string | `conflict` | one string, or two comma-separated for path1/path2 |
| `createEmptySrcDirs` | bool | false | |
| `removeEmptyDirs` | bool | false | |
| `backupDir1` / `backupDir2` | string | | must be non-overlapping, same remote |
| `filtersFile` | string | | filter rules file (bisync hashes this; changing it forces a resync) |
| `force` | bool | false | bypass `--max-delete` safety |
| `ignoreListingChecksum` | bool | false | |
| `downloadHash` | bool | false | compute hashes by downloading (slow) |
| `noSlowHash` | bool | false | |
| `slowHashSyncOnly` | bool | false | |
| `maxLock` | Duration | `0` (never expire) | **minimum `2m`** |
| `recover` | bool | false | auto-recover from interruption without `--resync` |
| `resilient` | bool | false | retry after less-serious errors |
| `noCleanup` | bool | false | keep working files |
| `workdir` | string | `~/.cache/rclone/bisync` | |

For OneDriveUI, bisync is the closest analogue to the real OneDrive client's two-way folder sync.
Recommended baseline: `resync: true` on first setup only, then
`{"conflictResolve":"newer","conflictLoser":"pathname","conflictSuffix":"onedriveui-conflict",
"resilient":true,"recover":true,"maxLock":"15m","createEmptySrcDirs":true,"compare":"size,modtime"}`
plus `_config.MaxDelete` and `_async: true`.

**Gotchas:** bisync takes a lock in `workdir`; a crashed run leaves it and the next run refuses until
`maxLock` expires (hence set `maxLock`, minimum 2m). Bisync state is keyed by the `session` string
derived from both paths — moving the local folder invalidates it and forces a resync.

---

## 7. `vfs/*` — the mounted-filesystem control surface

All synchronous. All take an optional **`fs`** naming a *VFS instance* (not a remote): if exactly one
VFS is active it may be omitted; otherwise it is required.

### 7.1 `vfs/list`

```json
{ "vfses": ["onedrive:"] }
```

Empty daemon: `{"vfses": []}`.

**Severe gotcha — duplicate VFSes are unaddressable.** Mounting the same `fs` twice with *different*
`vfsOpt` creates a second VFS and `vfs/list` starts returning disambiguated names:

```json
{ "vfses": ["/tmp/t[0]", "/tmp/t[1]"] }
```

but **neither the suffixed nor the bare name is accepted** by the other `vfs/*` calls:

```
fs="/tmp/t[0]" -> 500 {"error":"no VFS found with name \"/tmp/t[0]\""}
fs="/tmp/t"    -> 500 {"error":"more than one VFS active with name \"/tmp/t\""}
```

`fscache/clear` does **not** remove them, and `mount/unmount` does not either — the VFS objects persist
for the daemon's lifetime. **Rule for OneDriveUI: create exactly one VFS per remote per daemon, always
with byte-identical `vfsOpt`.** If you must change VFS options, restart the daemon.

### 7.2 `vfs/stats`

```json
{
  "diskCache": {                                    // present only when vfs_cache_mode > off
    "bytesUsed": 0, "erroredFiles": 0, "files": 1,
    "hashType": 4096, "outOfSpace": false,
    "path":     "/home/user/.cache/rclone/vfs/onedrive",
    "pathMeta": "/home/user/.cache/rclone/vfsMeta/onedrive",
    "uploadsInProgress": 0, "uploadsQueued": 0
  },
  "fs": "onedrive:",
  "inUse": 1,
  "metadataCache": { "dirs": 39, "files": 63 },
  "opt": { "CacheMode": 3, "CacheMaxAge": 3600000000000, ... }   // full VFS option block
}
```

`uploadsInProgress` + `uploadsQueued` + `erroredFiles` are the raw material for the OneDrive tray-icon
state machine (syncing / up-to-date / error). `outOfSpace` maps to the "storage full" badge.
`hashType` is a bitmask, not a name (4096 = quickxor on this build).

### 7.3 `vfs/refresh`

Params: `fs`, `recursive` (**string** `"true"`, see gotcha), and any number of keys **starting with
`dir`** whose values are paths.

```sh
# refresh the root
curl -X POST .../vfs/refresh -d '{}'                         # -> {"result":{"":"OK"}}
# refresh several dirs
curl -X POST .../vfs/refresh -d '{"dir":"sub","dir2":"Docs"}'
# -> {"result":{"sub":"OK","Docs":"OK"}}
# whole tree
curl -X POST .../vfs/refresh -d '{"recursive":"true"}'       # -> {"result":{"":"OK"}}
```

**Gotcha:** `"recursive": true` (JSON boolean) is rejected with **HTTP 400**
`{"error":"value must be string \"recursive\"=true"}`. It must be the **string** `"true"`. Every other
boolean parameter in the API takes a real boolean — this one does not.

The `result` map reports per-directory status; a bad path yields `"file does not exist"` for that key
while the call still returns 200. Passing `"dir": ""` explicitly also yields `"file does not exist"` —
to refresh the root, omit `dir` entirely.

Use this after a `sync/*` job finishes so the mount shows the new files immediately.

### 7.4 `vfs/forget`

Params: `fs`, plus any keys starting with `file` or `dir`.

```sh
curl -X POST .../vfs/forget -d '{"dir":"sub","file":"a.txt"}'
# -> {"forgotten": ["sub", "a.txt"]}
```
No args = forget the entire directory cache. Cheaper than `vfs/refresh` (invalidate now, re-read lazily).

### 7.5 `vfs/poll-interval`

Params: `fs`, `interval` (duration string, optional — omit to query), `timeout` (duration, optional;
`<=0` = wait forever).

On a ChangeNotify-capable remote (`onedrive:`), verbatim:

```json
// query
{ "enabled": true, "interval": { "raw": 10000000000, "seconds": 10, "string": "10s" },
  "supported": true }

// set {"interval":"30s","timeout":"10s"}
{ "enabled": true, "interval": { "raw": 30000000000, "seconds": 30, "string": "30s" },
  "supported": true, "timeout": false }
```

`timeout: false` means the new value was applied before the timeout. `interval: "0"` disables polling.

On a remote without ChangeNotify (**the local backend**) it is **HTTP 500**
`{"error":"poll-interval is not supported by this remote"}`. Gate on
`operations/fsinfo → Features.ChangeNotify` before calling.

This is how OneDriveUI gets near-real-time remote change detection: `PollInterval` 10 s on the OneDrive
VFS makes remote edits appear in the mount without a manual refresh.

### 7.6 `vfs/queue`

```json
{ "queue": [
  { "name": "queued.bin", "id": 1, "size": 6291456,
    "expiry": 4.996509308, "tries": 0, "delay": 5, "uploading": false } ]}
```

| Field | Meaning |
|---|---|
| `name` | full path within the VFS |
| `id` | integer handle for `vfs/queue-set-expiry` |
| `size` | bytes |
| `expiry` | float seconds until eligible for upload; **may go negative** |
| `tries` | upload attempts so far |
| `delay` | float seconds between attempts (backoff) |
| `uploading` | true only for the lowest `--transfers` expiry values |

Returns `{"queue": []}` when `--vfs-cache-mode` is `off` or nothing is pending. Together with
`vfs/stats.uploadsQueued` this is the "N files waiting to upload" indicator.

### 7.7 `vfs/queue-set-expiry`

Params: `fs`, `id` (int), `expiry` (float seconds), `relative` (bool, optional).

* `{"id":2,"expiry":-1000000000}` → `{}` — upload **now** (huge negative = highest priority).
* Large positive = defer.
* Unknown id → 500 `{"error":"id not found in queue"}`.
* Setting the expiry of an item that has already started uploading has no effect.
* **The window is tiny** — default `vfs_write_back` is 5 s, so an item is typically gone from the queue
  within 5 s of appearing. Read `vfs/queue`, act on the id immediately, and tolerate
  `id not found in queue` as a normal race, not an error.

This implements the Windows "Upload this file now" / priority-boost context action.

---

## 8. `mount/*`

### 8.1 `mount/types`

```json
{ "mountTypes": ["mount", "mount2", "nfsmount"] }
```
On this machine (libfuse3 present) `mount` is the cgofuse-free `bazil.org/fuse` implementation, `mount2`
the `go-fuse` v2 one, `nfsmount` a local NFS loopback. `cmount` is **not** available in this build —
do not hardcode it. Priority when `mountType` is omitted: `mount`, then `cmount`, then `mount2`.

### 8.2 `mount/mount` — synchronous (returns once the mount is live)

Params: `fs` (required), `mountPoint` (required), `mountType`, `mountOpt` (object), `vfsOpt` (object).
Flat top-level CLI-style keys (`vfs_cache_mode`, `volname`, …) are also accepted; **nested blocks win**
if both are given.

```sh
curl -X POST 127.0.0.1:5573/mount/mount -H 'Content-Type: application/json' -d '{
  "fs": "onedrive:",
  "mountPoint": "/home/user/OneDrive",
  "mountType": "mount",
  "vfsOpt": {
    "CacheMode": "full",
    "DirCacheTime":   300000000000,
    "PollInterval":    10000000000,
    "CacheMaxSize":    10737418240,
    "WriteBack":        5000000000
  },
  "mountOpt": { "VolumeName": "OneDrive", "AllowOther": false }
}'
```
```json
{ "mountPoint": "/home/user/OneDrive" }
```

`vfsOpt`/`mountOpt` keys are the **internal FieldNames** from `options/get`/`options/info`. Durations
and sizes accept either raw nanoseconds/bytes (ints) or suffixed strings; `CacheMode` accepts
`"off"|"minimal"|"writes"|"full"` or the int `0..3`.

**Failure is 500 with the input echoed**, e.g.
`{"error":"failed to mount FUSE fs: directory already mounted, use --allow-non-empty to mount anyway: /path", ...}`.

**Gotcha:** a *failed* `mount/mount` can still leave a VFS registered (it appears in `vfs/list`).
Combined with §7.1, a retry with different `vfsOpt` permanently poisons that fs's VFS addressing.
Always retry with **identical** `vfsOpt`.

### 8.3 vfsOpt ↔ CLI flag map (from `options/info blocks=vfs`)

| CLI flag | `vfsOpt` key | Type | Default |
|---|---|---|---|
| `--no-modtime` | `NoModTime` | bool | false |
| `--no-checksum` | `NoChecksum` | bool | false |
| `--no-seek` | `NoSeek` | bool | false |
| `--dir-cache-time` | `DirCacheTime` | Duration | 5m0s |
| `--vfs-refresh` | `Refresh` | bool | false |
| `--poll-interval` | `PollInterval` | Duration | 1m0s |
| `--read-only` | `ReadOnly` | bool | false |
| `--vfs-links` | `Links` | bool | false |
| `--vfs-cache-mode` | `CacheMode` | CacheMode | off |
| `--vfs-cache-poll-interval` | `CachePollInterval` | Duration | 1m0s |
| `--vfs-cache-max-age` | `CacheMaxAge` | Duration | 1h0m0s |
| `--vfs-cache-max-size` | `CacheMaxSize` | SizeSuffix | off |
| `--vfs-cache-min-free-space` | `CacheMinFreeSpace` | SizeSuffix | off |
| `--vfs-read-chunk-size` | `ChunkSize` | SizeSuffix | 128Mi |
| `--vfs-read-chunk-size-limit` | `ChunkSizeLimit` | SizeSuffix | off |
| `--vfs-read-chunk-streams` | `ChunkStreams` | int | 0 |
| `--dir-perms` | `DirPerms` | FileMode | 777 |
| `--file-perms` | `FilePerms` | FileMode | 666 |
| `--link-perms` | `LinkPerms` | FileMode | 666 |
| `--vfs-case-insensitive` | `CaseInsensitive` | bool | false |
| `--vfs-block-norm-dupes` | `BlockNormDupes` | bool | false |
| `--vfs-write-wait` | `WriteWait` | Duration | 1s |
| `--vfs-read-wait` | `ReadWait` | Duration | 20ms |
| `--vfs-write-back` | `WriteBack` | Duration | 5s |
| `--vfs-read-ahead` | `ReadAhead` | SizeSuffix | 0 |
| `--vfs-used-is-size` | `UsedIsSize` | bool | false |
| `--vfs-fast-fingerprint` | `FastFingerprint` | bool | false |
| `--vfs-disk-space-total-size` | `DiskSpaceTotalSize` | SizeSuffix | off |
| `--umask` | `Umask` | FileMode | 022 |
| `--uid` | `UID` | uint32 | 1000 |
| `--gid` | `GID` | uint32 | 1000 |
| `--vfs-handle-caching` | `HandleCaching` | Duration | 5s |
| `--vfs-metadata-extension` | `MetadataExtension` | string | "" |

### 8.4 mountOpt ↔ CLI flag map (from `options/info blocks=mount`)

| CLI flag | `mountOpt` key | Type | Default |
|---|---|---|---|
| `--debug-fuse` | `DebugFUSE` | bool | false |
| `--attr-timeout` | `AttrTimeout` | Duration | 1s |
| `--option` | `ExtraOptions` | stringArray | [] |
| `--fuse-flag` | `ExtraFlags` | stringArray | [] |
| `--daemon` | `Daemon` | bool | false |
| `--daemon-timeout` | `DaemonTimeout` | Duration | 0s |
| `--daemon-wait` | `DaemonWait` | Duration | 1m0s |
| `--default-permissions` | `DefaultPermissions` | bool | false |
| `--allow-non-empty` | `AllowNonEmpty` | bool | false |
| `--allow-root` | `AllowRoot` | bool | false |
| `--allow-other` | `AllowOther` | bool | false |
| `--allow-idmap` | `AllowIDMap` | bool | false |
| `--async-read` | `AsyncRead` | bool | **true** |
| `--max-read-ahead` | `MaxReadAhead` | SizeSuffix | 128Ki |
| `--write-back-cache` | `WritebackCache` | bool | false |
| `--devname` | `DeviceName` | string | "" |
| `--volname` | `VolumeName` | string | "" |
| `--mount-case-insensitive` | `CaseInsensitive` | Tristate | unset |
| `--direct-io` | `DirectIO` | bool | false |
| `--network-mode` | `NetworkMode` | bool | false |
| `--noappledouble` / `--noapplexattr` | `NoAppleDouble` / `NoAppleXattr` | bool | true / false | (macOS) |

For a GNOME 4x/Wayland desktop, `VolumeName: "OneDrive"` + `DeviceName` control what Nautilus shows in
the sidebar; leave `AllowOther` false unless you need other users/`user_allow_other` in
`/etc/fuse.conf`.

### 8.5 `mount/listmounts`

```json
{ "mountPoints": [
  { "Fs": "onedrive:",
    "MountPoint": "/home/user/OneDrive",
    "MountedOn": "2026-08-30T23:27:08.140450907-04:00" } ]}
```
Note the **capitalised** keys, unlike almost everything else in the API. Empty: `{"mountPoints": []}`.

### 8.6 `mount/unmount` / `mount/unmountall`

`mount/unmount {"mountPoint": "/path"}` → `{}`. `mount/unmountall {}` → `{}`. Both error if the
unmount fails (busy). **The VFS is not destroyed** — `vfs/list` still lists it afterwards (§7.1).

---

## 9. `config/*`

### 9.1 Read

| Endpoint | Params | Result |
|---|---|---|
| `config/listremotes` | — | `{"remotes":["onedrive"]}` (includes env-var-defined remotes) |
| `config/dump` | — | `{"onedrive": {"type":"onedrive","drive_id":"9E52…","drive_type":"personal","token":"{\"access_token\":\"EwB…\"}"}}` |
| `config/get` | `name` | just that remote's key/value map |
| `config/paths` | — | `{"cache":"/home/user/.cache/rclone","config":"/home/user/.config/rclone/rclone.conf","temp":"/tmp"}` |
| `config/providers` | — | `{"providers":[…69 objects…]}` |
| `config/setpath` | `path` | switch config file at runtime |
| `config/unlock` | `configPassword` | unlock an encrypted config (disable `AskPassword` first) |

**`config/dump` and `config/get` return the OAuth refresh token in plaintext.** Never log them, never
render them, never send them anywhere.

`config/providers` entry shape: `{Name, Description, Prefix, Options[], CommandHelp, Aliases, Hide,
MetadataInfo, Overview}`. `onedrive` has **30** options; each option object is identical in shape to
`options/info` entries:

```json
{ "Name": "client_id", "FieldName": "", "Help": "OAuth Client Id.\n\nLeave blank normally.",
  "Default": "", "Value": null, "Hide": 0, "Required": false, "IsPassword": false,
  "NoPrefix": false, "Advanced": false, "Exclusive": false, "Sensitive": true,
  "DefaultStr": "", "ValueStr": "", "Type": "string" }
```
Build the "Advanced settings" dialog from this — `Advanced`, `Required`, `IsPassword`, `Sensitive`,
`Examples[]` and `Type` give you the whole form.

### 9.2 Write

| Endpoint | Params |
|---|---|
| `config/create` | `name`, `type`, `parameters` (map), `opt` (map) |
| `config/update` | `name`, `parameters`, `opt` |
| `config/password` | `name`, `parameters` (**no `opt`**) |
| `config/unset` | `name`, `keys` (array) → `{"removed":["pass","drive_type"]}` (only keys that existed) |
| `config/delete` | `name` → `{}` |

`opt` keys: `obscure`, `noObscure`, `noOutput`, `nonInteractive`, `continue`, `all`, `state`, `result`.

### 9.3 The non-interactive OAuth state machine — **essential for OneDriveUI onboarding**

With `opt.nonInteractive: true`, `config/create` / `config/update` return a *question* instead of
prompting. Verified walkthrough for `type: "onedrive"`:

**Step 1** — `POST /config/create`
```json
{"name":"rctest3","type":"onedrive","parameters":{},
 "opt":{"nonInteractive":true,"noOutput":true}}
```
```json
{ "Error": "", "Result": "",
  "State": "*oauth-islocal,choose_type,,",
  "Option": {
    "Name": "config_is_local", "Type": "bool",
    "Help": "Use web browser to automatically authenticate rclone with remote?\n * Say Y if the machine running rclone has a web browser you can use\n * Say N if running rclone on a (remote) machine without web browser access\nIf not sure try Y. If Y failed, try N.\n",
    "Default": true, "DefaultStr": "true", "ValueStr": "true",
    "Exclusive": true,
    "Examples": [ {"Value":"true","Help":"Yes"}, {"Value":"false","Help":"No"} ],
    "Advanced": false, "Hide": 0, "IsPassword": false, "Required": false,
    "Sensitive": false, "Value": null, "FieldName": "" } }
```

**Step 2** — answer by echoing `State` and supplying `result`:
```json
{"name":"rctest3","type":"onedrive","parameters":{},
 "opt":{"nonInteractive":true,"noOutput":true,
        "continue":true,"state":"*oauth-islocal,choose_type,,","result":"false"}}
```
```json
{ "State": "*oauth-authorize,choose_type,,",
  "Option": { "Name": "config_token", "Type": "string",
              "Help": "For this to work, you will need rclone available on a machine that has a web browser available.\n\nFor more help and alternate methods see: https://rclone.org/remote_setup/\n\nExecute the following on the ..." } }
```

Loop until the response has no `Option` (or `Option` is null) — that means the remote is finished.
`opt.all: true` asks *every* option (advanced included) instead of only the post-config ones; step 1
then starts at `client_id` with `State: "*all-set,0,false"`.

**The remote row is written to `rclone.conf` on the very first `config/create` call**, before the flow
completes. Answering "false" to `config_is_local` yields the headless `config_token` path (paste a
token obtained elsewhere) — no browser, no blocking.

### 9.4 The interactive OAuth path (`config/oauthstatus`, `config/oauthstop`)

**Gotcha — this endpoint set can hang your GUI.** Calling `config/update` or, critically,
**`config/password` (which takes no `opt` and therefore cannot be made non-interactive)** on an OAuth
backend starts rclone's local OAuth webserver and **blocks the HTTP request indefinitely** waiting for
the browser callback. Observed: a `config/password` call hung for >60 s until cancelled.

Recovery / correct flow:

```json
// config/oauthstatus  {}
{ "status": "running",
  "authUrl": "http://127.0.0.1:53682/auth?state=TlEe1YWKV6E4_H7rFTUv4Q" }
// or
{ "status": "stopped" }
```
```json
// config/oauthstop  {}   -> {}   (the blocked call then fails with:)
{ "error": "config failed to refresh token: oauth authentication was cancelled",
  "path": "config/password", "status": 500 }
```

**OneDriveUI sign-in recipe:**
1. `config/create` with `_async: true` (so the HTTP call never blocks the UI thread).
2. Poll `config/oauthstatus` until `status == "running"`, then open `authUrl` with
   `Gio.AppInfo.launch_default_for_uri` / `xdg-open` — or render it in an embedded WebKit view.
3. On user cancel → `config/oauthstop`, then `job/stop` the create job.
4. On success the create job finishes; confirm with `config/get`.

The OAuth callback server binds **127.0.0.1:53682** — check it is free before starting.

---

## 10. `options/*`

### 10.1 `options/blocks`

```json
{ "options": ["main","filter","dlna","s3","vfs","restic","mount","ftp",
              "sftp","nfs","proxy","rc","log","http","webdav"] }
```

### 10.2 `options/get` — current global values

Params: `blocks` (comma-separated string, optional; all if omitted/empty).
Returns `{ "<block>": { "<FieldName>": <value>, ... } }`. Block sizes on this build: `main` 110,
`vfs` 33, `mount` 22, `filter` 18, `rc` 19, `log` 10, `s3` 8, `ftp`/`sftp` 7, `restic`/`webdav` 6,
`dlna`/`http` 5, `nfs` 4, `proxy` 1.

`main` block keys (the exact vocabulary for `_config`):

```
LogLevel StatsLogLevel UseJSONLog DryRun Interactive Links CheckSum SizeOnly IgnoreTimes
IgnoreExisting IgnoreErrors ModifyWindow Checkers Transfers ConnectTimeout Timeout
ExpectContinueTimeout Dump InsecureSkipVerify DeleteMode MaxDelete MaxDeleteSize TrackRenames
TrackRenamesStrategy Retries RetriesInterval LowLevelRetries UpdateOlder NoGzip MaxDepth IgnoreSize
IgnoreChecksum IgnoreCaseSync FixCase NoTraverse CheckFirst NoCheckDest NoUnicodeNormalization
NoUpdateModTime NoUpdateDirModTime DataRateUnit CompareDest CopyDest BackupDir Suffix
SuffixKeepExtension UseListR ListCutoff BufferSize BwLimit BwLimitFile TPSLimit TPSLimitBurst
BindAddr DisableFeatures UserAgent Immutable AutoConfirm StreamingUploadCutoff StatsFileNameLength
AskPassword PasswordCommand UseServerModTime MaxTransfer MaxDuration CutoffMode MaxBacklog
MaxStatsGroups StatsOneLine StatsOneLineDate StatsOneLineDateFormat ErrorOnNoTransfer Progress
ProgressTerminalTitle Cookie UseMmap MaxBufferMemory CaCert ClientCert ClientKey ClientPass
MultiThreadCutoff MultiThreadStreams MultiThreadSet MultiThreadChunkSize MultiThreadWriteBufferSize
OrderBy UploadHeaders DownloadHeaders Headers MetadataSet RefreshTimes NoConsole TrafficClass
FsCacheExpireDuration FsCacheExpireInterval DisableHTTP2 HumanReadable KvLockTime
DisableHTTPKeepAlives Metadata ServerSideAcrossConfigs TerminalColorMode DefaultTime Inplace
PartialSuffix MetadataMapper MaxConnections NameTransform HTTPProxy
```

Notable defaults: `Checkers 8`, `Transfers 4`, `Timeout 300000000000` (5m), `ConnectTimeout 60s`,
`Retries 3`, `LowLevelRetries 10`, `BufferSize 16777216`, `MultiThreadCutoff 268435456` (256Mi),
`MultiThreadStreams 4`, `MaxBacklog 10000`, `MaxStatsGroups 1000`, `PartialSuffix ".partial"`,
`FsCacheExpireDuration 300000000000` (5m).

The `rc` block exposes the live server config, including `"JobExpireDuration": 60000000000` and
`"JobExpireInterval": 10000000000`, and `HTTP.ListenAddr: ["127.0.0.1:5573"]` — handy for confirming
what you connected to.

### 10.3 `options/set` — mutate globals at runtime

```sh
curl -X POST .../options/set -H 'Content-Type: application/json' \
  -d '{"main":{"LogLevel":"INFO","Transfers":8}}'    # -> {}
```
Verified: `options/get` then reports `LogLevel INFO Transfers 8`.

* `LogLevel` accepts the string (`DEBUG|INFO|NOTICE|ERROR`) or the number (8 = DEBUG).
* **Unknown *keys* are silently ignored** (`{"main":{"NoSuchOption":123}}` → `{}`).
* Unknown *blocks* error: 500 `{"error":"unknown option block \"nosuchblock\""}`.
* Not every option takes effect after startup (e.g. `BwLimit` here — use `core/bwlimit`).

### 10.4 `options/info` — schema for building settings UI

Params: `blocks` (optional). Returns `{ "<block>": [ <option object>, ... ] }` in the same shape as
`config/providers`. Verbatim entry:

```json
{ "Name": "vfs_cache_mode", "FieldName": "CacheMode",
  "Help": "Cache mode off|minimal|writes|full", "Groups": "VFS",
  "Default": false, "Value": null, "Hide": 0, "Required": false, "IsPassword": false,
  "NoPrefix": true, "Advanced": false, "Exclusive": false, "Sensitive": false,
  "DefaultStr": "off", "ValueStr": "off", "Type": "CacheMode" }
```

`Name` = CLI flag (minus `--`, `-`→`_`); `FieldName` = the key to use in `_config`/`vfsOpt`/`mountOpt`.
**This mapping is the reason to call `options/info` at startup rather than hardcoding names.**

### 10.5 `options/local` — the debugger

Returns `{"config": {...}, "filter": {...}}` = the settings **this specific call** would run with.
Send the exact `_config`/`_filter` you plan to use on a real job and inspect the result. Use it in
OneDriveUI's "Test settings" path and in unit tests.

---

## 11. Remaining endpoints

### 11.1 `fscache/*`

`fscache/entries {}` → `{"entries": 8}`; `fscache/clear {}` → `{}` then `{"entries": 0}`.
Backends are cached for `FsCacheExpireDuration` (5m). Call `fscache/clear` after `config/update` so the
next operation picks up new credentials. **It does not clear VFSes** (§7.1).

### 11.2 `debug/*`

| Endpoint | Params | Result |
|---|---|---|
| `debug/set-gc-percent` | `gc-percent` (int) | `{"existing-gc-percent": 100}` |
| `debug/set-soft-memory-limit` | `mem-limit` (int bytes; negative = read only) | `{"existing-mem-limit": 9223372036854775807}` |
| `debug/set-mutex-profile-fraction` | `rate` (int; 0 off, <0 read) | `{"previousRate": 0}` |
| `debug/set-block-profile-rate` | `rate` (int; 1 = all, <=0 off) | `{}` |

Then `go tool pprof http://127.0.0.1:5573/debug/pprof/{block,mutex,heap,profile}`.
Note the inconsistent key names: hyphenated in `debug/*`, camelCase for `previousRate`.

### 11.3 `pluginsctl/*`

All six return **500 `{"error":"WebUI needs to be enabled for plugins to work"}`** unless `--rc-web-gui`
is on. **Irrelevant to OneDriveUI — ignore this namespace entirely.**

### 11.4 `serve/*` — run protocol servers on top of a remote

```json
// serve/types {}
{ "types": ["dlna","ftp","http","nfs","restic","s3","sftp","webdav"] }

// serve/start {"type":"http","fs":"/tmp/t","addr":"127.0.0.1:5580","vfs_cache_mode":"off"}
{ "addr": "127.0.0.1:5580", "id": "http-8f48e104" }

// serve/list {}
{ "list": [ { "id": "http-8f48e104", "addr": "127.0.0.1:5580",
              "params": { "addr":"127.0.0.1:5580", "fs":"/tmp/t",
                          "type":"http", "vfs_cache_mode":"off" } } ] }

// serve/stop {"id":"http-8f48e104"}  -> {}
// serve/stopall {}                   -> {}
```

`id` is `"<type>-<8 hex>"`. Extra options go flat (CLI names, `-`→`_`) or nested under
`opt` / `vfsOpt` / `proxyOpt`; nested wins. Verified the served HTTP endpoint answered 200.
Useful as a fallback if FUSE is unavailable (serve webdav + GVFS mount), but FUSE is present here.

### 11.5 `backend/command`

Params: `command`, `fs`, `arg` (array), `opt` (map). → `{"result": <backend-specific>}`.

**OneDrive does not support backend commands**: `{"error":"OneDrive root '': doesn't support backend
commands","status":500}`. Check `Features.Command` from `operations/fsinfo` first (false for onedrive,
true for local).

---

## 12. Owning and detecting the daemon

`core/pid` alone is not proof of ownership. Recommended scheme for OneDriveUI:

1. **Address:** unix socket at `$XDG_RUNTIME_DIR/onedriveui/rc.sock`, inside a `0700` directory.
   Avoids port collisions, gets kernel-level access control for free, no auth secrets to manage.
2. **Startup sequence:**
   ```python
   sock = Path(os.environ["XDG_RUNTIME_DIR"]) / "onedriveui" / "rc.sock"
   if sock.exists():
       try:
           v = rc("core/version")          # 1s timeout
           p = rc("core/pid")["pid"]
           cmdline = Path(f"/proc/{p}/cmdline").read_bytes().decode().split("\0")
           ours = str(sock) in " ".join(cmdline) and "rcd" in cmdline
           if ours and v["decomposed"] >= [1, 75, 0]:
               adopt(p)                    # reuse the running daemon
           else:
               raise RuntimeError("foreign daemon on our socket")
       except (ConnectionError, FileNotFoundError, socket.timeout):
           sock.unlink(missing_ok=True)    # stale socket -> remove and respawn
           spawn()
   else:
       spawn()
   ```
3. **Identity checks, strongest first:**
   * `/proc/<pid>/cmdline` contains our exact socket path **and** `rcd` — proves it is the process we
     configured, not an unrelated rclone.
   * `/proc/<pid>/stat` field 22 (starttime) — record it, so a recycled PID cannot fool you.
   * `job/list.executeId` — a UUID fixed per daemon process. **Store it. If it changes, the daemon
     restarted: every `jobid` you hold is invalid, `core/transferred` history is gone, all mounts and
     VFSes are gone.** Re-mount and re-sync from scratch.
   * `options/get blocks=rc` → `HTTP.ListenAddr` confirms the address, `NoAuth`/`Auth.BasicUser`
     confirm the auth mode.
4. **Never assume "socket file exists" ⇒ alive** (§1.4).
5. **Shutdown:** `core/quit` (returns `{}`, then exits), then `sock.unlink()`. If it does not exit
   within ~2 s, `SIGTERM` the pid; unmount first with `mount/unmountall` or FUSE mounts may be left
   dangling and need `fusermount3 -u`.
6. If TCP is preferred instead: `--rc-addr 127.0.0.1:0` and scrape the port from the NOTICE line on
   stderr, plus `--rc-user onedriveui --rc-pass <32 random bytes>` regenerated each launch.

Suggested launch line:

```sh
rclone rcd \
  --rc-addr "unix://$XDG_RUNTIME_DIR/onedriveui/rc.sock" \
  --rc-no-auth \
  --rc-job-expire-duration 10m \
  --rc-job-expire-interval 30s \
  --cache-dir "$XDG_CACHE_HOME/onedriveui" \
  --log-level INFO --use-json-log --log-file "$XDG_STATE_HOME/onedriveui/rclone.log"
```

(`--rc-no-auth` is safe *only* because the socket lives in a 0700 dir owned by the user.)

---

## 13. Minimal Python client

```python
import json, socket, http.client, urllib.request, urllib.error

class RcError(Exception):
    def __init__(self, payload, status):
        self.payload, self.status = payload, status
        super().__init__(f"{payload.get('path')}: {payload.get('error')} (HTTP {status})")

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.path = path
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.path)
        self.sock = s

class Rc:
    """One instance per daemon. Reuses one keep-alive connection."""
    def __init__(self, sock_path=None, base="http://127.0.0.1:5572", auth=None):
        self.sock_path, self.base, self.auth = sock_path, base, auth

    def call(self, path, params=None, timeout=30):
        body = json.dumps(params or {}).encode()
        headers = {"Content-Type": "application/json"}
        if self.auth:
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(
                f"{self.auth[0]}:{self.auth[1]}".encode()).decode()
        if self.sock_path:
            c = UnixHTTPConnection(self.sock_path, timeout=timeout)
            c.request("POST", "/" + path, body, headers)
            r = c.getresponse(); data, status = r.read(), r.status; c.close()
        else:
            req = urllib.request.Request(f"{self.base}/{path}", body, headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data, status = r.read(), r.status
            except urllib.error.HTTPError as e:
                data, status = e.read(), e.code
        out = json.loads(data) if data else {}
        if status >= 400:
            raise RcError(out, status)
        return out

    # ---- convenience ----
    def start_job(self, path, params, group):
        return self.call(path, {**params, "_async": True, "_group": group})["jobid"]

    def poll(self, jobid):
        """Returns (finished, success, error, output). Handles expiry."""
        try:
            s = self.call("job/status", {"jobid": jobid})
        except RcError as e:
            if "job not found" in e.payload.get("error", ""):
                return True, None, "job expired or daemon restarted", None
            raise
        return s["finished"], s["success"], s["error"], s["output"]

    def progress(self, group):
        s = self.call("core/stats", {"group": group})
        return {
            "bytes":      s["bytes"],
            "totalBytes": s["totalBytes"],
            "pct":        (100 * s["bytes"] // s["totalBytes"]) if s["totalBytes"] else 0,
            "speed":      s["speed"],
            "eta":        s.get("eta"),                 # may be None
            "errors":     s["errors"],
            "lastError":  s.get("lastError", ""),       # absent when errors == 0
            "files":      [t["name"] for t in s.get("transferring", [])],
            "checking":   s.get("checking", []),        # list[str]
        }
```

**Client rules**
* Use **one keep-alive connection per daemon** for the 1 Hz stats poll; a new TCP connect per tick is
  wasteful. Do stats polling on a `QThread`/worker and marshal to the GUI with a Qt signal — never call
  the rc from the Qt main thread.
* Give every call an explicit timeout. Synchronous `sync/*`, `operations/size` and `operations/check`
  on a large OneDrive tree can run for many minutes; **always `_async` those**.
* The rc server is fully concurrent; parallel polls are fine.

---

## 14. Consolidated gotcha list

1. **POST only.** GET/HEAD on an rc path → 404; PUT → 405.
2. Form and query parameters arrive as **strings**; use a JSON body for booleans/numbers/arrays.
3. **`vfs/refresh` needs `"recursive": "true"` as a STRING** — a JSON boolean is a 400 error. Unique in
   the whole API.
4. **`_config.BwLimit` does not throttle.** Only `core/bwlimit` does, and it is process-global.
5. `core/bwlimit` echoes normalised binary units (`1M:100k` → `"1Mi:100Ki"`); don't string-compare.
6. **Finished jobs vanish after 60 s** (`--rc-job-expire-duration`). Capture `output` the instant
   `finished` is true; treat `job not found` as "unknown outcome", not "failed".
7. **`job/status` has no `progress` field** despite the built-in help. Use `core/stats` + `_group`.
8. `job/list` arrays are **unordered**.
9. **Every** rc call — sync included — allocates a job id and a `job/N` stats group; `core/group-list`
   fills with `job/N` noise. Set `_group` on anything you care about and `core/stats-delete` when done.
10. `core/group-list` returns **`{"groups": null}`** when empty, not `[]`.
11. `core/stats` **omits** `transferring`, `checking` and `lastError` when they are empty. Use `.get()`.
12. `core/stats.checking` is `list[str]`; `transferring` is `list[dict]`.
13. `core/stats.eta` is **`null`** when indeterminate.
14. Global `core/stats` (no `group`) accumulates for the whole process lifetime.
15. **`core/transferred` returns `started_at`/`completed_at`/`group`/`srcFs`/`dstFs`, NOT the
    documented `timestamp`/`jobid`.** Only the last 100 entries; lost on restart.
16. In listings, **`Path` is relative to `fs`, not to `fs`+`remote`**.
17. On OneDrive, **directories have `Size: -1`**.
18. `operations/stat` on a missing path returns **200** with `{"item": null}`, not an error.
19. **`operations/uploadfile` takes its parameters in the query string** and names the file from the
    multipart `filename=`, not the field name. Returns a bare `{}`.
20. **OneDrive: `settier` and `backend/command` are unsupported; `ListR` is false** (no fast-list).
    `PublicLink` `unlink:true` appears to be a no-op that still returns the URL.
21. **`config/password` cannot be made non-interactive and blocks on OAuth backends.** Recover with
    `config/oauthstop`. Run `config/create`/`config/update` with `_async` and `opt.nonInteractive`.
22. `config/dump` / `config/get` expose the OAuth token in plaintext.
23. **Duplicate VFSes for the same fs become permanently unaddressable** (`"[0]"`/`"[1]"` names are
    returned by `vfs/list` but rejected everywhere else, and `fscache/clear` doesn't help). One VFS per
    remote, identical `vfsOpt`, always.
24. A **failed** `mount/mount` can still register a VFS; retry with byte-identical `vfsOpt`.
25. `mount/unmount` does **not** destroy the VFS — it stays in `vfs/list`.
26. `mount/listmounts` uses **capitalised** keys (`Fs`, `MountPoint`, `MountedOn`).
27. **`cmount` is not available in this build**; `mount/types` gives `["mount","mount2","nfsmount"]`.
28. `vfs/poll-interval` is **500** on backends without ChangeNotify (e.g. local). Gate on
    `Features.ChangeNotify`.
29. `vfs/queue-set-expiry` races with the 5 s `vfs_write_back`; `id not found in queue` is normal.
30. `options/set` **silently ignores unknown keys** but errors on unknown blocks.
31. **`core/quit` leaves the unix socket file on disk.** Unlink before respawning.
32. `pluginsctl/*` is dead without `--rc-web-gui`. Ignore it.
33. Error HTTP status varies: **400** missing param, **404** not found / unknown method, **500** other.
34. `--rc-no-auth` does not actually exempt anything in v1.75.0 (`NoAuth: false` on all 101 commands),
    so with `--rc-user/--rc-pass` set, `/metrics` needs credentials too.
35. `--rc-server-write-timeout` (1h) caps a synchronous call; longer work must be `_async`.
36. `executeId` changing means the daemon restarted — invalidate all job ids, mounts and history.
37. **`mount/listmounts` only reports mounts created through `mount/mount`.** A mount made by the
    `rclone mount` CLI is invisible to it (verified: a live `rclone mount onedrive: ~/OneDrive` with
    `--rc` reports `{"mountPoints": []}` while `vfs/list` shows its VFS and `/proc/mounts` shows the
    FUSE entry). You cannot discover or unmount a foreign mount via the rc — parse `/proc/self/mounts`
    for `type fuse.rclone` and use `fusermount3 -u`.
38. **`vfs/list` may return a config-hash-suffixed fs name** such as `onedrive{MxOuf}:` when the
    backend was instantiated with connection-string/flag overrides (e.g. `--onedrive-chunk-size 30M`).
    The bare `onedrive:` still resolves while unambiguous, but the suffixed form is what `vfs/list`
    reports — display it, and prefer echoing it back verbatim.
39. **rclone's default rc port 5572 is very likely already taken** by a user's existing
    `rclone mount --rc` (this machine has exactly that). Never bind 5572; never assume a daemon on
    5572 is yours.

---

## 15. Endpoint quick index (sync/async, return shape)

| Endpoint | Sync? | `_async`? | Returns |
|---|---|---|---|
| `rc/list`, `rc/noop`, `rc/noopauth` | sync | yes | command map / echo |
| `rc/error`, `rc/fatal`, `rc/panic` | sync | yes | 500 |
| `core/version`, `core/pid`, `core/memstats`, `core/du`, `core/disks`, `core/gc` | sync | yes | see §3 |
| `core/obscure` | sync | yes | `{obscured}` |
| `core/quit` | sync | — | `{}` then exit |
| `core/bwlimit` | sync | yes | `{bytesPerSecond,Tx,Rx,rate}` |
| `core/stats`, `core/transferred`, `core/group-list` | sync | yes | see §3.7–3.10 |
| `core/stats-reset`, `core/stats-delete` | sync | yes | `{}` |
| `core/command` | sync or streaming | yes | `{error,result,returnType}` |
| `job/list`, `job/status` | sync | yes | see §4 |
| `job/stop`, `job/stopgroup` | sync | yes | `{}` |
| `job/batch` | sync (runs children concurrently) | yes | `{results:[…]}` |
| `operations/list`, `stat`, `about`, `size`, `fsinfo`, `check`, `hashsum*` | sync | **yes — use it** | see §5 |
| `operations/mkdir`, `rmdir`, `rmdirs`, `purge`, `delete`, `deletefile`, `cleanup`, `copyfile`, `movefile`, `copyurl`, `settier*` | sync | yes | `{}` |
| `operations/publiclink` | sync | yes | `{url}` |
| `operations/uploadfile` | sync | yes (query param) | `{}` |
| `sync/copy`, `sync/move`, `sync/sync` | sync | **yes — always** | `{}` |
| `sync/bisync` | sync | **yes — always** | `{basePath,listing1,listing2,logFile,output,session,workDir}` |
| `vfs/list`, `stats`, `queue`, `refresh`, `forget`, `poll-interval` | sync | yes | see §7 |
| `vfs/queue-set-expiry` | sync | yes | `{}` |
| `mount/mount` | sync (blocks until mounted) | yes | `{mountPoint}` |
| `mount/unmount`, `unmountall` | sync | yes | `{}` |
| `mount/listmounts`, `mount/types` | sync | yes | `{mountPoints}` / `{mountTypes}` |
| `config/*` | sync (**may block on OAuth**) | yes | see §9 |
| `options/blocks`, `get`, `info`, `local` | sync | yes | see §10 |
| `options/set` | sync | yes | `{}` |
| `serve/start` | sync | yes | `{addr,id}` |
| `serve/stop`, `stopall` | sync | yes | `{}` |
| `serve/list`, `serve/types` | sync | yes | `{list}` / `{types}` |
| `fscache/entries`, `fscache/clear` | sync | yes | `{entries}` / `{}` |
| `debug/*` | sync | yes | see §11.2 |
| `pluginsctl/*` | sync | yes | 500 without `--rc-web-gui` |
| `backend/command` | sync | yes | `{result}`; 500 on OneDrive |

---

## 16. Field note: a foreign rclone rc daemon already exists on this machine

At the time of writing, this machine is already running:

```
PID 3040  /usr/bin/rclone mount onedrive: /home/user/OneDrive \
   --vfs-cache-mode full --vfs-cache-max-size 20G --vfs-cache-max-age 168h \
   --vfs-fast-fingerprint --vfs-read-ahead 128M --dir-cache-time 24h \
   --poll-interval 1m --attr-timeout 5s --buffer-size 32M \
   --transfers 8 --checkers 16 --onedrive-chunk-size 30M \
   --rc --rc-addr 127.0.0.1:5572 --rc-no-auth --umask 022
```

`/proc/self/mounts` shows `onedrive{MxOuf}: on /home/user/OneDrive type fuse.rclone
(rw,nosuid,nodev,relatime,user_id=1000,group_id=1000)`.

This is a **perfect worked example of the ownership problem**, and everything below was verified
read-only against it.

**Any `rclone mount`, `rclone serve`, `rclone copy`, … started with `--rc` exposes the full 101-command
rc API.** It is not special to `rclone rcd`. Probing port 5572 gave:

| Call | Result |
|---|---|
| `core/version` | `v1.75.0` |
| `core/pid` | `{"pid": 3040}` |
| `rc/list` | 101 commands — identical surface to our own `rcd` |
| `job/list.executeId` | `c2c38b58-4693-45df-8679-f087996df879` (**different** from our daemon's) |
| `options/get blocks=rc` | `ListenAddr ['127.0.0.1:5572']`, `NoAuth True`, `JobExpireDuration 60000000000` |
| `vfs/list` | `{"vfses": ["onedrive{MxOuf}:"]}` |
| `mount/listmounts` | **`{"mountPoints": []}`** — the CLI mount is *not* listed |
| `vfs/stats` | `{"fs":"onedrive{MxOuf}:", "inUse":1, "diskCache":{"bytesUsed":178709025,"files":22,"uploadsQueued":0,"uploadsInProgress":0,"erroredFiles":0,"outOfSpace":false,"path":"/home/user/.cache/rclone/vfs/onedrive{MxOuf}", ...}, "metadataCache":{"dirs":247,"files":1708}}` |
| `vfs/poll-interval` | `{"enabled":true,"interval":{"raw":60000000000,"seconds":60,"string":"1m0s"},"supported":true}` |
| `core/stats` | `bytes 0, transfers 0, elapsedTime ≈1767 s` — i.e. uptime, not job data |

### What this teaches

1. **Do not bind 5572.** It is rclone's default and is already in use here. Use a unix socket (§12) or
   `127.0.0.1:0`.
2. **`--rc-no-auth` on 5572 means anything on this box can already drive that daemon.** If OneDriveUI
   *adopts* a foreign daemon it inherits that exposure. Prefer spawning our own.
3. **`core/pid` + `/proc/<pid>/cmdline` is the discriminator.** Here the cmdline says `rclone mount`,
   not `rcd`, and does not contain our socket path — so it is provably not ours.
4. **`mount/listmounts` is blind to CLI mounts.** To enumerate *all* rclone mounts, read
   `/proc/self/mounts` and match `type fuse.rclone`; the device column carries the fs name
   (`onedrive{MxOuf}:`). Unmount those with `fusermount3 -u <path>` — `mount/unmount` will not find them.
5. **The `{MxOuf}` suffix** is rclone's hash of the non-config backend flags (`--onedrive-chunk-size`
   etc.). It appears in `vfs/list`, in the cache paths under `~/.cache/rclone/vfs/`, and in
   `/proc/mounts`. Both `onedrive:` and `onedrive{MxOuf}:` resolved for `vfs/stats` here, but only
   because there is a single VFS — see gotcha 23.
6. **`elapsedTime` from `core/stats` is process uptime**, which doubles as a cheap "how long has this
   daemon been up" reading (1767 s here).

### Recommended policy for OneDriveUI

* Enumerate candidates: our socket, then `ss -lntp` for `rclone` listeners.
* For each, `core/pid` → `/proc/<pid>/cmdline`. **Adopt only if the cmdline contains our own socket
  path and `rcd`.** Otherwise treat it as foreign: never mutate its config, never `core/quit` it.
* If a foreign daemon has OneDrive mounted at the path we want, surface a first-run dialog — "another
  rclone is already syncing this folder" — rather than fighting it. Offer to take over only after the
  user unmounts (`fusermount3 -u`) or we detect the mount is gone.
