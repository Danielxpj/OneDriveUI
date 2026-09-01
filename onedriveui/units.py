"""Every unit conversion and human-readable format, in one place.

The single most dangerous number in this application is a bandwidth limit,
because two incompatible units meet there:

  * **The OneDrive UI is KB/s, where K is 1000.** The Windows client's
    "Limit to [___] KB/s" spinner means 1000 bytes per second, and
    ``strings.SETTINGS.KB_PER_SEC`` says so.
  * **rclone's ``--bwlimit`` and ``core/bwlimit`` are KiB/s, where K is 1024.**
    ``core/bwlimit`` even echoes the value back normalised to binary units, so
    ``1M:100k`` comes home as ``"1Mi:100Ki"`` and a string comparison against
    what you sent is always wrong.

:func:`kb_to_kib` is THE conversion between them and it is never inlined
anywhere else: one wrong ``* 1000 / 1024`` in a settings page silently makes
every user's limit 2.4 % off, and nobody would ever notice.

Everything else here is presentation: decimal (1000-based) sizes because that is
what OneDrive shows, durations, ETAs and relative times. ``style="binary"`` is
available for developer-facing surfaces (the diagnostics bundle, the cache
pane), never for a string the user reads next to a Microsoft one.

Depends on nothing but the stdlib and ``onedriveui.constants``.
"""

from __future__ import annotations

import datetime as _dt
import math
import re

from onedriveui.constants import KB, KIB

__all__ = [
    "human_bytes", "human_rate", "human_duration", "eta_text", "relative_time",
    "kb_to_kib", "kib_to_kb", "parse_size", "format_size", "format_bwlimit",
    "format_rate", "DECIMAL_SUFFIXES", "BINARY_SUFFIXES", "SIZE_RE", "UNLIMITED",
    "MONTHS",
]

#: OneDrive's units. B, then powers of 1000.
DECIMAL_SUFFIXES: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB", "EB")

#: rclone's units. B, then powers of 1024. Developer-facing only.
BINARY_SUFFIXES: tuple[str, ...] = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")

#: rclone's SizeSuffix grammar: a number, an optional ``k|M|G|T|P|E`` (always a
#: power of 1024, whether or not the ``i`` is written) and an optional ``B``.
SIZE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([kKmMgGtTpPeE]?)\s*(i?)\s*[bB]?\s*$")

#: The multiplier for each rclone size suffix. rclone is binary throughout:
#: ``--bwlimit 10M`` is 10 MiB/s, exactly like ``10Mi``.
_SIZE_MULT: dict[str, int] = {
    "": 1, "k": KIB, "m": KIB ** 2, "g": KIB ** 3,
    "t": KIB ** 4, "p": KIB ** 5, "e": KIB ** 6,
}

#: What rclone accepts, and returns, for "no limit".
UNLIMITED = "off"

_MINUTE = 60
_HOUR = 3600
_DAY = 86400

#: Month names for :func:`relative_time`. Spelled out here rather than taken
#: from ``strftime("%B")`` because that reads the process locale, and Qt calls
#: ``setlocale(LC_ALL, "")`` when a QApplication is constructed. Without this
#: table the same function would return "1 June 2026" before the UI starts and
#: "1 junio 2026" after it, mixed into the hardcoded-English "Yesterday" beside
#: it. Localisation is a whole-application decision (``app.locale``), not one
#: this function makes by accident.
MONTHS: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


# ─────────────────────────────────────────────────────────────────────────────
# Sizes
# ─────────────────────────────────────────────────────────────────────────────

def human_bytes(n: int, *, style: str = "windows") -> str:
    """Format a byte count the way the OneDrive UI does.

    Args:
        n: The byte count. Negative values keep their sign; OneDrive reports
            ``-1`` as a directory size, and printing ``-1 B`` is more honest
            than clamping it to zero.
        style: ``"windows"`` (the default) uses decimal units — 1 KB is 1000
            bytes, matching every number Microsoft's client shows. ``"binary"``
            uses KiB/MiB/GiB and is for developer-facing surfaces only.

    Returns:
        A string such as ``"4.8 GB"``, ``"512 B"`` or ``"0 B"``. Whole bytes
        below the first suffix, then exactly one decimal place — the precision
        the OneDrive storage line uses ("252.5 GB of 1,024 GB used"), and
        enough that a slowly filling bar visibly moves.

    Raises:
        ValueError: If `style` is neither ``"windows"`` nor ``"binary"``.
    """
    if style == "windows":
        base, suffixes = KB, DECIMAL_SUFFIXES
    elif style == "binary":
        base, suffixes = KIB, BINARY_SUFFIXES
    else:
        raise ValueError(f"unknown style {style!r}; expected 'windows' or 'binary'")

    sign = "-" if n < 0 else ""
    value = float(abs(int(n)))
    if value < base:
        return f"{sign}{int(value)} {suffixes[0]}"

    index = 0
    while value >= base and index < len(suffixes) - 1:
        value /= base
        index += 1
    # Round first, then re-check the magnitude: 999.96 MB must print as 1.0 GB,
    # not as "1000.0 MB".
    if value >= 999.95 and index < len(suffixes) - 1:
        value /= base
        index += 1
    return f"{sign}{value:.1f} {suffixes[index]}"


def human_rate(bytes_per_s: float) -> str:
    """Format a transfer rate, e.g. ``"1.2 MB/s"``.

    Args:
        bytes_per_s: Bytes per second. rclone reports ``speed`` as a float and
            can report a negative or NaN value for an unstarted transfer; both
            come back as ``"0 B/s"``.

    Returns:
        A decimal-unit rate string.
    """
    try:
        value = float(bytes_per_s)
    except (TypeError, ValueError):
        return f"0 {DECIMAL_SUFFIXES[0]}/s"
    if not math.isfinite(value) or value <= 0:
        return f"0 {DECIMAL_SUFFIXES[0]}/s"
    return f"{human_bytes(int(value))}/s"


def parse_size(text: str) -> int:
    """Parse an rclone size string into bytes.

    rclone's ``SizeSuffix`` is binary throughout: ``k``, ``M``, ``G``, ``T``,
    ``P`` and ``E`` are powers of **1024** whether or not the ``i`` is written,
    so ``"10M"`` and ``"10Mi"`` are both 10485760. That is the opposite of what
    :func:`human_bytes` prints, which is exactly why parsing lives here rather
    than in each caller.

    Args:
        text: ``"30M"``, ``"20G"``, ``"512Ki"``, ``"1048576"``, or ``"off"``.

    Returns:
        The size in bytes, or ``-1`` for ``"off"`` / an empty string, which is
        how rclone spells "no limit".

    Raises:
        ValueError: If the text is not a size at all.
    """
    raw = str(text).strip()
    if not raw or raw.lower() in ("off", "unlimited"):
        return -1
    match = SIZE_RE.match(raw)
    if match is None:
        raise ValueError(f"not an rclone size: {text!r}")
    number, suffix, _binary_marker = match.groups()
    return int(float(number) * _SIZE_MULT[suffix.lower()])


def format_size(n: int) -> str:
    """Render a byte count as the shortest exact rclone size string.

    Used for ``chunk_size`` and friends, where a config value must round-trip
    through rclone's own parser unchanged.

    Args:
        n: A byte count, or a negative number for "no limit".

    Returns:
        ``"10M"``, ``"320k"``, ``"1048577"`` (when nothing divides evenly), or
        ``"off"``.
    """
    if n < 0:
        return UNLIMITED
    for suffix, mult in (("E", KIB ** 6), ("P", KIB ** 5), ("T", KIB ** 4),
                         ("G", KIB ** 3), ("M", KIB ** 2), ("k", KIB)):
        if n >= mult and n % mult == 0:
            return f"{n // mult}{suffix}"
    return str(n)


# ─────────────────────────────────────────────────────────────────────────────
# Durations
# ─────────────────────────────────────────────────────────────────────────────

def human_duration(seconds: float) -> str:
    """Format a duration with at most two units, e.g. ``"2h 15m"``.

    Args:
        seconds: A duration. Negative and non-finite values are treated as zero,
            because rclone reports ``-1`` for an unknown elapsed time.

    Returns:
        ``"45s"``, ``"5m 30s"``, ``"2h 15m"``, ``"3d 4h"``. The smaller unit is
        dropped when it is zero, so a round hour is ``"2h"``, not ``"2h 0m"``.
    """
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "0s"
    if not math.isfinite(total) or total < 0:
        total = 0.0
    total_i = int(total)

    if total_i < _MINUTE:
        return f"{total_i}s"
    if total_i < _HOUR:
        minutes, secs = divmod(total_i, _MINUTE)
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    if total_i < _DAY:
        hours, rest = divmod(total_i, _HOUR)
        minutes = rest // _MINUTE
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, rest = divmod(total_i, _DAY)
    hours = rest // _HOUR
    return f"{days}d {hours}h" if hours else f"{days}d"


def eta_text(seconds: int | None) -> str:
    """Format an rclone ETA.

    ``core/stats`` omits ``eta`` entirely while a transfer is starting and
    sends JSON ``null`` when it cannot estimate, which is why ``None`` is a
    first-class input rather than an error.

    Args:
        seconds: Seconds remaining, or ``None`` when rclone does not know.

    Returns:
        A duration string, or ``""`` when the ETA is unknown or negative.
    """
    if seconds is None:
        return ""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return ""
    if value < 0:
        return ""
    return human_duration(value)


def relative_time(iso: str, *, now: _dt.datetime | None = None) -> str:
    """Describe a timestamp relative to now, the way an activity list does.

    Args:
        iso: An RFC3339 stamp as produced by :func:`onedriveui.models.utcnow_iso`
            or emitted by rclone (nanosecond precision is tolerated).
        now: The reference instant, for tests. Defaults to the current UTC time.

    Returns:
        ``"Just now"``, ``"2 minutes ago"``, ``"Yesterday"``,
        ``"31 August 2026"``, or ``""`` when the stamp cannot be parsed. Always
        English and always the same, whatever the process locale — see
        :data:`MONTHS`. A future stamp — a clock skew between this machine and
        Graph — reads ``"Just now"`` rather than a negative age.
    """
    when = _parse_iso(iso)
    if when is None:
        return ""
    reference = now or _dt.datetime.now(_dt.UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=_dt.UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.UTC)

    delta = (reference - when).total_seconds()
    if delta < 45:
        return "Just now"
    if delta < 90:
        return "1 minute ago"
    if delta < _HOUR:
        return f"{int(round(delta / _MINUTE))} minutes ago"
    if delta < 2 * _HOUR:
        return "1 hour ago"
    if delta < _DAY:
        return f"{int(delta // _HOUR)} hours ago"

    # Past a full day, calendar days read better than elapsed hours: "2 days
    # ago" beats "31 hours ago", and the day boundary is what a person means by
    # "yesterday". Below a day the elapsed form above is kept, so something 12
    # hours old reads "12 hours ago" even across midnight.
    days = (reference.date() - when.date()).days
    if days <= 0:
        return "Just now"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "Last week"
    return f"{when.day} {MONTHS[when.month - 1]} {when.year}"


def _parse_iso(iso: str) -> _dt.datetime | None:
    """Parse an RFC3339 stamp, tolerating rclone's nanosecond precision.

    A private twin of ``models.parse_iso``; units.py deliberately does not
    import models, which is the one module in the tree that imports nothing.
    """
    if not iso:
        return None
    text = str(iso).strip().replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        frac, sign, offset = (tail.partition("+") if "+" in tail
                              else tail.partition("-"))
        text = f"{head}.{frac[:6]}{sign}{offset}" if sign else f"{head}.{frac[:6]}"
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# THE bandwidth conversion
# ─────────────────────────────────────────────────────────────────────────────

def kb_to_kib(kb: int) -> int:
    """Convert the UI's KB/s (1000) into rclone's KiB/s (1024).

    This is the only place the conversion exists. Every settings page, every
    ``core/bwlimit`` call and every ``--bwlimit`` argv goes through it.

    Args:
        kb: Kilobytes per second as the OneDrive UI spells them: K = 1000.

    Returns:
        Kibibytes per second, rounded to the nearest whole unit, as rclone's
        ``--bwlimit`` spells them: K = 1024. ``kb_to_kib(1000) == 977``.
        A non-positive input returns 0, since rclone spells "no limit" ``off``
        rather than ``0Ki``.
    """
    value = int(kb)
    if value <= 0:
        return 0
    return int(round(value * KB / KIB))


def kib_to_kb(kib: int) -> int:
    """Convert rclone's KiB/s (1024) back into the UI's KB/s (1000).

    The inverse of :func:`kb_to_kib`, used when reading a limit back out of
    ``core/bwlimit`` to populate the settings spinner.

    Args:
        kib: Kibibytes per second.

    Returns:
        Kilobytes per second, rounded. Not an exact inverse for every input —
        two adjacent KiB values can map to one KB value — which is why the
        configured value, never the echoed one, is the source of truth.
    """
    value = int(kib)
    if value <= 0:
        return 0
    return int(round(value * KIB / KB))


def format_rate(kb: int | None) -> str:
    """Render one side of a bandwidth limit as an rclone rate token.

    Args:
        kb: A limit in KB/s (1000), or ``None`` for unlimited.

    Returns:
        ``"977Ki"``, ``"1Mi"`` or ``"off"``. Always a binary suffix, because
        that is the only unit rclone's ``--bwlimit`` parser accepts without
        ambiguity.
    """
    if kb is None:
        return UNLIMITED
    kib = kb_to_kib(kb)
    if kib <= 0:
        return UNLIMITED
    total = kib * KIB
    for suffix, mult in (("Pi", KIB ** 5), ("Ti", KIB ** 4), ("Gi", KIB ** 3),
                         ("Mi", KIB ** 2), ("Ki", KIB)):
        if total >= mult and total % mult == 0:
            return f"{total // mult}{suffix}"
    return str(total)


def format_bwlimit(down_kb: int | None, up_kb: int | None) -> str:
    """Build the rate string for ``core/bwlimit`` / ``--bwlimit``.

    rclone's pair order is **upload:download**, the opposite of how the OneDrive
    settings page lists them, which is the whole reason this function takes its
    arguments in UI order and reverses them here rather than at each call site.

    Args:
        down_kb: The download limit in KB/s (1000), or ``None`` for unlimited.
        up_kb: The upload limit in KB/s (1000), or ``None`` for unlimited.

    Returns:
        ``"off"`` when neither side is limited, a single token when both sides
        are the same, and ``"<up>:<down>"`` otherwise — for example
        ``format_bwlimit(1000, 100)`` is ``"98Ki:977Ki"``.

        NEVER string-compare this against the value ``core/bwlimit`` echoes
        back: rclone re-normalises and ``"98Ki:977Ki"`` may come home spelled
        differently. Compare the configured KB values instead.
    """
    up = format_rate(up_kb)
    down = format_rate(down_kb)
    if up == UNLIMITED and down == UNLIMITED:
        return UNLIMITED
    if up == down:
        return up
    return f"{up}:{down}"
