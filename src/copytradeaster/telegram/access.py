"""Ephemeral human-check state and typed access-flow callbacks."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from random import SystemRandom

from aiogram.filters.callback_data import CallbackData


class GuestAccessAction(StrEnum):
    ANSWER = "a"
    RETRY = "r"
    STATUS = "s"


class GuestAccessCallback(CallbackData, prefix="g", sep=":"):
    action: GuestAccessAction
    token: str = ""
    choice: int = 0


class AccessDecisionAction(StrEnum):
    APPROVE = "a"
    REJECT = "r"


class AccessDecisionCallback(CallbackData, prefix="q", sep=":"):
    action: AccessDecisionAction
    request_id: int


class CaptchaResult(StrEnum):
    CORRECT = "correct"
    WRONG = "wrong"
    EXPIRED = "expired"
    BLOCKED = "blocked"


class CaptchaRateLimitedError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("captcha attempts are temporarily limited")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    token: str
    user_id: int
    chat_id: int
    prompt: str
    choices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _CaptchaRecord:
    challenge: CaptchaChallenge
    answer: int
    expires_at: float


class CaptchaChallengeStore:
    """One process-local challenge per Telegram user with a short cooldown."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        max_attempts: int = 3,
        attempt_window_seconds: float = 300.0,
        cooldown_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or attempt_window_seconds <= 0 or cooldown_seconds <= 0:
            raise ValueError("captcha time limits must be positive")
        if max_attempts <= 0:
            raise ValueError("captcha attempt limit must be positive")
        self._ttl = float(ttl_seconds)
        self._max_attempts = int(max_attempts)
        self._attempt_window = float(attempt_window_seconds)
        self._cooldown = float(cooldown_seconds)
        self._monotonic = monotonic
        self._records: dict[int, _CaptchaRecord] = {}
        self._failures: dict[int, list[float]] = {}
        self._blocked_until: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._random = SystemRandom()

    async def begin(
        self,
        user_id: int,
        chat_id: int,
        *,
        force_new: bool = False,
    ) -> CaptchaChallenge:
        now = self._monotonic()
        async with self._lock:
            blocked_until = self._blocked_until.get(user_id, 0.0)
            if blocked_until > now:
                raise CaptchaRateLimitedError(int(blocked_until - now) + 1)
            self._blocked_until.pop(user_id, None)
            current = self._records.get(user_id)
            if (
                not force_new
                and current is not None
                and current.challenge.chat_id == chat_id
                and current.expires_at > now
            ):
                return current.challenge

            left = self._random.randint(2, 9)
            right = self._random.randint(2, 9)
            answer = left + right
            nearby = [answer - 2, answer - 1, answer + 1, answer + 2, answer + 3]
            self._random.shuffle(nearby)
            choices = [answer, *nearby[:3]]
            self._random.shuffle(choices)
            challenge = CaptchaChallenge(
                token=secrets.token_urlsafe(6),
                user_id=int(user_id),
                chat_id=int(chat_id),
                prompt=f"{left} + {right} = ?",
                choices=tuple(choices),
            )
            self._records[user_id] = _CaptchaRecord(
                challenge=challenge,
                answer=answer,
                expires_at=now + self._ttl,
            )
            return challenge

    async def verify(
        self,
        user_id: int,
        chat_id: int,
        token: str,
        choice: int,
    ) -> CaptchaResult:
        now = self._monotonic()
        async with self._lock:
            blocked_until = self._blocked_until.get(user_id, 0.0)
            if blocked_until > now:
                return CaptchaResult.BLOCKED
            record = self._records.get(user_id)
            if (
                record is None
                or record.challenge.user_id != user_id
                or record.challenge.chat_id != chat_id
                or not secrets.compare_digest(record.challenge.token, str(token))
            ):
                return CaptchaResult.EXPIRED
            if record.expires_at <= now:
                self._records.pop(user_id, None)
                return CaptchaResult.EXPIRED

            self._records.pop(user_id, None)
            if int(choice) == record.answer:
                self._failures.pop(user_id, None)
                self._blocked_until.pop(user_id, None)
                return CaptchaResult.CORRECT

            failures = [
                value
                for value in self._failures.get(user_id, ())
                if now - value <= self._attempt_window
            ]
            failures.append(now)
            if len(failures) >= self._max_attempts:
                self._failures.pop(user_id, None)
                self._blocked_until[user_id] = now + self._cooldown
                return CaptchaResult.BLOCKED
            self._failures[user_id] = failures
            return CaptchaResult.WRONG


__all__ = [
    "AccessDecisionAction",
    "AccessDecisionCallback",
    "CaptchaChallenge",
    "CaptchaChallengeStore",
    "CaptchaRateLimitedError",
    "CaptchaResult",
    "GuestAccessAction",
    "GuestAccessCallback",
]
