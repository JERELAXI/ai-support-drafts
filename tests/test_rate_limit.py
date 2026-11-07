import fakeredis
import pytest

from app import rate_limit


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


def _freeze_time(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    # Fixed clock: a window boundary between calls would reset the counter.
    monkeypatch.setattr(rate_limit.time, "time", lambda: now)


def test_fourth_acquire_over_limit_is_refused(
    client: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_time(monkeypatch, 1_800_000_015.5)  # 15 s into a window

    results = [rate_limit.acquire(client, limit=3) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.retry_after for r in results[:3]] == [0, 0, 0]
    assert 1 <= results[3].retry_after <= 60
    assert results[3].retry_after == 45  # until the start of the next minute
    key = f"{rate_limit.KEY_PREFIX}:{1_800_000_015 // rate_limit.WINDOW_SECONDS}"
    assert 0 < client.ttl(key) <= rate_limit.WINDOW_SECONDS


def test_next_window_allows_again(
    client: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_time(monkeypatch, 1_800_000_059.0)
    for _ in range(3):
        rate_limit.acquire(client, limit=3)
    refused = rate_limit.acquire(client, limit=3)
    assert not refused.allowed
    assert refused.retry_after == 1

    _freeze_time(monkeypatch, 1_800_000_060.0)
    assert rate_limit.acquire(client, limit=3).allowed
