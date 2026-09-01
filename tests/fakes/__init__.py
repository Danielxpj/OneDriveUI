"""The WP-00 fakes: a whole OneDriveUI world with no rclone and no network.

    from tests.fakes import FakeRc, build_fake_fs, FakeServices

Every fixture in `tests/conftest.py` is built from these, and every work package
is expected to use them rather than mocking rclone by hand — the quirks baked
into `FakeRc` and `FakeFs` are exactly the ones that break naive code.
"""

from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from tests.fakes.fake_fs import (  # noqa: E402
    FS_NAME, ORPHAN_FS_NAME, SHAPES, FakeEntry, FakeFs, build_fake_fs, extents,
    write_sparse,
)
from tests.fakes.fake_rc import (  # noqa: E402
    BANNED_PATHS, CallRecord, FakeRc, FakeRcCall, RcFault, call_blocking,
    is_alive, registry, reset_registry,
)
from tests.fakes.fake_services import (  # noqa: E402
    ACCOUNT, FakeIssueEngine, FakeNotifier, FakePauseController, FakePauseManager,
    FakePinner, FakeQuotaService, FakeServices, FakeSupervisor, facts_for,
    snapshot_for,
)

__all__ = [
    # rc
    "FakeRc", "FakeRcCall", "RcFault", "CallRecord", "BANNED_PATHS",
    "call_blocking", "is_alive", "registry", "reset_registry",
    # fs
    "FakeFs", "FakeEntry", "build_fake_fs", "extents", "write_sparse",
    "FS_NAME", "ORPHAN_FS_NAME", "SHAPES",
    # services
    "FakeServices", "FakeSupervisor", "FakePinner", "FakeIssueEngine",
    "FakeQuotaService", "FakePauseManager", "FakePauseController", "FakeNotifier",
    "ACCOUNT", "facts_for", "snapshot_for",
]
