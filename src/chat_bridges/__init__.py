"""Chat bridges: reach the Faustus agent from a chat app (Telegram today).

Opt-in and off by default; see `docs/api/chat-bridges.md`.
"""
from .telegram_bridge import (  # noqa: F401
    TelegramBridge,
    apply_settings,
    check_token,
    get_bridge,
    load_config,
    start_telegram_bridge,
    stop_telegram_bridge,
)
