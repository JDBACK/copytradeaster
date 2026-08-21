"""Logging helpers. Private keys and signatures NEVER reach logs unmasked."""

from __future__ import annotations

import logging


def redact(secret: str) -> str:
    """Masks everything after the prefix (0x or first 2 chars) up to last 4 chars."""
    if not secret:
        return secret
    prefix = "0x" if secret.startswith("0x") else secret[:2]
    if len(secret) <= len(prefix) + 4:
        return f"{prefix}***"
    return f"{prefix}***{secret[-4:]}"


def get_logger(name: str = "copytradeaster") -> logging.Logger:
    return logging.getLogger(name)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
