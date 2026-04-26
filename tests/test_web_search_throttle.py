"""Testes offline para `Scripts/web_search_throttle.py` (padrão B)."""

import threading
import time

from web_search_throttle import WebSearchThrottle


class _FakeClock:
    def __init__(self, t=1000.0):
        self._t = float(t)

    def __call__(self):
        return self._t

    def set(self, t):
        self._t = float(t)


class TestReserveSlot:
    def test_first_reservation_has_no_wait(self):
        clock = _FakeClock(100.0)
        t = WebSearchThrottle(now_func=clock, rng_func=lambda lo, hi: 12.0)
        slot = t.reserve_slot(8, 22)
        assert slot["interval_sec"] == 12.0
        assert slot["requested_at"] == 100.0
        assert slot["scheduled_start_at"] == 100.0
        assert slot["wait_seconds"] == 0.0

    def test_second_reservation_waits_using_last_started_plus_new_interval(self):
        clock = _FakeClock(100.0)
        t = WebSearchThrottle(now_func=clock, rng_func=lambda lo, hi: 10.0)
        t.reserve_slot(8, 22)  # scheduled=100
        clock.set(101.0)
        slot = t.reserve_slot(8, 22)
        # earliest = max(now=101, last_started(100)+interval(10)=110)
        assert slot["scheduled_start_at"] == 110.0
        assert slot["wait_seconds"] == 9.0

    def test_wait_is_zero_when_now_already_after_earliest(self):
        clock = _FakeClock(100.0)
        t = WebSearchThrottle(now_func=clock, rng_func=lambda lo, hi: 8.0)
        t.reserve_slot(8, 22)  # scheduled=100
        clock.set(120.0)
        slot = t.reserve_slot(8, 22)
        assert slot["scheduled_start_at"] == 120.0
        assert slot["wait_seconds"] == 0.0

    def test_negative_bounds_are_clamped_to_zero(self):
        clock = _FakeClock(10.0)
        t = WebSearchThrottle(now_func=clock, rng_func=lambda lo, hi: lo)
        slot = t.reserve_slot(-5, -1)
        assert slot["interval_sec"] == 0.0
        assert slot["wait_seconds"] == 0.0

    def test_max_less_than_min_is_normalized(self):
        clock = _FakeClock(10.0)
        seen = {}

        def rng(lo, hi):
            seen["lo"] = lo
            seen["hi"] = hi
            return hi

        t = WebSearchThrottle(now_func=clock, rng_func=rng)
        slot = t.reserve_slot(20, 10)
        assert seen == {"lo": 20.0, "hi": 20.0}
        assert slot["interval_sec"] == 20.0


class TestSnapshot:
    def test_snapshot_initial_state(self):
        t = WebSearchThrottle(now_func=lambda: 1.0)
        assert t.snapshot() == {"last_started_at": 0.0, "last_interval_sec": 0.0}

    def test_snapshot_reflects_last_state(self):
        clock = _FakeClock(100.0)
        t = WebSearchThrottle(now_func=clock, rng_func=lambda lo, hi: 9.0)
        t.reserve_slot(8, 22)
        assert t.snapshot() == {"last_started_at": 100.0, "last_interval_sec": 9.0}

    def test_snapshot_thread_safe_under_concurrent_reservations(self):
        t = WebSearchThrottle(now_func=time.time, rng_func=lambda lo, hi: 8.0)
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                t.reserve_slot(8, 22)

        threads = [threading.Thread(target=writer, daemon=True) for _ in range(4)]
        for th in threads:
            th.start()
        try:
            for _ in range(200):
                snap = t.snapshot()
                assert "last_started_at" in snap
                assert "last_interval_sec" in snap
        finally:
            stop.set()


class TestForceState:
    def test_force_state_changes_snapshot(self):
        t = WebSearchThrottle(now_func=lambda: 10.0)
        t._force_state(last_started_at=77.0, last_interval_sec=11.0)
        assert t.snapshot() == {"last_started_at": 77.0, "last_interval_sec": 11.0}
