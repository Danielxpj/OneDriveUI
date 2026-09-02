"""FROZEN CONTRACT. Every user-visible string.

Provenance tags in comments:
  [verbatim] confirmed against Microsoft docs / Group Policy strings / MC posts
  [approx]   reconstructed from screenshots or tutorials
  [ours]     Linux-specific; no Windows original exists
"""

from __future__ import annotations

from onedriveui.models import (
    IssueCode, NotificationId, RecoveryAction, SyncState, TrayIcon,
)


def t(template: str, **fmt: object) -> str:
    """Format a template. Missing keys render as the literal placeholder rather
    than raising, so a wording bug never crashes the UI."""
    try:
        return template.format(**fmt)
    except (KeyError, IndexError):
        return template


# ─────────────────────────────────────────────────────────────────────────────
# Status lines — the single source for the tray tooltip, the Activity Center
# headline, and the Settings badge. Rendering these three from one table is what
# makes them structurally unable to disagree.
# ─────────────────────────────────────────────────────────────────────────────

STATUS_LINE: dict[SyncState, str] = {
    SyncState.UP_TO_DATE:      "Your files are synced",              # [verbatim]
    SyncState.INFO_NOTICE:     "Your files are synced",              # [verbatim]
    SyncState.SYNCING:         "Syncing {n} files",                  # [verbatim]
    SyncState.PROCESSING:      "Processing changes",                 # [verbatim]
    SyncState.MOUNTING:        "Processing changes",                 # [verbatim]
    SyncState.INITIALIZING:    "Starting OneDrive…",            # [approx]
    SyncState.PAUSED_MANUAL:   "Sync is paused",                     # [verbatim]
    SyncState.PAUSED_METERED:  "Sync is paused",                     # [verbatim]
    SyncState.PAUSED_BATTERY:  "Sync is paused",                     # [verbatim]
    SyncState.PAUSED_QUOTA:    "Your OneDrive is full",              # [verbatim]
    SyncState.SIGNED_OUT:      "You're not signed in",               # [verbatim]
    SyncState.AUTH_REQUIRED:   "Sign in required",                   # [verbatim]
    SyncState.ACCOUNT_BLOCKED: "Your account is blocked",            # [approx]
    SyncState.WARNING:         "Sync issues",                        # [verbatim]
    SyncState.ERROR:           "Action needed",                      # [verbatim]
    SyncState.NEEDS_ATTENTION: "Action needed",                      # [verbatim]
    SyncState.OFFLINE:         "OneDrive isn't connected",           # [approx]
    SyncState.NOT_RUNNING:     "OneDrive isn't running",             # [approx]
}

STATUS_SUB: dict[SyncState, str] = {
    SyncState.SYNCING:         "Uploading {done} of {total} ({bytes} of {size})",  # [approx]
    SyncState.PAUSED_MANUAL:   "Syncing will resume in {hh}h {mm}m",               # [verbatim]
    SyncState.PAUSED_METERED:  "This PC is on a metered network",                  # [approx]
    SyncState.PAUSED_BATTERY:  "This PC is in battery saver mode",                 # [approx]
    SyncState.WARNING:         "{n} files couldn't be synced",                     # [approx]
    SyncState.PAUSED_QUOTA:    "You need more storage to keep syncing",            # [approx]
    SyncState.OFFLINE:         "Your files will sync when you're back online",     # [approx]
    SyncState.UP_TO_DATE:      "{used} of {total} used",                           # [approx]
}

#: Shown while an account performs its very first scan. [verbatim]
FIRST_SYNC_BANNER = (
    "We're checking all your files to make sure they are up to date on this "
    "personal computer. This might take a while if you have a lot of files."
)

# ─────────────────────────────────────────────────────────────────────────────
# Tray menu. The DBusMenu is LABEL-ONLY: QWidgetAction is exported as an empty
# label, so no progress bars or custom widgets may appear here.
# ─────────────────────────────────────────────────────────────────────────────

class MENU:
    OPEN_ACTIVITY   = "Open Activity Center"        # [ours] — first and default item
    OPEN_FOLDER     = "Open your OneDrive folder"   # [verbatim]
    VIEW_ONLINE     = "View online"                 # [verbatim]
    SYNC_PROBLEMS   = "View sync problems ({n})"    # [verbatim]
    PAUSE           = "Pause syncing"               # [verbatim]
    PAUSE_2H        = "2 hours"                     # [verbatim]
    PAUSE_8H        = "8 hours"                     # [verbatim]
    PAUSE_24H       = "24 hours"                    # [verbatim]
    PAUSE_UNTIL     = "Until I resume"              # [ours]
    RESUME          = "Resume syncing"              # [verbatim]
    SETTINGS        = "Settings"                    # [verbatim]
    HELP            = "Help & Settings"             # [verbatim]
    QUIT            = "Quit OneDrive"               # [verbatim]
    RECYCLE_BIN     = "Recycle bin"                 # [verbatim] (MC333940)
    LOCK_VAULT      = "Lock Personal Vault"         # [verbatim]
    UNLOCK_VAULT    = "Unlock Personal Vault"       # [verbatim]


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

class SETTINGS:
    NAV_SYNC          = "Sync and back up"                     # [verbatim]
    NAV_ACCOUNT       = "Account"                              # [verbatim]
    NAV_NOTIFICATIONS = "Notifications"                        # [verbatim]
    NAV_ABOUT         = "About"                                # [verbatim]
    #: [ours] Not a Microsoft section — Microsoft has no rclone to configure.
    #: This client is a control surface for rclone, and until this page existed
    #: not one of the twenty-eight mount parameters it writes could be changed
    #: without hand-editing config.json.
    NAV_RCLONE        = "rclone engine"                        # [ours]

    MANAGE_BACKUP     = "Manage folder backup"                 # [verbatim]
    BACKUP_DESC       = "Back up your important PC folders to OneDrive"          # [approx]
    START_AT_SIGNIN   = "Start OneDrive when I sign in to this device"           # [verbatim]
    #: The shipped Windows string contains a grammatical error ("is in on").
    #: Microsoft's own policy documentation uses the corrected form. We ship the
    #: corrected wording and note the discrepancy here rather than cloning a typo.
    PAUSE_METERED     = "Pause syncing when this device is on a metered network" # [verbatim, corrected]
    PAUSE_BATTERY     = "Pause syncing when this device is in battery saver mode"# [verbatim]
    SCREENSHOTS       = "Save screenshots I capture to OneDrive"                 # [verbatim]
    CAMERA_IMPORT     = "Save photos and videos from devices to OneDrive"        # [verbatim]

    ADVANCED          = "Advanced settings"                    # [verbatim]

    # ── the rclone engine page ───────────────────────────────────────────────
    # All [ours]: these name rclone's own flags, so they deliberately read like
    # rclone's documentation rather than like Microsoft's UI. A user tuning a
    # mount is a user who will search rclone's manual for these words.
    RC_INTRO          = "These settings become the rclone command line. Changes marked below need the mount to restart before they take effect."
    RC_ENGINE         = "rclone binary"
    RC_ENGINE_DESC    = "Which rclone this client drives"
    RC_CACHE          = "VFS cache"
    RC_CACHE_DESC     = "How much of your drive is kept on this device"
    RC_CACHE_DIR      = "Cache directory"
    RC_CACHE_MAX_SIZE = "Maximum cache size (GB)"
    RC_CACHE_MAX_AGE  = "Discard cached files unused for (hours)"
    RC_CACHE_MIN_FREE = "Always leave free on disk (GB)"
    RC_WRITE_BACK     = "Upload a changed file after (seconds)"
    RC_FRESHNESS      = "Freshness"
    RC_FRESHNESS_DESC = "How quickly a change made elsewhere shows up here"
    RC_DIR_CACHE      = "Remember directory listings for (seconds)"
    RC_POLL           = "Check the cloud for changes every (seconds)"
    RC_ATTR_TIMEOUT   = "Cache file attributes for (milliseconds)"
    RC_TRANSFERS      = "Transfers"
    RC_TRANSFERS_DESC = "How hard this client pushes the network and the API"
    RC_N_TRANSFERS    = "Parallel transfers"
    RC_N_CHECKERS     = "Parallel checkers"
    RC_TPSLIMIT       = "API requests per second"
    RC_TPS_BURST      = "Request burst allowance"
    RC_RETRIES        = "Retries"
    RC_LOW_RETRIES    = "Low-level retries"
    RC_READS          = "Reads"
    RC_READS_DESC     = "How a file is fetched when you open it"
    RC_CHUNK          = "Read chunk size (MB)"
    RC_CHUNK_LIMIT    = "Read chunk size limit (MB)"
    RC_FILES          = "Files and permissions"
    RC_FILES_DESC     = "How the mounted files appear on this device"
    RC_UMASK          = "umask"
    RC_FILE_PERMS     = "File permissions"
    RC_DIR_PERMS      = "Directory permissions"
    RC_ALLOW_OTHER    = "Let other users on this machine read the mount"
    RC_LINKS          = "Translate symbolic links"
    RC_FAST_FINGER    = "Fast fingerprint (skip hashing to detect changes)"
    RC_BACKEND        = "Backend options"
    RC_BACKEND_DESC   = "Written into rclone.conf, never onto the command line — a backend flag on the command line renames the filesystem and orphans the cache."
    RC_CHUNK_UPLOAD   = "Upload chunk size"
    RC_BACKEND_APPLY  = "Write to rclone.conf"
    RC_BACKEND_OK     = "Written to rclone.conf."

    RC_EXTRA          = "Extra rclone arguments"
    RC_EXTRA_DESC     = "Passed through verbatim, one per line. Backend flags are refused: they change the filesystem name and orphan the cache."
    RC_LOG            = "Engine log"
    RC_LOG_DESC       = "What rclone and this client are doing, live"
    RC_LOG_OPEN       = "Open the full log"

    RC_COMMAND        = "The command this produces"
    RC_COMMAND_DESC   = "Exactly what will be run when the mount next starts"
    RC_APPLY          = "Restart the mount to apply"
    RC_PENDING        = "Saved. The mount is still running with the previous settings."
    RC_RESTARTING     = "Restarting the mount…"
    FILE_COLLAB       = "File collaboration"                   # [verbatim]
    COLLAB_ASK        = "Let me choose to merge changes or keep both copies"     # [verbatim]
    COLLAB_KEEP_BOTH  = "Always keep both copies (rename the copy on this computer)"  # [verbatim]

    BANDWIDTH         = "Bandwidth"                            # [verbatim]
    LIMIT_DOWNLOAD    = "Limit download rate"                  # [verbatim]
    LIMIT_UPLOAD      = "Limit upload rate"                    # [verbatim]
    LIMIT_TO          = "Limit to"                             # [verbatim]
    ADJUST_AUTO       = "Adjust automatically"                 # [verbatim]
    KB_PER_SEC        = "KB/s"                                 # [verbatim] — 1000, not 1024
    #: rclone's core/bwlimit is in KiB/s. units.kb_to_kib() converts in exactly
    #: one place; the UI never shows KiB.
    BANDWIDTH_GLOBAL_NOTE = "Bandwidth limits apply to all accounts on this device."  # [ours]

    FOD               = "Files On-Demand"                      # [verbatim]
    FREE_UP_SPACE     = "Free up disk space"                   # [verbatim]
    DOWNLOAD_ALL      = "Download all files"                   # [verbatim]
    FOD_DESC          = ("Save space by keeping files online-only until you open them, "
                         "or download everything to this device.")               # [approx]

    EXCLUDED_EXT      = "Excluded file extensions"             # [verbatim]
    CHOOSE_FOLDERS    = "Choose folders"                       # [verbatim]
    UNLINK            = "Unlink this PC"                       # [verbatim]
    ADD_ACCOUNT       = "Add an account"                       # [verbatim]
    GET_MORE_STORAGE  = "Get more storage"                     # [verbatim]
    VAULT_TIMEOUT     = "Lock Personal Vault after"            # [approx]

    # Notifications tab — all five default ON. [verbatim]
    N_PAUSED          = "Notify me when syncing is paused"
    N_SHARED          = "Notify me when others share with me or edit my shared items"
    N_MASS_DELETE     = "Notify me when many files are deleted in the cloud"
    N_MEMORIES        = "Notify me when this day in history memories are available"
    N_OTHER_ACCOUNTS  = "Notify me to load files from my other accounts to this PC"
    N_SYNC_ISSUES     = "Notify me when files can't be synced"                   # [ours]
    N_CONFLICTS       = "Notify me when there's a conflict"                      # [ours]


# ─────────────────────────────────────────────────────────────────────────────
# Dialogs
# ─────────────────────────────────────────────────────────────────────────────

class DIALOG:
    CONTINUE   = "Continue"          # [verbatim]
    OK         = "OK"                # [verbatim]
    CANCEL     = "Cancel"            # [verbatim]
    CLOSE      = "Close"             # [verbatim]
    SAVE       = "Save changes"      # [verbatim]

    FIRST_DELETE_TITLE = "Deleted files are removed everywhere"                  # [verbatim]
    FIRST_DELETE_BODY  = ("You deleted {name} from your OneDrive folder. It will be "
                          "deleted everywhere you're signed in to OneDrive.")    # [approx]
    FIRST_DELETE_OPT   = "Don't show this reminder again"                        # [verbatim]

    MASS_DELETE_TITLE  = "Delete these {n} items?"                               # [verbatim]
    MASS_DELETE_BODY   = ("{n} items were deleted from your OneDrive. If this wasn't "
                          "you, you can restore them.")                          # [approx]
    MASS_DELETE_YES    = "Delete them"                                           # [verbatim]
    MASS_DELETE_NO     = "Restore files"                                         # [verbatim]
    MASS_DELETE_ALWAYS = "Always remove files"                                   # [verbatim]
    #: Governed by the ForcedLocalMassDeleteDetection policy. [verbatim]
    MASS_DELETE_TIMEOUT = ("If you don't confirm this within seven days, the files "
                           "aren't deleted.")

    UNLINK_TITLE = "Unlink account on this PC?"                                  # [verbatim]
    UNLINK_BODY  = ("Any files you have marked as always keep on this device will stay "
                    "on this PC. You will not be able to see other OneDrive files on "
                    "this PC.")                                                  # [verbatim]

    CHOOSE_FOLDERS_ALL  = "Make all files available"                             # [verbatim]
    CHOOSE_FOLDERS_WARN = ("Unchecked folders will be removed from this computer. "
                           "They will still be available online.")               # [approx]

    STOP_BACKUP_TITLE   = "Stop backing up this folder?"                         # [approx]
    STOP_BACKUP_DESKTOP = "This computer only"                                   # [verbatim]
    WHERE_ARE_MY_FILES  = "Where are my files"                                   # [verbatim]

    FREE_UP_TITLE  = "Free up disk space?"                                       # [approx]
    FREE_UP_BODY   = ("Your files will stay in OneDrive and will download when you "
                      "open them. This frees {size} on this PC.")                # [approx]
    DOWNLOAD_ALL_TITLE = "Download all files?"                                   # [approx]
    DOWNLOAD_ALL_BODY  = ("This will use {size} on this PC. You can free up space "
                          "again at any time.")                                  # [approx]

    RESYNC_TITLE = "Reset sync?"                                                 # [ours]
    RESYNC_BODY  = ("Resetting only copies files — it never deletes them. Files you "
                    "deleted while sync was broken may come back, and renamed files "
                    "may appear twice.")                                         # [ours]

    RESET_TITLE = "Reset OneDriveUI?"                                            # [ours]
    RESET_BODY  = ("This clears the local cache and index. Every file on this PC and "
                   "in OneDrive is kept, and so are your settings and folder choices.")  # [ours]

    QUIT_TITLE = "Close OneDrive?"                                               # [verbatim]
    QUIT_BODY  = ("If you close OneDrive, your files will not be kept in sync until you "
                  "open it again.")                                              # [approx]

    #: Shown wherever a control is present but cannot work on Linux.
    UNAVAILABLE_PREFIX = "Not available on Linux: "                              # [ours]
    #: The label on the control that is always DISABLED. It is named here rather
    #: than in the Share dialog because a disabled control still has to say what
    #: it would have done — and because a user-facing literal outside this file
    #: is the one thing the contract checklist forbids outright.
    REMOVE_LINK = "Remove link"                                                  # [verbatim]
    REMOVE_LINK_WHY = ("rclone cannot revoke a OneDrive sharing link. Use the OneDrive "
                       "website to stop sharing.")                               # [ours]
    VERSION_HISTORY_WHY = ("Version history is stored by OneDrive and can only be "
                           "browsed on the web. OneDriveUI keeps its own snapshots of "
                           "files it replaced.")                                 # [ours]
    RECYCLE_BIN_WHY = ("OneDrive's recycle bin can only be browsed on the web. Files "
                       "you delete in OneDriveUI can be restored here.")         # [ours]
    VAULT_CLOUD_WHY = ("Your cloud Personal Vault can't be opened from Linux. This vault "
                       "encrypts files on this device only.")                    # [ours]
    DU_ON_MOUNT_NOTE = ("Disk-usage tools report the full size of online-only files. "
                        "OneDriveUI's own figures are accurate.")                # [ours]


# ─────────────────────────────────────────────────────────────────────────────
# OOBE — 7 pages, ~500x350
# ─────────────────────────────────────────────────────────────────────────────

class OOBE:
    WELCOME_TITLE  = "Set up OneDrive"                                           # [verbatim]
    WELCOME_BODY   = "Put your files in OneDrive to get them from any device."   # [verbatim]
    SIGNIN_BTN     = "Sign in"                                                   # [verbatim]
    SIGNIN_BROWSER = "Finish signing in in your browser, then come back here."   # [ours]
    FOLDER_TITLE   = "Your OneDrive folder"                                      # [verbatim]
    FOLDER_BODY    = "Add files here so you can get to them from any device."    # [verbatim]
    CHANGE_LOC     = "Change location"                                           # [verbatim]
    USE_LOC        = "Use this location"                                         # [verbatim]
    BACKUP_TITLE   = "Back up folders on this PC"                                # [verbatim]
    DELETE_TITLE   = "Deleting files removes them everywhere"                    # [verbatim]
    TUTORIAL_TITLE = "Get to know your OneDrive"                                 # [verbatim]
    DONE_TITLE     = "Your OneDrive is ready for you"                            # [verbatim]
    OPEN_FOLDER    = "Open my OneDrive folder"                                   # [verbatim]
    NEXT           = "Next"                                                      # [verbatim]
    NOT_NOW        = "Not now"                                                   # [verbatim]

    # ── the remaining page bodies and buttons. Windows screens 2, 3 and 8
    #    (the Microsoft sign-in page, two-step verification and the mobile-app
    #    promo) happen inside the browser or do not apply, so the 9 Windows
    #    screens collapse to the 7 pages named in PAGES below.
    WELCOME_EMAIL   = "Email address"                                            # [verbatim]
    CREATE_ACCOUNT  = "Create account"                                           # [verbatim]
    SIGNIN_WAITING  = "Waiting for you to finish signing in…"                     # [ours]
    FOLDER_LOCATION = "Your OneDrive folder will be here:"                       # [approx]
    FOLDER_EXISTS   = ("This folder already exists. Its contents will be merged "
                       "with your OneDrive.")                                    # [approx]
    BACKUP_BODY     = ("Selected folders are backed up to OneDrive and stay on "
                       "this PC.")                                               # [approx]
    BACKUP_START    = "Start syncing"                                            # [verbatim]
    BACKUP_LATER    = "I'll do it later"                                         # [verbatim]
    DELETE_BODY     = ("When you delete a file from your OneDrive folder it is "
                       "deleted everywhere you're signed in to OneDrive.")       # [approx]
    TUTORIAL_1      = "All your files are here, and on every device you sign in to."   # [approx]
    TUTORIAL_2      = "Share files and folders with anyone, without attaching them."   # [approx]
    TUTORIAL_3      = ("A cloud means the file is online-only. A green check means "
                       "it's on this device.")                                   # [approx]
    TUTORIAL_4      = "Get the OneDrive app on your phone to take your files with you."  # [approx]
    DONE_BODY       = ("Your files sync in the background. The OneDrive icon in "
                       "the system tray shows what's happening.")                # [ours]
    BACK            = "Back"                                                     # [verbatim]
    LATER           = "Later"                                                    # [verbatim]

    #: The 7 wizard pages in order; ui/wizard.py::PAGES renders exactly these.
    PAGES: tuple[str, ...] = (
        "welcome", "signin", "folder", "backup", "delete", "tutorial", "done",
    )
    TUTORIAL_SLIDES: tuple[str, ...] = (TUTORIAL_1, TUTORIAL_2, TUTORIAL_3, TUTORIAL_4)


# ─────────────────────────────────────────────────────────────────────────────
# Issues
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_TITLE: dict[IssueCode, str] = {
    IssueCode.NAME_INVALID:        "The file name contains characters that aren't allowed",
    IssueCode.RESERVED_NAME:       "This file name is reserved and can't be uploaded",
    IssueCode.PATH_TOO_LONG:       "The file path is too long",
    IssueCode.FILE_TOO_LARGE:      "This file is larger than OneDrive allows",
    IssueCode.QUOTA_EXCEEDED:      "Your OneDrive is full",
    IssueCode.DISK_FULL:           "There isn't enough space on this PC",
    IssueCode.AUTH_EXPIRED:        "Sign in required",
    IssueCode.AUTH_MFA:            "You need to verify your identity",
    IssueCode.AUTH_TENANT_BLOCKED: "Your organisation must claim this domain",
    IssueCode.THROTTLED:           "OneDrive is busy — retrying",
    IssueCode.NETWORK_UNREACHABLE: "OneDrive isn't connected",
    IssueCode.MALWARE_DETECTED:    "This file was blocked because it may be unsafe",
    IssueCode.FILE_IN_USE:         "The file is open in another program",
    IssueCode.PERMISSION_LOST:     "You no longer have permission to this item",
    IssueCode.CONFLICT:            "Two people edited this file",
    IssueCode.CASE_COLLISION:      "A file with the same name already exists",
    IssueCode.MASS_DELETE_BLOCKED: "Delete these {n} items?",
    IssueCode.ALL_FILES_CHANGED:   "Everything looks different — is this the right folder?",
    IssueCode.CHECK_ACCESS_FAILED: "OneDriveUI couldn't verify both sides",
    IssueCode.NEEDS_RESYNC:        "Sync needs to be reset",
    IssueCode.BISYNC_LOCK_STUCK:   "A previous sync didn't finish",
    IssueCode.BISYNC_CRITICAL:     "Sync stopped and needs attention",
    IssueCode.MOUNT_DEAD:          "Your OneDrive folder isn't available",
    IssueCode.ORPHANED_CACHE:      "Old cached files are using {size}",
    IssueCode.PARTIAL_FILE_FOUND:  "A previous transfer was interrupted",
    IssueCode.ONENOTE_HIDDEN:      "OneNote notebooks aren't synced",
    IssueCode.VAULT_INACCESSIBLE:  "Your cloud Personal Vault can't be opened from Linux",
    IssueCode.UPLOAD_FAILED:       "Couldn't upload this file",
    IssueCode.DOWNLOAD_FAILED:     "Couldn't download this file",
    IssueCode.UNKNOWN:             "This file couldn't be synced",
}

ACTION_LABEL: dict[RecoveryAction, str] = {
    RecoveryAction.RETRY:               "Try again",
    RecoveryAction.RENAME:              "Rename",
    RecoveryAction.SKIP:                "Ignore",
    RecoveryAction.KEEP_BOTH:           "Keep both",
    RecoveryAction.KEEP_LOCAL:          "Keep the version on this PC",
    RecoveryAction.KEEP_CLOUD:          "Keep the version in OneDrive",
    RecoveryAction.SIGN_IN:             "Sign in",
    # PIN and UNPIN joined `RecoveryAction` in f58e05a without reaching this
    # table, so `action_label()` raised KeyError for both. "Always keep on this
    # device" is verbatim from Windows and matches the Nautilus submenu
    # (`ext/nautilus_onedriveui.py`) word for word, which is the point: the same
    # action must not be named two different things in two menus.
    RecoveryAction.PIN:                 "Always keep on this device",
    RecoveryAction.UNPIN:               "Don't keep on this device",
    RecoveryAction.FREE_UP_SPACE:       "Free up space",
    RecoveryAction.GET_MORE_STORAGE:    "Get more storage",
    RecoveryAction.RESYNC:              "Reset sync",
    RecoveryAction.FORCE_DELETE:        "Delete them",
    RecoveryAction.RESTORE_FROM_BACKUP: "Restore files",
    RecoveryAction.UNLOCK_BISYNC:       "Clear and retry",
    RecoveryAction.RESTART_MOUNT:       "Reconnect",
    RecoveryAction.RECLAIM_CACHE:       "Reclaim space",
    RecoveryAction.OPEN_WEB:            "View online",
    RecoveryAction.SHOW_IN_FOLDER:      "Show in folder",
    RecoveryAction.STOP_SYNCING_ITEM:   "Stop syncing this item",
}


# ─────────────────────────────────────────────────────────────────────────────
# Toasts. GNOME renders about 3 action buttons; NotifySpec caps at 2.
# ─────────────────────────────────────────────────────────────────────────────

TOAST: dict[NotificationId, tuple[str, str, tuple[tuple[str, str], ...]]] = {
    NotificationId.SYNC_PAUSED_METERED: (
        "Sync is paused",
        "This PC is on a metered network.",
        (("sync_anyway", "Sync Anyway"),)),                                      # [verbatim]
    NotificationId.SYNC_PAUSED_BATTERY: (
        "Sync is paused",
        "This PC is in battery saver mode.",
        (("sync_anyway", "Sync Anyway"),)),                                      # [verbatim]
    NotificationId.SYNC_PAUSED_MANUAL: (
        "Sync is paused", "Your files are not currently syncing.", ()),          # [verbatim]
    NotificationId.SYNC_ISSUES: (
        "Sync issues",
        "{n} files couldn't be synced.",
        (("view", "View sync problems"),)),                                      # [verbatim]
    NotificationId.MASS_DELETE: (
        "Did you delete {n} files?",
        "{n} files were deleted from your OneDrive.",
        (("restore", "Restore files"), ("delete", "Delete them"))),              # [verbatim]
    NotificationId.QUOTA_FULL: (
        "Your OneDrive is full",
        "You need more storage to keep syncing.",
        (("storage", "Get more storage"),)),                                     # [verbatim]
    NotificationId.QUOTA_WARNING: (
        "You're running out of storage", "{pct}% of your OneDrive is used.",
        (("storage", "Get more storage"),)),                                     # [approx]
    NotificationId.SIGN_IN_REQUIRED: (
        "Sign in required", "OneDrive needs you to sign in again.",
        (("signin", "Sign in"),)),                                               # [verbatim]
    NotificationId.VAULT_WARNING: (
        "Still Using Your Personal Vault?",
        "Your Personal Vault will lock in 5 minutes.",
        (("lock", "Lock Personal Vault"),)),                                     # [verbatim]
    NotificationId.VAULT_LOCKED: (
        "Personal Vault Locked", "Your Personal Vault is now locked.", ()),      # [verbatim]
    NotificationId.CONFLICT_DETECTED: (
        "Two people edited this file",
        "A copy of {name} was saved as {loser}.",
        (("view", "Show in folder"),)),                                          # [approx]
    NotificationId.MOUNT_LOST: (
        "Your OneDrive folder isn't available",
        "OneDriveUI is reconnecting…", ()),                                 # [ours]
    NotificationId.MOUNT_RESTORED: (
        "OneDrive reconnected", "Your files are available again.", ()),          # [ours]
    NotificationId.ENGINE_DEAD: (
        "OneDrive stopped working",
        "OneDriveUI couldn't restart the sync engine.",
        (("report", "Report a problem"),)),                                      # [ours]
    NotificationId.NEEDS_RESYNC: (
        "Sync needs to be reset",
        "OneDriveUI can't continue until sync is reset.",
        (("resync", "Reset sync"),)),                                            # [ours]
    NotificationId.FILE_BLOCKED: (
        "A file was blocked", "{name} may be unsafe and wasn't synced.",
        (("web", "View online"),)),                                              # [approx]
    NotificationId.NAME_INVALID: (
        "Some names can't be synced",
        "{n} files have names that prevent syncing.",
        (("view", "View sync problems"),)),                                      # [verbatim]
    NotificationId.SHARED_WITH_ME: (
        "{who} shared {name} with you", "", (("open", "Open"),)),                # [approx]
    NotificationId.SHARED_ITEM_EDITED: (
        "{who} edited {name}", "", (("open", "Open"),)),                         # [approx]
    NotificationId.DOWNLOAD_ALL_DONE: (
        "All your files are on this PC", "{size} downloaded.", ()),              # [approx]
    NotificationId.FREE_UP_SPACE_DONE: (
        "Space freed", "{size} is now available on this PC.", ()),               # [approx]
    NotificationId.BACKUP_COMPLETE: (
        "Your folders are backed up", "{folders} are now in OneDrive.", ()),     # [approx]
    NotificationId.LOW_DISK: (
        "This PC is running out of space", "{size} free on this device.",
        (("free", "Free up space"),)),                                           # [approx]
    NotificationId.SYNC_COMPLETE: (
        "Your files are synced", "", ()),                                        # [verbatim]
    NotificationId.SYNC_RESUMED: (
        "Sync resumed", "", ()),                                                 # [approx]
    NotificationId.ACCOUNT_BLOCKED: (
        "Your account is blocked", "Contact your administrator.",
        (("web", "View online"),)),                                              # [approx]
    NotificationId.FIRST_DELETE: (
        "Deleted files are removed everywhere",
        "{name} will be deleted everywhere you're signed in.", ()),              # [verbatim]
    NotificationId.MEMORIES: (
        "On this day", "You have memories from {year}.", ()),                    # [approx]
    NotificationId.OTHER_ACCOUNTS: (
        "Load files from your other accounts",
        "You're signed in to another OneDrive account.", ()),                    # [approx]
}


# ─────────────────────────────────────────────────────────────────────────────
# Activity feed verbs
# ─────────────────────────────────────────────────────────────────────────────

VERB_LABEL: dict[str, str] = {
    "uploaded": "Uploaded", "downloaded": "Downloaded", "modified": "Modified",
    "created": "Created",   "deleted": "Deleted",       "renamed": "Renamed",
    "moved": "Moved",       "shared": "Shared",         "restored": "Restored",
    "pinned": "Always kept on this device",
    "freed": "Freed up space",
}

FILE_STATE_LABEL: dict[str, str] = {
    "online_only": "Available when online",   # [verbatim] — the Explorer Status column
    "local":       "Available on this device",# [verbatim]
    "pinned":      "Always available on this device",  # [verbatim]
    "partial":     "Downloading…",
    "dirty":       "Uploading…",
    "syncing":     "Syncing…",
    "excluded":    "Not syncing",
    "error":       "Sync problem",
    "unknown":     "",
}


# ─────────────────────────────────────────────────────────────────────────────
# Tray icon per state. The mapping lives here — beside STATUS_LINE, which the
# same tray tooltip renders — so that a state can never gain a headline without
# also gaining an icon. `ui/icons.py` re-exports this name rather than declaring
# a second copy: two tables would be free to disagree.
# ─────────────────────────────────────────────────────────────────────────────

TRAY_FOR_STATE: dict[SyncState, TrayIcon] = {
    SyncState.UP_TO_DATE:      TrayIcon.SYNCED,
    SyncState.INFO_NOTICE:     TrayIcon.INFO,
    SyncState.SYNCING:         TrayIcon.SYNCING,
    SyncState.PROCESSING:      TrayIcon.SYNCING,
    SyncState.MOUNTING:        TrayIcon.SYNCING,
    SyncState.INITIALIZING:    TrayIcon.SYNCING,
    SyncState.PAUSED_MANUAL:   TrayIcon.PAUSED,
    SyncState.PAUSED_METERED:  TrayIcon.PAUSED,
    SyncState.PAUSED_BATTERY:  TrayIcon.PAUSED,
    SyncState.PAUSED_QUOTA:    TrayIcon.WARNING,
    SyncState.SIGNED_OUT:      TrayIcon.SIGNED_OUT,
    SyncState.AUTH_REQUIRED:   TrayIcon.BLOCKED,
    SyncState.ACCOUNT_BLOCKED: TrayIcon.BLOCKED,
    SyncState.ERROR:           TrayIcon.ERROR,
    SyncState.WARNING:         TrayIcon.WARNING,
    SyncState.NEEDS_ATTENTION: TrayIcon.INFO,
    SyncState.OFFLINE:         TrayIcon.INFO,
    SyncState.NOT_RUNNING:     TrayIcon.NONE,
}


# ─────────────────────────────────────────────────────────────────────────────
# One namespace over every table above, so a consumer can `from onedriveui.strings
# import S` and reach everything as `S.MENU.PAUSE` / `S.STATUS_LINE[state]`.
# Purely an alias surface: it declares no string of its own.
# ─────────────────────────────────────────────────────────────────────────────

class S:
    """Every user-visible string, in one namespace."""

    t                 = staticmethod(t)

    STATUS_LINE       = STATUS_LINE
    STATUS_SUB        = STATUS_SUB
    FIRST_SYNC_BANNER = FIRST_SYNC_BANNER
    MENU              = MENU
    SETTINGS          = SETTINGS
    DIALOG            = DIALOG
    OOBE              = OOBE
    ISSUE_TITLE       = ISSUE_TITLE
    ACTION_LABEL      = ACTION_LABEL
    TOAST             = TOAST
    VERB_LABEL        = VERB_LABEL
    FILE_STATE_LABEL  = FILE_STATE_LABEL
    TRAY_FOR_STATE    = TRAY_FOR_STATE


def status_line(state: SyncState, **fmt: object) -> str:
    """The headline for a state, formatted. Unknown states fall back to the
    NOT_RUNNING wording rather than raising, because the tray must always paint."""
    return t(STATUS_LINE.get(state, STATUS_LINE[SyncState.NOT_RUNNING]), **fmt)


def status_sub(state: SyncState, **fmt: object) -> str:
    """The second line, formatted. Most states legitimately have none -> ''."""
    return t(STATUS_SUB.get(state, ""), **fmt)


def issue_title(code: IssueCode, **fmt: object) -> str:
    return t(ISSUE_TITLE.get(code, ISSUE_TITLE[IssueCode.UNKNOWN]), **fmt)


def action_label(action: RecoveryAction) -> str:
    return ACTION_LABEL[action]


def toast(nid: NotificationId, **fmt: object) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """(summary, body, actions) for a notification id, formatted."""
    summary, body, actions = TOAST[nid]
    return t(summary, **fmt), t(body, **fmt), actions
