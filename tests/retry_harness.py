import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable
from typing import Callable
from typing import TypeVar

T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_backoff_ms: float = 10.0
    max_backoff_ms: float = 200.0
    jitter_ms: float = 5.0
    multiplier: float = 2.0


async def with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    seed: int = 0,
) -> T:
    rng = random.Random(seed)
    attempt = 0
    backoff_ms = max(0.0, float(policy.base_backoff_ms))

    while True:
        attempt += 1
        try:
            return await op()
        except TimeoutError:
            if attempt >= max(1, int(policy.max_attempts)):
                raise
            jitter = rng.uniform(-policy.jitter_ms, policy.jitter_ms)
            sleep_ms = max(0.0, backoff_ms + jitter)
            if sleep_ms > 0:
                await asyncio.sleep(sleep_ms / 1000.0)
            backoff_ms = min(
                max(backoff_ms, 0.0) * max(1.0, float(policy.multiplier)),
                max(0.0, float(policy.max_backoff_ms)),
            )

