# OneDrive on Windows 11 — Exhaustive Feature Inventory

**Purpose.** This is the *spec source of truth* for OneDriveUI: an rclone-backed clone of the Microsoft
OneDrive sync client for Windows 11, on Linux (CachyOS/Arch, GNOME Shell 50.4 / Wayland),
Python 3.14 + PySide6 6.11.2, rclone v1.75.0 at `/usr/bin/rclone`, remote `onedrive:`.

**How to read this doc.** Every feature carries three tags:

| Tag | Meaning |
|---|---|
| **UI** | The exact Windows wording to reproduce. Strings marked `[verbatim]` are confirmed from Microsoft docs or Microsoft-sourced tutorials. Strings marked `[approx]` are reconstructed and should be treated as *best-effort* — do not invent extra words around them. |
| **Feasible** | `YES` / `PARTIAL` / `NO` on Linux + rclone. |
| **Mechanism** | The concrete rclone/Linux mechanism to implement it. |

**Terminology note.** Microsoft renamed the tray flyout several times. Current name: **Activity Center**
(older docs say "OneDrive flyout"). The gear button in it is called **"Help & Settings"** in most
Microsoft-derived docs and **"OneDrive Help and Settings"** in the newest support article. Use
**"Help & Settings"**.

---

## 0. Verified local environment facts (empirical, run on this machine)

```
rclone v1.75.0 / os cachyos / kernel 6.18.42-1-cachyos-lts / go1.26.5
rclone listremotes -> onedrive:
GNOME Shell 50.4
PySide6 6.11.2, Qt 6.11.2
nautilus 50.2.2-1, nautilus-python 4.1.0-3   <-- File Explorer clone is FEASIBLE
org.kde.StatusNotifierWatcher present on session bus  <-- tray icon is FEASIBLE (AppIndicator)
org.freedesktop.Notifications caps: actions, body, body-markup, icon-static, persistence, sound
```

`rclone rc` exposes **101 endpoints** in v1.75.0 (enumerated live via `rc/list`). Full list, since
implementation agents will need it:

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
pluginsctl/* (7)
rc/error rc/fatal rc/list rc/noop rc/noopauth rc/panic
serve/list serve/start serve/stop serve/stopall serve/types
sync/bisync sync/copy sync/move sync/sync
vfs/forget vfs/list vfs/poll-interval vfs/queue vfs/queue-set-expiry vfs/refresh vfs/stats
```

**Architecture implication.** Run one long-lived `rclone rcd --rc-addr 127.0.0.1:<port> --rc-no-auth
--rc-serve` (or with `--rc-user/--rc-pass`) as a `systemd --user` unit. The PySide6 GUI is a *pure
client* of that daemon: it never shells out per-operation. `core/stats` gives you the whole Activity
Center status line and progress rows in one poll.

---

## 1. Tray / notification-area icon

### 1.1 Base icon identity

| State | UI (verbatim) | Notes |
|---|---|---|
| Personal (Microsoft account) | **white cloud** | `[verbatim]` "The blue one is for your work or school account, the white one is for your personal account." |
| Work / school | **blue cloud** | Two separate tray icons appear when both accounts are signed in. |

The OneDrive glyph is the Fluent cloud: a wide, flat-bottomed cloud with three lobes. In Windows 11
the tray icon is drawn at 16×16 logical px in a 24×24 hit target, and the badge occupies the
bottom-right quadrant.

### 1.2 Every icon state

| # | State | Icon | UI meaning (verbatim from Microsoft) |
|---|---|---|---|
| 1 | **Synced / up to date** | plain cloud (white or blue), no badge | Hover tooltip: **"OneDrive — Up to date"** `[approx]`. Activity Center header: **"Your files are synced"** `[verbatim in client]`. |
| 2 | **Syncing** | cloud + circular sync arrows badge (animated rotation) | `[verbatim]` "The circular arrows over the OneDrive or OneDrive for work or school notification icons signify that sync is in progress. This includes when you are uploading files, or OneDrive is syncing new files from the cloud to your PC." |
| 3 | **Processing changes** | same sync-arrows icon | `[verbatim]` "OneDrive will also check for other file or folder changes and may show **Processing changes**." Distinct *status text*, same *icon*. |
| 4 | **Paused** | cloud + pause badge (two vertical bars) | `[verbatim]` "The paused symbol over the OneDrive or OneDrive for work or school icon means **your files are not currently syncing**." |
| 5 | **Signed out / setup incomplete** | **grey** cloud with a diagonal line through it | `[verbatim]` "A grayed-out OneDrive icon with a line through it means you're not signed in, or OneDrive setup hasn't completed." |
| 6 | **Sync error / attention** | cloud + **red circle with a white cross** | `[verbatim]` "A red circle with a white cross means that a file or folder cannot be synced." |
| 7 | **Warning / account needs attention** | cloud + **yellow warning triangle** | `[verbatim]` "…it means your account needs attention. Select the icon to see the warning message displayed in the activity center." |
| 8 | **Informational** | cloud + **blue circle with an "i"** | `[verbatim]` "OneDrive cloud icon with a blue circle with an 'i'". Non-blocking notice (e.g. a KFM prompt is waiting). |
| 9 | **Account blocked / frozen** | cloud + **red "no entry" circle (red circle with white horizontal bar)** | `[verbatim]` "If you see a red 'no entry' style icon over your OneDrive icon, it means your account is blocked." Raised when the account is over quota and frozen. |
| 10 | **Not running** | *no icon at all* | The clone must decide: OneDrive genuinely removes the tray icon. Recommended for us: keep the icon but use state 5 plus tooltip "OneDrive isn't running" `[approx]`, because a GNOME tray icon that vanishes is confusing. Make it a setting. |

There is no separate "offline/no network" icon; loss of connectivity surfaces as state 6/7 plus an
Activity Center banner.

### 1.3 Hover tooltip

Format is `OneDrive` or `OneDrive - <AccountLabel>` on line 1, then the status phrase.
Observed shapes (`[approx]` — Microsoft does not document tooltips):

```
OneDrive
Up to date

OneDrive - Personal
Syncing 42 files (1.2 GB of 3.4 GB)

OneDrive
Sync is paused

OneDrive
You're not signed in
```

Windows tray tooltips are capped at 127 UTF-16 chars; keep ours to two short lines.

### 1.4 Click behaviour

| Gesture | Windows 11 behaviour |
|---|---|
| **Left click** | Opens the **Activity Center** flyout, anchored above the tray icon, right-aligned to the icon. |
| **Right click** | Also opens the Activity Center (since the 2019 redesign the classic right-click context menu was **removed**). `[verbatim]` "Right-clicking the system tray icon will now open the Activity Center. … This Activity Center has completely replaced the right-click menu that you used to have earlier." |
| **Double click** | Opens the local OneDrive folder in File Explorer. |
| **Middle click** | No action. |

> **Design decision for OneDriveUI:** GNOME/AppIndicator gives you a *menu* on left-click, not an
> arbitrary popup, unless you use `SetXAyatanaSecondaryActivate` / `Activate`. Practical approach:
> register a StatusNotifierItem whose `Activate` (left click) raises our own frameless `Qt.Popup`
> window positioned near the pointer, and set a one-item fallback menu for shells that force a menu.

**Feasible: YES.** **Mechanism:** `QSystemTrayIcon` under the GNOME AppIndicator extension
(`org.kde.StatusNotifierWatcher` is present). Icons must be supplied as **themed icon names or
absolute file paths** — StatusNotifierItem cannot take pixmaps reliably through the GNOME extension,
so ship 10 pre-rendered SVG/PNG states (`odui-synced`, `odui-syncing-1..8` for the animation frames,
`odui-paused`, `odui-error`, `odui-warning`, `odui-info`, `odui-blocked`, `odui-signedout`) into
`~/.local/share/icons/hicolor/scalable/status/`. Animate by swapping icon names on a 125 ms
`QTimer` (8 frames = 1 s rotation) only while `core/stats` reports `transferring != []`.

---

## 2. Activity Center flyout

### 2.1 Geometry and chrome

- Width **360 px**, height variable, max ≈ **620 px** before the activity list scrolls.
- Corner radius **8 px** (Windows 11 Fluent flyout radius), 1 px border
  `#0000001A` light / `#FFFFFF14` dark, acrylic/mica backdrop.
- Anchored bottom-right, offset **12 px** from the taskbar and screen edge.
- Dismisses on focus-out, `Esc`, or clicking the tray icon again.
- Drop shadow: Windows 11 flyout shadow (blur 32, y-offset 8, 24 % black).

### 2.2 Header row

Left→right:

1. **Avatar** — 32 px circle. Profile photo, else initials on an accent-coloured disc.
2. **Account name** (bold, 14 px) — e.g. `Daniel X`. `[verbatim]` per MC333940: *"The account name
   will always be reflected in the title even during error scenarios to better reflect which OneDrive
   account has errors."*
3. **Account email / type** (secondary, 12 px) — `you@example.com` or `OneDrive - Personal` /
   `OneDrive - Contoso`.
4. **Account switcher** — if >1 account, a chevron opens a list of accounts; each row shows its own
   cloud colour (white=personal, blue=work).
5. **Settings (gear) button** — top-right corner. `[verbatim]` per MC333940: *"The settings entry
   point is now in the top right corner of the window."* Tooltip **"Help & Settings"**.

### 2.3 Status line — every state, exact wording

The status line sits directly under the header, with a 20 px status glyph on the left.

| Condition | Status text | Sub-text / secondary line |
|---|---|---|
| Everything synced | **"Your files are synced"** | — |
| Uploading/downloading | **"Syncing N files"** | `"Uploading X of Y (nn.n MB of nn.n GB)"` `[approx]`, plus an indeterminate-or-determinate progress bar |
| Single file | **"Syncing 1 file"** | filename |
| Enumerating/diffing | **"Processing changes"** `[verbatim]` | `"This might take a while"` `[approx]` |
| Initial reconciliation | **"We're checking all your files to make sure they are up to date on this personal computer. This might take a while if you have a lot of files."** `[verbatim]` | shown as a banner |
| Manually paused | **"Sync is paused"** / older wording **"Your files are not currently syncing"** `[verbatim]` | `"Syncing will resume in 1 hour 43 minutes"` `[approx]`, plus a **Resume** button |
| Auto-paused, metered | **"Sync is paused"** | banner: **"This PC is on a metered network"** `[verbatim]` with a **"Sync Anyway"** action `[verbatim]` |
| Auto-paused, battery saver | **"Sync is paused"** | banner: **"This PC is in battery saver mode"** `[approx]`, **"Sync Anyway"** |
| Not signed in | **"You're not signed in"** | **Sign in** button |
| One or more file errors | **"Sync issues"** | `"N files couldn't be synced"` `[approx]`, **"View sync problems"** link `[verbatim]` |
| Account needs attention | **"Action needed"** `[verbatim]` / **"Sign in required"** `[verbatim]` | context-specific action button |
| Over quota / frozen | **"Your OneDrive is full"** `[verbatim, from "My OneDrive says it's full"]` | **"Get more storage"** / **"Manage storage"** |
| Offline | **"OneDrive isn't connected"** `[approx]` | retry countdown |

> Implementation note: keep these strings in one `strings.py` table keyed by an enum
> `SyncState.{SYNCED, SYNCING, PROCESSING, PAUSED, PAUSED_METERED, PAUSED_BATTERY, SIGNED_OUT,
> ERRORS, ATTENTION, QUOTA_FULL, OFFLINE, NOT_RUNNING}`. The whole UI derives from that enum.

**Feasible: YES.** **Mechanism:** poll `rclone rc core/stats` every 500 ms while active, 2 s when
idle. Response shape (v1.75.0):

```json
{
  "bytes": 1048576, "checks": 12, "deletes": 0, "elapsedTime": 12.3,
  "errors": 0, "eta": 41, "fatalError": false, "renames": 0,
  "retryError": false, "speed": 2411000, "totalBytes": 104857600,
  "totalChecks": 40, "totalTransfers": 7, "transfers": 3,
  "transferring": [
    {"bytes": 524288, "dstFs": "onedrive:", "eta": 3, "group": "job/1",
     "name": "Documents/report.docx", "percentage": 50, "size": 1048576,
     "speed": 180000, "speedAvg": 175000, "srcFs": "/home/u/OneDrive"}
  ],
  "checking": ["Pictures/a.jpg"],
  "lastError": ""
}
```

`transferring[]` maps 1:1 onto the per-row progress bars. `errors` + `lastError` drive the
`ERRORS` state. Use `group` (`job/<id>`) with `job/status` to attribute transfers to a named job.

### 2.4 Storage quota bar

Directly under the status block:

- Text line: **"N.N GB of M GB used"** `[approx]` (Microsoft renders e.g. `4.8 GB of 5 GB used`).
- A 4 px-tall rounded progress bar, full width minus 16 px padding.
- Colour thresholds (Fluent): `< 80 %` accent blue `#0F6CBD`; `80–99 %` amber `#F7630C`;
  `>= 100 %` red `#C42B1C`.
- Trailing link **"Get more storage"** `[approx]` (personal) / **"Manage storage"** `[approx]`.
- At ≥90 %, a warning row appears above: **"You're running out of storage"** `[approx]`.

**Feasible: YES.** **Mechanism:** `rclone rc operations/about fs=onedrive:` →
`{"total":..., "used":..., "free":..., "trashed":...}` (bytes). Equivalent CLI:
`rclone about onedrive: --json`. Cache for 5 minutes; refresh on demand and after any large job.

### 2.5 Tabs / sections

The current Windows client has **no tab strip**; it is a single vertical stack:

```
┌ header (avatar, name, email, gear) ────────────┐
│ status glyph + status line                     │
│ [progress bar]                                 │
│ storage quota bar + "Get more storage"         │
├────────────────────────────────────────────────┤
│ error banner (only when errors exist)          │
├────────────────────────────────────────────────┤
│ "Recent activity"  section label               │
│  ▸ activity row                                │
│  ▸ activity row                                │
│  ▸ … (scrolls)                                 │
├────────────────────────────────────────────────┤
│ footer: [folder] [onedrive.com] [recycle bin]  │
│         [ ⋯ More ]                             │
└────────────────────────────────────────────────┘
```

On macOS 26 the redesign added an explicit "activity feed" panel one click away
(`[verbatim]` "The list of changes you've made to your OneDrive is just a click away… images and
videos that appear here show a helpful thumbnail of the content when available"). Adopt the
thumbnail behaviour — it is the direction of travel.

### 2.6 Recent-activity list — row anatomy

Each row is **48 px** tall, 12 px horizontal padding:

| Zone | Content |
|---|---|
| **Leading, 32 px** | File-type icon or **thumbnail** for images/video (rounded 4 px). For folders, a folder glyph. A small overlay badge in the corner shows per-item sync state (cloud / green check / sync arrows / red cross). |
| **Line 1** | **Filename** (ellipsised middle), 13 px, primary colour. |
| **Line 2** | **`<Verb> · <relative time>`** — verbs, `[approx]` but consistent with the client: `Uploaded`, `Downloaded`, `Modified`, `Created`, `Deleted`, `Renamed`, `Moved`, `Shared`, `Restored`, `Uploading…`, `Downloading…`. Relative time: `Just now`, `2 min ago`, `1 h ago`, `Yesterday`, `Mar 3`. |
| **Trailing (in-flight rows)** | A thin determinate progress bar under line 2 plus `"45%"` or `"2.1 MB of 4.8 MB"`; a **cancel (✕)** button on hover. |
| **Trailing (error rows)** | A red **error chip**: `Couldn't sync` / `Name isn't allowed` / `File too large` / `Not enough space` / `Blocked file type` `[approx]`. Clicking the chip opens the fix flow. |
| **Trailing (shared)** | small "people" glyph. |
| **Hover** | Row background tint; a kebab (⋯) appears with `Open`, `Open folder location`, `View online`, `Share`, `Version history`. |

Clicking a row opens the file with the default handler. Clicking the folder part opens the
containing folder.

**Feasible: YES.** **Mechanism:** three sources, merged:
1. **In-flight**: `core/stats.transferring[]` (name, percentage, bytes, size, speed, eta).
2. **Completed**: `core/transferred` returns `{"transferred":[{"name","size","bytes","checked",
   "started_at","completed_at","error","jobid","group","srcFs","dstFs"}]}` — this is your
   completed-activity feed, including the error string per item.
3. **Remote-side changes**: an inotify/`vfs/queue` feed for local, and periodic
   `operations/list` deltas or `--onedrive-delta` for remote. Persist a rolling SQLite table
   (`activity(id, path, verb, ts, bytes, state, error)`) capped at ~500 rows so the list survives
   restarts, which `core/transferred` alone does not (it is reset by `core/stats-reset`).

Thumbnails: generate locally with `QImageReader` + `setScaledSize` for local files; for online-only
files use the freedesktop thumbnail cache (`~/.cache/thumbnails/`) and fall back to a type glyph.

### 2.7 The "More" / kebab menu

Opened from the **⋯** in the footer (or the gear, depending on build). Items, in order:

```
Settings
Pause syncing            ▸  2 hours
                            8 hours
                            24 hours
Resume syncing                          (replaces "Pause syncing" while paused)
Unlock Personal Vault  /  Lock Personal Vault
Manage storage
View online
Send feedback
Report a problem
Help
Quit OneDrive
```

`[verbatim]` items confirmed: **"Settings"**, **"Pause syncing"**, **"Resume syncing"**,
**"Unlock Personal Vault"**, **"Lock Personal Vault"**, **"Quit OneDrive"**, **"Report a problem"**,
**"View Online"**, **"Recycle Bin"**. Quirk worth reproducing: `[verbatim]` *"If Personal Vault is
locked, click/tap on Pause syncing to expand it open, and click/tap on Quit OneDrive"* — i.e. the
menu reflows depending on vault state; **Quit OneDrive** shows a confirmation dialog whose button is
also labelled **"Quit OneDrive"**.

### 2.8 Footer buttons

Icon buttons, left-aligned, 32 px:

| Button | Tooltip | Action |
|---|---|---|
| Folder | **"Open folder"** | Opens the local sync root in the file manager. |
| Globe / cloud | **"View online"** `[verbatim]` | Opens `https://onedrive.live.com` (personal) in the browser. |
| Trash | **"Recycle bin"** `[verbatim]` | `[verbatim, MC333940]` "There is a new command **Recycle Bin** in the footer of the Activity Center which will direct the user to a browser window of the OneDrive … cloud recycle bin." |
| ⋯ | **"More"** | Section 2.7. |

**Feasible: YES.** Open folder → `xdg-open`/`QDesktopServices::openUrl`. View online / Recycle bin →
open the web URL (rclone has no recycle-bin API for OneDrive; `operations/cleanup` is *not*
implemented for the onedrive backend — do **not** wire it).

---

## 3. Settings window

Opened from Activity Center → gear → **Settings**, or from the file manager's OneDrive command-bar
gear. Modern layout (OneDrive 23.x and later): a **left navigation pane** with four items and a
scrolling right pane. Window ~**900 × 640 px**, resizable, Fluent Mica background.

Left pane items, in order — all `[verbatim]`:

1. **Sync and back up**
2. **Account**
3. **Notifications**
4. **About**

> **Legacy tabs** (pre-23.x, still referenced everywhere and worth supporting as an "old layout"
> reference): `Settings`, `Account`, `Backup`, `Network`, `Office`, `About`. The old dialog was a
> ~500 × 480 modal with an **OK** / **Cancel** pair; the new one applies immediately.

### 3.1 Sync and back up

#### 3.1.1 Back up important PC folders

- Section heading `[verbatim]`: **"Back up important PC folders to OneDrive"**
- Button `[verbatim]`: **"Manage backup"** (a few builds render **"Manage back up"**)
- Opens the **Manage folder backup** dialog:
  - Rows with a toggle each: **Desktop**, **Documents**, **Pictures**, **Music**, **Videos**
    `[verbatim — all five in current builds; older builds had only Desktop/Documents/Pictures]`
  - Each row shows the folder size and remaining quota.
  - Primary button `[verbatim]`: **"Save changes"**
  - After enabling: **"View sync progress"** `[verbatim]`
  - Turning a folder **off**: for Documents/Pictures/Music/Videos → confirm with **"OK"**;
    for **Desktop** → a radio choice, select **"This computer only"** then **"Continue"** `[verbatim]`.
    Then an optional feedback prompt with **"Submit"** / **"Close"**, then **"Close"**.
  - After stopping backup, a shortcut named **"Where are my files"** `[verbatim]` is left in the
    original folder pointing into the OneDrive folder.
- Semantics `[verbatim]`: *"the files in that folder (not folder) are moved to
  `C:\Users\<username>\OneDrive\<same folder name>`"*. i.e. **Known Folder Move (KFM)** relocates
  content and repoints the shell's known-folder registration.
- Policy names governing this: `KFMOptInWithWizard`, `KFMSilentOptIn`,
  `KFMSilentOptInDesktop/Documents/Pictures`, `KFMSilentOptInWithNotification`, `KFMBlockOptIn`
  (policy display name `BlockKnownFolderMove`), `KFMBlockOptOut`, `KFMForceWindowsDisplayLanguage`.
  The "Stop protecting" button in the *Set up protection of important folders* window is what
  `KFMBlockOptOut` disables — so that legacy dialog title is `[verbatim]`
  **"Set up protection of important folders"**.

**Feasible: YES (with a caveat).** **Mechanism:** on Linux the equivalent of KFM is XDG user
directories. Move `~/Desktop`, `~/Documents`, `~/Pictures`, `~/Music`, `~/Videos` into the sync root
and rewrite `~/.config/user-dirs.dirs` (`XDG_DESKTOP_DIR="$HOME/OneDrive/Desktop"` etc.), then
`xdg-user-dirs-update`. GNOME picks this up; some apps cache it until relogin — surface that as the
"you may need to sign out" note. Alternative, lower-risk: leave the real dirs in place and create
**symlinks** into the sync root; note that rclone follows symlinks only with `--links`/`-L`, so
prefer the real move.
*Caveat:* the "Where are my files" shortcut has no Linux analogue beyond a `.desktop` file or a
symlink named `Where are my files`.

#### 3.1.2 Personal-account extras

| Control | UI (verbatim) | Default | Effect |
|---|---|---|---|
| Camera upload | **"Save photos and videos from devices to OneDrive"** | **Off** | On removable-media insert where *every* file is a photo/video, import to `OneDrive/Pictures/Camera imports`. `[verbatim]` limits: files can't exceed **250 GB**; requires AutoPlay. |
| Screenshots | **"Save screenshots I capture to OneDrive"** (legacy: "Automatically save screenshots I capture to OneDrive") | **Off** | Screenshots go to `OneDrive/Pictures/Screenshots` *and* stay on the clipboard. `[verbatim]` limit: **10 GB** per screenshot. |

**Feasible: PARTIAL.**
- *Screenshots*: **YES** — watch `~/Pictures/Screenshots` (GNOME Shell's default) with inotify and
  copy into `<syncroot>/Pictures/Screenshots`. Better: change GNOME's screenshot directory, or add
  a `org.gnome.Shell.Screenshot` D-Bus watcher.
- *Camera imports*: **PARTIAL** — hook `udisks2` / `GVolumeMonitor` mount events, scan the volume,
  and if all files are image/video MIME types, import. AutoPlay parity is achievable via a
  `.desktop` file with `x-content/image-dcf` MIME association.

#### 3.1.3 Preferences

| Control | UI (verbatim) | Default | Effect | Registry key |
|---|---|---|---|---|
| Autostart | **"Start OneDrive when I sign in to Windows"** (legacy: "Start OneDrive automatically when I sign in to Windows") | **On** | Adds `HKCU\...\Run\OneDrive = "…\OneDrive.exe" /background`. Policy override `EnableAutoStart`. | `Run\OneDrive` |
| Battery saver | **"Pause syncing when this device is in battery saver mode"** | **On** | Auto-pause while battery saver is active; raises a toast with **"Sync Anyway"**. | `HKCU\Software\Microsoft\OneDrive\UserSettingBatterySaverEnabled` (1=on) |
| Metered network | **"Pause syncing when this device is in on a metered network"** *(sic — the shipped string contains "is in on"; Microsoft's own doc for the policy uses "…is on a metered network")* | **On** | Auto-pause on metered Wi-Fi/Ethernet; toast with **"Sync Anyway"**. | `HKCU\Software\Microsoft\OneDrive\UserSettingMeteredNetworkEnabled` |

Policy overrides: `DisablePauseOnBatterySaver`, `DisablePauseOnMeteredNetwork` (both under
`HKCU\SOFTWARE\Policies\Microsoft\OneDrive`); when enabled (=1) syncing *continues*.

**Feasible: YES.**
- Autostart → write `~/.config/autostart/onedriveui.desktop`, or better a `systemd --user` unit
  (`systemctl --user enable --now onedriveui.service`) plus `WantedBy=graphical-session.target`.
- Battery saver → `org.freedesktop.UPower` `OnBattery` property + GNOME's
  `org.gnome.settings-daemon.plugins.power` power-saver-profile, or `power-profiles-daemon`
  (`net.hadess.PowerProfiles.ActiveProfile == "power-saver"`).
- Metered → NetworkManager D-Bus: `org.freedesktop.NetworkManager.Metered`
  (`1 = yes`, `3 = guess-yes`). Watch `PropertiesChanged`.
- Acting on it → `rclone rc job/stop` or, cleaner, `core/bwlimit rate=off` won't pause; use a
  supervisor that suspends the sync job and sets state `PAUSED_METERED`.

#### 3.1.4 Advanced settings (collapsible — the label is a link, `[verbatim]` **"Advanced settings"**)

**(a) File collaboration**

| UI (verbatim) | Default | Effect |
|---|---|---|
| **"File collaboration"** section | — | Controls whether Office desktop apps handle co-authoring and how conflicts resolve. |
| **"Use Office applications to sync Office files that I open"** | On | Legacy label from the Office tab. |
| Sync conflicts: **"Let me choose to merge changes or keep both copies"** | selected | On conflict, prompt. |
| Sync conflicts: **"Always keep both copies (rename the copy on this computer)"** | — | Silently keep both, renaming the local copy. |

**Feasible: PARTIAL.** rclone has no co-authoring. Conflict *policy* is reproducible — see §9.

**(b) Bandwidth**

| Control | UI (verbatim) | Default | Range |
|---|---|---|---|
| **"Limit download rate"** toggle | Off | expands to a **"Limit to"** numeric field | **50 – 100,000 KB/s** |
| **"Limit upload rate"** toggle | Off | expands to radios **"Adjust automatically"** / **"Limit to"** | same |
| Legacy Network tab radios | **"Don't limit"** (default) · **"Adjust automatically"** · **"Limit to"** | | |

Semantics `[verbatim]`:
- **"Adjust automatically"** = *"enables the OneDrive sync client to upload data in the background by
  only consuming unused bandwidth and not interfere with other applications using the network"*;
  the admin doc pins this to **70 % of throughput**.
- **"Limit to"** = fixed KB/s. *"Any input lower than 50 KB/s sets the limit to 50 KB/s, even if the
  UI shows a lower value."*
- Admin percentage mode (`AutomaticUploadBandwidthPercentage`, **10–99**, recommended ≥50):
  *"The sync app periodically uploads without restriction for one minute and then slows down to the
  upload percentage you set."*

**Feasible: YES.** **Mechanism:** `rclone rc core/bwlimit rate=<value>` at runtime — accepts
`"off"`, `"1M"`, `"10M:1M"` (down:up), or a full bwlimit timetable. Note **rclone's `--bwlimit` unit
is KiB/s** while OneDrive's is **KB/s** — convert (`KB * 1000 / 1024`) or just label ours KiB/s and
document it. `--bwlimit-file` gives per-file limiting. "Adjust automatically" has no direct rclone
equivalent; implement it as a small controller that measures idle throughput and re-issues
`core/bwlimit` (target 70 % of measured capacity), sampling every 30 s and lifting the limit for
60 s each period to mimic Microsoft's burst behaviour.

**(c) Files On-Demand**

Current UI is **two buttons**, not a toggle:

| Button | UI (verbatim) | Effect |
|---|---|---|
| **"Free up disk space"** | turns Files On-Demand **on**; everything not pinned becomes online-only | confirmation button **"Continue"** |
| **"Download all files"** | turns Files On-Demand **off**; downloads the entire OneDrive folder | confirmation **"Continue"** (legacy: **"OK"**) |

Legacy label: a checkbox **"Save space and download files as you use them"** — still referenced in
Microsoft's own error-0x8007016A guidance. Legacy toggle: **"Files On-Demand"** on/off.
Default: **on** (build 23.066+ enables it by default). Policy: `FilesOnDemandEnabled`.

**Feasible: PARTIAL→YES.** See §5 for the full mapping.

**(d) Excluded file extensions**

- Section heading `[verbatim]`: **"Excluded file extensions"**
- Button `[verbatim]`: **"Exclude"** → a dialog with a field labelled **"Extension"** and a confirm
  button also labelled **"Exclude"**.
- Each excluded extension renders as a chip with an **X**; removing prompts **"Ok"**.
- `[verbatim]` *"Shortcut (`.lnk`) files are excluded by default."*
- `[verbatim]` *"Excluded file extension rules will not apply to files that were already backed up."*
- Policies: `EnableODIgnoreListFromGPO` (files), `EnableODIgnoreFolderListFromGPO` (folders).

**Feasible: YES.** **Mechanism:** maintain a filter file and pass `--exclude-from` /
`--filter-from` to sync jobs; at runtime `rclone rc options/set` can update
`{"filter": {"ExcludeRule": ["*.lnk","*.png"]}}`. Verify with `rclone rc options/get`.

### 3.2 Account

| Control | UI (verbatim) | Behaviour |
|---|---|---|
| Account header | avatar, display name, email, plan (`OneDrive Personal`, `Microsoft 365 Family`) | |
| Storage summary | **"N GB of M GB used"**, bar, **"Get more storage"** | |
| Add account | **"Add an account"** (some builds **"Add another account"**) | Launches the sign-in flow. `[verbatim]` limit: *"only one personal OneDrive account can be added"*; up to **nine** work/school accounts per device. |
| Unlink | **"Unlink this PC"** (a link, not a button) → confirm dialog with primary button **"Unlink account"** | `[verbatim]` *"You won't lose files or folders by removing an account. After unlinking, all your files will be available from OneDrive on the web."* Files marked **"Available on this device"** stay; files marked **"Available when online"** become web-only. Afterwards the **"Set up OneDrive"** window appears. |
| Selective sync | **"Choose folders"** button → dialog. Top checkbox **"Make all files available"**; below, a checkbox tree of top-level OneDrive folders with per-folder sizes; **OK** / **Cancel**. | `[verbatim]` *"If you uncheck a folder you are syncing to your computer, the folder will be removed from your computer. The folder and its contents will still be available online."* `[verbatim]` *"You cannot add non-OneDrive folders (such as C: and D:)."* Selections are **per-computer**. |
| Personal Vault | **"Lock Personal Vault after"** dropdown: **20 Minutes** (default) · **1 Hour** · **2 Hours** · **4 Hours** | Registry `HKCU\SOFTWARE\Microsoft\OneDrive\VaultInactivityTimeout` = `0/1/2/4`. |
| Folder location | (in Setup only) **"Change location"** link; policy `DisableCustomRoot` hides it | Default root `%userprofile%\OneDrive` or `OneDrive - {organization name}`. `CustomSyncRootFolderName` renames it. |

**Feasible: YES, except multi-account nuance.**
- Add/remove account → `rclone rc config/create` with `type=onedrive` and an OAuth token, or run
  `rclone authorize onedrive` and paste. Multiple accounts = multiple rclone remotes
  (`onedrive:`, `work1:`). This is *easier* than Windows: no one-personal-account limit.
- Unlink → `config/delete name=<remote>` plus tearing down the mount/sync job. **Never** delete the
  local folder.
- Choose folders → build the tree with `rclone rc operations/list fs=onedrive: remote="" opt='{"dirsOnly":true}'`
  (recurse lazily). Persist selections as `--filter` rules (`+ /Documents/**`, `- *`) applied to the
  sync job, and physically remove deselected local trees after a confirmation dialog.
- Storage → `operations/about`.

### 3.3 Notifications

All are individual toggles, **all On by default**:

| UI (verbatim / near-verbatim) | Trigger it gates | Registry |
|---|---|---|
| **"Notify me when syncing is paused"** | The metered-network / battery-saver auto-pause toasts | `HKCU\Software\Microsoft\OneDrive\UserSettingAutoPauseNotificationEnabled` |
| **"Notify me when others share with me or edit my shared items"** | Sharing + edit toasts | `HKCU\Software\Microsoft\OneDrive\Accounts\Personal\ShareNotificationDisabled` (1 = disabled) |
| **"Notify me when many files are deleted in the cloud"** | The mass-delete confirmation (§10) | gated further by policy `LocalMassDeleteFileDeleteThreshold` (default **200**) |
| **"Notify me when this day in history memories are available"** | OneDrive Photos "Memories" toast | |
| **"Notify me to load files from my other accounts to this PC"** | Prompt to sign in with a detected MSA/AAD credential; policy `DisableNewAccountDetection` | |

Legacy equivalents in the old **Settings** tab were checkboxes under a **Notifications** group,
including **"When sync pauses automatically"** and *(from policy text)* **"Notify me when many files
are deleted in the cloud"**.

**Feasible: YES.** **Mechanism:** `org.freedesktop.Notifications.Notify` over D-Bus (capabilities
verified: `actions`, `body`, `body-markup`, `icon-static`, `persistence`, `sound`). Because
`actions` is supported, the **"Sync Anyway"** / **"Restore files"** / **"Lock Personal Vault"**
buttons on toasts are reproducible. Use `QtDBus` directly rather than `QSystemTrayIcon.showMessage`
so you get action buttons and a stable notification id for replacement.

### 3.4 About

| Element | UI (verbatim) |
|---|---|
| Product line | **"Microsoft OneDrive"**, version/build, e.g. `Version 24.091.0505.0001` |
| Device identifier | **"OneDrive device ID"** with a copy affordance |
| Insider opt-in | **"Get OneDrive Insider preview updates before release"** checkbox — hidden entirely when policy `GPOSetUpdateRing` is set to Production (5) or Deferred (0) |
| Links | **"Release notes"** (the version number itself is the link), **"Privacy statement"**, **"Terms of use"**, **"Help"**, **"Send feedback"**, **"Report a problem"** |
| Update rings | Insiders (4) → Production (5, default) → Deferred (0) |

`[verbatim]` from the SilentConfig doc: *"Right-click the OneDrive icon in the notification area and
select **Report a problem**."*

**Feasible: YES.** Show `rclone version` output (`rclone rc core/version` →
`{"version":"v1.75.0","os":"linux","arch":"amd64","goVersion":"go1.26.5",...}`) plus our own app
version, a generated device id (`/etc/machine-id` hashed — never expose the raw value), and log
paths. "Report a problem" → open an issue template / copy diagnostics. There is no update ring;
replace with "Check for updates" or omit.

### 3.5 Settings surfaces we should *not* build

- **Office tab** co-authoring integration (no Office on Linux in-scope).
- **Update ring** selection.
- **Windows Information Protection / EDP** interactions.
- **Sync health reporting** (`EnableSyncAdminReports`).

---

## 4. First-run setup wizard (OOBE)

The wizard is a **single non-resizable window, ~500 × 350 px**, centred, with the OneDrive cloud
graphic at the top, a heading, body text, and one or two buttons bottom-right. Screens in order:

| # | Heading `[verbatim where marked]` | Body | Buttons |
|---|---|---|---|
| 1 | **"Set up OneDrive"** `[verbatim]` | "Put your files in OneDrive to get them from any device." + an **email address** field | **"Sign in"** `[verbatim]` (also labelled "Sign-in" in one build), **"Create account"** link |
| 2 | Microsoft sign-in page | password entry | **"Sign in"** |
| 3 | Two-step verification (conditional) | choose method (e.g. text), enter code | **"Send code"** `[verbatim]`, then **"Verify"** `[verbatim]` |
| 4 | **"Your OneDrive folder"** `[verbatim]` | shows the target path; **"Change location"** `[verbatim]` link | **"Next"** `[verbatim]` |
| 4b | "Use this folder?" (only if the folder already exists) | | **"Use this location"** `[verbatim]` — Microsoft's sync-problems doc also cites **"Use this folder"** and **"Choose new folder"** |
| 5 | **"Back up folders on this PC"** `[approx]` / legacy **"Protect your important folders"** | Toggles for **Desktop**, **Documents**, **Pictures** (+ Music, Videos in newer builds), each showing size | **"Start syncing"** `[verbatim]`, **"I'll do it later"** `[verbatim]` |
| 6 | Deleted-files reminder | "Deleted files are removed everywhere" explainer | **"Now now"** *(sic — this is the string the tutorial records; the intended string is **"Not now"**)* |
| 7 | **"Get to know your OneDrive"** `[verbatim]` — a 3–4 slide tutorial | slides cover: all your files are here · share files and folders · Files On-Demand icons meaning · get the mobile app | **"Next"** `[verbatim]` per slide |
| 8 | Mobile app promo | QR code / "we'll email you a link" | **"Later"** `[verbatim]` |
| 9 | **"Your OneDrive is ready for you"** `[approx]` | | **"Open my OneDrive folder"** `[verbatim]` |

Policies that alter the wizard: `DisableFREAnimation` (kills the intro animation),
`DisableFRETutorial` (skips screens 7–8), `DisableCustomRoot` (hides "Change location"),
`SilentAccountConfig` (skips 1–3), `DefaultRootDir` (pre-fills the path),
`DiskSpaceCheckThresholdMB` (default **500 MB**; above it, forces a "choose folders" step when
Files On-Demand is off).

**Feasible: YES.** **Mechanism:** OAuth via `rclone authorize onedrive` (spawns a local callback on
`127.0.0.1:53682`) or, better, embed the flow: run `rclone config create onedrive_new onedrive
config_refresh_token=false` non-interactively with `rclone rc config/create` and drive the
`config/oauthstatus` / `config/oauthstop` endpoints, opening the auth URL with
`QDesktopServices::openUrl`. Two-step verification happens inside Microsoft's web page — we never
see it, so screen 3 collapses into "a browser window opened; finish signing in there".

---

## 5. Files On-Demand

### 5.1 The three states

| State | Overlay icon | UI (verbatim) |
|---|---|---|
| **Online-only** | **blue cloud outline** (unfilled) | *"A blue cloud icon next to a OneDrive file or folder indicates that the file is only available online. Online-only files don't take up space on your computer. You see a cloud icon for each online-only file in File Explorer, but the file doesn't download to your device until you open it. You can't open online-only files when your device isn't connected to the Internet."* |
| **Locally available** | **white/hollow circle with a green outline and a green check** | *"When you open an online-only file, it downloads to your device and becomes a locally available file. You can open a locally available file anytime, even without Internet access. If you need more space, you can change the file back to online only. Just right-click the file and select Free up space."* Storage Sense can auto-dehydrate these. |
| **Always available** | **solid green circle with a white check mark** | *"Only files that you mark as **Always keep on this device** have the green circle with the white check mark. These always available files download to your device and take up space, but they're always there for you even when you're offline."* |

Additional benefits `[verbatim]`: *"See thumbnails of over 300 different file types even if you don't
have the required application installed to open it."*

### 5.2 Transitions

```
              open the file / "Always keep on this device"
 ONLINE-ONLY ──────────────────────────────────────────────► LOCALLY AVAILABLE ──► ALWAYS AVAILABLE
      ▲                                                             │  ▲               │
      │            "Free up space"  /  Storage Sense                │  │  uncheck      │
      └─────────────────────────────────────────────────────────────┘  └───────────────┘
```

- New files created online or on another device arrive as **online-only**.
- Marking a **folder** "Always keep on this device" makes **new files in that folder** download as
  always-available.
- Individual files inside an online-only folder can still be pinned.
- Turning Files On-Demand **off** ("Download all files") hydrates everything.

### 5.3 The underlying attributes (this is the exact semantics to clone)

Windows encodes the state in two file attributes, and this is the cleanest description of
hydrate/dehydrate available:

| State | `attrib` flags |
|---|---|
| Online-only (dehydrated) | `U` set (`FILE_ATTRIBUTE_UNPINNED` + `RECALL_ON_DATA_ACCESS`) |
| Locally available | neither `P` nor `U` |
| Always available (pinned) | `P` set (`FILE_ATTRIBUTE_PINNED`) |

Commands `[verbatim]`:
```
attrib +u "C:\Users\Brink\OneDrive\Document.docx"   # -> online-only  (dehydrate)
attrib -p "C:\Users\Brink\OneDrive\Document.docx"   # -> locally available
attrib +p "C:\Users\Brink\OneDrive\Document.docx"   # -> always available (hydrate + pin)
```

**Dehydrate** = replace the file's data with a sparse placeholder while keeping name, size, mtime and
thumbnail. **Hydrate** = fetch the data on first read; Windows blocks the reading process until the
data arrives (via the `CldFlt` "Windows Cloud Files Filter Driver", `HKLM\SYSTEM\CurrentControlSet\
Services\CldFlt\Start = 2`).

### 5.4 File Explorer right-click items

- **"Always keep on this device"** — a **checkable** item. Checked → pin. Unchecking → back to
  *locally available* (not online-only).
- **"Free up space"** — dehydrate to online-only.
- In Windows 11 build 26220.7271+ `[verbatim]`: *"Microsoft moved cloud provider options, like
  Always Keep on this Device and Free Up Space, into their relevant cloud provider flyout"* — i.e.
  they now live under the **OneDrive** submenu rather than the top level.

### 5.5 Linux reproduction

**Feasible: YES — this is the single most important design decision in the project.**

Two viable architectures:

**(A) VFS-mount architecture (recommended — true FOD parity).**
```bash
rclone mount onedrive: ~/OneDrive \
  --vfs-cache-mode full \
  --vfs-cache-max-size 20G \
  --vfs-cache-max-age 720h \
  --dir-cache-time 5m \
  --poll-interval 10s \
  --vfs-read-chunk-size 32M --vfs-read-chunk-size-limit 512M \
  --vfs-fast-fingerprint \
  --file-perms 0644 --dir-perms 0755 \
  --umask 022 --allow-non-empty=false
```
- FUSE (`fusermount3`, `/dev/fuse`) is available. `--vfs-cache-mode full` gives you *exactly* the
  hydrate-on-open semantics: the file appears with correct size and mtime, and data is fetched on
  first read.
- **Online-only** ⇔ not in the VFS cache.
- **Locally available** ⇔ present in `--cache-dir` (`~/.cache/rclone/vfs/onedrive/…`), evictable by
  `--vfs-cache-max-age` / `--vfs-cache-max-size`. This is the direct analogue of Storage Sense.
- **Always available (pinned)** ⇔ we maintain our own pin set and (i) pre-read the file to populate
  the cache and (ii) exclude it from eviction. rclone has **no pin API**, so implement pinning as:
  keep a `pins` table; on VFS cache pressure, re-hydrate pinned paths via a background
  `cat > /dev/null` read; and/or keep pinned trees in a *separate* `rclone sync`-managed real
  directory.
- **"Free up space"** ⇔ delete the file from the VFS cache dir and `rclone rc vfs/forget file=<path>`.
- **"Download all files"** ⇔ walk the tree and read every file (or use a second, non-mount
  bidirectional `bisync` root).
- Live cache introspection: `rclone rc vfs/stats` returns
  `{"diskCache":{"bytesUsed","erroredFiles","files","hashType","outOfSpace","path","pathMeta",
  "uploadsInProgress","uploadsQueued"}, "metadataCache":{...}, "inUse":N}` — feed the Files On-Demand
  UI and the "free up space" numbers from this.
- `rclone rc vfs/queue` + `vfs/queue-set-expiry` expose the pending-upload queue → per-file progress.

**(B) bisync architecture (real local files, no FUSE).**
```bash
rclone bisync ~/OneDrive onedrive: --resync   # first run only
rclone bisync ~/OneDrive onedrive: --check-access --conflict-resolve none \
  --conflict-loser pathname --conflict-suffix "-$(hostname)" --max-lock 15m
```
- Gives ordinary local files (best app compatibility, no FUSE weirdness), but **no online-only
  state** — everything is "always available". Files On-Demand becomes a no-op, so this mode should
  present the FOD section as "Download all files" only.

**Recommendation:** ship (A) as default with (B) selectable as "Download all files" mode. That maps
one-to-one onto Microsoft's own two buttons ("Free up disk space" vs "Download all files").

**Gotcha:** rclone's VFS cache is *not* sparse-file based; a dehydrated file consumes 0 bytes of
cache but the mount reports the real size, which is what we want. However `du` on the mount reports
apparent size, and GNOME's disk-usage displays will look wrong. Document it.

---

## 6. File Explorer integration

### 6.1 Status overlay icons (complete list)

| Overlay | Meaning (verbatim) |
|---|---|
| Blue cloud outline | Online-only |
| Green outline circle + green check | Locally available |
| Solid green circle + white check | Always keep on this device |
| Blue cloud + **person** | Shared, online-only |
| **"People" icon** | *"the file or folder has been shared with other people"* |
| **Red circle with white cross** | *"a file or folder cannot be synced"* |
| **Sync-pending arrows** | queued for sync |
| **Padlock** | *"OneDrive will show a padlock icon next to the sync status if the file or folder has settings which prevent it from syncing."* |
| **Grey circle with a flat bar** | *"Files with this icon won't sync"* — admin blocked this file type (commonly Outlook `.pst`) |
| **Chain / link on a folder** | *"a shortcut to another folder that has been shared"* (i.e. "Add shortcut to OneDrive") |

Non-OneDrive icons users confuse with these (document so support text can disambiguate): grey X on
desktop shortcuts (corrupt shortcut cache), brown box overlay (Explorer glitch), stray shortcut arrow.

### 6.2 The "Status" column

- File Explorer's Details view exposes a **`Status`** column; it renders the same overlay glyph plus
  no text.
- The navigation-pane variant is controlled by Folder Options → View →
  **"Always show availability status"** `[verbatim]`, default **on**
  (`HKCU\...\Explorer\Advanced\NavPaneShowAllCloudStates = 1`).

### 6.3 Navigation-pane entry

- A top-level **OneDrive** node (with the cloud icon) above "This PC".
- Personal shows as **OneDrive – Personal**; work/school as **OneDrive – {Organization}**.
- Registry-controlled via the shell namespace CLSID `{018D5C66-4533-4307-9B53-224DE2ED1FE6}`
  (`System.IsPinnedToNameSpaceTree`).

### 6.4 Context-menu entries (OneDrive submenu)

Windows 11 groups them under a **OneDrive** flyout in the "Show more options" / new context menu:

```
OneDrive ▸
   Share
   View online
   Manage access
   Version history
   Always keep on this device        (checkable)
   Free up space
   Lock Personal Vault / Unlock Personal Vault   (Personal Vault folder only)
   Move to OneDrive                  (for files outside the sync root)
```

Top-level (outside the submenu, when present): **Share**, **Free up space**,
**Always keep on this device**, **View online**.

### 6.5 Linux reproduction

**Feasible: YES — verified `nautilus-python 4.1.0` is installed.**

Write a Nautilus 4 extension at `~/.local/share/nautilus-python/extensions/onedriveui.py`:

```python
import gi
gi.require_version("Nautilus", "4.0")
from gi.repository import GObject, Nautilus

class OneDriveUI(GObject.GObject,
                 Nautilus.InfoProvider,      # emblems + the Status column value
                 Nautilus.ColumnProvider,    # the "Status" column itself
                 Nautilus.MenuProvider):     # the OneDrive submenu

    def get_columns(self):
        return [Nautilus.Column(name="OneDriveUI::status",
                                attribute="onedrive_status",
                                label="Status",
                                description="OneDrive sync status")]

    def update_file_info_full(self, provider, handle, closure, file):
        # ask the daemon over a unix socket; return IN_PROGRESS and complete async
        state = query_daemon(file.get_location().get_path())   # non-blocking cache lookup
        file.add_string_attribute("onedrive_status", state.label)
        file.add_emblem(state.emblem)     # "odui-online-only" | "odui-local" | "odui-pinned" ...
        return Nautilus.OperationResult.COMPLETE

    def get_file_items(self, files):
        top = Nautilus.MenuItem(name="OneDriveUI::root", label="OneDrive")
        menu = Nautilus.Menu(); top.set_submenu(menu)
        for name, label, cb in [...]:
            item = Nautilus.MenuItem(name=f"OneDriveUI::{name}", label=label)
            item.connect("activate", cb, files)
            menu.append_item(item)
        return [top]
```

Notes and gotchas:
- **Emblems, not overlays.** Nautilus draws emblems in the bottom-right of the icon — visually very
  close to Windows' overlay position. Install custom emblems as icon-theme entries named
  `emblem-odui-*` under `~/.local/share/icons/hicolor/scalable/emblems/` (a `.icon` file next to
  each SVG is *not* needed for `add_emblem`, but the icon name must resolve in the current theme).
- Nautilus **caches** `update_file_info` results aggressively. To push a state change, the daemon
  must `touch` nothing — instead call `Nautilus.FileInfo.invalidate_extension_info()` on tracked
  files, which requires keeping weak refs. Practical approach: keep a dict of
  `path -> Nautilus.FileInfo` and invalidate on daemon push.
- Use `update_file_info_full` + `Nautilus.OperationResult.IN_PROGRESS` +
  `Nautilus.info_provider_update_complete_invoke(...)` for anything that can block. **Never** do a
  network call synchronously — it freezes the file manager.
- The daemon↔extension channel should be a **Unix socket or D-Bus**, not the rclone RC port, so the
  extension has one tiny dependency.
- The **navigation-pane entry** ⇔ add a GTK bookmark: append
  `file:///home/<user>/OneDrive OneDrive - Personal` to `~/.config/gtk-3.0/bookmarks` (and
  `gtk-4.0`). Nautilus shows it in the sidebar with a custom icon if you set the folder's
  `metadata::custom-icon` via `gio set`.
- Other file managers (Dolphin, Thunar, Nemo) need separate plugins — treat as out of scope but
  note that KDE's `KFileItemActionPlugin` + `KOverlayIconPlugin` is the Dolphin equivalent.

---

## 7. Sharing

### 7.1 The Share dialog

Reached from the OneDrive context submenu → **Share**. Structure:

```
┌ Share "<name>"  ────────────────────────────────── [x] ┐
│  🔗 Anyone with the link can edit            ▾         │   <- link-settings chip (clickable)
│  ┌───────────────────────────────────────────────────┐ │
│  │ Name, group or email                              │ │
│  └───────────────────────────────────────────────────┘ │
│  ┌───────────────────────────────────────────────────┐ │
│  │ Message                                           │ │
│  └───────────────────────────────────────────────────┘ │
│                                        [   Send   ]    │
│ ────────────────────────────────────────────────────── │
│  Copy link                                  [ Copy ]   │
│ ────────────────────────────────────────────────────── │
│  Shared with   (o)(o)(o)                               │
└────────────────────────────────────────────────────────┘
```

`[verbatim]` strings: **"Send link"**, **"Copy link"**, **"Anyone with the link can edit"**,
**"Name, group or email"**, **"Message"**, **"Send"**, **"Copy"**, **"Shared with"**,
**"More settings"**, **"Apply"**, **"Manage Access"**.

### 7.2 Link settings sheet (opened from the chip)

| Control | UI (verbatim) | Notes |
|---|---|---|
| Audience radio | **"Anyone"** | *"share items with lots of people you might not even know personally… Anyone who gets the link can view or edit the item"* |
| | **"Specific people"** | requires each recipient to sign in |
| | *(work/school also has)* **"People in {Organization}"**, **"People with existing access"** | |
| Permission | **"Can edit"** / **"Can view"** | Edit = *"copy, move, edit, rename, share, and delete anything they have access to."* View = *"view, copy, or download your items without signing in. They can also forward the link"* |
| Expiry | **"Set expiration date"**, entered as **MM/DD/YYYY** | `[verbatim]` *"This setting is only available with OneDrive Premium."* After the date the link stops working. |
| Password | **"Set password"** | `[verbatim]` *"When a user clicks the link, they will be prompted to enter a password… You'll need to provide this password separately."* Premium only. |
| Block download | **"Block download"** toggle | only meaningful with **Can view**. |
| Confirm | **"Apply"** | |

Post-send confirmation: a "link sent" state in the dialog.

### 7.3 Manage access

Reached from context menu → OneDrive → **"Manage access"**.

- Two tabs: **"Links"** and **"People"** `[verbatim]`.
- **Links** tab: each existing link row with its audience/permission summary and a
  **Remove link (trash-can) button** → confirm **"Remove"** `[verbatim]`.
- **People** tab: person rows; selecting one expands **"Specific people with this link"** →
  **"This link works for"** → per-person **Remove (X)** → confirm **"Remove"** `[verbatim]`.
- **"Stop sharing"** removes all access.

### 7.4 Linux reproduction

**Feasible: PARTIAL.**

| Windows feature | rclone equivalent | Verdict |
|---|---|---|
| Copy link | `rclone rc operations/publiclink fs=onedrive: remote=<path>` → `{"url":"https://1drv.ms/..."}`; CLI `rclone link onedrive:path` | **YES** |
| Anyone / Specific people | `--onedrive-link-scope` = `anonymous` (default) \| `organization` \| `users` | **PARTIAL** — `users` exists but rclone gives no recipient list, so "Specific people" can't be targeted at named addresses |
| Can edit / Can view | `--onedrive-link-type` = `view` (default) \| `edit` \| `embed` | **YES** |
| Password | `--onedrive-link-password` | **YES** (Premium-gated server-side) |
| Expiry | `rclone link --expire 7d` / `operations/publiclink` with `expire` | **YES** — rclone: *"If you supply the --expire flag, it will set the expiration time otherwise it will use the default (100 years)."* |
| Remove link | `rclone link --unlink onedrive:path`; `operations/publiclink` with `unlink=true` | **YES** |
| Email invite / Send link | — | **NO.** Graph `invite` is not exposed by rclone. Fall back to composing a `mailto:` with the copied link, or mark the Send tab as unavailable. |
| Manage access (enumerate links/people) | `--onedrive-metadata-permissions read` exposes permissions as metadata on `lsjson --metadata` | **PARTIAL** — you can *list* permissions and, with `read,write`, set them; but this is metadata-level, not a first-class API. Expect rough edges. |
| Block download | — | **NO.** Not exposed. |
| "Shared with" avatars | from the permissions metadata (display names only) | **PARTIAL** |

Because these flags are backend options, set them per-call at runtime with
`rclone rc options/set` on the backend block, or create dedicated remotes
(`onedrive_edit:` with `link_type = edit`) — the latter is simpler and avoids race conditions
between concurrent share requests.

---

## 8. Version history, Recycle bin, and "Restore your OneDrive"

### 8.1 Version history dialog

- Entry point: File Explorer → right-click a **single** file → **OneDrive** → **"Version history"**
  `[verbatim]`.
- Dialog lists all available versions, newest at the top (the current version is the top row).
- Per-version right-click menu `[verbatim]`: **"Restore"**, **"View online"**, **"Delete version"**.
- `[verbatim]` *"If you select to restore a previous file version, it will become the current version
  (top). The previous current version will now become a previous version in the list."*
- `[verbatim]` *"If you sign in with a personal Microsoft account, you can retrieve the last **25
  versions**. If you sign in with a work or school account, the number of versions will depend on
  your library configuration."*
- Web equivalent: select file → **"More commands (3 dots)"** → **"Version history"**; per-version
  **"Show more actions for this item"** → **"Open File"**, **"Restore"**, **"Delete Version"**.

**Feasible: NO (natively).** rclone exposes **no version-history API** for OneDrive. The only
related flag is `--onedrive-no-versions` (*"Remove all versions on modifying operations"*), which
deletes versions rather than reading them.
**Workaround:** deep-link the dialog to the web UI
(`https://onedrive.live.com/?…&id=<itemid>` → Version history), or implement version listing with a
direct Microsoft Graph call (`GET /me/drive/items/{id}/versions`) reusing the token from
`rclone config dump` — mark this as an *optional* out-of-rclone path and be explicit in the code
that it bypasses rclone.

### 8.2 Recycle bin

- Activity Center footer command **"Recycle Bin"** `[verbatim]` → opens the *cloud* recycle bin in a
  browser.
- Retention `[verbatim]`: personal accounts **30 days**; work/school and SharePoint **93 days**.
- Deleting an **online-only** file deletes it from OneDrive and from every synced device.

**Feasible: PARTIAL.** rclone cannot list or restore the OneDrive recycle bin
(`operations/cleanup` is unimplemented for this backend). Open the web recycle bin. Locally, honour
the freedesktop Trash spec (`~/.local/share/Trash`) for the local side so users get an undo.

### 8.3 "Restore your OneDrive" (point-in-time restore)

- `[verbatim]` path: personal Microsoft 365 → **Settings** → **Options** → **"Restore your OneDrive"**
  in the left nav. Work/school → **Settings** → **"Restore OneDrive"**.
- Window: **the last 30 days**. `[verbatim]` *"undo all the actions that occurred on any files and
  folders within the last 30 days."*
- UI: a **"Select a date"** dropdown — confirmed options include **"Yesterday"** and
  **"Custom date and time"** `[verbatim]`; Microsoft's UI historically also offered
  **"One week ago"** and **"Three weeks ago"** `[approx — treat as likely but unconfirmed]`.
- Below: a **daily activity chart** covering the last 30 days, a **slider** under the chart to jump
  to a date, and an **activity feed** in reverse-chronological order with expand/collapse arrows per
  day.
- Primary button: **"Restore"** `[verbatim]`.
- `[verbatim]` caveats: *"If a file has been permanently deleted from your OneDrive Recycle Bin, it
  can never be recovered."* · *"When restoring, any files or folders created after the Restore point
  date will be sent to your OneDrive Recycle Bin."* · *"Albums are not restored."*

**Feasible: NO.** There is no rclone or Graph-lite path to bulk point-in-time restore. Deep-link to
the web page. **Mark clearly as infeasible in the product.**

---

## 9. Conflict handling

### 9.1 Naming of conflicted copies

- OneDrive keeps both versions and **renames the local copy by appending the computer name**:
  `MyFile.docx` → **`MyFile-LaptopName.docx`**.
- Office co-authoring conflicts may instead produce `MyFile (conflicted copy YYYY-MM-DD).docx`-style
  names in some paths; the computer-name form is the documented sync-client behaviour.

### 9.2 The "Keep both" UX

Dialog appears when a file changed on both sides:

| Element | UI (verbatim / near) |
|---|---|
| Title | **"Sync conflict"** `[approx]` |
| Body | explains both copies changed |
| Options | **"Keep both copies"** · **"Replace"/"Keep the version on this PC"** · **"Merge changes"** (Office only) |
| Setting that governs it | **"Let me choose to merge changes or keep both copies"** (default) vs **"Always keep both copies (rename the copy on this computer)"** `[verbatim]` |

`[verbatim]` note: *"If you made changes to files when they were not syncing, OneDrive will not
attempt to overwrite the older files with the newer ones."*

### 9.3 Sync-issues list and "Fix problems"

- The Activity Center status becomes **"Sync issues"** with a **"View sync problems"** link
  `[verbatim]`.
- The list shows one row per problem file with an error chip. Typical error categories and their
  fix flows:

| Error | UI text `[approx unless noted]` | Fix flow |
|---|---|---|
| Invalid characters | "The names of some items contain characters that prevent syncing" `[verbatim-ish, this is a real OneDrive error title]` | **Rename** inline |
| Path too long | "The file path is too long" | Rename / move |
| File too large | "This file is too big to upload" (>250 GB) | — |
| Blocked file type | "Files with this icon won't sync" `[verbatim]` | — |
| Not enough space | "There isn't enough space on your PC" | **"Free up space"** |
| Quota exceeded | "Your OneDrive is full" `[verbatim]` | **"Get more storage"** / delete |
| Locked by another process | "The file is in use" / error `0x8007018B` | Close the app, **"Retry"** |
| Permissions lost | "You no longer have permission to this folder" | **Remove** the folder shortcut |
| Merge/conflict | "Two versions of this file exist" | **"Keep both"** |
| Access denied | "Sign in required" `[verbatim]` | **"Sign in"** |

- Buttons in the fix flow: **"Retry"**, **"Keep both"**, **"Rename"**, **"Ignore"** /
  **"Stop syncing this item"**, **"Free up space"**, **"Get more storage"** `[approx]`.

### 9.4 Linux reproduction

**Feasible: YES.** `rclone bisync` (v1.75.0) has first-class conflict controls that map almost
exactly onto the Windows semantics:

```
--conflict-resolve none|newer|older|larger|smaller|path1|path2
--conflict-loser  numbered|pathname|delete
--conflict-suffix <str>[,<str2>]     e.g. --conflict-suffix "-$(hostname)"
```

- **"Always keep both copies (rename the copy on this computer)"** ⇔
  `--conflict-resolve none --conflict-loser pathname --conflict-suffix "-$(hostname -s)"`, which
  produces exactly `MyFile-hostname.docx`.
- **"Let me choose…"** ⇔ set `--conflict-resolve none` and surface a dialog before the next bisync
  run, then apply the user's choice by renaming/deleting locally.
- Filename validation ⇔ pre-flight check against the OneDrive restriction set (§13.6) and raise the
  "invalid characters" row *before* the transfer errors out. This is a strictly better UX than
  waiting for the server to reject.
- Per-item errors ⇔ `core/transferred[].error` and `core/stats.lastError`; retries via
  `--retries`, `--low-level-retries`, `--retries-sleep`.
- **Gotcha:** bisync aborts the whole run on "too many deletes" unless `--force` or
  `--max-delete <n>` is given (default `--max-delete 50` %). Surface that abort as the mass-delete
  confirmation dialog rather than an opaque failure.

---

## 10. Notifications (every toast and its trigger)

| # | Toast | Trigger | Actions | Governing setting/policy |
|---|---|---|---|---|
| 1 | **"Deleted files are removed everywhere"** `[verbatim]` — first-delete education dialog | The user's **first** delete from the sync folder | **"Don't show this reminder again"** `[verbatim]` | `DisableFirstDeleteDialog`; team-site variant reads **"Deleted files are removed for everyone"** `[verbatim]` |
| 2 | **Mass-delete confirmation** — "Did you delete these files?" `[approx]`, e.g. *"Delete these 13888 items?"* `[verbatim shape]` | >**200** items deleted locally in a short window (default) | **"Delete them"** / **"Restore files"**, plus **"Always remove files"** checkbox `[verbatim]` | `LocalMassDeleteFileDeleteThreshold` (0–100000, default 200); `ForcedLocalMassDeleteDetection` makes it non-dismissible and *"If a user doesn't confirm a delete operation within seven days, the files aren't deleted."* `[verbatim]` |
| 3 | **Shared-content delete confirmation** — *"Delete Shared Item?"* `[verbatim]` | Deleting locally-synced content that others have access to | Confirm / Cancel; *"After confirming, deletes of other shared content, for a short period of time, do not trigger additional confirmations."* `[verbatim]` | `SharedContentDeleteConfirmation` |
| 4 | **Auto-pause: metered** — *"This PC is on a metered network"* `[verbatim]` | NIC reports metered | **"Sync Anyway"** `[verbatim]` | Notifications → "Notify me when syncing is paused"; policy `DisablePauseOnMeteredNetwork` |
| 5 | **Auto-pause: battery saver** | Battery saver on | **"Sync Anyway"** | same toggle; `DisablePauseOnBatterySaver` |
| 6 | **Sharing** — *"<Person> shared '<file>' with you"* / *"<Person> edited '<file>'"* `[approx]` | Someone shares or edits a shared item | **"Open"**, **"View online"** | "Notify me when others share with me or edit my shared items" / `ShareNotificationDisabled` |
| 7 | **Sign-in required** — *"Sign in required"* / *"Action needed"* `[verbatim status strings]` | Token expired, password changed, MFA | **"Sign in"** | — |
| 8 | **New-account detection** — prompt to sign in with a detected MSA | Windows sees an MSA/AAD credential | **"Sign in"**, **"Not now"** | `DisableNewAccountDetection`; user toggle "Notify me to load files from my other accounts to this PC" |
| 9 | **KFM prompt** — *"Set up protection of important folders"* `[verbatim window title]` / **"Your important folders aren't backed up"** `[approx]` | KFM eligible, not opted in | **"Start backup"**, **"Stop protecting"** | `KFMOptInWithWizard`; reminder repeats in the Activity Center until all folders move |
| 10 | **KFM success** | Silent KFM completed | — | `KFMSilentOptInWithNotification=1` |
| 11 | **Low disk space warning** | Download would drop free space below the threshold | *"Users are prompted with options to help free up space."* `[verbatim]` | `WarningMinDiskSpaceLimitInMB` (0–10240000) |
| 12 | **Download blocked, low disk** | Free space below hard floor | — | `MinDiskSpaceLimitInMB` |
| 13 | **Storage nearly full** — *"You're running out of storage"* `[approx]` | ≥ ~90 % quota | **"Get more storage"**, **"Manage storage"** | — |
| 14 | **Storage full** — *"Your OneDrive is full"* `[verbatim, article title "My OneDrive says it's full"]`; email variants *"Your storage is full, and your files will be erased on [date]"* `[verbatim shape]` | Over quota | **"Get more storage"** | after 6 months over quota, files may be deleted and are non-recoverable |
| 15 | **Account frozen / blocked** | Over quota + grace expired, or ToS violation | **"Learn more"** | red "no entry" tray badge |
| 16 | **Personal Vault about to lock** — *"Still Using Your Personal Vault"* `[verbatim]` | 5 minutes before the inactivity timeout | **"Lock Personal Vault"** `[verbatim]` | `VaultInactivityTimeout` |
| 17 | **Personal Vault locked** — *"Personal Vault Locked"* `[verbatim]` | Vault locked (manual or auto) | — | — |
| 18 | **File locked / in use** — error `0x8007018B` | Deleting/moving/renaming a file OneDrive is using | **"Retry"** | — |
| 19 | **Sync complete** | Large job finished | **"Open folder"** | — |
| 20 | **Memories / "This day in history"** | OneDrive Photos memory available | **"View"** | "Notify me when this day in history memories are available" |
| 21 | **Sync errors** — *"We couldn't sync N files"* `[approx]` | ≥1 file error | **"View sync problems"** | — |
| 22 | **Folder shortcut permission loss** | Lost access to an "Add shortcut to OneDrive" folder | **"Remove"** | `AddedFolderUnmountOnPermissionsLoss` |
| 23 | **Files will be erased (inactivity)** — *"Your files will be erased on [date]"* `[verbatim shape]` | Account inactive 2 years | **"Sign in"** | — |

**Feasible: YES for all except the ones whose trigger doesn't exist on Linux** (metered/battery
triggers *do* exist; account-frozen/inactivity emails we can't see). Use
`org.freedesktop.Notifications` with `actions` (confirmed supported). Set `urgency` hint
(`0=low,1=normal,2=critical`) — use critical for #2, #3, #14, #15 so they persist.
Use the `resident`/`transient` hints appropriately, and reuse the returned `id` via `replaces_id`
so a progress-style toast updates instead of stacking.

---

## 11. Personal Vault

### 11.1 What it is

`[verbatim]` *"Personal Vault is a protected area in OneDrive where you can store your most important
or sensitive files and photos without sacrificing the convenience of anywhere access. Your locked
files in Personal Vault have an extra layer of security keeping them more secured in the event that
someone gains access to your account or your device."*

- Personal/home plans only — **not** available for OneDrive for Business / work or school.
- Must be set up **separately on each device**.
- Appears as a folder named **Personal Vault** inside the OneDrive root, with a distinct
  **safe/vault icon**; locked and unlocked states use different folder icons.
- On Windows the vault's local contents live in a **BitLocker-encrypted** area of the disk.

### 11.2 Setup flow

1. Activity Center shows **"Meet your Personal Vault"** with a **"Get started"** button `[verbatim]`,
   *or* gear → **"Unlock Personal Vault"** `[verbatim]`.
2. **"Next"** `[verbatim]`
3. **"Allow"** `[verbatim]`
4. Choose a verification method (2FA: authenticator, SMS, email, Windows Hello, fingerprint, face,
   PIN) and complete it.
5. The vault opens.

### 11.3 Lock / unlock

| Path | Steps |
|---|---|
| Tray | gear → **"Unlock Personal Vault"** / **"Lock Personal Vault"**; when unlocked, **"Lock Personal Vault"** is also promoted to the top level of the flyout |
| Folder | Click the locked **Personal Vault** folder in the file manager → verify identity |
| Context menu | right-click the vault folder → **OneDrive** → **"Lock Personal Vault"** / **"Unlock Personal Vault"** `[verbatim]` |
| Inactivity toast | **"Still Using Your Personal Vault"** → **"Lock Personal Vault"** `[verbatim]` |
| Confirmation | **"Personal Vault Locked"** notification `[verbatim]` |

### 11.4 Auto-lock

- `[verbatim]` *"Your Personal Vault will automatically lock after **20 minutes** of inactivity by
  default. You will get the notification … **5 minutes** before your Personal Vault automatically
  locks."*
- Settings → **Account** → **"Lock Personal Vault after"**: **20 Minutes** (default) · **1 Hour** ·
  **2 Hours** · **4 Hours**.
- Registry `HKCU\SOFTWARE\Microsoft\OneDrive\VaultInactivityTimeout` = `0` (20 min) / `1` / `2` / `4`.

### 11.5 Linux reproduction

**Feasible: PARTIAL — reproducible as a local-security feature, not as OneDrive's server-side vault.**

- The remote `Personal Vault` folder is *just a folder* in the drive as far as Graph/rclone is
  concerned, but OneDrive **refuses API access while locked**; rclone will typically see it as an
  empty or inaccessible folder. **Do not** promise cloud-side unlock.
- Local reproduction: keep the vault's local materialisation inside a **gocryptfs** or
  **LUKS-on-file** container mounted at `<syncroot>/Personal Vault`, unlocked with a passphrase
  stored in the GNOME keyring (`libsecret` via `QtKeychain` or `secretstorage`), optionally gated by
  `polkit` / `fprintd` for biometrics. Lock = unmount + drop the key.
- The **auto-lock timer** and both notifications (#16, #17) are fully reproducible.
- **Mark clearly:** "Personal Vault protection on Linux is local-device encryption; Microsoft's
  server-side vault protections are not replicated."

---

## 12. Pause syncing

### 12.1 Manual pause

- Path `[verbatim]`: tray icon → **Help & Settings (gear)** → **"Pause syncing"** → **2 hours** /
  **8 hours** / **24 hours**.
- `[verbatim]` *"Syncing will resume automatically after the pause time has finished, or you can
  manually resume syncing at any time."*
- While paused: the tray icon carries the **pause badge**; Activity Center shows
  **"Your files are not currently syncing"** with a **Pause/Resume** control; the gear menu shows
  **"Resume syncing"** `[verbatim]`.
- `[verbatim]` *"If you have both OneDrive and OneDrive for Business, you can pause and resume them
  independently."*
- Resume paths: click the alert at the top of the Activity Center, or gear → **"Resume syncing"**.

### 12.2 Automatic pause

| Trigger | Behaviour | Escape hatch |
|---|---|---|
| **Battery saver mode** | Auto-pause, toast raised | **"Sync Anyway"** in the toast; or turn off "Pause syncing when this device is in battery saver mode" |
| **Metered network** | Auto-pause, toast raised | **"Sync Anyway"**; or turn off the metered toggle |

`[verbatim]` *"When syncing is paused, to resume syncing, in the notification area of the taskbar,
select the OneDrive cloud icon, and at the top of the Activity Center, select the alert."*

Auto-pause has **no timeout** — it lasts until the condition clears or the user overrides.

### 12.3 Linux reproduction

**Feasible: YES.**

- Keep a `PauseController` with `paused_until: datetime | None` and `pause_reason: enum`.
- Implement pause by **stopping the sync job**, not by throttling:
  `rclone rc job/stop jobid=<n>` (or `job/stopgroup group=<g>`) and refusing to start new ones.
  For the mount, do **not** unmount — leave reads working (matching Windows, where paused sync still
  lets you open already-local files) but block uploads by pausing the VFS uploader (stop feeding new
  writes; `vfs/queue-set-expiry` can defer queued uploads).
- Timers: a `QTimer` for the 2/8/24 h auto-resume; persist `paused_until` so a restart honours it.
- Battery: `net.hadess.PowerProfiles` `ActiveProfile == "power-saver"`, or UPower `OnBattery` +
  `Percentage < threshold`.
- Metered: NetworkManager `org.freedesktop.NetworkManager` property **`Metered`**
  (`0=unknown, 1=yes, 2=no, 3=guess-yes, 4=guess-no`) — pause on `1` or `3`.

---

## 13. Everything else

### 13.1 Photos / camera upload

Covered in §3.1.2. Additional surface: Windows 11 now ships a separate **OneDrive Photos app**
(Gallery, Albums, Favorites, filters, light/dark theme, sidebar toggle). **Out of scope** for v1 —
note it exists so nobody is surprised.

### 13.2 "Add shortcut to OneDrive" (a.k.a. "Add a place")

- Web/SharePoint action **"Add shortcut to My files"** / **"Add shortcut to OneDrive"** `[verbatim]`.
- `[verbatim]` *"The Add shortcut to OneDrive option does not sync anything to your computer; it just
  creates a link or bookmark to the document library or folder in your OneDrive."* It then appears in
  File Explorer with the **chain/link folder overlay**.
- `[verbatim]` limits: internal users only; not for folders shared with external users; can't add
  multiple folders at once; not available for individual files or albums.
- Policies: `AddedFolderHardDeleteOnUnmount`, `AddedFolderUnmountOnPermissionsLoss`.

**Feasible: PARTIAL.** rclone can mount a *different remote path* as a subdirectory
(a second mount, or `--union` remote combining `onedrive:` with `onedrive_shared:Shared/X`).
The shared-with-me namespace requires `onedrive:` configured with `drive_type = ...` per drive;
rclone can address another user's drive if you have its drive-id. Treat as advanced.

### 13.3 Account switching / multiple accounts

- One **personal** account maximum; up to **nine** work/school accounts.
- Each account gets its **own tray icon**, its **own Activity Center**, its **own settings**, and its
  **own sync root** (`OneDrive` vs `OneDrive - Contoso`).
- Settings → Account → **"Add an account"**.

**Feasible: YES, and better.** One rclone remote per account, one tray icon per remote (SNI supports
multiple items from one process — give each a unique bus name/id). No personal-account limit.

### 13.4 Search

- The client itself has no search box; search is File Explorer's, and OneDrive contributes indexed
  content for locally-available files. Online-only files are found by **name only**.

**Feasible: PARTIAL.** Add a search field to the Activity Center backed by
`rclone rc operations/list fs=onedrive: remote=<dir> opt='{"recurse":true,"filesOnly":true}'` with a
name filter, cached. Full-text search of cloud content is **NO** without Graph search.

### 13.5 "View online" and offline behaviour

- **"View online"** `[verbatim]` opens `https://onedrive.live.com`.
- Offline: locally-available and pinned files open normally; **online-only files cannot be opened**
  (`[verbatim]` *"You can't open online-only files when your device isn't connected to the
  Internet."*). Windows shows an error and the file stays dehydrated. Changes made offline queue and
  sync when connectivity returns.

**Feasible: YES.** With `--vfs-cache-mode full` a read of an uncached file while offline returns
`EIO`/`EHOSTUNREACH`; catch it and raise the equivalent toast. Writes are queued in the VFS cache and
flushed on reconnect (`vfs/queue` shows the backlog).

### 13.6 Restrictions, limits and large-file behaviour (all `[verbatim]`)

| Limit | Value |
|---|---|
| Invalid characters in names | `" * : < > ? / \ |` — plus leading/trailing spaces disallowed |
| Reserved names | `.lock`, `CON`, `PRN`, `AUX`, `NUL`, `COM0`–`COM9`, `LPT0`–`LPT9`, `_vti_`, `desktop.ini`, any name starting with `~$`; the word `forms` at library root |
| Max file size | **250 GB** per file (upload and download; also inside zips) |
| Path length | OneDrive root + relative path (≤400 chars) must be **≤ 520 characters**; the web uploader requires **< 442 characters** |
| Recommended item count | **300,000** items per sync instance; a Windows-only preview supports up to **1,000,000** with ≥16 GB RAM, an SSD and an i5/Ryzen 5 or better |
| Web copy limit | **2,500** files at once |
| Thumbnails | none generated above **100 MB**; PDF preview unavailable above **100 MB** |
| Accounts per device | 1 personal + up to **9** work/school |
| Never synced | `.TMP` files, `desktop.ini` (Win), `.ds_store` (mac); `.PST` files sync less frequently; OneNote notebooks use their own sync |
| Not supported as sync locations | network / mapped drives; authenticated proxies |
| Screenshots feature | ≤ **10 GB** per screenshot |
| Camera import | ≤ **250 GB** per file |
| Version history | last **25** versions (personal) |
| Recycle bin | **30 days** (personal), **93 days** (work/school) |

**Feasible: YES to enforce.** Pre-flight validation is cheap and strictly improves UX:
- rclone's own `--onedrive-encoding` (default includes
  `LeftSpace,LeftTilde,RightPeriod,RightSpace,InvalidUtf8,Dot` plus the char set) will *encode*
  offending characters rather than reject them — **this differs from Windows**, where the client
  errors. Decide deliberately: either keep rclone's encoding (files sync but appear renamed in the
  cloud) or disable it and reproduce Windows' "rename to fix" flow. **Recommendation: reproduce
  Windows' behaviour** — validate locally and show an error row, because silent name mangling is
  surprising.
- `--max-transfer` + `--cutoff-mode` can enforce a size ceiling.
- `--onedrive-chunk-size` (default 10 MiB) governs large-file upload chunking; raise to 60–100 MiB
  for big files on fast links. **Must be a multiple of 320 KiB** — that's a hard Graph requirement.
- `--onedrive-upload-cutoff` sets the simple-vs-chunked switchover.

### 13.7 Throttling

- OneDrive/Graph returns **HTTP 429** with `Retry-After`; sustained heavy sync gets throttled per
  user and per tenant.
- The client backs off and shows **"Processing changes"** / a delay banner:
  *"You may be experiencing a temporary sync delay due to high service activity."* `[verbatim shape]`

**Feasible: YES.** rclone's onedrive backend already honours `Retry-After` and uses pacer-based
exponential backoff. Tune with `--tpslimit`, `--tpslimit-burst`, `--low-level-retries` (default 10),
`--retries` (default 3). Keep `--transfers` **≤ 4** and `--checkers` **≤ 8** for OneDrive; higher
values reliably trigger 429s.

### 13.8 Quota-exceeded behaviour

- Tray badge → **red "no entry"** (account blocked).
- Uploads stop; downloads continue.
- `[verbatim]` *"After 6 months of exceeding your OneDrive storage quota, your OneDrive files may be
  deleted and once deleted, files are non-recoverable."*

**Feasible: YES to detect** — `operations/about` gives `used`/`total`; server errors surface as
`quotaLimitReached` in `lastError`.

### 13.9 Reset / repair

- `[verbatim]` reset command: `%localappdata%\Microsoft\OneDrive\onedrive.exe /reset`
  (fallbacks in `C:\Program Files\Microsoft OneDrive\` and `Program Files (x86)`).
  *"It disconnects synchronization connections, deletes the OneDrive DAT file, stores application
  logs in the registry, and rebuilds the DAT file when OneDrive starts again."*
- `[verbatim]` *"Resetting OneDrive requires previously selected folder exclusions to be configured
  again."*

**Feasible: YES.** Our equivalent: stop the daemon, delete the VFS cache + our state DB (keeping the
rclone config and the user's files), restart. Offer it as **"Reset OneDriveUI"** with an explicit
warning that selective-sync choices are lost.

### 13.10 Miscellaneous Windows surfaces worth knowing about

| Feature | Note | Feasible |
|---|---|---|
| **Folder colours in OneDrive** | Business/School only; syncs across devices. Personal accounts don't sync colours. | PARTIAL — Nautilus supports per-folder custom icons via `gio set … metadata::custom-icon`, but nothing syncs |
| **"Move to OneDrive" context item** | Moves an external file into the sync root | YES (a menu item that does a `mv`) |
| **Send to → OneDrive** | shell "Send to" entry | YES (a `.desktop` action) |
| **OneDrive desktop icon** | optional desktop shortcut | YES |
| **Junction-point workaround for arbitrary folders** | `[verbatim]` `mklink /j "%UserProfile%\OneDrive\Name" "E:\Source"` — the documented way to sync a folder outside the root | YES via bind mounts or symlinks + `rclone --links` (note: `-L`/`--copy-links` follows symlinks; `--links` stores them as `.rclonelink` files — pick deliberately) |
| **Storage Sense** | Windows feature that dehydrates locally-available files after N days | YES — `--vfs-cache-max-age` is the exact analogue; expose it as "Free up space for files I haven't opened in [14/30/60] days" |
| **B2B sync / external orgs** | `BlockExternalSync` | N/A |
| **Lists sync** | separate feature | N/A |
| **Sync health reporting** | admin telemetry | N/A |

---

## 14. Consolidated feasibility matrix

| # | Feature | Feasible | Primary rclone/Linux mechanism |
|---|---|---|---|
| 1 | Tray icon, 10 states, animation | **YES** | `QSystemTrayIcon` + StatusNotifierItem; themed icon names |
| 2 | Activity Center status line | **YES** | `rc core/stats` poll |
| 3 | Per-file progress rows | **YES** | `core/stats.transferring[]` |
| 4 | Completed-activity feed | **YES** | `core/transferred` + local SQLite |
| 5 | Storage quota bar | **YES** | `operations/about` |
| 6 | Pause 2/8/24 h + auto-resume | **YES** | `job/stop` + timer |
| 7 | Auto-pause on metered | **YES** | NetworkManager `Metered` property |
| 8 | Auto-pause on battery saver | **YES** | UPower / power-profiles-daemon |
| 9 | Bandwidth limits (fixed) | **YES** | `core/bwlimit` (KiB/s — unit differs) |
| 10 | "Adjust automatically" upload | **PARTIAL** | custom controller re-issuing `core/bwlimit` at 70 % of measured throughput with 1-min bursts |
| 11 | Files On-Demand three states | **YES** | `mount --vfs-cache-mode full` + our own pin set |
| 12 | Free up space / Always keep on this device | **YES** | delete from VFS cache + `vfs/forget` / pre-read + pin |
| 13 | Download all files | **YES** | `bisync` mode or full pre-read |
| 14 | Selective sync ("Choose folders") | **YES** | filter rules + local prune |
| 15 | Excluded file extensions | **YES** | `--exclude` / `options/set` filter block |
| 16 | Known Folder Move (Desktop/Documents/Pictures/Music/Videos) | **YES** | XDG user-dirs rewrite + move |
| 17 | Autostart | **YES** | systemd --user unit |
| 18 | File-manager status overlays | **YES** | nautilus-python `InfoProvider.add_emblem` |
| 19 | File-manager "Status" column | **YES** | nautilus-python `ColumnProvider` |
| 20 | File-manager context menu | **YES** | nautilus-python `MenuProvider` |
| 21 | Sidebar/navigation entry | **YES** | GTK bookmarks file |
| 22 | Copy link / expiry / password / view-vs-edit | **YES** | `operations/publiclink`, `--onedrive-link-*`, `link --expire` |
| 23 | Send link by email to specific people | **NO** | Graph `invite` not exposed; fall back to `mailto:` |
| 24 | Manage access (list/revoke) | **PARTIAL** | `--onedrive-metadata-permissions read[,write]` |
| 25 | Block download | **NO** | not exposed |
| 26 | Version history | **NO via rclone** | deep-link to web, or optional direct Graph call |
| 27 | Recycle bin | **PARTIAL** | web deep-link; local freedesktop Trash |
| 28 | Restore your OneDrive (point-in-time) | **NO** | web deep-link only |
| 29 | Conflict "keep both" with hostname suffix | **YES** | `bisync --conflict-loser pathname --conflict-suffix "-$(hostname -s)"` |
| 30 | Sync-issues list + fix flows | **YES** | `core/transferred[].error`, pre-flight name validation |
| 31 | All toasts with action buttons | **YES** | `org.freedesktop.Notifications` (actions supported) |
| 32 | Mass-delete confirmation | **YES** | `--max-delete` abort → dialog |
| 33 | Personal Vault (cloud-side) | **NO** | locked vault is API-inaccessible |
| 34 | Personal Vault (local encryption + auto-lock UX) | **PARTIAL/YES** | gocryptfs/LUKS + libsecret + timer |
| 35 | Screenshots to OneDrive | **YES** | inotify on `~/Pictures/Screenshots` |
| 36 | Camera/device import | **PARTIAL** | GVolumeMonitor + MIME scan |
| 37 | Multiple accounts | **YES** (better than Windows) | one remote + one SNI item per account |
| 38 | Add shortcut to OneDrive | **PARTIAL** | second mount / union remote |
| 39 | Search | **PARTIAL** | `operations/list` name filter; no full-text |
| 40 | Offline behaviour | **YES** | VFS cache + queued writes |
| 41 | Throttling / 429 handling | **YES** | built into the backend; tune `--tpslimit`, `--transfers ≤4` |
| 42 | Quota-exceeded detection | **YES** | `operations/about` + `lastError` |
| 43 | Reset | **YES** | our own state wipe |
| 44 | Co-authoring / Office merge | **NO** | no Office integration |
| 45 | Folder colours | **PARTIAL** | local only, no sync |
| 46 | Update ring / Insider builds | **N/A** | drop |

---

## 15. Implementation checklist derived from this inventory

1. **One `strings.py`** holding every `[verbatim]` string in this doc, keyed by enum. No string
   literals in widget code.
2. **One state enum** (`SyncState`) driving tray icon, tooltip, status line, and banner
   simultaneously. Never let them disagree.
3. **A single `rclone rcd` daemon** managed by systemd --user, addressed only through `rc` HTTP.
   No per-action `subprocess` calls.
4. **A local SQLite state DB**: `activity`, `pins`, `errors`, `settings`, `accounts`.
5. **A Unix-socket IPC** between the daemon and the Nautilus extension (the extension must never
   touch the network or block).
6. **Pre-flight name validation** against §13.6 before any upload, so error rows appear immediately
   and in Windows' language.
7. **Decide the encoding policy up front** (§13.6) — rclone's default silently mangles names that
   Windows would flag.
8. **Two Files On-Demand modes** matching Microsoft's own two buttons: mount+VFS
   ("Free up disk space") and bisync ("Download all files").

---

## 16. Sources

- [What do the OneDrive icons mean? — Microsoft Support](https://support.microsoft.com/en-us/onedrive/what-do-the-onedrive-icons-mean)
- [Save disk space with OneDrive Files On-Demand for Windows — Microsoft Support](https://support.microsoft.com/en-us/onedrive/save-disk-space-with-onedrive-files-on-demand-for-windows)
- [IT Admins - Use OneDrive policies to control sync settings — Microsoft Learn](https://learn.microsoft.com/en-us/sharepoint/use-group-policy)
- [Change the OneDrive sync app upload or download rate — Microsoft Support](https://support.microsoft.com/en-us/office/change-the-onedrive-sync-app-upload-or-download-rate-71cc69da-2371-4981-8cc8-b4558bdda56e)
- [How to access OneDrive settings — Microsoft Support](https://support.microsoft.com/en-us/office/how-to-access-onedrive-settings-6173f176-fd9a-4a34-88f5-5646ec6f568b)
- [Restore your OneDrive — Microsoft Support](https://support.microsoft.com/en-us/onedrive/restore-your-onedrive)
- [Restrictions and limitations in OneDrive and SharePoint — Microsoft Support](https://support.microsoft.com/en-us/onedrive/restrictions-and-limitations-in-onedrive-and-sharepoint)
- [Fix OneDrive sync problems — Microsoft Support](https://support.microsoft.com/en-us/onedrive/fix-onedrive-personal-sync-problems)
- [Add shortcuts to shared folders in OneDrive — Microsoft Support](https://support.microsoft.com/en-us/onedrive/add-shortcuts-to-shared-folders-in-onedrive)
- [My OneDrive says it's full — Microsoft Support](https://support.microsoft.com/en-us/onedrive/my-onedrive-says-it-s-full)
- [OneDrive on Mac gets Liquid Glass with an All-New Activity Center — Microsoft Community Hub](https://techcommunity.microsoft.com/blog/onedriveblog/onedrive-on-mac-gets-liquid-glass-with-an-all-new-activity-center/4495501)
- [MC333940 — OneDrive Sync Activity Center is getting an experience refresh](https://m365admin.handsontek.net/onedrive-sync-activity-center-is-getting-an-experience-refresh/)
- [MC888872 — New prompt to confirm deletion of shared items and updated prompt for local mass item deletions](https://m365admin.handsontek.net/microsoft-onedrive-new-prompt-to-confirm-deletion-of-shared-items-and-updated-prompt-for-local-mass-item-deletions/)
- [MC241752 — First delete dialog alert for OneDrive files](https://m365admin.handsontek.net/first-delete-dialog-alert-for-onedrive-files/)
- [OneDrive Activity Center now houses the Settings and Pause menus — TheWindowsClub](https://www.thewindowsclub.com/onedrive-activity-center)
- [OneDrive Settings in Windows 11: Complete Guide — iTechGuides](https://www.itechguides.com/onedrive-settings-windows-11/)
- Windows 11 Forum (elevenforum.com) tutorials by Shawn Brink, which reproduce the shipped UI strings screen by screen:
  [sync status icons](https://www.elevenforum.com/t/what-do-the-onedrive-sync-status-icons-mean-in-windows-11.11685/) ·
  [set up OneDrive](https://www.elevenforum.com/t/set-up-onedrive-in-windows-11.10927/) ·
  [Files On-Demand](https://www.elevenforum.com/t/enable-or-disable-onedrive-files-on-demand-in-windows-11.4371/) ·
  [FOD status states](https://www.elevenforum.com/t/set-onedrive-files-on-demand-status-states-in-windows-11.4374/) ·
  [choose folders](https://www.elevenforum.com/t/choose-which-onedrive-folders-to-sync-in-windows-11.4324/) ·
  [folder backup](https://www.elevenforum.com/t/turn-on-or-off-onedrive-folder-backup-syncing-across-windows-11-devices.4321/) ·
  [pause/resume](https://www.elevenforum.com/t/pause-and-resume-onedrive-syncing-in-windows-11.4532/) ·
  [upload/download rate](https://www.elevenforum.com/t/change-onedrive-sync-upload-and-download-rate-in-windows-11.4530/) ·
  [metered network](https://www.elevenforum.com/t/enable-or-disable-onedrive-syncing-on-metered-network-in-windows-11.4424/) ·
  [battery saver](https://www.elevenforum.com/t/enable-or-disable-onedrive-syncing-in-battery-saver-mode-in-windows-11.4377/) ·
  [pause notifications](https://www.elevenforum.com/t/enable-or-disable-notifications-when-onedrive-syncing-is-paused-in-windows-11.10911/) ·
  [run at startup](https://www.elevenforum.com/t/turn-on-or-off-onedrive-run-at-startup-in-windows-11.2321/) ·
  [screenshots](https://www.elevenforum.com/t/turn-on-or-off-save-screenshots-to-onedrive-in-windows-11.10889/) ·
  [camera import](https://www.elevenforum.com/t/turn-on-or-off-save-photos-and-videos-from-devices-to-onedrive-in-windows-11.10890/) ·
  [excluded extensions](https://www.elevenforum.com/t/exclude-specific-file-extensions-from-backing-up-to-onedrive-in-windows-11.17380/) ·
  [Personal Vault setup](https://www.elevenforum.com/t/set-up-onedrive-personal-vault-in-windows-11-and-windows-10.13949/) ·
  [Vault lock/unlock](https://www.elevenforum.com/t/lock-and-unlock-onedrive-personal-vault-in-windows-11-and-windows-10.13970/) ·
  [Vault auto-lock](https://www.elevenforum.com/t/change-onedrive-personal-vault-auto-lock-after-time-in-windows-11.13967/) ·
  [unlink](https://www.elevenforum.com/t/unlink-account-and-pc-from-onedrive-in-windows-11.15162/) ·
  [version history](https://www.elevenforum.com/t/view-delete-or-restore-files-from-onedrive-version-history.46100/) ·
  [share](https://www.elevenforum.com/t/share-onedrive-files-and-folders-in-windows-11.12586/) ·
  [stop sharing](https://www.elevenforum.com/t/stop-sharing-onedrive-files-and-folders-in-windows-11.12592/) ·
  [nav-pane status](https://www.elevenforum.com/t/enable-or-disable-show-onedrive-status-on-navigation-pane-in-windows-11.10952/) ·
  [quit OneDrive](https://www.elevenforum.com/t/quit-and-close-onedrive-in-windows-10-and-windows-11.14469/) ·
  [sync any folder](https://www.elevenforum.com/t/sync-any-folder-to-onedrive-in-windows-11-and-windows-10.14509/)
