"""Personal Vault, honestly.

Microsoft's Personal Vault is a server-side feature: a folder in OneDrive that is
locked with a second factor and served encrypted by their infrastructure. **It
cannot be opened from Linux at all.** rclone sees it as an ordinary folder it
does not have permission to read, and there is no API, no unlock flow, and no
prospect of one.

Pretending otherwise would be the worst possible thing to do here. A user who
believes they have unlocked their Personal Vault and starts putting passport
scans and tax returns in it deserves to be right about that.

So this client offers something adjacent, and labels it plainly: **a locally
encrypted folder** (gocryptfs) whose passphrase lives in the login keyring, with
the whole Windows lock/unlock/auto-lock experience around it — the 20-minute
timer, the five-minute warning, locking on screensaver, locking on quit. The
files inside it are encrypted on this machine before they ever reach OneDrive,
which is a real and useful property, and it is **not** Microsoft's Personal
Vault. :data:`CLOUD_VAULT_NOTE` says so wherever the feature appears, and the
cloud vault folder itself is shown with an explanation rather than as a folder
that mysteriously fails to open.

The warning timer has one behaviour worth stating: it fires **once** per unlock
session, not once per tick. A "your vault locks in 5 minutes" toast that reappears
every two seconds for five minutes would be the most annoying thing in the
application.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.bus import BUS
from onedriveui.models import AccountInfo, VaultState
from onedriveui.platform import secrets
from onedriveui.strings import DIALOG

log = logging.getLogger(__name__)

__all__ = ["Vault", "CLOUD_VAULT_NOTE", "AUTO_LOCK_CHOICES", "WARN_BEFORE_MIN"]

#: Shown wherever the vault appears. The distinction between "encrypted on this
#: machine by OneDriveUI" and "Microsoft's Personal Vault" is not a technicality
#: — a user who confuses them will make a security decision on a false premise.
CLOUD_VAULT_NOTE: Final = DIALOG.VAULT_CLOUD_WHY

#: The auto-lock intervals Windows offers, in minutes.
AUTO_LOCK_CHOICES: Final[tuple[int, ...]] = (20, 60, 120, 240)

#: How long before the lock the warning appears.
WARN_BEFORE_MIN: Final = 5


class Vault(QObject):
    """A locally encrypted folder with Windows' vault behaviour around it.

    Args:
        account: The account.
        container: Where the encrypted container lives. Outside the sync root by
            default — the *ciphertext* is what syncs, and a container whose
            internal state files synced too would corrupt on a two-machine race.
        mountpoint: Where the decrypted view appears while unlocked.
        auto_lock_minutes: Idle minutes before locking.
        monotonic: The clock, injected for tests.
        parent: Qt parent.

    Signals:
        state_changed: The new :class:`~onedriveui.models.VaultState`.
        warning: ``minutes_left`` — emitted **once** per unlock session.
    """

    state_changed = Signal(VaultState)
    warning = Signal(int)

    def __init__(
        self,
        account: AccountInfo,
        *,
        container: Path | None = None,
        mountpoint: Path | None = None,
        auto_lock_minutes: int = AUTO_LOCK_CHOICES[0],
        monotonic: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._container = Path(container) if container else None
        self._mountpoint = Path(mountpoint) if mountpoint else None
        self._auto_lock_minutes = auto_lock_minutes
        self._monotonic = monotonic or time.monotonic

        self._state = VaultState.ABSENT
        self._last_touch = 0.0
        self._warned = False

        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self._on_tick)

    # ═════════════════════════════════════════════════════════════════════════
    # Availability
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def available() -> bool:
        """Is gocryptfs installed?

        The feature is offered only when it can actually work. A vault toggle
        that fails on click with "gocryptfs not found" is worse than one that is
        disabled with a sentence saying what to install.
        """
        return shutil.which("gocryptfs") is not None

    @staticmethod
    def unavailable_reason() -> str:
        """Why the vault cannot be used, or ``""`` when it can."""
        if not Vault.available():
            return "gocryptfs is not installed"
        if not secrets.available():
            return secrets.unavailable_reason()
        return ""

    def cloud_vault_note(self) -> str:
        """Why Microsoft's own Personal Vault does not open here.

        Shown beside the cloud vault folder rather than letting the user
        discover it as a folder that mysteriously will not open.
        """
        return CLOUD_VAULT_NOTE

    # ═════════════════════════════════════════════════════════════════════════
    # State
    # ═════════════════════════════════════════════════════════════════════════

    def state(self) -> VaultState:
        """Absent, locked, unlocked or broken."""
        if self._container is None or not self._container.is_dir():
            return self._set_state(VaultState.ABSENT)
        if self.is_unlocked():
            return self._set_state(VaultState.UNLOCKED)
        return self._set_state(VaultState.LOCKED)

    def is_unlocked(self) -> bool:
        """Is the decrypted view mounted right now?

        Read from ``/proc/self/mounts`` rather than remembered: the mount can go
        away underneath us — a manual ``fusermount -u``, a crash of the gocryptfs
        process — and a remembered flag would leave the UI offering to lock
        something that is not open, or worse, claiming a vault is open when its
        contents are unreachable.
        """
        if self._mountpoint is None:
            return False
        target = str(self._mountpoint)
        try:
            with open("/proc/self/mounts", encoding="utf-8") as handle:
                return any(line.split()[1].replace("\\040", " ") == target
                           for line in handle if len(line.split()) > 1)
        except OSError:
            return False

    def _set_state(self, state: VaultState) -> VaultState:
        if state is not self._state:
            self._state = state
            self.state_changed.emit(state)
            BUS.vault_state_changed.emit(state)
        return state

    # ═════════════════════════════════════════════════════════════════════════
    # Setup, unlock, lock
    # ═════════════════════════════════════════════════════════════════════════

    def setup(self, passphrase: str) -> bool:
        """Create the encrypted container and store its passphrase.

        Args:
            passphrase: The user's passphrase. It is stored in the login keyring
                and **never written to disk by this client**.

        Returns:
            True when the container was created.
        """
        if self._container is None or not self.available():
            return False
        self._container.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["gocryptfs", "-init", "-q", str(self._container)],
                input=f"{passphrase}\n{passphrase}\n", text=True,
                capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            log.error("could not initialise the vault container", exc_info=True)
            self._set_state(VaultState.ERROR)
            return False

        if result.returncode != 0:
            log.error("gocryptfs -init failed: %s", result.stderr.strip()[:200])
            self._set_state(VaultState.ERROR)
            return False

        secrets.store(passphrase, self.account.id)
        self._set_state(VaultState.LOCKED)
        log.info("vault container created at %s", self._container)
        return True

    def unlock(self, passphrase: str | None = None) -> bool:
        """Mount the decrypted view.

        Args:
            passphrase: The passphrase, or ``None`` to take it from the keyring.

        Returns:
            True when it is open.
        """
        if self._container is None or self._mountpoint is None:
            return False
        if self.is_unlocked():
            self.touch()
            return True

        secret = passphrase or secrets.lookup(self.account.id)
        if not secret:
            log.warning("no vault passphrase available")
            return False

        self._mountpoint.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["gocryptfs", "-q", str(self._container), str(self._mountpoint)],
                input=f"{secret}\n", text=True, capture_output=True,
                timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            log.error("could not unlock the vault", exc_info=True)
            self._set_state(VaultState.ERROR)
            return False

        if result.returncode != 0:
            log.warning("gocryptfs refused the passphrase")
            return False

        self.touch()
        self._timer.start()
        self._set_state(VaultState.UNLOCKED)
        log.info("vault unlocked at %s", self._mountpoint)
        return True

    def lock(self) -> bool:
        """Unmount the decrypted view.

        Lazy (``-uz``), so an application still holding a file inside the vault
        cannot keep it open indefinitely. The alternative — refusing to lock
        while anything has a handle — means a vault that stays open all day
        because a file manager has a thumbnail cached.
        """
        if self._mountpoint is None or not self.is_unlocked():
            return True
        try:
            subprocess.run(["fusermount3", "-uz", str(self._mountpoint)],
                           capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            log.error("could not lock the vault", exc_info=True)
            return False
        self._timer.stop()
        self._warned = False
        self._set_state(VaultState.LOCKED)
        log.info("vault locked")
        return True

    # ═════════════════════════════════════════════════════════════════════════
    # Auto-lock
    # ═════════════════════════════════════════════════════════════════════════

    def auto_lock_minutes(self) -> int:
        return self._auto_lock_minutes

    def set_auto_lock_minutes(self, minutes: int) -> None:
        self._auto_lock_minutes = minutes if minutes in AUTO_LOCK_CHOICES \
            else AUTO_LOCK_CHOICES[0]

    def touch(self) -> None:
        """Reset the idle timer. Called on any access inside the vault."""
        self._last_touch = self._monotonic()
        self._warned = False

    def idle_minutes(self) -> float:
        return (self._monotonic() - self._last_touch) / 60.0

    def _on_tick(self) -> None:
        """Warn once, then lock.

        The ``_warned`` flag is what stops the five-minute warning reappearing
        every thirty seconds for five minutes, which would be the single most
        irritating behaviour in the application. It is cleared by
        :meth:`touch` — a user who comes back and uses the vault has earned a
        fresh warning next time.
        """
        if not self.is_unlocked():
            self._timer.stop()
            return
        idle = self.idle_minutes()
        remaining = self._auto_lock_minutes - idle
        if remaining <= 0:
            log.info("vault auto-locking after %.0f idle minutes", idle)
            self.lock()
            return
        if remaining <= WARN_BEFORE_MIN and not self._warned:
            self._warned = True
            self.warning.emit(int(remaining) or 1)
