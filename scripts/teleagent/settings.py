"""Settings services for the Telegram relay."""

from __future__ import annotations


import os
import re
from datetime import timedelta, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]


SHELL_COMMANDS = {"bash", "sh", "zsh", "fish", "csh", "tcsh", "dash", "ksh"}


CODEX_COMMANDS = {"codex"}


DEFAULT_SCRATCH = os.environ.get(
    "TELEAGENT_SCRATCH",
    str(Path.home() / ".local" / "share" / "tele-agent"),
)


DEFAULT_TELEGRAM_LOG_DIR = Path(
    os.environ.get(
        "TELEAGENT_LOG_DIR", str(Path(DEFAULT_SCRATCH) / "logs" / "telegram")
    )
)


DEFAULT_INBOUND_DOCUMENTS_DIR = Path(
    os.environ.get(
        "TELEAGENT_INBOUND_DOCUMENTS_DIR",
        str(Path(DEFAULT_SCRATCH) / "inbound_documents"),
    )
)


DEFAULT_INBOUND_VOICE_DIR = Path(
    os.environ.get(
        "TELEAGENT_INBOUND_VOICE_DIR",
        str(Path(DEFAULT_SCRATCH) / "inbound_voice"),
    )
)


DEFAULT_INBOUND_PHOTO_DIR = Path(
    os.environ.get(
        "TELEAGENT_INBOUND_PHOTO_DIR",
        str(Path(DEFAULT_SCRATCH) / "inbound_photos"),
    )
)


DEFAULT_WHISPER_BIN = Path(
    os.environ.get(
        "TELEAGENT_WHISPER_BIN",
        str(Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli"),
    )
)


DEFAULT_WHISPER_MODEL = Path(
    os.environ.get(
        "TELEAGENT_WHISPER_MODEL",
        str(Path.home() / "whisper.cpp" / "models" / "ggml-small.bin"),
    )
)


DEFAULT_OPUSDEC_BIN = Path(
    os.environ.get(
        "TELEAGENT_OPUSDEC_BIN",
        str(Path.home() / "voice" / "usr" / "bin" / "opusdec"),
    )
)


DEFAULT_OPUSDEC_LIB_DIR = Path(
    os.environ.get(
        "TELEAGENT_OPUSDEC_LIB_DIR",
        str(Path.home() / "voice" / "usr" / "lib" / "x86_64-linux-gnu"),
    )
)


TELEGRAM_FILE_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024


DEFAULT_MAX_INBOUND_DOCUMENT_BYTES = TELEGRAM_FILE_DOWNLOAD_LIMIT_BYTES


DEFAULT_MAX_INBOUND_VOICE_BYTES = 5 * 1024 * 1024


DEFAULT_MAX_INBOUND_PHOTO_BYTES = 10 * 1024 * 1024


SUPPORTED_INBOUND_DOCUMENT_SUFFIXES = {".pdf", ".txt", ".md", ".html", ".htm"}


QUICK_ACTIONS_KEYBOARD = {
    "keyboard": [["/status"]],
    "resize_keyboard": True,
}


TRANSIENT_HTTP_CODES = {409, 429, 500, 502, 503, 504}


LEGACY_REPLY_PREFIX_RE = re.compile(r"^(?:ACK|PROGRESS|FINAL):\s*", re.IGNORECASE)


TELEGRAM_USER_MESSAGE_MARKER_RE = re.compile(
    r"\[TELEGRAM USER MESSAGE message_id=(\d+)\b"
)


TELEGRAM_ROUTE_ID_RE = re.compile(
    r"\[TELEGRAM (?:USER MESSAGE|REPLAY)[^\]]*?\broute_id=([^\s\]]+)"
)


DEFAULT_CODEX_AGENT_MODEL = os.environ.get("TELEAGENT_CODEX_MODEL", "gpt-5.6-sol")


DEFAULT_CODEX_AGENT_REASONING_EFFORT = os.environ.get(
    "TELEAGENT_CODEX_REASONING_EFFORT", "high"
)


ASTRA_CODEX_AGENT_MODEL = "gpt-6-astra"


SOL_CODEX_AGENT_MODEL = "gpt-5.6-sol"


LATEST_OPENAI_CODEX_AGENT_MODEL = ASTRA_CODEX_AGENT_MODEL


LATEST_OPENAI_CODEX_AGENT_REASONING_EFFORT = "high"


SPARK_CODEX_AGENT_MODEL = "gpt-5.3-codex-spark"


LUNA_CODEX_AGENT_MODEL = "gpt-5.6-luna"


DEEPSEEK_FLASH_CODEX_AGENT_MODEL = "deepseek-v4-flash"


DEEPSEEK_PRO_CODEX_AGENT_MODEL = "deepseek-v4-pro"


SUPPORTED_CODEX_AGENT_MODELS = {
    ASTRA_CODEX_AGENT_MODEL,
    SOL_CODEX_AGENT_MODEL,
    LATEST_OPENAI_CODEX_AGENT_MODEL,
    DEFAULT_CODEX_AGENT_MODEL,
    SPARK_CODEX_AGENT_MODEL,
    LUNA_CODEX_AGENT_MODEL,
    DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
    DEEPSEEK_PRO_CODEX_AGENT_MODEL,
}


CODEX_AGENT_MODEL_ALIASES = {
    "default": LATEST_OPENAI_CODEX_AGENT_MODEL,
    "latest": LATEST_OPENAI_CODEX_AGENT_MODEL,
    "astra": ASTRA_CODEX_AGENT_MODEL,
    "sol": SOL_CODEX_AGENT_MODEL,
    "spark": SPARK_CODEX_AGENT_MODEL,
    "luna": LUNA_CODEX_AGENT_MODEL,
    "ds-flash": DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
    "ds-pro": DEEPSEEK_PRO_CODEX_AGENT_MODEL,
}


LIVE_CODEX_AGENT_MODELS = frozenset(SUPPORTED_CODEX_AGENT_MODELS)


LIVE_CODEX_AGENT_MODEL_ALIASES = {
    **CODEX_AGENT_MODEL_ALIASES,
}


SUPPORTED_CODEX_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}


ASTRA_CODEX_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


LIVE_CODEX_REASONING_EFFORTS = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "xhigh": 3,
    "max": 4,
    "ultra": 4,
}


LIVE_CODEX_REASONING_ALIASES = {
    "extra high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
}


CODEX_RESET_CONFIRM_TTL_SECONDS = 300


RELAY_CONFIRMATION_RECOVERY_GRACE_SECONDS = 10


RELAY_CONFIRMATION_RECOVERY_INTERVAL_SECONDS = 30


RELAY_CONFIRMATION_RECOVERY_TIMEOUT_SECONDS = 2


INTERRUPT_CONFIRMATION_TIMEOUT_SECONDS = 10


TMUX_MUTATION_TIMEOUT_SECONDS = 30


TIMED_MESSAGE_RETRY_SECONDS = 15


TIMED_MESSAGE_DELIVERY_GRACE_SECONDS = 30


TIMED_MESSAGE_LIST_CHUNK_CHARS = 3000


TIMED_MESSAGE_LIST_PREVIEW_CHARS = 500


ACTIVE_TIMED_MESSAGE_STATUSES = {"pending", "delivering", "submitted"}


AGENT_DESIRED_RUNNING = "running"


AGENT_DESIRED_STOPPED = "stopped"


USAGE_LIMIT_REACHED_TYPES = {
    "rate_limit_reached",
    "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted",
    "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
}


USAGE_LIMIT_ERROR_CODES = {"usage_limit_exceeded", "usageLimitExceeded"}


CODEX_AUTH_ERROR_CODES = {"unauthorized"}


CODEX_AUTH_ERROR_MESSAGE_RE = re.compile(
    r"access token could not be refreshed.*(?:refresh token.*(?:revoked|already used)|"
    r"logged out|signed in to another account).*log out and sign in again",
    re.IGNORECASE | re.DOTALL,
)


DEVICE_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device\b")


CODEX_RELAY_COMMANDS = {
    "(agent-message)",
    "/agent",
    "/codex",
    "/interrupt",
    "/replay_last",
    "/replay_last_long",
    "/replay_messages",
    "/resume_goal",
}


CODEX_AUTH_BLOCKED_COMMANDS = CODEX_RELAY_COMMANDS | {
    "/model",
    "/reasoning",
    "/start_agent",
    "/restart_agent",
}


RELAYED_AGENT_ACTIONS = {"agent", "agent_document"}


SGT = timezone(timedelta(hours=8), name="SGT")


REGISTERED_CODEX_PID_CACHE: dict[str, tuple[int, str]] = {}


COMMONMARK_PUNCTUATION_RE = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~])")


GROUP_CHAT_TYPES = ("group", "supergroup")


GROUP_OWNER_MEMBER_TTL_SECONDS = 900.0


REPLY_ROUTE_MAX_AGE_SECONDS = 1800.0


REPLY_ROUTE_RECORD_MAX_AGE_SECONDS = 24 * 60 * 60


REPLY_ROUTE_RECORD_MAX_COUNT = 512


PER_CHAT_INBOX_RECORDS_ENV = "TELEAGENT_PER_CHAT_INBOX_RECORDS"


PEOPLE_MEMORY_ENV = "TELEAGENT_PEOPLE_MEMORY"


PEOPLE_MEMORY_MAX_OBSERVATIONS = 40


PEOPLE_MEMORY_PROMPT_OBSERVATIONS = 8


PEOPLE_MEMORY_MAX_TEXT_CHARS = 600


RELAY_QUEUE_GOAL_PAUSE_GRACE_SECONDS = 60


RELAY_QUEUE_GOAL_PAUSE_MAX_ATTEMPTS = 3


RELAY_PENDING_WEDGE_SECONDS = 120
