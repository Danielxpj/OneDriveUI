# rclone OneDrive Backend — Authoritative Reference for OneDriveUI

**Verified against:** rclone **v1.75.0** (`/usr/bin/rclone`, go1.26.5, linux/amd64) on CachyOS.
**Live remote used for verification:** `onedrive:` → `type=onedrive`, `drive_type=personal`, `drive_id=1A2B3C4D5E6F7890`.
**Date of verification:** 2026-08-30. All commands run against the live remote were read-only (`about`, `lsjson`, `backend features`, `config redacted`, `config userinfo`, rc GETs).

> **Headline constraints for the UI.** Read this box before designing anything.
>
> | Windows OneDrive feature | Reachable via rclone? | Verdict |
> |---|---|---|
> | Quota ring (used/total/free) | ✅ `rclone about --json` | Native |
> | Browse / list files | ✅ `lsjson` | Native |
> | Share → "Copy link" | ✅ `rclone link` | Native (view/edit/embed, anonymous/organization) |
> | Share → **remove** a link | ❌ `--unlink` is **silently ignored** | Must use metadata_permissions |
> | Share → invite a specific person | ⚠️ Only via `--metadata-mapper` + `metadata_permissions=write` | Awkward, not a first-class command |
> | Recycle bin: list / restore / empty | ❌ **No rclone API at all** | Must emulate locally or deep-link to web |
> | Version history: list / restore | ❌ rclone can only **delete** old versions | Must emulate via `--backup-dir` |
> | Remote change detection | ✅ ChangeNotify via Graph `/delta`, polled | ~1 min latency, mount-only |
> | Account name / email | ❌ `UserInfo: false` | Must get elsewhere |
> | "Files On-Demand" | ✅ `rclone mount --vfs-cache-mode full` | Native-ish |
> | Personal Vault | ⚠️ Appears as a folder on a **different drive ID** | Read-only quirk, see §12 |

---

## 1. Every `--onedrive-*` flag and config key

Source: `rclone help backend onedrive` (v1.75.0), verbatim defaults. Every flag has an
env-var form `RCLONE_ONEDRIVE_<UPPER_SNAKE>` and a config-file key.

### 1.1 Standard options

| Flag | Config key | Type | Default | Notes / when it matters |
|---|---|---|---|---|
| `--onedrive-client-id` | `client_id` | string | *(empty)* | Uses rclone's built-in app when blank. **Blank is what the user's remote has.** Register your own only if you hit app-wide throttling (§10). Marked `Sensitive: true`. |
| `--onedrive-client-secret` | `client_secret` | string | *(empty)* | Pairs with `client_id`. |
| `--onedrive-region` | `region` | string | `"global"` | `global` \| `us` \| `de` \| `cn`. Selects Graph + login endpoints (§1.5). `de` is deprecated. |
| `--onedrive-tenant` | `tenant` | string | *(empty)* | Directory ID; only for **client-credentials** flow. |

### 1.2 Advanced options

| Flag | Config key | Type | Default | Notes / when it matters |
|---|---|---|---|---|
| `--onedrive-token` | `token` | string | — | OAuth token as a JSON blob. This is what lives in `rclone.conf` (§2.3). |
| `--onedrive-auth-url` | `auth_url` | string | — | Override auth server. |
| `--onedrive-token-url` | `token_url` | string | — | Override token server. |
| `--onedrive-client-credentials` | `client_credentials` | bool | `false` | RFC 6749 client-credentials (app-only) flow. Requires `tenant` + a `drive_id`. Not for a consumer desktop client. |
| `--onedrive-upload-cutoff` | `upload_cutoff` | SizeSuffix | `off` | **Deliberately `off`.** Single-part uploads make OneDrive Business burn 2× quota because rclone sets mtime afterward, creating a version. Leave it off. (rclone#1716) |
| `--onedrive-tenant-url` | `tenant_url` | string | — | SharePoint v2.0 API endpoint for non-admin business access. Found via devtools `driveAccessToken` → `.driveUrl`. E.g. `https://your-tenant.sharepoint.com/_api`. |
| `--onedrive-chunk-size` | `chunk_size` | SizeSuffix | `10Mi` | **Must be a multiple of 320 KiB (327,680 bytes).** Should not exceed 250M or you get `Microsoft.SharePoint.Client.InvalidClientQueryException: The request message is too big.` **Chunks are buffered in RAM** — real memory cost is `chunk_size × transfers`. The user's live mount uses `30M`. |
| `--onedrive-drive-id` | `drive_id` | string | — | The drive to use. Set automatically at config time. |
| `--onedrive-drive-type` | `drive_type` | string | — | `personal` \| `business` \| `documentLibrary`. **Drives most feature branching** (§1.4). |
| `--onedrive-root-folder-id` | `root_folder_id` | string | — | Access a folder by ID when path traversal isn't possible. |
| `--onedrive-access-scopes` | `access_scopes` | SpaceSepList | `Files.Read Files.ReadWrite Files.Read.All Files.ReadWrite.All Sites.Read.All offline_access` | Alternatives: read-only set, or the set without `Sites.Read.All` (equivalent to the old `disable_site_permission`). |
| `--onedrive-expose-onenote-files` | `expose_onenote_files` | bool | `false` | OneNote files are **hidden by default** because Open/Update fail on them. Hiding them also **prevents deleting them**. Set true if the UI must show/delete OneNote notebooks. |
| `--onedrive-server-side-across-configs` | `server_side_across_configs` | bool | `false` | **Deprecated** — use the global `--server-side-across-configs`. |
| `--onedrive-list-chunk` | `list_chunk` | int | `1000` | Page size for listings. |
| `--onedrive-no-versions` | `no_versions` | bool | `false` | After each upload/mtime-set, deletes all but the latest version. **NB: OneDrive Personal cannot delete versions — do not set this on the user's remote.** See §9. |
| `--onedrive-hard-delete` | `hard_delete` | bool | `false` | Uses `POST /permanentDelete` instead of `DELETE`. **Business/SharePoint only — Personal does not support the permanentDelete API.** See §8. |
| `--onedrive-link-scope` | `link_scope` | string | `"anonymous"` | `anonymous` (anyone with link, no sign-in; admin may disable) \| `organization` (Business/SharePoint only). |
| `--onedrive-link-type` | `link_type` | string | `"view"` | `view` \| `edit` \| `embed`. |
| `--onedrive-link-password` | `link_password` | string | — | **OneDrive Personal *paid* accounts only.** Sent as `password` in the createLink body. |
| `--onedrive-hash-type` | `hash_type` | string | `"auto"` | `auto`\|`quickxor`\|`sha1`\|`sha256`\|`crc32`\|`none`. `auto` ⇒ QuickXorHash. See §6. |
| `--onedrive-av-override` | `av_override` | bool | `false` | Download files the server flags as infected. See §1.3. |
| `--onedrive-delta` | `delta` | bool | `false` | Advertise `ListR` via the delta API. **Root-only** — see §5.2. |
| `--onedrive-metadata-permissions` | `metadata_permissions` | Bits | `off` | `off`\|`read`\|`write`\|`read,write`\|`failok`. The **only** route to per-person sharing (§4.4). |
| `--onedrive-encoding` | `encoding` | Encoding | `Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,BackSlash,Del,Ctl,LeftSpace,LeftTilde,RightSpace,RightPeriod,InvalidUtf8,Dot` | See §1.6. |
| `--onedrive-description` | `description` | string | — | Free-text label for the remote. Useful for the UI's account list. |

### 1.3 `av_override` exact behaviour (v1.75.0 wording)

Error you will see without it:

```
server reports this file is infected with a virus - use --onedrive-av-override to download anyway: Infected (name of virus): 403 Forbidden:
```

When set, rclone downloads via **Graph beta APIs** with header `Prefer: forceInfectedDownload`
(`contentStream`, then `/content`). Clean files keep using stable v1.0.
Caveats straight from the help text: *"This is a beta API and may change. It works reliably with
application permissions (client_credentials). With delegated (user) login on OneDrive for Business,
Microsoft often still blocks the download. `tenant_url` configurations fall back to the legacy
`AVOverride` query parameter."*

**UI implication:** surface a per-file "Download anyway" affordance that re-runs the copy with
`--onedrive-av-override`, and warn it may still fail.

### 1.4 `drive_type` branching (from `backend/onedrive/onedrive.go`)

`drive_type` is the single biggest behavioural switch:

- **Server-side copy** is refused across a personal↔business boundary, and across two *different*
  business drive IDs (§7).
- **`hard_delete`** only works on business/documentLibrary.
- **`no_versions`** only works on business/documentLibrary.
- **`link_password`** only works on personal (paid).
- **`link_scope=organization`** only works on business/SharePoint.
- **Public-link → direct-download conversion** uses a different algorithm per type (§4.3).
- **Path resolution**: for `personal`, rclone addresses items as
  `drives/{driveID}/items/{itemID}:/relPath` rather than `drives/{driveID}/root:/path`, so that
  "shared with me" folders work (rclone#2536, #2778).
- **Metadata**: `btime`/`mtime`/`utime`/`shared-time` have **millisecond** accuracy on Personal,
  **second** accuracy on Business. Setting mtime/btime on a *folder* costs one extra API call on
  Business only.
- Error message on a failed public link is augmented only for non-personal:
  `... (is making public links permitted by the org admin?)`.

### 1.5 Region endpoint maps (verbatim from source)

```go
graphAPIEndpoint = map[string]string{
    "global": "https://graph.microsoft.com",
    "us":     "https://graph.microsoft.us",
    "de":     "https://graph.microsoft.de",
    "cn":     "https://microsoftgraph.chinacloudapi.cn",
}
authEndpoint = map[string]string{
    "global": "https://login.microsoftonline.com",
    "us":     "https://login.microsoftonline.us",
    "de":     "https://login.microsoftonline.de",
    "cn":     "https://login.chinacloudapi.cn",
}
```

API base is `graphAPIEndpoint[region] + "/v1.0"`, or `tenant_url + "/v2.0"` when `tenant_url` is set.

### 1.6 Restricted filename characters — the UI must validate these

Replaced **anywhere** in a name (rclone maps them to fullwidth look-alikes on upload):

| Char | Hex | Replacement |
|---|---|---|
| `"` | 0x22 | ＂ |
| `*` | 0x2A | ＊ |
| `:` | 0x3A | ： |
| `<` | 0x3C | ＜ |
| `>` | 0x3E | ＞ |
| `?` | 0x3F | ？ |
| `\` | 0x5C | ＼ |
| `\|` | 0x7C | ｜ |

Only at **end** of name: space (0x20) → ␠, `.` (0x2E) → ．
Only at **start** of name: space (0x20) → ␠, `~` (0x7E) → ～

**Other hard limits** (document these in the UI's error strings):
- Names are **case-insensitive** — you cannot have `Hello.doc` and `hello.doc` in one folder.
- **Max file size 250 GiB** (Personal and Business).
- **Full path must stay under 400 characters.** A 400 error with
  `InnerError.Code == "pathIsTooLong"` is returned as a `NoRetryError` — rclone will *not* retry it,
  so surface it to the user immediately as a rename prompt.
- **~50,000 files per directory** is fine; **100,000+ triggers listing errors**.

---

## 2. Authentication

### 2.1 What the OAuth flow actually is

rclone runs an OAuth2 **authorization-code** flow against
`https://login.microsoftonline.com` using its own built-in client ID (unless you set one).
It spins up a **local webserver**. Constants from `lib/oauthutil/oauthutil.go` (v1.75.0):

```go
bindPort             = "53682"
bindAddress          = "127.0.0.1:53682"
RedirectURL          = "http://127.0.0.1:53682/"
RedirectPublicURL    = "http://localhost.rclone.org:53682/"
RedirectLocalhostURL = "http://localhost:53682/"
TitleBarRedirectURL  = "urn:ietf:wg:oauth:2.0:oob"
```

The user is sent to a **local** URL first: `http://127.0.0.1:53682/auth?state=<128-char random>`,
which 302s to Microsoft. Microsoft redirects back to `http://localhost:53682/` with the code.

**If you register your own Azure app, its Redirect URI must be `http://localhost:53682/`.**

**Empirically verified** — `rclone authorize onedrive --auth-no-open-browser` writes to **stderr**:

```
NOTICE: Make sure your Redirect URL is set to "http://localhost:53682/" in your custom config.
NOTICE: Please go to the following link: http://127.0.0.1:53682/auth?state=xYdtscsz0dcZQDPTPETewA
NOTICE: Log in and authorize rclone for access
NOTICE: Waiting for code...
```

The token blob is later printed to **stdout** between the markers
`Paste the following into your remote machine --->` and `<---End paste`.

### 2.2 ⭐ How to drive sign-in from the GUI — the recommended path

**Use the rc API with `config/create` async + `config/oauthstatus`.** These endpoints exist and were
verified live on v1.75.0:

```
* config/create      -- create the config for a remote.
* config/update      -- update the config for a remote.
* config/oauthstatus -- Get the status of the OAuth authentication server.
* config/oauthstop   -- Stop any running OAuth authentication server.
```

`config/oauthstatus` returns (verified live):

```json
{ "status": "stopped" }
```

and while a flow is running:

```json
{ "status": "running", "authUrl": "http://127.0.0.1:53682/auth?state=..." }
```

**Exact GUI sequence:**

```python
import requests, time
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

RC = "http://127.0.0.1:5572"

# 1. Kick off config creation asynchronously. rclone starts the OAuth webserver
#    and blocks the job until the callback lands.
r = requests.post(f"{RC}/config/create", json={
    "name": "onedrive",
    "type": "onedrive",
    "parameters": {"config_is_local": "true", "region": "global"},
    "opt": {"nonInteractive": False},
    "_async": True,
}).json()
jobid = r["jobid"]

# 2. Poll config/oauthstatus until it reports the authUrl.
auth_url = None
for _ in range(50):
    st = requests.post(f"{RC}/config/oauthstatus").json()
    if st.get("status") == "running" and st.get("authUrl"):
        auth_url = st["authUrl"]
        break
    time.sleep(0.2)

# 3. Open it in the user's browser (GNOME/Wayland: this shells out to xdg-open).
QDesktopServices.openUrl(QUrl(auth_url))

# 4. Poll job/status until finished. `output` carries the final State/Option blob.
while True:
    js = requests.post(f"{RC}/job/status", json={"jobid": jobid}).json()
    if js["finished"]:
        ok, err = js["success"], js["error"]
        break
    time.sleep(0.5)

# 5. If the user cancels the dialog, tear the server down:
#    requests.post(f"{RC}/config/oauthstop")
```

`config/oauthstop` returns `{"error": "no oauth authentication is in progress", ...}` (HTTP 500) if
nothing is running — treat that as benign.

**Why this path and not the CLI:** it gives you the auth URL *programmatically* (so you can render
your own Fluent "Sign in" dialog and open the browser yourself), it gives you a cancel button, and
it never requires parsing NOTICE lines out of stderr.

### 2.2b Fallback path — subprocess `rclone authorize`

If you don't want a long-lived rcd, spawn:

```
rclone authorize onedrive --auth-no-open-browser
```

- Parse **stderr** for `Please go to the following link: (\S+)` → open in browser.
- Read **stdout** until `<---End paste`; the blob between the markers is the token.
- Then write it with `rclone config create onedrive onedrive token '<blob>' drive_type personal`
  (or the `config/create` rc call with `parameters: {"token": blob}`).

Kill the child process to cancel; it holds port 53682 until it gets a code.

### 2.2c The non-interactive state machine (empirically walked)

`rclone config create <name> onedrive --non-interactive [--all]` returns a JSON blob per question.
**Verified transitions** (walked in a throwaway config file):

```
step 0  State: "*oauth-islocal,choose_type,,"   Option.Name: config_is_local  (bool, default true)
        ├─ answer "true"  → rclone runs the LOCAL webserver flow (blocks until callback)
        └─ answer "false" ↓
step 1  State: "*oauth-authorize,choose_type,,"  Option.Name: config_token  (string, Required: true)
        Help: 'Execute the following on the machine with the web browser
               (same rclone version recommended):
                   rclone authorize "onedrive"
               Then paste the result.'
step 2  answer "" → hard error:
        Error: failed to configure OneDrive: empty token found - please run "rclone config reconnect myod:"
```

With `--all`, the walk starts earlier at `State: "*all-set,0,false"` asking `client_id`.

Continue the machine with:

```
rclone config create <name> onedrive --non-interactive \
    --continue --state "<State>" --result "<answer>"
```

An empty `State` in the response means the process is done. The `Option` object shape is
`{Name, FieldName, Help, Default, Value, Hide, Required, IsPassword, NoPrefix, Advanced,
Exclusive, Sensitive, DefaultStr, ValueStr, Type}` — `Exclusive: true` means "only offer the
`Examples`, no free text", which maps cleanly onto a Fluent combo box vs. text field.

The same protocol is available over rc as `config/create` with
`opt: {nonInteractive, continue, state, result, all, obscure, noObscure, noOutput}`.
Note that with `--continue`, **all passwords must be passed in the clear** and **all previously
answered default values must be re-passed on every invocation**.

> rclone ships `bin/config.py` in its source tree as a readable reference implementation of this
> protocol — worth reading before writing the Python driver.

### 2.3 Where the token lives

Config file path (verified): **`/home/user/.config/rclone/rclone.conf`**
(`rclone config paths` also reports cache `~/.cache/rclone`, temp `/tmp`).

The live section, redacted:

```ini
[onedrive]
type = onedrive
token = {"access_token":"EwBY...","token_type":"Bearer","refresh_token":"M.C5...","expiry":"2026-...","expires_in":...}
drive_id = 1A2B3C4D5E6F7890
drive_type = personal
```

The `token` value is a **single-line JSON object** with keys
`access_token`, `token_type`, `refresh_token`, `expiry`, `expires_in`.

- `rclone config show onedrive:` prints it **in the clear** — never log this.
- `rclone config redacted onedrive` replaces sensitive values with `XXX` (verified: it redacts
  `token` **and** `drive_id`). Use this for any "copy diagnostics" button.
- The rc `config/get {"name":"onedrive"}` and `config/dump` also return the token in the clear.

### 2.4 Detecting an expired / invalid token programmatically

There is **no dedicated "is my token valid" command**. Use these signals:

1. **Cheapest probe:** `rclone about onedrive: --json`. Exit code 0 ⇒ token is good and was
   refreshed if needed. Non-zero ⇒ inspect stderr.
2. **Missing/blank token** → exact string:
   `failed to configure OneDrive: empty token found - please run "rclone config reconnect <name>:"`
3. **Refresh token expired** (OneDrive expires refresh tokens after **90 days of non-use**) →
   Microsoft returns `invalid_grant`; rclone surfaces a `couldn't fetch token` / `invalid_grant`
   error. Remedy is `rclone config reconnect onedrive:`.
4. **MFA required** → `AADSTS50076` (`invalid_grant`). Remedy: re-run the OAuth flow.
5. **Unmanaged tenant** → `AADSTS65005` (`access_denied`). Not fixable by re-auth; the org admin
   must claim the domain via DNS. Consider telling the user to use the WebDAV backend.
6. **Transient 401**: rclone auto-retries a 401 when `Www-Authenticate` contains `expired_token`,
   or when the error text contains `Unable to initialize RPS`. Don't treat a single 401 in the log
   as sign-out.

**Recommended UI rule:** run `rclone about` on startup and every N minutes. Classify stderr with a
regex for `empty token found|invalid_grant|AADSTS50076|AADSTS65005|couldn't fetch token` → show the
"Sign in again" banner. Everything else → transient network error.

### 2.5 Re-auth from the GUI

`rclone config reconnect onedrive:` exists but has **no `--non-interactive` flag** — it drops into
the interactive TUI. **Do not shell out to it from the GUI.**

Instead, re-run the §2.2 flow against the *existing* remote name using **`config/update`** (same
parameter shape as `config/create`). Per `rclone config update --help`: *"If the remote uses OAuth
the token will be updated"* — and you can suppress that with `config_refresh_token=false`.

---

## 3. Quota — `rclone about`

**Verified live** on `onedrive:`:

```console
$ rclone about onedrive: --json
{
	"total": 1104880336896,
	"used": 252544077005,
	"trashed": 0,
	"free": 852336259891
}
```

```console
$ rclone about onedrive:
Total:   1.005 TiB
Used:    235.200 GiB
Free:    793.800 GiB
Trashed: 0 B
```

Via rc (verified live against the running mount's rc on 127.0.0.1:5572):

```console
$ curl -s -X POST 127.0.0.1:5572/operations/about -H 'Content-Type: application/json' \
       -d '{"fs":"onedrive:"}'
{ "free": 852336259891, "total": 1104880336896, "trashed": 0, "used": 252544077005 }
```

**Field semantics** (all values are **bytes**):

| Field | Meaning | Present for onedrive? |
|---|---|---|
| `total` | Total size available | ✅ |
| `used` | Total size used | ✅ |
| `free` | Space available to this user | ✅ |
| `trashed` | Space used by trash | ✅ (but see below) |
| `other` | Other storage (e.g. Gmail/Photos) | ❌ omitted for onedrive |
| `objects` | Total object count | ❌ omitted for onedrive |

- `Features.About == true` → supported. Feature-detect with `operations/fsinfo`.
- ⚠️ **`trashed` reads `0` on this Personal account** even though the recycle bin is a real thing in
  the web UI. Do **not** build a "Recycle bin: X GB" tile off this number — it is not reliable on
  Personal. Note that `total - used - free` = 0 here, so the ring can be drawn from `used`/`total`.
- The `--full` flag prints raw bytes in the human format instead of IEC units.
- **UI:** draw the Windows-11-style quota ring from `used / total`. Refresh on a timer (60 s is
  plenty; it is a single cheap API call) and after any large transfer completes.

---

## 4. Sharing links

### 4.1 The command

```console
rclone link onedrive:path/to/file
rclone link onedrive:path/to/folder/
rclone link --expire 1d onedrive:path/to/file
rclone link --unlink onedrive:path/to/folder/     # ⚠️ NO-OP on onedrive — see 4.2
```

Flags: `--expire Duration` (default `off` ⇒ ~100 years), `--unlink`.

rc equivalent:

```json
POST /operations/publiclink
{ "fs": "onedrive:", "remote": "path/to/file", "expire": "24h", "unlink": false }
→ { "url": "https://1drv.ms/..." }
```

`Features.PublicLink == true` for onedrive (verified).

### 4.2 ⚠️ `--unlink` is silently ignored — verified in source

The signature accepts it, but grepping the entire v1.75.0 `backend/onedrive/onedrive.go` for
`unlink` returns **exactly one hit — the parameter declaration itself**:

```go
func (f *Fs) PublicLink(ctx context.Context, remote string, expire fs.Duration, unlink bool) (link string, err error) {
```

The body never reads it. It unconditionally does `POST /items/{id}/createLink`.

**So `rclone link --unlink onedrive:x` CREATES a link rather than removing one.**
Do **not** wire a "Remove link" button to `--unlink`. Either:
- remove the sharing permission via `metadata_permissions` (§4.4), or
- deep-link the user to the OneDrive web UI, or
- hide the affordance.

### 4.3 What gets sent and what comes back

Request body (`api.CreateShareLinkRequest`):

```go
type CreateShareLinkRequest struct {
    Type     string     `json:"type"`                         // link_type: view | edit | embed
    Scope    string     `json:"scope,omitempty"`              // link_scope: anonymous | organization
    Password string     `json:"password,omitempty"`           // link_password; OneDrive Personal only
    Expiry   *time.Time `json:"expirationDateTime,omitempty"` // yyyy-MM-ddTHH:mm:ssZ
}
```

`Expiry` is only set when `expire < fs.DurationOff`.

The response `CreateShareLinkResponse` carries `{ID, Roles[], Link{Type, Scope, WebURL, Application{...}}}`.
rclone returns `Link.WebURL`, **then rewrites it into a direct-download URL** for non-folders on
`region=global` only:

- **Personal** — expects a 5-segment URL. Base64-encodes the whole share URL, `/`→`_`, `+`→`-`,
  strips `=`, then:
  `https://api.onedrive.com/v1.0/shares/u!<enc>/root/content`
- **Business** — expects an 8-segment URL:
  `https://{tenant}-my.sharepoint.com/:t:/g/personal/{user_email}/{Opaque}` becomes
  `https://{tenant}-my.sharepoint.com/personal/{user_email}/_layouts/15/download.aspx?share={Opaque}`
- **Folders** are never converted — you get the plain `https://1drv.ms/...` share URL, logged as
  `Can't convert share link for folder to direct link - returning the link as is`.
- Conversion failure logs `Don't know how to convert share link to direct link - returning the link as is`
  and returns the share URL unchanged.

> **UI consequence:** for a *file*, `rclone link` hands you a **direct-download** URL, not the
> pretty `1drv.ms` page URL that Windows' "Copy link" produces. If you want Windows parity in the
> "Copy link" flyout you must either accept the difference, or only use `rclone link` on folders,
> or call the Graph `createLink` endpoint yourself. Document this to the user.

Error augmentation on non-personal drives:
`... (is making public links permitted by the org admin?)` when a 400 comes back.

### 4.4 Per-person invites — the honest answer

There is **no `rclone share --to alice@example.com`**. The only route is system metadata:

```console
rclone lsjson onedrive:path --stat -M --onedrive-metadata-permissions read
```

**Verified live** (redacted) — the `permissions` key is present and populated:

```json
{
 "Path": "aow bjj.txt",
 "Size": 2723,
 "MimeType": "text/plain",
 "ModTime": "2023-01-11T21:45:42Z",
 "IsDir": false,
 "Hashes": { "quickxor": "d166c83af5c63e4c7c9fc378bdde63c5ff7355b0" },
 "ID": "1A2B3C4D5E6F7890#1A2B3C4D5E6F7890!4182",
 "Metadata": {
   "btime": "2023-02-21T16:00:15Z",
   "content-type": "text/plain",
   "created-by-display-name": "<redacted>",
   "created-by-id": "<redacted>",
   "id": "1A2B3C4D5E6F7890#1A2B3C4D5E6F7890!4182",
   "last-modified-by-display-name": "<redacted>",
   "last-modified-by-id": "<redacted>",
   "malware-detected": "false",
   "mtime": "2023-01-11T21:45:42Z",
   "permissions": "<redacted>",
   "utime": "2023-02-21T16:00:15Z"
 }
}
```

**Writing** permissions requires `--onedrive-metadata-permissions write` (or `read,write`) plus
`--metadata-mapper`. Schema is the raw Graph `permission` resource. Personal uses `grantedTo` +
`invitation`; Business uses `grantedToIdentities`.

Add a "read" grant:

```json
{
    "Metadata": {
        "permissions": "[{\"grantedToIdentities\":[{\"user\":{\"id\":\"ryan@contoso.com\"}}],\"roles\":[\"read\"]}]"
    }
}
```

Rules:
- `read,write` is strongly preferred over bare `write`, because updating/removing needs the
  Permission **ID**, which you only get by reading first.
- **Updating**: pass the Permission ID + new `roles`. `roles` is the *only* mutable property.
- **Removing**: pass a blob containing only the permissions you want to **keep** (empty array
  removes all). The `owner` role is always ignored and cannot be removed. **This is your
  "Remove link" / "Stop sharing" implementation.**
- Creating a public link this way works if `Link.Scope == "anonymous"`.
- Adding a permission **fails if a conflicting one already exists**.
- `failok` in the Bits value downgrades permission-write failures to logged errors instead of a
  failed transfer.
- Both reading and writing permissions cost **extra API calls** (and per §10, permission
  operations cost **5 resource units** each vs 1–2 for normal ops) — leave `metadata_permissions`
  at `off` for normal browsing and only enable it on the specific "Manage access" call.

**Not reachable at all:** the "Manage access" pane's richer features — expiry per-person, block
download, "Anyone with existing access", sharing-audit, or email invitations with a message body.
Deep-link to `https://onedrive.live.com` for those.

---

## 5. Change detection

### 5.1 ChangeNotify — supported ✅

**Verified:** `rclone backend features onedrive:` reports `"ChangeNotify": true`.

Implementation (`onedrive.go` v1.75.0):

```go
func (f *Fs) ChangeNotify(ctx, notifyFunc func(string, fs.EntryType), pollIntervalChan <-chan time.Duration) {
    go func() {
        // get the StartPageToken early so all changes from now on get processed
        nextDeltaToken, err := f.changeNotifyStartPageToken(ctx)   // == changeNotifyNextChange(ctx, "latest")
        ...
        for {
            select {
            case pollInterval, ok := <-pollIntervalChan:   // reset ticker, 0 disables
            case <-tickerC:
                nextDeltaToken, err = f.changeNotifyRunner(ctx, notifyFunc, nextDeltaToken)
            }
        }
    }()
}
```

The request it makes:

```go
GET {graphAPIEndpoint[region]}/v1.0/drives/{driveID}/root/delta?token={token}
```

(or `{tenant_url}/v2.0/drives/...` when `tenant_url` is set).

The initial token is fetched with the literal string `"latest"`, so only changes *from startup
onward* are reported. Each round parses `delta.DeltaLink`'s `token` query param as the next cursor.

**Crucially, it notifies the whole ancestor chain:** *"if a/b/c is changed, this function will call
notifyFunc with a, a/b and a/b/c."* Your UI's invalidation logic must expect several callbacks per
logical change and de-duplicate.

Items whose `parentReference.ID == ""` (the drive root itself) are skipped, as is anything outside
`f.root`.

### 5.2 How you actually consume it — **only via a mount**

⚠️ **ChangeNotify is not exposed as a standalone rclone command or rc endpoint.** It is consumed by
the **VFS layer**. So the UI gets remote-change notifications only by running `rclone mount`
(or `serve`) with:

```
--poll-interval 1m      # default 1m0s; MUST be smaller than --dir-cache-time; 0 disables
--dir-cache-time 5m     # default 5m0s
```

The user's existing live mount already does this:

```
rclone mount onedrive: /home/user/OneDrive --vfs-cache-mode full \
  --vfs-cache-max-size 20G --vfs-cache-max-age 168h --vfs-fast-fingerprint \
  --vfs-read-ahead 128M --dir-cache-time 24h --poll-interval 1m --attr-timeout 5s \
  --buffer-size 32M --transfers 8 --checkers 16 --onedrive-chunk-size 30M \
  --rc --rc-addr 127.0.0.1:5572 --rc-no-auth --umask 022
```

**Latency budget: remote changes appear after at most `--poll-interval` (≈60 s).** That is the
floor for "a file someone else edited shows up in our UI." Windows' OneDrive client uses push
notifications and is faster; you cannot match it through rclone. Setting `--poll-interval 15s` is
reasonable and costs 1 resource unit per poll (delta-with-token is discounted to 1 RU — see §10),
i.e. ~4 RU/min, which is negligible against the 3,000-requests-per-5-min user budget.

For a **local** filesystem watcher you're on your own: `rclone backend features /tmp` reports
`"ChangeNotify": false` for the local backend in v1.75.0. Use Python `inotify`/`watchdog` or
`QFileSystemWatcher` for the local side.

### 5.3 `--onedrive-delta` (ListR / `--fast-list`) — a different thing

`delta = true` makes the backend advertise **`ListR`**, i.e. recursive listing in one pass.
**Verified empirically:**

```console
$ rclone backend features onedrive:                  → "ListR": false
$ rclone backend features onedrive: --onedrive-delta → "ListR": true
```

Speeds up `rclone lsf -R`, `rclone size`, `rclone rc vfs/refresh recursive=true`.

**The caveat, verbatim from the help text:**

> *"the delta listing API **only** works at the root of the drive. If you use it not at the root then
> it recurses from the root and discards all the data that is not under the directory you asked for.
> So it will be correct but may not be very efficient. … As a rule of thumb if nearly all of your
> data is under rclone's root directory then using this flag will be a big performance win. If your
> data is mostly not under the root then using this flag will be a big performance loss."*

This is confirmed by `buildDriveDeltaOpts`, which always builds `/{driveID}/root/delta`.

**Recommendation for OneDriveUI:** the remote is mounted at the drive root, so **enable
`delta = true`** and use `--fast-list` for the initial tree scan and for `vfs/refresh recursive=true`.
Disable per-command with `--disable ListR` if a subtree operation turns pathological.

---

## 6. Hashes

- **Verified live:** `rclone backend features onedrive:` → `"Hashes": ["quickxor"]`,
  `"Precision": 1000000000` (1 s modtime precision), `"SlowHash": false`.
- Default since rclone **1.62** is **QuickXorHash for all OneDrive types**. Before that, Personal
  used SHA1.
- **Since July 2023 QuickXorHash is the only hash Microsoft offers** for both Business and Personal.
  Treat `hash_type=sha1` as dead for new data.
- `hash_type` accepts `auto|quickxor|sha1|sha256|crc32|none`. If the requested hash doesn't exist on
  the object, rclone returns an **empty string**, which it treats as a *missing* hash (not a
  mismatch).
- A QuickXorHash is 20 bytes / 40 hex chars, e.g. `d166c83af5c63e4c7c9fc378bdde63c5ff7355b0`
  (verified on a real file above).

### 6.1 Implications for cheap local-vs-remote comparison

- The **local backend can compute QuickXorHash** — verified: `rclone backend features /tmp` lists
  `quickxor` among its hashes. So `rclone check onedrive: /local --checksum` is a true
  content comparison with **no download**. This is the single most important fact for building a
  reliable sync status indicator.
- But QuickXor on the local side is a **full file read**. For a large tree, prefer size+modtime
  (rclone's default) and reserve `--checksum` for a "Verify" action or for files where
  size+modtime are ambiguous.
- `--size-only` is the cheapest and is what you want for a fast "does anything look different"
  sweep; it is also the documented workaround for the iOS Live Photos bug (§12).
- `Precision: 1000000000` ns = **1 second**. Never compare local vs remote mtimes with sub-second
  resolution — you will get infinite re-uploads. rclone handles this internally via `--modify-window`,
  which it derives from the precision automatically.

### 6.2 bisync `--compare`

`rclone bisync --compare` takes a comma-separated list: `size,modtime,checksum` (default
`size,modtime`). Relevant companions:

- `--download-hash` — *"Compute hash by downloading when otherwise unavailable. (warning: may be
  slow and use lots of data!)"* — you will **not** need this for onedrive↔local since both sides do
  quickxor natively.
- `--conflict-resolve none|path1|path2|newer|older|larger|smaller` (default `none`)
- `--conflict-loser ,num,pathname,delete` (default `num`)
- `--conflict-suffix` (default `conflict`; accepts two comma-separated values for path1/path2)
- `--check-access` / `--check-filename` (default `RCLONE_TEST`)
- `--check-sync true|false|only` (default `true`)
- `--resync`, `--recover`, `--force`, `--create-empty-src-dirs`, `--filters-file`

Recommended for OneDriveUI: `--compare size,modtime` for routine runs (fast, both sides give
1 s-accurate mtimes), and offer a "Deep verify" toggle that adds `checksum`.

---

## 7. Server-side operations

**Verified feature flags:** `"Copy": true`, `"Move": true`, `"DirMove": true`, `"Purge": true`,
`"ServerSideAcrossConfigs": false`.

From `func (f *Fs) Copy`, server-side copy is **refused** (returns `fs.ErrorCantCopy`, which makes
rclone fall back to download+upload) when:

```go
// cross-type: personal ↔ business/sharepoint
if (f.driveType == driveTypePersonal && srcObj.fs.driveType != driveTypePersonal) ||
   (f.driveType != driveTypePersonal && srcObj.fs.driveType == driveTypePersonal) { ... }
// two different business drives
else if f.driveType == driveTypeBusiness && srcObj.fs.driveType == driveTypeBusiness &&
        srcObj.fs.driveID != f.driveID { ... }
```

Also refused when source and destination differ only by case:
`can't copy %q -> %q as are same name when lowercase` — a direct consequence of
`CaseInsensitive: true`.

`--server-side-across-configs` (global; the `--onedrive-server-side-across-configs` form is
deprecated) enables cross-config server-side ops, but **only** for: two OneDrive **Personal** drives
where the files are *already shared* between them; or a user with permissions across
Business↔SharePoint **in the same tenant**; or SharePoint↔SharePoint in the same tenant.
Otherwise rclone falls back to a normal (slower) copy.

**UI implication:** within the user's single drive, drag-to-move and copy/paste are server-side and
effectively instant regardless of file size — the UI should show them as near-instant, not as
transfers with a progress bar. Any cross-account operation is a full round-trip through the local
machine; show a real progress bar there.

`--track-renames` (with `--track-renames-strategy hash|modtime|leaf`, default `hash`) will turn
detected renames into server-side moves during sync — worth enabling since quickxor is available on
both sides.

---

## 8. Trash / recycle bin — mostly **impossible**

### What rclone does on delete

```go
func (f *Fs) deleteObject(ctx context.Context, id string) error {
    var opts rest.Opts
    if f.opt.HardDelete {
        opts = f.newOptsCall(id, "POST", "/permanentDelete")
    } else {
        opts = f.newOptsCall(id, "DELETE", "")
    }
    ...
}
```

- **Default (`hard_delete=false`)**: plain `DELETE` → the item goes to the OneDrive **recycle bin**.
- **`hard_delete=true`**: `POST /items/{id}/permanentDelete`.
  **Personal accounts do not support the permanentDelete API** — the help text is explicit:
  *"OneDrive personal accounts do not support the permanentDelete API, it only applies to OneDrive
  for Business and SharePoint document libraries."* On the user's Personal drive this flag will
  fail. **Leave it off.**

### What is NOT possible

| Recycle-bin operation | Reachable? |
|---|---|
| List items in the recycle bin | ❌ No rclone command, no rc endpoint, no backend command |
| Restore an item from the recycle bin | ❌ |
| Empty the recycle bin | ❌ (`rclone cleanup` does **not** do this — see §9) |
| Know how much the bin uses | ⚠️ `about.trashed`, but it reported `0` on this Personal account |

The rclone docs state plainly that Microsoft provides no API for emptying the trash and direct the
user to Microsoft's own apps or the web interface.

Also: `rclone backend help onedrive` →
`NOTICE: Failed to backend: onedrive backend has no commands`
and `"Command": false` in the features. **There is no backend-specific escape hatch.**

### What OneDriveUI must do instead

1. **Emulate a local recycle bin.** On delete, `rclone move` the item to a hidden
   `.onedriveui-trash/<timestamp>/` folder **on the remote** rather than deleting it. That move is
   server-side and instant. Your UI then lists that folder as "Recycle bin", supports restore
   (another server-side move back), and "empty" (a real delete, which sends it to the *actual*
   OneDrive bin as a second safety net).
2. Provide a **"Open recycle bin on the web"** button that launches
   `https://onedrive.live.com/?id=recyclebin` — this is what the real client effectively falls back
   to for advanced cases anyway.
3. Never advertise a restore capability you can't deliver.

---

## 9. Version history — rclone can only **delete**, never list or restore

### `rclone cleanup` on onedrive deletes VERSIONS, not trash

This is the most commonly misunderstood point. The generic help for `cleanup` says *"Empty the trash
or delete old file versions"* — for onedrive it is **strictly the latter**. Verified in source:

```go
func (f *Fs) CleanUp(ctx context.Context) error {
    // walks the whole Fs, and for every object:
    err := o.deleteVersions(ctx)
    ...
}

func (o *Object) deleteVersions(ctx context.Context) error {
    opts := o.fs.newOptsCall(o.id, "GET", "/versions")
    var versions api.VersionsResponse
    ...
    if len(versions.Versions) < 2 { return nil }
    for _, version := range versions.Versions[1:] {   // keeps [0], the latest
        err = o.deleteVersion(ctx, version.ID)
    }
}

func (o *Object) deleteVersion(ctx context.Context, ID string) error {
    // honours --dry-run / --interactive via operations.SkipDestructive
    fs.Infof(o, "removing version %q", ID)
    opts := o.fs.newOptsCall(o.id, "DELETE", "/versions/"+ID)
    ...
}
```

So rclone **does** touch `GET /items/{id}/versions` internally — but **only to enumerate IDs it is
about to delete**. That result is never surfaced to any CLI command, rc endpoint, or backend command.

### Definitive verdict

| Version operation | rclone support |
|---|---|
| List a file's versions | ❌ Not exposed (used internally by `cleanup` only) |
| Restore a previous version | ❌ Not implemented at all |
| Delete all but the newest version | ✅ `rclone cleanup onedrive:path` |
| Auto-delete old versions on every write | ✅ `--onedrive-no-versions` (**Business/SharePoint only**) |

**⚠️ Do not set `no_versions` on this remote.** The help is explicit:
*"NB Onedrive personal can't currently delete versions so don't use this flag there."*
The user's `drive_type` is `personal`.

### Why versions matter even if you ignore them

OneDrive creates a **new version on every overwrite and on every mtime set**. Because rclone sets
mtime after upload, a plain `rclone copy` of a new file can consume **twice** the file's size in
quota on Business. `--onedrive-upload-cutoff` defaults to `off` specifically to mitigate this.

`rclone cleanup --interactive onedrive:path/subdir` previews what it would delete
(`--dry-run` also works, via `operations.SkipDestructive`).

### What OneDriveUI must do

**Emulate version history locally with `--backup-dir`.** On every sync that overwrites a remote file,
pass:

```
--backup-dir onedrive:.onedriveui-versions/$(date -u +%Y%m%dT%H%M%SZ) --suffix ""
```

rclone then *moves* (server-side, instant) the about-to-be-overwritten file into a timestamped
hierarchy instead of destroying it. Your "Version history" pane lists those directories for the
file's path; "Restore" is a server-side move back. This also happens to be the documented workaround
for the SharePoint *"item not found"* bug on replace/delete of Office and web files.

Offer a **"Free up space"** action that runs `rclone cleanup onedrive:` — but gate it behind a
confirmation, note that it is irreversible, and **disable it entirely on Personal drives** where
version deletion is unsupported.

---

## 10. Rate limits and throttling

### 10.1 What Microsoft enforces (SharePoint Online / OneDrive limits, Oct 2025 doc)

**Resource-unit cost per Graph request:**

| RU | Operations |
|---|---|
| 1 | Single-item query (get item); **delta with a token**; download file |
| 2 | Multi-item query (list children); create, update, delete, upload |
| 5 | **All permission operations**, including `$expand=permissions` |

**Per-user limits** (this is the one a desktop client will hit):

| Category | Type | Interval | Limit |
|---|---|---|---|
| User | Requests | 5 min | **3,000** |
| User | Ingress | 1 h | 50 GB |
| User | Egress | 1 h | 100 GB |
| User | Delegation token request | 5 min | 50 |
| User | External sharing emails | 1 h | 200 |

**Per-app-per-tenant** (for a 0–1,000-license tenant): 1,250 RU/min and 1,200,000 RU/24 h;
400 GB/h ingress and egress; 300 "specific sharing API" calls per 5 min.

Microsoft's own note: *"one user syncing a large amount of data across 10 machines at the same time
could trigger throttling"* and *"Running the OneDrive Sync client while also running migration
applications … can result in high request volumes that may trigger throttling."* Our client will
often run **alongside** other tools on the same account — budget conservatively.

**Behaviour on throttle:** HTTP **429** ("Too many requests") or **503** ("Server too busy"), always
with a **`Retry-After`** header in seconds. Critically: *"Throttled requests count towards usage
limits, so failure to honor Retry-After may result in more throttling."* Persistent abuse escalates
to a **block** (permanent 503 + a message in the tenant's Office 365 Message Center).

**SharePoint Online does *not* support IETF `RateLimit-*` headers** — only honour `Retry-After`.

### 10.2 What rclone does about it

From `shouldRetry` in `onedrive.go`:

```go
case 400:
    if apiErr.ErrorInfo.InnerError.Code == "pathIsTooLong" {
        return false, fserrors.NoRetryError(err)      // never retried
    }
case 401:
    // retried if Www-Authenticate contains "expired_token"
    // or the error text contains "Unable to initialize RPS"
case 429, 503:
    if values := resp.Header["Retry-After"]; len(values) == 1 && values[0] != "" {
        retryAfter, _ := strconv.Atoi(values[0])
        duration := time.Second * time.Duration(retryAfter)
        retry = true
        err = pacer.RetryAfterError(err, duration)     // honours Retry-After exactly ✅
    }
case 504:
    // one-shot warning: "upload chunks may be taking too long -
    //   try reducing --onedrive-chunk-size or decreasing --transfers"
case 507: // Insufficient Storage
    return false, fserrors.FatalError(err)             // fatal, never retried
```

Pacer constants:

```go
minSleep      = 10 * time.Millisecond
maxSleep      = 2 * time.Second
decayConstant = 2                       // bigger for slower decay, exponential
```

**Good news: rclone honours `Retry-After` exactly**, so you do not need to implement backoff.
**507 Insufficient Storage is fatal and never retried** — map that to a "Your OneDrive is full"
banner immediately. **`pathIsTooLong` is never retried** — map to a rename prompt.

### 10.3 The `--user-agent` trick

Microsoft prioritises "decorated" traffic. The required format:

| Type | User-Agent |
|---|---|
| ISV application | `ISV\|CompanyName\|AppName/Version` |
| Enterprise application | `NONISV\|CompanyName\|AppName/Version` |

rclone's own docs recommend, for excessive SharePoint throttling:

```
--user-agent "ISV|rclone.org|rclone/v1.55.1"
```

For OneDriveUI, set:

```
--user-agent "ISV|OneDriveUI|OneDriveUI/0.1.0"
```

(default is `rclone/v1.75.0`). Microsoft also asks that you register your own AppID/AppTitle for
best prioritisation — that means shipping your own `client_id` (§1.1), which additionally isolates
you from other rclone users' consumption of the shared app quota.

### 10.4 ⭐ Recommended safe defaults for a consumer desktop client

```
--tpslimit 10               # ≈3,000 req / 5 min is the user cap; 10/s = 3,000/5min exactly.
                            #   Use 8 to leave headroom for the browser/mobile app.
--tpslimit-burst 10         # default is 1, which is needlessly jerky for interactive use
--transfers 4               # default. 8 is fine on fast links; each costs chunk_size of RAM
--checkers 8                # default
--retries 3                 # default
--low-level-retries 10      # default
--retries-sleep 10s         # default is 0 (disabled); a small sleep is kinder after a 429 storm
--onedrive-chunk-size 10Mi  # default. Multiple of 320KiB, ≤250M. RAM = chunk_size × transfers
--user-agent "ISV|OneDriveUI|OneDriveUI/0.1.0"
--bwlimit 8M:off            # expose as a user setting; Windows' client has an identical control
```

Notes:
- `--tpslimit 8 --tpslimit-burst 10` is the single most valuable knob. It is what keeps you
  under the 3,000-per-5-minutes user cap even during a big initial scan.
- Lower `--onedrive-chunk-size` (and/or `--transfers`) if you see 504s.
- Keep `metadata_permissions=off` for normal browsing — permission ops cost **5 RU** each.
- Prefer **delta with a token** for scanning: Microsoft explicitly discounts it to **1 RU** as a
  reward for following their scan guidance. This is another argument for `delta = true` (§5.3).
- `--fast-list` reduces transaction count a lot at the cost of memory. With `delta=true` it is a
  strong win at the drive root.

---

## 11. Feature detection for other backends

### 11.1 The mechanism

Two equivalent forms — use whichever fits:

```console
rclone backend features <remote>:
```

```json
POST /operations/fsinfo   { "fs": "<remote>:" }
```

**Exact JSON shape** (verified live against `onedrive:`; `MetadataInfo` elided):

```json
{
  "Name": "onedrive",
  "Root": "",
  "String": "OneDrive root ''",
  "Precision": 1000000000,
  "Hashes": ["quickxor"],
  "Features": {
    "About": true,                     "BucketBased": false,
    "BucketBasedRootOK": false,        "CanHaveEmptyDirectories": true,
    "CaseInsensitive": true,           "ChangeNotify": true,
    "ChunkWriterDoesntSeek": false,    "CleanUp": true,
    "Command": false,                  "Copy": true,
    "DirCacheFlush": true,             "DirModTimeUpdatesOnWrite": false,
    "DirMove": true,                   "DirSetModTime": true,
    "Disconnect": false,               "DoubleSlash": false,
    "DuplicateFiles": false,           "FilterAware": false,
    "GetTier": false,                  "IsLocal": false,
    "ListP": true,                     "ListR": false,
    "MergeDirs": false,                "MkdirMetadata": true,
    "Move": true,                      "NoMultiThreading": false,
    "OpenChunkWriter": false,          "OpenWriterAt": false,
    "Overlay": false,                  "PartialUploads": false,
    "PublicLink": true,                "Purge": true,
    "PutStream": false,                "PutUnchecked": false,
    "ReadDirMetadata": true,           "ReadMetadata": true,
    "ReadMimeType": true,              "ServerSideAcrossConfigs": false,
    "SetTier": false,                  "SetWrapper": false,
    "Shutdown": true,                  "SlowHash": false,
    "SlowModTime": false,              "UnWrap": false,
    "UserDirMetadata": false,          "UserInfo": false,
    "UserMetadata": false,             "WrapFs": false,
    "WriteDirMetadata": true,          "WriteDirSetModTime": true,
    "WriteMetadata": true,             "WriteMimeType": false
  },
  "MetadataInfo": { "System": { "btime": {"Help": "...", "Type": "RFC 3339", "Example": "...", "ReadOnly": false}, ... }, "Help": "..." }
}
```

> ⚠️ `Name` comes back as `"onedrive{MxOuf}"` when you query through a daemon that has config
> overrides applied — the `{...}` suffix is a config hash. Strip it if you display it.

### 11.2 The gates the UI should check

| Feature key | Gate this UI element |
|---|---|
| `About` | Quota ring. `false` ⇒ hide it. |
| `PublicLink` | "Copy link" / Share menu. |
| `ChangeNotify` | Live remote updates. `false` ⇒ fall back to a manual Refresh button + timer. |
| `CleanUp` | "Free up space" / version pruning. |
| `Copy` / `Move` / `DirMove` | Instant drag-drop vs. progress-bar transfer. |
| `Purge` | Fast recursive folder delete. |
| `ListR` | Whether `--fast-list` helps. |
| `CaseInsensitive` | Rename-collision validation. |
| `UserInfo` | An "Account" pane fed by `rclone config userinfo`. |
| `ReadMetadata` / `WriteMetadata` | Properties dialog fields. |
| `Command` | Whether `rclone backend <cmd>` extras exist. |
| `SlowHash` | Whether to offer checksum comparison interactively. |
| `Hashes` (∩ with local) | Whether cheap checksum compare is possible at all. |
| `Precision` | Modtime comparison window. |

### 11.3 Degradation for other backends (verified `local` for contrast)

`rclone backend features /tmp` →
`Precision: 1`, and
`{'About': True, 'ChangeNotify': False, 'CleanUp': False, 'Copy': False, 'Move': True,
'DirMove': True, 'PublicLink': False, 'Purge': False, 'ListR': False, 'CaseInsensitive': False,
'UserInfo': False, 'ReadMetadata': True, 'WriteMetadata': True, 'ServerSideAcrossConfigs': False}`
with hashes `md5, sha1, whirlpool, crc32, sha256, sha512, blake3, xxh3, xxh128, dropbox, hidrive,
mailru, quickxor`.

Rough map of what breaks elsewhere:

- **Google Drive** — has `About`, `PublicLink`, `ChangeNotify`, `CleanUp` (empties trash, unlike
  onedrive!), `ListR`, and `UserInfo`. `DuplicateFiles: true` and case-**sensitive** names, so your
  collision logic must be conditional.
- **Dropbox** — `About`, `PublicLink`, `ChangeNotify` yes; hash is `dropbox` (its own), so a local
  compare still works because the local backend implements it.
- **S3 / bucket backends** — `BucketBased: true`, no `ChangeNotify`, no `About` on most, `PublicLink`
  varies, `CleanUp` only for aborting multipart uploads. Your folder tree must handle a flat
  namespace.
- **local** — no `ChangeNotify` in v1.75.0 (use `inotify`/`QFileSystemWatcher` yourself), no
  `PublicLink`, no `Purge`, no `CleanUp`, and `Copy: false` (there is no server-side copy).
- **crypt / chunker / union (wrappers)** — `WrapFs: true`, `Overlay: true`. Features are inherited
  from the wrapped remote but hashes usually collapse to none, killing cheap checksum compare.

**Rule for the codebase:** never hardcode `if backend == "onedrive"`. Call `operations/fsinfo` once
at remote-open, cache the `Features` dict, and gate every optional UI affordance on it.

---

## 12. Known issues, quirks and gotchas

1. **`rclone link --unlink` is a silent no-op on onedrive** (§4.2). Highest-severity trap in this doc.
2. **`rclone cleanup` deletes file versions, not the recycle bin** (§9).
3. **`mount/listmounts` returns `[]` for CLI-started mounts.** Verified live: the user has an active
   `rclone mount` yet `POST /mount/listmounts` → `{"mountPoints": []}`. The rc only tracks mounts it
   created via `mount/mount`. **If OneDriveUI wants to manage the mount through the rc, it must
   *start* it through the rc** (or track the process itself / via systemd --user).
4. **`UserInfo` is false for onedrive** — verified:
   `Error: OneDrive root '' doesn't support UserInfo`. You cannot get the account email or display
   name from rclone. Workarounds: read `created-by-display-name` metadata off any file the user owns
   (verified present), or capture it during the OAuth flow, or just ask.
5. **Personal Vault is a different drive.** Verified in the root listing: every normal item has
   `ID` prefixed `1A2B3C4D5E6F7890#...`, but `Personal Vault` has
   `ID: "b!36vy6FBBe0uBX-CYoi-ncsS3a3YkGd1KsWzrkFaMzuPdUikH0B3eSIRP3KItN3Ng#012HZH4I..."`.
   Expect locked/permission errors there and consider excluding it by default.
6. **`.Trash-1000` shows up in the remote root** (from the existing FUSE mount). Filter freedesktop
   trash directories out of the UI listing.
7. **"Shared with me" is not supported.** Workaround is for the user to add a shortcut to "My files".
   Also, change-polling is broken for shared folders because rclone builds the delta request with the
   *user's* drive ID rather than the folder owner's (rclone#6681).
8. **iOS Live Photos.** OneDrive stores the full video but serves only the still frame, so the
   downloaded `.heic` size differs from the reported size. Symptoms: endless re-copies, check
   failures, and mount read errors (`unexpected EOF`, `416 Requested Range Not Satisfiable`).
   Workaround `--ignore-size` (syncs the still only).
9. **SharePoint silently modifies uploaded files**, especially Office formats, changing size and
   hash after upload. Workaround: `--ignore-checksum --ignore-size`.
10. **"Item not found" on replace/delete** of Office and web files on SharePoint. Workaround:
    `--backup-dir` so rclone moves instead of deleting/replacing — which is what we want for version
    emulation anyway (§9).
11. **OneNote files are hidden by default**, and being hidden also **blocks deleting them**. Set
    `expose_onenote_files=true` if the UI must manage notebooks.
12. **Refresh tokens die after 90 days of non-use.** Remedy `rclone config reconnect remote:`.
    A background daemon that calls `about` periodically keeps the token alive naturally.
13. **`chunk_size` must be a multiple of 320 KiB and chunks are buffered in RAM.** Total RAM ≈
    `chunk_size × transfers`. The user's mount at `30M × 8` is ~240 MB of upload buffers.
14. **`--poll-interval` must be strictly smaller than `--dir-cache-time`**, or polling is useless.
15. `region=de` is deprecated — try `global` first.
16. **Never log `rclone config show` output.** Use `rclone config redacted` for diagnostics.

---

## 13. Command cookbook for OneDriveUI

```bash
# --- Read-only, safe to run any time ---
rclone about onedrive: --json
rclone backend features onedrive:
rclone lsjson onedrive: --max-depth 1
rclone lsjson onedrive:path --stat -M --hash --onedrive-metadata-permissions read
rclone config redacted onedrive
rclone config paths

# --- Sign in / re-auth (see §2.2 for the rc-driven version) ---
rclone authorize onedrive --auth-no-open-browser     # parse stderr for the link, stdout for the token

# --- Sharing ---
rclone link onedrive:path/to/file
rclone link --expire 168h onedrive:path/to/folder/

# --- Recommended daemon for the app ---
rclone rcd --rc-addr 127.0.0.1:5572 --rc-no-auth \
  --user-agent "ISV|OneDriveUI|OneDriveUI/0.1.0" \
  --tpslimit 8 --tpslimit-burst 10

# --- Recommended mount (Files On-Demand equivalent) ---
rclone mount onedrive: ~/OneDrive \
  --vfs-cache-mode full --vfs-cache-max-size 20G --vfs-cache-max-age 168h \
  --vfs-fast-fingerprint --vfs-read-ahead 128M \
  --dir-cache-time 24h --poll-interval 1m --attr-timeout 5s \
  --onedrive-delta --buffer-size 32M \
  --transfers 4 --checkers 8 --onedrive-chunk-size 10Mi \
  --tpslimit 8 --tpslimit-burst 10 \
  --user-agent "ISV|OneDriveUI|OneDriveUI/0.1.0" --umask 022

# --- Version-history emulation on sync ---
rclone sync ~/OneDrive onedrive: \
  --backup-dir "onedrive:.onedriveui-versions/$(date -u +%Y%m%dT%H%M%SZ)"

# --- DESTRUCTIVE, gate behind confirmation; disable on Personal ---
rclone cleanup --interactive onedrive:path/subdir     # deletes old VERSIONS, not trash
```

### Key rc endpoints

| Endpoint | Use |
|---|---|
| `core/version` | rclone version check at startup |
| `core/stats` (`{"group":..., "short":true}`) | Drive the transfer list / progress UI |
| `job/status` `{"jobid":N}` | Track any `_async: true` call |
| `config/listremotes` → `{"remotes":[...]}` | Account picker |
| `config/create` / `config/update` | Sign in / re-auth (§2.2) |
| `config/oauthstatus` / `config/oauthstop` | Get the `authUrl`; cancel the flow |
| `config/get` `{"name":...}` | ⚠️ returns the token in the clear |
| `operations/about` `{"fs":"onedrive:"}` | Quota ring |
| `operations/fsinfo` `{"fs":"onedrive:"}` | Feature detection (§11) |
| `operations/list` `{"fs","remote","opt":{recurse,showHash,metadata,filesOnly,...}}` → `{"list":[...]}` | File browser |
| `operations/stat` → `{"item": {...}\|null}` | Properties dialog |
| `operations/publiclink` `{"fs","remote","expire","unlink"}` → `{"url":...}` | Share (`unlink` ignored!) |
| `sync/sync` / `sync/copy` / `sync/move` / `sync/bisync` | Sync engine |
| `vfs/refresh` `{"fs","dir","recursive"}` | Force-refresh after a known change |
| `mount/mount` / `mount/unmount` / `mount/listmounts` | Mount control — **only sees rc-created mounts** |

Special params on **every** endpoint: `_async` (returns `jobid` + `executeId`), `_config` (per-call
global flags), `_filter` (per-call filter rules), `_group` (stats group — use one group per
user-visible operation so `core/stats` can report per-operation progress).

**Security:** *"Access to the rc API is equivalent to shell access as the user running rclone."*
Bind to `127.0.0.1` only. `--rc-no-auth` is acceptable **only** on loopback; otherwise use
`--rc-user`/`--rc-pass`.

---

## 14. Open questions / things to confirm during implementation

- Whether `about.trashed` ever becomes non-zero on this Personal account (it reads `0` today). If it
  stays 0, drop any recycle-bin size display.
- Exact behaviour of `metadata_permissions=read,write` when removing the last non-owner permission
  on a Personal drive — test on a throwaway file before wiring the "Stop sharing" button.
- Whether `--onedrive-delta` + `--fast-list` remains fast as the user's tree grows (2,836 entries
  listed in the current mount session per `core/stats.listed`).
- Whether starting the mount via `mount/mount` rather than the CLI is workable, given it is the only
  way `mount/listmounts` will report it.
