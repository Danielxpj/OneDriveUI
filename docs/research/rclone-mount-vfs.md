# rclone mount and the VFS layer — authoritative reference for OneDriveUI

**Scope:** everything the OneDriveUI implementation needs to build "Files On-Demand" on top of
`rclone mount` + the rclone VFS cache.

**Verified against:** rclone **v1.75.0** (`/usr/bin/rclone`, go1.26.5, linux/amd64), kernel
6.18.42-1-cachyos-lts, systemd 261, fusermount3 3.x at `/usr/bin/fusermount3`,
`/dev/fuse` mode `crw-rw-rw-`, GNOME on Wayland.
Every claim marked **[V]** was verified empirically on this machine on 2026-08-30 with a
throwaway `local` remote (`testlocal:`) in a scratch config. Claims marked **[D]** come from
rclone.org docs. The user's real `onedrive:` remote was **not** mutated; only read-only
queries (`rclone backend features onedrive:`, `GET`-equivalent rc calls on its existing
rc port 5572) were made against it.

---

## 0. TL;DR for the implementer

| Question | Answer |
| --- | --- |
| Which cache mode = Files On-Demand? | `--vfs-cache-mode full`. Nothing else gives you sparse partial materialisation. |
| How do I know a file is locally available? | Read `~/.cache/rclone/vfsMeta/<fsname>/<path>` JSON. `Rs == [{Pos:0,Size:<Size>}]` ⇒ fully cached. `Rs == null`/`[]` ⇒ online-only. Anything else ⇒ partial. **[V]** |
| Live/authoritative alternative? | `SEEK_DATA`/`SEEK_HOLE` on `~/.cache/rclone/vfs/<fsname>/<path>` returns byte-identical ranges to `Rs`. **[V]** |
| How do I pin a file? | Read it end-to-end through the mount (`cat file > /dev/null`). There is no rc "pin" endpoint. **[V]** |
| How do I free up space for a file? | `unlink()` **both** `vfs/<path>` and `vfsMeta/<path>`. rclone logs `detected external removal of cache file` and recovers cleanly. There is no rc "evict" endpoint. **[V]** |
| Does OneDrive support ChangeNotify? | **Yes.** `ChangeNotify: True`, and the live mount reports `{"enabled":true,"supported":true,"interval":{"string":"1m0s"}}`. **[V]** |
| How do I detect a live mount? | `/proc/mounts` entry with fstype `fuse.rclone` **AND** a `statfs()` that does not return `ENOTCONN` (errno 107). `mount/listmounts` does **not** see CLI-started mounts. **[V]** |
| Biggest footgun? | Adding/removing a backend flag (e.g. `--onedrive-chunk-size`) changes the fs canonical name to `onedrive{HASH}:` which changes the cache directory and **orphans the entire cache**. **[V]** |

---

## 1. Mount mechanics on Linux

### 1.1 Invocation

```console
rclone mount <remote>:<path> /path/to/empty/existing/dir [flags]
```

The mountpoint must be an **empty, existing** directory, unless `--allow-non-empty` is
given. **[D]**

**[V]** Exact error when the mountpoint is not empty:

```
CRITICAL: Fatal error: failed to mount FUSE fs: "/…/mnt4" is not empty, use --allow-non-empty to mount anyway
```

`rclone mount` runs in the **foreground by default**. It talks to the kernel via
`libfuse`/`fusermount3`; the actual mount syscall is performed by the setuid helper
`/usr/bin/fusermount3`, which is why an unprivileged user can mount at all.

### 1.2 `--daemon` vs foreground — **use foreground under systemd**

`--daemon` makes the parent set up the mount, fork a child, wait for readiness
(`--daemon-wait`, default `1m0s`), and exit. **[D]**

**[V] CRITICAL BUG in v1.75.0: `--daemon` is incompatible with `--rc --rc-addr`.**
The parent binds the rc port *before* forking, then the child tries to bind the same
port and dies:

```
NOTICE: Serving remote control on http://127.0.0.1:5599/
…
CRITICAL: Failed to start remote control: failed to init server: listen tcp 127.0.0.1:5599: bind: address already in use
ERROR : Daemon timed out. Failed to terminate daemon pid 10244: os: process already finished
CRITICAL: Fatal error: daemon exited with error code 1
```

Since OneDriveUI **needs** the rc API, **never use `--daemon`.** Run rclone in the
foreground and let systemd own the process (§6).

**[V]** `--daemon` also suppresses the child's stderr entirely — a failing mount reports
only `daemon exited with error code 1`. If you ever do use it, `--log-file` is mandatory
to see the real error. **[D]** confirms: *"as background output is suppressed, use
`--log-file` with `--log-format=pid,…` to monitor"*.

### 1.3 Clean shutdown

| Situation | Action |
| --- | --- |
| Foreground process you own | `SIGINT` or `SIGTERM` → rclone unmounts itself. **[V]** log: `INFO : Signal received: terminated` … `NOTICE: /…/mnt: Unmounted rclone mount` … `INFO : Exiting...` |
| Any mount, from outside | `fusermount3 -u /path/to/mount` (lazy: `-uz`) |
| systemd unit | `systemctl --user stop <unit>`; add `ExecStop=/usr/bin/fusermount3 -uz %h/OneDrive` as a belt-and-braces |

`fusermount -u` may fail with `EBUSY` if a process has a cwd or open fd inside the mount.
`fusermount3 -uz` (lazy) always succeeds and detaches immediately. **[V]** For a desktop
client, prefer `-uz`.

> **Do not** call `umount(8)` — it needs root for a FUSE mount owned by the user.
> `fusermount3` is the setuid helper that exists for exactly this.

### 1.4 What happens on a crash (`SIGKILL`, OOM, panic) — **[V] all of this was measured**

After `kill -9 <rclone pid>`:

1. The `/proc/mounts` line **stays**:
   ```
   testlocal:/…/src /…/mnt fuse.rclone rw,nosuid,nodev,relatime,user_id=1000,group_id=1000 0 0
   ```
2. Every filesystem call on the mountpoint fails with **`ENOTCONN` (errno 107)**:
   ```
   ls: cannot access '/…/mnt': Transport endpoint is not connected
   stat: cannot read file system information for '/…/mnt': Transport endpoint is not connected
   python: OSError errno 107 Transport endpoint is not connected
   ```
3. systemd marks the unit `failed`, `Result=signal`, `ExecMainStatus=9`.
4. Recovery is exactly one command, and it succeeds (`rc=0`):
   ```console
   fusermount3 -uz /path/to/mount
   ```
   After that `/proc/mounts` is clean and the directory reads as an ordinary empty dir.

**Implication for OneDriveUI:** a stale mount is *indistinguishable from a live mount* if
you only look at `/proc/mounts`. Always probe.

**[V]** The VFS cache itself survives a crash intact: on restart rclone reloads every
`vfsMeta/**.json` and rebuilds `bytesUsed`/`files` from the `Rs` ranges. Files that were
`Dirty:true` (written but not yet uploaded) are re-queued for upload on the next start
with the same flags. **[D]** *"If rclone is quit or dies with files that haven't been
uploaded, these will be uploaded next time rclone is run with the same flags."*

### 1.5 Detecting a live mount — the correct algorithm

```python
import os, errno

FUSE_RCLONE = "fuse.rclone"

def _unescape(s: str) -> str:
    # /proc/mounts octal-escapes space(\040) tab(\011) newline(\012) backslash(\134)
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) and s[i+1:i+4].isdigit():
            out.append(chr(int(s[i+1:i+4], 8))); i += 4
        else:
            out.append(s[i]); i += 1
    return "".join(out)

def rclone_mounts():
    """[(device, mountpoint)] for every fuse.rclone mount, from /proc/mounts."""
    res = []
    with open("/proc/mounts", "rb") as fh:
        for raw in fh:
            parts = raw.decode("utf-8", "replace").split()
            if len(parts) >= 3 and parts[2] == FUSE_RCLONE:
                res.append((_unescape(parts[0]), _unescape(parts[1])))
    return res

def mount_state(mountpoint: str) -> str:
    """'unmounted' | 'stale' | 'live'"""
    if mountpoint not in (m for _, m in rclone_mounts()):
        return "unmounted"
    try:
        os.statvfs(mountpoint)          # cheap; does not hit the network
        return "live"
    except OSError as e:
        if e.errno in (errno.ENOTCONN, errno.ENODEV, errno.EIO):
            return "stale"
        raise
```

**[V] Field 1 of `/proc/mounts` is the rclone *device name*, which defaults to the fs
canonical string.** On this machine the real mount shows:

```
onedrive{MxOuf}: /home/user/OneDrive fuse.rclone rw,nosuid,nodev,relatime,user_id=1000,group_id=1000 0 0
```

The `{MxOuf}` suffix is rclone's hash of the *command-line backend option overrides*
(here `--onedrive-chunk-size 30M`). See §3.2 — this is load-bearing. Set `--devname` to
a stable string (e.g. `--devname OneDrive`) if you want a predictable, pretty device name
in the GNOME Files sidebar and in `df`; it does **not** change the cache directory.

`statvfs()` on a live rclone mount is answered from the VFS's cached `about` data and
does not block on the network. **[V]** Do **not** use `os.path.ismount()` alone — it
returns `True` for a stale mount.

### 1.6 Mounting via the rc API (`mount/mount`)

**[V]** `mount/mount` works and returns the actual mountpoint:

```console
curl -s -X POST http://127.0.0.1:5599/mount/mount -H 'Content-Type: application/json' -d '{
  "fs": "onedrive:",
  "mountPoint": "/home/user/OneDrive",
  "mountType": "mount",
  "vfsOpt":   {"CacheMode": "full", "DirCacheTime": "24h", "CacheMaxSize": "20G"},
  "mountOpt": {"AllowOther": false, "AttrTimeout": "5s", "DeviceName": "OneDrive"}
}'
→ {"mountPoint":"/home/user/OneDrive"}
```

Parameters **[D]**:
- `fs` (required), `mountPoint` (required)
- `mountType` — one of `mount/listmounts`' siblings; **[V]** `mount/types` on this Linux
  build returns `["mount","mount2","nfsmount"]` (no `cmount`).
- `mountOpt` / `vfsOpt` — JSON objects keyed by **Go field names** (see §2.7 table).
- Flat top-level params are also accepted using the CLI name with `-`→`_`, e.g.
  `{"vfs_cache_mode":"full","volname":"MyVol"}`. Nested blocks win over flat. **[D]**

**[V] Two hard gotchas with the rc mount API:**

1. **`mount/listmounts` only lists mounts created via `mount/mount`.**
   A mount started from the CLI — including the one hosting the rc server — reports
   `{"mountPoints": []}`. Never use `mount/listmounts` to answer "is OneDrive mounted?";
   use `/proc/mounts` (§1.5).

2. **A second VFS on the same `fs` becomes unaddressable.** After `mount/mount`-ing the
   same remote twice, `vfs/list` returns disambiguated names:
   ```json
   {"vfses": ["testlocal:/…/src[0]", "testlocal:/…/src[1]"]}
   ```
   but passing either of those back as `fs` fails:
   ```json
   {"error": "no VFS found with name \"testlocal:/…/src[0]\"", "status": 500}
   ```
   and omitting `fs` fails with `more than one VFS active - need "fs" parameter`.
   **Design rule: exactly one VFS per rclone process.** If you need a second mount of the
   same remote, spawn a second rclone process with its own rc port.

`mount/unmount` (`{"mountPoint": "..."}`) returns `{}` and cleanly removes the
`/proc/mounts` entry. **[V]** `mount/unmountall` takes no params. Both only act on
rc-created mounts.

**Recommendation for OneDriveUI:** start the mount from a **systemd user unit** (§6), not
from `mount/mount`. Reasons: the unit gives you restart-on-failure, `ExecStop`, an
`sd_notify` status line, and journal logging; and the CLI mount is the one that hosts the
rc server, so there is exactly one VFS and every `vfs/*` call works without an `fs`
parameter.

---

## 2. VFS cache modes and flags

### 2.1 `--vfs-cache-mode` — exact semantics **[D]**

Internally an int: `off=0, minimal=1, writes=2, full=3` (`vfs/stats` shows the string
`"full"`; the raw options block may show the int). **[V]**

**`off`** (default) — reads and writes stream straight to/from the remote, nothing on disk.
Cannot:
- open a file for read **and** write at once
- seek in a file opened for write
- open an existing file for write without `O_TRUNC`
- retry a failed upload
`O_APPEND`/`O_TRUNC` are ignored; a file opened read with `O_TRUNC` becomes write-only.

**`minimal`** — as `off`, except a file opened read+write is buffered to disk. Still cannot
seek a write-only file, still cannot retry failed uploads.

**`writes`** — reads stream from the remote; **writes** (write-only and read-write) buffer
to disk first. Supports all normal filesystem operations. Failed uploads are retried at
exponentially increasing intervals up to 1 minute.

**`full`** — **all** reads and writes go through disk. Data read from the remote is written
into a **sparse file**; rclone tracks exactly which byte ranges it holds. Supports all
normal filesystem operations. Otherwise identical to `writes`.

### 2.2 Why `full` is the only mode that maps to Files On-Demand

Windows OneDrive's Files On-Demand has three per-file states:

| Windows state | rclone `full` equivalent | Detection |
| --- | --- | --- |
| ☁️ Online-only | no cache item, or `Rs` empty/null | no `vfsMeta` file, or `Rs` covers 0 bytes |
| 🟢 / ✅ Locally available | `Rs == [{Pos:0, Size:Size}]` | full single range |
| 📌 Always keep on this device | full range **+** your own pin list (rclone has no pin concept) | app-level pin set + re-materialise on eviction |
| (no Windows analogue) | **partially** cached | `Rs` has ≥1 range, sum < `Size` |

Only `full` gives you: (a) a per-file, per-byte-range record of what is local, (b) reads
served from disk without touching the network, (c) an LRU/quota evictor
(`--vfs-cache-max-size`, `--vfs-cache-max-age`, `--vfs-cache-min-free-space`).
`writes` caches nothing on read; `minimal`/`off` cache almost nothing at all. There is no
choice here.

**[D] Sparse-file requirement:** *"Not all file systems support sparse files. In particular
FAT/exFAT do not. Rclone will perform very badly if the cache directory is on a filesystem
which doesn't support sparse files."* ext4, btrfs, xfs, f2fs, tmpfs all support them —
verify with `stat -c '%b %s'` (blocks*512 ≪ size ⇒ sparse works). **[V]** tmpfs
(`f_type 0x01021994`) honours sparseness and `SEEK_DATA`/`SEEK_HOLE` correctly.

### 2.3 Cache sizing / lifetime flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--cache-dir DIR` | `~/.cache/rclone` | root of `vfs/` and `vfsMeta/` |
| `--vfs-cache-mode off\|minimal\|writes\|full` | `off` | see §2.1 |
| `--vfs-cache-max-size SizeSuffix` | `off` (unlimited) | total size of cached objects |
| `--vfs-cache-max-age Duration` | `1h0m0s` | max time since **last access** (ATime) |
| `--vfs-cache-min-free-space SizeSuffix` | `off` | target min free space on the cache filesystem |
| `--vfs-cache-poll-interval Duration` | `1m0s` | how often the evictor runs |

**[D] The quota is soft, for two reasons:** *"Firstly because it is only checked every
`--vfs-cache-poll-interval`. Secondly because open files cannot be evicted from the cache."*

**[V] Eviction is measured and behaves like this.** With `--vfs-cache-max-size 4M
--vfs-cache-poll-interval 5s`, reading a 3 MB then a 5 MB file left the cache **completely
empty** within ~7 s — the evictor removes whole items LRU-by-ATime until under quota, and
since a single 5 MB item already exceeds 4 MB, everything went. Log lines to grep for:

```
INFO  : big1.bin: vfs cache: removed cache file as Removing old cache file not in use
DEBUG : big1.bin: vfs cache: removed metadata from cache as Removing old cache file not in use
```

**Eviction granularity is the whole file, never a byte range.** There is no partial evict.

**[V]** `--vfs-cache-max-age` is measured against the `ATime` field in the sidecar JSON,
which rclone updates on every open — *not* the filesystem atime (which is `relatime` and
unreliable).

### 2.4 Write-back

| Flag | Default | Meaning |
| --- | --- | --- |
| `--vfs-write-back Duration` | `5s` | delay after last use before uploading |
| `--vfs-write-wait Duration` | `1s` | wait for an in-sequence write before erroring |
| `--vfs-handle-caching Duration` | `5s` | keep file handles + downloaders alive after last close |

**[V] Measured write lifecycle** (2 MB file written into the mount, `--vfs-write-back 5s`):

*t=0, immediately after `close()`* — sidecar exists, marked dirty, no fingerprint yet:
```json
{"ModTime":"…","ATime":"…","Size":2000000,
 "Rs":[{"Pos":0,"Size":2000000}],"Fingerprint":"","Dirty":true}
```
`vfs/queue` shows it counting down:
```json
{"queue":[{"name":"newfile.bin","id":1,"size":2000000,
           "expiry":4.99659764,"tries":0,"delay":5,"uploading":false}]}
```
`vfs/stats.diskCache.uploadsQueued == 1`.

*t≈10s* — uploaded; `vfs/queue` is `{"queue":[]}`, and the sidecar flips:
```json
{"…":"…","Fingerprint":"2000000,2026-08-31 03:29:32.729272255 +0000 UTC,036abd4912918e587f27bc9be749fc81","Dirty":false}
```

So: **`Dirty:true` ⇔ pending local change; `Dirty:false` + non-empty `Fingerprint` ⇔ in sync
with the remote.** That is your "sync pending" ⟷ "synced" badge.

`vfs/queue-set-expiry` can force an immediate upload (**[D]** *"set it to a large negative
number (eg -1000000000)"*) — that is your "Sync now" button:

```console
# 1. read the id
curl -sX POST :5572/vfs/queue -d '{}'
# 2. push it to the front
curl -sX POST :5572/vfs/queue-set-expiry -d '{"id":1,"expiry":-1000000000}'
```
Returns `{}` on success; errors if `--vfs-cache-mode off` or the id is unknown. Setting the
expiry of an item that has already started uploading has no effect. **[D]**

### 2.5 Read-ahead and chunked reading

| Flag | Default | Meaning |
| --- | --- | --- |
| `--vfs-read-ahead SizeSuffix` | `0` | extra read-ahead **over** `--buffer-size`; **only in cache-mode full** |
| `--vfs-read-chunk-size SizeSuffix` | `128Mi` | initial chunk size for reads from the remote |
| `--vfs-read-chunk-size-limit SizeSuffix` | `off` | if > chunk-size, double each chunk up to this cap |
| `--vfs-read-chunk-streams int` | `0` | number of parallel fixed-size chunk streams |
| `--vfs-read-wait Duration` | `20ms` | wait for an in-sequence read before seeking |
| `--buffer-size SizeSuffix` | `16Mi` (global) | in-memory read-ahead per open file |
| `--max-read-ahead SizeSuffix` | `128Ki` | kernel-side FUSE prefetch for sequential reads |

**[D] Chunk doubling (`--vfs-read-chunk-streams 0`, the default):** rclone starts with
`--vfs-read-chunk-size` and doubles for each read. With `--vfs-read-chunk-size 100M` and no
limit: 0–100M, 100M–200M, 200M–300M… With `--vfs-read-chunk-size-limit 500M`: 0–100M,
100M–300M, 300M–700M, 700M–1200M…

**[D] Parallel streams (`--vfs-read-chunk-streams > 0`):** rclone reads N chunks of
constant `--vfs-read-chunk-size` concurrently. For high-throughput stores a reasonable
start is `--vfs-read-chunk-streams 16 --vfs-read-chunk-size 4M`. **OneDrive/Graph is not
such a store** — it rate-limits aggressively; leave streams at `0`.

**[V]** Cache range bookkeeping is visible at `-vv`; this is the single most useful debug line:
```
DEBUG : vfs cache: looking for range={Pos:2228224 Size:131072} in [{Pos:0 Size:3141632}] - present true
DEBUG : vfs cache: looking for range={Pos:3276800 Size:131072} in [{Pos:0 Size:3141632}] - present false
```
`present false` ⇒ a network fetch is about to happen. This is exactly the in-memory `Rs`.

### 2.6 Other VFS flags you must set or knowingly not set

**`--vfs-fast-fingerprint`** — use a cheaper fingerprint for change detection.
The fingerprint is `size,modtime[,hash]`. Fast mode drops the hash **only for backends
whose hash is slow** (`SlowHash: true`). **[V] OneDrive has `SlowHash: False`, so
`--vfs-fast-fingerprint` does NOT drop the quickxor hash** — the real cache on this machine
was written by a mount using `--vfs-fast-fingerprint` and its fingerprints still carry
40 hex chars of quickxor:
```
"Fingerprint": "25546,2026-06-12 17:50:37 +0000 UTC,f70415f1811f4fa89a4ad5d87e3fcfdb244b5f34"
```
Keep it on anyway: it also skips slow modtime lookups (`SlowModTime`).

**`--vfs-refresh`** — recursively walk and warm the directory cache in the background at
startup. **[V]** logs `DEBUG : … : Refreshing VFS directory cache`. **Recommended for
OneDriveUI**: the file browser feels instant after ~a minute, and it makes
`--dir-cache-time 24h` actually pay off. It costs one full recursive listing at start;
OneDrive has `ListR: False` **[V]**, so this is one Graph request per directory. For a
1373-file / 120-directory account (this machine's real numbers **[V]**) that is ~120
requests — acceptable at login, run it once.

**`--vfs-disk-space-total-size SizeSuffix`** (default `off`) — override the total size the
mount reports. **[V]** With `--vfs-disk-space-total-size 1T`, `df` reported:
```
Size 1,0T  Used 1009G  Avail 16G  Use% 99%
```
i.e. **Total is overridden, Free still comes from the backend's `about`, and Used is
derived as Total−Free.** That combination is dangerous — set this only if the backend's
`about` is broken. **[V] OneDrive has `About: True`**, so leave this `off` and let the
real quota show, which is what the Windows client does.

**`--vfs-links`** (default `false`) — translate `.rclonelink` objects on the remote into
real symlinks in the mount, and vice-versa. **[D]** *"a file which appears as a symlink
`link-to-file.txt` would be stored on cloud storage as `link-to-file.txt.rclonelink`"*.
**[V]** logs `NOTICE: … : Symlinks support enabled`. **[V] Caveat:** `--vfs-links` governs
the *VFS* side only. With a `local` source backend you additionally need `-l/--links` on
the backend or you get `NOTICE: link-to-big1: Can't follow symlink without -L/--copy-links`
and the symlink is simply omitted from the listing. For a cloud remote like OneDrive,
`--vfs-links` alone is what you want. **Recommendation: leave OFF** — the Windows OneDrive
client has no symlink concept, and enabling it means any file literally named `*.rclonelink`
in the user's OneDrive silently becomes a broken symlink.

**`--vfs-metadata-extension EXT`** (default empty) — expose per-file metadata as a JSON
blob at `<name><EXT>`. **[V]** With `--vfs-metadata-extension .metadata.json`:
```console
$ ls /mnt                      # sidecars are NOT listed
big1.bin  big2.bin  dir1  newfile.bin  pollme.txt  small.txt
$ cat /mnt/small.txt.metadata.json     # but ARE readable by exact name
{
	"atime": "2026-08-30T23:24:34.131323516-04:00",
	"btime": "2026-08-30T23:24:34.131323516-04:00",
	"gid": "1000",
	"mode": "100644",
	"mtime": "2026-08-30T23:24:34.131323516-04:00",
	"uid": "1000"
}
```
The keys are backend-dependent; **[V] OneDrive has `ReadMetadata: True`,
`ReadDirMetadata: True`, `WriteMetadata: True`, `UserMetadata: False`**, so you get
system metadata (btime, mtime, permissions/`description`) but not arbitrary user keys.
Hidden-from-listing is a nice property (it will not pollute the file browser) but every
sidecar you read becomes a **cache item in its own right** — **[V]** the evictor logged
`small.txt.metadata.json: vfs cache: removed cache file`. Useful if you want the OneDrive
"Date created" column; otherwise leave off.

**`--vfs-used-is-size`** — **[D]** compute Used by scanning the whole remote like
`rclone size`. Expensive; OneDrive's `about` already reports used bytes. Leave off.

**`--vfs-case-insensitive`** / **`--vfs-block-norm-dupes`** — **[V] OneDrive is
`CaseInsensitive: True`.** The backend already handles this; do not set
`--vfs-case-insensitive` (it adds a fallback lookup per miss). `--vfs-block-norm-dupes`
costs performance and only matters if the account contains names that collide after
Unicode normalisation.

**`--no-modtime`** — skips reading/writing modtimes. Speeds up listings but breaks the
fingerprint and the "Date modified" column. **Do not set.**

### 2.7 `vfsOpt` / `mountOpt` field names for `mount/mount` and `options/set`

**[V]** Dumped live from `options/info`. Left column is the JSON key; right is the CLI flag.

| `vfsOpt` field | CLI flag | Type | Default |
| --- | --- | --- | --- |
| `NoModTime` | `--no-modtime` | bool | false |
| `NoChecksum` | `--no-checksum` | bool | false |
| `NoSeek` | `--no-seek` | bool | false |
| `DirCacheTime` | `--dir-cache-time` | Duration | 5m0s |
| `Refresh` | `--vfs-refresh` | bool | false |
| `PollInterval` | `--poll-interval` | Duration | 1m0s |
| `ReadOnly` | `--read-only` | bool | false |
| `Links` | `--vfs-links` | bool | false |
| `CacheMode` | `--vfs-cache-mode` | CacheMode | off |
| `CachePollInterval` | `--vfs-cache-poll-interval` | Duration | 1m0s |
| `CacheMaxAge` | `--vfs-cache-max-age` | Duration | 1h0m0s |
| `CacheMaxSize` | `--vfs-cache-max-size` | SizeSuffix | off |
| `CacheMinFreeSpace` | `--vfs-cache-min-free-space` | SizeSuffix | off |
| `ChunkSize` | `--vfs-read-chunk-size` | SizeSuffix | 128Mi |
| `ChunkSizeLimit` | `--vfs-read-chunk-size-limit` | SizeSuffix | off |
| `ChunkStreams` | `--vfs-read-chunk-streams` | int | 0 |
| `DirPerms` | `--dir-perms` | FileMode | 777 |
| `FilePerms` | `--file-perms` | FileMode | 666 |
| `LinkPerms` | `--link-perms` | FileMode | 666 |
| `CaseInsensitive` | `--vfs-case-insensitive` | bool | false |
| `BlockNormDupes` | `--vfs-block-norm-dupes` | bool | false |
| `WriteWait` | `--vfs-write-wait` | Duration | 1s |
| `ReadWait` | `--vfs-read-wait` | Duration | 20ms |
| `WriteBack` | `--vfs-write-back` | Duration | 5s |
| `ReadAhead` | `--vfs-read-ahead` | SizeSuffix | 0 |
| `UsedIsSize` | `--vfs-used-is-size` | bool | false |
| `FastFingerprint` | `--vfs-fast-fingerprint` | bool | false |
| `DiskSpaceTotalSize` | `--vfs-disk-space-total-size` | SizeSuffix | off |
| `Umask` | `--umask` | FileMode | 022 |
| `UID` | `--uid` | uint32 | 1000 |
| `GID` | `--gid` | uint32 | 1000 |
| `HandleCaching` | `--vfs-handle-caching` | Duration | 5s |
| `MetadataExtension` | `--vfs-metadata-extension` | string | "" |

| `mountOpt` field | CLI flag | Type | Default |
| --- | --- | --- | --- |
| `DebugFUSE` | `--debug-fuse` | bool | false |
| `AttrTimeout` | `--attr-timeout` | Duration | 1s |
| `ExtraOptions` | `-o/--option` | stringArray | [] |
| `ExtraFlags` | `--fuse-flag` | stringArray | [] |
| `Daemon` | `--daemon` | bool | false |
| `DaemonTimeout` | `--daemon-timeout` | Duration | 0s |
| `DaemonWait` | `--daemon-wait` | Duration | 1m0s |
| `DefaultPermissions` | `--default-permissions` | bool | false |
| `AllowNonEmpty` | `--allow-non-empty` | bool | false |
| `AllowRoot` | `--allow-root` | bool | false |
| `AllowOther` | `--allow-other` | bool | false |
| `AllowIDMap` | `--allow-idmap` | bool | false |
| `AsyncRead` | `--async-read` | bool | **true** |
| `MaxReadAhead` | `--max-read-ahead` | SizeSuffix | 128Ki |
| `WritebackCache` | `--write-back-cache` | bool | false |
| `DeviceName` | `--devname` | string | "" |
| `CaseInsensitive` | `--mount-case-insensitive` | Tristate | unset |
| `DirectIO` | `--direct-io` | bool | false |
| `VolumeName` | `--volname` | string | "" (Win/macOS only) |
| `NoAppleDouble` | `--noappledouble` | bool | true (macOS) |
| `NoAppleXattr` | `--noapplexattr` | bool | false (macOS) |
| `NetworkMode` | `--network-mode` | bool | false (Windows only) |

> **[V] `options/set` does NOT affect an already-running VFS.** Setting
> `{"vfs":{"CacheMaxAge":"1s"}}` returned `{}` and `options/get` reflected the change, but
> `vfs/stats.opt.CacheMaxAge` stayed at `3600000000000` and no eviction happened after
> 25 s. `options/set` mutates the *global template* used for future VFS creation only.
> **To change a running mount's VFS options you must restart the mount.**
> (`vfs/poll-interval` is the one exception — it does update the live poller.)
>
> Also note **`options/get` returns pre-umask defaults** (`DirPerms:511`=0777,
> `FilePerms:438`=0666) while **`vfs/stats.opt` returns the effective, umask-applied
> values** (`DirPerms:2147484141` = `os.ModeDir|0755`, `FilePerms:420`=0644). Always read
> `vfs/stats` for what is actually in force.

---

## 3. THE CRITICAL QUESTION — is this file locally available?

### 3.1 On-disk cache layout — **[V] verified on both the test remote and the real OneDrive cache**

```
<cache-dir>/                       # --cache-dir, default ~/.cache/rclone
├── vfs/<fsname>/<remote/path>     # the DATA, a sparse file, mode 0600
└── vfsMeta/<fsname>/<remote/path> # the SIDECAR, a JSON file, mode 0644
```

The two trees mirror each other **exactly** — same relative path, same filename, **no added
extension**. Directories are mode `0700`.

Real observed layout on this machine:

```
/home/user/.cache/rclone/
├── vfs/
│   ├── local/tmp/…
│   ├── onedrive/                    ← from a mount with NO backend overrides
│   └── onedrive{MxOuf}/             ← from the current mount (--onedrive-chunk-size 30M)
│       ├── firma.png
│       ├── .Trash-1000/directorysizes
│       ├── Imágenes/MicrosoftTeams-image.png
│       └── Escritorio/Latam importantes/maturity model.png
└── vfsMeta/
    └── onedrive{MxOuf}/…            ← identical tree
```

**`<fsname>` derivation:** it is the fs canonical string (`onedrive:`, `onedrive{MxOuf}:`,
`testlocal:/tmp/x/src`) with the `:` turned into a path separator:

| fs string | cache subdirectory |
| --- | --- |
| `onedrive:` | `vfs/onedrive` |
| `onedrive{MxOuf}:` | `vfs/onedrive{MxOuf}` |
| `onedrive:Documents` | `vfs/onedrive/Documents` |
| `testlocal:/tmp/x/src` | `vfs/testlocal/tmp/x/src` |

> **DO NOT derive this path yourself.** Read it from `vfs/stats`:
> `diskCache.path` and `diskCache.pathMeta` are the authoritative absolute paths and
> already account for the `{HASH}` suffix, the sub-path, and `--cache-dir`.
> This is the single most important API call in the whole document.

**Filename encoding.** The cache trees are served by an internal local backend created as
`:local,encoding='Slash,Dot',links=false:` (**[V]**, seen in the debug log). Only `/` (which
cannot appear in a POSIX filename anyway) and the special names `.`/`..` are encoded.
**[V]** A source file named ``weird:name?with*chars.txt`` appears in the cache verbatim as
``weird:name?with*chars.txt`` — colons, question marks, asterisks and non-ASCII
(`Imágenes/`) are all preserved byte-for-byte. So the mapping
`mount-relative path → cache path` is a plain `os.path.join`.

**Cache items only exist for files that have been opened.** **[V]** Before any read, both
trees contained only empty directories. A file never touched has *no* entry in either tree —
that is "online-only".

### 3.2 THE `{HASH}` FOOTGUN — read this twice

**[V]** This machine has **two** parallel OneDrive caches, `vfs/onedrive/` (1202-byte dir
entry, last written Aug 21) and `vfs/onedrive{MxOuf}/` (316 bytes, Aug 25), because the
mount command line changed. rclone appends `{base64hash}` to the remote name whenever any
**backend option is overridden on the command line** (`--onedrive-chunk-size 30M`,
`--onedrive-*` anything, a connection string, `--drive-*`, …). Changing, adding or removing
any such flag ⇒ new `<fsname>` ⇒ **new, empty cache directory; every previously
materialised file instantly becomes "online-only" and the old tree is orphaned on disk
forever.**

**Mitigations for OneDriveUI, in order of preference:**

1. **Put every backend option in `rclone.conf`, not on the command line.**
   `chunk_size = 30M` under `[onedrive]` produces no hash suffix.
2. Freeze the mount command line and never edit it without a cache migration.
3. Always read `diskCache.path` from `vfs/stats` rather than assuming; and on startup,
   if `~/.cache/rclone/vfs/` contains more than one `onedrive*` directory, offer the user a
   "reclaim orphaned cache" action (`du -sh` each, delete the ones not equal to
   `diskCache.path`).

### 3.3 The sidecar JSON — exact shape **[V]**

```json
{
	"ModTime": "2026-08-30T23:26:05.861069681-04:00",
	"ATime":   "2026-08-30T23:30:05.535426864-04:00",
	"Size":    5000000,
	"Rs": [
		{ "Pos": 0,       "Size": 520192 },
		{ "Pos": 4096000, "Size": 126976 }
	],
	"Fingerprint": "5000000,2026-08-31 03:24:34.131323516 +0000 UTC,3a96446a13959c1d5634f71a66e131e4",
	"Dirty": false
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `ModTime` | RFC3339Nano string | modtime rclone set on the **cache file** |
| `ATime` | RFC3339Nano string | last access, **as tracked by rclone**; drives `--vfs-cache-max-age` LRU |
| `Size` | int | full size of the object on the remote |
| `Rs` | `[{Pos,Size}]` \| `null` | **the byte ranges present in the sparse cache file**, sorted, non-overlapping, coalesced |
| `Fingerprint` | string | `"<size>,<modtime UTC>,<hash>"`; `""` while the item is dirty/never-uploaded |
| `Dirty` | bool | `true` = local changes not yet uploaded |

**Encoding:** UTF-8, tab-indented, trailing newline. `Rs` is `null` (JSON null, not `[]`)
when nothing is cached. Sizes are plain ints (bytes).

**Fingerprint hash algorithm is backend-dependent.** **[V]** local ⇒ MD5 (32 hex);
**OneDrive ⇒ quickXorHash, 40 hex chars** (`Hashes: ['quickxor']`, `hashType: 4096` in
`vfs/stats`; local's `hashType` is `1` = MD5).

### 3.4 (a) Listing which files are cached — the reference implementation

Walk `vfsMeta`, not `vfs`: the sidecar is what rclone actually consults, so it is the
source of truth for what rclone will serve without a network round trip.

```python
"""Enumerate the VFS cache: what is local, how much of it, and is it dirty."""
import json, os, urllib.request
from dataclasses import dataclass

RC = "http://127.0.0.1:5572"

def rc(path, payload=None):
    body = json.dumps(payload or {}).encode()
    req  = urllib.request.Request(f"{RC}/{path}", body,
                                  {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

@dataclass
class CacheEntry:
    remote: str          # path relative to the mount root, e.g. "Imágenes/x.png"
    size: int            # full size on the remote
    cached: int          # bytes present locally
    ranges: list         # [(pos, size), ...]
    dirty: bool
    atime: str
    fingerprint: str

    @property
    def state(self):
        if self.dirty:                  return "pending-upload"   # ↻ badge
        if self.size == 0:              return "local"            # empty file
        if self.cached >= self.size:    return "local"            # ✅ green tick
        if self.cached > 0:             return "partial"          # ◑
        return "online-only"                                      # ☁️

def scan_cache():
    st        = rc("vfs/stats")["diskCache"]
    meta_root = st["pathMeta"]          # ← AUTHORITATIVE. never hand-derive.
    out = {}
    for dirpath, _dirs, files in os.walk(meta_root):
        for name in files:
            mp  = os.path.join(dirpath, name)
            rel = os.path.relpath(mp, meta_root)
            try:
                with open(mp, "rb") as fh:
                    m = json.load(fh)
            except (OSError, ValueError):
                continue                # torn write / concurrent evict: skip
            rs = m.get("Rs") or []
            out[rel] = CacheEntry(
                remote=rel, size=m["Size"],
                cached=sum(r["Size"] for r in rs),
                ranges=[(r["Pos"], r["Size"]) for r in rs],
                dirty=m.get("Dirty", False),
                atime=m.get("ATime", ""),
                fingerprint=m.get("Fingerprint", ""),
            )
    return out

def state_of(remote_path):
    """State for ONE file, without walking the whole tree."""
    st = rc("vfs/stats")["diskCache"]
    mp = os.path.join(st["pathMeta"], remote_path)
    if not os.path.exists(mp):
        return "online-only"
    with open(mp, "rb") as fh:
        m = json.load(fh)
    rs = m.get("Rs") or []
    got = sum(r["Size"] for r in rs)
    if m.get("Dirty"):             return "pending-upload"
    if got >= m["Size"]:           return "local"
    return "partial" if got else "online-only"
```

**Verified against the real OneDrive cache** — this exact code produced:

```
FULL      25546/25546      alloc=28672      dirty=False firma.png
FULL    1386943/1386943    alloc=1388544    dirty=False Paradox_cosmic_neon_….png
FULL     146220/146220     alloc=147456     dirty=False Imágenes/MicrosoftTeams-image.png
FULL    5451760/5451760    alloc=5451776    dirty=False Imágenes/Gemini_Generated_Image_….png
FULL      13595/13595      alloc=16384      dirty=False Escritorio/Latam importantes/instructions-chatbot-ardoq.md
```

**Freshness caveat — important for a live UI. [V]** The sidecar is written to disk when the
item is released, not on every read. Measured lag: immediately after a `dd` read, `Rs` was
still `null`; ~10 s later (after `--vfs-handle-caching 5s` released the handle and the
next cache poll ran) it read `[{Pos:0,Size:520192}]`. So:

- For **badge painting** (a list of 1000 files), the sidecar walk is perfect — a few ms,
  no rclone involvement, and 10 s of staleness is invisible.
- For **"did my pin finish?"**, poll the sparse file with `SEEK_DATA` (§3.4b) — that is
  updated synchronously as bytes land.
- Watch the tree with `inotify` (`IN_CLOSE_WRITE|IN_DELETE|IN_MOVED_TO`) on `vfsMeta` to
  drive badge invalidation instead of polling. Note `vfsMeta` mirrors the whole tree, so
  use a recursive watch and add watches for new directories.

### 3.5 (b) How much of each file is cached — sparse files

Two independent measurements, **[V] byte-identical in every test**:

| Method | Source | Freshness |
| --- | --- | --- |
| `sum(r["Size"] for r in Rs)` | sidecar JSON | lags up to ~10 s |
| `SEEK_DATA`/`SEEK_HOLE` extents on the data file | kernel | synchronous |
| `st_blocks * 512` | kernel | synchronous, but rounds **up** to the block size |

Measured on a 5 000 000-byte file after a 4 KiB head read and a 4 KiB read at offset
4 096 000:

```
Rs       = [{Pos:0, Size:520192}, {Pos:4096000, Size:126976}]   # sum = 647168
extents  = [(0, 520192), (4096000, 126976)]                     # sum = 647168  ← identical
st_blocks*512 = 647168
apparent size = 5000000
```

Never use `du`/`st_size` alone: the cache file is **preallocated to the full remote size**
the moment it is first opened, so `ls -l` always shows the full size even for a file with
zero bytes cached. **[V]** `du -B1 --apparent-size` = 5000000 while `du -B1` = 28672.

```python
import os

def cached_extents(data_path):
    """[(pos, length)] of the byte ranges physically present. Kernel truth."""
    fd = os.open(data_path, os.O_RDONLY)
    try:
        size, out, pos = os.fstat(fd).st_size, [], 0
        while pos < size:
            try:
                d = os.lseek(fd, pos, os.SEEK_DATA)
            except OSError:
                break                       # only holes remain
            try:
                h = os.lseek(fd, d, os.SEEK_HOLE)
            except OSError:
                h = size
            out.append((d, h - d))
            pos = h
        return out
    finally:
        os.close(fd)

def cached_bytes_fast(data_path):
    """Cheapest possible progress read; rounds up to the fs block size."""
    st = os.stat(data_path)
    return min(st.st_blocks * 512, st.st_size)
```

> `SEEK_DATA`/`SEEK_HOLE` require Linux ≥ 3.1 and a filesystem that implements them
> (ext4, btrfs, xfs, f2fs, tmpfs all do). If `lseek` raises `EINVAL`, the filesystem does
> not support them — fall back to the sidecar `Rs`.

**One consistency caveat. [V]** The sidecar and the sparse file can disagree after an
abnormal event. After I deleted a cache file out from under a running mount and let rclone
re-download it, then killed the process, the restarted mount showed `Rs: null` for a file
whose sparse image on disk held all 3 000 000 bytes — and `vfs/stats.bytesUsed` excluded
those 3 MB. **`Rs` wins**: rclone will re-download regardless of what is physically on disk.
So render badges from `Rs`, and treat `st_blocks` only as a live progress bar.

### 3.6 (c) Pin a file / "Always keep on this device"

**There is no rc endpoint that pins or pre-fetches.** The only way to materialise a file is
to read it through the mount. **[V]** Reading a partially-cached 5 MB file end-to-end took
`Rs` from `[{0,520192},{4096000,126976}]` to `[{Pos:0,Size:5000000}]`.

```python
import os

CHUNK = 4 * 1024 * 1024

def pin(mount_path, progress=None):
    """Force a full download. Run on a worker thread — this blocks on the network."""
    total = os.path.getsize(mount_path)
    done  = 0
    # O_RDONLY, sequential. posix_fadvise(WILLNEED) does NOT work through FUSE.
    with open(mount_path, "rb", buffering=0) as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            done += len(b)
            if progress:
                progress(done, total)
    return done
```

Notes:
- Sequential reads keep rclone on the fast path; `--vfs-read-wait 20ms` exists precisely to
  absorb small out-of-order reads before rclone gives up and reopens at a new offset.
- Do **not** use `sendfile`/`copy_file_range` to `/dev/null` — behaviour through FUSE
  varies; a plain read loop is predictable.
- Run at most `--transfers`-worth of pins concurrently. For OneDrive, 2–4.
- **Pinning is not sticky.** rclone's evictor will happily reclaim a "pinned" file once it
  is the LRU victim (**[V]** confirmed: quota eviction removes any item not currently open).
  OneDriveUI must keep its **own** pin set (a sqlite table or a JSON file next to the app
  config) and re-pin on an eviction event. Detect evictions by watching `vfsMeta` with
  inotify for `IN_DELETE`, or by grepping the journal for
  `vfs cache: removed cache file as Removing old cache file not in use`.
- If a large fraction of the account is pinned, consider not fighting the evictor: raise
  `--vfs-cache-max-size` above the pinned total, or use `--vfs-cache-min-free-space`
  instead of a hard size cap.

### 3.7 (d) Evict / "Free up space"

Neither `vfs/forget` nor `options/set` will do it:

- **[V] `vfs/forget file=big1.bin` returns `{"forgotten":["big1.bin"]}` but does NOT touch
  the disk cache.** It only drops the entry from the in-memory *directory* cache, forcing a
  re-`stat` from the remote. Cache files and `bytesUsed` were unchanged afterwards.
- **[V] `options/set {"vfs":{"CacheMaxAge":"1s"}}` returns `{}` but does not affect the
  running VFS** (§2.7). Useless for eviction.

**The supported mechanism is to unlink both files.** **[V] rclone detects this and recovers
correctly**: it logs

```
ERROR : big1.bin: vfs cache: detected external removal of cache file
```

recreates the sparse file on the next open, re-downloads, and the md5 of the re-read file
matched the source exactly.

```python
import os, contextlib

def evict(remote_path, rc_stats=None):
    """Free up space for one file. Returns bytes reclaimed (approx)."""
    st = rc_stats or rc("vfs/stats")["diskCache"]
    data = os.path.join(st["path"],     remote_path)
    meta = os.path.join(st["pathMeta"], remote_path)

    # 1. Refuse if the local copy is the only copy.
    try:
        with open(meta, "rb") as fh:
            import json; m = json.load(fh)
        if m.get("Dirty"):
            raise RuntimeError("refusing to evict: not yet uploaded")
    except FileNotFoundError:
        pass

    freed = 0
    with contextlib.suppress(OSError):
        freed = os.stat(data).st_blocks * 512
    # 2. Meta FIRST: if we die between the two unlinks, rclone sees a data file with
    #    no metadata and treats the item as uncached — safe. The reverse leaves
    #    metadata claiming ranges that no longer exist.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(meta)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(data)
    return freed
```

**Rules for a safe evict:**

1. **Never evict a `Dirty:true` item** — that is an un-uploaded local change and deleting it
   is data loss. Check the sidecar first, and cross-check `vfs/queue` for the name.
2. **Prefer not to evict an open file.** rclone tolerates it (it recreates the file), but any
   process mid-read will see the remaining bytes re-fetched from the network. There is no
   API to ask "is this item open"; `vfs/stats.diskCache.uploadsInProgress` only covers
   uploads. In practice: check `lsof`-style via `/proc/*/fd` on the *mount* path, or just
   accept the transparent re-fetch.
3. **Unlink meta before data** (see comment above).
4. Prune the now-empty directories in both trees afterwards if you want the tree tidy;
   rclone recreates them on demand.
5. Do not `truncate()` the data file to 0 as an alternative — the sidecar would still claim
   the ranges and rclone would serve zeros.

**Bulk "Free up space" (whole folder / whole drive):** simply `rm -rf` the corresponding
subtree in **both** `diskCache.path` and `diskCache.pathMeta`, after verifying nothing under
it is `Dirty` and `vfs/queue` is empty. rclone will log one `detected external removal` per
in-memory item and carry on. Alternatively, stop the mount, delete the trees, start the
mount — cleanest, and what a "Reset cache" menu item should do.

### 3.8 `vfs/stats` — the aggregate view **[V]**

```console
curl -sX POST http://127.0.0.1:5572/vfs/stats -H 'Content-Type: application/json' -d '{}'
```

Real response from this machine's live OneDrive mount:

```json
{
  "fs": "onedrive{MxOuf}:",
  "inUse": 1,
  "diskCache": {
    "bytesUsed": 178709025,
    "erroredFiles": 0,
    "files": 22,
    "hashType": 4096,
    "outOfSpace": false,
    "path":     "/home/user/.cache/rclone/vfs/onedrive{MxOuf}",
    "pathMeta": "/home/user/.cache/rclone/vfsMeta/onedrive{MxOuf}",
    "uploadsInProgress": 0,
    "uploadsQueued": 0
  },
  "metadataCache": { "dirs": 120, "files": 1373 },
  "opt": { "...": "the EFFECTIVE, umask-applied VFS options" }
}
```

| Field | Use in OneDriveUI |
| --- | --- |
| `diskCache.path` / `pathMeta` | **always** derive cache file paths from these |
| `diskCache.bytesUsed` | "OneDrive is using N GB on this PC" (sum of `Rs`, so it *under*-reports if a sidecar is stale) |
| `diskCache.files` | number of cache items |
| `diskCache.uploadsQueued` / `uploadsInProgress` | drive the tray "Syncing…" state |
| `diskCache.erroredFiles` | drive the tray "Attention needed" state |
| `diskCache.outOfSpace` | show the "Your disk is full" banner |
| `diskCache.hashType` | `1`=MD5 (local), `4096`=quickxor (OneDrive) |
| `metadataCache.dirs`/`files` | how much of the tree the dir-cache is holding; grows after `--vfs-refresh`, resets when `--dir-cache-time` expires **[V]** |
| `inUse` | refcount of the VFS |
| `opt` | effective options; the only reliable place to read them back |

`diskCache` is **only present when `--vfs-cache-mode > off`**. `hashType` is an int in the
JSON response but the debug log renders it as a name (`hashType:md5`).

---

## 4. Change propagation: `--poll-interval`, ChangeNotify, `--dir-cache-time`

### 4.1 The two mechanisms

**`--dir-cache-time Duration`** (default `5m0s`) — how long a directory listing is trusted.
When it expires, the next `readdir` re-lists from the remote. This always works, on every
backend.

**`--poll-interval Duration`** (default `1m0s`) — how often rclone asks the backend
"what changed?" via the backend's `ChangeNotify` implementation. **[D]** *"must be smaller
than dir-cache-time and only on supported remotes (set 0 to disable)"*. When supported,
remote changes appear within roughly one poll interval regardless of `--dir-cache-time`.

### 4.2 Does OneDrive support it? — **YES [V]**

Two independent confirmations on this machine:

```console
$ rclone backend features onedrive:
  "ChangeNotify": true,
  "About": true, "CleanUp": true, "Copy": true, "Move": true, "DirMove": true,
  "Purge": true, "PublicLink": true, "DirCacheFlush": true, "DirSetModTime": true,
  "CaseInsensitive": true, "ReadMetadata": true, "WriteMetadata": true,
  "ReadDirMetadata": true, "Shutdown": true,
  "ListR": false, "OpenWriterAt": false, "OpenChunkWriter": false,
  "PutStream": false, "PartialUploads": false, "SlowHash": false, "SlowModTime": false,
  "Hashes": ["quickxor"], "Precision": 1000000000     ← 1 second modtime precision
```

```console
$ curl -sX POST http://127.0.0.1:5572/vfs/poll-interval -d '{}'
{ "enabled": true,
  "interval": { "raw": 60000000000, "seconds": 60, "string": "1m0s" },
  "supported": true }
```

rclone's OneDrive `ChangeNotify` is built on the Microsoft Graph **delta** API
(`/me/drive/root/delta`), so it is a cheap incremental query rather than a re-listing.
Poll intervals as low as `10s` are practical; `30s`–`1m` is the sensible default for a
desktop client (the Windows client is push-based via a webhook and so appears instant —
you cannot match that with rclone alone).

**[V] Contrast with a non-supporting backend.** The `local` backend logs at startup:

```
INFO : Local file system at /…/src: poll-interval is not supported by this remote
```

and `vfs/poll-interval` returns
`{"error":"poll-interval is not supported by this remote","status":500}`.
On such a backend, external changes surface only when `--dir-cache-time` expires —
**[V]** confirmed: a file created directly in the source appeared in the mount ~7 s later,
with `--dir-cache-time 10s`.

**Use this as the capability probe:** call `vfs/poll-interval` at startup; a 500 with that
message means the client must fall back to a short `--dir-cache-time` and/or an explicit
refresh button.

### 4.3 Adjusting the poll interval at runtime **[D]**

```console
rclone rc vfs/poll-interval interval=30s          # 0 disables
rclone rc vfs/poll-interval interval=30s timeout=10s
```
The change only takes effect if the current poll function applies it before `timeout`
(`timeout<=0` ⇒ wait indefinitely, the default). **[D]** warns that changes made while
polling is disabled or being reconfigured *"might not get picked up… depending on the used
remote"* — after re-enabling, follow with a `vfs/refresh` to be safe.

**This is the one VFS option that can be changed on a live mount** (§2.7).

### 4.4 Forcing a rescan

| Call | Effect |
| --- | --- |
| `vfs/refresh` | re-read the root directory into the dir cache; `{"result":{"":"OK"}}` **[V]** |
| `vfs/refresh {"dir":"dir1"}` | refresh one directory; `{"result":{"dir1":"OK"}}` **[V]** |
| `vfs/refresh {"dir":"a","dir2":"b","dir3":"c"}` | any key starting with `dir` refreshes that path **[D]** |
| `vfs/refresh {"recursive":"true"}` | whole tree; uses `--fast-list` if enabled **[D]** — but **[V] OneDrive has `ListR: False`**, so this is one request per directory |
| `vfs/forget` | drop everything from the dir cache (lazy re-read on next access) **[D]** |
| `vfs/forget {"file":"a/b.txt","dir":"a"}` | drop specific entries; keys starting with `file`/`dir` **[D]** |
| `kill -SIGHUP $(pidof rclone)` | flush all caches **[D]** — blunt; prefer the rc calls |

`vfs/refresh` is **eager** (it fetches now, so the UI is instantly correct);
`vfs/forget` is **lazy** (it just invalidates, cheaper if the user may never look).

**Recommended OneDriveUI wiring:**
- F5 / "Refresh" button in a folder view ⇒ `vfs/refresh {"dir": "<current folder>"}`.
- Pull-to-refresh at root, or after the app regains focus after >`--dir-cache-time` ⇒
  `vfs/refresh {}`.
- After OneDriveUI itself uploads/deletes something through a non-mount path (e.g.
  `operations/copyfile`) ⇒ `vfs/forget {"dir": "<parent>"}` then
  `vfs/refresh {"dir": "<parent>"}`.
- Never call `vfs/refresh {"recursive":"true"}` from a UI action on OneDrive; reserve it for
  the `--vfs-refresh` startup warm-up.

### 4.5 `--attr-timeout` — the corruption knob **[D]**

*"The kernel can cache the info about a file for the time given by `--attr-timeout`. You may
see corruption if the remote file changes length during this window… The default setting of
'1s' is the lowest setting which mitigates the problems above."* Raising it to `5s`–`1m`
cuts kernel↔rclone round trips noticeably. The existing mount on this machine uses
`--attr-timeout 5s`; that is a reasonable consumer default given that a desktop user rarely
has the same file changing size on two devices simultaneously.

---

## 5. Permissions, ownership and `/etc/fuse.conf`

### 5.1 The flags

| Flag | Default (v1.75.0 CLI) | Notes |
| --- | --- | --- |
| `--uid uint32` | `1000` (the running user) | uid reported for every file |
| `--gid uint32` | `1000` | gid reported for every file |
| `--file-perms FileMode` | `666` | before umask |
| `--dir-perms FileMode` | `777` | before umask |
| `--link-perms FileMode` | `666` | before umask |
| `--umask FileMode` | **`022`** | **[V]** the local binary's help says `022`; rclone.org's rendered page says `002` — **trust the binary** |
| `--allow-other` | off | let other users (incl. root, and GNOME's portals) see the mount |
| `--allow-root` | off | let root only |
| `--allow-non-empty` | off | mount over a non-empty dir |
| `--default-permissions` | off | make the **kernel** enforce access control from the mode bits |
| `--read-only` | off | reject all writes |

**[V] umask is applied *after* `--file-perms`/`--dir-perms`:**
`effective = perms &^ umask`.

| flags | `vfs/stats.opt` | effective |
| --- | --- | --- |
| defaults (`--umask 022`) | `FilePerms:420`, `DirPerms:2147484141` | `0644` file, `0755` dir |
| `--umask 077 --file-perms 0666 --dir-perms 0777` | `FilePerms:384`, `DirPerms:2147484096` | `0600` file, `0700` dir — **[V]** `ls -la` on the mount showed `-rw-------` / `drwx------` |

(`DirPerms` is a Go `os.FileMode`, so the value carries `os.ModeDir` = `0x80000000`
= 2147483648; subtract it to get the permission bits. `LinkPerms` carries
`os.ModeSymlink` = `0x08000000` = 134217728.)

**Recommendation for OneDriveUI:** `--umask 022` (default) so files are `0644`/`0755` —
this matches what GNOME Files, Nautilus thumbnailers and normal desktop apps expect, and it
matches the existing unit on this machine. Do **not** use `--umask 077` unless the user asks
for a private mount; it breaks group-readable sharing and some sandboxed Flatpak apps.

### 5.2 `--allow-other` and `/etc/fuse.conf`

By default a FUSE mount is visible **only to the user who mounted it** — not root, not other
users, not processes in a different user namespace.

**[V] `/etc/fuse.conf` on this machine has `user_allow_other` COMMENTED OUT.** Attempting
`--allow-other` therefore fails, foreground, with exactly:

```
NOTICE: mount helper error: fusermount3: option allow_other only allowed if 'user_allow_other' is set in /etc/fuse.conf
CRITICAL: Fatal error: failed to mount FUSE fs: fusermount: exit status 1
```

To enable it a **root** edit is required:

```console
echo user_allow_other | sudo tee -a /etc/fuse.conf
```

`/etc/fuse.conf` also accepts `mount_max = n` (default 1000), written *exactly* with one
space either side of `=`. **[V]** the file's own comments confirm both.

**Do you need `--allow-other`?** For OneDriveUI, **no, and prefer not to**:

- The mount lives under `$HOME` and is used by the same user's desktop session.
- `--allow-other` bypasses the kernel's owner check for *everyone* on the machine unless you
  also pass `--default-permissions`. If you ever do enable it, pair them:
  `--allow-other --default-permissions --umask 077`.
- It requires a root-privileged config change during install, which a consumer app should
  avoid.

**Exception:** Flatpak/Snap apps run in a mount namespace. They see the host mount through
the portal, not directly, so `--allow-other` does not help them either — a Flatpak app
needs `--filesystem=home` (or the file chooser portal), which works with a plain
user-owned FUSE mount.

**Root cannot read a non-`allow-other` FUSE mount either.** If you ever run a
`systemd --system` service that needs to touch the mount, you need `--allow-root` (or
`--allow-other`) plus `user_allow_other`. Another reason to keep everything in
`systemd --user`.

### 5.3 AppArmor (not applicable here, but document it)

**[D]** On newer Ubuntu, AppArmor blocks `fusermount3` and mounts fail with
`fusermount3: mount failed: Permission denied`; the workaround is
`sudo aa-disable /usr/bin/fusermount3`. **[V]** CachyOS/Arch does not ship that profile —
mounts succeed with no AppArmor intervention. Detect this failure string and surface a
distro-specific hint rather than a generic error.

---

## 6. Making the mount survive: the systemd user unit

### 6.1 The unit — verified working on this machine

There is already an `rclone-onedrive.service` in `~/.config/systemd/user/` on this box, and
**[V]** it is `enabled`, `active (running)`, and has restarted cleanly across three reboots
per the journal. Here it is, with the two flaws it contains corrected:

```ini
# ~/.config/systemd/user/onedriveui-mount.service
[Unit]
Description=OneDrive on-demand mount (rclone)
Documentation=https://rclone.org/commands/rclone_mount/
# NOTE: network-online.target does NOT exist in the systemd --user manager (see 6.3).
# Order against the graphical session instead, and let rclone's own retries handle
# a not-yet-up network.
After=graphical-session-pre.target
PartOf=graphical-session.target
AssertPathIsDirectory=%h/OneDrive

[Service]
Type=notify
Environment=RCLONE_CONFIG=%h/.config/rclone/rclone.conf
ExecStartPre=-/usr/bin/fusermount3 -uz %h/OneDrive
ExecStart=/usr/bin/rclone mount onedrive: %h/OneDrive \
    --config %h/.config/rclone/rclone.conf \
    --devname OneDrive \
    --vfs-cache-mode full \
    --cache-dir %h/.cache/rclone \
    --vfs-cache-max-size 20G \
    --vfs-cache-max-age 168h \
    --vfs-cache-poll-interval 1m \
    --vfs-cache-min-free-space 5G \
    --vfs-fast-fingerprint \
    --vfs-read-ahead 128M \
    --vfs-refresh \
    --dir-cache-time 24h \
    --poll-interval 1m \
    --attr-timeout 5s \
    --buffer-size 32M \
    --transfers 4 \
    --checkers 8 \
    --umask 022 \
    --rc --rc-addr 127.0.0.1:5572 --rc-no-auth \
    --log-level INFO --use-json-log
ExecStop=/usr/bin/fusermount3 -uz %h/OneDrive
Restart=on-failure
RestartSec=10
# Give in-flight uploads time to finish before SIGKILL.
TimeoutStopSec=120
KillMode=mixed

[Install]
WantedBy=default.target
```

```console
systemctl --user daemon-reload
systemctl --user enable --now onedriveui-mount.service
```

### 6.2 `Type=notify` works — **[V]** and gives you free telemetry

rclone implements `sd_notify`. With `Type=notify`:

- systemd reports the unit `active (running)` only **after the mount is actually usable**,
  so anything with `After=` on it can rely on the mount. No `--daemon`, no polling loop.
- **[V]** rclone continuously pushes a **status line** that `systemctl status` and
  `systemctl show -p StatusText` expose:

  ```
  Status: "[23:29] vfs cache: objects 3 (was 3) in use 0, to upload 0, uploading 0, total size 2.525Mi (was 2.525Mi)"
  ```

  ```console
  $ systemctl --user show onedriveui-mount.service -p StatusText
  StatusText=[23:29] vfs cache: objects 3 (was 3) in use 0, to upload 0, uploading 0, total size 2.525Mi (was 2.525Mi)
  ```

  A zero-cost health/telemetry channel if the rc port is ever unavailable. Prefer
  `vfs/stats` for real data, but this is a good fallback and a good thing to log.

`Type=simple` also works but systemd will report `active` immediately, before the mount
exists — every consumer then races. Do not use `Type=forking` + `--daemon` (§1.2).

### 6.3 **[V] Ordering vs the network — the trap in the existing unit**

The existing unit on this machine contains:

```ini
After=network-online.target
Wants=network-online.target
```

but in the **user** manager:

```console
$ systemctl --user show network-online.target -p LoadState -p LoadError
LoadState=not-found
LoadError=org.freedesktop.systemd1.NoSuchUnit "Unit network-online.target not found."
```

`network-online.target` is a **system**-manager unit. A `--user` unit cannot order against
it. The `Wants=` on a not-found unit is tolerated (the service still starts — confirmed in
the journal), so the directives are **silently ignored**: they buy nothing and mislead the
next maintainer. Options, best first:

1. **Do nothing and rely on rclone's retries.** rclone mount starts fine with no network;
   the first listing fails, and once connectivity arrives, `--poll-interval` /
   `--dir-cache-time` recover. This is what the running unit actually does today, correctly.
   Combine with `Restart=on-failure` + `RestartSec=10`.
2. Order against the graphical session, which by construction comes up after the system's
   network target: `After=graphical-session-pre.target`, `PartOf=graphical-session.target`.
3. If you truly must gate on connectivity, add a user-level
   `ExecStartPre=/usr/bin/nm-online -q -t 30` (NetworkManager is enabled here —
   `NetworkManager-wait-online.service` is `enabled` and `network-online.target` is `active`
   at the *system* level **[V]**). Prefix with `-` so a timeout does not abort the start.

Do **not** enable `linger` (`loginctl enable-linger`) unless the user wants the mount alive
without a login session; it changes `graphical-session` semantics and starts the mount at
boot with no keyring, which will break the OAuth token refresh for OneDrive.

### 6.4 Automount semantics

systemd's `.automount` units pair with `.mount` units and require the mount to be expressed
as a `/etc/fstab`-style `.mount` unit — which for FUSE means invoking `mount.fuse` with the
rclone mount helper. That path exists (`rclone` installs no `mount.rclone` helper by
default, so you would need `/sbin/mount.rclone` yourself) but it is **not worth it**:

- On-demand triggering conflicts with the whole point of the VFS: the mount must be up for
  the poller to see remote changes and for pending uploads to drain.
- `.automount` idle-timeout unmounts would silently discard queued uploads.
- The GNOME sidebar and Nautilus want a persistent mount to bookmark.

**Recommendation: a plain, always-running `Type=notify` service.** If OneDriveUI wants
"start on demand", start the *service* from the app (`systemctl --user start
onedriveui-mount.service` over D-Bus) rather than using kernel automount.

To control the unit from Python without shelling out, use the systemd D-Bus API on the user
bus (`org.freedesktop.systemd1`, object `/org/freedesktop/systemd1`, methods
`StartUnit(name, "replace")`, `StopUnit`, `RestartUnit`, and
`GetUnit`→`org.freedesktop.systemd1.Unit`'s `ActiveState`/`SubState` properties). `gdbus`
and `gio` are both present **[V]**; from PySide6 use `QDBusConnection.sessionBus()`.

### 6.5 Logging

Prefer `--log-level INFO --use-json-log` and let systemd capture stdout into the journal
(`journalctl --user -u onedriveui-mount.service -f -o cat`). Structured JSON lines are far
easier for OneDriveUI to tail and turn into notifications than the plain format. Reserve
`--log-file` for when you deliberately want a file outside the journal. Do not run
production at `-vv` (DEBUG) — it emits a `looking for range=…` line per 128 KiB read.

**Log lines worth parsing:**

| Pattern | Meaning |
| --- | --- |
| `vfs cache: removed cache file as Removing old cache file not in use` | eviction — invalidate the badge, maybe re-pin |
| `vfs cache: detected external removal of cache file` | someone (probably you) evicted |
| `Signal received: terminated` / `Unmounted rclone mount` | clean shutdown |
| `failed to mount FUSE fs: … is not empty` | mountpoint dirty |
| `option allow_other only allowed if 'user_allow_other' is set` | needs the root fuse.conf edit |
| `poll-interval is not supported by this remote` | fall back to dir-cache expiry |
| **[V] observed live:** `IO error: couldn't list files: invalidRequest: invalidResourceId: ObjectHandle is Invalid` | a real Graph-side listing failure surfacing through the mount; surface as "Couldn't refresh — retrying" and trigger `vfs/forget` + `vfs/refresh` |

---

## 7. A bisync'd folder AND a mount of the same remote

### 7.1 The pitfalls, concretely

**Never point bisync at the mount.** `rclone bisync ~/OneDrive onedrive:` where `~/OneDrive`
*is* the mount is pathological:

- Every bisync listing pass reads every file's metadata **through** the VFS, which round-
  trips the same remote it is comparing against — you compare a remote to a cached view of
  itself.
- bisync writes to the local side ⇒ the VFS marks items `Dirty` ⇒ the write-back queue
  uploads them back to the same remote ⇒ modtimes change ⇒ the next bisync sees "changed on
  both sides" ⇒ conflict files (`.conflict1`, `.conflict2`) multiply.
- `--vfs-write-back 5s` means a file bisync just wrote is **not yet on the remote**;
  bisync's next listing sees a size/modtime that the remote does not have. **[D]** bisync's
  own caveat applies with force: *"Files that **change during** a bisync run may result in
  data loss."* Since v1.66 bisync uses a snapshot model, but *"an error can still occur if a
  file happens to change at the exact moment it's being read/written."* A VFS write-back
  queue guarantees exactly that timing.
- **[V]** OneDrive is `CaseInsensitive: True` and has 1-second modtime precision
  (`Precision: 1000000000`). Combined with the VFS's own modtime handling
  (`vfs cache: setting modification time to …` **[V]**), spurious "changed" verdicts are easy.

**Second pitfall: the same remote mounted while a separate local folder syncs it.**
This is fine *as long as the two live on different sub-paths of the remote*, or the mount
is read-only. If both cover the same remote sub-tree, a bisync-side change and a
mount-side change to the same file race, and the mount's `--poll-interval` will re-list the
folder mid-bisync.

**Third pitfall: shared cache directory.** If you run two rclone processes (one mount, one
bisync) against the same remote and one of them passes a backend option flag, they get
different `{HASH}` fs names and therefore different cache dirs (§3.2). Harmless but wasteful.
If they get the *same* fs name, the bisync process (cache-mode off) will not touch
`vfs/`, so there is no corruption risk either way — but keep `--cache-dir` explicit and
identical across all your invocations.

### 7.2 Recommended layout for a client offering BOTH

Give each mode its **own disjoint sub-tree of the remote**, and never overlap:

```
onedrive:                             (the account root)
├── /                (everything)  ──► MOUNT, on-demand, read-write
│                                       ~/OneDrive              ← the Files On-Demand view
└── (a chosen subset, by folder) ───► not applicable — see below
```

Two workable topologies:

**Topology A — mount-only (recommended; this is what Windows actually does).**
One mount at `~/OneDrive` with `--vfs-cache-mode full`. "Always keep on this device" is
implemented by OneDriveUI's own pin list (§3.6) + a big `--vfs-cache-max-size`. There is
**no bisync at all**. This is the closest analogue to the real OneDrive client, it has one
source of truth, and it eliminates every pitfall above.

*Trade-off:* pinned files live in `~/.cache/rclone/vfs/...`, not in `~/OneDrive`, so they
are not directly usable offline by path. In practice this does not matter — the mount
serves them from cache with no network, which is exactly "offline available". It only
matters if the mount is down, in which case nothing under `~/OneDrive` is reachable anyway.

**Topology B — split by folder, for users who want a real synced directory.**

```
~/OneDrive          → rclone mount onedrive:            (on-demand, full account)
~/OneDrive-Offline  → rclone bisync onedrive:Offline ~/OneDrive-Offline
```

Rules that make B safe:

1. The bisync'd remote path (`onedrive:Offline`) must be **excluded from the mount**:
   `rclone mount onedrive: ~/OneDrive --exclude '/Offline/**'`. **[D]** *"All the rclone
   filters can be used to select a subset of the files to be visible in the mount."*
   Now the two never see the same object.
2. bisync runs against the **remote directly** and a **real local directory**, never
   through the mount.
3. Serialise: never run bisync while the mount has a non-empty upload queue. Gate on
   `vfs/stats.diskCache.uploadsQueued == 0 && uploadsInProgress == 0`, or drain first via
   `vfs/queue-set-expiry`.
4. After bisync completes, tell the mount the remote changed:
   `vfs/forget {"dir":"Offline"}` — irrelevant if you excluded it, but do it for the parent
   if the exclusion is partial.
5. Keep `--resync` behind an explicit, scary UI action; use `--check-access` and a
   conservative `--max-delete` (bisync's default is 50%).
6. Two rclone processes ⇒ two rc ports. Use `--rc-addr 127.0.0.1:5572` for the mount and
   `--rc-addr 127.0.0.1:5573` for a long-lived `rclone rcd` that runs `sync/bisync` as an
   async job (`_async=true`) so you can poll `job/status`.

**[V]** Note this machine already has an `rclone rcd --rc-addr 127.0.0.1:5573 --rc-no-auth
--rc-files` process running alongside the mount on 5572 — that is exactly the shape of
topology B's control plane, and it is why port collisions must be handled: **check the port
before binding, or make it configurable.**

### 7.3 The one thing you must never do

Do not run `rclone sync`, `rclone bisync`, `rclone copy` or `rclone move` with the **mount
path as either side** while the mount is up. If a user asks for it, refuse and explain, or
transparently rewrite the mount path to the equivalent `onedrive:` path.

---

## 8. Recommended production flag set for a consumer OneDrive-like client

```console
rclone mount onedrive: "$HOME/OneDrive" \
  --config "$HOME/.config/rclone/rclone.conf" \
  --devname OneDrive \
  \
  `# --- Files On-Demand core ---` \
  --vfs-cache-mode full \
  --cache-dir "$HOME/.cache/rclone" \
  --vfs-cache-max-size 20G \
  --vfs-cache-min-free-space 5G \
  --vfs-cache-max-age 720h \
  --vfs-cache-poll-interval 1m \
  \
  `# --- responsiveness ---` \
  --dir-cache-time 24h \
  --poll-interval 1m \
  --vfs-refresh \
  --attr-timeout 5s \
  --vfs-fast-fingerprint \
  \
  `# --- throughput ---` \
  --buffer-size 32M \
  --vfs-read-ahead 128M \
  --vfs-read-chunk-size 32M \
  --vfs-read-chunk-size-limit 1G \
  --transfers 4 \
  --checkers 8 \
  --vfs-write-back 5s \
  \
  `# --- desktop integration ---` \
  --umask 022 \
  --rc --rc-addr 127.0.0.1:5572 --rc-no-auth \
  --log-level INFO --use-json-log
```

**Rationale, per flag:**

| Choice | Why |
| --- | --- |
| `--vfs-cache-mode full` | the only mode with per-range materialisation (§2.2) |
| `--vfs-cache-max-size 20G` | a visible, user-adjustable budget; expose it as a slider like OneDrive's storage settings |
| `--vfs-cache-min-free-space 5G` | protects the user's `/home` from the cache filling the disk; belt-and-braces with the size cap |
| `--vfs-cache-max-age 720h` (30d) | the `1h` default is far too aggressive for on-demand — it evicts files the user just "made available offline". 30 days, with the size cap doing the real work |
| `--dir-cache-time 24h` | safe *because* `--poll-interval` works on OneDrive; makes browsing instant |
| `--poll-interval 1m` | matches the existing working unit; drop to `30s` for snappier cross-device feel, at the cost of one delta query per 30 s |
| `--vfs-refresh` | warms the whole tree at login; with `ListR: False` this is ~1 request per directory, one time |
| `--attr-timeout 5s` | 5× fewer kernel round-trips than the `1s` default; the corruption window is acceptable for a single-user desktop (§4.5) |
| `--vfs-fast-fingerprint` | skips slow modtime lookups; keeps quickxor because OneDrive is `SlowHash: False` (§2.6) |
| `--buffer-size 32M` + `--vfs-read-ahead 128M` | smooth video/large-file streaming; read-ahead only applies in `full` mode |
| `--vfs-read-chunk-size 32M` + limit `1G` | the `128Mi` default means a 128 MiB request just to peek at a file header. 32M start with doubling to 1G gives quick first-byte **and** good sustained throughput |
| `--vfs-read-chunk-streams 0` (default) | Graph throttles; parallel streams cause 429s |
| `--transfers 4 --checkers 8` | Graph rate limits hard. The existing unit uses 8/16 — fine for a fast link, but 4/8 is the safer consumer default. Never exceed 8 transfers on OneDrive Personal |
| `--vfs-write-back 5s` (default) | fast enough to feel instant, long enough to coalesce an editor's save-temp-rename dance |
| `--umask 022` | 0644/0755, matches desktop expectations (§5.1) |
| `--rc --rc-no-auth` on `127.0.0.1` | loopback-only; **[V]** the port may already be taken — probe first |
| no `--allow-other` | avoids a root edit of `/etc/fuse.conf` (§5.2) |
| no `--daemon` | incompatible with `--rc` (§1.2); systemd owns the process |
| no `--vfs-links`, no `--vfs-metadata-extension` | neither has a Windows-client analogue; both add failure modes (§2.6) |

**Put `chunk_size = 30M` (and any other `--onedrive-*` tuning) in `rclone.conf`,
NOT on the command line** — otherwise the fs name becomes `onedrive{HASH}:` and the cache
directory changes out from under you (§3.2).

**Secure the rc port if you ever bind beyond loopback:** replace `--rc-no-auth` with
`--rc-user`/`--rc-pass` (or `--rc-htpasswd`). `--rc-no-auth` still requires auth for the
dangerous methods only when auth is configured; on a shared machine, prefer
`--rc-addr` on a unix socket via `rclone rc --unix-socket`, which is available
(**[V]** `rclone rc --unix-socket` exists in this build).

---

## 9. rc endpoint quick reference (all **[V]** present in v1.75.0)

All calls are `POST <base>/<path>` with `Content-Type: application/json` and a JSON body
(`{}` when there are no parameters). Errors return HTTP 500 with
`{"error":…,"input":…,"path":…,"status":500}`.

| Endpoint | Body | Returns |
| --- | --- | --- |
| `vfs/stats` | `{}` (or `{"fs":…}`) | `{fs, inUse, diskCache{…}, metadataCache{dirs,files}, opt{…}}` |
| `vfs/list` | `{}` | `{"vfses":[names]}` — **suffixed names are not addressable, see §1.6** |
| `vfs/queue` | `{}` | `{"queue":[{name,id,size,expiry,tries,delay,uploading}]}` |
| `vfs/queue-set-expiry` | `{"id":1,"expiry":-1e9}` (+ optional `"relative":true`) | `{}` |
| `vfs/refresh` | `{}` / `{"dir":"a","dir2":"b"}` / `{"recursive":"true"}` | `{"result":{"path":"OK"}}` |
| `vfs/forget` | `{}` / `{"file":"a.txt","dir":"b"}` | `{"forgotten":[…]}` |
| `vfs/poll-interval` | `{}` / `{"interval":"30s","timeout":"10s"}` | `{enabled,interval{raw,seconds,string},supported}` |
| `mount/mount` | `{"fs":…,"mountPoint":…,"vfsOpt":{…},"mountOpt":{…}}` | `{"mountPoint":…}` |
| `mount/unmount` | `{"mountPoint":…}` | `{}` |
| `mount/unmountall` | `{}` | `{}` |
| `mount/listmounts` | `{}` | `{"mountPoints":[{Fs,MountPoint,MountedOn}]}` — **rc-created mounts only** |
| `mount/types` | `{}` | `{"mountTypes":["mount","mount2","nfsmount"]}` |
| `core/stats` | `{}` / `{"group":…,"short":true}` | transfer stats incl. `transferring[]` with `bytes,eta,name,percentage,speed,speedAvg,size` |
| `core/pid` | `{}` | `{"pid":N}` |
| `core/version` | `{}` | version info |
| `core/quit` | `{}` | terminates rclone (systemd will restart it) |
| `core/bwlimit` | `{"rate":"1M"}` | set/read the bandwidth limit — wire this to a "Limit upload rate" setting |
| `options/get` / `options/info` / `options/blocks` | `{}` | global option template (**not** the live VFS, §2.7) |
| `operations/about` | `{"fs":"onedrive:"}` | quota — `{total,used,free,trashed}` |
| `operations/stat` | `{"fs":…,"remote":"a/b.txt"}` | `{"item":{Path,Name,Size,MimeType,ModTime,IsDir}}` |
| `operations/list` | `{"fs":…,"remote":…,"opt":{…}}` | lsjson-shaped listing |
| `operations/publiclink` | `{"fs":…,"remote":…}` | share link — OneDrive has `PublicLink: True` **[V]** |
| `job/status`, `job/list`, `job/stop` | `{"jobid":N}` | for `_async=true` calls |

Add `"_async": true` to any call to get `{"jobid":N}` back immediately and poll
`job/status`. Add `"_group":"name"` to tag its stats for `core/stats {"group":"name"}` —
that is how you build a per-operation progress bar.

---

## 10. Reproducing the experiment

```console
SP=/tmp/rclone-vfs-lab
mkdir -p $SP/{src,mnt,cache,conf}
printf '[testlocal]\ntype = local\n' > $SP/conf/rclone.conf
head -c 5000000 /dev/urandom > $SP/src/big2.bin

# Foreground under systemd so it survives your shell (a plain `&` gets SIGTERMed
# when the shell session ends — verified the hard way).
systemd-run --user --unit=rclone-lab --service-type=notify \
  -p ExecStop="/usr/bin/fusermount3 -uz $SP/mnt" \
  /usr/bin/rclone --config $SP/conf/rclone.conf mount "testlocal:$SP/src" $SP/mnt \
    --cache-dir $SP/cache --vfs-cache-mode full --vfs-cache-poll-interval 5s \
    --rc --rc-addr 127.0.0.1:5599 --rc-no-auth -vv --log-file $SP/mount.log

C=$(curl -sX POST :5599/vfs/stats -d '{}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["diskCache"]["path"])')
M=$(curl -sX POST :5599/vfs/stats -d '{}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["diskCache"]["pathMeta"])')

dd if=$SP/mnt/big2.bin of=/dev/null bs=4096 count=1        # partial read
sleep 10 && cat $M/big2.bin                                # → Rs: [{Pos:0,Size:520192}]
cat $SP/mnt/big2.bin > /dev/null                           # pin
sleep 8  && cat $M/big2.bin                                # → Rs: [{Pos:0,Size:5000000}]
rm -f $M/big2.bin $C/big2.bin                              # evict
grep 'external removal' $SP/mount.log

systemctl --user stop rclone-lab
fusermount3 -uz $SP/mnt; rm -rf $SP
```

**Housekeeping:** every mount created during this research was unmounted and every transient
unit stopped. The only remaining `fuse.rclone` entry in `/proc/mounts` is the user's
pre-existing `onedrive{MxOuf}: /home/user/OneDrive`, which was never modified.

---

## Sources

- [rclone mount](https://rclone.org/commands/rclone_mount/) — command and VFS documentation
- [rclone overview](https://rclone.org/overview/) — backend optional-features matrix
- [rclone bisync](https://rclone.org/bisync/) — concurrent-modification and caveat sections
- Local binary: `rclone v1.75.0` — `rclone mount --help`, `rclone help flags rc`,
  `rclone backend features onedrive:`, and the live rc API (`rc/list`, `options/info`,
  `vfs/*`, `mount/*`) on ports 5572 (real) and 5599 (test)
- `/etc/fuse.conf`, `/proc/mounts`, `systemctl --user`, `fusermount3` on this machine
