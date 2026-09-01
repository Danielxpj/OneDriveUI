"""Bandwidth limiting, and the two ways of getting it wrong.

**Wrong way one: the config field.** ``_config.BwLimit`` is accepted by every rc
call, echoed back in the response, and does not throttle anything. It looks like
it works right up until someone measures it. ``core/bwlimit`` is the only
setting that actually limits a transfer, and it is process-global — which is why
this module exists as a controller rather than a parameter.

**Wrong way two: the units.** The OneDrive settings page says KB/s and means
1000 bytes. rclone's ``--bwlimit`` and ``core/bwlimit`` say KB and mean 1024.
An open-coded ratio between the two anywhere makes every user's limit 2.4 %
wrong, forever, and nobody ever notices. The conversion happens in exactly one
place — :func:`onedriveui.units.kb_to_kib` — and this module is one of its
callers, not a second implementation. A test greps the source tree to keep it
that way.

There is a third trap that only shows up in review: ``core/bwlimit`` **echoes
the rate back normalised to binary units.** Send ``1M:100k`` and the response
says ``1Mi:100Ki``. Any code that confirms a limit by string-comparing the echo
against what it sent is permanently wrong; :meth:`BandwidthController.apply`
compares the configured KB values instead.

Finally, and least obviously: **the limit has to go to both daemons.** The
control plane runs the jobs, but the mount daemon is the process that moves
bytes for every ordinary file operation. Throttling only the one whose name
contains "rcd" limits almost nothing a user does.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.bus import BUS
from onedriveui.constants import (
    AUTO_UPLOAD_BURST_S,
    AUTO_UPLOAD_PERCENT,
    BANDWIDTH_CEIL_KB,
    BANDWIDTH_FLOOR_KB,
    KB,
)
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import BandwidthState, RcEndpoint
from onedriveui.rc import ops
from onedriveui.units import format_bwlimit

log = logging.getLogger(__name__)

__all__ = ["BandwidthController", "AutoUploadController", "SAMPLE_INTERVAL_S",
           "clamp_kb"]

#: How often the automatic controller re-measures achieved throughput.
SAMPLE_INTERVAL_S: Final = 30


def clamp_kb(kb: int | None) -> int | None:
    """Hold a limit inside the range the Windows spinner allows.

    Args:
        kb: A limit in KB/s (1000), or ``None`` for unlimited.

    Returns:
        The clamped value, or ``None``.

    The floor matters more than the ceiling. Below about 50 KB/s the TLS
    handshake and the Graph metadata round trips dominate, and a large upload
    stops making progress at all rather than merely going slowly — a limit that
    means "never finish" is not a limit the UI should be able to express.
    """
    if kb is None:
        return None
    return max(BANDWIDTH_FLOOR_KB, min(int(kb), BANDWIDTH_CEIL_KB))


class BandwidthController(QObject):
    """Applies a :class:`~onedriveui.models.BandwidthState` to every daemon.

    Args:
        endpoints: ``() -> list[RcEndpoint]`` returning every daemon that should
            be throttled. A callable rather than a list because the mount's
            endpoint changes when it restarts.
        writer: Unused here; accepted so the service wiring is uniform.
        parent: Qt parent.

    Signals:
        applied: The state that was successfully applied to at least one daemon.
    """

    applied = Signal(BandwidthState)

    def __init__(
        self,
        *,
        endpoints: Any = None,
        writer: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._endpoints = endpoints or (lambda: [])
        self._writer = writer
        self._state = BandwidthState()

    # ═════════════════════════════════════════════════════════════════════════
    # Applying
    # ═════════════════════════════════════════════════════════════════════════

    def apply(self, state: BandwidthState) -> None:
        """Set the limit on **both** daemons.

        Args:
            state: The limits in KB/s (1000), as the UI spells them. Clamped to
                the 50–100 000 range before anything is sent.

        The result is never verified by comparing the echoed ``rate`` string:
        rclone normalises it to binary units, so ``"98Ki:977Ki"`` may come back
        spelled differently and a string comparison would report a failure that
        did not happen. The configured KB values are the record.
        """
        state = BandwidthState(
            download_kb=clamp_kb(state.download_kb),
            upload_kb=clamp_kb(state.upload_kb),
            upload_auto=state.upload_auto,
            auto_percent=state.auto_percent or AUTO_UPLOAD_PERCENT,
            measured_capacity_kb=state.measured_capacity_kb,
        )
        rate = format_bwlimit(state.download_kb, state.upload_kb)
        applied_to = 0
        for endpoint in self._endpoints():
            if self._send(endpoint, rate):
                applied_to += 1
        self._state = state
        if applied_to:
            log.info("bandwidth limit %s applied to %d daemon(s)", rate, applied_to)
            self.applied.emit(state)
            BUS.bandwidth_changed.emit(state)
        else:
            # Remembered anyway: the state is the user's intent, and
            # `reapply_after_restart()` exists precisely for this case.
            log.warning("no daemon accepted the bandwidth limit %s; "
                        "it will be re-applied when one comes back", rate)

    def _send(self, endpoint: RcEndpoint, rate: str) -> bool:
        try:
            ops.set_bwlimit(rate, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.warning("could not set the bandwidth limit on %s",
                        getattr(endpoint, "kind", "?"), exc_info=True)
            return False
        return True

    def set_auto(self, on: bool, percent: int = AUTO_UPLOAD_PERCENT) -> None:
        """Turn "Adjust automatically" on or off for uploads.

        Args:
            on: Whether to hand the upload limit to
                :class:`AutoUploadController`.
            percent: The share of measured throughput to use. Microsoft pins
                this at 70 % and offers no control; it is a parameter here only
                so the value has one home.
        """
        self.apply(BandwidthState(
            download_kb=self._state.download_kb,
            upload_kb=None if on else self._state.upload_kb,
            upload_auto=on,
            auto_percent=percent,
            measured_capacity_kb=self._state.measured_capacity_kb,
        ))

    def current(self) -> BandwidthState:
        """The limits currently in force, as the user expressed them."""
        return self._state

    def reapply_after_restart(self) -> None:
        """Re-send the limit to a daemon that has just come back.

        ``core/bwlimit`` is process state, not configuration: a restarted daemon
        starts unlimited. Without this, every mount restart silently removes the
        user's limit and the settings page keeps claiming it is set. Wire this
        to :data:`~onedriveui.bus.BUS.daemon_restarted`.
        """
        if self._state.download_kb is None and self._state.upload_kb is None:
            return
        log.info("re-applying the bandwidth limit after a daemon restart")
        self.apply(self._state)


class AutoUploadController(QObject):
    """"Adjust automatically": measure, take 70 %, and let go once a minute.

    The measurement problem is circular. Achieved throughput under a limit tells
    you about the limit, not about the connection, so a controller that only
    ever samples while throttled converges downward until uploads crawl — each
    period's 70 % is 70 % of the previous 70 %.

    The fix is to lift the limit for :data:`~onedriveui.constants.AUTO_UPLOAD_BURST_S`
    at the start of each period and measure *then*. It costs one unthrottled
    minute in every sampling period and it is the only way the number means
    anything.

    Args:
        controller: The :class:`BandwidthController` to drive.
        throughput: ``() -> bytes per second`` currently achieved, normally
            ``core/stats.speed`` from the mount daemon.
        percent: The share to take. Microsoft's value is 70.
        parent: Qt parent.
    """

    def __init__(
        self,
        controller: BandwidthController,
        *,
        throughput: Any = None,
        percent: int = AUTO_UPLOAD_PERCENT,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._throughput = throughput or (lambda: 0.0)
        self._percent = percent
        self._measuring = False
        self._measured_kb = 0

        self._period = QTimer(self)
        self._period.setInterval(SAMPLE_INTERVAL_S * 1000)
        self._period.timeout.connect(self._begin_measurement)
        self._burst = QTimer(self)
        self._burst.setSingleShot(True)
        self._burst.setInterval(AUTO_UPLOAD_BURST_S * 1000)
        self._burst.timeout.connect(self._end_measurement)

    def start(self) -> None:
        """Begin sampling. The first measurement runs immediately."""
        self._period.start()
        self._begin_measurement()

    def stop(self) -> None:
        self._period.stop()
        self._burst.stop()
        self._measuring = False

    @property
    def measured_kb(self) -> int:
        """The last unthrottled measurement, in KB/s."""
        return self._measured_kb

    def _begin_measurement(self) -> None:
        """Lift the limit so the next sample describes the connection."""
        if self._measuring:
            return
        self._measuring = True
        state = self._controller.current()
        self._controller.apply(BandwidthState(
            download_kb=state.download_kb, upload_kb=None,
            upload_auto=True, auto_percent=self._percent,
            measured_capacity_kb=state.measured_capacity_kb))
        self._burst.start()

    def _end_measurement(self) -> None:
        """Take 70 % of what was achieved and put the limit back."""
        self._measuring = False
        try:
            achieved_bps = float(self._throughput())
        except (TypeError, ValueError):
            achieved_bps = 0.0
        self._measured_kb = int(achieved_bps / KB)
        state = self._controller.current()
        if self._measured_kb <= 0:
            # Nothing was uploading, so nothing was learned. Leaving the limit
            # off is the honest response: throttling to a floor derived from an
            # idle connection would cap the next real upload at 50 KB/s.
            log.debug("no throughput to measure; leaving the upload limit off")
            return
        target = clamp_kb(int(self._measured_kb * self._percent / 100))
        log.info("auto upload limit: measured %d KB/s, applying %s KB/s",
                 self._measured_kb, target)
        self._controller.apply(BandwidthState(
            download_kb=state.download_kb, upload_kb=target,
            upload_auto=True, auto_percent=self._percent,
            measured_capacity_kb=self._measured_kb))
