import json
import threading

import pytest

from sync_dedup import SyncDedup, DEFAULT_DEDUP_WINDOW_SEC


class _Clock:
    """Relógio determinístico para testes."""
    def __init__(self, t0: float = 1000.0):
        self.t = float(t0)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


# ─────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────
class TestDefaults:
    def test_default_window_is_120(self):
        assert DEFAULT_DEDUP_WINDOW_SEC == 120

    def test_instance_uses_default_window(self):
        d = SyncDedup()
        assert d.window_sec == 120

    def test_custom_window(self):
        d = SyncDedup(window_sec=30)
        assert d.window_sec == 30


# ─────────────────────────────────────────────────────────
# try_acquire
# ─────────────────────────────────────────────────────────
class TestTryAcquire:
    def test_first_call_acquires(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        ok, elapsed, retry = d.try_acquire("k1")
        assert ok is True
        assert elapsed == 0
        assert retry == 0

    def test_second_call_within_window_is_blocked(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        d.try_acquire("k1")
        clock.advance(45)
        ok, elapsed, retry = d.try_acquire("k1")
        assert ok is False
        assert elapsed == 45
        assert retry == 75  # 120 - 45

    def test_after_window_acquires_again(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        d.try_acquire("k1")
        clock.advance(120)  # exatamente fora — comparação é `<`
        ok, _, _ = d.try_acquire("k1")
        assert ok is True

    def test_different_keys_are_independent(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        ok1, _, _ = d.try_acquire("a")
        ok2, _, _ = d.try_acquire("b")
        assert ok1 is True
        assert ok2 is True

    def test_retry_after_is_at_least_one(self):
        # Quando elapsed == window-1, retry = 1. Quando elapsed == window-0,
        # já estamos fora da janela e o ramo `acquired` se aplica. Mas se
        # a aritmética desse 0, devemos clampar em 1.
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        d.try_acquire("k")
        clock.advance(119)
        ok, elapsed, retry = d.try_acquire("k")
        assert ok is False
        assert elapsed == 119
        assert retry == 1

    def test_elapsed_is_int_truncated(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        d.try_acquire("k")
        clock.advance(45.9)
        _, elapsed, _ = d.try_acquire("k")
        assert isinstance(elapsed, int)
        assert elapsed == 45


# ─────────────────────────────────────────────────────────
# release
# ─────────────────────────────────────────────────────────
class TestRelease:
    def test_release_allows_immediate_reacquire(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        d.try_acquire("k")
        d.release("k")
        ok, _, _ = d.try_acquire("k")
        assert ok is True

    def test_release_missing_key_is_noop(self):
        d = SyncDedup()
        d.release("never-acquired")  # não deve levantar
        d.release("never-acquired")  # idempotente

    def test_release_does_not_affect_other_keys(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        d.try_acquire("a")
        d.try_acquire("b")
        d.release("a")
        ok_b, _, _ = d.try_acquire("b")
        # b ainda está dentro da janela
        assert ok_b is False


# ─────────────────────────────────────────────────────────
# active_count e snapshot
# ─────────────────────────────────────────────────────────
class TestActiveCount:
    def test_zero_when_empty(self):
        assert SyncDedup().active_count() == 0

    def test_increments_on_acquire(self):
        d = SyncDedup()
        d.try_acquire("a")
        d.try_acquire("b")
        assert d.active_count() == 2

    def test_does_not_increment_on_blocked_acquire(self):
        clock = _Clock()
        d = SyncDedup(window_sec=120, now_func=clock)
        d.try_acquire("a")
        d.try_acquire("a")  # bloqueado
        assert d.active_count() == 1

    def test_decrements_on_release(self):
        d = SyncDedup()
        d.try_acquire("a")
        d.try_acquire("b")
        d.release("a")
        assert d.active_count() == 1


class TestSnapshot:
    def test_snapshot_shape(self):
        d = SyncDedup(window_sec=60)
        snap = d.snapshot()
        assert snap == {"window_sec": 60, "active_keys": 0}

    def test_snapshot_reflects_state(self):
        d = SyncDedup()
        d.try_acquire("a")
        d.try_acquire("b")
        snap = d.snapshot()
        assert snap["active_keys"] == 2

    def test_snapshot_is_json_serializable(self):
        d = SyncDedup()
        d.try_acquire("x")
        # Não deve levantar
        json.dumps(d.snapshot())


# ─────────────────────────────────────────────────────────
# Concorrência (happy path com Lock real)
# ─────────────────────────────────────────────────────────
class TestThreadSafety:
    def test_only_one_acquires_under_contention(self):
        d = SyncDedup(window_sec=120)  # usa time.time real
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            ok, _, _ = d.try_acquire("contended-key")
            results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 7
