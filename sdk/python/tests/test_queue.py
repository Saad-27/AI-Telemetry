from yourpkg._queue import BoundedQueue


def test_drops_newest_when_full_and_counts() -> None:
    q: BoundedQueue[int] = BoundedQueue(2)
    assert q.put(1) and q.put(2)
    assert not q.put(3)
    assert q.take(10) == [1, 2]
    assert q.take_dropped() == 1
    assert q.take_dropped() == 0


def test_take_is_fifo_and_partial() -> None:
    q: BoundedQueue[int] = BoundedQueue(10)
    for i in range(5):
        q.put(i)
    assert q.take(2) == [0, 1]
    assert len(q) == 3
    assert q.take(0) == []
