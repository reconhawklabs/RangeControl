from rangecontrol.bot.pending import MAX_PENDING, PendingRegistry, PendingRequest


def request(n: int = 1) -> PendingRequest:
    return PendingRequest(
        channel_id=100 + n, message_id=200 + n, user_id=300 + n,
        user_name=f"user{n}", question=f"q{n}", public_text=f"reply{n}",
    )


def test_a_request_can_be_taken_back_only_once():
    """Two white cell members reacting must not release the reply twice."""
    registry = PendingRegistry()
    registry.add(999, request())

    assert registry.take(999).public_text == "reply1"
    assert registry.take(999) is None
    assert registry.take(12345) is None


def test_the_registry_is_bounded_and_evicts_the_oldest_first():
    """An exercise nobody is reviewing must not grow the process without limit."""
    registry = PendingRegistry()
    for n in range(MAX_PENDING + 10):
        registry.add(n, request(n))

    assert len(registry) == MAX_PENDING
    assert registry.take(9) is None
    assert registry.take(10) is not None


def test_on_change_reports_the_live_count_only_when_it_changes():
    """Feeds the GUI's "N awaiting review" count."""
    seen = []
    registry = PendingRegistry(on_change=seen.append)

    registry.add(1, request())
    registry.take(1)
    registry.take(1)  # nothing left to take

    assert seen == [1, 0]
