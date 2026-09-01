"""FROZEN CONTRACT. Every hard limit and magic number lives here.

Values sourced from Microsoft's published OneDrive restrictions, from rclone
v1.75.0's own behaviour, and from empirical measurement on the target machine.
"""

from __future__ import annotations

# ── OneDrive name and path restrictions ─────────────────────────────────────
INVALID_CHARS = '"*:<>?/\\|'
RESERVED_NAMES: frozenset[str] = frozenset({
    ".lock", "CON", "PRN", "AUX", "NUL", "desktop.ini",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
})
RESERVED_PREFIXES: tuple[str, ...] = ("~$",)
RESERVED_SUBSTRINGS: tuple[str, ...] = ("_vti_",)

MAX_FILE_BYTES = 250 * 1000**3          # 250 GB
MAX_REL_PATH_CHARS = 400                # the part below the sync root
MAX_TOTAL_PATH_CHARS = 520
RECOMMENDED_ITEM_COUNT = 300_000        # per sync instance
NO_THUMBNAIL_ABOVE_BYTES = 100 * 1024**2

# ── rclone / Microsoft Graph ceilings ───────────────────────────────────────
#: Graph requires the resumable-upload chunk size to be a multiple of 320 KiB.
#: This is a hard API requirement, not an rclone preference.
ONEDRIVE_CHUNK_MULTIPLE = 327_680
MAX_TRANSFERS = 4                        # >4 reliably triggers HTTP 429
MAX_CHECKERS = 8
DEFAULT_TPSLIMIT = 8.0                   # 10/s exactly equals 3000 req / 5 min
DEFAULT_TPSLIMIT_BURST = 10              # rclone's default of 1 makes the UI jerky
DEFAULT_RETRIES = 3
DEFAULT_LOW_LEVEL_RETRIES = 10
#: Microsoft's per-user caps, for the About pane and for our own guard rails.
GRAPH_REQ_PER_5MIN = 3_000
GRAPH_INGRESS_PER_HOUR = 50 * 1000**3
GRAPH_EGRESS_PER_HOUR = 100 * 1000**3

# ── rc transport ────────────────────────────────────────────────────────────
#: 5572 and 5573 are ALREADY OCCUPIED on the target machine by the user's own
#: rclone processes. Never bind them; never assume a daemon there is ours.
RC_PORT_RANGE = range(17800, 17900)
RC_FORBIDDEN_PORTS: frozenset[int] = frozenset({5572, 5573, 53682})
RC_TIMEOUT_S = 4.0
RC_JOB_EXPIRE = "10m"                    # the 60 s default GC's job output too soon
RC_JOB_EXPIRE_INTERVAL = "30s"
OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 53682              # rclone's fixed bindPort; check it is free first

# ── ticking and pumping ─────────────────────────────────────────────────────
TICK_IDLE_MS = 2000
TICK_ACTIVE_MS = 400
TICK_PAUSED_MS = 10_000
GLIB_PUMP_MS = 50                        # LOAD-BEARING: all D-Bus rides this pump
SPINNER_FRAME_MS = 125                   # 8 frames == a 1 s rotation
STARTUP_GRACE_S = 8.0
IPC_BUDGET_MS = 20                       # Nautilus calls us on its UI thread
NAUTILUS_IPC_TIMEOUT_MS = 200

# ── hysteresis ──────────────────────────────────────────────────────────────
DEBOUNCE_SEVERE_TICKS = 1
DEBOUNCE_NORMAL_TICKS = 2
DEBOUNCE_IDLE_TICKS = 3                  # UP_TO_DATE needs 3 quiet ticks
PROCESSING_ENTRY_DELAY_MS = 250
MOUNTING_SUPPRESS_S = 15
OFFLINE_FAILURE_THRESHOLD = 3

# ── restart ladders ─────────────────────────────────────────────────────────
MOUNT_RESTART_LADDER_S = (10, 30, 120, 600)
MOUNT_RESTART_MAX_PER_HOUR = 3
RCD_RESTART_LADDER_S = (1, 2, 4, 8, 30)
RCD_MAX_FAILURES = 5
RCD_FAILURE_WINDOW_S = 300

# ── concurrency ─────────────────────────────────────────────────────────────
IO_POOL_THREADS = 4
MAX_CONCURRENT_PINS = 3                  # <= MAX_TRANSFERS
HYDRATE_BLOCK_BYTES = 4 * 1024**2        # sequential reads through FUSE, buffering=0
DB_FLUSH_MS = 100

# ── safety ──────────────────────────────────────────────────────────────────
MASS_DELETE_DEFAULT_THRESHOLD = 200
DECISION_EXPIRY_DAYS = 7                 # expiry means DO NOT DELETE
BISYNC_MAX_LOCK_MIN = 2                  # rclone's hard minimum; smaller is auto-raised
BISYNC_DEFAULT_MAX_DELETE_PCT = 25
TRASH_RETENTION_DAYS_PERSONAL = 30
TRASH_RETENTION_DAYS_BUSINESS = 93
BANDWIDTH_FLOOR_KB = 50                  # matches the Windows UI's minimum
BANDWIDTH_CEIL_KB = 100_000
AUTO_UPLOAD_PERCENT = 70                 # Microsoft pins "Adjust automatically" here
AUTO_UPLOAD_BURST_S = 60

# ── row caps ────────────────────────────────────────────────────────────────
ACTIVITY_CAP_ROWS = 5_000
ISSUE_CAP_ROWS = 5_000
ACTIVITY_UI_ROWS = 50
CACHE_SCAN_INTERVAL_S = 6 * 3600
QUOTA_TTL_S = 300
TOKEN_KEEPALIVE_S = 24 * 3600            # refresh tokens die after 90 days of non-use
VERIFY_INTERVAL_S = 7 * 24 * 3600

# ── units ───────────────────────────────────────────────────────────────────
KB = 1000                                # the OneDrive UI's unit
KIB = 1024                               # rclone's --bwlimit unit. NEVER mix them.

# ── systemd ─────────────────────────────────────────────────────────────────
UNIT_GUI = "onedriveui.service"
UNIT_RCD = "onedriveui-rcd.service"
UNIT_MOUNT_TMPL = "onedriveui-mount@{}.service"
UNIT_BISYNC_TMPL = "onedriveui-bisync-{}"
#: network-online.target DOES NOT EXIST in the systemd --user manager. After= and
#: Wants= on it are silently ignored. Never emit it.
ORDERING_GUI = ("PartOf=graphical-session.target\n"
                "After=graphical-session.target\n")
ORDERING_DAEMON = "After=graphical-session-pre.target\n"

# ── remote emulation paths ──────────────────────────────────────────────────
REMOTE_TRASH_DIR = ".onedriveui-trash"
REMOTE_VERSIONS_DIR = ".onedriveui-versions"

# ── filters ─────────────────────────────────────────────────────────────────
#: Prepended to EVERY generated filters-<account>.txt and to the mount argv, in
#: this order, before any user rule. Each entry is a literal rclone filter line.
#: `.Trash-1000/` is the file-manager trash dir created *inside* the mount: it is
#: uid-specific (1000 on this machine, matching XDG_RUNTIME_DIR=/run/user/1000)
#: and would otherwise be synced to the cloud. `*.one`/`*.onetoc2` are excluded
#: because OneNote files cannot be synced through Graph at all, and a hidden
#: OneNote file cannot even be deleted.
MANDATORY_EXCLUDES: tuple[str, ...] = (
    "- *.partial",
    "- .Trash-1000/",
    f"- {REMOTE_TRASH_DIR}/",
    f"- {REMOTE_VERSIONS_DIR}/",
    "- *.tmp",
    "- ~$*",
    "- desktop.ini",
    "- .DS_Store",
    "- *.one",
    "- *.onetoc2",
)

# ── web deep-links ──────────────────────────────────────────────────────────
WEB_ROOT = "https://onedrive.live.com/"
#: Microsoft moved this from the old `?id=recyclebin` query form to a path.
#: Reported from a live click, 2026-09-01. It matters more than most links here:
#: rclone cannot list OneDrive's recycle bin at all, so this is the ONLY route a
#: user has to the files they deleted through the file manager — a stale URL
#: makes "Recycle bin" a dead end rather than a detour.
WEB_RECYCLE_BIN = "https://onedrive.live.com/recycle"
WEB_RESTORE = "https://onedrive.live.com/RestoreYourOneDrive"
WEB_GET_MORE_STORAGE = "https://www.microsoft.com/microsoft-365/onedrive/compare-onedrive-plans"

# ── Windows 11 metrics reproduced verbatim ──────────────────────────────────
ACTIVITY_CENTER_WIDTH = 360
ACTIVITY_CENTER_HEADER_H = 64
ACTIVITY_CENTER_STORAGE_H = 56
ACTIVITY_CENTER_FOOTER_H = 48
ACTIVITY_ROW_H_2LINE = 56
ACTIVITY_ROW_H_1LINE = 48
SETTINGS_W, SETTINGS_H = 1024, 720
NAV_PANE_W, NAV_PANE_COMPACT_W = 320, 48
NAV_ITEM_H = 36
SETTINGS_CARD_MIN_H = 68
WIZARD_W, WIZARD_H = 500, 350
