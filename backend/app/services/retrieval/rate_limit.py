"""
A client-side rate limiter.

Semantic Scholar allows **one request per second, cumulative across all endpoints**,
and its documentation asks callers to stay *below* that threshold. Handling 429 alone
is not enough for a limit that tight: a run with six sub-questions would fire six
requests, collect five rejections, and spend its whole time budget backing off from a
limit it could simply have respected. Throttling before sending is the difference
between a run that takes seven seconds and one that takes a minute and annoys the
provider.

Two properties make this correct rather than approximately correct:

**It is process-wide, keyed by provider.** "Cumulative across all endpoints" means two
`SemanticScholarService` instances must not each get a request per second. A module-level
registry gives every caller in the process the same limiter, so the budget is shared
however many services exist.

**A 429 penalises everyone, not just the caller who got it.** Without that, N queued
callers each hit the limit, each back off independently, and each retry into the same
wall. `penalise()` pushes the shared next-allowed time forward so the whole process
waits once.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Allows at most one acquisition per `min_interval` seconds.

    Fair in the sense that matters here: waiters are served in the order the event
    loop wakes them, and each one *reserves* its slot before sleeping, so two
    concurrent callers cannot both decide the same instant is free.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        name: str = "default",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must not be negative")
        self.min_interval = min_interval
        self.name = name
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        # The earliest moment the next request may be sent. Starts in the past so the
        # first request is never delayed -- a limiter that made the first call wait
        # would add latency for nothing.
        self._next_allowed = clock() - min_interval
        self.acquisitions = 0
        self.total_waited = 0.0

    async def acquire(self) -> float:
        """Wait until a request may be sent. Returns how long this call waited.

        The slot is reserved *inside* the lock, before sleeping, so concurrent callers
        queue behind each other instead of all concluding that the same moment is free.
        """
        async with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_allowed - now)
            # Reserve from whichever is later: now, or the moment we were already
            # cleared for. Using `now + interval` would let a long gap between calls
            # collapse into a burst.
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
            self.acquisitions += 1

        if wait > 0:
            self.total_waited += wait
            logger.debug("rate limiter %s: waiting %.2fs", self.name, wait)
            await self._sleep(wait)
        return wait

    def penalise(self, seconds: float) -> None:
        """Push the next allowed time forward for **every** caller.

        Called when the provider says 429. Backing off only the caller that was
        rejected leaves the others queued to make the same mistake in turn; this makes
        the whole process wait once.

        Never moves the deadline earlier, so two concurrent 429s do not cancel out.
        """
        if seconds <= 0:
            return
        target = self._clock() + seconds
        if target > self._next_allowed:
            self._next_allowed = target
            logger.info("rate limiter %s: penalised for %.2fs", self.name, seconds)

    @property
    def seconds_until_next(self) -> float:
        """How long a request would have to wait right now. For logs and diagnostics."""
        return max(0.0, self._next_allowed - self._clock())

    def reset(self) -> None:
        """Forget all history. For tests, and after a long idle period."""
        self._next_allowed = self._clock() - self.min_interval
        self.acquisitions = 0
        self.total_waited = 0.0


# --------------------------------------------------------------------------- #
# The process-wide registry
# --------------------------------------------------------------------------- #

_limiters: dict[str, AsyncRateLimiter] = {}


def get_limiter(provider: str, min_interval: float) -> AsyncRateLimiter:
    """The shared limiter for a provider, created on first use.

    Shared because the provider's limit is per *account*, not per object. Two service
    instances in one process are still one account, and giving each its own limiter
    would double the request rate and get both rejected.

    If `min_interval` changes between calls -- a setting edited at runtime, or a test
    asking for something faster -- the existing limiter is updated rather than replaced,
    so callers already holding a reference are not left on the old budget.
    """
    limiter = _limiters.get(provider)
    if limiter is None:
        limiter = AsyncRateLimiter(min_interval, name=provider)
        _limiters[provider] = limiter
    elif limiter.min_interval != min_interval:
        logger.info(
            "rate limiter %s: interval changed %.2fs -> %.2fs",
            provider,
            limiter.min_interval,
            min_interval,
        )
        limiter.min_interval = min_interval
    return limiter


def reset_limiters() -> None:
    """Clear the registry. For test isolation -- one test's throttle must not leak
    into the next, which would make timings depend on test order."""
    _limiters.clear()
