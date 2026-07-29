from rangecontrol.bot.pending import MAX_PENDING, PendingRegistry, PendingRequest


def request(n: int = 1) -> PendingRequest:
    return PendingRequest(
        channel_id=100 + n, message_id=200 + n, user_id=300 + n,
        user_name=f"user{n}", question=f"q{n}", public_text=f"reply{n}",
    )


def test_a_stored_request_can_be_taken_back():
    registry = PendingRegistry()
    registry.add(999, request())
    assert registry.take(999).public_text == "reply1"


def test_taking_removes_it():
    """Two white cell members reacting must not release the reply twice."""
    registry = PendingRegistry()
    registry.add(999, request())
    assert registry.take(999) is not None
    assert registry.take(999) is None


def test_an_unknown_id_returns_none():
    assert PendingRegistry().take(12345) is None


def test_the_registry_is_bounded():
    """An exercise nobody is reviewing must not grow the process without limit."""
    registry = PendingRegistry()
    for n in range(MAX_PENDING + 10):
        registry.add(n, request(n))
    assert len(registry) == MAX_PENDING


def test_eviction_drops_the_oldest_first():
    registry = PendingRegistry()
    for n in range(MAX_PENDING + 1):
        registry.add(n, request(n))
    assert registry.take(0) is None
    assert registry.take(MAX_PENDING) is not None


# -- on_change: lets the GUI publish a live pending count --------------------


def test_on_change_fires_after_add():
    seen = []
    registry = PendingRegistry(on_change=seen.append)
    registry.add(1, request())
    assert seen == [1]


def test_on_change_fires_after_take():
    seen = []
    registry = PendingRegistry(on_change=seen.append)
    registry.add(1, request())
    registry.take(1)
    assert seen == [1, 0]


def test_on_change_does_not_fire_when_take_finds_nothing():
    seen = []
    registry = PendingRegistry(on_change=seen.append)
    registry.take(999)
    assert seen == []


def test_on_change_is_optional():
    registry = PendingRegistry()
    registry.add(1, request())  # must not raise
    registry.take(1)  # must not raise
