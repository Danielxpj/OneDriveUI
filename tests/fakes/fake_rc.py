"""A dict-backed rclone rc daemon, with v1.75.0's real quirks baked in.

`FakeRc` exposes `rc/client.RcClient`'s frozen surface (CONTRACTS §10.1) over
canned responses, so every work package can be tested with **no live rclone and
no network**. It is deliberately a small simulator rather than a mock: the
quirks below are the ones that break naive code, and a fake that hides them is
worse than no fake at all.

Quirks reproduced verbatim from `docs/research/rclone-rc-api.md`:

  * `core/stats` **omits** `transferring`, `checking` and `lastError` entirely
    when they are empty, and `short: true` drops the two lists as well.
  * `core/stats.eta` is present but **null** when indeterminate.
  * `checking` is a list of **plain strings**; `transferring` is a list of dicts.
  * `operations/stat` on a missing path returns **HTTP 200 `{"item": null}`**,
    while `operations/list` on a missing directory returns **HTTP 404**.
  * `vfs/refresh` rejects a JSON boolean for `recursive` with **HTTP 400**
    `value must be string "recursive"=true`. Every other boolean in the API is
    a real boolean; this one is not.
  * Job ids come back in the **`X-Rclone-Jobid`** response header on every call,
    sync or async, and `job/status` answers **HTTP 500 `job not found`** once a
    finished job has been garbage-collected.
  * `job/list` returns an `executeId` that is stable for the daemon process and
    **changes after a restart** — which is how a caller tells "the job expired"
    from "the daemon died".
  * Errors are the stable 4-key envelope `{"error", "input", "path", "status"}`.
  * `core/group-list` returns `{"groups": null}` (JSON null) when empty.
  * `core/bwlimit` echoes the rate normalised to binary units (`1M:100k` ->
    `1Mi:100Ki`), so the echo may never be string-compared with the input.
  * `vfs/queue-set-expiry` on an id that already left the queue is
    **HTTP 500 `id not found in queue`** — a normal race, not a failure.
  * `vfs/poll-interval` is **HTTP 500** on a backend without ChangeNotify.

The endpoints banned by invariants I7/I8 (`mount/mount`, `mount/unmount`,
`mount/listmounts`, `operations/cleanup`) raise `AssertionError` here, so a
package that calls one fails its own test instead of shipping the bug.

Usage
-----
    rc = FakeRc()                                  # or the `fake_rc` fixture
    rc.set_quota(total=1_104_880_336_896, used=252_544_077_005)
    rc.set_transfers([{"name": "a.bin", "size": 100, "bytes": 40}])
    stats = rc.call_blocking("core/stats")         # synchronous surface
    call = rc.call("core/stats")                   # asynchronous surface
    call.succeeded.connect(handler)
    rc.flush()                                     # deliver pending signals
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.constants import RC_TIMEOUT_S
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import RcEndpoint, utcnow_iso

__all__ = [
    "FakeRc", "FakeRcCall", "RcFault", "CallRecord",
    "BANNED_PATHS", "call_blocking", "is_alive", "registry", "reset_registry",
    "GO_ZERO_TIME",
]

#: Go's zero time, which `job/status.endTime` carries while a job is running.
GO_ZERO_TIME = "0001-01-01T00:00:00Z"

#: Endpoints no OneDriveUI module may ever call (ARCHITECTURE §3, I7 and I8).
BANNED_PATHS: frozenset[str] = frozenset({
    "mount/mount", "mount/unmount", "mount/unmountall", "mount/listmounts",
    "operations/cleanup",
})

_KIB = 1024


# ─────────────────────────────────────────────────────────────────────────────
# Scripting helpers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RcFault:
    """A scripted failure. Yields the real 4-key rclone error envelope."""
    status: int = 500
    message: str = "arbitrary error"

    def to_error(self, path: str, params: dict[str, Any]) -> RcError:
        return RcError(path, self.status, {
            "error": self.message,
            "input": dict(params),
            "path": path,
            "status": self.status,
        })


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One observed call, for assertions."""
    path: str
    params: dict[str, Any]
    group: str | None = None
    async_: bool = False
    config: dict[str, Any] | None = None
    filt: dict[str, Any] | None = None
    jobid: int = 0
    at: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# The async call object — mirrors rc/client.RcCall
# ─────────────────────────────────────────────────────────────────────────────

class FakeRcCall(QObject):
    """Stand-in for `RcCall`. Exactly one of `succeeded` / `failed` fires, once.

    Delivery is deferred, as it is over real HTTP, so a caller can connect after
    `call()` returns. `FakeRc.flush()` delivers everything pending without an
    event loop; with a running loop the delivery happens on the next turn.
    """

    succeeded = Signal(dict)
    failed = Signal(object)          # RcError

    def __init__(self, path: str, params: dict[str, Any], *,
                 result: dict[str, Any] | None = None,
                 error: RcError | None = None,
                 headers: dict[str, str] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.params = dict(params)
        self.headers: dict[str, str] = dict(headers or {})
        self.result: dict[str, Any] | None = result
        self.error: RcError | None = error
        self.delivered = False

    @property
    def jobid(self) -> int:
        return int(self.headers.get("X-Rclone-Jobid", 0))

    def deliver(self) -> bool:
        """Emit the outcome. Idempotent — a second call is a no-op."""
        if self.delivered:
            return False
        self.delivered = True
        if self.error is not None:
            self.failed.emit(self.error)
        else:
            self.succeeded.emit(self.result or {})
        return True

    def wait(self) -> dict[str, Any]:
        """Deliver and return the result, raising the error like a blocking call."""
        self.deliver()
        if self.error is not None:
            raise self.error
        return self.result or {}


# ─────────────────────────────────────────────────────────────────────────────
# The fake daemon
# ─────────────────────────────────────────────────────────────────────────────

class FakeRc(QObject):
    """A whole rc daemon in a dict.

    Everything a test wants to steer lives in a plain attribute; anything not
    steered answers with a realistic default. Responses can also be overridden
    per endpoint (`set`), scripted as a sequence (`script`) or failed (`fail`).
    """

    #: Emitted by `restart()` with the new executeId, like `RcdSupervisor.restarted`.
    restarted = Signal(str)

    def __init__(self, *, endpoint: RcEndpoint | None = None,
                 execute_id: str | None = None,
                 deliver_mode: str = "queued",
                 register: bool = True,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        if deliver_mode not in ("queued", "manual", "immediate"):
            raise ValueError(f"deliver_mode must be queued|manual|immediate, not {deliver_mode!r}")
        self.deliver_mode = deliver_mode
        self.execute_id = execute_id or str(uuid.uuid4())
        self.endpoint = endpoint or RcEndpoint(
            kind="rcd", host="127.0.0.1", port=17800, user="onedriveui",
            password="fake-rc-password", pid=424242, starttime=99887766,
            execute_id=self.execute_id, account_id="onedrive",
        )
        #: RcEndpoint is frozen, so the executeId is kept in step by REBUILDING
        #: the endpoint — here and again on every restart().
        self.endpoint = self._with_execute_id(self.endpoint, self.execute_id)

        # ── observable world ────────────────────────────────────────────────
        self.alive = True
        self.offline = False                 #: every call raises DaemonUnavailable
        self.foreign = False                 #: ownership proof fails
        self.version = "v1.75.0"
        self.decomposed = [1, 75, 0]
        self.pid = self.endpoint.pid
        self.fs_name = "onedrive:"
        self.vfs_names: list[str] = ["onedrive:"]
        self.supports_change_notify = True
        self.poll_interval_s = 10
        self.auth_error: str | None = None   #: makes operations/about fail

        self.bwlimit_rate = "off"
        self.bytes_per_second = -1
        self.bytes_per_second_tx = -1
        self.bytes_per_second_rx = -1

        self.about: dict[str, Any] = {
            "total": 1_104_880_336_896, "used": 252_544_077_005,
            "free": 852_336_259_891, "trashed": 0,
        }
        self.stats: dict[str, Any] = self._empty_stats()
        self.transferring: list[dict[str, Any]] = []
        self.checking: list[str] = []
        self.transferred: list[dict[str, Any]] = []
        self.groups: list[str] = []
        self.queue: list[dict[str, Any]] = []
        self.disk_cache: dict[str, Any] = {
            "bytesUsed": 0, "erroredFiles": 0, "files": 0, "hashType": 4096,
            "outOfSpace": False,
            "path": "/home/u/.cache/rclone/vfs/onedrive",
            "pathMeta": "/home/u/.cache/rclone/vfsMeta/onedrive",
            "uploadsInProgress": 0, "uploadsQueued": 0,
        }
        self.metadata_cache: dict[str, int] = {"dirs": 39, "files": 63}
        self.in_use = 1
        self.vfs_opt: dict[str, Any] = {"CacheMode": 3, "CacheMaxAge": 3600000000000}
        self.remotes: list[str] = ["onedrive"]
        self.config: dict[str, dict[str, Any]] = {
            "onedrive": {"type": "onedrive", "drive_id": "b!fake", "drive_type": "personal"},
        }
        self.fsinfo: dict[str, Any] = self._default_fsinfo()
        #: (fs, remote) -> list of operations/list entries. Built by add_file().
        self.tree: dict[tuple[str, str], list[dict[str, Any]]] = {}

        # ── scripting ───────────────────────────────────────────────────────
        self.responses: dict[str, Any] = {}
        self._scripts: dict[str, list[Any]] = {}
        self._script_repeat: dict[str, bool] = {}
        self._faults: dict[str, list[RcFault | None]] = {}
        self.calls: list[CallRecord] = []
        self.pending: list[FakeRcCall] = []
        self.closed = False

        # ── jobs ────────────────────────────────────────────────────────────
        self._next_jobid = 1
        self.jobs: dict[int, dict[str, Any]] = {}
        self.expired_jobs: set[int] = set()
        self.last_headers: dict[str, str] = {}

        if register:
            registry[(self.endpoint.host, self.endpoint.port)] = self

    # ── construction helpers ────────────────────────────────────────────────

    @staticmethod
    def _with_execute_id(ep: RcEndpoint, execute_id: str) -> RcEndpoint:
        return RcEndpoint(
            kind=ep.kind, host=ep.host, port=ep.port, user=ep.user,
            password=ep.password, pid=ep.pid, starttime=ep.starttime,
            execute_id=execute_id, mountpoint=ep.mountpoint, account_id=ep.account_id,
        )

    @staticmethod
    def _empty_stats() -> dict[str, Any]:
        """The verbatim idle `core/stats` body, minus the omitted-when-empty keys."""
        return {
            "bytes": 0, "checks": 0, "deletedDirs": 0, "deletes": 0,
            "elapsedTime": 7.925e-06, "errors": 0, "eta": None, "fatalError": False,
            "listed": 0, "renames": 0, "retryError": False,
            "serverSideCopies": 0, "serverSideCopyBytes": 0,
            "serverSideMoveBytes": 0, "serverSideMoves": 0,
            "speed": 0, "totalBytes": 0, "totalChecks": 0, "totalTransfers": 0,
            "transferTime": 0, "transfers": 0,
        }

    @staticmethod
    def _default_fsinfo() -> dict[str, Any]:
        true_features = (
            "About", "CanHaveEmptyDirectories", "CaseInsensitive", "ChangeNotify",
            "CleanUp", "Copy", "DirCacheFlush", "DirMove", "DirSetModTime", "ListP",
            "MkdirMetadata", "Move", "PublicLink", "Purge", "ReadDirMetadata",
            "ReadMetadata", "ReadMimeType", "Shutdown", "WriteDirMetadata",
            "WriteDirSetModTime", "WriteMetadata",
        )
        false_features = (
            "BucketBased", "BucketBasedRootOK", "CaseSensitive", "Command",
            "DuplicateFiles", "FilterAware", "GetTier", "IsLocal", "ListR",
            "NoMultiThreading", "OpenChunkWriter", "OpenWriterAt", "PartialUploads",
            "PutStream", "PutUnchecked", "ReadDirMetadata", "ServerSideAcrossConfigs",
            "SetTier", "SetWrapper", "SlowHash", "SlowModTime", "UnWrap",
            "UserDirMetadata", "UserInfo", "UserMetadata", "WrapFs",
        )
        features = {name: False for name in false_features}
        features.update({name: True for name in true_features})
        return {
            "Name": "onedrive", "Root": "", "String": "OneDrive root ''",
            "Precision": 1_000_000_000, "Hashes": ["quickxor"], "Features": features,
            "MetadataInfo": {"System": {}, "Help": ""},
        }

    # ── world steering ──────────────────────────────────────────────────────

    def set_quota(self, total: int | None = None, used: int | None = None,
                  free: int | None = None, trashed: int | None = 0) -> None:
        """Set what `operations/about` answers. `trashed=None` omits the key, as
        the local backend does."""
        if total is not None:
            self.about["total"] = total
        if used is not None:
            self.about["used"] = used
        self.about["free"] = free if free is not None else max(
            0, int(self.about.get("total", 0)) - int(self.about.get("used", 0)))
        if trashed is None:
            self.about.pop("trashed", None)
        else:
            self.about["trashed"] = trashed

    def set_transfers(self, rows: Iterable[dict[str, Any]]) -> None:
        """Populate `core/stats.transferring[]`. Missing keys are filled in with
        the shape v1.75.0 really emits (`speedAvg`, `group`, `srcFs`, `dstFs`)."""
        out: list[dict[str, Any]] = []
        for row in rows:
            size = int(row.get("size", 0))
            done = int(row.get("bytes", 0))
            entry = {
                "name": row["name"],
                "size": size,
                "bytes": done,
                "percentage": int(row.get("percentage", (done * 100 // size) if size > 0 else 0)),
                "speed": float(row.get("speed", 533074.320824698)),
                "speedAvg": float(row.get("speedAvg", 536569.9085890673)),
                "eta": row.get("eta", None),
                "group": row.get("group", "sync/onedrive"),
                "srcFs": row.get("srcFs", "/home/u/OneDrive"),
                "dstFs": row.get("dstFs", "onedrive:"),
            }
            out.append(entry)
        self.transferring = out
        self.stats["bytes"] = sum(e["bytes"] for e in out) or self.stats.get("bytes", 0)
        self.stats["totalBytes"] = sum(e["size"] for e in out) or self.stats.get("totalBytes", 0)

    def set_checking(self, names: Iterable[str]) -> None:
        """`checking` is a list of PLAIN STRINGS, never objects."""
        self.checking = [str(n) for n in names]

    def set_error(self, message: str, *, errors: int = 1, fatal: bool = False,
                  retry: bool = False) -> None:
        """Set `errors` / `lastError`; `lastError` only appears when errors > 0."""
        self.stats["errors"] = errors
        self.stats["fatalError"] = fatal
        self.stats["retryError"] = retry
        self.stats["lastError"] = message

    def clear_error(self) -> None:
        self.stats["errors"] = 0
        self.stats["fatalError"] = False
        self.stats["retryError"] = False
        self.stats.pop("lastError", None)

    def set_eta(self, seconds: int | None) -> None:
        """`eta` is an int or `null` — never absent."""
        self.stats["eta"] = seconds

    def add_transferred(self, name: str, *, size: int = 0, done: int | None = None,
                        error: str = "", group: str = "sync/onedrive",
                        src_fs: str = "/home/u/OneDrive", dst_fs: str = "onedrive:",
                        what: str = "transferring") -> dict[str, Any]:
        """Append to `core/transferred` (which keeps only the last 100 rows and
        uses `started_at`/`completed_at`, NOT the documented `timestamp`)."""
        row = {
            "error": error, "name": name, "size": size,
            "bytes": size if done is None else done, "checked": False, "what": what,
            "started_at": utcnow_iso(), "completed_at": utcnow_iso(),
            "group": group, "srcFs": src_fs, "dstFs": dst_fs,
        }
        self.transferred.append(row)
        del self.transferred[:-100]
        return row

    def add_queue_item(self, name: str, *, size: int = 0, expiry: float = 4.99,
                       tries: int = 0, delay: float = 5.0,
                       uploading: bool = False) -> dict[str, Any]:
        item = {"name": name, "id": len(self.queue) + 1, "size": size,
                "expiry": expiry, "tries": tries, "delay": delay,
                "uploading": uploading}
        self.queue.append(item)
        self.disk_cache["uploadsQueued"] = sum(1 for q in self.queue if not q["uploading"])
        self.disk_cache["uploadsInProgress"] = sum(1 for q in self.queue if q["uploading"])
        return item

    def add_file(self, rel_path: str, *, size: int = 0, is_dir: bool = False,
                 fs: str | None = None, mod_time: str = "2026-08-30T23:25:08Z",
                 item_id: str = "", malware: bool = False,
                 mime_type: str | None = None) -> dict[str, Any]:
        """Add one object to the fake remote, visible to operations/list + stat.

        `Path` is relative to `fs`, exactly as rclone returns it, and a directory
        reports `Size: -1` the way OneDrive does.
        """
        fs = fs or self.fs_name
        parent, _, name = rel_path.rpartition("/")
        entry = {
            "Path": rel_path,
            "Name": name or rel_path,
            "Size": -1 if is_dir else size,
            "MimeType": mime_type or ("inode/directory" if is_dir else "application/octet-stream"),
            "ModTime": mod_time,
            "IsDir": is_dir,
            "ID": item_id or f"FAKEID!{abs(hash(rel_path)) % 10_000}",
            "Metadata": {
                "btime": mod_time, "mtime": mod_time, "utime": mod_time,
                "content-type": "inode/directory" if is_dir else "application/octet-stream",
                "created-by-display-name": "Test User",
                "last-modified-by-display-name": "Test User",
                "malware-detected": "true" if malware else "false",
            },
        }
        self.tree.setdefault((fs, parent), []).append(entry)
        return entry

    # ── scripting ───────────────────────────────────────────────────────────

    def set(self, path: str, response: Any) -> None:
        """Override one endpoint permanently. `response` may be a dict, an
        `RcFault`, or a callable taking the params dict."""
        self.responses[path] = response

    def script(self, path: str, responses: Iterable[Any], *,
               repeat_last: bool = True) -> None:
        """Queue a sequence of answers for successive calls to `path`.

        Each item may be a dict, a callable(params) -> dict, or an `RcFault`.
        When the queue empties the last item repeats unless `repeat_last=False`,
        in which case the endpoint falls back to its built-in behaviour.
        """
        self._scripts[path] = list(responses)
        self._script_repeat[path] = repeat_last

    def fail(self, path: str, *, status: int = 500, message: str = "arbitrary error",
             times: int | None = None) -> None:
        """Make `path` fail with the real 4-key envelope, `times` times (or
        forever when `times` is None)."""
        fault = RcFault(status=status, message=message)
        if times is None:
            self.responses[path] = fault
        else:
            self._faults[path] = [fault] * int(times)

    def clear_scripts(self) -> None:
        self.responses.clear()
        self._scripts.clear()
        self._faults.clear()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def restart(self, *, new_execute_id: str | None = None) -> str:
        """Simulate a daemon restart: a NEW executeId, job ids from 1 again, no
        job history, no groups and zeroed stats. This is what tells a caller
        that a `job not found` means "gone", not merely "expired"."""
        self.execute_id = new_execute_id or str(uuid.uuid4())
        self.endpoint = self._with_execute_id(self.endpoint, self.execute_id)
        registry[(self.endpoint.host, self.endpoint.port)] = self
        self.jobs.clear()
        self.expired_jobs.clear()
        self._next_jobid = 1
        self.groups.clear()
        self.stats = self._empty_stats()
        self.transferring.clear()
        self.checking.clear()
        self.transferred.clear()
        self.restarted.emit(self.execute_id)
        return self.execute_id

    def expire_jobs(self, *job_ids: int) -> list[int]:
        """Garbage-collect finished jobs, as --rc-job-expire-duration does.
        With no arguments every finished job expires."""
        if job_ids:
            gone = [j for j in job_ids if j in self.jobs]
        else:
            gone = [i for i, j in self.jobs.items() if j.get("finished")]
        for job_id in gone:
            self.jobs.pop(job_id, None)
            self.expired_jobs.add(job_id)
        return gone

    def close(self) -> None:
        """Mirror `RcClient.close()`; pending calls are dropped, not delivered."""
        self.closed = True
        self.pending.clear()

    def stop(self) -> None:
        """The daemon goes away — every later call raises DaemonUnavailable."""
        self.alive = False
        self.offline = True

    # ── call recording helpers ──────────────────────────────────────────────

    def calls_to(self, path: str) -> list[CallRecord]:
        return [c for c in self.calls if c.path == path]

    def count(self, path: str) -> int:
        return len(self.calls_to(path))

    def last(self, path: str) -> CallRecord | None:
        rows = self.calls_to(path)
        return rows[-1] if rows else None

    def assert_never(self, path: str) -> None:
        assert self.count(path) == 0, f"{path} was called {self.count(path)}x"

    # ── the RcClient surface ────────────────────────────────────────────────

    def call(self, path: str, params: dict[str, Any] | None = None, *,
             group: str | None = None, async_: bool = False,
             config: dict[str, Any] | None = None,
             filt: dict[str, Any] | None = None,
             timeout_s: float = RC_TIMEOUT_S) -> FakeRcCall:
        """Asynchronous surface. The outcome is delivered on `flush()` or on the
        next event-loop turn, never before this returns."""
        params = dict(params or {})
        if group is not None:
            params.setdefault("_group", group)
        if async_:
            params.setdefault("_async", True)
        if config is not None:
            params.setdefault("_config", dict(config))
        if filt is not None:
            params.setdefault("_filter", dict(filt))
        try:
            result, headers = self._dispatch(path, params)
            call = FakeRcCall(path, params, result=result, headers=headers, parent=self)
        except RcError as exc:
            call = FakeRcCall(path, params, error=exc,
                              headers=dict(self.last_headers), parent=self)
        self.pending.append(call)
        if self.deliver_mode == "immediate":
            self.flush()
        elif self.deliver_mode == "queued":
            QTimer.singleShot(0, call.deliver)
        return call

    def call_blocking(self, path: str, params: dict[str, Any] | None = None, *,
                      timeout_s: float = 30.0) -> dict[str, Any]:
        """Synchronous surface, matching module-level `rc.client.call_blocking`.
        Raises `RcError` / `DaemonUnavailable` exactly as the real one does."""
        result, _headers = self._dispatch(path, dict(params or {}))
        return result

    def flush(self) -> int:
        """Deliver every pending async outcome, in call order."""
        pending, self.pending = self.pending, []
        return sum(1 for call in pending if call.deliver())

    def is_alive(self, timeout_s: float = 1.0) -> bool:
        """`rc/noop` liveness probe."""
        if self.offline or not self.alive:
            return False
        try:
            self.call_blocking("rc/noop")
        except RcError:
            return False
        return True

    def job_status(self, job_id: int) -> dict[str, Any]:
        return self.call_blocking("job/status", {"jobid": job_id})

    # ── dispatch ────────────────────────────────────────────────────────────

    def _dispatch(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        if path in BANNED_PATHS:
            raise AssertionError(
                f"{path} is banned by invariant I7/I8 and must never be called"
            )
        if self.closed:
            raise DaemonUnavailable(path, 503, {
                "error": "client closed", "input": dict(params), "path": path,
                "status": 503})
        if self.offline or not self.alive:
            raise DaemonUnavailable(path, 503, {
                "error": ("connection failed: Post \"http://%s:%d/%s\": dial tcp: "
                          "connect: connection refused"
                          % (self.endpoint.host, self.endpoint.port, path)),
                "input": dict(params), "path": path, "status": 503})

        jobid = self._next_jobid
        self._next_jobid += 1
        headers = {
            "Content-Type": "application/json",
            "Server": f"rclone/{self.version}",
            "X-Rclone-Jobid": str(jobid),
        }
        self.last_headers = headers
        self.calls.append(CallRecord(
            path=path, params=dict(params), group=params.get("_group"),
            async_=bool(params.get("_async")), config=params.get("_config"),
            filt=params.get("_filter"), jobid=jobid, at=utcnow_iso()))

        scripted = self._take_scripted(path)
        if scripted is not None:
            body = self._materialise(scripted, path, params)
        else:
            handler = self._handlers().get(path)
            if handler is None:
                raise RcError(path, 404, {
                    "error": f"couldn't find method \"{path}\"",
                    "input": dict(params), "path": path, "status": 404})
            body = handler(params)

        if params.get("_async"):
            # An async call answers immediately with the job handle; the body it
            # would have returned becomes job/status.output.
            group = params.get("_group") or f"job/{jobid}"
            if group not in self.groups:
                self.groups.append(group)
            self.jobs[jobid] = {
                "id": jobid, "group": group, "finished": True, "success": True,
                "error": "", "output": body, "executeId": self.execute_id,
                "startTime": utcnow_iso(), "endTime": utcnow_iso(), "duration": 0.0,
            }
            return {"jobid": jobid, "executeId": self.execute_id}, headers

        group = params.get("_group")
        if group and group not in self.groups:
            self.groups.append(group)
        return body, headers

    def _take_scripted(self, path: str) -> Any:
        faults = self._faults.get(path)
        if faults:
            return faults.pop(0)
        queue = self._scripts.get(path)
        if queue:
            item = queue.pop(0)
            if not queue and self._script_repeat.get(path, True):
                queue.append(item)
            return item
        if path in self.responses:
            return self.responses[path]
        return None

    def _materialise(self, scripted: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if isinstance(scripted, RcFault):
            raise scripted.to_error(path, params)
        if isinstance(scripted, RcError):
            raise scripted
        if callable(scripted):
            produced = scripted(params)
            if isinstance(produced, (RcFault, RcError)):
                return self._materialise(produced, path, params)
            return dict(produced or {})
        return dict(scripted or {})

    def _error(self, path: str, status: int, message: str,
               params: dict[str, Any]) -> RcError:
        return RcError(path, status, {"error": message, "input": dict(params),
                                      "path": path, "status": status})

    # ── endpoint handlers ───────────────────────────────────────────────────

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "rc/noop": self._rc_noop,
            "rc/noopauth": self._rc_noop,
            "rc/error": self._rc_error,
            "rc/list": lambda p: {"commands": []},
            "core/version": self._core_version,
            "core/pid": lambda p: {"pid": self.pid},
            "core/quit": self._core_quit,
            "core/bwlimit": self._core_bwlimit,
            "core/stats": self._core_stats,
            "core/stats-reset": self._core_stats_reset,
            "core/stats-delete": self._core_stats_delete,
            "core/transferred": self._core_transferred,
            "core/group-list": self._core_group_list,
            "core/memstats": lambda p: {"Alloc": 4_194_304, "Sys": 25_165_824,
                                        "HeapAlloc": 4_194_304, "TotalAlloc": 8_388_608},
            "core/obscure": lambda p: {"obscured": "URnbSiLBL00tUaMC1aZyo15gHBx7wEQ"},
            "core/du": lambda p: {"dir": p.get("dir", "/home/u/.cache/rclone"),
                                  "info": {"Free": 108_777_537_536,
                                           "Available": 106_200_920_064,
                                           "Total": 263_064_326_144}},
            "core/disks": lambda p: {"disks": ["/home/u", "/"]},
            "core/gc": lambda p: {},
            "job/list": self._job_list,
            "job/status": self._job_status,
            "job/stop": self._job_stop,
            "job/stopgroup": self._job_stopgroup,
            "operations/list": self._operations_list,
            "operations/stat": self._operations_stat,
            "operations/about": self._operations_about,
            "operations/fsinfo": lambda p: dict(self.fsinfo),
            "operations/mkdir": lambda p: {},
            "operations/rmdir": lambda p: {},
            "operations/purge": lambda p: {},
            "operations/deletefile": lambda p: {},
            "operations/movefile": lambda p: {},
            "operations/copyfile": lambda p: {},
            "operations/size": lambda p: {"count": 2, "bytes": 143_330},
            "operations/publiclink": lambda p: {
                "url": "https://1drv.ms/u/s!FakeShareLink"},
            "operations/check": lambda p: {"success": True, "status": "0 differences found"},
            "sync/copy": lambda p: {},
            "sync/move": lambda p: {},
            "sync/sync": lambda p: {},
            "sync/bisync": lambda p: {},
            "vfs/list": lambda p: {"vfses": list(self.vfs_names)},
            "vfs/stats": self._vfs_stats,
            "vfs/refresh": self._vfs_refresh,
            "vfs/forget": self._vfs_forget,
            "vfs/poll-interval": self._vfs_poll_interval,
            "vfs/queue": lambda p: {"queue": [dict(q) for q in self.queue]},
            "vfs/queue-set-expiry": self._vfs_queue_set_expiry,
            "config/listremotes": lambda p: {"remotes": list(self.remotes)},
            "config/dump": lambda p: {k: dict(v) for k, v in self.config.items()},
            "config/get": lambda p: dict(self.config.get(p.get("name", ""), {})),
            "config/create": self._config_create,
            "config/update": self._config_create,
            "config/password": lambda p: {},
            "options/blocks": lambda p: {"blocks": ["main", "vfs", "mount", "filter"]},
            "options/get": lambda p: {"main": {"Transfers": 4, "Checkers": 8}},
        }

    # rc/*, core/*
    def _rc_noop(self, params: dict[str, Any]) -> dict[str, Any]:
        return dict(params)

    def _rc_error(self, params: dict[str, Any]) -> dict[str, Any]:
        raise self._error("rc/error", 500,
                          f"arbitrary error on input {params}", params)

    def _core_version(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "arch": "amd64", "decomposed": list(self.decomposed), "goTags": "none",
            "goVersion": "go1.26.5-X:nodwarf5", "isBeta": False, "isGit": False,
            "linking": "dynamic", "os": "linux", "osArch": "amd64",
            "osKernel": "6.18.42-1-cachyos-lts (x86_64)",
            "osVersion": "cachyos (64 bit)", "version": self.version,
        }

    def _core_quit(self, params: dict[str, Any]) -> dict[str, Any]:
        self.alive = False
        return {}

    def _core_bwlimit(self, params: dict[str, Any]) -> dict[str, Any]:
        rate = params.get("rate")
        if rate is not None:
            self.bwlimit_rate = self._normalise_rate(str(rate))
            tx, rx = self._parse_rate(str(rate))
            self.bytes_per_second_tx = tx
            self.bytes_per_second_rx = rx
            self.bytes_per_second = tx
        return {
            "bytesPerSecond": self.bytes_per_second,
            "bytesPerSecondRx": self.bytes_per_second_rx,
            "bytesPerSecondTx": self.bytes_per_second_tx,
            "rate": self.bwlimit_rate,
        }

    @staticmethod
    def _one_rate(text: str) -> int:
        text = text.strip()
        if not text or text.lower() in ("off", "0"):
            return -1
        units = {"b": 1, "k": _KIB, "m": _KIB ** 2, "g": _KIB ** 3,
                 "t": _KIB ** 4, "p": _KIB ** 5}
        body = text.rstrip("iIbB") if text[-1] in "iIbB" else text
        suffix = body[-1].lower()
        if suffix in units:
            return int(float(body[:-1]) * units[suffix])
        return int(float(body))

    def _parse_rate(self, rate: str) -> tuple[int, int]:
        """-> (tx, rx). rclone's own order is upload:download."""
        if ":" in rate:
            up, _, down = rate.partition(":")
            return self._one_rate(up), self._one_rate(down)
        one = self._one_rate(rate)
        return one, one

    def _normalise_rate(self, rate: str) -> str:
        """rclone echoes back binary units — '1M:100k' becomes '1Mi:100Ki'."""
        tx, rx = self._parse_rate(rate)
        if tx < 0 and rx < 0:
            return "off"

        def fmt(value: int) -> str:
            if value < 0:
                return "off"
            for suffix, mult in (("Pi", _KIB ** 5), ("Ti", _KIB ** 4), ("Gi", _KIB ** 3),
                                 ("Mi", _KIB ** 2), ("Ki", _KIB)):
                if value >= mult and value % mult == 0:
                    return f"{value // mult}{suffix}"
            return str(value)

        return fmt(tx) if tx == rx else f"{fmt(tx)}:{fmt(rx)}"

    def _core_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Absent keys mean empty — that is the single most-missed rc quirk."""
        out = dict(self.stats)
        out.setdefault("eta", None)
        if int(out.get("errors", 0)) <= 0:
            out.pop("lastError", None)
        if not params.get("short"):
            if self.transferring:
                out["transferring"] = [dict(t) for t in self.transferring]
            if self.checking:
                out["checking"] = list(self.checking)
        return out

    def _core_stats_reset(self, params: dict[str, Any]) -> dict[str, Any]:
        self.stats = self._empty_stats()
        self.transferring.clear()
        self.checking.clear()
        self.transferred.clear()
        return {}

    def _core_stats_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        group = params.get("group")
        if group in self.groups:
            self.groups.remove(group)
        return {}

    def _core_transferred(self, params: dict[str, Any]) -> dict[str, Any]:
        group = params.get("group")
        rows = [dict(r) for r in self.transferred
                if not group or r.get("group") == group]
        return {"transferred": rows}

    def _core_group_list(self, params: dict[str, Any]) -> dict[str, Any]:
        #: JSON null, not [] — null-check this one.
        return {"groups": list(self.groups) if self.groups else None}

    # job/*
    def _job_list(self, params: dict[str, Any]) -> dict[str, Any]:
        running = [i for i, j in self.jobs.items() if not j.get("finished")]
        finished = [i for i, j in self.jobs.items() if j.get("finished")]
        return {"executeId": self.execute_id,
                "jobids": running + finished,
                "runningIds": running,
                "finishedIds": finished}

    def _job_status(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            job_id = int(params.get("jobid", -1))
        except (TypeError, ValueError):
            raise self._error("job/status", 400,
                              "Didn't find key \"jobid\" in input", params) from None
        job = self.jobs.get(job_id)
        if job is None:
            #: Expired OR the daemon restarted — disambiguate on executeId.
            raise self._error("job/status", 500, "job not found", params)
        out = dict(job)
        out["executeId"] = self.execute_id
        if not out.get("finished"):
            out["endTime"] = GO_ZERO_TIME
        return out

    def _job_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.get(int(params.get("jobid", -1)))
        if job is not None and not job.get("finished"):
            job.update(finished=True, success=False, error="context canceled",
                       output={}, endTime=utcnow_iso())
        return {}

    def _job_stopgroup(self, params: dict[str, Any]) -> dict[str, Any]:
        group = params.get("group")
        for job in self.jobs.values():
            if job.get("group") == group and not job.get("finished"):
                job.update(finished=True, success=False, error="context canceled",
                           output={}, endTime=utcnow_iso())
        return {}

    # operations/*
    def _operations_list(self, params: dict[str, Any]) -> dict[str, Any]:
        fs = params.get("fs", self.fs_name)
        remote = params.get("remote", "") or ""
        opt = params.get("opt") or {}
        key = (fs, remote)
        if key not in self.tree and remote:
            #: A missing directory is a 404 here — but a 200 for operations/stat.
            raise self._error("operations/list", 404,
                              "error in ListJSON: directory not found", params)
        #: The root always exists, even when empty.
        rows = [dict(r) for r in self.tree.get(key, [])]
        if opt.get("recurse"):
            for (kfs, kremote), entries in self.tree.items():
                if kfs != fs or kremote == remote:
                    continue
                if remote and not kremote.startswith(f"{remote}/"):
                    continue
                rows.extend(dict(r) for r in entries)
        if opt.get("dirsOnly"):
            rows = [r for r in rows if r["IsDir"]]
        if opt.get("filesOnly"):
            rows = [r for r in rows if not r["IsDir"]]
        if opt.get("noModTime"):
            for row in rows:
                row["ModTime"] = ""
        if opt.get("noMimeType"):
            for row in rows:
                row.pop("MimeType", None)
        if not opt.get("metadata"):
            for row in rows:
                row.pop("Metadata", None)
        return {"list": rows}

    def _find(self, fs: str, remote: str) -> dict[str, Any] | None:
        parent, _, _name = remote.rpartition("/")
        for entry in self.tree.get((fs, parent), []):
            if entry["Path"] == remote:
                return entry
        for (kfs, _kremote), entries in self.tree.items():
            if kfs != fs:
                continue
            for entry in entries:
                if entry["Path"] == remote:
                    return entry
        return None

    def _operations_stat(self, params: dict[str, Any]) -> dict[str, Any]:
        fs = params.get("fs", self.fs_name)
        remote = params.get("remote", "") or ""
        entry = self._find(fs, remote)
        #: HTTP 200 with a null item — NOT an error — for a missing path.
        if entry is None:
            return {"item": None}
        item = dict(entry)
        if not (params.get("opt") or {}).get("metadata"):
            item.pop("Metadata", None)
        return {"item": item}

    def _operations_about(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.auth_error:
            raise self._error("operations/about", 500, self.auth_error, params)
        return dict(self.about)

    # vfs/*
    def _vfs_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "diskCache": dict(self.disk_cache),
            "fs": self.fs_name,
            "inUse": self.in_use,
            "metadataCache": dict(self.metadata_cache),
            "opt": dict(self.vfs_opt),
        }

    def _vfs_refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        recursive = params.get("recursive")
        #: The one boolean in the whole API that must be the STRING "true".
        if isinstance(recursive, bool):
            raise self._error("vfs/refresh", 400,
                              'value must be string "recursive"=true', params)
        if recursive is not None and str(recursive).lower() not in ("true", "false"):
            raise self._error("vfs/refresh", 400,
                              'value must be string "recursive"=true', params)
        dirs = {k: v for k, v in params.items() if k.startswith("dir")}
        if not dirs:
            return {"result": {"": "OK"}}
        result: dict[str, str] = {}
        for _key, value in dirs.items():
            name = str(value)
            #: An explicit empty dir is an error; omit `dir` to refresh the root.
            result[name] = "OK" if name else "file does not exist"
        return {"result": result}

    def _vfs_forget(self, params: dict[str, Any]) -> dict[str, Any]:
        names = [str(v) for k, v in params.items()
                 if k.startswith("dir") or k.startswith("file")]
        return {"forgotten": names}

    def _vfs_poll_interval(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.supports_change_notify:
            raise self._error("vfs/poll-interval", 500,
                              "poll-interval is not supported by this remote", params)
        interval = params.get("interval")
        if interval is not None:
            text = str(interval)
            seconds = int(float(text[:-1])) if text.endswith("s") else int(float(text))
            self.poll_interval_s = seconds
        out = {
            "enabled": self.poll_interval_s > 0,
            "interval": {"raw": self.poll_interval_s * 1_000_000_000,
                         "seconds": self.poll_interval_s,
                         "string": f"{self.poll_interval_s}s"},
            "supported": True,
        }
        if interval is not None:
            out["timeout"] = False
        return out

    def _vfs_queue_set_expiry(self, params: dict[str, Any]) -> dict[str, Any]:
        item_id = int(params.get("id", -1))
        for item in self.queue:
            if item["id"] == item_id:
                expiry = float(params.get("expiry", 0.0))
                item["expiry"] = item["expiry"] + expiry if params.get("relative") else expiry
                return {}
        #: A normal ~5 s race against --vfs-write-back, not a failure.
        raise self._error("vfs/queue-set-expiry", 500, "id not found in queue", params)

    # config/*
    def _config_create(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        if name:
            self.config.setdefault(name, {}).update(params.get("parameters") or {})
            if name not in self.remotes:
                self.remotes.append(name)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Module-level surface, mirroring rc/client.py so it can be monkeypatched in
# ─────────────────────────────────────────────────────────────────────────────

#: (host, port) -> FakeRc. Populated by every FakeRc built with register=True.
registry: dict[tuple[str, int], FakeRc] = {}


def reset_registry() -> None:
    registry.clear()


def _lookup(ep: RcEndpoint) -> FakeRc:
    fake = registry.get((ep.host, ep.port))
    if fake is None:
        raise DaemonUnavailable("rc/noop", 503, {
            "error": f"no FakeRc registered on {ep.host}:{ep.port}",
            "input": {}, "path": "rc/noop", "status": 503})
    return fake


def call_blocking(ep: RcEndpoint, path: str, params: dict[str, Any] | None = None,
                  timeout_s: float = 30.0) -> dict[str, Any]:
    """Drop-in for `rc.client.call_blocking`, resolved through the registry."""
    return _lookup(ep).call_blocking(path, params, timeout_s=timeout_s)


def is_alive(ep: RcEndpoint, timeout_s: float = 1.0) -> bool:
    """Drop-in for `rc.client.is_alive`."""
    try:
        return _lookup(ep).is_alive(timeout_s)
    except RcError:
        return False
