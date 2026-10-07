"""
The client-side rate limiter.

Semantic Scholar allows one request per second, cumulative across all endpoints. These
tests use a fake clock and a fake sleep, so the behaviour is asserted rather than waited
for — a test that genuinely slept one second per request would take a minute and nobody
would run it.
"""

import asyncio
from itertools import pairwise

import pytest

from app.services.retrieval.rate_limit import (
    AsyncRateLimiter,
    get_limiter,
    reset_limiters,
)


class FakeClock:
    """A clock that only advances when something sleeps.

    This is what makes the tests deterministic: real elapsed time plays no part, so a
    slow machine cannot change the result.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def limiter(clock: FakeClock, interval: float = 1.1) -> AsyncRateLimiter:
    return AsyncRateLimiter(interval, name="test", clock=clock, sleep=clock.sleep)


@pytest.fixture(autouse=True)
def _isolate_registry():
    """One test's throttle must not leak into the next, or timings would depend on
    test order."""
    reset_limiters()
    yield
    reset_limiters()


class TestFirstRequest:
    @pytest.mark.asyncio
    async def test_the_first_request_is_never_delayed(self, clock):
        """A limiter that made the first call wait would add latency for nothing."""
        assert await limiter(clock).acquire() == 0.0
        assert clock.slept == []


class TestSpacing:
    @pytest.mark.asyncio
    async def test_the_second_request_waits_a_full_interval(self, clock):
        rl = limiter(clock, 1.1)
        await rl.acquire()
        waited = await rl.acquire()
        assert waited == pytest.approx(1.1)

    @pytest.mark.asyncio
    async def test_requests_are_spaced_not_merely_counted(self, clock):
        rl = limiter(clock, 1.0)
        for _ in range(4):
            await rl.acquire()
        # Three waits of one second: the first went straight through.
        assert clock.slept == pytest.approx([1.0, 1.0, 1.0])

    @pytest.mark.asyncio
    async def test_time_already_elapsed_counts_toward_the_interval(self, clock):
        """A caller that waited on its own should not then wait again."""
        rl = limiter(clock, 1.0)
        await rl.acquire()
        clock.advance(1.0)
        assert await rl.acquire() == 0.0

    @pytest.mark.asyncio
    async def test_a_long_idle_period_does_not_bank_up_a_burst(self, clock):
        """Reserving from `now + interval` after a gap would let ten idle seconds buy
        ten instant requests, which is exactly what the provider is rate-limiting."""
        rl = limiter(clock, 1.0)
        await rl.acquire()
        clock.advance(10.0)
        assert await rl.acquire() == 0.0
        assert await rl.acquire() == pytest.approx(1.0)


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_callers_are_spaced_a_full_interval_apart(self, clock):
        """The requirement is one request per second, so what matters is the interval
        between the moments requests are actually *sent*.

        Each waiter's own wait is computed when it takes the lock, by which time earlier
        sleeps have already advanced the clock -- so the individual waits are not
        0, 1, 2, 3 but 0, 1, 1, 1. The send times are what must be a second apart, and
        they are.
        """
        rl = limiter(clock, 1.0)
        send_times: list[float] = []

        async def send() -> None:
            await rl.acquire()
            send_times.append(clock.now)

        await asyncio.gather(*(send() for _ in range(4)))

        send_times.sort()
        gaps = [b - a for a, b in pairwise(send_times)]
        assert all(gap >= 1.0 for gap in gaps), f"sent too close together: {gaps}"
        # Four requests at one per second cannot finish in less than three seconds.
        assert send_times[-1] - send_times[0] == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_a_slot_is_reserved_before_sleeping(self, clock):
        """Otherwise two concurrent callers would both read the same clock, both decide
        the moment is free, and both send -- which is the bug this ordering prevents."""
        rl = limiter(clock, 1.0)
        await asyncio.gather(*(rl.acquire() for _ in range(3)))
        # Three requests consumed three slots, so a fourth must still wait.
        assert rl.seconds_until_next > 0

    @pytest.mark.asyncio
    async def test_every_acquisition_is_counted(self, clock):
        rl = limiter(clock, 1.0)
        await asyncio.gather(*(rl.acquire() for _ in range(5)))
        assert rl.acquisitions == 5


class TestPenalty:
    @pytest.mark.asyncio
    async def test_a_penalty_delays_every_caller_not_just_the_one_rejected(self, clock):
        """Backing off only the caller that got the 429 leaves the others queued to
        make the same mistake in turn."""
        rl = limiter(clock, 1.0)
        await rl.acquire()
        rl.penalise(5.0)
        assert await rl.acquire() == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_a_penalty_never_moves_the_deadline_earlier(self, clock):
        """So two concurrent 429s cannot cancel each other out."""
        rl = limiter(clock, 1.0)
        rl.penalise(10.0)
        rl.penalise(1.0)
        assert await rl.acquire() == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_a_zero_or_negative_penalty_is_ignored(self, clock):
        rl = limiter(clock, 1.0)
        rl.penalise(0)
        rl.penalise(-5)
        assert await rl.acquire() == 0.0


class TestBookkeeping:
    @pytest.mark.asyncio
    async def test_total_wait_is_tracked_so_throttling_can_be_reported(self, clock):
        """A search that spent four seconds queued, not on the network, is worth
        distinguishing in a benchmark's timing."""
        rl = limiter(clock, 1.0)
        for _ in range(3):
            await rl.acquire()
        assert rl.total_waited == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_seconds_until_next_reports_the_current_wait(self, clock):
        rl = limiter(clock, 2.0)
        await rl.acquire()
        assert rl.seconds_until_next == pytest.approx(2.0)
        clock.advance(2.0)
        assert rl.seconds_until_next == 0.0

    @pytest.mark.asyncio
    async def test_reset_clears_history(self, clock):
        rl = limiter(clock, 1.0)
        await rl.acquire()
        await rl.acquire()
        rl.reset()
        assert rl.acquisitions == 0
        assert rl.total_waited == 0.0
        assert await rl.acquire() == 0.0

    def test_a_negative_interval_is_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            AsyncRateLimiter(-1.0)

    @pytest.mark.asyncio
    async def test_a_zero_interval_never_waits(self, clock):
        """So a provider without a limit can share the same code path."""
        rl = limiter(clock, 0.0)
        for _ in range(5):
            assert await rl.acquire() == 0.0


class TestSharedRegistry:
    def test_one_limiter_per_provider_across_the_process(self):
        """The provider's limit is per account, not per object. Two services in one
        process are still one account, so giving each its own limiter would double the
        request rate and get both rejected."""
        assert get_limiter("semantic_scholar", 1.1) is get_limiter("semantic_scholar", 1.1)

    def test_different_providers_get_different_limiters(self):
        assert get_limiter("a", 1.0) is not get_limiter("b", 1.0)

    def test_a_changed_interval_updates_the_existing_limiter(self):
        """Rather than replacing it, so a caller already holding a reference is not
        left enforcing the old budget."""
        first = get_limiter("semantic_scholar", 1.1)
        second = get_limiter("semantic_scholar", 2.0)
        assert first is second
        assert first.min_interval == 2.0

    @pytest.mark.asyncio
    async def test_two_service_instances_share_one_budget(self):
        from app.core.config import Settings
        from app.services.retrieval import SemanticScholarService

        settings = Settings(SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS=1.1)
        a = SemanticScholarService(settings=settings)
        b = SemanticScholarService(settings=settings)
        try:
            assert a.rate_limiter is b.rate_limiter
        finally:
            await a.aclose()
            await b.aclose()
