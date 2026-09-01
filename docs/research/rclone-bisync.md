# rclone bisync v1.75.0 — Authoritative Reference for OneDriveUI

**Verified against:** `rclone v1.75.0` (`os/version: cachyos`, `go1.26.5`), local machine, 2026-08-30.
All log output, file names, exit codes and JSON shapes in this document were **produced by running the
command locally** in `/tmp/.../scratchpad` with two local directories. Sources: `rclone bisync --help`,
<https://rclone.org/bisync/>, <https://rclone.org/filtering/>, <https://rclone.org/rc/>.

> **Hard rule for implementers:** never run a mutating bisync against `onedrive:` in tests. Use two local
> dirs, or an `alias` remote pointing at a local dir.

---

## 0. TL;DR — the exact command OneDriveUI should run

```bash
# FIRST TIME ONLY (or after a settings/filter change, or after a critical abort):
rclone bisync "$HOME/OneDrive" "onedrive:" \
  --resync --resync-mode path1 \
  --workdir       "$HOME/.local/state/onedriveui/bisync" \
  --filters-file  "$HOME/.config/onedriveui/filters.txt" \
  --backup-dir1   "$HOME/.local/share/onedriveui/recycle/local" \
  --backup-dir2   "onedrive:.onedriveui-recycle" \
  --conflict-resolve newer --conflict-loser num --conflict-suffix conflict \
  --create-empty-src-dirs --track-renames \
  --max-delete 25 --resilient --recover --max-lock 2m \
  --transfers 4 --checkers 8 \
  --use-json-log --color NEVER --stats 500ms -v

# EVERY SUBSEQUENT RUN: identical, but WITHOUT --resync / --resync-mode.
```

Key per-flag rationale is in §6, §9, §12.

---

## 1. Semantics: what bisync does on each run

### 1.1 The algorithm

bisync keeps a **snapshot of the last known-good state** of both paths in two listing files. On each run:

1. **Acquire the lock file** (`<session>.lck`). Abort (exit 1) if a live lock exists.
2. **Build fresh listings** of Path1 and Path2 concurrently (using the same `march` engine as `sync`/`check`),
   applying `--filters-file` / `--include` / `--exclude`. Written to `<session>.path1.lst-new` /
   `.path2.lst-new`.
3. **Compute deltas** for each side by diffing `.lst-new` against the prior `.lst`:
   `New`, `Newer`, `Older`, `Larger`, `Smaller`, `Deleted`. (`Changed` = any of newer/older/larger/smaller.)
4. **Safety checks** — `--check-access`, `--max-delete`, "all files changed". Abort before any mutation.
5. **Resolve** the delta matrix (tables in §1.5/§1.6), including conflict handling.
6. **Apply**: one `sync`-style operation per direction (copies + deletes are performed in one pass since v1.66),
   honouring `--backup-dir1`/`--backup-dir2`.
7. **Update listings** from the *sync results* (no re-listing). `.lst` → `.lst-old`, `.lst-new` → `.lst`.
8. **Validate** (`--check-sync`, default on): compare the two final listing snapshots.
9. **Cleanup**: delete `.lst-new`, release the lock (unless `--no-cleanup`).

Observed log skeleton of a successful, non-resync run (real output, `--color NEVER -v`):

```
INFO  : Setting --ignore-listing-checksum as neither --checksum nor --compare checksum are set.
INFO  : Bisyncing with Comparison Settings:
{ "Modtime": true, "Size": true, "Checksum": false, "HashType1": 0, "HashType2": 0,
  "NoSlowHash": false, "SlowHashSyncOnly": false, "SlowHashDetected": true, "DownloadHash": false }
INFO  : Synching Path1 "/…/local/" with Path2 "/…/cloud/"
INFO  : Using filters file /…/filters.txt
INFO  : Building Path1 and Path2 listings
INFO  : Path1 checking for diffs
INFO  : - Path1             File changed: size (larger), time (newer)   - Documents/report.docx
INFO  : - Path1             File is new                                 - newfile.txt
INFO  : Path1:    2 changes:    1 new,    1 modified,    0 deleted
INFO  : (Modified:    1 newer,    0 older,    1 larger,    0 smaller)
INFO  : Path2 checking for diffs
INFO  : - Path2             File was deleted                            - Documents/cloudfile.txt
INFO  : Path2:    2 changes:    0 new,    1 modified,    1 deleted
INFO  : Applying changes
INFO  : - Path1             Queue copy to Path2       - /…/cloud/newfile.txt
INFO  : - Path1             Queue delete              - /…/local/Documents/cloudfile.txt
INFO  : - Path2             Do queued copies to       - Path1
INFO  : - Path1             Do queued copies to       - Path2
INFO  : Updating listings
INFO  : Validating listings for Path1 "/…/local/" vs Path2 "/…/cloud/"
INFO  : Bisync successful
```

**UI-parseable milestone strings** (stable, use these as state-machine transitions):

| String in `msg` | UI state |
|---|---|
| `Building Path1 and Path2 listings` | "Looking for changes…" |
| `Path1 checking for diffs` / `Path2 checking for diffs` | "Looking for changes…" |
| `Applying changes` | "Syncing N changes…" |
| `Do queued copies to` | "Uploading/Downloading…" |
| `Updating listings` | "Finishing…" |
| `Validating listings for Path1` | "Finishing…" |
| `Bisync successful` | "Your files are up to date" (success) |
| `No changes found` | "Your files are up to date" |
| `Bisync aborted. Must run --resync to recover.` | **critical**, needs resync |
| `Bisync aborted. Please try again.` | recoverable, retry next tick |
| `Bisync aborted. Error is retryable without --resync due to --resilient mode.` | recoverable (with `--resilient`) |

### 1.2 Working directory

* Default: **`~/.cache/rclone/bisync/`** on Linux (confirmed by `rclone bisync --help`:
  `--workdir string … (default: /home/user/.cache/rclone/bisync)`).
  macOS: `~/Library/Caches/rclone/bisync`. Windows: `%LOCALAPPDATA%\rclone\bisync`.
* Override with `--workdir DIR` (rc: `workdir`). **OneDriveUI should use its own workdir**
  (e.g. `~/.local/state/onedriveui/bisync`) so `rclone` cache cleaning never nukes sync state.
* The directory must exist and be writable; bisync creates it if the parent exists.

### 1.3 Session name (listing-file naming convention) — EXACT

The session name is built from the two **canonical fs config strings** (`fs.ConfigString(f)` — i.e. what
`rclone backend features X` reports as `Name` + `:` + `Root`, with the `Name:` part **omitted for a bare local
path**), each sanitised, joined by `..`:

```
session = sanitize(configstring(Path1)) + ".." + sanitize(configstring(Path2))
sanitize(s) = every char not in [A-Za-z0-9.-] → "_" , then strip a leading "_"
```

Verified empirically:

| Path1 arg | Path2 arg | ConfigString(Path2) | Session |
|---|---|---|---|
| `/tmp/x/bs/p1` | `/tmp/x/bs/p2` | `/tmp/x/bs/p2` | `tmp_x_bs_p1..tmp_x_bs_p2` |
| `/…/t_n2/local` | `od:/…/t_n2/remote` | `od:/…/t_n2/remote` | `…_t_n2_local..od__tmp_…_t_n2_remote` (`:`→`_`, `/`→`_` ⇒ the double `__`) |
| `/…/t_n2/local` | `od:/…/t_n2/remote/sub` | — | `…_t_n2_local..od__tmp_…_t_n2_remote_sub` |
| `/…/local` | `fakeod:Documents/My Files` (**alias**) | resolves to `/…/remote/Documents/My Files` | `…_local..…_remote_Documents_My_Files` (spaces → `_`) |

`rclone backend features onedrive:` reports `Name='onedrive'`, `Root=''`, so ConfigString is `onedrive:` and
`~/OneDrive` ↔ `onedrive:` yields:

```
home_user_OneDrive..onedrive_
```

(and `onedrive:Documents` would give `…..onedrive_Documents`).

> **`alias` remotes are resolved to their target before naming** — an `alias` pointing at `/x/y` produces
> the *local path* in the session name, not the alias name. Do **not** wrap `onedrive:` in an alias if you
> want a stable readable session name.

**Files in the workdir** (all prefixed with `<session>.`):

| File | Meaning |
|---|---|
| `<session>.path1.lst` | Last known-good Path1 snapshot. **Its presence is what makes a non-resync run legal.** |
| `<session>.path2.lst` | Last known-good Path2 snapshot. |
| `<session>.path1.lst-old` / `.path2.lst-old` | The **previous** good snapshot, kept as the backup used by `--recover`. Appears after the first successful *non-resync* run. |
| `<session>.path1.lst-new` / `.path2.lst-new` | Listing built during the current run. Left behind on an aborted run (unless `--no-cleanup` is off and cleanup ran). |
| `<session>.path1.lst-err` / `.path2.lst-err` | `.lst` renamed after a **critical** error. Because `.lst` is now missing, all further runs abort with exit 7 until `--resync`. |
| `<session>.lck` | Lock file (JSON). See §4.5. |

### 1.4 Listing file format (`.lst`) — EXACT

```
# bisync listing v1 from 2026-08-31T03:24:41.745002790+0000
-        4 - - 2026-08-31T03:24:33.378333936+0000 "Documents/d1.txt"
-        8 - - 2026-08-31T03:24:33.378333936+0000 "a.txt"
d       -1 - - 2026-08-31T03:30:21.004522724+0000 "EmptyDir"
-        6 md5:b1946ac92492d2347c6235b4d2611184 - 2026-08-31T03:30:32.433364555+0000 "a.txt"
```

Go format string is `"%s %8d %s %s %s %q\n"`. Fields, left to right:

1. **flag** — `-` for a file, `d` for a directory (directories only appear with `--create-empty-src-dirs`).
2. **size** — right-aligned in 8 columns; `-1` for directories and for unknown-size objects (Google Docs).
3. **hash** — `-` when absent, else `<hashname>:<hex>` e.g. `md5:b1946ac…`, `quickxor:…`.
4. **id** — backend object ID, `-` when absent.
5. **modtime** — RFC3339Nano, **UTC with `+0000` offset** (note: `+0000`, not `Z`).
6. **name** — Go-quoted (`%q`) path relative to the sync root, forward slashes.

Line 1 is always the header comment `# bisync listing v1 from <RFC3339Nano>`.

**OneDriveUI can parse `.lst` directly** to render "what rclone thinks is synced" without shelling out —
this is the cheapest source of truth for a file-explorer overlay (green check = present in both `.lst` files
with matching size/modtime).

### 1.5 Normal sync checks (from the docs, verified)

| Type | Description | Result | Implementation |
|---|---|---|---|
| Path2 new | new on P2, absent on P1 | P2 survives | `copy` P2→P1 |
| Path2 newer | newer on P2, unchanged P1 | P2 survives | `copy` P2→P1 |
| Path2 deleted | deleted on P2, unchanged P1 | deleted | `delete` on P1 |
| Path1 new | new on P1, absent on P2 | P1 survives | `copy` P1→P2 |
| Path1 newer | newer on P1, unchanged P2 | P1 survives | `copy` P1→P2 |
| Path1 older | older on P1, unchanged P2 | P1 survives | `copy` P1→P2 |
| Path2 older | older on P2, unchanged P1 | P2 survives | `copy` P2→P1 |
| Path1 deleted | gone on P1 | deleted | `delete` on P2 |

### 1.6 Unusual sync checks

| Type | Result |
|---|---|
| Changed on both AND currently **identical** | **No change** (not a conflict — bisync runs an equality check first, since v1.64) |
| New on both, not identical | conflict → `--conflict-resolve` / `--conflict-loser` |
| P2 newer AND P1 changed, not identical | conflict |
| P2 newer AND P1 deleted | P2 survives (copy P2→P1) |
| P2 deleted AND P1 changed | P1 survives (copy P1→P2) |
| P1 deleted AND P2 changed | P2 survives (copy P2→P1) |

> Deletion never wins over a modification. This matches OneDrive's own behaviour and is a good thing to
> surface in the UI ("a file you deleted came back because it was edited elsewhere").

---

## 2. `--resync` and `--resync-mode`

### 2.1 When it is REQUIRED

Exactly three situations (per the docs, and the only ones you should ever auto-trigger):

1. **First bisync run** for this Path1/Path2 pair (no `.lst` files in the workdir).
2. **Settings changed** — most importantly the `--filters-file` content (see §5.2), and `--compare`.
3. **After a critical abort** — `.lst` files were renamed to `.lst-err`.

**Never run `--resync` on a schedule.** `--resync` only *copies* both ways; it never deletes. A file you
delete on one side would be restored from the other side forever, and a rename would leave both names.

### 2.2 What it does

```
rclone copy Path2 Path1 --ignore-existing [--create-empty-src-dirs]
rclone copy Path1 Path2 [--create-empty-src-dirs]
```

i.e. both sides end up with a **matching superset**. Both base directories must exist (bisync fails otherwise —
this is deliberate). One side may be empty. `--track-renames` is **ignored** during `--resync`
(`ERROR : … Ignoring --track-renames as it doesn't work with copy or move, only sync` — harmless, see §10.3).

### 2.3 `--resync-mode CHOICE`

Decides the winner **for files that exist on both sides and differ** during resync. Options (named after the winner):

| Value | Winner |
|---|---|
| `path1` | **Default with `--resync`.** Path1 unconditionally. |
| `path2` | Path2 unconditionally. |
| `newer` | newer modtime (like `copy --update` both ways) |
| `older` | older modtime |
| `larger` | larger size |
| `smaller` | smaller size |
| `none` | "no resync" — only meaningful as the absence of resync |

Rules:
* `--resync` ⇒ `--resync-mode path1` unless another mode is given.
* Any `--resync-mode` except `none` ⇒ `--resync`. You only need one of the two flags.
* If the backend can't support the method (no modtime), it silently falls back to `path1`.
* If the attribute is missing/equal, bisync falls back to the other `--compare` methods; if still tied,
  the "source at that moment" wins — in practice a slight edge to **Path2** (the 2→1 copy runs first).
* **Nothing is renamed during a resync.** The loser is *overwritten*. Use `--backup-dir1/2` to retain it.
* `--conflict-resolve`, `--conflict-loser`, `--conflict-suffix` **do not apply** during `--resync`.

**For OneDriveUI:** on the very first sync of an existing cloud account into a fresh local folder, use
`--resync` (mode `path1`) only if the local folder is authoritative; otherwise use `--resync-mode newer`,
which is the closest analogue to what the Windows OneDrive client does on first pair-up.

### 2.4 The EXACT "must resync" error (verified)

Running a first-ever bisync without `--resync`:

```
ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings, likely due to critical error on prior run
Tip: here are the filenames we were looking for. Do they exist?
Path1: /…/work/<session>.path1.lst
Path2: /…/work/<session>.path2.lst
Try running this command to inspect the work dir:
rclone lsl "/…/work"
ERROR : Bisync aborted. Must run --resync to recover.
NOTICE: Failed to bisync: bisync aborted
```
Exit code **7**.

Other messages that end in the same `Bisync aborted. Must run --resync to recover.` line (all exit 7):

```
ERROR : Bisync critical error: filters file has changed (must run --resync): /…/filters.txt
ERROR : Bisync critical error: check file check failed
ERROR : Bisync critical error: empty current Path2 listing: /…/<session>.path2.lst-new
ERROR : Empty current Path2 listing. Cannot sync to an empty directory: /…/<session>.path2.lst-new
```

With `--resilient`, the last line becomes:

```
ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.
```

**Detection rule for the UI:** `msg` contains `Must run --resync to recover` ⇒ set state
`NEEDS_RESYNC`, disable auto-sync, show a "Reset sync" affordance. `Error is retryable without --resync`
⇒ just retry on the next tick.

---

## 3. Conflict handling — EXACT filenames

A **conflict** = the file is new-or-changed on *both* sides since the last run **AND** the two versions are
not currently byte-identical. (If they *are* identical, it is silently skipped — no conflict.)

### 3.1 `--conflict-resolve` (who wins)

`none` (default) | `path1` | `path2` | `newer` | `older` | `larger` | `smaller`

* `none` — no winner; **both** files are renamed and cross-copied.
* Any other value picks a winner; the winner keeps the original filename and is copied to the other side;
  the loser is handled per `--conflict-loser`.
* If the backend lacks the attribute, or the attribute is equal/missing, it falls back to `none`.

### 3.2 `--conflict-loser` (what happens to the loser)

`num` (default) | `pathname` | `delete`

* `num` — append the next free number to the suffix: `file.txt.conflict1`, and if that exists,
  `file.txt.conflict2`, … up to 9223372036854775807.
* `pathname` — name by origin. **A trailing digit is still appended when only ONE suffix is given**
  (or two identical ones). The digit is omitted only when two *different* suffixes are supplied.
* `delete` — keep the winner, delete the loser. If no winner can be determined, `delete` is ignored and
  `num` is used instead (nothing is deleted).

### 3.3 `--conflict-suffix STRING[,STRING]`

Default `conflict`. One string, or `path1suffix,path2suffix`. Supports Go time layouts in `{}`:
`{DateOnly}`, `{RFC3339}`, `{MacFriendlyTime}` / `{mac}` (→ `2006-01-02 0304PM`), etc.
Independent of rclone's global `--suffix` (they can be used together).
`--suffix-keep-extension` moves the suffix before the extension.

### 3.4 Resulting filenames — MEASURED, not guessed

Setup for all rows: `a.txt` edited on Path1, then (1.1 s later) edited differently and longer on Path2.
Result files exist on **both** sides afterwards.

| Flags | Files on both sides | Original `a.txt` still present? |
|---|---|---|
| *(defaults)* | `a.txt.conflict1` (Path1's copy), `a.txt.conflict2` (Path2's copy) | **No** |
| `--conflict-resolve newer` | `a.txt` (Path2 winner), `a.txt.conflict1` (Path1 loser) | Yes |
| `--conflict-resolve newer --conflict-loser pathname` | `a.txt`, `a.txt.conflict1` | Yes |
| `--conflict-resolve newer --conflict-loser delete` | `a.txt` only | Yes |
| `--conflict-suffix onedrive-conflict` | `a.txt.onedrive-conflict1`, `a.txt.onedrive-conflict2` | No |
| `--conflict-suffix p1conflict,p2conflict` | `a.txt.p1conflict1`, `a.txt.p2conflict1` | No |
| `--conflict-loser pathname --conflict-suffix path` | `a.txt.path1`, `a.txt.path2` | No |
| `--conflict-loser pathname --conflict-suffix path1,path2` | `a.txt.path1`, `a.txt.path2` | No |
| `--conflict-resolve newer --conflict-loser pathname --conflict-suffix "{DateOnly}-conflict"` | `a.txt`, `a.txt.2026-08-30-conflict1` | Yes |
| `--conflict-suffix conflict --suffix-keep-extension` | `a.conflict1.txt`, `a.conflict2.txt` | No |

> **Gotcha:** the docs' phrasing suggests `--conflict-loser pathname` yields `.path1`/`.path2`, but that is
> only true if you *also* set `--conflict-suffix path` (or `path1,path2`). With the default suffix you get
> `.conflict1` / `.conflict2`, identical to `num` in the single-conflict case.

### 3.5 Log lines to detect conflicts in the UI

Plain text (`--color NEVER -v`):

```
NOTICE: - WARNING           New or changed in both paths                - Documents/report.docx
INFO  : Documents/report.docx: Path2 is newer. Path1: 2026-08-30 23:32:01.603130472 -0400 -04, Path2: 2026-08-30 23:32:02.704115234 -0400 -04, Difference: 1.100984762s
INFO  : Documents/report.docx: The winner is: Path2
NOTICE: - Path1             Renaming Path1 copy                         - /…/local/Documents/report.docx.conflict1
NOTICE: - Path1             Queue copy to Path2                         - /…/cloud/Documents/report.docx.conflict1
NOTICE: - Path2             Not renaming Path2 copy, as it was determined the winner - /…/cloud/Documents/report.docx
NOTICE: - Path2             Queue copy to Path1                         - /…/local/Documents/report.docx
```

With `--conflict-loser delete` the only conflict line is the `WARNING New or changed in both paths` NOTICE.

JSON (`--use-json-log --color NEVER`) — note the conflict NOTICEs carry **no `object` field**, only `msg`:

```json
{"time":"…","level":"notice","msg":"- Path1             Renaming Path1 copy                         - /…/p1/a.txt.conflict1","source":"bisync/resolve.go:318"}
{"time":"…","level":"info","msg":"Moved (server-side) to: a.txt.conflict1","object":"a.txt","objectType":"*local.Object","source":"operations/operations.go:493"}
{"time":"…","level":"notice","msg":"- Path2             Renaming Path2 copy                         - /…/p2/a.txt.conflict2","source":"bisync/resolve.go:318"}
{"time":"…","level":"info","msg":"Copied (new)","size":15,"object":"a.txt.conflict2","objectType":"*local.Object","source":"operations/copy.go:380"}
```

**Recommended UI detection (two complementary signals):**

1. **Live**: regex `^- WARNING\s+New or changed in both paths\s+- (?P<path>.+)$` on `msg`
   (after collapsing runs of spaces). That yields the *relative path* of the conflicted file.
2. **Durable**: after each run, glob the local tree for `*.{suffix}[0-9]*` using the configured
   `--conflict-suffix`. This survives restarts and matches conflicts created by other machines.
   With the recommended config the glob is `**/*.conflict[0-9]*`.

**Recommended OneDriveUI defaults:** `--conflict-resolve newer --conflict-loser num --conflict-suffix conflict`.
This mirrors Windows OneDrive: the newest edit keeps the real name and the other copy is kept beside it
(Windows names it `report-DESKTOP-ABC123.docx`; we surface `report.docx.conflict1` in the UI as
"report.docx (conflicted copy)" and offer *Keep this one* / *Keep other* / *Keep both*).

---

## 4. Safety features

### 4.1 `--max-delete PERCENT` (default 50)

Aborts **before making any change** if more than PERCENT of the files listed on either side were deleted.

Exact output (6 of 10 files deleted on Path1):

```
INFO  : Path1:    6 changes:    0 new,    0 modified,    6 deleted
ERROR : Safety abort: too many deletes (>50%, 6 of 10) on Path1 "/…/p1/". Run with --force if desired.
NOTICE: Bisync aborted. Please try again.
NOTICE: Failed to bisync: too many deletes
```

* Exit code **1** (non-critical). `.lst` files are **preserved**; `.lst-new` are left behind. No `--resync` needed.
* Bypass with `--force`, or raise the limit: `--max-delete 75`.
* `--max-delete 0` is *not* "no deletes" — use it only if you mean 0 %.
* `--track-renames` does **not** help here: renames are counted as deletes because the check runs before
  rename detection.

**UI behaviour:** treat "too many deletes" as a *confirmation prompt*, exactly like the Windows client's
"You deleted a lot of files" dialog: show the count, list the files (parse the `File was deleted` lines),
and offer **Delete them everywhere** (re-run with `--force`) or **Restore them** (re-run with `--resync`).

### 4.2 "All files changed" check

If *every* pre-existing file on one side changed (typical of a timezone/clock change), bisync aborts:

```
ERROR : Safety abort: all files were changed on Path1 "/…/p1/". Run with --force if desired.
NOTICE: Bisync aborted. Please try again.
NOTICE: Failed to bisync: all files were changed
```
Exit code **1**. New files are not counted.

> **Gotcha:** this fires trivially on tiny trees. In a 1-file sync pair, editing that one file trips it every
> time. Not a problem for a real OneDrive folder, but it will bite you in tests. Bypass with `--force`.

### 4.3 `--check-access` / `--check-filename` (RCLONE_TEST files)

Verifies that identically-placed files named `RCLONE_TEST` (or `--check-filename NAME`) exist in **both**
listings. They are **never created automatically**.

Failure output:

```
NOTICE: --check-access: Failed to find any files named RCLONE_TEST
 More info: https://rclone.org/bisync/#check-access
ERROR : Access test failed: Path1 count 0, Path2 count 0 - RCLONE_TEST
ERROR : Bisync critical error: check file check failed
ERROR : Bisync aborted. Must run --resync to recover.
```

Partial failure (one missing on Path2):

```
ERROR : Access test failed: Path1 count 2, Path2 count 1 - RCLONE_TEST
ERROR : -          Access test failed: Path1 file not found in Path2 - sub/RCLONE_TEST
ERROR : Bisync critical error: check file check failed
```

Success (`-v`): `INFO  : Checking access health`.

Notes:
* This is a **critical** abort → exit 7, `.lst-err`, `--resync` required (unless `--resilient`).
* `--check-access` is **enforced during `--resync` too**, so you cannot use `bisync --resync --check-access`
  to seed the files. Seed them first: `rclone touch Path1/RCLONE_TEST` then
  `rclone copyto Path1/RCLONE_TEST Path2/RCLONE_TEST`, or run one bisync without `--check-access`.
* Files hidden by the filters file are not in the listings and therefore not checked.
* Content and timestamps are irrelevant — only name + location.

**For OneDriveUI:** put one `RCLONE_TEST` at the sync root and one inside each top-level included folder.
This is the mechanism that stops "the mount went away / the network dropped" from being read as
"the user deleted everything". Strongly recommended.

### 4.4 `--check-sync true|false|only` (default `true`)

Runs at the end and compares the two **final listing snapshots**.
* `false` — skip (meaningful speed-up on very large trees).
* `only` — run the check and exit, no syncing. Output: `Validating listings for Path1 … vs Path2 …` +
  `Bisync successful`.

> **Critical limitation, confirmed by experiment:** `--check-sync only` reads the stored `.lst` files and does
> **not** re-list the remotes. Adding a file directly on Path2 and then running `--check-sync only` reports
> success. For a real integrity check use `rclone check -MvPc Path1 Path2 --filter-from filters.txt`.

**For OneDriveUI:** wire a weekly `rclone check` into a background "Verify my files" maintenance job; do
**not** rely on `--check-sync only`.

### 4.5 Lock file (`<session>.lck`) — KEY for detecting a stuck run

Created at the start of a run in the workdir, removed at the end. Contents (real, from a live run):

```json
{"Session":"/…/work/tmp_…_p1..tmp_…_p2",
 "PID":"12737",
 "TimeRenewed":"2026-08-30T23:27:39.491467617-04:00",
 "TimeExpires":"2226-07-13T23:27:39.491467654-04:00"}
```

* With `--max-lock 0` (default) `TimeExpires` is set ~200 years out ⇒ effectively never expires.
* With `--max-lock 2m`, `TimeExpires = TimeRenewed + 2m`, and bisync **renews it every `max-lock − 1 minute`**
  while running (`INFO : lock file renewed for 2m0s. New expiration: 2026-08-30 23:34:01.599598987 -0400 -04`).
* `--max-lock` minimum is 2 minutes; a smaller value is silently raised:
  `NOTICE: --max-lock cannot be shorter than 2 minutes (unless 0.) Changing --max-lock from 30s to 2m0s`.
* A live lock blocks a second run with **exit code 1**:

```
NOTICE: Failed to bisync: prior lock file found: /…/work/<session>.lck
If you're SURE you want to override this safety feature, you can delete the lock file with the following command, then run bisync again:
rclone deletefile "/…/work/<session>.lck"
Tip: this indicates that another bisync run (of these same paths) either is still running or was interrupted before completion.
```

* An expired lock is removed automatically:
  `INFO : …lck: Lock file found, but it expired at 2026-08-30 23:23:07.474211 -0400 -04. Will delete it and proceed.`
* An **unreadable** lock is treated as expired **only if `--max-lock > 0`**; otherwise it is a hard block
  (`Lock file exists, but contents are unreadable. (decode error: …)`).

**OneDriveUI "stuck run" detector:**
```python
import json, os, signal, datetime
lck = f"{workdir}/{session}.lck"
if os.path.exists(lck):
    d = json.load(open(lck))
    pid = int(d["PID"])
    alive = True
    try: os.kill(pid, 0)
    except (ProcessLookupError, PermissionError) as e:
        alive = isinstance(e, PermissionError)
    expired = datetime.datetime.now().astimezone() > datetime.datetime.fromisoformat(d["TimeExpires"])
    #   alive  -> a run is genuinely in progress; show the spinner
    #   !alive -> stale; safe to os.remove(lck) and continue
```
Always pass `--max-lock 2m` so rclone can self-heal even if our process is killed.

### 4.6 `--recover`

On the next run after an *un-graceful* interruption, recover from the `.lst-old` backup snapshot instead of
demanding `--resync`. Slightly increases the chance of a false conflict (a file synced during the aborted run
that then changes again looks "changed on both sides"). Use it together with Graceful Shutdown.

### 4.7 `--resilient`

Downgrades certain "less serious" aborts so that the **next** run may proceed without `--resync`. Verified:

| Scenario | Without `--resilient` | With `--resilient` |
|---|---|---|
| Filters file changed | exit 7, `.lst` → `.lst-err` | exit 7, **`.lst` preserved** |
| Access test failure | exit 7, `.lst-err` | exit 7, `.lst` preserved |
| Missing listing files | exit 7, `.lst-err` | retryable |

Note it still **aborts the current run and still returns 7** — it only avoids the permanent lockout. More
serious errors (a failed copy/move) still force `.lst-err` even under `--resilient` (observed in §4.8).

`--retries N` / `--retries-sleep D` on bisync **require `--resilient`** (per `rclone bisync --help`).

### 4.8 `--no-cleanup`

Keeps `.lst-new` and other working files after the run. Useful for debugging; do not ship it enabled.

### 4.9 Graceful Shutdown (SIGINT / Ctrl-C)

Success path (measured, 10 MB file at `--bwlimit 2M`, SIGINT after 2 s):

```
NOTICE: Attempting to gracefully shutdown. (Send exit signal again for immediate un-graceful shutdown.)
INFO  : Canceling Sync if not done in: 30s
INFO  : Canceling Sync if not done in: 29s
INFO  : big.bin: Copied (new)
NOTICE: Graceful shutdown completed successfully.
INFO  : Bisync successful
INFO  : Exiting...
```
**Exit code 130.** Workdir afterwards: `.path1.lst`, `.path1.lst-old`, `.path2.lst`, `.path2.lst-old` — a
clean, resumable state. The next run needs nothing special.

Failure path (40 MB at 2 MB/s — could not finish inside the 30 s window):

```
INFO  : Canceling Sync if not done in: 14s
ERROR : big.bin: Failed to copy: chtimes /…/p2/big.bin.86ca1d09.partial: no such file or directory
ERROR : Local file system at /…/p2: not deleting files as there were IO errors
ERROR : Bisync critical error: chtimes /…/p2/big.bin.86ca1d09.partial: no such file or directory
ERROR : Bisync aborted. Must run --resync to recover.
NOTICE: Failed to bisync with 2 errors: last error was: bisync aborted
```
**Exit code 130**, `.lst-err` written, `--resync` required. `--resilient` did **not** save it.

Budget: 30 s to drain the transfer queue, then a further 60 s total to save state. A second SIGINT exits
immediately and messily.

> **OneDriveUI must send SIGINT (not SIGTERM/SIGKILL) when the user pauses sync or quits the app**, and must
> wait up to ~90 s before escalating. Show a "Finishing up…" state during that window.
> **Never use `--inplace`** — a killed in-place transfer corrupts the destination file and the corruption
> propagates back on the next run.

---

## 5. Filters — how "Choose folders" (selective sync) is implemented

### 5.1 `--filters-file` vs `--filter-from`

* `--filters-file FILE` is bisync's extension of `--filter-from`. **Exactly one** may be given per run.
* `--include*`, `--exclude*`, and `-f/--filter` also work on the bisync command line, but they are **not**
  MD5-guarded (see §5.2) and so are unsafe for selective sync. **Use `--filters-file` for OneDriveUI.**

### 5.2 The MD5 guard on the filters file

On a `--resync` run bisync writes the file's MD5 next to it:

```
INFO  : Using filters file /…/filters.txt
INFO  : Storing filters file hash to /…/filters.txt.md5
```

`filters.txt.md5` is a **32-char lowercase hex digest with no trailing newline**, mode `0600`, identical to
`md5sum filters.txt`:

```
$ cat filters.txt.md5 ; echo ; md5sum filters.txt
2c1bc5ec63212b51881fa60cd5d333cf
2c1bc5ec63212b51881fa60cd5d333cf  /…/filters.txt
```

On every subsequent run the hash is re-checked. Mismatch ⇒ critical abort:

```
ERROR : Bisync critical error: filters file has changed (must run --resync): /…/filters.txt
ERROR : Bisync aborted. Must run --resync to recover.
```
Exit **7**, `.lst` → `.lst-err` (preserved under `--resilient`).

**Therefore the "Choose folders" flow in OneDriveUI is:**
1. User toggles folders in the dialog → write the new `filters.txt` atomically.
2. Immediately run `rclone bisync … --filters-file filters.txt --resync` (with the resync mode you want —
   `path1` is fine; nothing is deleted by a resync).
3. Delete `filters.txt.md5` first only if you want to be sure; rclone rewrites it during `--resync` anyway.

**Verified consequence:** excluding a folder does **not** delete it from either side. Before:
`p1/A/a.txt`, `p1/B/b.txt`, `p2/A/a.txt`, `p2/B/b.txt`. After adding `- /B/` and running `--resync`:
both sides still have `A/` **and** `B/`; `B/` is simply invisible to bisync from now on.

> To make "unchecking a folder" actually free local disk space (what Windows OneDrive does), OneDriveUI must
> **delete the local folder itself** after the resync completes. Order matters:
> write filters → `--resync` → verify exit 0 → `shutil.rmtree(local_folder)`. Deleting first and resyncing
> after also works (the folder is already excluded so nothing propagates), but resync-then-delete is safer
> because a failed resync leaves the data intact.

### 5.3 Filter file syntax — the exact rules

From <https://rclone.org/filtering/> plus bisync's own additions:

* Leading whitespace is stripped. Blank lines are ignored.
* A line whose first non-whitespace char is `#` (or `;`) is a comment.
* The first non-whitespace char **must** be `+` (include) or `-` (exclude).
* **Exactly one space** between the `+`/`-` and the pattern.
* Everything after that space is the pattern, **including trailing whitespace** (which is almost always a bug).
* Only forward slashes, even on Windows. No quoting needed — spaces in names are literal (`- /My Folder/` works).
* Rules are evaluated **top to bottom; first match wins**.
* `!` on its own line clears all previously-defined rules.

**Pattern metacharacters:**

| Token | Meaning |
|---|---|
| `*` | any sequence of characters **except** `/` |
| `**` | any sequence of characters **including** `/` |
| `?` | exactly one character except `/` |
| `[abc]`, `[a-z]`, `[^a-z]` | character class (Go regexp classes such as `[\d]` allowed) |
| `{a,b,c}` | alternation — `*.{jpg,png,heic}` |
| `{{regexp}}` | raw Go regular expression |
| `\` | escape the next reserved character |

**Anchoring:**

* A pattern **starting with `/`** is anchored to the sync root: `/Videos/` matches only the top-level `Videos`.
* A pattern **without** a leading `/` matches at **any depth**, but only on whole path elements:
  `desktop.ini` matches `a/b/desktop.ini`, and does **not** match `mydesktop.ini`.
* A pattern **ending with `/`** matches directories only. Everything beneath an excluded directory is pruned —
  rclone never even lists inside it. **`**` at the end is unnecessary and slower.**
* rclone infers implied directory rules from file patterns (`/a/*.jpg` implies `/a/` must be traversable).

**Include semantics:**

* `--include`/`--include-from` implicitly append `- **`. `--filter +` / a `+` line in a filters file does **not**.
* With `+` rules you must include the parent directories too, or the children are never reached. The idiom
  `+ /Documents/Work/**` works because rclone infers the parent dirs; `+ /*` is needed to also get the
  loose files sitting at the sync root.

### 5.4 Exclude-style filters file (recommended for OneDriveUI)

This is the "sync everything except…" model, which is what the Windows client's "Choose folders" produces
when the user unchecks a few things.

```
# ~/.config/onedriveui/filters.txt
# NOTICE: any edit to this file REQUIRES a bisync --resync run.

# --- junk that must never sync (matched at any depth) ---
- .DS_Store
- desktop.ini
- Thumbs.db
- ~$*
- .~*
- *.tmp
- *.partial
- .onedriveui-recycle/

# --- folders the user unchecked in "Choose folders" (anchored, top-level) ---
- /Videos/
- /Apps/

# --- nested folders the user unchecked ---
- /Documents/Personal/
- /My Folder/

# NOTE: no "- **" line. Everything not excluded above is synced.
```

**Verified result** for a tree containing `Documents/{Work/w.txt,Personal/p.txt,top.txt}`,
`Pictures/2024/a.jpg`, `Videos/v.mp4`, `My Folder/x.txt`, `Apps/Backup/b.bin`, `root.txt`:

```
p2/Documents/top.txt
p2/Documents/Work/w.txt
p2/Pictures/2024/a.jpg
p2/root.txt
```

### 5.5 Include-style filters file

"Sync only these folders". Must end with `- **`.

```
+ /Documents/Work/**
+ /Pictures/**
+ /*
- **
```

Verified result: `Documents/Work/w.txt`, `Pictures/2024/a.jpg`, `root.txt`.
**Dropping the `+ /*` line drops the root-level files** — verified: without it, `root.txt` is not synced.

### 5.6 Writing the filters file from the "Choose folders" dialog

```python
JUNK = [".DS_Store", "desktop.ini", "Thumbs.db", "~$*", ".~*", "*.tmp", "*.partial"]

def escape_pattern(name: str) -> str:
    # escape rclone glob metacharacters that can legally appear in a filename
    out = []
    for ch in name:
        if ch in "*?[]{}\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)

def write_filters(path: str, excluded_rel_dirs: list[str], recycle_dir_name: str) -> None:
    """excluded_rel_dirs: paths relative to the sync root, forward slashes, no leading/trailing slash.
       e.g. ['Videos', 'Documents/Personal', 'My Folder']"""
    lines = [
        "# OneDriveUI selective-sync filters — generated file, do not edit by hand.",
        "# NOTICE: changing this file requires a bisync --resync run.",
        "",
    ]
    lines += [f"- {p}" for p in JUNK]
    lines.append(f"- /{escape_pattern(recycle_dir_name)}/")
    lines.append("")
    for rel in sorted(excluded_rel_dirs):
        rel = rel.strip("/")
        lines.append(f"- /{escape_pattern(rel)}/")   # anchored, directory rule, no trailing **
    lines.append("")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)      # atomic
```

Rules to enforce in the UI: always write `\n` line endings, always exactly one space after `-`,
never emit trailing spaces, always anchor with a leading `/`, always terminate directory rules with `/`,
never emit `**` on a directory exclusion.

To *preview* the effect without touching the sync state:
```bash
rclone lsf -R --dirs-only --filter-from filters.txt onedrive:
```
(read-only, safe against the real remote).

---

## 6. Comparison, empty dirs, renames, backup dirs

### 6.1 `--compare size,modtime,checksum`

* Default (no flag): inherits `sync` semantics = **size + modtime**.
* `--compare` overrides conflicting flags (`--size-only`, `--checksum`). Don't mix both styles.
* Changing `--compare` **requires a `--resync`** — old listings lack the new attributes.
* The active setting is printed at the top of every run, which is the cleanest thing for the UI to log:

```json
{"Modtime": true, "Size": true, "Checksum": true,
 "HashType1": 1, "HashType2": 1,
 "NoSlowHash": false, "SlowHashSyncOnly": false, "SlowHashDetected": true, "DownloadHash": false}
```

* With checksums on, the `.lst` hash column is populated:
  `-        6 md5:b1946ac92492d2347c6235b4d2611184 - 2026-08-31T… "a.txt"`.
* If both sides support checksums but share **no** hash type, checksums are used only for *intra-side* change
  detection, not cross-side comparison. If only one side supports them, only that side uses them.
* Without modtime in `--compare`, bisync can only say "changed", never "newer/older" — which disables
  `--conflict-resolve newer`.

**Local ↔ onedrive specifics (measured on this machine):**

```
onedrive:  Precision: 1000000000 ns (1 s)   Hashes: ['quickxor']   CaseInsensitive: True
           ListR: False   CanHaveEmptyDirectories: True   DirSetModTime: True
local:     Precision: 1 ns                  Hashes: ['md5','sha1','whirlpool','crc32','sha256',
                                                     'sha512','blake3','xxh3','xxh128','dropbox',
                                                     'hidrive','mailru','quickxor']
```
The **common hash is `quickxor`** — so `--compare size,modtime,checksum` *does* work between the local disk
and OneDrive, but the local side must read and hash every byte of every file on every run. Hence:

**Recommended for OneDriveUI:** `--compare size,modtime` (the default — omit the flag), i.e. exactly what the
Windows client does. Offer `--compare size,modtime,checksum --slow-hash-sync-only` as an opt-in
"Extra verification" setting.

### 6.2 `--ignore-listing-checksum`, `--no-slow-hash`, `--slow-hash-sync-only`, `--download-hash`

| Flag | Effect |
|---|---|
| `--ignore-listing-checksum` | Don't put checksums in the listings / don't use them for delta detection. **Auto-enabled since v1.66 when neither `--checksum` nor `--compare checksum` is used** — you will see `INFO : Setting --ignore-listing-checksum as neither --checksum nor --compare checksum are set.` on every default run. Not the same as `--ignore-checksum` (which governs the post-copy verification). |
| `--no-slow-hash` | Skip checksums on backends that report `SlowHash` (local does), still use them on fast ones. No effect without `--compare checksum`. |
| `--slow-hash-sync-only` | Slow hashes are skipped for listings/deltas/resync but **still used during the sync calls**. Best speed/safety trade-off for a big local tree. Requires a common hash type, else falls back to `--no-slow-hash`. Trade-off: content changed without a size/modtime change is not detected. |
| `--download-hash` | Download files to compute MD5 when no checksum is available. Very slow and data-hungry. Automatically skipped for size < 0 (Google Docs). **Do not enable for OneDrive.** |

### 6.3 `--modify-window`

Default `1ns`. rclone automatically widens it to the coarser of the two backends' precisions, so local↔onedrive
effectively uses **1 s**. Only set it explicitly (e.g. `--modify-window 2s`) if you observe spurious
"File changed: time (newer)" churn (some SharePoint/OneDrive-for-Business sites round to 2 s).

### 6.4 `--create-empty-src-dirs`

Off by default: empty directories are **not** propagated. With the flag, empty-dir creation *and* deletion are
propagated, and directories appear in the listing files as `d       -1 - - <modtime> "EmptyDir"`.

Verified: without it, `p1/EmptyDir` never appears on `p2`; with it, it does.

* Incompatible with `--remove-empty-dirs` (which purges ALL empty dirs on both sides at the end).
* **Do not toggle it back and forth without a `--resync`** — it makes every directory look created/deleted.
* **OneDriveUI should enable it**, because Windows OneDrive syncs empty folders.

### 6.5 `--track-renames` under bisync

Supported since v1.66. Verified working:

```
INFO  : Local file system at /…/p2: Making map for --track-renames
INFO  : Local file system at /…/p2: Finished making map for --track-renames
INFO  : renamed.bin: Moved (server-side) to: orig.bin
INFO  : orig.bin: Renamed from "renamed.bin"
Renamed:                1
```
vs. without it, the same rename costs a full re-upload plus a delete:
```
INFO  : renamed.bin: Copied (new)
INFO  : orig.bin: Deleted
```

Caveats:
* **Not available during `--resync`.** It logs a red-herring at ERROR level (see §10.3).
* Renames still count toward `--max-delete` (the check runs before rename detection).
* `--track-renames-strategy hash|modtime|leaf` (default `hash`). For local↔onedrive the common hash is
  quickxor, so `hash` means hashing the local candidates — usable, but `--track-renames-strategy modtime,leaf`
  is much cheaper if you see slowdowns.
* Renaming a **directory** still looks like mass delete + mass create unless `--track-renames` catches the
  individual files. The cheapest fix is to rename on both sides identically (no `--resync` needed since v1.64).

### 6.6 `--backup-dir1` / `--backup-dir2` — version history + recycle bin (KEY)

**Semantics**
* `--backup-dir1` must be on the **same remote as Path1**; `--backup-dir2` on the same remote as Path2.
* Each must not overlap its Path unless excluded by a filter rule. (Put the cloud recycle dir *outside* the
  synced root if possible; if it must be inside, add `- /.onedriveui-recycle/` to the filters file.)
* If both paths are on the same remote, the plain `--backup-dir` works, but the two sides' deletions get mixed.
  `--backup-dir1`/`2` override `--backup-dir`.
* **A conflict rename is not treated as a delete**, so conflicts do not fill the backup dir — unless a
  same-named conflict file already existed and would be overwritten.

**Layout — measured**

Files are moved (server-side where possible) into the backup dir **preserving the full relative path**, flat
with respect to time:

```
# after deleting p2/Documents/doc.txt  (delete propagates to p1, p1's copy is backed up)
bk1/Documents/doc.txt

# after modifying p1/Documents/doc.txt (p2's old version is backed up before being overwritten)
bk2/Documents/doc.txt      # content: the previous version ("v1")
```

Both *overwrites* and *deletes* land in the backup dir. Log lines:
```
INFO  : Documents/doc.txt: Moved (server-side)
INFO  : Documents/cloudfile.txt: Moved into backup dir
```

**Only ONE version is retained per path** unless you add `--suffix`, because a second backup of the same
relative path overwrites the first. To build real version history, add a timestamp suffix per run:

```bash
--suffix "-$(date +%Y-%m-%dT%H%M%S)" --suffix-keep-extension
```

Measured result of two successive edits:
```
bk2/Documents/doc.txt                       # 1st backup (no --suffix on that run)
bk2/Documents/doc-2026-08-30-233000.txt     # 2nd backup, --suffix -2026-08-30-233000 --suffix-keep-extension
```
Log: `INFO  : Documents/doc.txt: Moved (server-side) to: Documents/doc-2026-08-30-233000.txt`

**OneDriveUI design:**
* Recycle bin = `--backup-dir1 ~/.local/share/onedriveui/recycle/local` and
  `--backup-dir2 onedrive:.onedriveui-recycle` (excluded via `- /.onedriveui-recycle/` in the filters file).
* Always pass `--suffix "-<ISO8601 of run start>" --suffix-keep-extension` so nothing is ever clobbered.
* "Version history" for `Documents/report.docx` = `glob(recycle/*/Documents/report-*.docx)` sorted by the
  parsed timestamp, plus `recycle/*/Documents/report.docx` for the un-suffixed case.
* "Restore" = copy the chosen backup back to its original relative path in the *local* tree; the next bisync
  propagates it up. Do **not** restore straight into the cloud path, or the change is invisible to Path1's
  delta detection until the following run.
* "Empty recycle bin" = delete the backup dirs. They are outside the sync tree, so no bisync state is touched.

---

## 7. Exit codes, error surfacing, progress parsing

### 7.1 Exit codes (documented + verified)

| Code | Meaning | Verified scenario |
|---|---|---|
| `0` | Successful run | normal + resync runs |
| `1` | Non-critical failure; a rerun may succeed | `--max-delete` abort; "all files changed" abort; prior lock file found |
| `2` | Syntax / usage error | unknown flag |
| `7` | **Critical** abort — `--resync` required | missing prior listings; filters file changed; `--check-access` failure; empty Path listing |
| `130` | Interrupted by SIGINT (Graceful Shutdown path) | Ctrl-C — may end in either success or a critical abort; **check the log, not just the code** |

Rclone's general exit codes also apply (3 directory-not-found, 4 file-not-found, 5 temporary error,
6 less-serious errors, 8 `--max-transfer` reached, 9 no files transferred, 10 duration exceeded).

**Critical rule:** exit 130 is *not* by itself a success or a failure. Look for `Graceful shutdown completed
successfully.` + `Bisync successful` (fine) vs `Bisync aborted. Must run --resync to recover.` (needs resync).

### 7.2 The three terminal log lines

Every bisync run ends with exactly one of:

```
INFO  : Bisync successful                                                # ok (also emitted after graceful shutdown)
NOTICE: Bisync aborted. Please try again.                                # non-critical (exit 1)
ERROR : Bisync aborted. Must run --resync to recover.                    # critical (exit 7)
ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.   # critical-but-unlocked
```
plus a `NOTICE: Failed to bisync: <short reason>` line on failure, where `<short reason>` is one of
`bisync aborted`, `too many deletes`, `all files were changed`, `prior lock file found: …`.

Intermediate `ERROR :` / `NOTICE: WARNING listing try N failed.` lines are **not** failures if the run ends in
`Bisync successful` — rclone retries internally. The UI must only treat the *terminal* line as the verdict.

### 7.3 JSON logging: `--use-json-log --color NEVER`

> **MANDATORY: pass `--color NEVER`.** Without it, `msg` fields contain raw ANSI escape sequences even in JSON
> mode — verified: `"msg":"[2mSetting --ignore-listing-checksum …[0m"`. `--color NEVER` also
> normalises the column padding in the `- Path1   …` lines.

**Plain log record** (one JSON object per line on stderr):
```json
{"time":"2026-08-30T23:28:06.368873832-04:00","level":"info","msg":"Copying Path2 files to Path1","source":"bisync/resync.go:44"}
```
Keys: `time` (RFC3339Nano with local offset), `level` (`debug|info|notice|warning|error|critical`),
`msg`, `source` (`<pkg>/<file>.go:<line>` — stable enough to key on, e.g. `bisync/resolve.go:318` = a conflict rename).

**Object-scoped record** (adds three keys):
```json
{"time":"…","level":"info","msg":"Copied (new)","size":8000000,"object":"big.bin","objectType":"*local.Object","source":"operations/copy.go:380"}
{"time":"…","level":"info","msg":"Deleted","object":"f1.bin","objectType":"*local.Object","source":"operations/operations.go:581"}
{"time":"…","level":"info","msg":"Moved (server-side) to: a.txt.conflict1","object":"a.txt","objectType":"*local.Object","source":"operations/operations.go:493"}
```
* `object` — path **relative to the fs root**, forward slashes.
* `objectType` — Go type, e.g. `*local.Object`, `*onedrive.Object`.
* `size` — only on copy records.
* **No `error` key**; failures come through as `"level":"error"` with the message in `msg`.

**Stats record** (emitted every `--stats` interval, `"level":"info"` by default — change with
`--stats-log-level`). `msg` holds the human-readable block; `stats` holds the machine-readable object:

```json
{"time":"…","level":"info",
 "msg":"\nTransferred:   \t   32.544 MiB / 32.544 MiB, 100%, 0 B/s, ETA -\nChecks: …\n",
 "stats":{"bytes":900000,"checks":0,"deletedDirs":0,"deletes":0,"elapsedTime":0.001692985,
          "errors":0,"eta":null,"fatalError":false,"listed":6,"renames":0,"retryError":false,
          "serverSideCopies":0,"serverSideCopyBytes":0,"serverSideMoveBytes":0,"serverSideMoves":0,
          "speed":0,"totalBytes":900000,"totalChecks":0,"totalTransfers":3,
          "transferTime":0.00086067,"transfers":3},
 "source":"accounting/stats.go:551"}
```

While transfers are in flight the `stats` object gains a **`transferring` array**:

```json
"transferring":[{"bytes":3796992,"dstFs":"/…/t_j2/p2","eta":1,"group":"global_stats",
                 "name":"big.bin","percentage":47,"size":8000000,
                 "speed":3167427.94,"speedAvg":3174220.50,"srcFs":"/…/t_j2/p1"}]
```

`transferring[i]` keys: `name`, `size`, `bytes`, `percentage` (int 0–100), `speed` (B/s instantaneous),
`speedAvg` (B/s, `0` until rclone has enough samples), `eta` (seconds, or `null`), `srcFs`, `dstFs`, `group`.

**Progress mapping for the OneDriveUI tray/flyout:**

| UI element | Source |
|---|---|
| Overall percentage | `stats.bytes / stats.totalBytes` (guard `totalBytes == 0`) |
| "N of M files" | `stats.transfers` / `stats.totalTransfers` |
| Speed | `stats.speed` (bytes/s) |
| ETA | `stats.eta` (seconds, may be `null`) |
| Per-file rows | `stats.transferring[]` → `name`, `percentage`, `speed` |
| Errors badge | `stats.errors`, plus `stats.fatalError` / `stats.retryError` booleans |
| Direction arrow | compare `srcFs`/`dstFs` against the configured local path |

**Reference parser:**

```python
import json, subprocess

def run_bisync(argv):
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True, bufsize=1)
    verdict = None
    for line in p.stderr:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue                       # non-JSON preamble (e.g. cobra usage errors)
        if "stats" in rec:
            on_stats(rec["stats"])
            continue
        msg = rec.get("msg", "")
        if "Bisync successful" in msg:                       verdict = "ok"
        elif "Must run --resync to recover" in msg:          verdict = "needs_resync"
        elif "retryable without --resync" in msg:            verdict = "retryable"
        elif "Bisync aborted. Please try again." in msg:     verdict = "retryable"
        on_log(rec)
    rc = p.wait()
    return verdict, rc
```

Non-JSON stderr can appear before rclone's logger is initialised (flag-parsing errors go to stdout/stderr
as plain cobra usage text), so always guard the `json.loads`.

### 7.4 `--stats-one-line` (non-JSON alternative)

```
--stats 500ms --stats-one-line --stats-one-line-date
```
yields a single line per interval, e.g. `Transferred: 878.906 KiB / 878.906 KiB, 100%, 0 B/s, ETA -`.
Useful for a log file; **JSON is strictly better for the UI** — prefer `--use-json-log`.

---

## 8. Running bisync via the rc API (`sync/bisync`)

### 8.1 Starting the daemon

```bash
rclone rcd --rc-addr 127.0.0.1:5572 \
           --rc-user onedriveui --rc-pass "$SECRET" \
           --rc-serve=false --rc-web-gui=false \
           --use-json-log --color NEVER
```
(`--rc-no-auth` is fine only for a loopback socket in dev.)

### 8.2 Every `sync/bisync` parameter

Verbatim from `rclone rc --help`/<https://rclone.org/rc/>, cross-checked against the CLI flags:

| rc parameter | Type | CLI equivalent |
|---|---|---|
| `path1` **(required)** | string | Path1 positional |
| `path2` **(required)** | string | Path2 positional |
| `dryRun` | bool | `--dry-run` |
| `resync` | bool | `--resync` |
| `resyncMode` | string | `--resync-mode` (`path1\|path2\|newer\|older\|larger\|smaller`) |
| `checkAccess` | bool | `--check-access` |
| `checkFilename` | string | `--check-filename` |
| `checkSync` | string | `--check-sync` (`true\|false\|only`) |
| `compare` | string | `--compare` (`size,modtime,checksum`) |
| `conflictResolve` | string | `--conflict-resolve` |
| `conflictLoser` | ConflictLoserAction | `--conflict-loser` (`num\|pathname\|delete`) |
| `conflictSuffix` | string | `--conflict-suffix` |
| `createEmptySrcDirs` | bool | `--create-empty-src-dirs` |
| `removeEmptyDirs` | bool | `--remove-empty-dirs` |
| `downloadHash` | bool | `--download-hash` |
| `ignoreListingChecksum` | bool | `--ignore-listing-checksum` |
| `noSlowHash` | bool | `--no-slow-hash` |
| `slowHashSyncOnly` | bool | `--slow-hash-sync-only` |
| `filtersFile` | string | `--filters-file` |
| `force` | bool | `--force` |
| `backupDir1` | string | `--backup-dir1` |
| `backupDir2` | string | `--backup-dir2` |
| `maxLock` | Duration | `--max-lock` |
| `noCleanup` | bool | `--no-cleanup` |
| `recover` | bool | `--recover` |
| `resilient` | bool | `--resilient` |
| `workdir` | string | `--workdir` |

Plus the universal rc parameters: `_async`, `_group`, `_config`, `_filter`, `_context`.

Flags with no rc parameter (`--max-delete`, `--transfers`, `--checkers`, `--track-renames`, `--suffix`,
`--suffix-keep-extension`, `--modify-window`, `--bwlimit`, `--inplace`, `--partial-suffix`, `--fix-case`,
`--metadata`) must be set either globally on the daemon (`rclone rcd --transfers 4 …`) or per-call via
`_config` using the `options/get` `main` key names — e.g. `"_config":{"Transfers":4,"TrackRenames":true}`.

> ### 8.2.1 SHOWSTOPPER: `--max-delete` is broken over the rc (measured, v1.75.0)
>
> `sync/bisync` invoked through the rc behaves as if **`--max-delete 0`** — *any* deletion aborts the run.
> Measured with 10 files, 1 deleted (10 %):
>
> ```
> ERROR : Safety abort: too many deletes (>0%, 1 of 10) on Path1 "/…/p1/". Run with --force if desired.
> ```
> HTTP 500, `{"error":"too many deletes", …}`.
>
> Neither of these changes it:
> * `"_config": {"MaxDelete": 90}` → still `>0%`.
> * starting the daemon with `rclone rcd --max-delete 90` → still `>0%`.
>
> The only escape is the rc parameter `"force": true`, which **disables the delete-percentage check *and* the
> "all files changed" check entirely** — i.e. you lose the safety net rather than tuning it.
> Over the CLI the same scenario works correctly (`--max-delete 75` allowed 6-of-10 deletions).
>
> **Consequence: do not run bisync through the rc for OneDriveUI.** Use a subprocess (§8.6).

### 8.3 Synchronous call and its output (real)

```bash
curl -s -X POST 'http://127.0.0.1:5572/sync/bisync' -H 'Content-Type: application/json' \
  -d '{"path1":"/home/me/OneDrive","path2":"onedrive:","workdir":"/home/me/.local/state/onedriveui/bisync","resync":true}'
```
```json
{
  "basePath": "/…/work/tmp_…_p1..tmp_…_p2",
  "listing1": "/…/work/tmp_…_p1..tmp_…_p2.path1.lst",
  "listing2": "/…/work/tmp_…_p1..tmp_…_p2.path2.lst",
  "logFile":  "",
  "output":   "",
  "session":  "tmp_…_p1..tmp_…_p2",
  "workDir":  "/…/work"
}
```

This is gold for the UI: **rclone hands you the session name and the two listing-file paths**, so you never
have to re-implement the sanitisation rules of §1.3.

### 8.4 Async with a job id (real)

```bash
JOB=$(curl -s -X POST 'http://127.0.0.1:5572/sync/bisync' -H 'Content-Type: application/json' \
  -d '{"path1":"…","path2":"…","workdir":"…","_async":true,"_group":"onedrive-sync"}')
# -> {"executeId":"b16db48a-a4b6-4439-ab02-e8b6d66c7022","jobid":103}
```

Poll:
```bash
curl -s -X POST .../job/status  -d '{"jobid":103}'
```
```json
{"duration":0.000755156,"endTime":"2026-08-30T23:28:23.961543895-04:00","error":"",
 "executeId":"b16db48a-…","finished":true,"group":"onedrive-sync","id":103,
 "output":{ …the same object as §8.3… },
 "startTime":"2026-08-30T23:28:23.960788736-04:00","success":true}
```

Live progress for that job:
```bash
curl -s -X POST .../core/stats -d '{"group":"onedrive-sync"}'
```
returns exactly the same object as the `stats` key in §7.3 (including `transferring[]`).

Other useful endpoints: `job/list`, `job/stop` (`{"jobid":103}` — sends the graceful-cancel path),
`core/stats-reset`, `core/transferred`.

### 8.5 rc error shapes (real)

Missing parameter (HTTP 400):
```json
{"error":"Didn't find key \"path2\" in input","input":{"path1":"…"},"path":"sync/bisync","status":400}
```

bisync abort (HTTP 500):
```json
{"error":"bisync aborted","input":{"path1":"…","path2":"…","workdir":"…"},"path":"sync/bisync","status":500}
```

> **Gotcha:** bisync's own log lines are re-emitted by the daemon **double-timestamped and level-shifted**,
> e.g. `NOTICE: 2026/08/30 23:40:09 ERROR : Safety abort: too many deletes (>0%, 1 of 10) on Path1 "…"`.
> Any parser reading `rcd`'s log must strip the outer `<time> <LEVEL>: ` prefix before matching.

> **Gotcha:** the rc error string is only the terse `"bisync aborted"` / `"too many deletes"` — it does **not**
> tell you whether a `--resync` is required. To distinguish, you must either read the daemon's log stream
> (run `rclone rcd --use-json-log --color NEVER` and tail it) or check for `.lst-err` in the workdir.
> Async jobs put the same string in `job/status.error`.

### 8.6 CLI vs rc — which should OneDriveUI use?

**Recommendation: drive bisync via `subprocess` with `--use-json-log --color NEVER`, not the rc.** Reasons:
* **`--max-delete` does not work over the rc** (§8.2.1) — the single most important safety feature is either
  0 % or disabled.
* You get the full, unambiguous log stream (including `Must run --resync to recover`) on one pipe.
* You get an honest exit code (1 vs 7 vs 130) which the rc collapses to `500 / "bisync aborted"`.
* SIGINT for Graceful Shutdown is a direct `Popen.send_signal(signal.SIGINT)`.
* No extra daemon, no auth secret to manage.

Keep the rc daemon around for the *other* jobs (`operations/about` for quota, `core/stats`,
`vfs/*` for the on-demand mount), but let bisync be a child process.

---

## 9. Performance guidance

| Flag | Guidance for local ↔ `onedrive:` |
|---|---|
| `--fast-list` | **Bisync enables it by default** for backends that support ListR. `onedrive` reports `ListR: False`, so it is a no-op here. If you ever see the Google-Drive-style empty-dir bug on another backend, add `--disable ListR`. |
| `--checkers` | Default 8. OneDrive tolerates 8–16 well. Higher raises 429 risk. |
| `--transfers` | Default 4. **Keep 4** for OneDrive personal; the Graph API throttles aggressively above that. |
| `--onedrive-chunk-size` | Default 10 Mi. Raise to `32M`/`64M` for large files on a fast link. |
| `--tpslimit` / `--tpslimit-burst` | Consider `--tpslimit 10 --tpslimit-burst 20` if you see `429 Too Many Requests`. |
| `--no-slow-hash` / `--slow-hash-sync-only` | Only matter with `--compare checksum`. Prefer `--slow-hash-sync-only`. |
| `--check-sync=false` | Saves a full double listing-load at the end. Worth it above ~200 k files; keep it on below that. |
| `--resilient --recover --max-lock 2m` | The "unreliable link" triad. Always on for a desktop client. |
| `--retries 3 --retries-sleep 30s` | Requires `--resilient`. |
| `--low-level-retries 10` | rclone default; fine. |
| `--bwlimit` | Expose as a user setting; accepts a timetable (`--bwlimit "08:00,512k 19:00,off"`) which is exactly the Windows client's "limit upload rate" scheduler. |
| `--list-cutoff 1000000` | Sorts directory listings on disk above this many entries; leave default. |
| `-M/--metadata` | Preserves mtime/permissions metadata. Recommended (`-M`). |
| `--inplace` | **NEVER.** See §4.9. |

**On interruption:** the current run's mutations are partial but never inconsistent in a dangerous way —
copies that completed are complete; the `.lst` snapshots are only advanced at the very end. The three outcomes:

1. **Graceful shutdown succeeded** → `.lst` + `.lst-old` present, next run is a normal run.
2. **Killed but `.lst` intact** (e.g. SIGKILL before the update phase) → next run works; if it also left a
   `.lck`, wait for `--max-lock` expiry or delete it.
3. **Critical abort** → `.lst-err`; next run needs `--resync` (or is retryable under `--resilient`).

**`.partial` files are a real hazard.** Verified: SIGKILL mid-transfer left
`p2/big.bin.677c7953.partial`, and the *next* bisync run treated it as a brand-new file and dutifully synced
it back to Path1:
```
INFO  : - Path2    File is new               - big.bin.677c7953.partial
INFO  : big.bin.677c7953.partial: Copied (new)
```
**Always put `- *.partial` in the filters file** (the default `--partial-suffix` is `.partial`), or set a
distinctive `--partial-suffix .onedriveui-partial` and filter that.

---

## 10. Known limitations, failure modes, recovery procedures

### 10.1 Failure/recovery matrix

| Symptom (exact string) | Exit | Cause | Recovery |
|---|---|---|---|
| `cannot find prior Path1 or Path2 listings` | 7 | first run, or prior critical abort left `.lst-err` | `--resync` |
| `filters file has changed (must run --resync)` | 7 | `filters.txt` MD5 mismatch | `--resync` (this is the normal selective-sync path) |
| `check file check failed` / `Access test failed: Path1 count N, Path2 count M` | 7 | missing/unbalanced `RCLONE_TEST` | fix/seed the test files, then `--resync` |
| `Empty current Path2 listing. Cannot sync to an empty directory` | 7 | one side is empty (unmounted drive, wiped folder) | investigate first; then `--resync` from the good side (`--resync-mode path1` or `path2`) |
| `Safety abort: too many deletes (>50%, N of M) on Path1` | 1 | mass delete or a directory rename | confirm with the user → `--force`, or raise `--max-delete`, or `--resync` to restore |
| `Safety abort: all files were changed on Path1` | 1 | clock/timezone change, bulk `touch` | `--force` (the changed side wins) or `--resync` |
| `prior lock file found: …lck` | 1 | concurrent or crashed run | check `PID` liveness (§4.5); delete the lock or wait for `--max-lock` |
| `Lock file exists, but contents are unreadable` | 1 | truncated `.lck` | delete it manually; with `--max-lock > 0` rclone does it for you |
| `file name too long` on the `.lck`/`.lst` | 1/7 | session name > NAME_MAX (255 B) | **no hashing fallback exists** — shorten the paths, or use a short-named remote |
| `Safety abort: too many deletes (>0%, 1 of 10)` when invoked over the rc | — (HTTP 500) | rc bug, §8.2.1 | stop using the rc for bisync; use a subprocess |
| Copy/move error mid-sync (`not deleting files as there were IO errors`) | 7 | backend error, disk full, permission | fix, then `--resync` (even with `--resilient`) |
| `Bisync successful` but with earlier `ERROR :` / `WARNING listing try N failed` lines | 0 | internal retry succeeded | nothing — do **not** surface as a failure |

### 10.2 Other documented limitations

* **Empty directories** are not synced without `--create-empty-src-dirs`; toggling the flag without a
  `--resync` looks like every directory was created/deleted.
* **Renamed directories** become delete+create unless `--track-renames` catches the files, and the deletes
  still count against `--max-delete`.
* **Concurrent modification** during a run: greatly improved by the v1.66 snapshot model (changes missed this
  run are caught next run), but a file changing at the exact instant it's read can still error, and with
  `--inplace` on local/FTP/SFTP it can corrupt and propagate.
* **Case / Unicode**: no longer critical errors since v1.66. OneDrive is **case-insensitive**
  (`CaseInsensitive: True`), the local ext4 is case-sensitive. Relevant flags: `--fix-case`,
  `--ignore-case-sync`, `--no-unicode-normalization`. With `--fix-case`, when a file is changed on both sides,
  checksums match and only the case differs, **Path1's spelling wins**.
* **Google Docs** and other unknown-size objects: size `-1`, no checksum; never use `--checksum`/`--size-only`
  with them. Irrelevant for OneDrive, but note that OneNote notebooks on OneDrive behave similarly and are
  best excluded (`- *.one`, `- *.onetoc2`).
* **Backend test status**: OneDrive is **not** on the known-issues list — it is a fully-supported bisync backend.
* **`--dry-run` oddity**: because the copies don't happen, the follow-on deletes appear as
  `Not deleting as --dry-run`. Deletes shown in a dry run whose files would have been copied first can be
  ignored. Also note that in `--dry-run` the delta lines are emitted at NOTICE, not INFO
  (verified: `NOTICE: - Path2  File is new  - newoncloud.txt`).

### 10.3 Red herrings the UI must NOT treat as failures

```
ERROR : Local file system at /…: Ignoring --track-renames as it doesn't work with copy or move, only sync
```
Emitted at **ERROR level** on every `--resync` run when `--track-renames` is set. Completely harmless.
Either strip `--track-renames` from resync invocations, or whitelist this exact message.

```
NOTICE: WARNING  listing try 1 failed.        - onedrive:
ERROR : <path>: error listing: <transient>
```
Internal retries. Only the terminal verdict line matters.

---

## 11. Complete worked example (real output, run locally)

Setup: `demo/local` ↔ `demo/cloud` standing in for `~/OneDrive` ↔ `onedrive:`, with the full recommended
flag set. Script: two runs — an initial `--resync`, then a run with a conflicting edit, a cloud-side delete,
and a new local file.

```bash
FLAGS=(--workdir "$D/work" --filters-file "$D/filters.txt"
  --conflict-resolve newer --conflict-loser num --conflict-suffix conflict
  --backup-dir1 "$D/bk_local" --backup-dir2 "$D/bk_cloud"
  --max-delete 25 --resilient --recover --max-lock 2m
  --create-empty-src-dirs --track-renames --transfers 4 --checkers 8 --color NEVER)

rclone bisync "$D/local" "$D/cloud" "${FLAGS[@]}" --resync -v
rclone bisync "$D/local" "$D/cloud" "${FLAGS[@]}" -v
```

**Run 1 — `--resync` (exit 0):**
```
INFO  : Setting --ignore-listing-checksum as neither --checksum nor --compare checksum are set.
INFO  : lock file renewed for 2m0s. New expiration: 2026-08-30 23:34:01.599598987 -0400 -04
INFO  : Synching Path1 "/…/demo/local/" with Path2 "/…/demo/cloud/"
INFO  : Using filters file /…/demo/filters.txt
INFO  : Storing filters file hash to /…/demo/filters.txt.md5
INFO  : Copying Path2 files to Path1
INFO  : - Path2             Resync is copying files to                  - Path1
ERROR : Local file system at /…/demo/local: Ignoring --track-renames as it doesn't work with copy or move, only sync
INFO  : Documents/cloudfile.txt: Copied (new)
INFO  : - Path1             Resync is copying files to                  - Path2
INFO  : Pictures: Made directory with metadata (mtime=2026-08-30T23:32:01.572742197-04:00)
INFO  : Pictures/photo.jpg: Copied (new)
INFO  : Documents/report.docx: Copied (new)
INFO  : Resync updating listings
INFO  : Validating listings for Path1 "/…/demo/local/" vs Path2 "/…/demo/cloud/"
INFO  : Bisync successful
Transferred:   	         25 B / 25 B, 100%, 0 B/s, ETA -
Checks:                 1 / 1, 100%, Listed 13
Transferred:            3 / 3, 100%
```

**Run 2 — conflicting edit + cloud delete + new local file (exit 0):**
```
INFO  : Building Path1 and Path2 listings
INFO  : Path1 checking for diffs
INFO  : - Path1             File changed: size (larger), time (newer)   - Documents/report.docx
INFO  : - Path1             File is new                                 - newfile.txt
INFO  : Path1:    2 changes:    1 new,    1 modified,    0 deleted
INFO  : (Modified:    1 newer,    0 older,    1 larger,    0 smaller)
INFO  : Path2 checking for diffs
INFO  : - Path2             File was deleted                            - Documents/cloudfile.txt
INFO  : - Path2             File changed: size (larger), time (newer)   - Documents/report.docx
INFO  : Path2:    2 changes:    0 new,    1 modified,    1 deleted
INFO  : Applying changes
NOTICE: - WARNING           New or changed in both paths                - Documents/report.docx
INFO  : Documents/report.docx: Path2 is newer. Path1: 2026-08-30 23:32:01.603130472 -0400 -04, Path2: 2026-08-30 23:32:02.704115234 -0400 -04, Difference: 1.100984762s
INFO  : Documents/report.docx: The winner is: Path2
NOTICE: - Path1             Renaming Path1 copy                         - /…/demo/local/Documents/report.docx.conflict1
INFO  : Documents/report.docx: Moved (server-side) to: Documents/report.docx.conflict1
NOTICE: - Path1             Queue copy to Path2                         - /…/demo/cloud/Documents/report.docx.conflict1
NOTICE: - Path2             Not renaming Path2 copy, as it was determined the winner - /…/demo/cloud/Documents/report.docx
NOTICE: - Path2             Queue copy to Path1                         - /…/demo/local/Documents/report.docx
INFO  : - Path1             Queue copy to Path2                         - /…/demo/cloud/newfile.txt
INFO  : - Path1             Queue delete                                - /…/demo/local/Documents/cloudfile.txt
INFO  : - Path2             Do queued copies to                         - Path1
INFO  : Documents/report.docx: Copied (new)
INFO  : Documents/cloudfile.txt: Moved (server-side)
INFO  : Documents/cloudfile.txt: Moved into backup dir
INFO  : - Path1             Do queued copies to                         - Path2
INFO  : newfile.txt: Copied (new)
INFO  : Documents/report.docx.conflict1: Copied (new)
INFO  : Updating listings
INFO  : Validating listings for Path1 "/…/demo/local/" vs Path2 "/…/demo/cloud/"
INFO  : Bisync successful
Transferred:   	         53 B / 53 B, 100%, 0 B/s, ETA -
Checks:                12 / 12, 100%, Listed 22
Deleted:                1 (files), 0 (dirs), 11 B (freed)
Renamed:                2
Transferred:            4 / 4, 100%
Server Side Moves:      2 @ 22 B
```

**Resulting trees:**
```
local/                                cloud/                                bk_local/
├── Documents/                        ├── Documents/                        └── Documents/
│   ├── report.docx        (P2 wins)  │   ├── report.docx                       └── cloudfile.txt
│   └── report.docx.conflict1         │   └── report.docx.conflict1          (the deleted file's
├── newfile.txt                       ├── newfile.txt                         local copy — this is
└── Pictures/photo.jpg                └── Pictures/photo.jpg                  our recycle bin)

work/
├── <session>.path1.lst      <session>.path1.lst-old
└── <session>.path2.lst      <session>.path2.lst-old
```

---

## 12. Implementation checklist for OneDriveUI

1. **Own the workdir.** `--workdir ~/.local/state/onedriveui/bisync`. Never rely on `~/.cache/rclone/bisync`.
2. **Compute the session name yourself** with the §1.3 rules *or* — better — take `session`, `listing1`,
   `listing2` from the rc `sync/bisync` response once at setup time and cache them.
3. **Always** `--use-json-log --color NEVER --stats 500ms`. Parse per §7.3.
4. **State machine** driven by the terminal verdict line, not the exit code alone
   (`ok` / `retryable` / `needs_resync`), with exit 130 disambiguated by the log.
5. **Resync triggers**: no `.lst` present; `.lst-err` present; filters file MD5 mismatch;
   user pressed "Reset sync". Nothing else.
6. **Selective sync** = regenerate `filters.txt` atomically → `--resync` → on exit 0, `rmtree` the newly
   excluded local folders.
7. **Recycle bin / version history** = `--backup-dir1`/`--backup-dir2` + a per-run
   `--suffix "-<ISO8601>" --suffix-keep-extension`; exclude the cloud recycle dir in the filters file.
8. **Conflict UI** = `- WARNING New or changed in both paths` for live toasts + a `**/*.conflict[0-9]*`
   glob for the persistent "Resolve conflicts" list.
9. **Stuck-run detection** = read `<session>.lck`, check `PID` liveness and `TimeExpires`; always run with
   `--max-lock 2m`.
10. **Pause/quit** = `SIGINT` and wait up to 90 s for `Graceful shutdown completed successfully.`
    Never `SIGKILL`. Never `--inplace`. Always filter out `*.partial`.
11. **Seed `RCLONE_TEST`** at the sync root (and inside each top-level included folder) during onboarding,
    then enable `--check-access` — this is the single best guard against "the network blipped, delete
    everything".
12. **Periodic verification** job: `rclone check -MvPc "$LOCAL" onedrive: --filter-from filters.txt`
    weekly, not `--check-sync only`.
