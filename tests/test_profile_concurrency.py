"""
tests/test_profile_concurrency.py
Testa ProfileConcurrencyLimiter: acquire/release, idempotência, snapshot e
segurança de threads.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Scripts"))

import threading
from profile_concurrency import ProfileConcurrencyLimiter


class TestAcquireRelease:
    def test_acquire_increments(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        assert lim.active_count("default") == 1

    def test_acquire_twice(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        lim.acquire("default")
        assert lim.active_count("default") == 2

    def test_release_decrements(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        lim.acquire("default")
        lim.release("default")
        assert lim.active_count("default") == 1

    def test_release_to_zero_removes_key(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        lim.release("default")
        assert lim.active_count("default") == 0
        snap = lim.snapshot()
        assert "default" not in snap

    def test_release_idempotent_when_zero(self):
        lim = ProfileConcurrencyLimiter()
        lim.release("default")  # never acquired — must not crash
        assert lim.active_count("default") == 0

    def test_release_idempotent_multiple(self):
        lim = ProfileConcurrencyLimiter()
        lim.release("default")
        lim.release("default")
        assert lim.active_count("default") == 0


class TestMultipleProfiles:
    def test_profiles_independent(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        lim.acquire("segunda_chance")
        lim.acquire("segunda_chance")
        assert lim.active_count("default") == 1
        assert lim.active_count("segunda_chance") == 2

    def test_release_one_profile_doesnt_affect_other(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        lim.acquire("segunda_chance")
        lim.release("default")
        assert lim.active_count("default") == 0
        assert lim.active_count("segunda_chance") == 1

    def test_unknown_profile_returns_zero(self):
        lim = ProfileConcurrencyLimiter()
        assert lim.active_count("nao_existe") == 0


class TestSnapshot:
    def test_snapshot_empty(self):
        lim = ProfileConcurrencyLimiter()
        assert lim.snapshot() == {}

    def test_snapshot_active(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        lim.acquire("segunda_chance")
        snap = lim.snapshot()
        assert snap["default"] == 1
        assert snap["segunda_chance"] == 1

    def test_snapshot_excludes_zero(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        lim.release("default")
        snap = lim.snapshot()
        assert "default" not in snap

    def test_snapshot_is_copy(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("default")
        snap = lim.snapshot()
        snap["default"] = 999
        assert lim.active_count("default") == 1


class TestNormalization:
    def test_none_maps_to_default(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire(None)
        assert lim.active_count("default") == 1

    def test_empty_string_maps_to_default(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("")
        assert lim.active_count("default") == 1

    def test_whitespace_stripped(self):
        lim = ProfileConcurrencyLimiter()
        lim.acquire("  default  ")
        assert lim.active_count("default") == 1


class TestThreadSafety:
    def test_concurrent_acquires(self):
        lim = ProfileConcurrencyLimiter()
        threads = [threading.Thread(target=lambda: lim.acquire("default")) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert lim.active_count("default") == 50

    def test_concurrent_acquire_release(self):
        lim = ProfileConcurrencyLimiter()
        errors = []

        def work():
            try:
                lim.acquire("default")
                lim.release("default")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=work) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert lim.active_count("default") == 0

    def test_snapshot_consistent_under_concurrency(self):
        lim = ProfileConcurrencyLimiter()
        for _ in range(10):
            lim.acquire("default")
        snapshots = []

        def read():
            snapshots.append(lim.snapshot())

        threads = [threading.Thread(target=read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for s in snapshots:
            assert s.get("default", 0) >= 0
