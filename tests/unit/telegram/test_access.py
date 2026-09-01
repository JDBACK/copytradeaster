from __future__ import annotations

import pytest

from copytradeaster.telegram.access import (
    AccessDecisionAction,
    AccessDecisionCallback,
    CaptchaChallengeStore,
    CaptchaRateLimitedError,
    CaptchaResult,
    GuestAccessAction,
    GuestAccessCallback,
)


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def _answer(prompt: str) -> int:
    left, right = (int(value.strip()) for value in prompt.removesuffix("= ?").split("+"))
    return left + right


@pytest.mark.asyncio
async def test_captcha_is_bound_single_use_expires_and_rate_limits() -> None:
    clock = Clock()
    store = CaptchaChallengeStore(
        monotonic=clock,
        ttl_seconds=10,
        max_attempts=3,
        cooldown_seconds=60,
    )
    challenge = await store.begin(7, 42)
    answer = _answer(challenge.prompt)

    assert await store.verify(8, 42, challenge.token, answer) is CaptchaResult.EXPIRED
    assert await store.verify(7, 43, challenge.token, answer) is CaptchaResult.EXPIRED
    assert await store.verify(7, 42, challenge.token, answer) is CaptchaResult.CORRECT
    assert await store.verify(7, 42, challenge.token, answer) is CaptchaResult.EXPIRED

    for expected in (CaptchaResult.WRONG, CaptchaResult.WRONG, CaptchaResult.BLOCKED):
        current = await store.begin(7, 42, force_new=True)
        wrong = next(value for value in current.choices if value != _answer(current.prompt))
        assert await store.verify(7, 42, current.token, wrong) is expected
    with pytest.raises(CaptchaRateLimitedError):
        await store.begin(7, 42)

    clock.value += 61
    assert (await store.begin(7, 42)).user_id == 7


@pytest.mark.asyncio
async def test_captcha_expiration_and_callback_budgets() -> None:
    clock = Clock()
    store = CaptchaChallengeStore(monotonic=clock, ttl_seconds=5)
    challenge = await store.begin(7, 42)
    clock.value += 6
    assert await store.verify(7, 42, challenge.token, challenge.choices[0]) is CaptchaResult.EXPIRED

    guest = GuestAccessCallback(
        action=GuestAccessAction.ANSWER,
        token="x" * 16,
        choice=18,
    ).pack()
    decision = AccessDecisionCallback(
        action=AccessDecisionAction.APPROVE,
        request_id=2_147_483_647,
    ).pack()
    assert len(guest.encode()) <= 64
    assert len(decision.encode()) <= 64
