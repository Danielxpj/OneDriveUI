"""OneDriveUI — a Windows-11-parity OneDrive client for Linux, on rclone."""

__version__ = "0.1.0"

APP_ID = "onedriveui"                 # Wayland app_id, .desktop basename, desktop-entry hint
APP_NAME = "OneDriveUI"
APP_DISPLAY_NAME = "OneDrive"         # what the user sees; we clone OneDrive's chrome
ORG_NAME = "OneDriveUI"

#: Load-bearing for Microsoft's throttle prioritisation. Format is
#: "ISV|CompanyName|AppName/Version" (or NONISV|...). Do not reformat.
USER_AGENT = f"ISV|OneDriveUI|OneDriveUI/{__version__}"

RCLONE_MIN_VERSION = (1, 75, 0)

__all__ = [
    "__version__", "APP_ID", "APP_NAME", "APP_DISPLAY_NAME",
    "ORG_NAME", "USER_AGENT", "RCLONE_MIN_VERSION",
]
