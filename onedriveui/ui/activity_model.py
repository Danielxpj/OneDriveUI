"""The Activity Center's row model: live transfers first, then history.

Three sources feed the OneDrive activity feed and they overlap, so the merge
order and the dedupe rule are the whole design:

  * **Live** — ``core/stats.transferring[]``, re-read every tick. These rows are
    what the user is watching happen, so they sort **first**, above everything
    already finished.
  * **History** — the ``activity`` table. ``core/transferred`` holds only 100
    entries and is wiped by ``core/stats-reset``, which is why the rows are
    persisted at all.
  * **Dedupe** — by the schema's ``dedupe_key``, ``sha1(group|name|completed_at)``
    (`data/schema.sql`). A live row has no completion stamp, so its key is
    ``sha1(group|name|)`` — byte-identical to the key of the *persisted in-flight
    row for the same transfer*, which is exactly the duplicate that has to
    collapse. A completed row keeps a different key and survives, as it should.

There is a second dedupe rule that the key alone cannot express: ``group`` is
``job/<id>`` and is **renumbered when the daemon restarts**, so an in-flight row
persisted before a restart keeps a stale group and its key no longer matches the
live row that replaced it. Any persisted row still marked ``INFLIGHT`` whose
``rel_path`` is currently transferring is therefore suppressed as well.

Row payloads are built **lazily and cached**. `data()` runs for every visible row
on every scroll frame, and formatting a relative time means parsing an RFC3339
stamp — doing that eagerly for 5 000 rows would put ~40 ms into `set_history()`
for rows nobody scrolls to. The cache is dropped by :meth:`ActivityModel.refresh_times`,
which is also what re-ages "2 minutes ago" without rebuilding the model.

Nothing here paints, and nothing here words anything: the verbs come from
`strings.VERB_LABEL` through `ActivityRow`, the in-flight second line from
`strings.FILE_STATE_LABEL`, and every number from `units`. The widget kit
deliberately cannot import `units` — this module is the seam where the two meet.
"""

from __future__ import annotations

import hashlib
import posixpath
from typing import Any, Iterable, Sequence

from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QPersistentModelIndex, Qt, Slot,
)

from onedriveui import units
from onedriveui.bus import BUS
from onedriveui.constants import ACTIVITY_CAP_ROWS, ACTIVITY_UI_ROWS
from onedriveui.models import (
    ActivityEvent, ActivityState, ActivityVerb, FileState, TransferInfo,
)
from onedriveui.strings import FILE_STATE_LABEL
from onedriveui.ui import icons
from onedriveui.ui.widgets.lists import (
    PATH_ROLE, ROW_ROLE, SUBTITLE_SEPARATOR, ActivityRow,
)

__all__ = [
    "ROLE_ROW", "ROLE_PATH", "ROLE_KEY", "ROLE_LIVE", "ROLE_SOURCE",
    "ICON_FOR_SUFFIX", "GLYPH_FILE", "GLYPH_FOLDER",
    "LIVE_FILE_STATE", "LIVE_VERB",
    "dedupe_key", "key_for_event", "key_for_transfer", "icon_key_for",
    "row_for_event", "row_for_transfer", "ActivityModel",
]


# ═════════════════════════════════════════════════════════════════════════════
# Item roles. ROW_ROLE and PATH_ROLE are the widget kit's own numbers, re-exported
# rather than re-declared: two tables of role integers would be free to disagree.
# ═════════════════════════════════════════════════════════════════════════════

#: The `ActivityRow` payload the delegate paints.
ROLE_ROW: int = ROW_ROLE
#: The account-relative POSIX path of the row's item.
ROLE_PATH: int = PATH_ROLE
#: The row's dedupe key.
ROLE_KEY: int = int(Qt.ItemDataRole.UserRole) + 3
#: True while the row is a live `transferring[]` entry rather than history.
ROLE_LIVE: int = int(Qt.ItemDataRole.UserRole) + 4
#: The `TransferInfo` or `ActivityEvent` the row was built from.
ROLE_SOURCE: int = int(Qt.ItemDataRole.UserRole) + 5


# ═════════════════════════════════════════════════════════════════════════════
# Leading glyphs. Every value is a key of the frozen `icons.GLYPHS` registry.
# ═════════════════════════════════════════════════════════════════════════════

GLYPH_FILE: str = "file"
GLYPH_FOLDER: str = "folder"

#: Lower-cased file extension -> a key of `icons.GLYPHS`. OneDrive's feed shows a
#: per-type icon, and a document, a photo and an archive reading identically is
#: the fastest way for a 56 px row to look generic.
ICON_FOR_SUFFIX: dict[str, str] = {
    ".pdf": "file_pdf",
    ".txt": "file_text", ".md": "file_text", ".rtf": "file_text",
    ".doc": "file_text", ".docx": "file_text", ".odt": "file_text",
    ".xls": "file_table", ".xlsx": "file_table", ".csv": "file_table",
    ".ods": "file_table", ".tsv": "file_table",
    ".ppt": "file_slides", ".pptx": "file_slides", ".odp": "file_slides",
    ".py": "file_code", ".js": "file_code", ".ts": "file_code",
    ".json": "file_code", ".html": "file_code", ".css": "file_code",
    ".c": "file_code", ".h": "file_code", ".cpp": "file_code",
    ".rs": "file_code", ".go": "file_code", ".sh": "file_code",
    ".xml": "file_code", ".yaml": "file_code", ".yml": "file_code",
    ".toml": "file_code", ".ini": "file_code", ".sql": "file_code",
    ".zip": "file_zip", ".tar": "file_zip", ".gz": "file_zip",
    ".xz": "file_zip", ".bz2": "file_zip", ".zst": "file_zip",
    ".7z": "file_zip", ".rar": "file_zip",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".svg": "image", ".heic": "image",
    ".tif": "image", ".tiff": "image", ".avif": "image",
    ".mp4": "video", ".mkv": "video", ".mov": "video", ".avi": "video",
    ".webm": "video", ".m4v": "video", ".wmv": "video",
    ".mp3": "music", ".flac": "music", ".ogg": "music", ".wav": "music",
    ".m4a": "music", ".opus": "music", ".aac": "music",
    ".one": "notebook", ".onetoc2": "notebook",
}

#: An upload in flight and a download in flight are different per-file states,
#: and it is that state that picks both the trailing glyph and the second line's
#: label in `strings.FILE_STATE_LABEL`.
LIVE_FILE_STATE: dict[bool, FileState] = {
    True: FileState.DIRTY,       # is_upload
    False: FileState.PARTIAL,
}

#: The verb a live transfer records. `strings.VERB_LABEL` has only completed
#: wordings, so a live row's second line comes from `FILE_STATE_LABEL` instead —
#: the verb here is what the row becomes once it lands.
LIVE_VERB: dict[bool, ActivityVerb] = {
    True: ActivityVerb.UPLOADED,
    False: ActivityVerb.DOWNLOADED,
}

for _key in (GLYPH_FILE, GLYPH_FOLDER, *ICON_FOR_SUFFIX.values()):
    icons.glyph_stem(_key)                  # raises KeyError on an unknown key
del _key

_missing = [state.name for state in LIVE_FILE_STATE.values()
            if str(state) not in FILE_STATE_LABEL]
if _missing:                                            # pragma: no cover
    raise ValueError(f"activity_model: FILE_STATE_LABEL is missing {_missing}")
_bad = [key for key in ICON_FOR_SUFFIX if not key.startswith(".")]
if _bad:                                                # pragma: no cover
    raise ValueError(f"activity_model: ICON_FOR_SUFFIX keys must be suffixes: {_bad}")
del _missing, _bad


# ═════════════════════════════════════════════════════════════════════════════
# Identity
# ═════════════════════════════════════════════════════════════════════════════

def dedupe_key(group: str, name: str, completed_at: str = "") -> str:
    """The `activity.dedupe_key` of `data/schema.sql`, verbatim.

    Args:
        group: The rclone job group, e.g. ``"job/7"``. Empty for a row that
            never belonged to a job.
        name: The transfer's name — the account-relative POSIX path.
        completed_at: The RFC3339 completion stamp, or ``""`` while in flight.

    Returns:
        ``sha1(group|name|completed_at)`` as lower-case hex.
    """
    raw = f"{group}|{name}|{completed_at}".encode("utf-8")
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()


def key_for_event(event: ActivityEvent) -> str:
    """The dedupe key of a persisted row.

    The stored key wins when there is one — it is what the unique index in
    `data/schema.sql` enforces — and is otherwise derived from the same three
    fields so a row written before the key column was populated still collapses
    against its live counterpart.
    """
    if event.dedupe_key:
        return event.dedupe_key
    return dedupe_key(event.job_group, event.rel_path or event.name,
                      event.completed_at or "")


def key_for_transfer(transfer: TransferInfo) -> str:
    """The dedupe key of a live ``transferring[]`` row.

    No completion stamp, because it has not completed — which is precisely what
    makes it equal to the key of the persisted in-flight row for the same
    transfer.
    """
    return dedupe_key(transfer.group, transfer.name, "")


def icon_key_for(rel_path: str, *, is_dir: bool = False) -> str:
    """The leading glyph key for a path, from the frozen icon registry."""
    if is_dir:
        return GLYPH_FOLDER
    suffix = posixpath.splitext(rel_path)[1].lower()
    return ICON_FOR_SUFFIX.get(suffix, GLYPH_FILE)


def _basename(rel_path: str) -> str:
    """The display name of a path. Never empty for a non-empty path."""
    return rel_path.rsplit("/", 1)[-1] or rel_path


# ═════════════════════════════════════════════════════════════════════════════
# Row construction. Everything the widget kit refuses to format lands here.
# ═════════════════════════════════════════════════════════════════════════════

def row_for_transfer(transfer: TransferInfo) -> ActivityRow:
    """Build the row a live ``transferring[]`` entry paints.

    The second line joins three parts with the widget kit's own separator: the
    per-file state label from `strings.FILE_STATE_LABEL`, the rate from
    `units.human_rate` and the remaining time from `units.eta_text`.
    `strings.VERB_LABEL` is deliberately **not** used — every verb in it is past
    tense, and a transfer in flight has not happened yet. A part with nothing to
    say is dropped rather than drawn empty: rclone omits ``eta`` while a transfer
    starts and reports ``speed`` as 0 (or NaN) before the first block moves.

    Args:
        transfer: One parsed ``core/stats.transferring[]`` row.

    Returns:
        An in-flight :class:`~onedriveui.ui.widgets.lists.ActivityRow` carrying a
        determinate progress fraction.
    """
    upload = transfer.is_upload
    file_state = LIVE_FILE_STATE[upload]
    rate = units.human_rate(transfer.speed) if transfer.speed > 0 else ""
    eta = units.eta_text(transfer.eta)
    parts = [text for text in (FILE_STATE_LABEL[str(file_state)], rate, eta) if text]
    if transfer.size > 0:
        progress = min(1.0, max(0.0, transfer.bytes / transfer.size))
    else:
        progress = min(1.0, max(0.0, transfer.percentage / 100.0))
    return ActivityRow(
        name=_basename(transfer.name),
        verb=LIVE_VERB[upload],
        state=ActivityState.INFLIGHT,
        file_state=file_state,
        icon_key=icon_key_for(transfer.name),
        time_text="",
        rate_text=rate,
        eta_text=eta,
        subtitle=SUBTITLE_SEPARATOR.join(parts),
        progress=progress,
    )


def row_for_event(event: ActivityEvent) -> ActivityRow:
    """Build the row a persisted `ActivityEvent` paints.

    The relative timestamp is the completion stamp when there is one and the
    start stamp otherwise, so an interrupted row still says when it began rather
    than showing a blank second line.

    Args:
        event: One row of the ``activity`` table.

    Returns:
        An :class:`~onedriveui.ui.widgets.lists.ActivityRow`. The error copy is
        resolved by `ActivityRow.from_event` from `strings.issue_title`; the raw
        rclone error is never drawn.
    """
    return ActivityRow.from_event(
        event,
        time_text=units.relative_time(event.completed_at or event.started_at),
        icon_key=icon_key_for(event.rel_path or event.name, is_dir=event.is_dir),
    )


# ═════════════════════════════════════════════════════════════════════════════
# ActivityModel
# ═════════════════════════════════════════════════════════════════════════════

class ActivityModel(QAbstractListModel):
    """The merged activity feed: live transfers, then deduped history.

    The model owns no widgets and no colours. Its only job is ordering, dedupe,
    capping and lazy row construction, which is what lets it be driven straight
    from `FakeServices` with no engine, no database and no rclone.

    Attributes:
        account_id: Rows for another account are ignored on the bus.
        cap: The hard row ceiling, `constants.ACTIVITY_CAP_ROWS` by default —
            the same 5 000 the `trg_activity_cap` trigger enforces in SQLite.
    """

    #: The default number of history rows a caller asks the repository for.
    PAGE: int = ACTIVITY_UI_ROWS

    def __init__(self,
                 parent: QObject | None = None,
                 *,
                 account_id: str = "",
                 cap: int = ACTIVITY_CAP_ROWS) -> None:
        """
        Args:
            account_id: Restricts bus updates to one account. Empty accepts all.
            cap: Maximum total rows, live plus history.
        """
        super().__init__(parent)
        self.account_id = str(account_id)
        self.cap = max(1, int(cap))
        self._live: tuple[TransferInfo, ...] = ()
        self._live_keys: tuple[str, ...] = ()
        self._history: tuple[ActivityEvent, ...] = ()
        self._visible: tuple[ActivityEvent, ...] = ()
        self._keys: tuple[str, ...] = ()
        self._index: dict[str, int] = {}
        self._cache: list[ActivityRow | None] = []
        self._attached = False

    # ── Qt model surface ─────────────────────────────────────────────────
    def rowCount(self,
                 parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """The number of rows. A list model has none under a valid parent."""
        if parent.isValid():
            return 0
        return len(self._keys)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        """Rows are selectable and enabled; nothing here is editable."""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self,
             index: QModelIndex | QPersistentModelIndex,
             role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        """The value for one role.

        `ROLE_ROW` is the payload `ActivityDelegate` paints; `Qt.DisplayRole`
        falls back to the file name so a plain view still reads.
        """
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._keys):
            return None
        if role == ROLE_ROW:
            return self.row_at(row)
        if role == int(Qt.ItemDataRole.DisplayRole):
            return self.row_at(row).name
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return self._tooltip(row)
        if role == ROLE_KEY:
            return self._keys[row]
        if role == ROLE_PATH:
            return self.path_at(row)
        if role == ROLE_LIVE:
            return row < len(self._live)
        if role == ROLE_SOURCE:
            return self.source_at(row)
        return None

    # ── reads ────────────────────────────────────────────────────────────
    def live_count(self) -> int:
        """How many rows are live `transferring[]` entries."""
        return len(self._live)

    def history_count(self) -> int:
        """How many history rows survived the dedupe and the cap."""
        return len(self._visible)

    def keys(self) -> tuple[str, ...]:
        """Every row's dedupe key, in display order."""
        return self._keys

    def index_of(self, key: str) -> int:
        """The row a dedupe key sits at, or ``-1``."""
        return self._index.get(key, -1)

    def row_at(self, row: int) -> ActivityRow:
        """The payload for a row, built on first use and cached."""
        payload = self._cache[row]
        if payload is None:
            source = self.source_at(row)
            payload = (row_for_transfer(source)
                       if isinstance(source, TransferInfo)
                       else row_for_event(source))
            self._cache[row] = payload
        return payload

    def source_at(self, row: int) -> TransferInfo | ActivityEvent:
        """The `TransferInfo` or `ActivityEvent` behind a row."""
        if row < len(self._live):
            return self._live[row]
        return self._visible[row - len(self._live)]

    def path_at(self, row: int) -> str:
        """The account-relative POSIX path of a row's item."""
        source = self.source_at(row)
        if isinstance(source, TransferInfo):
            return source.name
        return source.rel_path or source.name

    def event_at(self, row: int) -> ActivityEvent | None:
        """The persisted event behind a row, or None for a live one."""
        source = self.source_at(row)
        return source if isinstance(source, ActivityEvent) else None

    def transfer_at(self, row: int) -> TransferInfo | None:
        """The live transfer behind a row, or None for a history one."""
        source = self.source_at(row)
        return source if isinstance(source, TransferInfo) else None

    def _tooltip(self, row: int) -> str:
        """The full path over the composed second line — the name in a 216 px
        text column is middle-elided, so the tooltip is where the rest lives."""
        payload = self.row_at(row)
        second = payload.second_line()
        path = self.path_at(row)
        return f"{path}\n{second}" if second else path

    # ── writes ───────────────────────────────────────────────────────────
    def set_live(self, transfers: Sequence[TransferInfo] | Iterable[TransferInfo]) -> None:
        """Replace the live rows from one ``core/stats`` sample.

        A tick that reports the **same files** at new percentages is the common
        case at 2.5 Hz, and it takes the cheap path: the payload cache is dropped
        for the live rows only and one `dataChanged` covers them. The model is
        reset only when the set of files actually changes, because that is also
        when the history dedupe has to be recomputed.
        """
        live = tuple(transfers)
        keys = tuple(key_for_transfer(transfer) for transfer in live)
        if keys == self._live_keys and keys:
            self._live = live
            for row in range(len(live)):
                self._cache[row] = None
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(live) - 1, 0))
            return
        self.beginResetModel()
        self._live = live
        self._live_keys = keys
        self._recompute()
        self.endResetModel()

    def set_history(self,
                    events: Sequence[ActivityEvent] | Iterable[ActivityEvent]) -> None:
        """Replace the persisted rows. Newest first, as the repository returns
        them; the model does not re-sort, so a caller's ordering is preserved."""
        self.beginResetModel()
        self._history = tuple(events)
        self._recompute()
        self.endResetModel()

    def append_event(self, event: ActivityEvent) -> None:
        """Add one newly recorded row at the top of the history block.

        A duplicate — the same dedupe key as a row already shown — updates that
        row in place instead of growing the feed, which is what makes replaying
        ``core/transferred`` after a daemon restart harmless.
        """
        if not self._accepts(event):
            return
        key = key_for_event(event)
        existing = self.index_of(key)
        if existing >= 0:
            self.update_event(event)
            return
        history = (event,) + self._history
        self._apply_history(history)

    def update_event(self, event: ActivityEvent) -> None:
        """Advance one row in place, or append it when it is not shown yet.

        A key already claimed by a **live** row is dropped: the live sample is
        newer than any persisted echo of the same transfer, and letting the
        stored row overwrite it would make the percentage go backwards.
        """
        if not self._accepts(event):
            return
        key = key_for_event(event)
        row = self.index_of(key)
        if row < 0:
            self.append_event(event)
            return
        if row < len(self._live):
            return
        position = row - len(self._live)
        self._history = tuple(
            event if key_for_event(stored) == key else stored
            for stored in self._history
        )
        self._visible = (self._visible[:position] + (event,)
                         + self._visible[position + 1:])
        self._cache[row] = None
        self.dataChanged.emit(self.index(row, 0), self.index(row, 0))

    def clear(self) -> None:
        """Drop every row, live and persisted."""
        self.beginResetModel()
        self._live = ()
        self._live_keys = ()
        self._history = ()
        self._recompute()
        self.endResetModel()

    def refresh_times(self) -> None:
        """Re-age every relative timestamp.

        "2 minutes ago" is baked into a cached payload, so a feed left open goes
        stale; this drops the cache and repaints without touching the rows.
        """
        if not self._keys:
            return
        self._cache = [None] * len(self._keys)
        self.dataChanged.emit(self.index(0, 0),
                              self.index(len(self._keys) - 1, 0))

    # ── bus ──────────────────────────────────────────────────────────────
    def attach_bus(self) -> None:
        """Follow `BUS.transfers_updated` / `activity_appended` / `activity_updated`.

        Idempotent. `BUS` is a process-wide singleton, so a model that connects
        without a matching :meth:`detach_bus` outlives its window — the pairing
        is not optional.
        """
        if self._attached:
            return
        BUS.transfers_updated.connect(self._on_transfers)
        BUS.activity_appended.connect(self._on_appended)
        BUS.activity_updated.connect(self._on_updated)
        self._attached = True

    def detach_bus(self) -> None:
        """Stop following the bus. Safe to call when never attached."""
        if not self._attached:
            return
        for signal, slot in ((BUS.transfers_updated, self._on_transfers),
                             (BUS.activity_appended, self._on_appended),
                             (BUS.activity_updated, self._on_updated)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):            # pragma: no cover
                pass
        self._attached = False

    def is_attached(self) -> bool:
        return self._attached

    @Slot(list)
    def _on_transfers(self, transfers: list) -> None:
        self.set_live([t for t in transfers if isinstance(t, TransferInfo)])

    @Slot(object)
    def _on_appended(self, event: ActivityEvent) -> None:
        self.append_event(event)

    @Slot(object)
    def _on_updated(self, event: ActivityEvent) -> None:
        self.update_event(event)

    # ── internals ────────────────────────────────────────────────────────
    def _accepts(self, event: ActivityEvent) -> bool:
        """True when an event belongs to the account this model shows."""
        if not self.account_id or not event.account_id:
            return True
        return event.account_id == self.account_id

    def _apply_history(self, history: tuple[ActivityEvent, ...]) -> None:
        """Install a new history tuple, inserting surgically when exactly one
        row appeared at the top of the history block and resetting otherwise."""
        before = self._keys
        live = len(self._live)
        visible = self._dedupe(history)
        after = self._live_keys + tuple(key_for_event(e) for e in visible)
        if (len(after) == len(before) + 1
                and after[:live] == before[:live]
                and after[live + 1:] == before[live:]):
            self.beginInsertRows(QModelIndex(), live, live)
            self._history = history
            self._install(visible, after)
            self.endInsertRows()
            return
        self.beginResetModel()
        self._history = history
        self._install(visible, after)
        self.endResetModel()

    def _recompute(self) -> None:
        """Rebuild the visible history, the key list and the payload cache."""
        visible = self._dedupe(self._history)
        keys = self._live_keys + tuple(key_for_event(e) for e in visible)
        self._install(visible, keys)

    def _install(self, visible: tuple[ActivityEvent, ...],
                 keys: tuple[str, ...]) -> None:
        self._visible = visible
        self._keys = keys
        self._index = {key: row for row, key in enumerate(keys)}
        self._cache = [None] * len(keys)

    def _dedupe(self, history: tuple[ActivityEvent, ...]) -> tuple[ActivityEvent, ...]:
        """History minus every row a live transfer already represents, capped.

        Two rules, both documented in this module's docstring: the dedupe key,
        and the in-flight-path rule that survives a ``group`` renumbering.
        """
        seen = set(self._live_keys)
        live_paths = {transfer.name for transfer in self._live}
        room = self.cap - len(self._live)
        out: list[ActivityEvent] = []
        if room <= 0:
            return ()
        for event in history:
            key = key_for_event(event)
            if key in seen:
                continue
            if (event.state is ActivityState.INFLIGHT
                    and (event.rel_path or event.name) in live_paths):
                continue
            seen.add(key)
            out.append(event)
            if len(out) >= room:
                break
        return tuple(out)
