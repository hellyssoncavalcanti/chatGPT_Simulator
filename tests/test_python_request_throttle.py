"""Testes offline de `Scripts/python_request_throttle.py` (padrão B).

Cobre os 4 métodos públicos de :class:`PythonRequestThrottle` + o
state-machine completo do throttle global de pedidos Python:

1. ``begin()`` — curto-circuitos (limites zero, first-call) e cálculo
   de ``(base, target, last_ts)`` via ``compute_python_request_interval``.
2. ``remaining_seconds(target, last_ts)`` — pura, idempotente, clamp em 0.
3. ``commit()`` — atualiza ``_last_ts`` para ``now()`` após espera.
4. ``snapshot()`` — read-only thread-safe.

State-machine:
- Sequência ``begin() → tight-loop com remaining_seconds() → commit()``
  reproduz o fluxo de ``server._wait_python_request_interval_if_needed``.
"""

import threading
import time

import pytest

from python_request_throttle import PythonRequestThrottle


class _FakeClock:
    """Relógio determinístico que avança apenas quando solicitado."""

    def __init__(self, t: float = 1000.0) -> None:
        self._t = float(t)

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += float(dt)


# ─────────────────────────────────────────────────────────
# begin() — curto-circuitos
# ─────────────────────────────────────────────────────────
class TestBeginShortCircuits:
    def test_both_limits_zero_returns_none_and_records(self):
        clock = _FakeClock(t=1000.0)
        t = PythonRequestThrottle(now_func=clock)
        result = t.begin(0, 0, 2)
        assert result is None
        assert t.snapshot()["last_ts"] == 1000.0

    def test_negative_limits_treated_as_zero(self):
        clock = _FakeClock(t=2000.0)
        t = PythonRequestThrottle(now_func=clock)
        result = t.begin(-5, -10, 2)
        assert result is None
        assert t.snapshot()["last_ts"] == 2000.0

    def test_first_call_returns_none_and_records(self):
        # last_ts inicial é 0.0; primeira chamada deve só registrar.
        clock = _FakeClock(t=500.0)
        t = PythonRequestThrottle(now_func=clock)
        result = t.begin(10, 60, 2)
        assert result is None
        assert t.snapshot()["last_ts"] == 500.0

    def test_first_call_resets_last_ts_to_now(self):
        clock = _FakeClock(t=750.0)
        t = PythonRequestThrottle(now_func=clock)
        t.begin(10, 60, 2)
        assert t.snapshot()["last_ts"] == 750.0
        # Avança e segue com a SEGUNDA chamada — não é mais first-call.
        clock.advance(5.0)
        result = t.begin(10, 60, 2)
        assert result is not None  # agora aguarda


# ─────────────────────────────────────────────────────────
# begin() — cálculo de tupla
# ─────────────────────────────────────────────────────────
class TestBeginComputesTuple:
    def test_returns_base_target_last_ts_after_init(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        # Primeira chamada apenas registra.
        assert t.begin(10, 60, 2) is None
        # Segunda chamada: calcula (base, target, last_ts).
        clock.advance(2.0)
        result = t.begin(10, 60, 2, rng=lambda lo, hi: 30.0)
        assert result is not None
        base, target, last_ts = result
        assert base == 30.0
        assert target == 15.0  # 30 / 2 perfis
        assert last_ts == 100.0  # ainda é o last_ts da primeira chamada

    def test_profile_count_one_returns_base_equal_target(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t.begin(10, 60, 1)  # init
        result = t.begin(10, 60, 1, rng=lambda lo, hi: 30.0)
        base, target, _ = result
        assert base == 30.0
        assert target == 30.0

    def test_rng_is_passed_to_compute(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t.begin(10, 60, 2)  # init
        seen = {}
        def rng(lo, hi):
            seen["lo"] = lo
            seen["hi"] = hi
            return lo
        t.begin(10, 60, 2, rng=rng)
        assert seen == {"lo": 10.0, "hi": 60.0}

    def test_last_ts_NOT_updated_on_waiting_call(self):
        # begin() retornando tupla NÃO mexe em last_ts (commit faz isso).
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t.begin(10, 60, 2)  # init: last_ts = 100
        clock.advance(20.0)  # now = 120
        t.begin(10, 60, 2, rng=lambda lo, hi: 30.0)
        assert t.snapshot()["last_ts"] == 100.0  # inalterado pela 2a chamada


# ─────────────────────────────────────────────────────────
# remaining_seconds() — função pura
# ─────────────────────────────────────────────────────────
class TestRemainingSeconds:
    def test_zero_when_target_already_elapsed(self):
        clock = _FakeClock(t=200.0)
        t = PythonRequestThrottle(now_func=clock)
        # last_ts=100, target=30 → elapsed=100, remaining = max(0, 30-100) = 0
        assert t.remaining_seconds(target=30.0, last_ts=100.0) == 0.0

    def test_full_target_when_no_elapsed(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        assert t.remaining_seconds(target=30.0, last_ts=100.0) == 30.0

    def test_partial_remaining(self):
        clock = _FakeClock(t=110.0)
        t = PythonRequestThrottle(now_func=clock)
        # elapsed=10, remaining = 30-10 = 20
        assert t.remaining_seconds(target=30.0, last_ts=100.0) == 20.0

    def test_clamps_negative_to_zero(self):
        clock = _FakeClock(t=999999.0)
        t = PythonRequestThrottle(now_func=clock)
        assert t.remaining_seconds(target=5.0, last_ts=10.0) == 0.0

    def test_does_not_acquire_lock_or_mutate_state(self):
        # Garante que remaining_seconds é "view only".
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t._force_last_ts(50.0)
        before = t.snapshot()
        t.remaining_seconds(30.0, 50.0)
        t.remaining_seconds(60.0, 50.0)
        assert t.snapshot() == before


# ─────────────────────────────────────────────────────────
# commit() — registra término da espera
# ─────────────────────────────────────────────────────────
class TestCommit:
    def test_commit_sets_last_ts_to_now(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t._force_last_ts(50.0)
        clock.advance(75.0)  # now=175
        t.commit()
        assert t.snapshot()["last_ts"] == 175.0

    def test_commit_idempotent_at_same_clock(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t.commit()
        t.commit()
        assert t.snapshot()["last_ts"] == 100.0

    def test_commit_after_full_cycle(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        # 1) init
        t.begin(10, 60, 2)
        assert t.snapshot()["last_ts"] == 100.0
        # 2) advance, begin returns waiting tuple
        clock.advance(5.0)  # 105
        result = t.begin(10, 60, 2, rng=lambda lo, hi: 30.0)
        assert result is not None
        # 3) tight-loop simulado: caller termina espera
        clock.advance(15.0)  # 120
        t.commit()
        assert t.snapshot()["last_ts"] == 120.0


# ─────────────────────────────────────────────────────────
# snapshot() — read-only thread-safe
# ─────────────────────────────────────────────────────────
class TestSnapshot:
    def test_initial_snapshot_is_zero(self):
        t = PythonRequestThrottle(now_func=lambda: 100.0)
        snap = t.snapshot()
        assert snap == {"last_ts": 0.0, "age_seconds": 0.0}

    def test_snapshot_reflects_last_ts(self):
        clock = _FakeClock(t=300.0)
        t = PythonRequestThrottle(now_func=clock)
        t.commit()
        snap = t.snapshot()
        assert snap["last_ts"] == 300.0
        assert snap["age_seconds"] == 0.0  # acabou de commitar

    def test_snapshot_age_seconds_advances_with_clock(self):
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t.commit()  # last_ts=100
        assert t.snapshot()["age_seconds"] == 0.0
        clock.advance(7.5)
        assert t.snapshot()["age_seconds"] == 7.5

    def test_snapshot_age_zero_when_last_ts_never_set(self):
        clock = _FakeClock(t=99999.0)
        t = PythonRequestThrottle(now_func=clock)
        # Nunca houve commit/begin → age_seconds=0 mesmo com clock alto.
        assert t.snapshot()["age_seconds"] == 0.0
        assert t.snapshot()["last_ts"] == 0.0

    def test_snapshot_age_clamps_negative_to_zero(self):
        # Clock retrocedeu (ex.: ajuste NTP) — age nunca negativo.
        clock = _FakeClock(t=200.0)
        t = PythonRequestThrottle(now_func=clock)
        t.commit()  # last_ts=200
        clock._t = 150.0  # retrocede
        assert t.snapshot()["age_seconds"] == 0.0

    def test_snapshot_returns_dict(self):
        t = PythonRequestThrottle(now_func=lambda: 50.0)
        snap = t.snapshot()
        assert isinstance(snap, dict)
        assert "last_ts" in snap
        assert "age_seconds" in snap

    def test_snapshot_thread_safe_under_concurrent_commit(self):
        # Garantia básica: sob 4 threads commitando, snapshot nunca quebra.
        t = PythonRequestThrottle(now_func=time.time)
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                t.commit()

        threads = [threading.Thread(target=writer, daemon=True) for _ in range(4)]
        for th in threads:
            th.start()
        try:
            for _ in range(200):
                snap = t.snapshot()
                assert "last_ts" in snap
        finally:
            stop.set()
            for th in threads:
                th.join(timeout=1.0)


# ─────────────────────────────────────────────────────────
# State-machine ponta-a-ponta
# ─────────────────────────────────────────────────────────
class TestFullStateMachine:
    def test_simulates_server_wait_loop(self):
        """Reproduz o fluxo de ``_wait_python_request_interval_if_needed``."""
        clock = _FakeClock(t=1000.0)
        t = PythonRequestThrottle(now_func=clock)
        rng = lambda lo, hi: 60.0  # base sempre 60

        # Primeira chamada: registra e retorna.
        assert t.begin(10, 120, 2, rng=rng) is None
        assert t.snapshot()["last_ts"] == 1000.0

        # Segunda chamada: precisa esperar.
        clock.advance(5.0)  # now=1005, elapsed=5s desde last_ts
        result = t.begin(10, 120, 2, rng=rng)
        assert result is not None
        base, target, last_ts = result
        assert base == 60.0
        assert target == 30.0  # 60 / 2 perfis
        assert last_ts == 1000.0

        # Tight-loop: chama remaining_seconds até ficar 0.
        remaining_history = []
        loop_count = 0
        while True:
            r = t.remaining_seconds(target, last_ts)
            remaining_history.append(r)
            if r <= 0:
                break
            clock.advance(min(5.0, r))  # tick de 5s ou menos
            loop_count += 1
            assert loop_count < 50  # safety

        assert remaining_history[0] == 25.0  # target=30, elapsed=5
        assert remaining_history[-1] == 0.0
        # commit registra o término.
        t.commit()
        assert t.snapshot()["last_ts"] == clock()  # = 1030

    def test_no_wait_when_limits_zero(self):
        clock = _FakeClock(t=500.0)
        t = PythonRequestThrottle(now_func=clock)
        # begin() retorna None imediatamente em ambos cenários.
        assert t.begin(0, 0, 2) is None
        assert t.snapshot()["last_ts"] == 500.0
        clock.advance(60.0)
        assert t.begin(0, 0, 2) is None
        assert t.snapshot()["last_ts"] == 560.0

    def test_default_now_func_is_time_time(self):
        # Sem injeção, usa time.time(); apenas garante que não levanta.
        t = PythonRequestThrottle()
        # begin com both<=0 deve registrar e retornar.
        assert t.begin(0, 0, 1) is None
        snap = t.snapshot()
        assert snap["last_ts"] > 0


# ─────────────────────────────────────────────────────────
# Equivalência com a implementação histórica em server.py
# ─────────────────────────────────────────────────────────
class TestEquivalenceWithLegacyImplementation:
    """Confirma que o state-machine produz exatamente os mesmos efeitos
    observáveis que o `_wait_python_request_interval_if_needed` legado."""

    def test_legacy_short_circuit_zero_limits(self):
        """Legacy: pmin<=0 and pmax<=0 → registra last_ts e retorna sem esperar."""
        clock = _FakeClock(t=42.0)
        t = PythonRequestThrottle(now_func=clock)
        assert t.begin(0, 0, 2) is None
        assert t.snapshot()["last_ts"] == 42.0
        # Após zero-limit, NUNCA entra em loop.
        clock.advance(1000.0)
        assert t.begin(0, 0, 2) is None  # registra de novo

    def test_legacy_first_call_skips_loop(self):
        """Legacy: last_ts==0 (boot) → registra e retorna sem esperar."""
        clock = _FakeClock(t=10.0)
        t = PythonRequestThrottle(now_func=clock)
        assert t.begin(10, 60, 2) is None
        assert t.snapshot()["last_ts"] == 10.0

    def test_legacy_target_divided_by_profile_count(self):
        """Legacy: target = base / profile_count."""
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t.begin(10, 60, 2)  # init
        _, target1, _ = t.begin(10, 60, 2, rng=lambda lo, hi: 60.0)
        assert target1 == 30.0  # 60/2

        t._force_last_ts(50.0)  # reseta para forçar nova rodada
        _, target2, _ = t.begin(10, 60, 4, rng=lambda lo, hi: 60.0)
        assert target2 == 15.0  # 60/4

    def test_legacy_commit_after_loop(self):
        """Legacy: após loop, last_ts = time.time()."""
        clock = _FakeClock(t=100.0)
        t = PythonRequestThrottle(now_func=clock)
        t._force_last_ts(50.0)
        clock.advance(40.0)  # now=140
        t.commit()
        assert t.snapshot()["last_ts"] == 140.0
