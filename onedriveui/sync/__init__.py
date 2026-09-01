"""The sync engine: the layer between the rc transport and the UI.

Everything in here answers one of two questions.

**"What is going on?"** is answered by :mod:`~onedriveui.sync.facts` and
:mod:`~onedriveui.sync.reducer`. ``facts`` re-observes the world every tick —
the kernel, rclone, the disk and the session bus — and packs it into one
immutable :class:`~onedriveui.models.Facts`. ``reducer`` turns that into a
:class:`~onedriveui.models.SyncState` with a pure, first-match-wins priority
ladder. Nothing is remembered between ticks that is not either re-observed or
read back out of SQLite, which is what makes crash recovery exact rather than
approximate.

**"Make it so"** is answered by :mod:`~onedriveui.sync.supervisor`. It owns the
tick loop, executes the effects the reducer declares, and exposes
:meth:`~onedriveui.sync.supervisor.Supervisor.do` — the single entry point for
every user action that changes the world. UI code never calls a service
directly, so there is exactly one place where "what the user asked for" turns
into "what happened".
"""

from __future__ import annotations

__all__: list[str] = []
