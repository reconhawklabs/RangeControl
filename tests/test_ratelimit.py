from rangecontrol.bot.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_the_limit_applies_per_user():
    limiter = RateLimiter(max_calls=2, per_seconds=60, clock=FakeClock())

    assert [limiter.allow(1) for _ in range(3)] == [True, True, False]
    assert limiter.allow(2) is True


def test_the_window_slides_one_call_at_a_time():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=2, per_seconds=60, clock=clock)
    limiter.allow(1)
    clock.advance(30)
    limiter.allow(1)
    assert limiter.allow(1) is False

    clock.advance(31)  # first call ages out, second has not
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False

    clock.advance(61)  # the whole window has passed
    assert limiter.allow(1) is True
