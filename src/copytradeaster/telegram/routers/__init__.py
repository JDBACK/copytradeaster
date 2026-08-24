"""Telegram router composition kept separate from runtime lifecycle."""

from copytradeaster.telegram.routers.common import create_common_router
from copytradeaster.telegram.routers.settings import create_settings_router

__all__ = ["create_common_router", "create_settings_router"]
