# OneDriveUI

A OneDrive client for Linux that reproduces the **Microsoft OneDrive sync client for Windows 11** —
its features, its wording and its Fluent look — on top of `rclone` as the sync engine.

![The windows and dialogs, dark theme](docs/gallery-dark.png)

> **Status: alpha.** All fifteen work packages are built and the suite passes (4708 tests), but the
> client has not yet been through the 24-hour stress run, and the Nautilus integration has not been
> exercised in a long real session. Read [`docs/PENDING.md`](docs/PENDING.md) before trusting it with
> anything you cannot lose.

---

## What it is

One GUI application, two supervised `rclone` services, one Nautilus extension.

| Surface | What you get |
|---|---|
| **Tray icon** | A StatusNotifierItem with ten distinct states and an eight-frame spinner |
| **Activity Center** | The 360 px flyout, as a normal window — Wayland forbids self-positioning |
| **Settings** | 1024×720, four-page navigation: Sync, Account, Notifications, About |
| **Files On-Demand** | `rclone mount --vfs-cache-mode full` plus a real pin table: online-only, locally available, always available |
| **File manager** | Emblems, a Status column and a OneDrive submenu in Nautilus |
| **Notifications** | Toasts with action buttons, over `org.freedesktop.Notifications` |
| **First run** | The nine-screen setup wizard |

**The governing design principle** is that every visible surface is a projection of one derived state.
The tray icon, the tooltip, the Activity Center status line, the Settings badges and the Nautilus
emblems all read from a single `SyncState`, produced by one pure reducer over one immutable `Facts`
snapshot. The tray cannot say "synced" while the flyout says "paused" — they are the same value
rendered twice.

**The governing safety principle** is that nothing authoritative lives in memory. Your *intent* lives
in `config.json`; latched hazards and history live in SQLite; everything else is re-observed each tick
from the kernel (`/proc/self/mounts`, `statvfs`, inotify), from rclone's rc API, or from rclone's own
on-disk state. A `SIGKILL` of the GUI loses nothing.

---

## Requirements

Four things `pip` cannot install correctly, and the installer refuses to continue without the first three:

| | Why pip cannot do it |
|---|---|
| **rclone** ≥ 1.75 | a Go binary |
| **PySide6** | the PyPI wheel ships its own Qt and *shadows* the system one. The symptom is not an import error — it is a tray icon that registers no StatusNotifierItem |
| **PyGObject** | same problem, against the system GLib. It carries the notifications, the network and power monitors |
| **nautilus-python** 4.1 | optional. Without it everything works except the file-manager emblems and Status column |

Plus Python ≥ 3.12. On Arch / CachyOS:

```bash
sudo pacman -S rclone pyside6 python-gobject nautilus-python
```

Developed and tested against CachyOS, GNOME Shell 50.4 on Wayland, Python 3.14.7, PySide6 6.11.2,
rclone v1.75.0.

---

## Install

```bash
git clone https://github.com/Danielxpj/OneDriveUI
cd OneDriveUI
./scripts/install.sh
```

It checks the dependencies above and stops before touching anything if one is missing, creates
`.venv` **with `--system-site-packages`** (that flag is the whole reason this script exists), installs
the package editable, links `~/.local/bin/onedriveui`, installs the Nautilus extension, 27 icons and
the launcher entry, and finishes with `--doctor`.

It does **not** enable autostart, run the wizard, sign you in, or mount anything.

```bash
nautilus -q          # Nautilus does not reload extensions while it is running
onedriveui           # first run opens the setup wizard
```

To remove it:

```bash
./scripts/install.sh --uninstall
```

which takes out the extension, the icons, the launcher and the venv, and leaves your config, your
database, your `rclone.conf` and your files exactly where they are.

---

## Command line

```
onedriveui                    the GUI
onedriveui --state            one word: the current sync state
onedriveui --status           the whole snapshot, as JSON
onedriveui --doctor           every self-check, and what is wrong
onedriveui --diagnostics PATH a redacted bundle for a bug report
onedriveui --pause [HOURS]    pause syncing (2, 8, 24, or until you resume)
onedriveui --open-folder      open the sync root
onedriveui --settings         open Settings
onedriveui --install-extension / --uninstall-extension
```

`--state`, `--status` and `--doctor` run the *whole engine* headless — the same fact collector, the
same reducer — so their answer is the answer the tray is showing, not a separate guess made for the
command line.

---

## How it works

Three processes, each doing one job:

```
onedriveui                     the GUI. Owns no sync state; renders one.
  │
  ├── onedriveui-rcd.service          rclone rcd — the control plane
  │                                   config, token, quota, remote listings
  │
  └── onedriveui-mount@<acct>.service rclone mount — the data plane
                                      the VFS, the cache, the transfers
```

Every tick, a `FactCollector` reads ten named sources within a 1500 ms budget — a source that misses
its budget carries its previous value forward and is marked *stale* rather than inventing one. The
resulting immutable `Facts` goes through a 17-rung first-match-wins ladder that is pure and
clock-free, and a debouncer with per-state hysteresis decides whether the answer is stable enough to
show. Every world-changing action goes through one method, `Supervisor.do(action, **kw)`.

The full design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the frozen interfaces are in
[`docs/CONTRACTS.md`](docs/CONTRACTS.md).

---

## Safety

Fifteen hard invariants are enforced in code by `onedriveui/rc/guards.py`, which raises
`SafetyRefusal`. None is overridable by config. A few, to show what kind of rule they are:

- **I1** — no `--onedrive-*` flag on any rclone command line. A backend override on the command line
  renames the fs to `onedrive{HASH}:` and silently relocates the entire VFS cache, turning every
  materialised file online-only.
- **I3** — nothing whose VFS sidecar is `Dirty` may be evicted, force-unmounted or bisync'd around.
  A dirty cache item is a local change that exists nowhere else.
- **I8** — `operations/cleanup` is never called. On OneDrive it deletes *file versions*, not the
  trash, contradicting its own help text.
- **I10** — every local deletion this app performs goes to the freedesktop Trash, never `unlink()`.
- **I15** — `--resync` is never run on a schedule; it requires an answered decision. A scheduled
  resync resurrects deleted files forever.

The full table, with the failure each one prevents, is §3 of `docs/ARCHITECTURE.md`.

---

## What it cannot do

Honest gaps, each with a cause that is not fixable from here:

| Gap | Why |
|---|---|
| Remote changes take ~60 s to appear | `ChangeNotify` has no rc endpoint; it is consumed only by the VFS. Windows is push-based |
| "Remove link" cannot revoke a share | rclone declares `--unlink` and never reads it. The control ships **disabled**, with a web hand-off — never a silent lie |
| No cloud recycle-bin browse or restore | no rclone API at all. Web deep-link, plus an in-app trash for deletions made through this UI |
| No version history | rclone can only *delete* versions, and Personal drives cannot even do that |
| Emblems sit beside the filename, not in the corner | Nautilus 4.x renders them that way |
| The Activity Center is not anchored to the tray | Wayland forbids a client from positioning its own windows |
| `du` overstates usage on the mount | a dehydrated file reports its full size. Our own numbers come from `vfs/stats` |

§14 of `docs/ARCHITECTURE.md` has the rest, including the explicit non-goals.

---

## Testing against a real account

```bash
./scripts/livetest.sh            # set up and run
./scripts/livetest.sh --doctor
./scripts/livetest.sh --teardown
```

This mounts a **dedicated subfolder** — it creates `onedrive:OneDriveUI-test`, an rclone `alias`
remote pointing at exactly that folder, and mounts *that* at `~/.onedriveui-test`. The client cannot
see anything else in your account.

> **Never mount a remote you already have mounted, a second time.** Two mounts of one remote each keep
> their own directory cache and neither knows about the other. A rename made against a stale listing
> does what rclone always does when overwriting: delete the destination on the server, then move the
> source — and the move fails. That is not hypothetical; it is why this script was redesigned.

The mountpoint is a hidden directory on purpose. GLib hides any mount whose path contains a dot
component, which keeps a second OneDrive-shaped entry out of the Nautilus sidebar right below your
real one. Open it with `Ctrl+L` or `Ctrl+H`.

---

## Development

```bash
python3 -m pytest -q             # 4708 tests
scripts/preview.py --list        # every window and dialog, openable on its own
scripts/preview.py settings
scripts/preview.py --all --shot /tmp/ui   # a PNG of each, headless
```

102 modules, ~54k lines, 61 test files. The build was directed by three documents treated as
authority: `docs/BUILD_PLAN.md` (fifteen packages with exclusive file ownership),
`docs/CONTRACTS.md` (frozen signatures) and `docs/ARCHITECTURE.md`.

---

## On disk

```
~/.config/onedriveui/        config.json (0600, atomic, .bak), filters
~/.local/share/onedriveui/   state.db (SQLite, WAL)
~/.local/state/onedriveui/   bisync workdir, run logs, app.log
~/.cache/rclone/             the VFS cache — rclone's, not ours
~/.config/rclone/rclone.conf the only place backend options live (I1), and
                             the OAuth token, which is never logged (I14)
```

---

## License

MIT.
