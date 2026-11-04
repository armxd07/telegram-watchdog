#!/usr/bin/env python3
"""ARIX IG Watchdog Ultimate — single-file build.

All 18 source modules merged inline so the bot runs from one file:
``python bot.py``. No package, no subfolders, no relative imports.

External dependencies (install via requirements.txt on Render/any host):
    python-telegram-bot, instagrapi, Pillow

Set TELEGRAM_BOT_TOKEN in the environment before running.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional
from datetime import datetime, timedelta, timezone
import logging
import logging.handlers
import re
from pathlib import Path
import unicodedata
from dataclasses import dataclass
import os
from dataclasses import dataclass, field
from typing import List
import asyncio
import hashlib
import tempfile
import time
from typing import Awaitable, Callable, Optional
import io
import math
import textwrap
from typing import List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple
from typing import Dict, List, Optional
import uuid
from typing import Awaitable, Callable, List, Optional
from typing import Dict, Optional
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, Tuple
from typing import List, Optional, Sequence
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
import csv
from datetime import datetime, timedelta
from typing import Any, Optional
from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import signal
import sys


# ======================================================================
# Module: models
# ======================================================================
class MonitorType(str, Enum):
    LIVE = "live"
    BAN = "ban"
    UNBAN = "unban"


class MonitorState(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class AvailabilityState(str, Enum):
    """The only availability states the classifier may emit.

    ``UNKNOWN`` must never be promoted into a ban or unban event.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


class LookupStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventType(str, Enum):
    LIVE = "live"
    CHANGE = "change"
    BAN = "ban"
    UNBAN = "unban"
    CUSTOM = "custom"


class AvailabilityTransition(str, Enum):
    GOING_INTO_VOID = "going_into_void"
    BACK_FROM_VOID = "back_from_void"


@dataclass
class User:
    id: int
    telegram_user_id: int
    username: Optional[str]
    first_seen: datetime
    is_active: bool = True
    language_code: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Monitor:
    id: int
    user_id: int
    ig_username: str
    monitor_type: MonitorType
    state: MonitorState
    interval_seconds: int
    created_at: datetime
    started_at: Optional[datetime] = None
    last_check_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    success_count: int = 0
    fail_count: int = 0
    scheduler_job_id: Optional[str] = None
    notes: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.state == MonitorState.ACTIVE

    @property
    def monitor_type_label(self) -> str:
        return {
            MonitorType.LIVE: "Live Monitor",
            MonitorType.BAN: "Ban Monitor",
            MonitorType.UNBAN: "Unban Monitor",
        }[self.monitor_type]


@dataclass
class Snapshot:
    id: int
    monitor_id: Optional[int]
    ig_username: str
    display_name: Optional[str]
    biography: Optional[str]
    followers: Optional[int]
    following: Optional[int]
    posts: Optional[int]
    profile_pic_url: Optional[str]
    profile_pic_path: Optional[str]
    verified: Optional[bool]
    is_private: Optional[bool]
    fetched_at: datetime
    lookup_duration_ms: Optional[int]
    lookup_status: LookupStatus
    availability_state: AvailabilityState
    error_message: Optional[str] = None

    def numeric(self, field_name: str) -> Optional[int]:
        mapping = {
            "followers": self.followers,
            "following": self.following,
            "posts": self.posts,
        }
        return mapping.get(field_name)

    def comparable(self) -> dict:
        """Return the normalized fields used for change detection."""
        return {
            "display_name": (self.display_name or ""),
            "biography": (self.biography or ""),
            "followers": self.followers,
            "following": self.following,
            "posts": self.posts,
            "verified": self.verified,
            "is_private": self.is_private,
            "profile_pic_url": (self.profile_pic_url or ""),
        }


@dataclass
class ChangeEvent:
    id: int
    monitor_id: int
    ig_username: str
    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
    diff: Optional[int]
    detected_at: datetime
    prev_snapshot_id: Optional[int]
    new_snapshot_id: int


@dataclass
class AvailabilityEvent:
    id: int
    monitor_id: int
    ig_username: str
    transition: AvailabilityTransition
    detected_at: datetime
    first_unavailable_at: Optional[datetime]
    last_unavailable_at: Optional[datetime]
    recovered_at: Optional[datetime]
    last_verified_snapshot_id: Optional[int]
    success_count: int
    fail_count: int
    event_code: str
    metadata: Optional[str] = None

    @property
    def is_recovery(self) -> bool:
        return self.transition == AvailabilityTransition.BACK_FROM_VOID


@dataclass
class CustomCard:
    id: int
    user_id: int
    ig_username: str
    display_name: Optional[str]
    biography: Optional[str]
    followers: Optional[int]
    following: Optional[int]
    posts: Optional[int]
    event_type: EventType
    event_timestamp: Optional[datetime]
    elapsed_text: Optional[str]
    footer: Optional[str]
    pic_path: Optional[str]
    created_at: datetime
    metadata_json: Optional[str] = None

    @property
    def is_custom(self) -> bool:
        return True


@dataclass
class UserSettings:
    user_id: int
    interval_seconds: int
    notify_changes: bool
    notify_availability: bool
    timezone: Optional[str]
    card_style: str
    max_monitors_override: Optional[int]


@dataclass
class CheckAttempt:
    id: int
    monitor_id: int
    attempted_at: datetime
    status: LookupStatus
    duration_ms: Optional[int]
    availability_state: AvailabilityState
    error_class: Optional[str]
    error_message: Optional[str]


@dataclass
class AppMetadata:
    key: str
    value: str


@dataclass
class MonitorSummary:
    """Lightweight projection used by the watchlist and dashboard."""

    id: int
    user_id: int
    ig_username: str
    monitor_type: MonitorType
    state: MonitorState
    interval_seconds: int
    created_at: datetime
    started_at: Optional[datetime]
    last_check_at: Optional[datetime]
    last_success_at: Optional[datetime]
    success_count: int
    fail_count: int


# ======================================================================
# Module: time_utils
# ======================================================================
def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=timezone.utc)


def to_utc(value: datetime) -> datetime:
    """Normalize any datetime to a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ensure_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return to_utc(value)


def format_duration(seconds: float) -> str:
    """Format an elapsed duration in seconds into a compact human string.

    The granularity rules come directly from the product specification and are
    unit-tested. Negative durations are clamped to zero.
    """
    if seconds is None:
        return "unknown"
    total = int(seconds)
    if total < 0:
        total = 0
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days >= 1:
        return f"{days}d {hours}h {minutes}m"
    if hours >= 1:
        return f"{hours}h {minutes}m"
    if minutes >= 1:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_duration_between(start: Optional[datetime], end: Optional[datetime]) -> str:
    """Format the delta between two timestamps; returns ``unknown`` if either is missing."""
    if start is None or end is None:
        return "unknown"
    delta = to_utc(end) - to_utc(start)
    return format_duration(max(0.0, delta.total_seconds()))


def format_timestamp(value: Optional[datetime], tz_name: Optional[str] = None) -> str:
    """Render a timestamp as ``YYYY-MM-DD HH:MM:SS UTC`` (or in the named zone)."""
    if value is None:
        return "never"
    aware = ensure_aware(value)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            target = aware.astimezone(ZoneInfo(tz_name))
            return target.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            pass
    return aware.strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime."""
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        return to_utc(dt)
    except ValueError:
        return None


def iso_now() -> str:
    return now_utc().isoformat()


def next_check_time(last: Optional[datetime], interval_seconds: int) -> Optional[datetime]:
    """Return the next scheduled check time, or None if there is no last check."""
    if last is None:
        return None
    return to_utc(last) + timedelta(seconds=interval_seconds)


def format_ms_or_seconds(seconds: float) -> str:
    """Format a short lookup duration. Sub-second values show milliseconds."""
    if seconds is None:
        return "unknown"
    if seconds < 1.0:
        return f"{int(seconds * 1000)} ms"
    return f"{seconds:.2f} s"


# ======================================================================
# Module: logging_config
# ======================================================================
_SENSITIVE_PATTERNS = [
    re.compile(r"(token=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(sessionid=)[^&\s]+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(\"sessionid\":\s*\")[^\"]+\""),
]

_CONFIGURED = False


class SensitiveFilter(logging.Filter):
    """Redact known secret patterns from log records before they are emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact(a) if isinstance(a, str) else a for a in record.args)
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern in _SENSITIVE_PATTERNS:
            text = pattern.sub(r"\1<redacted>", text)
        return text


def setup_logging(level: str = "INFO", log_dir: Optional[Path] = None) -> logging.Logger:
    """Configure the root ``arix`` logger exactly once.

    Returns the configured logger so callers can immediately use it.
    """
    global _CONFIGURED
    logger = logging.getLogger("arix")
    if _CONFIGURED:
        logger.setLevel(level)
        return logger

    logger.setLevel(level)
    logger.addFilter(SensitiveFilter())
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(SensitiveFilter())
    logger.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "arix_watchdog.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(SensitiveFilter())
        logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(name: str = "arix") -> logging.Logger:
    return logging.getLogger(f"arix.{name}") if name else logging.getLogger("arix")


# ======================================================================
# Module: validators
# ======================================================================
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# Instagram usernames: 1-30 chars, letters/digits/dots/underscores, no leading
# dot or underscore, no consecutive dots, no trailing dot per the public rules.
USERNAME_STRICT_RE = re.compile(r"^(?!.*\.\.)(?![._])[A-Za-z0-9._]{1,30}(?<![._])$")


class ValidationError(ValueError):
    """Raised when user input fails validation."""


@dataclass(frozen=True)
class CleanedUsername:
    value: str
    normalized: str


def validate_username(raw: str) -> CleanedUsername:
    """Validate and normalize an Instagram username.

    Strips whitespace and a leading ``@``. Returns a lowercased normalized form
    for storage while preserving the displayed casing.
    """
    if raw is None:
        raise ValidationError("Username is required.")
    cleaned = str(raw).strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    if not cleaned:
        raise ValidationError("Username cannot be empty.")
    if not USERNAME_RE.match(cleaned):
        raise ValidationError(
            "Username may only contain letters, numbers, dots and underscores, up to 30 characters."
        )
    if ".." in cleaned:
        raise ValidationError("Username may not contain consecutive dots.")
    if cleaned.startswith(".") or cleaned.startswith("_"):
        raise ValidationError("Username may not start with a dot or underscore.")
    if cleaned.endswith("."):
        raise ValidationError("Username may not end with a dot.")
    return CleanedUsername(value=cleaned, normalized=cleaned.lower())


def validate_count(raw: str, field_name: str, minimum: int = 0, maximum: int = 10_000_000_000) -> int:
    """Validate a non-negative integer such as follower counts."""
    if raw is None:
        raise ValidationError(f"{field_name} is required.")
    text = str(raw).strip().replace(",", "")
    if not text:
        raise ValidationError(f"{field_name} cannot be empty.")
    if not text.lstrip("+").isdigit():
        raise ValidationError(f"{field_name} must be a whole number.")
    value = int(text)
    if value < minimum:
        raise ValidationError(f"{field_name} must be at least {minimum}.")
    if value > maximum:
        raise ValidationError(f"{field_name} is unreasonably large.")
    return value


def validate_int(raw: str, field_name: str, minimum: int, maximum: int) -> int:
    if raw is None:
        raise ValidationError(f"{field_name} is required.")
    text = str(raw).strip()
    if not text:
        raise ValidationError(f"{field_name} cannot be empty.")
    if not text.isdigit() and not (text.startswith("-") and text[1:].isdigit()):
        raise ValidationError(f"{field_name} must be an integer.")
    value = int(text)
    if value < minimum or value > maximum:
        raise ValidationError(f"{field_name} must be between {minimum} and {maximum}.")
    return value


def sanitize_text(raw: str, max_length: int = 1000) -> str:
    """Sanitize free-form text (biography, footers).

    - Normalizes to NFC.
    - Strips control characters except newlines.
    - Truncates to ``max_length`` characters.
    """
    if raw is None:
        return ""
    text = unicodedata.normalize("NFC", str(raw))
    cleaned = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return cleaned.strip()[:max_length]


def validate_event_type(value: str) -> str:
    allowed = {"live", "change", "ban", "unban", "custom"}
    key = (value or "").strip().lower()
    if key not in allowed:
        raise ValidationError(f"Event type must be one of: {', '.join(sorted(allowed))}.")
    return key


def validate_timezone_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        from zoneinfo import available_timezones

        if value.strip() not in available_timezones():
            raise ValidationError(f"Unknown timezone: {value}")
    except Exception as exc:  # pragma: no cover - zoneinfo is stdlib on 3.12
        if not isinstance(exc, ValidationError):
            return value.strip()
        raise
    return value.strip()


def safe_int_or_none(raw: str) -> Optional[int]:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text.isdigit():
        return int(text)
    return None


# ======================================================================
# Module: config
# ======================================================================
try:  # python-dotsoft optional dependency
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "arix_watchdog.db"
DEFAULT_IG_SETTINGS = PROJECT_ROOT / "data" / "ig_settings.json"


@dataclass(frozen=True)
class IntervalPreset:
    """A polling interval option that is safe to expose in the UI."""

    seconds: int
    label: str

    def __post_init__(self) -> None:
        if self.seconds < 60:
            raise ValueError("interval presets must be >= 60 seconds")


# Conservative presets. Aggressive polling burns through Instagram quotas and
# risks checkpoint challenges; the smallest offered interval is one minute.
INTERVAL_PRESETS: List[IntervalPreset] = [
    IntervalPreset(60, "1 minute (aggressive)"),
    IntervalPreset(300, "5 minutes (balanced)"),
    IntervalPreset(900, "15 minutes (recommended)"),
    IntervalPreset(1800, "30 minutes (relaxed)"),
    IntervalPreset(3600, "1 hour (gentle)"),
    IntervalPreset(21600, "6 hours (background)"),
]

DEFAULT_INTERVAL_SECONDS = 900


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Resolved application settings."""

    telegram_bot_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    ig_sessionid: str = field(default_factory=lambda: os.environ.get("IG_SESSIONID", ""))
    database_path: Path = field(default_factory=lambda: Path(os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH))))
    ig_settings_path: Path = field(default_factory=lambda: Path(os.environ.get("IG_SETTINGS_PATH", str(DEFAULT_IG_SETTINGS))))
    check_seconds: int = field(default_factory=lambda: _env_int("CHECK_SECONDS", DEFAULT_INTERVAL_SECONDS))
    max_monitors_per_user: int = field(default_factory=lambda: _env_int("MAX_MONITORS_PER_USER", 10))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO").upper())
    timezone: str = field(default_factory=lambda: os.environ.get("TIMEZONE", "UTC"))
    request_timeout: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT", 25))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 3))
    backoff_base: float = field(default_factory=lambda: float(os.environ.get("BACKOFF_BASE", "2.0")))
    backoff_cap: float = field(default_factory=lambda: float(os.environ.get("BACKOFF_CAP", "300.0")))
    backoff_jitter: float = field(default_factory=lambda: float(os.environ.get("BACKOFF_JITTER", "0.3")))
    enable_dev_fallback: bool = field(default_factory=lambda: _env_bool("ENABLE_DEV_FALLBACK", False))
    webhook_mode: bool = field(default_factory=lambda: _env_bool("WEBHOOK_MODE", False))
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", ""))
    webhook_port: int = field(default_factory=lambda: _env_int("WEBHOOK_PORT", 8443))

    @property
    def interval_presets(self) -> List[IntervalPreset]:
        return list(INTERVAL_PRESETS)

    def preset_for_seconds(self, seconds: int) -> IntervalPreset:
        for preset in INTERVAL_PRESETS:
            if preset.seconds == seconds:
                return preset
        # Custom interval: still legal as long as >= 60s.
        return IntervalPreset(max(60, seconds), f"{seconds // 60} minutes")

    def validate(self) -> List[str]:
        """Return a list of human-readable configuration problems."""
        problems: List[str] = []
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set; the bot cannot start.")
        if self.check_seconds < 60:
            problems.append("CHECK_SECONDS must be at least 60 to avoid Instagram rate limits.")
        if self.max_monitors_per_user < 1:
            problems.append("MAX_MONITORS_PER_USER must be at least 1.")
        if self.backoff_base < 1.0:
            problems.append("BACKOFF_BASE must be >= 1.0.")
        return problems

    def mask_secrets(self) -> dict:
        """Return a JSON-safe snapshot suitable for /status output."""
        return {
            "telegram_bot_token": _mask(self.telegram_bot_token),
            "ig_sessionid": _mask(self.ig_sessionid),
            "database_path": str(self.database_path),
            "ig_settings_path": str(self.ig_settings_path),
            "check_seconds": self.check_seconds,
            "max_monitors_per_user": self.max_monitors_per_user,
            "log_level": self.log_level,
            "timezone": self.timezone,
            "request_timeout": self.request_timeout,
            "max_retries": self.max_retries,
            "backoff_base": self.backoff_base,
            "backoff_cap": self.backoff_cap,
            "webhook_mode": self.webhook_mode,
        }

    def ensure_dirs(self) -> None:
        """Create the data directory used by SQLite and the session settings."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.ig_settings_path.parent.mkdir(parents=True, exist_ok=True)


def _mask(value: str) -> str:
    if not value:
        return "<unset>"
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def load_settings() -> Settings:
    """Build and validate the runtime Settings object."""
    settings = Settings()
    settings.ensure_dirs()
    return settings


# ======================================================================
# Module: availability_classifier
# ======================================================================
@dataclass(frozen=True)
class Classification:
    state: AvailabilityState
    reason: str
    error_class: Optional[str] = None
    error_message: Optional[str] = None


# Names of exception classes that mean "Instagram explicitly says the profile
# is gone". These are the only inputs that may yield UNAVAILABLE.
_EXPLICIT_UNAVAILABLE = {
    "UserNotFound",
    "AccountSuspended",
    "ClientNotFoundError",
    "NotFoundError",
}

_CHALLENGE_OR_LOGIN = {
    "LoginRequired",
    "ClientLoginRequired",
    "ChallengeRequired",
    "ChallengeError",
    "ChallengeRedirection",
    "ChallengeUnknownStep",
    "CaptchaChallengeRequired",
    "ChallengeSelfieCaptcha",
    "RecaptchaChallengeForm",
    "BadCredentials",
    "BadPassword",
}

_RATE_LIMITED = {
    "RateLimitError",
    "ClientThrottledError",
}

_NETWORK = {
    "ClientConnectionError",
    "ClientRequestTimeout",
    "ClientIncompleteReadError",
}


def _coerce_exception_name(exc: BaseException) -> str:
    return exc.__class__.__name__


def classify_exception(exc: BaseException) -> Classification:
    """Map an exception raised during a lookup to an :class:`AvailabilityState`."""
    if exc is None:
        return Classification(AvailabilityState.UNKNOWN, "no exception supplied")
    name = _coerce_exception_name(exc)
    message = str(exc) or name

    if name in _EXPLICIT_UNAVAILABLE:
        return Classification(AvailabilityState.UNAVAILABLE, "explicit-unavailable", name, message)
    if name in _RATE_LIMITED:
        return Classification(AvailabilityState.RATE_LIMITED, "rate-limited", name, message)
    if name in _CHALLENGE_OR_LOGIN:
        return Classification(AvailabilityState.UNKNOWN, "challenge-or-login", name, message)
    if name in _NETWORK:
        return Classification(AvailabilityState.NETWORK_ERROR, "network-error", name, message)

    # Fallback heuristics on the message, since some libraries wrap the real
    # error. We only escalate to UNAVAILABLE on very specific wording.
    lowered = message.lower()
    if "user not found" in lowered or "not found" in lowered and "user" in lowered:
        return Classification(AvailabilityState.UNAVAILABLE, "explicit-unavailable-message", name, message)
    if "rate limit" in lowered or "throttl" in lowered or "too many requests" in lowered:
        return Classification(AvailabilityState.RATE_LIMITED, "rate-limited-message", name, message)
    if "challenge" in lowered or "checkpoint" in lowered or "login required" in lowered:
        return Classification(AvailabilityState.UNKNOWN, "challenge-message", name, message)
    if "timed out" in lowered or "timeout" in lowered or "connection" in lowered:
        return Classification(AvailabilityState.NETWORK_ERROR, "network-message", name, message)
    if "suspended" in lowered or "disabled" in lowered:
        return Classification(AvailabilityState.UNAVAILABLE, "explicit-suspended-message", name, message)

    return Classification(AvailabilityState.UNKNOWN, "unknown-error", name, message)


def classify_none_result(username: str) -> Classification:
    """Some clients return ``None`` instead of raising for missing users."""
    return Classification(
        AvailabilityState.UNAVAILABLE,
        "explicit-unavailable-none",
        error_message=f"client returned no profile for {username}",
    )


def is_ban_capable(state: AvailabilityState) -> bool:
    """Only ``UNAVAILABLE`` may ever trigger a ban event."""
    return state == AvailabilityState.UNAVAILABLE


def is_recovery_capable(state: AvailabilityState) -> bool:
    """Only ``AVAILABLE`` may ever trigger an unban/recovery event."""
    return state == AvailabilityState.AVAILABLE


def describe_state(state: AvailabilityState) -> str:
    """A short, honest human description used in cards and notifications."""
    return {
        AvailabilityState.AVAILABLE: "Profile accessible — verified snapshot stored.",
        AvailabilityState.UNAVAILABLE: "Profile explicitly unavailable on Instagram.",
        AvailabilityState.RATE_LIMITED: "Instagram rate-limited the request — no ban inferred.",
        AvailabilityState.NETWORK_ERROR: "Network error — no ban inferred.",
        AvailabilityState.UNKNOWN: "Lookup result unknown — no ban or recovery inferred.",
    }[state]


# ======================================================================
# Module: instagram_service
# ======================================================================
logger = get_logger("instagram")

Fetcher = Callable[[str], Optional["ProfileData"]]


class InstagramNotConfigured(RuntimeError):
    """Raised when no Instagram session is available."""


@dataclass
class ProfileData:
    username: str
    display_name: Optional[str] = None
    biography: Optional[str] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    posts: Optional[int] = None
    profile_pic_url: Optional[str] = None
    verified: Optional[bool] = None
    is_private: Optional[bool] = None

    def to_snapshot_fields(self) -> dict:
        return {
            "display_name": self.display_name,
            "biography": self.biography,
            "followers": self.followers,
            "following": self.following,
            "posts": self.posts,
            "profile_pic_url": self.profile_pic_url,
            "verified": self.verified,
            "is_private": self.is_private,
        }


@dataclass
class LookupResult:
    username: str
    profile: Optional[ProfileData]
    availability: AvailabilityState
    status: LookupStatus
    fetched_at: object
    duration_ms: int
    error_class: Optional[str]
    error_message: Optional[str]
    pic_path: Optional[str] = None
    classification_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == LookupStatus.SUCCESS and self.profile is not None


class InstagramService:
    """High-level Instagram lookup facade."""

    def __init__(
        self,
        settings,
        fetcher: Optional[Fetcher] = None,
        pic_dir: Optional[Path] = None,
        http_get: Optional[Callable[[str], Awaitable[bytes]]] = None,
    ) -> None:
        self.settings = settings
        self._fetcher: Fetcher = fetcher or self._default_fetcher
        self.pic_dir = Path(pic_dir or (settings.database_path.parent / "avatars"))
        self.pic_dir.mkdir(parents=True, exist_ok=True)
        self._http_get = http_get
        self._client = None  # lazily constructed default instagrapi client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def lookup(self, raw_username: str) -> LookupResult:
        """Look up a single Instagram username and return a typed result."""
        cleaned = validate_username(raw_username)
        username = cleaned.normalized
        start = time.perf_counter()
        fetched_at = now_utc()
        try:
            profile = await asyncio.to_thread(self._fetcher, username)
            duration_ms = int((time.perf_counter() - start) * 1000)
            if profile is None:
                cls = classify_none_result(username)
                logger.info("lookup none username=%s reason=%s", username, cls.reason)
                return LookupResult(
                    username=username, profile=None, availability=cls.state,
                    status=LookupStatus.FAILED, fetched_at=fetched_at, duration_ms=duration_ms,
                    error_class=cls.error_class, error_message=cls.error_message,
                    classification_reason=cls.reason,
                )
            pic_path = await self._download_pic(profile, username)
            logger.info("lookup ok username=%s duration_ms=%s", username, duration_ms)
            return LookupResult(
                username=username, profile=profile, availability=AvailabilityState.AVAILABLE,
                status=LookupStatus.SUCCESS, fetched_at=fetched_at, duration_ms=duration_ms,
                error_class=None, error_message=None, pic_path=pic_path,
                classification_reason="available",
            )
        except Exception as exc:  # classify everything
            duration_ms = int((time.perf_counter() - start) * 1000)
            cls = classify_exception(exc)
            logger.warning(
                "lookup failed username=%s state=%s class=%s reason=%s",
                username, cls.state.value, cls.error_class, cls.reason,
            )
            return LookupResult(
                username=username, profile=None, availability=cls.state,
                status=LookupStatus.FAILED, fetched_at=fetched_at, duration_ms=duration_ms,
                error_class=cls.error_class, error_message=cls.error_message,
                classification_reason=cls.reason,
            )

    async def _download_pic(self, profile: ProfileData, username: str) -> Optional[str]:
        url = profile.profile_pic_url
        if not url:
            return None
        try:
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
            target = self.pic_dir / f"{username}_{digest}.jpg"
            if target.exists() and target.stat().st_size > 0:
                return str(target)
            data = await self._fetch_bytes(url)
            if not data:
                return None
            target.write_bytes(data)
            return str(target)
        except Exception as exc:
            logger.debug("pic download skipped: %s", exc.__class__.__name__)
            return None

    async def _fetch_bytes(self, url: str) -> bytes:
        if self._http_get is not None:
            return await self._http_get(url)
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.settings.request_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return b""
                return await resp.read()

    # ------------------------------------------------------------------
    # Default instagrapi fetcher (override in tests)
    # ------------------------------------------------------------------
    def _get_client(self):
        if self._client is not None:
            return self._client
        sessionid = self.settings.ig_sessionid
        if not sessionid:
            raise InstagramNotConfigured(
                "No Instagram session configured. Set IG_SESSIONID or IG_SETTINGS_PATH."
            )
        try:
            from instagrapi import Client
        except Exception as exc:  # pragma: no cover - import guard
            raise InstagramNotConfigured(f"instagrapi unavailable: {exc.__class__.__name__}")
        client = Client()
        client.set_country("US")
        client.set_timezone_offset(0)
        # Prefer a persisted settings file when present; fall back to sessionid.
        settings_path = self.settings.ig_settings_path
        if settings_path.exists():
            try:
                client.load_settings(str(settings_path))
            except Exception:
                logger.warning("ig settings file present but unreadable")
        client.sessionid = sessionid  # never logged
        self._client = client
        return client

    def _default_fetcher(self, username: str) -> Optional[ProfileData]:
        client = self._get_client()
        user = client.user_info_by_username(username)
        if user is None:
            return None
        return ProfileData(
            username=username,
            display_name=getattr(user, "full_name", None),
            biography=getattr(user, "biography", None),
            followers=getattr(user, "follower_count", None),
            following=getattr(user, "following_count", None),
            posts=getattr(user, "media_count", None),
            profile_pic_url=getattr(user, "profile_pic_url", None),
            verified=bool(getattr(user, "is_verified", False)),
            is_private=bool(getattr(user, "is_private", False)),
        )

    def close(self) -> None:
        self._client = None


def make_fake_profile(username: str, **overrides) -> ProfileData:
    """Helper used by tests and the dev fallback to build a ProfileData."""
    base = dict(
        username=username,
        display_name=f"Display {username}",
        biography="A verified snapshot biography.",
        followers=1000,
        following=300,
        posts=42,
        profile_pic_url=None,
        verified=False,
        is_private=False,
    )
    base.update(overrides)
    return ProfileData(**base)


# ======================================================================
# Module: card_renderer
# ======================================================================
logger = get_logger("renderer")

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
WIDTH, HEIGHT = 1080, 1350
PADDING = 64
ACCENT = (30, 144, 255)          # electric blue
ACCENT_BRIGHT = (0, 194, 255)    # cyan accent
ACCENT_DIM = (16, 60, 110)
NAVY_TOP = (10, 21, 48)
NAVY_BOTTOM = (2, 4, 9)
TEXT_PRIMARY = (235, 240, 250)
TEXT_SECONDARY = (150, 170, 200)
TEXT_MUTED = (110, 130, 165)
DANGER = (255, 78, 90)
RECOVERY = (60, 220, 140)
CARD_BG = (16, 28, 56)
PANEL_BG = (22, 38, 74)

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# Per-event-type palette for the title bar.
EVENT_PALETTE = {
    "live": (ACCENT_BRIGHT, "LIVE PROFILE"),
    "change": (ACCENT_BRIGHT, "PROFILE CHANGE"),
    "ban": (DANGER, "GOING INTO VOID"),
    "unban": (RECOVERY, "BACK FROM VOID"),
    "custom": (ACCENT_BRIGHT, "CUSTOM CARD"),
}

CUSTOM_TITLES = {
    "custom_unban": ("BACK FROM VOID", RECOVERY),
    "custom_ban": ("GOING INTO VOID", DANGER),
    "custom_live": ("LIVE PROFILE", ACCENT_BRIGHT),
    "custom_change": ("PROFILE CHANGE", ACCENT_BRIGHT),
}


# ---------------------------------------------------------------------------
# Card specification
# ---------------------------------------------------------------------------
@dataclass
class ChangeRow:
    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
    diff: Optional[int] = None


@dataclass
class CardSpec:
    event_type: str  # live | change | ban | unban | custom
    username: str
    display_name: Optional[str] = None
    biography: Optional[str] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    posts: Optional[int] = None
    avatar_path: Optional[str] = None
    verified: Optional[bool] = None
    is_private: Optional[bool] = None
    profile_url: Optional[str] = None
    fetched_at: Optional[datetime] = None
    lookup_duration_ms: Optional[int] = None
    last_success_at: Optional[datetime] = None
    # change-specific
    changes: List[ChangeRow] = field(default_factory=list)
    # availability-specific
    event_code: Optional[str] = None
    first_unavailable_at: Optional[datetime] = None
    last_unavailable_at: Optional[datetime] = None
    recovered_at: Optional[datetime] = None
    last_verified_display: Optional[str] = None
    last_verified_followers: Optional[int] = None
    last_verified_following: Optional[int] = None
    last_verified_posts: Optional[int] = None
    success_count: int = 0
    fail_count: int = 0
    monitor_started_at: Optional[datetime] = None
    # custom
    custom_title: Optional[str] = None
    custom_event_timestamp: Optional[datetime] = None
    custom_elapsed_text: Optional[str] = None
    footer: Optional[str] = None
    is_custom: bool = False
    timezone: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_MONO if mono else (FONT_BOLD if bold else FONT_REGULAR)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _format_count(value: Optional[int]) -> str:
    if value is None:
        return "—"
    if value < 0:
        return "—"
    if value < 1000:
        return str(value)

    def _compact(divisor: int, suffix: str, precision: int) -> str:
        formatted = f"{value / divisor:.{precision}f}"
        # Strip trailing zeros and a dangling decimal point before the suffix.
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return f"{formatted}{suffix}"

    if value < 1_000_000:
        return _compact(1000, "K", 1)
    if value < 1_000_000_000:
        return _compact(1_000_000, "M", 2)
    return _compact(1_000_000_000, "B", 2)


def _format_signed(value: Optional[int]) -> str:
    if value is None:
        return ""
    if value > 0:
        return f"+{_format_count(value)}"
    if value < 0:
        return f"-{_format_count(abs(value))}"
    return "0"


def _gradient_background(top: Tuple[int, int, int], bottom: Tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), bottom)
    px = img.load()
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        r = int(top[0] + (bottom[0] - top[0]) * ratio)
        g = int(top[1] + (bottom[1] - top[1]) * ratio)
        b = int(top[2] + (bottom[2] - top[2]) * ratio)
        for x in range(WIDTH):
            px[x, y] = (r, g, b)
    return img


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _text(draw, xy, text, font, fill, anchor="la"):
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _text_width(font, text) -> int:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _truncate_biography(bio: str, font, max_width: int, max_lines: int = 4) -> Tuple[str, bool]:
    if not bio:
        return "", False
    wrapped = textwrap.wrap(bio, width=60)
    lines = []
    for piece in wrapped:
        while _text_width(font, piece) > max_width and len(piece) > 4:
            piece = piece[:-1]
        lines.append(piece)
    if len(lines) <= max_lines:
        return "\n".join(lines), False
    kept = lines[:max_lines]
    last = kept[-1]
    while _text_width(font, last + "…") > max_width and len(last) > 2:
        last = last[:-1]
    kept[-1] = last + "…"
    return "\n".join(kept), True


def _circular_avatar(path: Optional[str], size: int, fallback_initial: str) -> Image.Image:
    """Return a circular RGBA avatar with a fallback gradient + initial."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    if path:
        try:
            img = Image.open(path).convert("RGB")
            img.thumbnail((size, size))
            # Center-crop to square then resize.
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side)).resize((size, size))
            rgba = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            rgba.paste(img, (0, 0), mask)
            return rgba
        except Exception as exc:
            logger.debug("avatar fallback: %s", exc.__class__.__name__)
    # Fallback: gradient circle with initial letter.
    base = Image.new("RGB", (size, size), NAVY_BOTTOM)
    px = base.load()
    for y in range(size):
        for x in range(size):
            ratio = (x + y) / (2 * size)
            px[x, y] = (
                int(ACCENT_DIM[0] + (ACCENT[0] - ACCENT_DIM[0]) * ratio),
                int(ACCENT_DIM[1] + (ACCENT[1] - ACCENT_DIM[1]) * ratio),
                int(ACCENT_DIM[2] + (ACCENT[2] - ACCENT_DIM[2]) * ratio),
            )
    rgba = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rgba.paste(base, (0, 0), mask)
    draw = ImageDraw.Draw(rgba)
    font = _font(int(size * 0.42), bold=True)
    initial = (fallback_initial or "?").upper()[:1]
    draw.text((size / 2, size / 2), initial, font=font, fill=TEXT_PRIMARY, anchor="mm")
    return rgba


def _verified_badge(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int = 14):
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=ACCENT_BRIGHT)
    # simple check mark
    font = _font(int(radius * 1.2), bold=True)
    draw.text((cx, cy), "✓", font=font, fill=(2, 6, 20), anchor="mm")


def _draw_section_label(draw, x, y, text, font):
    _text(draw, (x, y), text.upper(), font, fill=TEXT_MUTED, anchor="la")
    return y + font.size + 10


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------
class CardRenderer:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(self, spec: CardSpec) -> Path:
        img = _gradient_background(NAVY_TOP, NAVY_BOTTOM)
        draw = ImageDraw.Draw(img, "RGBA")

        # Outer accent border.
        draw.rounded_rectangle((8, 8, WIDTH - 9, HEIGHT - 9), radius=36, outline=ACCENT_DIM, width=3)

        title, accent = self._resolve_title(spec)
        self._draw_title_bar(draw, title, accent, spec)
        self._draw_avatar_block(img, draw, spec)
        self._draw_stats_panels(draw, spec)
        cursor_y = self._draw_biography(draw, spec)
        if spec.event_type == "change":
            self._draw_change_list(draw, spec, cursor_y)
        elif spec.event_type in ("ban", "unban"):
            self._draw_availability_block(draw, spec, cursor_y)
        self._draw_footer(draw, spec, accent)
        if spec.is_custom:
            # Small, tasteful metadata tag — never a large DEMO watermark.
            self._draw_custom_tag(draw, spec)

        out_path = self.output_dir / f"card_{spec.event_type}_{abs(hash(spec.username + title))}.png"
        img.save(out_path, "PNG", optimize=True)
        return out_path

    # ------------------------------------------------------------------
    def _resolve_title(self, spec: CardSpec) -> Tuple[str, Tuple[int, int, int]]:
        if spec.is_custom and spec.custom_title:
            title_text, color = CUSTOM_TITLES.get(spec.custom_title, (spec.custom_title, ACCENT_BRIGHT))
            return title_text, color
        palette = EVENT_PALETTE.get(spec.event_type, EVENT_PALETTE["live"])
        return palette[1], palette[0]

    def _draw_title_bar(self, draw, title, accent, spec):
        bar_top = PADDING
        bar_h = 92
        _rounded_rect(
            draw, (PADDING, bar_top, WIDTH - PADDING, bar_top + bar_h),
            radius=20, fill=(20, 34, 66), outline=accent, width=2,
        )
        # Accent side stripe.
        draw.rounded_rectangle((PADDING, bar_top, PADDING + 8, bar_top + bar_h), radius=4, fill=accent)
        font = _font(36, bold=True)
        _text(draw, (PADDING + 34, bar_top + bar_h // 2), title, font, fill=accent, anchor="lm")
        if spec.event_code:
            code_font = _font(20, mono=True)
            _text(draw, (WIDTH - PADDING - 24, bar_top + bar_h // 2), spec.event_code, code_font, fill=TEXT_SECONDARY, anchor="rm")

    # ------------------------------------------------------------------
    def _draw_avatar_block(self, img: Image.Image, draw: ImageDraw.ImageDraw, spec):
        avatar_size = 168
        ax = PADDING + avatar_size // 2
        ay = PADDING + 92 + 30 + avatar_size // 2
        avatar = _circular_avatar(spec.avatar_path, avatar_size, spec.username[:1])
        img.paste(avatar, (ax - avatar_size // 2, ay - avatar_size // 2), avatar)
        # Ring around avatar.
        draw.ellipse(
            (ax - avatar_size // 2 - 4, ay - avatar_size // 2 - 4,
             ax + avatar_size // 2 + 4, ay + avatar_size // 2 + 4),
            outline=ACCENT, width=3,
        )
        # Username + display name to the right.
        text_x = ax + avatar_size // 2 + 28
        uname_font = _font(40, bold=True)
        _text(draw, (text_x, ay - 36), f"@{spec.username}", uname_font, fill=TEXT_PRIMARY, anchor="lm")
        if spec.verified:
            vw = _text_width(uname_font, f"@{spec.username}")
            _verified_badge(draw, text_x + vw + 22, ay - 36, radius=15)
        name_font = _font(26)
        name = spec.display_name or ""
        if _text_width(name_font, name) > WIDTH - text_x - PADDING - 40:
            while _text_width(name_font, name + "…") > WIDTH - text_x - PADDING - 40 and len(name) > 2:
                name = name[:-1]
            name = name + "…"
        _text(draw, (text_x, ay + 10), name or "—", name_font, fill=TEXT_SECONDARY, anchor="lm")
        if spec.is_private is not None:
            priv_font = _font(18)
            label = "Private account" if spec.is_private else "Public account"
            _text(draw, (text_x, ay + 44), label, priv_font, fill=TEXT_MUTED, anchor="lm")

    # ------------------------------------------------------------------
    def _draw_stats_panels(self, draw, spec):
        top = PADDING + 92 + 30 + 168 + 36
        panel_h = 132
        gap = 18
        total_w = WIDTH - 2 * PADDING
        panel_w = (total_w - 2 * gap) // 3
        stats = [
            ("Followers", _format_count(spec.followers)),
            ("Following", _format_count(spec.following)),
            ("Posts", _format_count(spec.posts)),
        ]
        for i, (label, value) in enumerate(stats):
            x0 = PADDING + i * (panel_w + gap)
            _rounded_rect(
                draw, (x0, top, x0 + panel_w, top + panel_h), radius=18,
                fill=PANEL_BG, outline=ACCENT_DIM, width=1,
            )
            _text(draw, (x0 + panel_w // 2, top + 28), label.upper(), _font(18, bold=True), fill=TEXT_MUTED, anchor="mm")
            _text(draw, (x0 + panel_w // 2, top + 76), value, _font(42, bold=True), fill=TEXT_PRIMARY, anchor="mm")
        return top + panel_h

    # ------------------------------------------------------------------
    def _draw_biography(self, draw, spec) -> int:
        top = PADDING + 92 + 30 + 168 + 36 + 132 + 30
        label_font = _font(20, bold=True)
        body_font = _font(24)
        bio = spec.biography or ""
        max_w = WIDTH - 2 * PADDING - 20
        text, truncated = _truncate_biography(bio, body_font, max_w, max_lines=4)
        y = _draw_section_label(draw, PADDING, top, "Biography", label_font)
        if not text:
            _text(draw, (PADDING, y), "No biography available.", body_font, fill=TEXT_MUTED, anchor="la")
            return y + 30
        for line in text.split("\n"):
            _text(draw, (PADDING, y), line, body_font, fill=TEXT_SECONDARY, anchor="la")
            y += 32
        if truncated:
            _text(draw, (PADDING, y), "(Use ‘View Full Biography’ for the complete text.)", _font(18), fill=TEXT_MUTED, anchor="la")
            y += 26
        return y + 8

    # ------------------------------------------------------------------
    def _draw_change_list(self, draw, spec, y):
        if not spec.changes:
            return
        label_font = _font(20, bold=True)
        y = _draw_section_label(draw, PADDING, y + 10, "Detected changes", label_font)
        row_font = _font(22)
        value_font = _font(22, bold=True)
        max_rows = 6
        for i, change in enumerate(spec.changes[:max_rows]):
            field_label = change.field_name.replace("_", " ").title()
            old = change.old_value or "—"
            new = change.new_value or "—"
            row_h = 56
            _rounded_rect(draw, (PADDING, y, WIDTH - PADDING, y + row_h), radius=12, fill=PANEL_BG, outline=ACCENT_DIM, width=1)
            _text(draw, (PADDING + 18, y + row_h // 2), field_label, row_font, fill=TEXT_SECONDARY, anchor="lm")
            mid = f"{old}  →  {new}"
            _text(draw, (WIDTH - PADDING - 18, y + row_h // 2), mid, value_font, fill=ACCENT_BRIGHT, anchor="rm")
            if change.diff is not None:
                diff_color = RECOVERY if change.diff > 0 else (DANGER if change.diff < 0 else TEXT_SECONDARY)
                _text(draw, (WIDTH // 2, y + row_h // 2), _format_signed(change.diff), _font(20, bold=True), fill=diff_color, anchor="mm")
            y += row_h + 8
        if len(spec.changes) > max_rows:
            _text(draw, (PADDING, y), f"+{len(spec.changes) - max_rows} more changes…", _font(18), fill=TEXT_MUTED, anchor="la")

    # ------------------------------------------------------------------
    def _draw_availability_block(self, draw, spec, y):
        label_font = _font(20, bold=True)
        value_font = _font(22)
        y = _draw_section_label(draw, PADDING, y + 10, "Availability timeline", label_font)

        def row(label, value, color=TEXT_PRIMARY):
            nonlocal y
            _text(draw, (PADDING, y), label, value_font, fill=TEXT_SECONDARY, anchor="la")
            _text(draw, (WIDTH - PADDING, y), value, value_font, fill=color, anchor="ra")
            y += 30

        tz = spec.timezone
        if spec.event_type == "ban":
            row("Last successful access", format_timestamp(spec.last_success_at, tz))
            row("First confirmed unavailable", format_timestamp(spec.first_unavailable_at, tz), DANGER)
            row("Last confirmed unavailable", format_timestamp(spec.last_unavailable_at, tz), DANGER)
            elapsed = format_duration_between_safe(spec.last_success_at, spec.first_unavailable_at)
            row("Elapsed since last success", elapsed, DANGER)
            row("Successful checks", str(spec.success_count))
            row("Failed checks", str(spec.fail_count), DANGER)
        else:  # unban / recovery
            row("First confirmed unavailable", format_timestamp(spec.first_unavailable_at, tz))
            row("Last confirmed unavailable", format_timestamp(spec.last_unavailable_at, tz))
            row("First successful recovery", format_timestamp(spec.recovered_at, tz), RECOVERY)
            if spec.first_unavailable_at and spec.recovered_at:
                dur = format_duration_between_safe(spec.first_unavailable_at, spec.recovered_at)
                row("Total unavailable duration", dur, RECOVERY)
            row("Successful checks", str(spec.success_count), RECOVERY)
            row("Failed checks", str(spec.fail_count))

    # ------------------------------------------------------------------
    def _draw_footer(self, draw, spec, accent):
        y = HEIGHT - PADDING - 96
        _rounded_rect(draw, (PADDING, y, WIDTH - PADDING, HEIGHT - PADDING - 16), radius=16, fill=(14, 24, 48), outline=ACCENT_DIM, width=1)
        font = _font(18)
        mono = _font(16, mono=True)
        tz = spec.timezone
        left_x = PADDING + 18
        right_x = WIDTH - PADDING - 18
        _text(draw, (left_x, y + 18), f"Fetched: {format_timestamp(spec.fetched_at, tz)}", font, fill=TEXT_MUTED, anchor="la")
        _text(draw, (left_x, y + 44), f"Lookup: {format_ms_or_seconds((spec.lookup_duration_ms or 0) / 1000)}", font, fill=TEXT_MUTED, anchor="la")
        if spec.last_success_at:
            _text(draw, (left_x, y + 70), f"Last verified: {format_timestamp(spec.last_success_at, tz)}", font, fill=TEXT_MUTED, anchor="la")
        if spec.profile_url:
            _text(draw, (right_x, y + 18), "instagram.com/" + spec.username, mono, fill=TEXT_MUTED, anchor="ra")
        if spec.footer:
            _text(draw, (right_x, y + 44), spec.footer[:48], mono, fill=TEXT_MUTED, anchor="ra")
        if spec.custom_elapsed_text:
            _text(draw, (right_x, y + 70), spec.custom_elapsed_text[:48], mono, fill=TEXT_MUTED, anchor="ra")

    # ------------------------------------------------------------------
    def _draw_custom_tag(self, draw, spec):
        # Small bottom-left metadata tag so custom cards are identifiable
        # without a large obtrusive watermark.
        font = _font(14, bold=True)
        text = "CUSTOM CARD · not a verified Instagram event"
        w = _text_width(font, text) + 24
        box = (PADDING, HEIGHT - PADDING - 12 - 22, PADDING + w, HEIGHT - PADDING - 12)
        _rounded_rect(draw, box, radius=10, fill=(40, 30, 60), outline=(90, 70, 130), width=1)
        _text(draw, (PADDING + 12, box[1] + 11), text, font, fill=(180, 170, 210), anchor="lm")


def format_duration_between_safe(start: Optional[datetime], end: Optional[datetime]) -> str:
    return format_duration_between(start, end)


def render_full_biography(spec: CardSpec, output_dir: Path) -> Path:
    """Render a tall image with the complete biography for the 'View Full Biography' button."""
    output_dir.mkdir(parents=True, exist_ok=True)
    bio = spec.biography or "No biography available."
    lines = textwrap.wrap(bio, width=58) or [bio]
    line_h = 36
    h = min(1800, 240 + len(lines) * line_h)
    img = _gradient_background(NAVY_TOP, NAVY_BOTTOM)
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rounded_rectangle((8, 8, WIDTH - 9, h - 9), radius=28, outline=ACCENT_DIM, width=2)
    _text(draw, (PADDING, 48), f"@{spec.username}", _font(40, bold=True), fill=TEXT_PRIMARY, anchor="la")
    _text(draw, (PADDING, 100), "Full biography", _font(22, bold=True), fill=ACCENT_BRIGHT, anchor="la")
    y = 160
    for line in lines:
        _text(draw, (PADDING, y), line, _font(26), fill=TEXT_SECONDARY, anchor="la")
        y += line_h
    out = output_dir / f"bio_{abs(hash(spec.username))}.png"
    img.save(out, "PNG", optimize=True)
    return out


# ======================================================================
# Module: database
# ======================================================================
logger = get_logger("database")

SCHEMA_VERSION = 4


def _adapt_datetime(value: datetime) -> str:
    return ensure_aware(value).isoformat()


sqlite3.register_adapter(datetime, _adapt_datetime)


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        telegram_user_id=row["telegram_user_id"],
        username=row["username"],
        first_seen=ensure_aware(datetime.fromisoformat(row["first_seen"])),
        is_active=bool(row["is_active"]),
        language_code=row["language_code"],
    )


def _row_to_monitor(row: sqlite3.Row) -> Monitor:
    return Monitor(
        id=row["id"],
        user_id=row["user_id"],
        ig_username=row["ig_username"],
        monitor_type=MonitorType(row["monitor_type"]),
        state=MonitorState(row["state"]),
        interval_seconds=row["interval_seconds"],
        created_at=ensure_aware(datetime.fromisoformat(row["created_at"])),
        started_at=ensure_aware(datetime.fromisoformat(row["started_at"])) if row["started_at"] else None,
        last_check_at=ensure_aware(datetime.fromisoformat(row["last_check_at"])) if row["last_check_at"] else None,
        last_success_at=ensure_aware(datetime.fromisoformat(row["last_success_at"])) if row["last_success_at"] else None,
        last_error=row["last_error"],
        success_count=row["success_count"],
        fail_count=row["fail_count"],
        scheduler_job_id=row["scheduler_job_id"],
        notes=row["notes"],
    )


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        monitor_id=row["monitor_id"],
        ig_username=row["ig_username"],
        display_name=row["display_name"],
        biography=row["biography"],
        followers=row["followers"],
        following=row["following"],
        posts=row["posts"],
        profile_pic_url=row["profile_pic_url"],
        profile_pic_path=row["profile_pic_path"],
        verified=None if row["verified"] is None else bool(row["verified"]),
        is_private=None if row["is_private"] is None else bool(row["is_private"]),
        fetched_at=ensure_aware(datetime.fromisoformat(row["fetched_at"])),
        lookup_duration_ms=row["lookup_duration_ms"],
        lookup_status=LookupStatus(row["lookup_status"]),
        availability_state=AvailabilityState(row["availability_state"]),
        error_message=row["error_message"],
    )


class Database:
    """A thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage txns explicitly
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA temp_store=MEMORY;")
        self.migrate()

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------
    def migrate(self) -> None:
        with self._lock, self.transaction():
            cur = self._conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            cur.execute("SELECT value FROM app_metadata WHERE key='schema_version'")
            row = cur.fetchone()
            current = int(row["value"]) if row else 0
            if current >= SCHEMA_VERSION:
                return
            for version, statements in _MIGRATIONS:
                if current < version:
                    for stmt in statements:
                        cur.execute(stmt)
                    cur.execute(
                        "INSERT INTO app_metadata(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(version),),
                    )
                    current = version
                    logger.info("database migrated to schema version %s", version)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Run statements inside a single transaction. Rolls back on error."""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            try:
                yield cur
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    def upsert_user(self, telegram_user_id: int, username: Optional[str], language_code: Optional[str] = None) -> User:
        now = now_utc()
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO users(telegram_user_id, username, first_seen, is_active, language_code) "
                "VALUES(?, ?, ?, 1, ?) "
                "ON CONFLICT(telegram_user_id) DO UPDATE SET "
                "username=COALESCE(excluded.username, users.username), "
                "language_code=COALESCE(excluded.language_code, users.language_code), "
                "is_active=1",
                (telegram_user_id, username, now, language_code),
            )
            cur.execute("SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,))
            return _row_to_user(cur.fetchone())

    def get_user(self, telegram_user_id: int) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)
            ).fetchone()
            return _row_to_user(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return _row_to_user(row) if row else None

    # ------------------------------------------------------------------
    # Monitors
    # ------------------------------------------------------------------
    def count_active_monitors_for_user(self, telegram_user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM monitors m JOIN users u ON u.id=m.user_id "
                "WHERE u.telegram_user_id=? AND m.state IN ('active','paused')",
                (telegram_user_id,),
            ).fetchone()
            return int(row["c"])

    def find_monitor(self, telegram_user_id: int, ig_username: str, monitor_type: MonitorType) -> Optional[Monitor]:
        with self._lock:
            row = self._conn.execute(
                "SELECT m.* FROM monitors m JOIN users u ON u.id=m.user_id "
                "WHERE u.telegram_user_id=? AND LOWER(m.ig_username)=? AND m.monitor_type=? "
                "AND m.state IN ('active','paused') LIMIT 1",
                (telegram_user_id, ig_username.lower(), monitor_type.value),
            ).fetchone()
            return _row_to_monitor(row) if row else None

    def create_monitor(
        self,
        telegram_user_id: int,
        ig_username: str,
        monitor_type: MonitorType,
        interval_seconds: int,
    ) -> Monitor:
        user = self.get_user(telegram_user_id)
        if user is None:
            user = self.upsert_user(telegram_user_id, None)
        now = now_utc()
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO monitors(user_id, ig_username, monitor_type, state, interval_seconds, "
                "created_at, started_at, success_count, fail_count) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (user.id, ig_username, monitor_type.value, MonitorState.ACTIVE.value,
                 interval_seconds, now, now),
            )
            monitor_id = cur.lastrowid
            job_id = f"monitor:{monitor_id}"
            cur.execute("UPDATE monitors SET scheduler_job_id=? WHERE id=?", (job_id, monitor_id))
            cur.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,))
            return _row_to_monitor(cur.fetchone())

    def get_monitor(self, monitor_id: int) -> Optional[Monitor]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
            return _row_to_monitor(row) if row else None

    def list_monitors(self, telegram_user_id: int) -> List[MonitorSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.id, m.user_id, m.ig_username, m.monitor_type, m.state, m.interval_seconds, "
                "m.created_at, m.started_at, m.last_check_at, m.last_success_at, "
                "m.success_count, m.fail_count "
                "FROM monitors m JOIN users u ON u.id=m.user_id "
                "WHERE u.telegram_user_id=? AND m.state IN ('active','paused') "
                "ORDER BY m.created_at DESC",
                (telegram_user_id,),
            ).fetchall()
            return [
                MonitorSummary(
                    id=r["id"], user_id=r["user_id"], ig_username=r["ig_username"],
                    monitor_type=MonitorType(r["monitor_type"]), state=MonitorState(r["state"]),
                    interval_seconds=r["interval_seconds"],
                    created_at=ensure_aware(datetime.fromisoformat(r["created_at"])),
                    started_at=ensure_aware(datetime.fromisoformat(r["started_at"])) if r["started_at"] else None,
                    last_check_at=ensure_aware(datetime.fromisoformat(r["last_check_at"])) if r["last_check_at"] else None,
                    last_success_at=ensure_aware(datetime.fromisoformat(r["last_success_at"])) if r["last_success_at"] else None,
                    success_count=r["success_count"], fail_count=r["fail_count"],
                )
                for r in rows
            ]

    def list_monitors_by_state(self, state: MonitorState) -> List[Monitor]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM monitors WHERE state=? ORDER BY created_at ASC", (state.value,)
            ).fetchall()
            return [_row_to_monitor(r) for r in rows]

    def list_all_active_monitors(self) -> List[Monitor]:
        return self.list_monitors_by_state(MonitorState.ACTIVE)

    def update_monitor_state(self, monitor_id: int, state: MonitorState) -> None:
        with self.transaction() as cur:
            cur.execute("UPDATE monitors SET state=? WHERE id=?", (state.value, monitor_id))

    def update_monitor_check(
        self,
        monitor_id: int,
        *,
        last_check: datetime,
        last_success: Optional[datetime],
        success_delta: int,
        fail_delta: int,
        last_error: Optional[str],
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE monitors SET last_check_at=?, last_success_at=COALESCE(?, last_success_at), "
                "success_count=success_count+?, fail_count=fail_count+?, last_error=? WHERE id=?",
                (last_check, last_success, success_delta, fail_delta, last_error, monitor_id),
            )

    def change_interval(self, monitor_id: int, interval_seconds: int) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE monitors SET interval_seconds=? WHERE id=?", (interval_seconds, monitor_id)
            )

    def stop_monitor(self, monitor_id: int) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE monitors SET state=?, scheduler_job_id=NULL WHERE id=?",
                (MonitorState.STOPPED.value, monitor_id),
            )

    def stop_all_for_user(self, telegram_user_id: int) -> int:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE monitors SET state=?, scheduler_job_id=NULL "
                "WHERE user_id=(SELECT id FROM users WHERE telegram_user_id=?) "
                "AND state IN ('active','paused')",
                (MonitorState.STOPPED.value, telegram_user_id),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def save_snapshot(self, snapshot: Snapshot) -> Snapshot:
        now = now_utc()
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO snapshots(monitor_id, ig_username, display_name, biography, "
                "followers, following, posts, profile_pic_url, profile_pic_path, verified, is_private, "
                "fetched_at, lookup_duration_ms, lookup_status, availability_state, error_message) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot.monitor_id, snapshot.ig_username, snapshot.display_name, snapshot.biography,
                 snapshot.followers, snapshot.following, snapshot.posts, snapshot.profile_pic_url,
                 snapshot.profile_pic_path, snapshot.verified, snapshot.is_private,
                 snapshot.fetched_at or now, snapshot.lookup_duration_ms, snapshot.lookup_status.value,
                 snapshot.availability_state.value, snapshot.error_message),
            )
            snapshot_id = cur.lastrowid
            cur.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,))
            return _row_to_snapshot(cur.fetchone())

    def latest_snapshot(self, monitor_id: int) -> Optional[Snapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE monitor_id=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
                (monitor_id,),
            ).fetchone()
            return _row_to_snapshot(row) if row else None

    def latest_successful_snapshot(self, monitor_id: int) -> Optional[Snapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE monitor_id=? AND availability_state='available' "
                "ORDER BY fetched_at DESC, id DESC LIMIT 1",
                (monitor_id,),
            ).fetchone()
            return _row_to_snapshot(row) if row else None

    def previous_snapshot(self, monitor_id: int, before_id: int) -> Optional[Snapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE monitor_id=? AND id<? ORDER BY id DESC LIMIT 1",
                (monitor_id, before_id),
            ).fetchone()
            return _row_to_snapshot(row) if row else None

    def count_snapshots(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM snapshots WHERE availability_state='available'"
            ).fetchone()
            return int(row["c"])

    # ------------------------------------------------------------------
    # Change events
    # ------------------------------------------------------------------
    def save_change_event(self, event: ChangeEvent) -> ChangeEvent:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO change_events(monitor_id, ig_username, field_name, old_value, new_value, "
                "diff, detected_at, prev_snapshot_id, new_snapshot_id) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.monitor_id, event.ig_username, event.field_name, event.old_value,
                 event.new_value, event.diff, event.detected_at, event.prev_snapshot_id,
                 event.new_snapshot_id),
            )
            event_id = cur.lastrowid
            cur.execute("SELECT * FROM change_events WHERE id=?", (event_id,))
            row = cur.fetchone()
            return ChangeEvent(
                id=row["id"], monitor_id=row["monitor_id"], ig_username=row["ig_username"],
                field_name=row["field_name"], old_value=row["old_value"], new_value=row["new_value"],
                diff=row["diff"], detected_at=ensure_aware(datetime.fromisoformat(row["detected_at"])),
                prev_snapshot_id=row["prev_snapshot_id"], new_snapshot_id=row["new_snapshot_id"],
            )

    # ------------------------------------------------------------------
    # Availability events
    # ------------------------------------------------------------------
    def save_availability_event(self, event: AvailabilityEvent) -> AvailabilityEvent:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO availability_events(monitor_id, ig_username, transition, detected_at, "
                "first_unavailable_at, last_unavailable_at, recovered_at, last_verified_snapshot_id, "
                "success_count, fail_count, event_code, metadata) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.monitor_id, event.ig_username, event.transition.value, event.detected_at,
                 event.first_unavailable_at, event.last_unavailable_at, event.recovered_at,
                 event.last_verified_snapshot_id, event.success_count, event.fail_count,
                 event.event_code, event.metadata),
            )
            event_id = cur.lastrowid
            cur.execute("SELECT * FROM availability_events WHERE id=?", (event_id,))
            row = cur.fetchone()
            return AvailabilityEvent(
                id=row["id"], monitor_id=row["monitor_id"], ig_username=row["ig_username"],
                transition=AvailabilityTransition(row["transition"]),
                detected_at=ensure_aware(datetime.fromisoformat(row["detected_at"])),
                first_unavailable_at=ensure_aware(datetime.fromisoformat(row["first_unavailable_at"])) if row["first_unavailable_at"] else None,
                last_unavailable_at=ensure_aware(datetime.fromisoformat(row["last_unavailable_at"])) if row["last_unavailable_at"] else None,
                recovered_at=ensure_aware(datetime.fromisoformat(row["recovered_at"])) if row["recovered_at"] else None,
                last_verified_snapshot_id=row["last_verified_snapshot_id"], success_count=row["success_count"],
                fail_count=row["fail_count"], event_code=row["event_code"], metadata=row["metadata"],
            )

    def latest_open_void_event(self, monitor_id: int) -> Optional[AvailabilityEvent]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM availability_events WHERE monitor_id=? AND transition='going_into_void' "
                "AND recovered_at IS NULL ORDER BY detected_at DESC LIMIT 1",
                (monitor_id,),
            ).fetchone()
            if not row:
                return None
            return AvailabilityEvent(
                id=row["id"], monitor_id=row["monitor_id"], ig_username=row["ig_username"],
                transition=AvailabilityTransition(row["transition"]),
                detected_at=ensure_aware(datetime.fromisoformat(row["detected_at"])),
                first_unavailable_at=ensure_aware(datetime.fromisoformat(row["first_unavailable_at"])) if row["first_unavailable_at"] else None,
                last_unavailable_at=ensure_aware(datetime.fromisoformat(row["last_unavailable_at"])) if row["last_unavailable_at"] else None,
                recovered_at=None, last_verified_snapshot_id=row["last_verified_snapshot_id"],
                success_count=row["success_count"], fail_count=row["fail_count"],
                event_code=row["event_code"], metadata=row["metadata"],
            )

    def mark_void_event_recovered(self, event_id: int, recovered_at: datetime) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE availability_events SET recovered_at=? WHERE id=?", (recovered_at, event_id)
            )

    def bump_void_last_unavailable(self, event_id: int, when: datetime) -> None:
        """Record that a still-open void event saw another unavailable check."""
        with self.transaction() as cur:
            cur.execute(
                "UPDATE availability_events SET last_unavailable_at=? WHERE id=? "
                "AND recovered_at IS NULL",
                (when, event_id),
            )

    def latest_void_event(self, monitor_id: int) -> Optional[AvailabilityEvent]:
        """Most recent availability event of any kind, used by recovery cards."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM availability_events WHERE monitor_id=? "
                "ORDER BY detected_at DESC, id DESC LIMIT 1",
                (monitor_id,),
            ).fetchone()
            if not row:
                return None
            return AvailabilityEvent(
                id=row["id"], monitor_id=row["monitor_id"], ig_username=row["ig_username"],
                transition=AvailabilityTransition(row["transition"]),
                detected_at=ensure_aware(datetime.fromisoformat(row["detected_at"])),
                first_unavailable_at=ensure_aware(datetime.fromisoformat(row["first_unavailable_at"])) if row["first_unavailable_at"] else None,
                last_unavailable_at=ensure_aware(datetime.fromisoformat(row["last_unavailable_at"])) if row["last_unavailable_at"] else None,
                recovered_at=ensure_aware(datetime.fromisoformat(row["recovered_at"])) if row["recovered_at"] else None,
                last_verified_snapshot_id=row["last_verified_snapshot_id"],
                success_count=row["success_count"], fail_count=row["fail_count"],
                event_code=row["event_code"], metadata=row["metadata"],
            )

    # ------------------------------------------------------------------
    # Custom cards
    # ------------------------------------------------------------------
    def save_custom_card(self, card: CustomCard) -> CustomCard:
        now = now_utc()
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO custom_cards(user_id, ig_username, display_name, biography, followers, "
                "following, posts, event_type, event_timestamp, elapsed_text, footer, pic_path, "
                "created_at, metadata_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (card.user_id, card.ig_username, card.display_name, card.biography, card.followers,
                 card.following, card.posts, card.event_type.value, card.event_timestamp,
                 card.elapsed_text, card.footer, card.pic_path, card.created_at or now,
                 card.metadata_json),
            )
            card_id = cur.lastrowid
            cur.execute("SELECT * FROM custom_cards WHERE id=?", (card_id,))
            row = cur.fetchone()
            return CustomCard(
                id=row["id"], user_id=row["user_id"], ig_username=row["ig_username"],
                display_name=row["display_name"], biography=row["biography"], followers=row["followers"],
                following=row["following"], posts=row["posts"], event_type=EventType(row["event_type"]),
                event_timestamp=ensure_aware(datetime.fromisoformat(row["event_timestamp"])) if row["event_timestamp"] else None,
                elapsed_text=row["elapsed_text"], footer=row["footer"], pic_path=row["pic_path"],
                created_at=ensure_aware(datetime.fromisoformat(row["created_at"])),
                metadata_json=row["metadata_json"],
            )

    def list_custom_cards(self, user_id: int) -> List[CustomCard]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM custom_cards WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
            return [
                CustomCard(
                    id=r["id"], user_id=r["user_id"], ig_username=r["ig_username"],
                    display_name=r["display_name"], biography=r["biography"], followers=r["followers"],
                    following=r["following"], posts=r["posts"], event_type=EventType(r["event_type"]),
                    event_timestamp=ensure_aware(datetime.fromisoformat(r["event_timestamp"])) if r["event_timestamp"] else None,
                    elapsed_text=r["elapsed_text"], footer=r["footer"], pic_path=r["pic_path"],
                    created_at=ensure_aware(datetime.fromisoformat(r["created_at"])),
                    metadata_json=r["metadata_json"],
                )
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self, user_id: int, default_interval: int) -> UserSettings:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row:
                return UserSettings(
                    user_id=user_id, interval_seconds=default_interval,
                    notify_changes=True, notify_availability=True, timezone=None,
                    card_style="default", max_monitors_override=None,
                )
            return UserSettings(
                user_id=row["user_id"], interval_seconds=row["interval_seconds"],
                notify_changes=bool(row["notify_changes"]), notify_availability=bool(row["notify_availability"]),
                timezone=row["timezone"], card_style=row["card_style"],
                max_monitors_override=row["max_monitors_override"],
            )

    def upsert_settings(self, settings: UserSettings) -> None:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO user_settings(user_id, interval_seconds, notify_changes, "
                "notify_availability, timezone, card_style, max_monitors_override) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "interval_seconds=excluded.interval_seconds, "
                "notify_changes=excluded.notify_changes, "
                "notify_availability=excluded.notify_availability, "
                "timezone=excluded.timezone, card_style=excluded.card_style, "
                "max_monitors_override=excluded.max_monitors_override",
                (settings.user_id, settings.interval_seconds, int(settings.notify_changes),
                 int(settings.notify_availability), settings.timezone, settings.card_style,
                 settings.max_monitors_override),
            )

    # ------------------------------------------------------------------
    # Check attempts
    # ------------------------------------------------------------------
    def save_check_attempt(self, attempt: CheckAttempt) -> int:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO check_attempts(monitor_id, attempted_at, status, duration_ms, "
                "availability_state, error_class, error_message) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (attempt.monitor_id, attempt.attempted_at, attempt.status.value,
                 attempt.duration_ms, attempt.availability_state.value, attempt.error_class,
                 attempt.error_message),
            )
            return cur.lastrowid

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def set_metadata(self, key: str, value: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO app_metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_metadata WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default

    # ------------------------------------------------------------------
    # Aggregations for the dashboard
    # ------------------------------------------------------------------
    def aggregate_statistics(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.cursor()
            counts = {}
            for state in ("active", "paused", "stopped"):
                row = cur.execute(
                    "SELECT COUNT(*) AS c FROM monitors WHERE state=?", (state,)
                ).fetchone()
                counts[state] = int(row["c"])
            by_type = {}
            for mtype in ("live", "ban", "unban"):
                row = cur.execute(
                    "SELECT COUNT(*) AS c FROM monitors WHERE monitor_type=? AND state IN ('active','paused')",
                    (mtype,),
                ).fetchone()
                by_type[mtype] = int(row["c"])
            snaps = cur.execute(
                "SELECT COUNT(*) AS c FROM snapshots WHERE availability_state='available'"
            ).fetchone()
            changes = cur.execute("SELECT COUNT(*) AS c FROM change_events").fetchone()
            avail = cur.execute(
                "SELECT COUNT(*) AS c FROM availability_events"
            ).fetchone()
            lookups_ok = cur.execute(
                "SELECT COUNT(*) AS c FROM check_attempts WHERE status='success'"
            ).fetchone()
            lookups_fail = cur.execute(
                "SELECT COUNT(*) AS c FROM check_attempts WHERE status='failed'"
            ).fetchone()
            durations = cur.execute(
                "SELECT duration_ms FROM check_attempts WHERE duration_ms IS NOT NULL ORDER BY id DESC LIMIT 200"
            ).fetchall()
            last_lookup = cur.execute(
                "SELECT duration_ms FROM check_attempts WHERE duration_ms IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_success = cur.execute(
                "SELECT MAX(last_success_at) AS v FROM monitors WHERE last_success_at IS NOT NULL"
            ).fetchone()
            recent_event = cur.execute(
                "SELECT detected_at AS d FROM availability_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            recent_change = cur.execute(
                "SELECT detected_at AS d FROM change_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        dur_values = [int(r["duration_ms"]) for r in durations if r["duration_ms"] is not None]
        avg_dur = (sum(dur_values) / len(dur_values)) if dur_values else 0.0
        return {
            "monitors_active": counts["active"],
            "monitors_paused": counts["paused"],
            "monitors_stopped": counts["stopped"],
            "by_type": by_type,
            "snapshots_verified": int(snaps["c"]),
            "changes_detected": int(changes["c"]),
            "availability_transitions": int(avail["c"]),
            "lookups_success": int(lookups_ok["c"]),
            "lookups_failed": int(lookups_fail["c"]),
            "avg_lookup_ms": avg_dur,
            "last_lookup_ms": int(last_lookup["duration_ms"]) if last_lookup and last_lookup["duration_ms"] else 0,
            "last_success_at": ensure_aware(datetime.fromisoformat(last_success["v"])) if last_success and last_success["v"] else None,
            "recent_event_at": ensure_aware(datetime.fromisoformat(recent_event["d"])) if recent_event and recent_event["d"] else None,
            "recent_change_at": ensure_aware(datetime.fromisoformat(recent_change["d"])) if recent_change and recent_change["d"] else None,
        }

    # ------------------------------------------------------------------
    # History queries
    # ------------------------------------------------------------------
    def list_change_events(
        self,
        telegram_user_id: Optional[int] = None,
        ig_username: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ChangeEvent]:
        sql = (
            "SELECT ce.* FROM change_events ce "
            "JOIN monitors m ON m.id=ce.monitor_id "
            "JOIN users u ON u.id=m.user_id WHERE 1=1"
        )
        params: List[Any] = []
        if telegram_user_id is not None:
            sql += " AND u.telegram_user_id=?"
            params.append(telegram_user_id)
        if ig_username:
            sql += " AND LOWER(ce.ig_username)=?"
            params.append(ig_username.lower())
        if date_from:
            sql += " AND ce.detected_at>=?"
            params.append(date_from)
        if date_to:
            sql += " AND ce.detected_at<=?"
            params.append(date_to)
        sql += " ORDER BY ce.detected_at DESC, ce.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            ChangeEvent(
                id=r["id"], monitor_id=r["monitor_id"], ig_username=r["ig_username"],
                field_name=r["field_name"], old_value=r["old_value"], new_value=r["new_value"],
                diff=r["diff"], detected_at=ensure_aware(datetime.fromisoformat(r["detected_at"])),
                prev_snapshot_id=r["prev_snapshot_id"], new_snapshot_id=r["new_snapshot_id"],
            )
            for r in rows
        ]

    def list_availability_events(
        self,
        telegram_user_id: Optional[int] = None,
        ig_username: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[AvailabilityEvent]:
        sql = (
            "SELECT ae.* FROM availability_events ae "
            "JOIN monitors m ON m.id=ae.monitor_id "
            "JOIN users u ON u.id=m.user_id WHERE 1=1"
        )
        params: List[Any] = []
        if telegram_user_id is not None:
            sql += " AND u.telegram_user_id=?"
            params.append(telegram_user_id)
        if ig_username:
            sql += " AND LOWER(ae.ig_username)=?"
            params.append(ig_username.lower())
        sql += " ORDER BY ae.detected_at DESC, ae.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            AvailabilityEvent(
                id=r["id"], monitor_id=r["monitor_id"], ig_username=r["ig_username"],
                transition=AvailabilityTransition(r["transition"]),
                detected_at=ensure_aware(datetime.fromisoformat(r["detected_at"])),
                first_unavailable_at=ensure_aware(datetime.fromisoformat(r["first_unavailable_at"])) if r["first_unavailable_at"] else None,
                last_unavailable_at=ensure_aware(datetime.fromisoformat(r["last_unavailable_at"])) if r["last_unavailable_at"] else None,
                recovered_at=ensure_aware(datetime.fromisoformat(r["recovered_at"])) if r["recovered_at"] else None,
                last_verified_snapshot_id=r["last_verified_snapshot_id"],
                success_count=r["success_count"], fail_count=r["fail_count"],
                event_code=r["event_code"], metadata=r["metadata"],
            )
            for r in rows
        ]

    def delete_user_data(self, telegram_user_id: int) -> int:
        """Delete all monitoring data for a user. Returns the number of monitors removed."""
        with self.transaction() as cur:
            cur.execute("SELECT id FROM users WHERE telegram_user_id=?", (telegram_user_id,))
            row = cur.fetchone()
            if not row:
                return 0
            user_id = row["id"]
            cur.execute(
                "DELETE FROM change_events WHERE monitor_id IN "
                "(SELECT id FROM monitors WHERE user_id=?)", (user_id,)
            )
            cur.execute(
                "DELETE FROM availability_events WHERE monitor_id IN "
                "(SELECT id FROM monitors WHERE user_id=?)", (user_id,)
            )
            cur.execute(
                "DELETE FROM check_attempts WHERE monitor_id IN "
                "(SELECT id FROM monitors WHERE user_id=?)", (user_id,)
            )
            cur.execute(
                "DELETE FROM snapshots WHERE monitor_id IN "
                "(SELECT id FROM monitors WHERE user_id=?)", (user_id,)
            )
            cur.execute("DELETE FROM monitors WHERE user_id=?", (user_id,))
            removed = cur.rowcount
            cur.execute("DELETE FROM custom_cards WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM users WHERE id=?", (user_id,))
            return removed

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_history_csv(self, telegram_user_id: int) -> str:
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["type", "id", "username", "field/transition", "old", "new", "diff", "detected_at"])
        for ce in self.list_change_events(telegram_user_id=telegram_user_id, limit=10000):
            writer.writerow(["change", ce.id, ce.ig_username, ce.field_name, ce.old_value, ce.new_value, ce.diff, ce.detected_at.isoformat()])
        for ae in self.list_availability_events(telegram_user_id=telegram_user_id, limit=10000):
            writer.writerow(["availability", ae.id, ae.ig_username, ae.transition.value, ae.first_unavailable_at, ae.recovered_at, "", ae.detected_at.isoformat()])
        return buf.getvalue()


_MIGRATIONS: List[Tuple[int, List[str]]] = [
    (
        1,
        [
            """CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                first_seen TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                language_code TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_users_tg ON users(telegram_user_id)",
            """CREATE TABLE IF NOT EXISTS monitors(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ig_username TEXT NOT NULL,
                monitor_type TEXT NOT NULL,
                state TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                last_check_at TEXT,
                last_success_at TEXT,
                last_error TEXT,
                success_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                scheduler_job_id TEXT,
                notes TEXT,
                UNIQUE(user_id, ig_username, monitor_type, state)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_monitors_user ON monitors(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_monitors_state ON monitors(state)",
            "CREATE INDEX IF NOT EXISTS idx_monitors_username ON monitors(ig_username)",
            """CREATE TABLE IF NOT EXISTS snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER REFERENCES monitors(id) ON DELETE CASCADE,
                ig_username TEXT NOT NULL,
                display_name TEXT,
                biography TEXT,
                followers INTEGER,
                following INTEGER,
                posts INTEGER,
                profile_pic_url TEXT,
                profile_pic_path TEXT,
                verified INTEGER,
                is_private INTEGER,
                fetched_at TEXT NOT NULL,
                lookup_duration_ms INTEGER,
                lookup_status TEXT NOT NULL,
                availability_state TEXT NOT NULL,
                error_message TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_snap_monitor ON snapshots(monitor_id, fetched_at)",
            "CREATE INDEX IF NOT EXISTS idx_snap_username ON snapshots(ig_username)",
            """CREATE TABLE IF NOT EXISTS change_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                ig_username TEXT NOT NULL,
                field_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                diff INTEGER,
                detected_at TEXT NOT NULL,
                prev_snapshot_id INTEGER,
                new_snapshot_id INTEGER NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_change_monitor ON change_events(monitor_id, detected_at)",
            """CREATE TABLE IF NOT EXISTS availability_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                ig_username TEXT NOT NULL,
                transition TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                first_unavailable_at TEXT,
                last_unavailable_at TEXT,
                recovered_at TEXT,
                last_verified_snapshot_id INTEGER,
                success_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                event_code TEXT NOT NULL,
                metadata TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_avail_monitor ON availability_events(monitor_id, detected_at)",
            """CREATE TABLE IF NOT EXISTS custom_cards(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ig_username TEXT NOT NULL,
                display_name TEXT,
                biography TEXT,
                followers INTEGER,
                following INTEGER,
                posts INTEGER,
                event_type TEXT NOT NULL,
                event_timestamp TEXT,
                elapsed_text TEXT,
                footer TEXT,
                pic_path TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_cards_user ON custom_cards(user_id)",
            """CREATE TABLE IF NOT EXISTS user_settings(
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                interval_seconds INTEGER NOT NULL,
                notify_changes INTEGER NOT NULL DEFAULT 1,
                notify_availability INTEGER NOT NULL DEFAULT 1,
                timezone TEXT,
                card_style TEXT NOT NULL DEFAULT 'default',
                max_monitors_override INTEGER
            )""",
            """CREATE TABLE IF NOT EXISTS check_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                attempted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                availability_state TEXT NOT NULL,
                error_class TEXT,
                error_message TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_attempts_monitor ON check_attempts(monitor_id, attempted_at)",
        ],
    ),
    # v2: nothing to alter (profile_pic_path shipped in v1). Kept as a no-op
    # placeholder so the migration chain stays monotonic and restart recovery
    # tests can assert a known final version.
    (
        2,
        [
            "SELECT 1",
        ],
    ),
    # v3: store notes + scheduler_job_id helper indexes (idempotent via IF NOT EXISTS).
    (
        3,
        [
            "CREATE INDEX IF NOT EXISTS idx_monitors_job ON monitors(scheduler_job_id)",
            "CREATE INDEX IF NOT EXISTS idx_snap_avail ON snapshots(availability_state)",
        ],
    ),
    # v4: app lifecycle metadata defaults.
    (
        4,
        [
            "INSERT OR IGNORE INTO app_metadata(key, value) VALUES('started_at', '')",
            "INSERT OR IGNORE INTO app_metadata(key, value) VALUES('last_shutdown_at', '')",
        ],
    ),
]


# ======================================================================
# Module: event_history
# ======================================================================
logger = get_logger("history")

PAGE_SIZE = 6


@dataclass
class HistoryPage:
    items: List[dict]
    page: int
    total_pages: int
    total_items: int

    @property
    def is_empty(self) -> bool:
        return not self.items


class EventHistoryService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    def change_events(
        self,
        telegram_user_id: int,
        *,
        username: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 1,
    ) -> HistoryPage:
        total = len(
            self.db.list_change_events(
                telegram_user_id=telegram_user_id, ig_username=username,
                date_from=date_from, date_to=date_to, limit=100000, offset=0,
            )
        )
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * PAGE_SIZE
        rows = self.db.list_change_events(
            telegram_user_id=telegram_user_id, ig_username=username,
            date_from=date_from, date_to=date_to, limit=PAGE_SIZE, offset=offset,
        )
        items = [self._format_change(ev) for ev in rows]
        return HistoryPage(items=items, page=page, total_pages=total_pages, total_items=total)

    def availability_events(
        self,
        telegram_user_id: int,
        *,
        username: Optional[str] = None,
        page: int = 1,
    ) -> HistoryPage:
        total = len(
            self.db.list_availability_events(
                telegram_user_id=telegram_user_id, ig_username=username, limit=100000, offset=0,
            )
        )
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * PAGE_SIZE
        rows = self.db.list_availability_events(
            telegram_user_id=telegram_user_id, ig_username=username,
            limit=PAGE_SIZE, offset=offset,
        )
        items = [self._format_availability(ev) for ev in rows]
        return HistoryPage(items=items, page=page, total_pages=total_pages, total_items=total)

    def custom_cards(self, telegram_user_id: int) -> List[CustomCard]:
        user = self.db.get_user(telegram_user_id)
        if user is None:
            return []
        return self.db.list_custom_cards(user.id)

    # ------------------------------------------------------------------
    @staticmethod
    def _format_change(ev: ChangeEvent) -> dict:
        diff_text = ""
        if ev.diff is not None:
            sign = "+" if ev.diff > 0 else ""
            diff_text = f" ({sign}{ev.diff})"
        return {
            "id": ev.id,
            "type": "change",
            "username": ev.ig_username,
            "field": ev.field_name.replace("_", " ").title(),
            "old": ev.old_value or "—",
            "new": ev.new_value or "—",
            "diff": diff_text,
            "detected_at": ev.detected_at,
            "detected_at_text": format_timestamp(ev.detected_at),
        }

    @staticmethod
    def _format_availability(ev: AvailabilityEvent) -> dict:
        title = "Going into void" if not ev.is_recovery else "Back from void"
        duration = ""
        if ev.first_unavailable_at and ev.recovered_at:
            duration = format_duration((ev.recovered_at - ev.first_unavailable_at).total_seconds())
        return {
            "id": ev.id,
            "type": "availability",
            "username": ev.ig_username,
            "title": title,
            "first_unavailable_at": ev.first_unavailable_at,
            "last_unavailable_at": ev.last_unavailable_at,
            "recovered_at": ev.recovered_at,
            "duration": duration,
            "success_count": ev.success_count,
            "fail_count": ev.fail_count,
            "event_code": ev.event_code,
            "detected_at": ev.detected_at,
            "detected_at_text": format_timestamp(ev.detected_at),
        }

    # ------------------------------------------------------------------
    def render_change_text(self, page: HistoryPage) -> str:
        if page.is_empty:
            return "No profile change events recorded yet."
        lines = [f"📜 Change history — page {page.page}/{page.total_pages}"]
        lines.append("")
        for item in page.items:
            lines.append(
                f"• #{item['id']} @{item['username']} — {item['field']}: "
                f"{item['old']} → {item['new']}{item['diff']}"
            )
            lines.append(f"   {item['detected_at_text']}")
        lines.append("")
        lines.append(f"Total: {page.total_items}")
        return "\n".join(lines)

    def render_availability_text(self, page: HistoryPage) -> str:
        if page.is_empty:
            return "No availability transitions recorded yet."
        lines = [f"📜 Availability history — page {page.page}/{page.total_pages}"]
        lines.append("")
        for item in page.items:
            recovered = format_timestamp(item["recovered_at"]) if item["recovered_at"] else "still open"
            lines.append(
                f"• #{item['id']} @{item['username']} — {item['title']} "
                f"[{item['event_code']}]"
            )
            lines.append(f"   detected {item['detected_at_text']} · recovered {recovered}")
        lines.append("")
        lines.append(f"Total: {page.total_items}")
        return "\n".join(lines)

    def render_custom_cards_text(self, cards: List[CustomCard]) -> str:
        if not cards:
            return "No custom cards created yet."
        lines = [f"🎨 Custom card history ({len(cards)})"]
        lines.append("")
        for card in cards[:20]:
            lines.append(
                f"• #{card.id} @{card.ig_username} — {card.event_type.value} "
                f"· {format_timestamp(card.created_at)}"
            )
        if len(cards) > 20:
            lines.append(f"...and {len(cards) - 20} more")
        lines.append("")
        lines.append("Custom cards are stored separately from verified monitoring events.")
        return "\n".join(lines)

    def export_csv(self, telegram_user_id: int) -> str:
        return self.db.export_history_csv(telegram_user_id)

    def split_event_type(self, value: str) -> Tuple[str, Optional[str]]:
        """Parse a filter like ``change:followers`` into (kind, subfilter)."""
        if ":" in value:
            kind, _, sub = value.partition(":")
            return kind.strip().lower(), sub.strip().lower() or None
        return value.strip().lower(), None


# ======================================================================
# Module: statistics
# ======================================================================
logger = get_logger("statistics")


@dataclass
class DashboardReport:
    total_active_monitors: int
    live_monitors: int
    ban_monitors: int
    unban_monitors: int
    paused_monitors: int
    stopped_monitors: int
    total_verified_snapshots: int
    total_detected_changes: int
    total_availability_transitions: int
    successful_lookups: int
    failed_lookups: int
    uptime_seconds: float
    avg_lookup_ms: float
    last_lookup_ms: int
    last_success_at: Optional[datetime]
    recent_event_at: Optional[datetime]
    recent_change_at: Optional[datetime]
    polling_config: Dict[str, object] = field(default_factory=dict)

    def to_lines(self, timezone: Optional[str] = None) -> List[str]:
        lines = []
        lines.append("📊 Statistics Dashboard")
        lines.append("")
        lines.append(f"• Active monitors: {self.total_active_monitors}")
        lines.append(f"• Live monitors: {self.live_monitors}")
        lines.append(f"• Ban monitors: {self.ban_monitors}")
        lines.append(f"• Unban monitors: {self.unban_monitors}")
        lines.append(f"• Paused monitors: {self.paused_monitors}")
        lines.append(f"• Stopped monitors: {self.stopped_monitors}")
        lines.append("")
        lines.append(f"• Verified snapshots: {self.total_verified_snapshots}")
        lines.append(f"• Detected changes: {self.total_detected_changes}")
        lines.append(f"• Availability transitions: {self.total_availability_transitions}")
        lines.append("")
        total_lookups = self.successful_lookups + self.failed_lookups
        success_rate = (self.successful_lookups / total_lookups * 100) if total_lookups else 0.0
        lines.append(f"• Successful lookups: {self.successful_lookups}")
        lines.append(f"• Failed lookups: {self.failed_lookups}")
        lines.append(f"• Lookup success rate: {success_rate:.1f}%")
        lines.append("")
        lines.append(f"• Application uptime: {format_duration(self.uptime_seconds)}")
        lines.append(f"• Average lookup duration: {format_ms_or_seconds(self.avg_lookup_ms / 1000)}")
        lines.append(f"• Last lookup duration: {format_ms_or_seconds(self.last_lookup_ms / 1000)}")
        lines.append(f"• Last successful check: {format_timestamp(self.last_success_at, timezone)}")
        lines.append(f"• Most recent event: {format_timestamp(self.recent_event_at, timezone)}")
        lines.append(f"• Most recent change: {format_timestamp(self.recent_change_at, timezone)}")
        lines.append("")
        lines.append("Polling configuration:")
        for key, value in self.polling_config.items():
            lines.append(f"  — {key}: {value}")
        return lines


class StatisticsService:
    def __init__(self, db: Database, settings) -> None:
        self.db = db
        self.settings = settings
        self._started_at: Optional[datetime] = None

    def mark_started(self) -> None:
        self._started_at = now_utc()
        self.db.set_metadata("started_at", self._started_at.isoformat())

    def mark_shutdown(self) -> None:
        self.db.set_metadata("last_shutdown_at", now_utc().isoformat())

    def _resolve_started_at(self) -> datetime:
        if self._started_at is not None:
            return self._started_at
        stored = self.db.get_metadata("started_at")
        if stored:
            try:
                parsed = ensure_aware(datetime.fromisoformat(stored))
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                self._started_at = parsed
                return parsed
        self._started_at = now_utc()
        return self._started_at

    def report(self) -> DashboardReport:
        agg = self.db.aggregate_statistics()
        started = self._resolve_started_at()
        uptime = max(0.0, (now_utc() - started).total_seconds())
        polling = {
            "default_interval_seconds": self.settings.check_seconds,
            "max_monitors_per_user": self.settings.max_monitors_per_user,
            "request_timeout": self.settings.request_timeout,
            "max_retries": self.settings.max_retries,
            "backoff_base": self.settings.backoff_base,
            "presets": [p.label for p in self.settings.interval_presets],
        }
        by_type = agg.get("by_type", {})
        return DashboardReport(
            total_active_monitors=agg["monitors_active"],
            live_monitors=by_type.get("live", 0),
            ban_monitors=by_type.get("ban", 0),
            unban_monitors=by_type.get("unban", 0),
            paused_monitors=agg["monitors_paused"],
            stopped_monitors=agg["monitors_stopped"],
            total_verified_snapshots=agg["snapshots_verified"],
            total_detected_changes=agg["changes_detected"],
            total_availability_transitions=agg["availability_transitions"],
            successful_lookups=agg["lookups_success"],
            failed_lookups=agg["lookups_failed"],
            uptime_seconds=uptime,
            avg_lookup_ms=agg["avg_lookup_ms"],
            last_lookup_ms=agg["last_lookup_ms"],
            last_success_at=agg["last_success_at"],
            recent_event_at=agg["recent_event_at"],
            recent_change_at=agg["recent_change_at"],
            polling_config=polling,
        )

    def monitor_breakdown(self, telegram_user_id: int) -> List[dict]:
        rows = []
        for summary in self.db.list_monitors(telegram_user_id):
            rows.append({
                "username": summary.ig_username,
                "type": summary.monitor_type.value,
                "state": summary.state.value,
                "interval_seconds": summary.interval_seconds,
                "started_at": summary.started_at,
                "last_success_at": summary.last_success_at,
                "success_count": summary.success_count,
                "fail_count": summary.fail_count,
            })
        return rows

    def lookup_statistics(self) -> Dict[str, float]:
        agg = self.db.aggregate_statistics()
        total = agg["lookups_success"] + agg["lookups_failed"]
        return {
            "success": float(agg["lookups_success"]),
            "failed": float(agg["lookups_failed"]),
            "total": float(total),
            "success_rate": (agg["lookups_success"] / total * 100) if total else 0.0,
            "avg_lookup_ms": float(agg["avg_lookup_ms"]),
            "last_lookup_ms": float(agg["last_lookup_ms"]),
        }


# ======================================================================
# Module: monitoring_engine
# ======================================================================
logger = get_logger("engine")

# Fields compared for live change detection. Numeric fields carry a diff.
NUMERIC_FIELDS = ("followers", "following", "posts")
TEXT_FIELDS = ("display_name", "biography", "profile_pic_url")
BOOL_FIELDS = ("verified", "is_private")


@dataclass(frozen=True)
class ChangeDescription:
    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
    diff: Optional[int]


class ChangeDetector:
    """Pure comparison between two snapshots."""

    @staticmethod
    def _fmt(value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)

    def compare(self, prev: Snapshot, new: Snapshot) -> List[ChangeDescription]:
        if prev is None or new is None:
            return []
        changes: List[ChangeDescription] = []
        prev_map = prev.comparable()
        new_map = new.comparable()

        for fname in NUMERIC_FIELDS:
            old = prev_map.get(fname)
            cur = new_map.get(fname)
            if old is None or cur is None:
                continue  # comparison data missing -> never report
            if int(old) != int(cur):
                changes.append(
                    ChangeDescription(
                        field_name=fname,
                        old_value=str(old),
                        new_value=str(cur),
                        diff=int(cur) - int(old),
                    )
                )
        for fname in TEXT_FIELDS:
            old = prev_map.get(fname)
            cur = new_map.get(fname)
            if old is None or cur is None:
                continue
            if str(old) != str(cur):
                changes.append(
                    ChangeDescription(
                        field_name=fname, old_value=str(old), new_value=str(cur), diff=None
                    )
                )
        for fname in BOOL_FIELDS:
            old = prev_map.get(fname)
            cur = new_map.get(fname)
            if old is None or cur is None:
                continue
            if bool(old) != bool(cur):
                changes.append(
                    ChangeDescription(
                        field_name=fname,
                        old_value=self._fmt(old),
                        new_value=self._fmt(cur),
                        diff=None,
                    )
                )
        return changes


@dataclass
class EventPayload:
    """Everything the notifier needs to render and send a card."""

    event_kind: str  # 'change' | 'going_into_void' | 'back_from_void'
    monitor_id: int
    telegram_user_id: int
    ig_username: str
    monitor_type: MonitorType
    changes: List[ChangeDescription] = field(default_factory=list)
    prev_snapshot: Optional[Snapshot] = None
    new_snapshot: Optional[Snapshot] = None
    availability_event: Optional[AvailabilityEvent] = None
    detected_at: datetime = field(default_factory=now_utc)
    last_verified_snapshot: Optional[Snapshot] = None


Notifier = Callable[[EventPayload], Awaitable[None]]


class MonitoringEngine:
    """Coordinates lookups, persistence, and event emission."""

    def __init__(
        self,
        db: Database,
        instagram: InstagramService,
        settings,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.db = db
        self.instagram = instagram
        self.settings = settings
        self.notifier = notifier
        self.detector = ChangeDetector()

    # ------------------------------------------------------------------
    async def check_monitor(self, monitor_id: int) -> Optional[EventPayload]:
        """Run one polling cycle for a monitor. Returns the payload if an event fired."""
        monitor = self.db.get_monitor(monitor_id)
        if monitor is None or monitor.state not in (MonitorState.ACTIVE, MonitorState.PAUSED):
            return None
        if monitor.state == MonitorState.PAUSED:
            return None

        result: LookupResult = await self.instagram.lookup(monitor.ig_username)
        fetched_at = ensure_aware(result.fetched_at) if result.fetched_at else now_utc()
        attempt = CheckAttempt(
            id=0, monitor_id=monitor.id, attempted_at=fetched_at,
            status=result.status, duration_ms=result.duration_ms,
            availability_state=result.availability,
            error_class=result.error_class,
            error_message=result.error_message,
        )
        self.db.save_check_attempt(attempt)

        prev_success = self.db.latest_successful_snapshot(monitor.id)

        snapshot = Snapshot(
            id=0, monitor_id=monitor.id, ig_username=result.username,
            display_name=result.profile.display_name if result.profile else None,
            biography=result.profile.biography if result.profile else None,
            followers=result.profile.followers if result.profile else None,
            following=result.profile.following if result.profile else None,
            posts=result.profile.posts if result.profile else None,
            profile_pic_url=result.profile.profile_pic_url if result.profile else None,
            profile_pic_path=result.pic_path,
            verified=result.profile.verified if result.profile else None,
            is_private=result.profile.is_private if result.profile else None,
            fetched_at=fetched_at, lookup_duration_ms=result.duration_ms,
            lookup_status=result.status, availability_state=result.availability,
            error_message=result.error_message,
        )
        saved = self.db.save_snapshot(snapshot)

        success_delta = 1 if result.status == LookupStatus.SUCCESS else 0
        fail_delta = 0 if result.status == LookupStatus.SUCCESS else 1
        last_success = fetched_at if result.status == LookupStatus.SUCCESS else None
        last_error = result.error_message if result.status != LookupStatus.SUCCESS else None
        self.db.update_monitor_check(
            monitor.id, last_check=fetched_at, last_success=last_success,
            success_delta=success_delta, fail_delta=fail_delta, last_error=last_error,
        )

        payload = await self._dispatch_events(monitor, result, saved, prev_success)
        return payload

    # ------------------------------------------------------------------
    async def _dispatch_events(
        self,
        monitor: Monitor,
        result: LookupResult,
        new_snapshot: Snapshot,
        prev_success: Optional[Snapshot],
    ) -> Optional[EventPayload]:
        state = result.availability
        user = self.db.get_user_by_id(monitor.user_id)
        tg_user_id = user.telegram_user_id if user else 0

        # 1) Availability transitions — only explicit states count.
        if is_ban_capable(state):
            return await self._handle_unavailable(monitor, new_snapshot, prev_success, tg_user_id)
        if is_recovery_capable(state):
            return await self._handle_available(monitor, new_snapshot, prev_success, tg_user_id)

        # RATE_LIMITED / NETWORK_ERROR / UNKNOWN -> never an event.
        logger.info(
            "monitor %s skipped event state=%s reason=%s",
            monitor.id, state.value, result.classification_reason,
        )
        return None

    # ------------------------------------------------------------------
    async def _handle_unavailable(
        self,
        monitor: Monitor,
        new_snapshot: Snapshot,
        prev_success: Optional[Snapshot],
        tg_user_id: int,
    ) -> Optional[EventPayload]:
        # Only fire GOING_INTO_VOID when we previously had a verified snapshot
        # (the brief: transition from successfully accessible to unavailable).
        open_event = self.db.latest_open_void_event(monitor.id)
        if open_event is not None:
            # Already in the void — keep the open event current, no new card.
            self.db.bump_void_last_unavailable(open_event.id, new_snapshot.fetched_at)
            logger.info("monitor %s still unavailable, void event %s open", monitor.id, open_event.id)
            return None
        if prev_success is None:
            logger.info(
                "monitor %s unavailable but no verified baseline -> no void event",
                monitor.id,
            )
            return None

        now = now_utc()
        event = AvailabilityEvent(
            id=0, monitor_id=monitor.id, ig_username=monitor.ig_username,
            transition=AvailabilityTransition.GOING_INTO_VOID, detected_at=now,
            first_unavailable_at=new_snapshot.fetched_at,
            last_unavailable_at=new_snapshot.fetched_at, recovered_at=None,
            last_verified_snapshot_id=prev_success.id,
            success_count=0, fail_count=1,
            event_code=f"VOID-{uuid.uuid4().hex[:8].upper()}",
            metadata=None,
        )
        saved_event = self.db.save_availability_event(event)
        payload = EventPayload(
            event_kind="going_into_void", monitor_id=monitor.id,
            telegram_user_id=tg_user_id, ig_username=monitor.ig_username,
            monitor_type=monitor.monitor_type, changes=[],
            prev_snapshot=prev_success, new_snapshot=new_snapshot,
            availability_event=saved_event, detected_at=now,
            last_verified_snapshot=prev_success,
        )
        await self._notify(payload)
        return payload

    # ------------------------------------------------------------------
    async def _handle_available(
        self,
        monitor: Monitor,
        new_snapshot: Snapshot,
        prev_success: Optional[Snapshot],
        tg_user_id: int,
    ) -> Optional[EventPayload]:
        payload: Optional[EventPayload] = None

        # Recovery: a previous GOING_INTO_VOID event that is still open.
        open_event = self.db.latest_open_void_event(monitor.id)
        if open_event is not None:
            now = now_utc()
            self.db.mark_void_event_recovered(open_event.id, now)
            recovery = AvailabilityEvent(
                id=0, monitor_id=monitor.id, ig_username=monitor.ig_username,
                transition=AvailabilityTransition.BACK_FROM_VOID, detected_at=now,
                first_unavailable_at=open_event.first_unavailable_at,
                last_unavailable_at=open_event.last_unavailable_at or open_event.detected_at,
                recovered_at=now, last_verified_snapshot_id=open_event.last_verified_snapshot_id,
                success_count=1, fail_count=0,
                event_code=f"RECOVER-{uuid.uuid4().hex[:8].upper()}",
                metadata=open_event.event_code,
            )
            saved_recovery = self.db.save_availability_event(recovery)
            payload = EventPayload(
                event_kind="back_from_void", monitor_id=monitor.id,
                telegram_user_id=tg_user_id, ig_username=monitor.ig_username,
                monitor_type=monitor.monitor_type, changes=[],
                prev_snapshot=prev_success, new_snapshot=new_snapshot,
                availability_event=saved_recovery, detected_at=now,
                last_verified_snapshot=prev_success,
            )
            await self._notify(payload)

        # Live change detection only for LIVE monitors with a prior snapshot.
        if monitor.monitor_type == MonitorType.LIVE and prev_success is not None and prev_success.id != new_snapshot.id:
            changes = self.detector.compare(prev_success, new_snapshot)
            for change in changes:
                evt = ChangeEvent(
                    id=0, monitor_id=monitor.id, ig_username=monitor.ig_username,
                    field_name=change.field_name, old_value=change.old_value,
                    new_value=change.new_value, diff=change.diff,
                    detected_at=new_snapshot.fetched_at, prev_snapshot_id=prev_success.id,
                    new_snapshot_id=new_snapshot.id,
                )
                self.db.save_change_event(evt)
            if changes and payload is None:
                payload = EventPayload(
                    event_kind="change", monitor_id=monitor.id,
                    telegram_user_id=tg_user_id, ig_username=monitor.ig_username,
                    monitor_type=monitor.monitor_type, changes=changes,
                    prev_snapshot=prev_success, new_snapshot=new_snapshot,
                    availability_event=None, detected_at=new_snapshot.fetched_at,
                    last_verified_snapshot=prev_success,
                )
                await self._notify(payload)
            elif changes and payload is not None:
                # Recovery with simultaneous changes — fold changes into payload.
                payload.changes = list(changes)
        return payload

    # ------------------------------------------------------------------
    async def _notify(self, payload: EventPayload) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(payload)
        except Exception as exc:
            logger.warning("notifier failed: %s", exc.__class__.__name__)


# ======================================================================
# Module: scheduler
# ======================================================================
logger = get_logger("scheduler")

# Initial spread delay so a freshly started fleet does not hit Instagram at
# the exact same millisecond.
INITIAL_SPREAD_SECONDS = 3
MIN_INTERVAL_SECONDS = 60


class MonitorScheduler:
    def __init__(self, db: Database, engine: MonitoringEngine, settings) -> None:
        self.db = db
        self.engine = engine
        self.settings = settings
        self._tasks: Dict[int, asyncio.Task] = {}
        self._stopping = False
        self._started = False

    # ------------------------------------------------------------------
    async def start(self) -> int:
        """Reload active monitors from the database and schedule them."""
        self._stopping = False
        monitors = self.db.list_all_active_monitors()
        scheduled = 0
        for monitor in monitors:
            if monitor.state == MonitorState.ACTIVE:
                self.schedule(monitor.id, monitor.interval_seconds)
                scheduled += 1
        self._started = True
        logger.info("scheduler recovered %d active monitors", scheduled)
        return scheduled

    # ------------------------------------------------------------------
    def schedule(self, monitor_id: int, interval_seconds: int) -> None:
        """Schedule a monitor. Idempotent: no duplicate task is created."""
        existing = self._tasks.get(monitor_id)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("schedule called outside a running loop; monitor=%s", monitor_id)
            return
        task = loop.create_task(self._run_loop(monitor_id, max(MIN_INTERVAL_SECONDS, interval_seconds)))
        self._tasks[monitor_id] = task
        logger.info("scheduled monitor %s every %ss", monitor_id, interval_seconds)

    def unschedule(self, monitor_id: int) -> None:
        task = self._tasks.pop(monitor_id, None)
        if task is not None and not task.done():
            task.cancel()

    def reschedule(self, monitor_id: int, interval_seconds: int) -> None:
        self.unschedule(monitor_id)
        self.db.change_interval(monitor_id, max(MIN_INTERVAL_SECONDS, interval_seconds))
        self.schedule(monitor_id, max(MIN_INTERVAL_SECONDS, interval_seconds))

    def pause(self, monitor_id: int) -> None:
        self.db.update_monitor_state(monitor_id, MonitorState.PAUSED)
        self.unschedule(monitor_id)

    def resume(self, monitor_id: int, interval_seconds: Optional[int] = None) -> None:
        monitor = self.db.get_monitor(monitor_id)
        if monitor is None:
            return
        self.db.update_monitor_state(monitor_id, MonitorState.ACTIVE)
        self.schedule(monitor_id, interval_seconds or monitor.interval_seconds)

    def stop(self, monitor_id: int) -> None:
        self.db.stop_monitor(monitor_id)
        self.unschedule(monitor_id)

    def stop_all_for_user(self, telegram_user_id: int) -> int:
        """Stop every active/paused monitor owned by a Telegram user.

        Updates persisted state AND cancels the in-flight asyncio tasks so a
        long sleep does not keep a 'removed' monitor alive until its next tick.
        """
        monitors = self.db.list_monitors(telegram_user_id)
        stopped = 0
        for summary in monitors:
            monitor = self.db.get_monitor(summary.id)
            if monitor is None:
                continue
            if monitor.state in (MonitorState.ACTIVE, MonitorState.PAUSED):
                self.db.stop_monitor(monitor.id)
                self.unschedule(monitor.id)
                stopped += 1
        logger.info("stopped %s monitors for user %s", stopped, telegram_user_id)
        return stopped

    # ------------------------------------------------------------------
    async def _run_loop(self, monitor_id: int, interval_seconds: int) -> None:
        try:
            await asyncio.sleep(INITIAL_SPREAD_SECONDS)
            while not self._stopping:
                monitor = self.db.get_monitor(monitor_id)
                if monitor is None or monitor.state != MonitorState.ACTIVE:
                    logger.info("monitor %s no longer active; loop exiting", monitor_id)
                    return
                try:
                    await self.engine.check_monitor(monitor_id)
                except Exception as exc:
                    logger.warning("check raised monitor=%s: %s", monitor_id, exc.__class__.__name__)
                # Re-read so interval changes take effect on the next cycle.
                monitor = self.db.get_monitor(monitor_id)
                if monitor is None or monitor.state != MonitorState.ACTIVE:
                    return
                await asyncio.sleep(max(MIN_INTERVAL_SECONDS, monitor.interval_seconds))
        except asyncio.CancelledError:
            logger.info("monitor %s task cancelled", monitor_id)
            raise

    # ------------------------------------------------------------------
    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    def is_scheduled(self, monitor_id: int) -> bool:
        task = self._tasks.get(monitor_id)
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False
        logger.info("scheduler shut down")


# ======================================================================
# Module: security
# ======================================================================
@dataclass
class RateBucket:
    window_seconds: int
    max_calls: int


class RateLimiter:
    """A simple sliding-window in-memory rate limiter keyed by ``(user_id, action)``.

    The limiter is used to throttle expensive lookups per Telegram user so that
    one user cannot exhaust the Instagram quota for everyone.
    """

    def __init__(self, default_bucket: RateBucket = RateBucket(60, 20)) -> None:
        self._default = default_bucket
        self._buckets: Dict[str, RateBucket] = {}
        self._hits: Dict[Tuple[int, str], Deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def configure(self, action: str, bucket: RateBucket) -> None:
        with self._lock:
            self._buckets[action] = bucket

    def check(self, user_id: int, action: str) -> Tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``."""
        bucket = self._buckets.get(action, self._default)
        now = time.monotonic()
        key = (user_id, action)
        with self._lock:
            queue = self._hits[key]
            cutoff = now - bucket.window_seconds
            while queue and queue[0] < cutoff:
                queue.popleft()
            if len(queue) >= bucket.max_calls:
                retry_after = int(bucket.window_seconds - (now - queue[0])) + 1
                return False, max(1, retry_after)
            queue.append(now)
            return True, 0

    def reset(self, user_id: int) -> None:
        with self._lock:
            keys_to_drop = [k for k in self._hits if k[0] == user_id]
            for k in keys_to_drop:
                self._hits.pop(k, None)


def mask_token(value: str) -> str:
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def user_can_create_monitor(current_count: int, limit: int) -> Tuple[bool, str]:
    if limit <= 0:
        return True, ""
    if current_count >= limit:
        return False, f"You already have {current_count} monitors (limit: {limit})."
    return True, ""


def is_owner(owner_id: int, actor_id: int) -> bool:
    """Return True only when the actor matches the resource owner."""
    return owner_id == actor_id


def safe_error_message(exc: BaseException) -> str:
    """Return a user-facing error string that never contains credentials."""
    text = str(exc) or exc.__class__.__name__
    # Strip anything that looks like a token or session id just in case.
    lowered = text.lower()
    for token in ("sessionid", "authorization", "bearer", "token="):
        if token in lowered:
            return "An authentication detail leaked into the error and was suppressed."
    if len(text) > 280:
        text = text[:280] + "…"
    return text


# ======================================================================
# Module: conversations
# ======================================================================
class LiveProfileStates(int, Enum):
    ENTER_USERNAME = 10
    SHOW_PROFILE = 11


class MonitorCreateStates(int, Enum):
    ENTER_USERNAME = 20
    SELECT_INTERVAL = 21
    CONFIRM = 22


class CustomCardStates(int, Enum):
    STUDIO = 30
    EDIT_FIELD = 31
    INPUT_FIELD = 32
    SELECT_EVENT_TYPE = 33
    PREVIEW = 34


class RemoveStates(int, Enum):
    SELECT = 40
    CONFIRM = 41


class HistoryFilterStates(int, Enum):
    ENTER_USERNAME = 50
    ENTER_DATE = 51


class SettingsStates(int, Enum):
    INPUT = 60


# Termination sentinels.
END = -1
CANCEL = -2


@dataclass
class CustomCardDraft:
    """In-progress custom card buffer held in ``context.user_data``."""

    username: Optional[str] = None
    display_name: Optional[str] = None
    biography: Optional[str] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    posts: Optional[int] = None
    event_type: str = "custom_live"
    event_timestamp: Optional[datetime] = None
    elapsed_text: Optional[str] = None
    footer: Optional[str] = None
    avatar_path: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    FIELD_LABELS = {
        "username": "Username",
        "display_name": "Display name",
        "biography": "Biography",
        "followers": "Followers",
        "following": "Following",
        "posts": "Posts",
        "event_type": "Event type",
        "event_timestamp": "Event timestamp",
        "elapsed_text": "Elapsed duration",
        "footer": "Footer",
        "avatar": "Avatar (upload an image)",
    }

    NUMERIC_FIELDS = ("followers", "following", "posts")

    def set_field(self, key: str, value):
        if key == "avatar":
            self.avatar_path = value
        elif key in self.NUMERIC_FIELDS:
            self.__dict__[key] = value
        else:
            self.__dict__[key] = value

    def reset_field(self, key: str) -> None:
        if key == "avatar":
            self.avatar_path = None
        elif key in self.NUMERIC_FIELDS:
            self.__dict__[key] = None
        elif key == "event_type":
            self.event_type = "custom_live"
        else:
            self.__dict__[key] = None

    def is_complete(self) -> bool:
        return bool(self.username)

    def summary(self) -> str:
        lines = ["🎨 Custom Card Studio — current draft", ""]
        for key, label in self.FIELD_LABELS.items():
            value = getattr(self, key, None) if key != "avatar" else self.avatar_path
            if key == "event_type":
                value = self.event_type
            display = "—" if value in (None, "") else str(value)
            lines.append(f"• {label}: {display}")
        return "\n".join(lines)


# ======================================================================
# Module: keyboards
# ======================================================================
# ---------------------------------------------------------------------------
# Main reply-keyboard menu (persistent)
# ---------------------------------------------------------------------------
MAIN_MENU_LABELS: Sequence[Sequence[str]] = (
    ("📸 Live Profile", "👁 Live Monitor"),
    ("🚫 Ban Monitor", "🔓 Unban Monitor"),
    ("🎨 Custom Card Studio", "📋 Active Watchlist"),
    ("📊 Statistics", "📜 Event History"),
    ("⏱ Time & Duration", "🗑 Remove Monitor"),
    ("🔄 Refresh Data", "⚙️ Settings"),
    ("❓ Help",),
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(label) for label in row] for row in MAIN_MENU_LABELS]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


# ---------------------------------------------------------------------------
# Callback data helpers
# ---------------------------------------------------------------------------
def cb(prefix: str, *parts: object) -> str:
    """Build a callback_data string ``prefix:p1:p2:...`` truncated to 64 bytes."""
    tokens = [str(prefix)] + [str(p) for p in parts]
    data = ":".join(tokens)
    return data[:64]


def parse_cb(data: str) -> List[str]:
    return data.split(":")


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------
def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data=cb("nav", "home"))],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖️ Cancel", callback_data=cb("nav", "cancel"))],
    ])


def live_profile_actions(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Profile", callback_data=cb("lp", "refresh", username)),
         InlineKeyboardButton("👁 Start Live Monitor", callback_data=cb("mon", "start", "live", username))],
        [InlineKeyboardButton("🚫 Start Ban Monitor", callback_data=cb("mon", "start", "ban", username)),
         InlineKeyboardButton("🔓 Start Unban Monitor", callback_data=cb("mon", "start", "unban", username))],
        [InlineKeyboardButton("🎨 Create Custom Card", callback_data=cb("cc", "fromlive", username)),
         InlineKeyboardButton("📄 View Full Biography", callback_data=cb("lp", "bio", username))],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data=cb("nav", "home"))],
    ])


def interval_keyboard(prefix: str = "int", username: str = "", mtype: str = "") -> InlineKeyboardMarkup:
    rows = []
    for preset in INTERVAL_PRESETS:
        rows.append([InlineKeyboardButton(preset.label, callback_data=cb(prefix, preset.seconds, username, mtype))])
    rows.append([InlineKeyboardButton("⬅️ Cancel", callback_data=cb("nav", "cancel"))])
    return InlineKeyboardMarkup(rows)


def confirm_monitor_keyboard(username: str, mtype: str, interval: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Start", callback_data=cb("mon", "confirm", mtype, username, interval))],
        [InlineKeyboardButton("↩️ Change Interval", callback_data=cb("mon", "interval", mtype, username)),
         InlineKeyboardButton("✖️ Cancel", callback_data=cb("nav", "cancel"))],
    ])


def monitor_detail_keyboard(monitor_id: int, mtype: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⏸ Pause", callback_data=cb("wl", "pause", monitor_id)),
         InlineKeyboardButton("▶️ Resume", callback_data=cb("wl", "resume", monitor_id))],
        [InlineKeyboardButton("🔄 Refresh Now", callback_data=cb("wl", "refresh", monitor_id)),
         InlineKeyboardButton("🖼 View Latest Snapshot", callback_data=cb("snap", "latest", monitor_id))],
        [InlineKeyboardButton("🖼 View Previous Snapshot", callback_data=cb("snap", "prev", monitor_id)),
         InlineKeyboardButton("📜 View Change History", callback_data=cb("hist", "for", monitor_id))],
        [InlineKeyboardButton("⏱ Change Interval", callback_data=cb("wl", "interval", monitor_id)),
         InlineKeyboardButton("⏹ Stop Monitor", callback_data=cb("wl", "stop", monitor_id))],
        [InlineKeyboardButton("⬅️ Back to Watchlist", callback_data=cb("wl", "list")),
         InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ]
    if mtype == "ban":
        rows.insert(2, [InlineKeyboardButton("👁 View Last Verified Snapshot", callback_data=cb("snap", "verified", monitor_id)),
                        InlineKeyboardButton("📜 View Event History", callback_data=cb("hist", "avail", monitor_id))])
    return InlineKeyboardMarkup(rows)


def watchlist_pagination_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=cb("wl", "page", page - 1)))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data=cb("noop")))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=cb("wl", "page", page + 1)))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=cb("wl", "list")),
        InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home")),
    ])
    return InlineKeyboardMarkup(rows)


def watchlist_item_keyboard(monitor_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Open Detail", callback_data=cb("wl", "detail", monitor_id))],
    ])


def history_pagination_keyboard(kind: str, page: int, total_pages: int, username: Optional[str] = None) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=cb("hist", "page", kind, page - 1, username or "")))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data=cb("noop")))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=cb("hist", "page", kind, page + 1, username or "")))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=cb("hist", "view", kind)),
        InlineKeyboardButton("⬅️ Filter Menu", callback_data=cb("hist", "menu")),
        InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home")),
    ])
    return InlineKeyboardMarkup(rows)


def history_filter_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Change events", callback_data=cb("hist", "view", "change")),
         InlineKeyboardButton("🌀 Availability events", callback_data=cb("hist", "view", "avail"))],
        [InlineKeyboardButton("🎨 Custom cards", callback_data=cb("hist", "custom")),
         InlineKeyboardButton("📥 Export CSV", callback_data=cb("hist", "export"))],
        [InlineKeyboardButton("🔍 Filter by username", callback_data=cb("hist", "askuser")),
         InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ])


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Dashboard", callback_data=cb("stat", "refresh")),
         InlineKeyboardButton("🧩 Monitor Breakdown", callback_data=cb("stat", "breakdown"))],
        [InlineKeyboardButton("🔍 Lookup Statistics", callback_data=cb("stat", "lookups")),
         InlineKeyboardButton("📜 Recent Events", callback_data=cb("stat", "recent"))],
        [InlineKeyboardButton("⚠️ View Errors", callback_data=cb("stat", "errors")),
         InlineKeyboardButton("📥 Export Statistics", callback_data=cb("stat", "export"))],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data=cb("nav", "home"))],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ Monitoring interval presets", callback_data=cb("set", "interval")),
         InlineKeyboardButton("🔔 Notification preferences", callback_data=cb("set", "notify"))],
        [InlineKeyboardButton("🌍 Timezone / display format", callback_data=cb("set", "timezone")),
         InlineKeyboardButton("🎨 Card style settings", callback_data=cb("set", "cardstyle"))],
        [InlineKeyboardButton("🔢 Maximum monitor count", callback_data=cb("set", "maxmon")),
         InlineKeyboardButton("⏸ Pause all / ▶️ Resume all", callback_data=cb("set", "pauseall"))],
        [InlineKeyboardButton("📥 Export personal data", callback_data=cb("set", "export")),
         InlineKeyboardButton("🗑 Delete personal data", callback_data=cb("set", "delete"))],
        [InlineKeyboardButton("🔑 Session status", callback_data=cb("set", "session")),
         InlineKeyboardButton("ℹ️ Application status", callback_data=cb("set", "appstatus"))],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data=cb("nav", "home"))],
    ])


def custom_card_studio_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Custom Card", callback_data=cb("cc", "new")),
         InlineKeyboardButton("📋 My Custom Cards", callback_data=cb("hist", "custom"))],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data=cb("nav", "home"))],
    ])


def custom_card_editor_keyboard() -> InlineKeyboardMarkup:
    fields = [
        ("username", "Username"), ("display_name", "Display name"), ("biography", "Biography"),
        ("followers", "Followers"), ("following", "Following"), ("posts", "Posts"),
        ("event_type", "Event type"), ("event_timestamp", "Event timestamp"),
        ("elapsed_text", "Elapsed duration"), ("footer", "Footer"), ("avatar", "Avatar"),
    ]
    rows = []
    pair = []
    for key, label in fields:
        pair.append(InlineKeyboardButton(f"✏️ {label}", callback_data=cb("cc", "edit", key)))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        InlineKeyboardButton("👁 Preview Card", callback_data=cb("cc", "preview")),
        InlineKeyboardButton("🔄 Regenerate", callback_data=cb("cc", "regen")),
    ])
    rows.append([
        InlineKeyboardButton("💾 Save Card", callback_data=cb("cc", "save")),
        InlineKeyboardButton("♻️ Reset Field", callback_data=cb("cc", "reset")),
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Back to Studio", callback_data=cb("cc", "studio")),
        InlineKeyboardButton("✖️ Cancel", callback_data=cb("nav", "cancel")),
    ])
    return InlineKeyboardMarkup(rows)


def custom_card_event_type_keyboard() -> InlineKeyboardMarkup:
    options = [
        ("custom_unban", "Custom Unban Card"),
        ("custom_ban", "Custom Ban Card"),
        ("custom_live", "Custom Live Profile Card"),
        ("custom_change", "Custom Profile Change Card"),
    ]
    rows = [[InlineKeyboardButton(label, callback_data=cb("cc", "setevent", key))] for key, label in options]
    rows.append([InlineKeyboardButton("⬅️ Back to editor", callback_data=cb("cc", "editor"))])
    return InlineKeyboardMarkup(rows)


def remove_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Remove One", callback_data=cb("rm", "list")),
         InlineKeyboardButton("🗑 Remove All", callback_data=cb("rm", "allconfirm"))],
        [InlineKeyboardButton("✖️ Cancel", callback_data=cb("nav", "cancel")),
         InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ])


def remove_confirm_keyboard(monitor_id: int, kind: str = "one") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Removal", callback_data=cb("rm", "confirm", monitor_id, kind))],
        [InlineKeyboardButton("↩️ Undo (re-activate)", callback_data=cb("rm", "undo", monitor_id)),
         InlineKeyboardButton("✖️ Cancel", callback_data=cb("nav", "cancel"))],
    ])


def remove_all_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ YES, REMOVE EVERYTHING", callback_data=cb("rm", "confirm", "all", "all"))],
        [InlineKeyboardButton("✖️ Cancel", callback_data=cb("nav", "cancel")),
         InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ])


def time_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=cb("time", "refresh")),
         InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ])


def void_card_keyboard(monitor_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Continue Monitoring", callback_data=cb("wl", "resume", monitor_id)),
         InlineKeyboardButton("🔄 Check Now", callback_data=cb("wl", "refresh", monitor_id))],
        [InlineKeyboardButton("🖼 View Last Verified Snapshot", callback_data=cb("snap", "verified", monitor_id)),
         InlineKeyboardButton("📜 View Event History", callback_data=cb("hist", "avail", monitor_id))],
        [InlineKeyboardButton("⏹ Stop Monitor", callback_data=cb("wl", "stop", monitor_id)),
         InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ])


def recovery_card_keyboard(monitor_id: int, username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Profile", callback_data=cb("lp", "refresh", username)),
         InlineKeyboardButton("👁 Start Live Monitor", callback_data=cb("mon", "start", "live", username))],
        [InlineKeyboardButton("📜 View Recovery Timeline", callback_data=cb("hist", "avail", monitor_id)),
         InlineKeyboardButton("🖼 View Last Unavailable Snapshot", callback_data=cb("snap", "prev", monitor_id))],
        [InlineKeyboardButton("▶️ Continue Monitoring", callback_data=cb("wl", "resume", monitor_id)),
         InlineKeyboardButton("⏹ Stop Monitor", callback_data=cb("wl", "stop", monitor_id))],
        [InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ])


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Home", callback_data=cb("nav", "home"))],
    ])


# ======================================================================
# Module: telegram_handlers
# ======================================================================
logger = get_logger("telegram")

MAIN_MENU_TEXT = (
    "👋 *Welcome to ARIX IG Watchdog Ultimate*\n\n"
    "Monitor Instagram profiles for follower changes, availability transitions, "
    "and recoveries. Pick an option below or use a command like /live, /monitor, "
    "/banmonitor, /unbanmonitor, /customcard."
)

HELP_TEXT = (
    "❓ *ARIX IG Watchdog — Help*\n\n"
    "*Commands*\n"
    "/start — open the main menu\n"
    "/menu — show the persistent menu\n"
    "/live — look up a live profile\n"
    "/monitor — start a live monitor\n"
    "/banmonitor — start a ban (availability) monitor\n"
    "/unbanmonitor — start an unban (recovery) monitor\n"
    "/customcard — open the Custom Card Studio\n"
    "/watchlist — your active monitors\n"
    "/stats — statistics dashboard\n"
    "/history — event history\n"
    "/duration — time & duration tools\n"
    "/refresh — refresh a monitor\n"
    "/pause — pause a monitor\n"
    "/resume — resume a monitor\n"
    "/remove — remove a monitor\n"
    "/settings — settings\n"
    "/status — application status\n"
    "/help — this help\n"
    "/cancel — cancel the current input flow\n\n"
    "*Safety notes*\n"
    "• Only explicit Instagram unavailability triggers a *Going into Void* card.\n"
    "• Rate limits, network errors, and login challenges are *never* classified as bans.\n"
    "• Custom cards are stored separately from verified monitoring events."
)


# ---------------------------------------------------------------------------
# Notifier: renders + sends cards when the engine emits an EventPayload
# ---------------------------------------------------------------------------
class TelegramNotifier:
    """Async callable wired into the monitoring engine as ``notifier``."""

    def __init__(self, bot, renderer: CardRenderer, db: Database) -> None:
        self.bot = bot
        self.renderer = renderer
        self.db = db

    async def __call__(self, payload: EventPayload) -> None:
        try:
            await self._handle(payload)
        except Exception as exc:
            logger.warning("notify failed: %s", exc.__class__.__name__)

    async def _handle(self, payload: EventPayload) -> None:
        chat_id = payload.telegram_user_id
        if not chat_id:
            return
        spec = self._payload_to_spec(payload)
        path = self.renderer.render(spec)
        caption = self._caption(payload)
        with open(path, "rb") as fh:
            await self.bot.send_photo(chat_id=chat_id, photo=fh, caption=caption, parse_mode=ParseMode.HTML)

    def _payload_to_spec(self, payload: EventPayload) -> CardSpec:
        new_snap = payload.new_snapshot
        prev_snap = payload.last_verified_snapshot or payload.prev_snapshot
        if payload.event_kind == "going_into_void":
            verified = prev_snap
            return CardSpec(
                event_type="ban",
                username=payload.ig_username,
                display_name=verified.display_name if verified else None,
                biography=verified.biography if verified else None,
                followers=verified.followers if verified else None,
                following=verified.following if verified else None,
                posts=verified.posts if verified else None,
                avatar_path=verified.profile_pic_path if verified else None,
                verified=verified.verified if verified else None,
                is_private=verified.is_private if verified else None,
                profile_url=f"https://instagram.com/{payload.ig_username}",
                fetched_at=payload.detected_at,
                lookup_duration_ms=new_snap.lookup_duration_ms if new_snap else None,
                last_success_at=verified.fetched_at if verified else None,
                event_code=payload.availability_event.event_code if payload.availability_event else None,
                first_unavailable_at=payload.availability_event.first_unavailable_at if payload.availability_event else None,
                last_unavailable_at=payload.availability_event.last_unavailable_at if payload.availability_event else None,
                success_count=payload.availability_event.success_count if payload.availability_event else 0,
                fail_count=payload.availability_event.fail_count if payload.availability_event else 0,
            )
        if payload.event_kind == "back_from_void":
            return CardSpec(
                event_type="unban",
                username=payload.ig_username,
                display_name=new_snap.display_name if new_snap else None,
                biography=new_snap.biography if new_snap else None,
                followers=new_snap.followers if new_snap else None,
                following=new_snap.following if new_snap else None,
                posts=new_snap.posts if new_snap else None,
                avatar_path=new_snap.profile_pic_path if new_snap else None,
                verified=new_snap.verified if new_snap else None,
                is_private=new_snap.is_private if new_snap else None,
                profile_url=f"https://instagram.com/{payload.ig_username}",
                fetched_at=payload.detected_at,
                lookup_duration_ms=new_snap.lookup_duration_ms if new_snap else None,
                last_success_at=new_snap.fetched_at if new_snap else None,
                event_code=payload.availability_event.event_code if payload.availability_event else None,
                first_unavailable_at=payload.availability_event.first_unavailable_at if payload.availability_event else None,
                last_unavailable_at=payload.availability_event.last_unavailable_at if payload.availability_event else None,
                recovered_at=payload.availability_event.recovered_at if payload.availability_event else None,
                success_count=payload.availability_event.success_count if payload.availability_event else 0,
                fail_count=payload.availability_event.fail_count if payload.availability_event else 0,
            )
        # change
        changes = [
            ChangeRow(field_name=c.field_name, old_value=c.old_value, new_value=c.new_value, diff=c.diff)
            for c in payload.changes
        ]
        return CardSpec(
            event_type="change",
            username=payload.ig_username,
            display_name=new_snap.display_name if new_snap else None,
            biography=new_snap.biography if new_snap else None,
            followers=new_snap.followers if new_snap else None,
            following=new_snap.following if new_snap else None,
            posts=new_snap.posts if new_snap else None,
            avatar_path=new_snap.profile_pic_path if new_snap else None,
            verified=new_snap.verified if new_snap else None,
            is_private=new_snap.is_private if new_snap else None,
            profile_url=f"https://instagram.com/{payload.ig_username}",
            fetched_at=payload.detected_at,
            lookup_duration_ms=new_snap.lookup_duration_ms if new_snap else None,
            last_success_at=prev_snap.fetched_at if prev_snap else None,
            changes=changes,
        )

    @staticmethod
    def _caption(payload: EventPayload) -> str:
        if payload.event_kind == "going_into_void":
            return f"🚫 <b>GOING INTO VOID</b> — @{payload.ig_username} became unavailable."
        if payload.event_kind == "back_from_void":
            return f"✅ <b>BACK FROM VOID</b> — @{payload.ig_username} is accessible again."
        fields = ", ".join(c.field_name for c in payload.changes)
        return f"🔁 <b>Profile change</b> — @{payload.ig_username}: {fields}"


# ---------------------------------------------------------------------------
# Main handler registry
# ---------------------------------------------------------------------------
class BotHandlers:
    def __init__(
        self,
        settings,
        db: Database,
        instagram: InstagramService,
        engine: MonitoringEngine,
        scheduler: MonitorScheduler,
        statistics: StatisticsService,
        history: EventHistoryService,
        renderer: CardRenderer,
    ) -> None:
        self.settings = settings
        self.db = db
        self.instagram = instagram
        self.engine = engine
        self.scheduler = scheduler
        self.statistics = statistics
        self.history = history
        self.renderer = renderer
        self.rate_limiter = RateLimiter()

    # ------------------------------------------------------------------
    def register(self, application: Application) -> None:
        application.add_handler(CommandHandler(["start", "menu"], self.cmd_start))
        application.add_handler(CommandHandler("live", self.cmd_live))
        application.add_handler(CommandHandler("monitor", self.cmd_monitor))
        application.add_handler(CommandHandler("banmonitor", self.cmd_banmonitor))
        application.add_handler(CommandHandler("unbanmonitor", self.cmd_unbanmonitor))
        application.add_handler(CommandHandler("customcard", self.cmd_customcard))
        application.add_handler(CommandHandler("watchlist", self.cmd_watchlist))
        application.add_handler(CommandHandler("stats", self.cmd_stats))
        application.add_handler(CommandHandler("history", self.cmd_history))
        application.add_handler(CommandHandler("duration", self.cmd_duration))
        application.add_handler(CommandHandler("refresh", self.cmd_refresh))
        application.add_handler(CommandHandler("pause", self.cmd_pause))
        application.add_handler(CommandHandler("resume", self.cmd_resume))
        application.add_handler(CommandHandler("remove", self.cmd_remove))
        application.add_handler(CommandHandler("settings", self.cmd_settings))
        application.add_handler(CommandHandler("status", self.cmd_status))
        application.add_handler(CommandHandler("help", self.cmd_help))
        application.add_handler(CommandHandler("cancel", self.cmd_cancel))
        # Text: menu labels + awaited inputs.
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        # One global callback dispatcher.
        application.add_handler(CallbackQueryHandler(self.on_callback))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _user_id(self, update: Update) -> int:
        return update.effective_user.id if update.effective_user else 0

    async def _answer_await(self, update: Update, text: str = "") -> None:
        if update.callback_query:
            await update.callback_query.answer(text=text or None)

    async def _edit_or_send(self, update: Update, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
        """Edit the originating message when possible, otherwise send a new one."""
        if update.callback_query and update.callback_query.message:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
                return
            except Exception:
                pass  # message too old / not text — fall through to send.
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _send_card(self, update: Update, spec: CardSpec, caption: str = "") -> None:
        path = self.renderer.render(spec)
        with open(path, "rb") as fh:
            await update.effective_message.reply_photo(photo=fh, caption=caption or None)

    def _resolve_settings(self, update: Update):
        user = self.db.upsert_user(self._user_id(update), update.effective_user.username)
        return self.db.get_settings(user.id, self.settings.check_seconds), user

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.db.upsert_user(self._user_id(update), update.effective_user.username, update.effective_user.language_code)
        await update.effective_message.reply_text(MAIN_MENU_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN, reply_markup=help_keyboard())

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("await_state", None)
        context.user_data.pop("cc_draft", None)
        context.user_data.pop("monitor_draft", None)
        await update.effective_message.reply_text("✖️ Cancelled. Use /menu to continue.", reply_markup=main_menu_keyboard())

    async def cmd_live(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args
        if args:
            await self._do_live_lookup(update, context, args[0])
        else:
            await self._ask_username(
                update, context, "live_username",
                "🔍 Send the Instagram username to look up:",
            )

    async def cmd_monitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start_monitor_flow(update, context, MonitorType.LIVE)

    async def cmd_banmonitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start_monitor_flow(update, context, MonitorType.BAN)

    async def cmd_unbanmonitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start_monitor_flow(update, context, MonitorType.UNBAN)

    async def cmd_customcard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_custom_studio(update)

    async def cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_watchlist(update, page=1)

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_stats(update)

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_history_menu(update)

    async def cmd_duration(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_time_dashboard(update)

    async def cmd_refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args
        if args:
            await self._refresh_username(update, context, args[0])
        else:
            await self._show_watchlist(update, page=1)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_watchlist(update, page=1, action_note="Select a monitor to pause:")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_watchlist(update, page=1, action_note="Select a monitor to resume:")

    async def cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_remove_menu(update)

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_settings(update)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_app_status(update)

    # ------------------------------------------------------------------
    # Text dispatcher: menu labels + awaited input
    # ------------------------------------------------------------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.effective_message.text or ""
        await_state = context.user_data.get("await_state")

        if await_state:
            await self._handle_awaited(update, context, text)
            return

        mapping = {
            "📸 Live Profile": self.cmd_live,
            "👁 Live Monitor": self.cmd_monitor,
            "🚫 Ban Monitor": self.cmd_banmonitor,
            "🔓 Unban Monitor": self.cmd_unbanmonitor,
            "🎨 Custom Card Studio": self.cmd_customcard,
            "📋 Active Watchlist": self.cmd_watchlist,
            "📊 Statistics": self.cmd_stats,
            "📜 Event History": self.cmd_history,
            "⏱ Time & Duration": self.cmd_duration,
            "🗑 Remove Monitor": self.cmd_remove,
            "🔄 Refresh Data": self.cmd_refresh,
            "⚙️ Settings": self.cmd_settings,
            "❓ Help": self.cmd_help,
        }
        handler = mapping.get(text)
        if handler:
            await handler(update, context)
        else:
            await update.effective_message.reply_text(
                "I didn't recognise that. Send /menu or tap a button.", reply_markup=main_menu_keyboard()
            )

    # ------------------------------------------------------------------
    # Awaited text input
    # ------------------------------------------------------------------
    async def _handle_awaited(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        state = context.user_data.get("await_state")
        context.user_data.pop("await_state", None)
        try:
            if state == "live_username":
                await self._do_live_lookup(update, context, text)
            elif state == "monitor_username":
                await self._do_monitor_after_username(update, context, text)
            elif state == "refresh_username":
                await self._refresh_username(update, context, text)
            elif state == "history_username":
                await self._history_filter_by_username(update, context, text)
            elif state == "cc_field":
                await self._cc_receive_field(update, context, text)
            elif state == "set_input":
                await self._set_receive_input(update, context, text)
            else:
                await update.effective_message.reply_text("Input flow expired. Use /menu.")
        except ValidationError as exc:
            context.user_data["await_state"] = state
            await update.effective_message.reply_text(f"⚠️ {exc}\nPlease try again, or /cancel.")

    # ------------------------------------------------------------------
    # Username prompt helper
    # ------------------------------------------------------------------
    async def _ask_username(self, update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str) -> None:
        context.user_data["await_state"] = state
        await update.effective_message.reply_text(prompt, reply_markup=None)

    # ------------------------------------------------------------------
    # Live profile lookup
    # ------------------------------------------------------------------
    async def _do_live_lookup(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
        allowed, retry = self.rate_limiter.check(self._user_id(update), "live_lookup")
        if not allowed:
            await update.effective_message.reply_text(f"⏳ Slow down — try again in {retry}s.")
            return
        try:
            cleaned = validate_username(raw)
        except ValidationError as exc:
            await update.effective_message.reply_text(f"⚠️ {exc}")
            return
        notice = await update.effective_message.reply_text(f"🔍 Looking up @{cleaned.normalized}…")
        result = await self.instagram.lookup(cleaned.value)
        await notice.delete()
        if not result.ok:
            await update.effective_message.reply_text(
                self._lookup_error_text(result), reply_markup=main_menu_keyboard()
            )
            return
        profile = result.profile
        spec = CardSpec(
            event_type="live",
            username=result.username,
            display_name=profile.display_name,
            biography=profile.biography,
            followers=profile.followers,
            following=profile.following,
            posts=profile.posts,
            avatar_path=result.pic_path,
            verified=profile.verified,
            is_private=profile.is_private,
            profile_url=f"https://instagram.com/{result.username}",
            fetched_at=result.fetched_at,
            lookup_duration_ms=result.duration_ms,
            last_success_at=result.fetched_at,
        )
        context.user_data["last_live"] = {
            "username": result.username,
            "fetched_at": result.fetched_at.isoformat(),
        }
        await self._send_card(update, spec, caption=f"📸 Live profile for @{result.username}")

    @staticmethod
    def _lookup_error_text(result) -> str:
        return (
            f"⚠️ Lookup for @{result.username} did not return a verified profile.\n"
            f"State: {result.availability.value}\n"
            f"Reason: {result.classification_reason}\n"
            f"Detail: {result.error_message or 'no detail available'}\n\n"
            f"This is *not* treated as a ban. The last verified snapshot (if any) is preserved."
        )

    # ------------------------------------------------------------------
    # Monitor creation flow
    # ------------------------------------------------------------------
    async def _start_monitor_flow(self, update: Update, context: ContextTypes.DEFAULT_TYPE, mtype: MonitorType) -> None:
        if context.args:
            await self._do_monitor_after_username(update, context, context.args[0], mtype)
            return
        context.user_data["monitor_draft"] = {"mtype": mtype.value}
        await self._ask_username(
            update, context, "monitor_username",
            f"Send the Instagram username to monitor ({mtype.value}).",
        )

    async def _do_monitor_after_username(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str, mtype: Optional[MonitorType] = None) -> None:
        draft = context.user_data.get("monitor_draft") or {}
        if mtype is None:
            mtype = MonitorType(draft.get("mtype", "live"))
        else:
            draft["mtype"] = mtype.value
        context.user_data["monitor_draft"] = draft
        try:
            cleaned = validate_username(raw)
        except ValidationError as exc:
            context.user_data["await_state"] = "monitor_username"
            await update.effective_message.reply_text(f"⚠️ {exc}\nPlease send a valid username, or /cancel.")
            return
        # Baseline lookup for live/ban (unban may start from an unavailable baseline).
        notice = await update.effective_message.reply_text(f"🔍 Verifying @{cleaned.normalized}…")
        result = await self.instagram.lookup(cleaned.value)
        await notice.delete()
        if not result.ok and mtype in (MonitorType.LIVE, MonitorType.BAN):
            await update.effective_message.reply_text(
                "⚠️ Could not retrieve a verified baseline snapshot.\n" + self._lookup_error_text(result),
                reply_markup=main_menu_keyboard(),
            )
            return
        # Duplicate prevention.
        existing = self.db.find_monitor(self._user_id(update), cleaned.normalized, mtype)
        if existing:
            await update.effective_message.reply_text(
                f"ℹ️ You already have an active {mtype.value} monitor for @{cleaned.normalized} (#{existing.id}).",
                reply_markup=main_menu_keyboard(),
            )
            return
        draft["username"] = cleaned.normalized
        context.user_data["monitor_draft"] = draft
        await self._present_interval_choice(update, context, mtype, cleaned.normalized)

    async def _present_interval_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE, mtype: MonitorType, username: str) -> None:
        text = (
            f"⏱ Choose a polling interval for @{username} ({mtype.value}).\n\n"
            "Conservative intervals protect your Instagram session from rate limits."
        )
        await update.effective_message.reply_text(
            text, reply_markup=interval_keyboard(prefix="int", username=username, mtype=mtype.value)
        )

    # ------------------------------------------------------------------
    # Global callback dispatcher
    # ------------------------------------------------------------------
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        data = query.data or ""
        parts = parse_cb(data)
        prefix = parts[0] if parts else "noop"
        method_name = f"_cb_{prefix}"
        method = getattr(self, method_name, None)
        if method is None:
            try:
                await query.edit_message_text("This button is not connected yet.")
            except Exception:
                pass
            logger.warning("unhandled callback: %s", data)
            return
        try:
            await method(update, context, parts[1:])
        except Exception as exc:
            logger.warning("callback error %s: %s", data, safe_error_message(exc))
            try:
                await query.edit_message_text(f"⚠️ {safe_error_message(exc)}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Callback: noop
    # ------------------------------------------------------------------
    async def _cb_noop(self, update: Update, context, parts) -> None:
        return

    # ------------------------------------------------------------------
    # Callback: navigation
    # ------------------------------------------------------------------
    async def _cb_nav(self, update: Update, context, parts) -> None:
        dest = parts[0] if parts else "home"
        context.user_data.pop("await_state", None)
        if dest == "home":
            await self._edit_or_send(update, MAIN_MENU_TEXT, main_menu_keyboard())
        elif dest == "cancel":
            context.user_data.pop("cc_draft", None)
            context.user_data.pop("monitor_draft", None)
            await self._edit_or_send(update, "✖️ Cancelled. /menu to continue.", main_menu_keyboard())
        else:
            await self._edit_or_send(update, MAIN_MENU_TEXT, main_menu_keyboard())

    # ------------------------------------------------------------------
    # Callback: live profile
    # ------------------------------------------------------------------
    async def _cb_lp(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else ""
        if action == "refresh":
            username = parts[1] if len(parts) > 1 else ""
            await self._do_live_lookup(update, context, username)
        elif action == "bio":
            username = parts[1] if len(parts) > 1 else ""
            await self._view_full_bio(update, context, username)
        else:
            await self._edit_or_send(update, "Unknown live-profile action.")

    async def _view_full_bio(self, update: Update, context, username: str) -> None:
        result = await self.instagram.lookup(username)
        if not result.ok or not result.profile.biography:
            await update.effective_message.reply_text("No biography available for this profile.")
            return
        spec = CardSpec(event_type="live", username=username, biography=result.profile.biography)
        path = render_full_biography(spec, self.renderer.output_dir)
        with open(path, "rb") as fh:
            await update.effective_message.reply_photo(photo=fh, caption=f"📄 Full biography for @{username}")

    # ------------------------------------------------------------------
    # Callback: interval selection (during monitor creation)
    # ------------------------------------------------------------------
    async def _cb_int(self, update: Update, context, parts) -> None:
        seconds = int(parts[0])
        username = parts[1] if len(parts) > 1 else ""
        mtype_value = parts[2] if len(parts) > 2 else "live"
        mtype = MonitorType(mtype_value)
        draft = context.user_data.get("monitor_draft") or {}
        draft.update({"username": username, "mtype": mtype_value, "interval": seconds})
        context.user_data["monitor_draft"] = draft
        preset = self.settings.preset_for_seconds(seconds)
        text = (
            f"✅ Ready to start a *{mtype.value}* monitor for @{username}.\n\n"
            f"Interval: {preset.label} ({seconds}s)\n"
            "Confirm to begin monitoring. You can change the interval later."
        )
        await self._edit_or_send(update, text, confirm_monitor_keyboard(username, mtype_value, seconds))

    # ------------------------------------------------------------------
    # Callback: monitor start / confirm / interval-change
    # ------------------------------------------------------------------
    async def _cb_mon(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else ""
        if action == "start":
            mtype_value = parts[1] if len(parts) > 1 else "live"
            username = parts[2] if len(parts) > 2 else ""
            await self._begin_monitor_from_button(update, context, MonitorType(mtype_value), username)
        elif action == "interval":
            mtype_value = parts[1] if len(parts) > 1 else "live"
            username = parts[2] if len(parts) > 2 else ""
            await self._present_interval_choice(update, context, MonitorType(mtype_value), username)
        elif action == "confirm":
            mtype_value = parts[1]
            username = parts[2]
            interval = int(parts[3]) if len(parts) > 3 else self.settings.check_seconds
            await self._confirm_and_start(update, context, MonitorType(mtype_value), username, interval)
        else:
            await self._edit_or_send(update, "Unknown monitor action.")

    async def _begin_monitor_from_button(self, update: Update, context, mtype: MonitorType, username: str) -> None:
        context.user_data["monitor_draft"] = {"mtype": mtype.value, "username": username}
        await self._present_interval_choice(update, context, mtype, username)

    async def _confirm_and_start(self, update: Update, context, mtype: MonitorType, username: str, interval: int) -> None:
        tg = self._user_id(update)
        existing = self.db.find_monitor(tg, username, mtype)
        if existing:
            await self._edit_or_send(
                update,
                f"ℹ️ Active {mtype.value} monitor for @{username} already exists (#{existing.id}).",
                main_menu_keyboard(),
            )
            return
        count = self.db.count_active_monitors_for_user(tg)
        allowed, msg = user_can_create_monitor(count, self.settings.max_monitors_per_user)
        if not allowed:
            await self._edit_or_send(update, f"⚠️ {msg}", main_menu_keyboard())
            return
        monitor = self.db.create_monitor(tg, username, mtype, interval)
        self.scheduler.schedule(monitor.id, interval)
        await self._edit_or_send(
            update,
            f"✅ Started {mtype.value} monitor #{monitor.id} for @{username} every {interval}s.",
            monitor_detail_keyboard(monitor.id, mtype.value),
        )

    # ------------------------------------------------------------------
    # Callback: watchlist
    # ------------------------------------------------------------------
    async def _cb_wl(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else "list"
        if action == "list":
            await self._show_watchlist(update, page=1, edit=True)
        elif action == "page":
            page = int(parts[1]) if len(parts) > 1 else 1
            await self._show_watchlist(update, page=page, edit=True)
        elif action == "detail":
            monitor_id = int(parts[1])
            await self._show_monitor_detail(update, monitor_id)
        elif action == "pause":
            monitor_id = int(parts[1])
            self.scheduler.pause(monitor_id)
            await self._show_monitor_detail(update, monitor_id)
        elif action == "resume":
            monitor_id = int(parts[1])
            self.scheduler.resume(monitor_id)
            await self._show_monitor_detail(update, monitor_id)
        elif action == "refresh":
            monitor_id = int(parts[1])
            await self._refresh_monitor_now(update, monitor_id)
        elif action == "interval":
            monitor_id = int(parts[1])
            await self._change_interval_prompt(update, monitor_id)
        elif action == "setint":
            monitor_id = int(parts[1])
            seconds = int(parts[2]) if len(parts) > 2 else self.settings.check_seconds
            self.scheduler.reschedule(monitor_id, seconds)
            await self._show_monitor_detail(update, monitor_id)
        elif action == "stop":
            monitor_id = int(parts[1])
            self.scheduler.stop(monitor_id)
            await self._edit_or_send(update, f"⏹ Monitor #{monitor_id} stopped.", main_menu_keyboard())
        else:
            await self._edit_or_send(update, "Unknown watchlist action.")

    async def _show_watchlist(self, update: Update, page: int = 1, edit: bool = False, action_note: str = "") -> None:
        tg = self._user_id(update)
        monitors = self.db.list_monitors(tg)
        total = len(monitors)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PAGE_SIZE
        chunk = monitors[start:start + PAGE_SIZE]
        if not chunk:
            text = "📋 Your watchlist is empty.\nStart one with /live, /monitor, /banmonitor or /unbanmonitor."
            if action_note:
                text = f"{action_note}\n\n{text}"
            await self._edit_or_send(update, text, main_menu_keyboard())
            return
        lines = [f"📋 Active Watchlist — page {page}/{total_pages} ({total} total)"]
        if action_note:
            lines.append(action_note)
        lines.append("")
        for m in chunk:
            state_icon = "🟢" if m.state.value == "active" else "⏸"
            lines.append(
                f"{state_icon} #{m.id} @{m.ig_username} · {m.monitor_type.value} · "
                f"{m.state.value} · {m.interval_seconds}s"
            )
            lines.append(f"   last success: {format_timestamp(m.last_success_at)}")
            lines.append("")
        keyboard = watchlist_pagination_keyboard(page, total_pages)
        # Attach detail buttons for the current page.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        rows = []
        for m in chunk:
            rows.append([InlineKeyboardButton(f"🔎 #{m.id} @{m.ig_username} ({m.monitor_type.value})", callback_data=cb("wl", "detail", m.id))])
        rows.extend(keyboard.inline_keyboard)
        await self._edit_or_send(update, "\n".join(lines), InlineKeyboardMarkup(rows))

    async def _show_monitor_detail(self, update: Update, monitor_id: int) -> None:
        monitor = self.db.get_monitor(monitor_id)
        if monitor is None:
            await self._edit_or_send(update, "Monitor not found.", main_menu_keyboard())
            return
        user = self.db.get_user_by_id(monitor.user_id)
        if user is None or user.telegram_user_id != self._user_id(update):
            await self._edit_or_send(update, "Not your monitor.", main_menu_keyboard())
            return
        snap = self.db.latest_successful_snapshot(monitor.id)
        next_at = next_check_time(monitor.last_check_at, monitor.interval_seconds)
        text = (
            f"🔎 Monitor #{monitor.id}\n\n"
            f"• Username: @{monitor.ig_username}\n"
            f"• Type: {monitor.monitor_type.value}\n"
            f"• State: {monitor.state.value}\n"
            f"• Interval: {monitor.interval_seconds}s\n"
            f"• Started: {format_timestamp(monitor.started_at)}\n"
            f"• Last check: {format_timestamp(monitor.last_check_at)}\n"
            f"• Last success: {format_timestamp(monitor.last_success_at)}\n"
            f"• Next check: {format_timestamp(next_at)}\n"
            f"• Success / fail: {monitor.success_count} / {monitor.fail_count}\n"
            f"• Monitor age: {format_duration((now_utc() - monitor.started_at).total_seconds())}\n"
        )
        if snap:
            text += f"\nLatest verified: {snap.followers} followers, {snap.posts} posts."
        await self._edit_or_send(update, text, monitor_detail_keyboard(monitor.id, monitor.monitor_type.value))

    async def _refresh_monitor_now(self, update: Update, monitor_id: int) -> None:
        await update.callback_query.answer("Refreshing…")
        payload = await self.engine.check_monitor(monitor_id)
        note = "✅ Check complete."
        if payload is None:
            note += " No event fired (rate limit, network, or no change)."
        else:
            note += f" Event: {payload.event_kind}."
        await self._show_monitor_detail(update, monitor_id)

    async def _change_interval_prompt(self, update: Update, monitor_id: int) -> None:
        monitor = self.db.get_monitor(monitor_id)
        if monitor is None:
            await self._edit_or_send(update, "Monitor not found.")
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        rows = []
        for preset in self.settings.interval_presets:
            rows.append([InlineKeyboardButton(preset.label, callback_data=cb("wl", "setint", monitor_id, preset.seconds))])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=cb("wl", "detail", monitor_id))])
        await self._edit_or_send(update, f"⏱ New interval for @{monitor.ig_username}:", InlineKeyboardMarkup(rows))

    async def _cb_wl_setint(self, *args, **kwargs):  # pragma: no cover - routed via on_callback? no
        return

    # ------------------------------------------------------------------
    # Callback: snapshots
    # ------------------------------------------------------------------
    async def _cb_snap(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else "latest"
        monitor_id = int(parts[1]) if len(parts) > 1 else 0
        monitor = self.db.get_monitor(monitor_id)
        if monitor is None:
            await self._edit_or_send(update, "Monitor not found.")
            return
        snap: Optional[Snapshot] = None
        if action == "latest":
            snap = self.db.latest_snapshot(monitor.id)
        elif action == "verified":
            snap = self.db.latest_successful_snapshot(monitor.id)
        elif action == "prev":
            latest = self.db.latest_snapshot(monitor.id)
            if latest:
                snap = self.db.previous_snapshot(monitor.id, latest.id)
        if snap is None:
            await update.effective_message.reply_text("No snapshot available for this action.")
            return
        spec = self._snapshot_to_spec(snap, monitor)
        await self._send_card(update, spec, caption=f"🖼 Snapshot for @{snap.ig_username} ({action})")

    def _snapshot_to_spec(self, snap: Snapshot, monitor: Monitor) -> CardSpec:
        return CardSpec(
            event_type="live",
            username=snap.ig_username,
            display_name=snap.display_name,
            biography=snap.biography,
            followers=snap.followers,
            following=snap.following,
            posts=snap.posts,
            avatar_path=snap.profile_pic_path,
            verified=snap.verified,
            is_private=snap.is_private,
            profile_url=f"https://instagram.com/{snap.ig_username}",
            fetched_at=snap.fetched_at,
            lookup_duration_ms=snap.lookup_duration_ms,
            last_success_at=snap.fetched_at if snap.lookup_status == LookupStatus.SUCCESS else None,
        )

    # ------------------------------------------------------------------
    # Callback: history
    # ------------------------------------------------------------------
    async def _cb_hist(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else "menu"
        if action == "menu":
            await self._show_history_menu(update, edit=True)
        elif action == "view":
            kind = parts[1] if len(parts) > 1 else "change"
            await self._show_history(update, context, kind, page=1)
        elif action == "page":
            kind = parts[1] if len(parts) > 1 else "change"
            page = int(parts[2]) if len(parts) > 2 else 1
            username = parts[3] if len(parts) > 3 and parts[3] else None
            await self._show_history(update, context, kind, page=page, username=username)
        elif action == "custom":
            await self._show_custom_history(update)
        elif action == "export":
            await self._export_history(update, context)
        elif action == "askuser":
            context.user_data["await_state"] = "history_username"
            await self._edit_or_send(update, "🔍 Send a username to filter history by:")
        elif action == "for":
            monitor_id = int(parts[1]) if len(parts) > 1 else 0
            monitor = self.db.get_monitor(monitor_id)
            uname = monitor.ig_username if monitor else None
            await self._show_history(update, context, "change", page=1, username=uname)
        elif action == "avail":
            monitor_id = int(parts[1]) if len(parts) > 1 else 0
            monitor = self.db.get_monitor(monitor_id)
            uname = monitor.ig_username if monitor else None
            await self._show_history(update, context, "avail", page=1, username=uname)
        else:
            await self._edit_or_send(update, "Unknown history action.")

    async def _show_history_menu(self, update: Update, edit: bool = False) -> None:
        text = "📜 Event History\n\nFilter and browse verified monitoring events. Custom cards are listed separately."
        if edit and update.callback_query:
            await self._edit_or_send(update, text, history_filter_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=history_filter_keyboard())

    async def _show_history(self, update: Update, context, kind: str, page: int = 1, username: Optional[str] = None) -> None:
        tg = self._user_id(update)
        if kind == "change":
            p = self.history.change_events(tg, username=username, page=page)
            text = self.history.render_change_text(p)
            kb = history_pagination_keyboard("change", p.page, p.total_pages, username)
        else:
            p = self.history.availability_events(tg, username=username, page=page)
            text = self.history.render_availability_text(p)
            kb = history_pagination_keyboard("avail", p.page, p.total_pages, username)
        await self._edit_or_send(update, text, kb)

    async def _show_custom_history(self, update: Update) -> None:
        cards = self.history.custom_cards(self._user_id(update))
        # Need the user id from the users table.
        text = self.history.render_custom_cards_text(self._resolve_cards(update, cards))
        await self._edit_or_send(update, text, history_filter_keyboard())

    def _resolve_cards(self, update: Update, cards):
        # custom_cards expects List[CustomCard]; the service already resolved via user id.
        return cards

    async def _export_history(self, update: Update, context) -> None:
        csv_text = self.history.export_csv(self._user_id(update))
        buf = io.StringIO(csv_text)
        buf.name = "arix_history.csv"
        from telegram import InputFile
        await update.effective_message.reply_document(document=InputFile(buf, filename="arix_history.csv"))

    async def _history_filter_by_username(self, update: Update, context, text: str) -> None:
        try:
            cleaned = validate_username(text)
        except ValidationError as exc:
            context.user_data["await_state"] = "history_username"
            await update.effective_message.reply_text(f"⚠️ {exc}\nSend a valid username, or /cancel.")
            return
        await self._show_history(update, context, "change", page=1, username=cleaned.normalized)

    # ------------------------------------------------------------------
    # Callback: statistics
    # ------------------------------------------------------------------
    async def _cb_stat(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else "refresh"
        if action == "refresh":
            await self._show_stats(update, edit=True)
        elif action == "breakdown":
            data = self.statistics.monitor_breakdown(self._user_id(update))
            if not data:
                await self._edit_or_send(update, "No active monitors.", stats_keyboard())
                return
            lines = ["🧩 Monitor breakdown", ""]
            for row in data:
                lines.append(
                    f"• @{row['username']} · {row['type']} · {row['state']} · {row['interval_seconds']}s"
                )
                lines.append(f"   success/fail: {row['success_count']}/{row['fail_count']}")
            await self._edit_or_send(update, "\n".join(lines), stats_keyboard())
        elif action == "lookups":
            stats = self.statistics.lookup_statistics()
            lines = [
                "🔍 Lookup statistics",
                "",
                f"• Success: {int(stats['success'])}",
                f"• Failed: {int(stats['failed'])}",
                f"• Total: {int(stats['total'])}",
                f"• Success rate: {stats['success_rate']:.1f}%",
                f"• Avg duration: {format_ms_or_seconds(stats['avg_lookup_ms'] / 1000)}",
                f"• Last duration: {format_ms_or_seconds(stats['last_lookup_ms'] / 1000)}",
            ]
            await self._edit_or_send(update, "\n".join(lines), stats_keyboard())
        elif action == "recent":
            await self._show_history(update, context, "change", page=1)
        elif action == "errors":
            await self._show_errors(update)
        elif action == "export":
            await self._export_history(update, context)
        else:
            await self._edit_or_send(update, "Unknown stats action.")

    async def _show_stats(self, update: Update, edit: bool = False) -> None:
        report = self.statistics.report()
        tz = self.db.get_settings(self._user_id(update), self.settings.check_seconds).timezone if self.db.get_user(self._user_id(update)) else None
        text = "\n".join(report.to_lines(timezone=tz))
        if edit and update.callback_query:
            await self._edit_or_send(update, text, stats_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=stats_keyboard())

    async def _show_errors(self, update: Update) -> None:
        # Surface the most recent failed monitors for this user.
        monitors = self.db.list_monitors(self._user_id(update))
        lines = ["⚠️ Recent monitor errors", ""]
        any_err = False
        for m in monitors:
            if m.fail_count > 0 or (m.last_success_at is None):
                any_err = True
                lines.append(f"• #{m.id} @{m.ig_username}: {m.fail_count} failed checks")
        if not any_err:
            lines.append("No monitors with errors. 🎉")
        await self._edit_or_send(update, "\n".join(lines), stats_keyboard())

    # ------------------------------------------------------------------
    # Callback: settings
    # ------------------------------------------------------------------
    async def _cb_set(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else ""
        settings_kb = settings_keyboard()
        if action == "interval":
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            rows = []
            for preset in self.settings.interval_presets:
                rows.append([InlineKeyboardButton(preset.label, callback_data=cb("set", "setint", preset.seconds))])
            rows.append([InlineKeyboardButton("⬅️ Back", callback_data=cb("set", "back"))])
            await self._edit_or_send(update, "⏱ Default monitoring interval:", InlineKeyboardMarkup(rows))
        elif action == "setint":
            seconds = int(parts[1])
            self._save_setting(update, interval_seconds=seconds)
            await self._edit_or_send(update, f"✅ Default interval set to {seconds}s.", settings_kb)
        elif action == "notify":
            self._save_setting(update, notify_changes=not self._current_setting(update).notify_changes)
            s = self._current_setting(update)
            await self._edit_or_send(update, f"🔔 Change notifications: {'on' if s.notify_changes else 'off'}\nAvailability notifications: {'on' if s.notify_availability else 'off'}", settings_kb)
        elif action == "timezone":
            context.user_data["await_state"] = "set_input"
            context.user_data["set_field"] = "timezone"
            await self._edit_or_send(update, "🌍 Send a timezone name (e.g. Europe/London):")
        elif action == "cardstyle":
            self._save_setting(update, card_style="premium" if self._current_setting(update).card_style == "default" else "default")
            await self._edit_or_send(update, f"🎨 Card style: {self._current_setting(update).card_style}", settings_kb)
        elif action == "maxmon":
            context.user_data["await_state"] = "set_input"
            context.user_data["set_field"] = "maxmon"
            await self._edit_or_send(update, f"🔢 Send a max monitor count (1–50, server cap {self.settings.max_monitors_per_user}):")
        elif action == "pauseall":
            count = self.scheduler.stop_all_for_user(self._user_id(update))
            await self._edit_or_send(update, f"⏸ Stopped {count} monitors. Use /resume or a monitor's ▶️ Resume to re-activate.", settings_kb)
        elif action == "export":
            await self._export_history(update, context)
        elif action == "delete":
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            await self._edit_or_send(
                update,
                "🗑 This deletes ALL your monitors, snapshots, events, custom cards and settings. Confirm?",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ YES, delete everything", callback_data=cb("set", "dodelete"))],
                    [InlineKeyboardButton("✖️ Cancel", callback_data=cb("set", "back"))],
                ]),
            )
        elif action == "dodelete":
            removed = self.db.delete_user_data(self._user_id(update))
            await self._edit_or_send(update, f"🗑 Deleted {removed} monitors and all related data.", settings_kb)
        elif action == "session":
            masked = mask_token(self.settings.ig_sessionid) if self.settings.ig_sessionid else "<unset>"
            await self._edit_or_send(update, f"🔑 Instagram session: {masked}\nToken: {mask_token(self.settings.telegram_bot_token)}", settings_kb)
        elif action == "appstatus":
            await self._show_app_status(update, edit=True)
        elif action == "back":
            await self._show_settings(update, edit=True)
        else:
            await self._edit_or_send(update, "Unknown settings action.", settings_kb)

    def _current_setting(self, update: Update):
        user = self.db.upsert_user(self._user_id(update), update.effective_user.username)
        return self.db.get_settings(user.id, self.settings.check_seconds)

    def _save_setting(self, update: Update, **kwargs) -> None:
        s = self._current_setting(update)
        for k, v in kwargs.items():
            setattr(s, k, v)
        self.db.upsert_settings(s)

    async def _set_receive_input(self, update: Update, context, text: str) -> None:
        field = context.user_data.pop("set_field", None)
        if field == "timezone":
            try:
                tz = validate_timezone_name(text) or "UTC"
            except ValidationError as exc:
                await update.effective_message.reply_text(f"⚠️ {exc}")
                return
            self._save_setting(update, timezone=tz)
            await update.effective_message.reply_text(f"✅ Timezone set to {tz}.", reply_markup=settings_keyboard())
        elif field == "maxmon":
            try:
                value = validate_int(text, "max monitors", 1, max(50, self.settings.max_monitors_per_user))
            except ValidationError as exc:
                await update.effective_message.reply_text(f"⚠️ {exc}")
                return
            self._save_setting(update, max_monitors_override=value)
            await update.effective_message.reply_text(f"✅ Max monitors set to {value}.", reply_markup=settings_keyboard())

    async def _show_settings(self, update: Update, edit: bool = False) -> None:
        text = "⚙️ Settings"
        if edit and update.callback_query:
            await self._edit_or_send(update, text, settings_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=settings_keyboard())

    async def _show_app_status(self, update: Update, edit: bool = False) -> None:
        masked = self.settings.mask_secrets()
        report = self.statistics.report()
        lines = [
            "ℹ️ Application status",
            "",
            f"• Telegram token: {masked['telegram_bot_token']}",
            f"• Instagram session: {masked['ig_sessionid']}",
            f"• Database: {masked['database_path']}",
            f"• Check interval (default): {masked['check_seconds']}s",
            f"• Max monitors/user: {masked['max_monitors_per_user']}",
            f"• Log level: {masked['log_level']}",
            f"• Timezone: {masked['timezone']}",
            f"• Uptime: {format_duration(report.uptime_seconds)}",
            f"• Active monitors: {report.total_active_monitors}",
            f"• Scheduler tasks: {self.scheduler.active_count()}",
        ]
        text = "\n".join(lines)
        if edit and update.callback_query:
            await self._edit_or_send(update, text, settings_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=settings_keyboard())

    # ------------------------------------------------------------------
    # Callback: custom card studio
    # ------------------------------------------------------------------
    async def _cb_cc(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else "studio"
        if action == "studio":
            await self._show_custom_studio(update, edit=True)
        elif action == "new":
            draft = CustomCardDraft()
            context.user_data["cc_draft"] = draft
            await self._show_cc_editor(update, context)
        elif action == "fromlive":
            username = parts[1] if len(parts) > 1 else ""
            draft = CustomCardDraft(username=username, event_type="custom_live")
            context.user_data["cc_draft"] = draft
            await self._show_cc_editor(update, context)
        elif action == "editor":
            await self._show_cc_editor(update, context)
        elif action == "edit":
            field = parts[1] if len(parts) > 1 else ""
            await self._cc_edit_field(update, context, field)
        elif action == "setevent":
            key = parts[1] if len(parts) > 1 else "custom_live"
            draft = context.user_data.get("cc_draft")
            if draft:
                draft.event_type = key
            await self._show_cc_editor(update, context)
        elif action == "preview":
            await self._cc_preview(update, context)
        elif action == "regen":
            await self._cc_preview(update, context)
        elif action == "save":
            await self._cc_save(update, context)
        elif action == "reset":
            field = parts[1] if len(parts) > 1 else ""
            draft = context.user_data.get("cc_draft")
            if draft and field:
                draft.reset_field(field)
            await self._show_cc_editor(update, context)
        else:
            await self._edit_or_send(update, "Unknown custom-card action.")

    async def _show_custom_studio(self, update: Update, edit: bool = False) -> None:
        text = "🎨 Custom Card Studio\n\nCreate premium ban / unban / live / change cards from manually entered values. Custom values stay separate from verified monitoring data."
        if edit and update.callback_query:
            await self._edit_or_send(update, text, custom_card_studio_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=custom_card_studio_keyboard())

    async def _show_cc_editor(self, update: Update, context) -> None:
        draft: Optional[CustomCardDraft] = context.user_data.get("cc_draft")
        if draft is None:
            draft = CustomCardDraft()
            context.user_data["cc_draft"] = draft
        await self._edit_or_send(update, draft.summary(), custom_card_editor_keyboard())

    async def _cc_edit_field(self, update: Update, context, field: str) -> None:
        draft = context.user_data.get("cc_draft")
        if draft is None:
            await self._edit_or_send(update, "No draft in progress. Start a new card.")
            return
        if field == "event_type":
            await self._edit_or_send(update, "Choose an event type:", custom_card_event_type_keyboard())
            return
        if field == "avatar":
            context.user_data["await_state"] = "cc_field"
            context.user_data["cc_field"] = "avatar"
            await self._edit_or_send(update, "🖼 Send a profile picture image to use as the avatar.")
            return
        context.user_data["await_state"] = "cc_field"
        context.user_data["cc_field"] = field
        prompts = {
            "username": "Send the Instagram username:",
            "display_name": "Send the display name:",
            "biography": "Send the biography (up to 1000 chars):",
            "followers": "Send the follower count:",
            "following": "Send the following count:",
            "posts": "Send the post count:",
            "event_timestamp": "Send the event timestamp (YYYY-MM-DD HH:MM:SS), or 'now':",
            "elapsed_text": "Send the elapsed duration text:",
            "footer": "Send the footer text:",
        }
        await self._edit_or_send(update, prompts.get(field, f"Send value for {field}:"))

    async def _cc_receive_field(self, update: Update, context, text: str) -> None:
        draft: Optional[CustomCardDraft] = context.user_data.get("cc_draft")
        field = context.user_data.pop("cc_field", None)
        if draft is None or field is None:
            await update.effective_message.reply_text("Draft expired. Use /customcard.")
            return
        try:
            if field in CustomCardDraft.NUMERIC_FIELDS:
                value = validate_count(text, CustomCardDraft.FIELD_LABELS[field])
                draft.set_field(field, value)
            elif field == "username":
                cleaned = validate_username(text)
                draft.username = cleaned.normalized
            elif field == "biography":
                draft.biography = sanitize_text(text, 1000)
            elif field == "event_timestamp":
                if text.strip().lower() == "now":
                    draft.event_timestamp = now_utc()
                else:
                    draft.event_timestamp = self._parse_timestamp(text)
            elif field in ("display_name", "elapsed_text", "footer"):
                draft.set_field(field, sanitize_text(text, 200 if field == "footer" else 500))
            else:
                draft.set_field(field, sanitize_text(text, 500))
        except ValidationError as exc:
            context.user_data["await_state"] = "cc_field"
            context.user_data["cc_field"] = field
            await update.effective_message.reply_text(f"⚠️ {exc}\nPlease try again, or /cancel.")
            return
        await self._show_cc_editor(update, context)

    @staticmethod
    def _parse_timestamp(text: str) -> Optional[datetime]:
        text = text.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return ensure_aware(datetime.strptime(text, fmt))
            except ValueError:
                continue
        return None

    async def _cc_preview(self, update: Update, context) -> None:
        draft = context.user_data.get("cc_draft")
        if draft is None or not draft.username:
            await self._edit_or_send(update, "⚠️ Set at least a username before previewing.")
            return
        spec = self._draft_to_spec(draft)
        await self._send_card(update, spec, caption="🎨 Custom card preview")
        await self._show_cc_editor(update, context)

    async def _cc_save(self, update: Update, context) -> None:
        draft = context.user_data.get("cc_draft")
        if draft is None or not draft.username:
            await self._edit_or_send(update, "⚠️ Set at least a username before saving.")
            return
        user = self.db.upsert_user(self._user_id(update), update.effective_user.username)
        card = CustomCard(
            id=0, user_id=user.id, ig_username=draft.username,
            display_name=draft.display_name, biography=draft.biography,
            followers=draft.followers, following=draft.following, posts=draft.posts,
            event_type=EventType.CUSTOM, event_timestamp=draft.event_timestamp,
            elapsed_text=draft.elapsed_text, footer=draft.footer, pic_path=draft.avatar_path,
            created_at=now_utc(), metadata_json='{"custom":true,"subtype":"' + draft.event_type + '"}',
        )
        saved = self.db.save_custom_card(card)
        context.user_data.pop("cc_draft", None)
        await self._edit_or_send(
            update,
            f"💾 Custom card #{saved.id} saved. It is stored separately from verified events.",
            custom_card_studio_keyboard(),
        )

    def _draft_to_spec(self, draft: CustomCardDraft) -> CardSpec:
        title_key = draft.event_type
        title_text, _ = CUSTOM_TITLES.get(title_key, ("CUSTOM CARD", (0, 194, 255)))
        return CardSpec(
            event_type="custom",
            is_custom=True,
            custom_title=title_key,
            username=draft.username or "user",
            display_name=draft.display_name,
            biography=draft.biography,
            followers=draft.followers,
            following=draft.following,
            posts=draft.posts,
            avatar_path=draft.avatar_path,
            profile_url=f"https://instagram.com/{draft.username}",
            fetched_at=draft.event_timestamp or now_utc(),
            lookup_duration_ms=0,
            last_success_at=draft.event_timestamp,
            event_code=draft.footer or None,
            custom_event_timestamp=draft.event_timestamp,
            custom_elapsed_text=draft.elapsed_text,
            footer=draft.footer,
        )

    # ------------------------------------------------------------------
    # Callback: remove monitor
    # ------------------------------------------------------------------
    async def _cb_rm(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else "list"
        if action == "list":
            await self._show_watchlist(update, page=1, edit=True, action_note="Select a monitor to remove:")
        elif action == "allconfirm":
            await self._edit_or_send(
                update,
                "⚠️ Remove ALL your monitors? This cannot be undone from the buttons.",
                remove_all_confirm_keyboard(),
            )
        elif action == "confirm":
            target = parts[1] if len(parts) > 1 else "0"
            if target == "all":
                removed = self.scheduler.stop_all_for_user(self._user_id(update))
                await self._edit_or_send(update, f"🗑 Stopped {removed} monitors.", main_menu_keyboard())
            else:
                monitor_id = int(target)
                monitor = self.db.get_monitor(monitor_id)
                if monitor is None:
                    await self._edit_or_send(update, "Monitor not found.")
                    return
                self.scheduler.stop(monitor_id)
                await self._edit_or_send(
                    update,
                    f"🗑 Monitor #{monitor_id} (@{monitor.ig_username}) removed. Undo available below.",
                    remove_confirm_keyboard(monitor_id, "one"),
                )
        elif action == "undo":
            monitor_id = int(parts[1]) if len(parts) > 1 else 0
            monitor = self.db.get_monitor(monitor_id)
            if monitor is None:
                await self._edit_or_send(update, "Cannot undo — monitor record gone.")
                return
            self.db.update_monitor_state(monitor_id, MonitorState.ACTIVE)
            self.scheduler.schedule(monitor_id, monitor.interval_seconds)
            await self._edit_or_send(update, f"♻️ Monitor #{monitor_id} re-activated.", monitor_detail_keyboard(monitor_id, monitor.monitor_type.value))
        else:
            await self._edit_or_send(update, "Unknown remove action.")

    async def _show_remove_menu(self, update: Update) -> None:
        await update.effective_message.reply_text(
            "🗑 Remove Monitor\n\nRemove one monitor or all of them. Destructive actions require confirmation.",
            reply_markup=remove_keyboard(),
        )

    # ------------------------------------------------------------------
    # Callback: time dashboard
    # ------------------------------------------------------------------
    async def _cb_time(self, update: Update, context, parts) -> None:
        action = parts[0] if parts else "refresh"
        await self._show_time_dashboard(update, edit=True)

    async def _show_time_dashboard(self, update: Update, edit: bool = False) -> None:
        monitors = self.db.list_monitors(self._user_id(update))
        report = self.statistics.report()
        lines = ["⏱ Time & Duration dashboard", ""]
        if not monitors:
            lines.append("No active monitors.")
        else:
            now = now_utc()
            for m in monitors[:8]:
                age = format_duration((now - m.started_at).total_seconds()) if m.started_at else "unknown"
                since_check = format_duration((now - m.last_check_at).total_seconds()) if m.last_check_at else "never"
                since_success = format_duration((now - m.last_success_at).total_seconds()) if m.last_success_at else "never"
                nxt = next_check_time(m.last_check_at, m.interval_seconds)
                lines.append(f"• #{m.id} @{m.ig_username}")
                lines.append(f"   age: {age} · since check: {since_check} · since success: {since_success}")
                lines.append(f"   next check: {format_timestamp(nxt)}")
            lines.append("")
        lines.append(f"• App uptime: {format_duration(report.uptime_seconds)}")
        lines.append(f"• Last lookup duration: {format_ms_or_seconds(report.last_lookup_ms / 1000)}")
        text = "\n".join(lines)
        if edit and update.callback_query:
            await self._edit_or_send(update, text, time_dashboard_keyboard())
        else:
            await update.effective_message.reply_text(text, reply_markup=time_dashboard_keyboard())

    # ------------------------------------------------------------------
    # Refresh by username (used by /refresh with arg)
    # ------------------------------------------------------------------
    async def _refresh_username(self, update: Update, context, raw: str) -> None:
        try:
            cleaned = validate_username(raw)
        except ValidationError as exc:
            await update.effective_message.reply_text(f"⚠️ {exc}")
            return
        # find a monitor for this user
        monitors = self.db.list_monitors(self._user_id(update))
        match = next((m for m in monitors if m.ig_username.lower() == cleaned.normalized), None)
        if match is None:
            await self._do_live_lookup(update, context, cleaned.value)
            return
        await self._refresh_monitor_now(update, match.id)


# ---------------------------------------------------------------------------
# Wiring entry point used by main.py
# ---------------------------------------------------------------------------
def build_notifier(application: Application, renderer: CardRenderer, db: Database) -> TelegramNotifier:
    return TelegramNotifier(application.bot, renderer, db)


# ======================================================================
# Module: main
# ======================================================================
# Card renderer lives next to the database so generated PNGs persist across
# restarts for the snapshot/full-biography viewers.


def build_application():
    """Construct the Telegram Application and all supporting services.

    Returns ``(application, services)`` so tests can drive individual pieces
    without starting polling.
    """
    settings = load_settings()
    logger = setup_logging(settings.log_level, settings.database_path.parent / "logs")

    problems = settings.validate()
    for problem in problems:
        logger.warning("config: %s", problem)

    db = Database(settings.database_path)
    instagram = InstagramService(settings)
    renderer = CardRenderer(settings.database_path.parent / "cards")
    engine = MonitoringEngine(db, instagram, settings, notifier=None)
    scheduler = MonitorScheduler(db, engine, settings)
    statistics = StatisticsService(db, settings)
    history = EventHistoryService(db)

    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set; cannot start the bot. See .env.example.")
        # Still return the services so unit tests can exercise the engine.

    from telegram.ext import Application as PTBApplication, ApplicationBuilder

    builder = ApplicationBuilder().token(settings.telegram_bot_token or "missing-token")
    application: PTBApplication = builder.build()

    handlers = BotHandlers(settings, db, instagram, engine, scheduler, statistics, history, renderer)
    handlers.register(application)

    notifier = TelegramNotifier(application.bot, renderer, db)
    engine.notifier = notifier

    async def on_startup(_app) -> None:
        statistics.mark_started()
        recovered = await scheduler.start()
        logger.info("application starting; recovered %s monitors", recovered)

    async def on_shutdown(_app) -> None:
        await scheduler.shutdown()
        statistics.mark_shutdown()
        instagram.close()
        db.close()
        logger.info("application shut down cleanly")

    application.post_init = on_startup
    application.post_shutdown = on_shutdown

    services = {
        "settings": settings,
        "db": db,
        "instagram": instagram,
        "engine": engine,
        "scheduler": scheduler,
        "statistics": statistics,
        "history": history,
        "renderer": renderer,
        "handlers": handlers,
        "notifier": notifier,
    }
    return application, services


async def _run_polling(application) -> None:
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    stop_event = asyncio.Event()

    def _request_stop(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass
    try:
        await stop_event.wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main() -> int:
    application, services = build_application()
    settings = services["settings"]
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run_polling(application))
    except KeyboardInterrupt:
        pass
    return 0

# =============================================================================
# Entry point
# =============================================================================
if __name__ == "__main__":
    raise SystemExit(main())
