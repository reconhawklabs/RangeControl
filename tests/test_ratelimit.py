from rangecontrol.bot.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_allows_up_to_the_limit():
    limiter = RateLimiter(max_calls=3, per_seconds=60, clock=FakeClock())
    assert [limiter.allow(1) for _ in range(3)] == [True, True, True]


def test_blocks_beyond_the_limit():
    limiter = RateLimiter(max_calls=2, per_seconds=60, clock=FakeClock())
    limiter.allow(1)
    limiter.allow(1)
    assert limiter.allow(1) is False


def test_limits_are_per_user():
    limiter = RateLimiter(max_calls=1, per_seconds=60, clock=FakeClock())
    assert limiter.allow(1) is True
    assert limiter.allow(2) is True
    assert limiter.allow(1) is False


def test_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=1, per_seconds=60, clock=clock)
    assert limiter.allow(1) is True
    clock.advance(61)
    assert limiter.allow(1) is True


def test_partial_window_expiry_frees_one_slot():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=2, per_seconds=60, clock=clock)
    limiter.allow(1)
    clock.advance(30)
    limiter.allow(1)
    assert limiter.allow(1) is False
    clock.advance(31)  # first call ages out, second has not
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False
