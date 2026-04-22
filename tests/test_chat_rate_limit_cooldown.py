"""Testes do módulo `chat_rate_limit_cooldown` (Lote P1 opção 1).

Cobertura-alvo: ≥3 casos por método público (`register`, `remaining_seconds`,
`snapshot`) + teste do backoff exponencial end-to-end (strikes 0→6, clamp
em 1800s).
"""

from chat_rate_limit_cooldown import ChatRateLimitCooldown


def _cooldown(now=1000.0, **kw):
    """Cria um `ChatRateLimitCooldown` com `now_func` determinístico."""
    clock = {"t": now}
    kw.setdefault("default_cooldown_sec", 240)
    kw.setdefault("max_cooldown_sec", 1800)
    kw.setdefault("max_strikes", 6)
    c = ChatRateLimitCooldown(now_func=lambda: clock["t"], **kw)
    return c, clock


class TestRegister:
    def test_first_call_uses_default_cooldown_and_resets_strikes(self):
        c, _ = _cooldown()
        adjusted = c.register()
        assert adjusted == 240
        snap = c.snapshot()
        assert snap["strikes"] == 0
        assert snap["remaining_seconds"] == 240

    def test_retry_after_seconds_overrides_default(self):
        c, _ = _cooldown()
        adjusted = c.register(retry_after_seconds=90)
        assert adjusted == 90
        assert c.snapshot()["remaining_seconds"] == 90

    def test_consecutive_hits_within_window_increment_strikes(self):
        c, clock = _cooldown()
        first = c.register(retry_after_seconds=60)
        assert first == 60
        assert c.snapshot()["strikes"] == 0
        # Avança o relógio, mas ainda dentro do cooldown.
        clock["t"] += 10
        second = c.register(retry_after_seconds=60)
        # Segunda infração: strikes=1 → 60 * 2 = 120
        assert second == 120
        assert c.snapshot()["strikes"] == 1

    def test_hit_after_cooldown_expires_resets_strikes(self):
        c, clock = _cooldown()
        c.register(retry_after_seconds=60)
        clock["t"] += 10
        c.register(retry_after_seconds=60)  # strikes=1
        assert c.snapshot()["strikes"] == 1
        # Avança bem além do until_ts para expirar o cooldown.
        clock["t"] += 10_000
        adjusted = c.register(retry_after_seconds=60)
        assert adjusted == 60
        assert c.snapshot()["strikes"] == 0

    def test_until_ts_never_regresses(self):
        c, clock = _cooldown()
        c.register(retry_after_seconds=500)  # until_ts = 1500
        until_high = c.snapshot()["until_ts"]
        # Registra de novo mais cedo com cooldown menor (dentro da janela).
        clock["t"] += 1
        c.register(retry_after_seconds=10)
        # Com strikes=1 → 10*2 = 20s a partir do now atual (1001) = 1021.
        # Mas `_until_ts` não pode regredir de 1500.
        assert c.snapshot()["until_ts"] == until_high


class TestRemainingSeconds:
    def test_zero_when_never_registered(self):
        c, _ = _cooldown()
        assert c.remaining_seconds() == 0.0

    def test_positive_immediately_after_register(self):
        c, _ = _cooldown()
        c.register(retry_after_seconds=120)
        assert c.remaining_seconds() == 120.0

    def test_decreases_as_clock_advances(self):
        c, clock = _cooldown()
        c.register(retry_after_seconds=100)
        clock["t"] += 30
        assert c.remaining_seconds() == 70.0

    def test_returns_zero_after_expiry(self):
        c, clock = _cooldown()
        c.register(retry_after_seconds=60)
        clock["t"] += 1000
        assert c.remaining_seconds() == 0.0


class TestSnapshot:
    def test_initial_snapshot(self):
        c, _ = _cooldown()
        snap = c.snapshot()
        assert snap == {"remaining_seconds": 0.0, "strikes": 0, "until_ts": 0.0}

    def test_snapshot_after_single_register(self):
        c, _ = _cooldown()
        c.register(retry_after_seconds=240)
        snap = c.snapshot()
        assert snap["strikes"] == 0
        assert snap["remaining_seconds"] == 240
        assert snap["until_ts"] == 1240.0  # now(1000) + 240

    def test_snapshot_reflects_strikes(self):
        c, clock = _cooldown()
        c.register(retry_after_seconds=60)
        clock["t"] += 10
        c.register(retry_after_seconds=60)
        clock["t"] += 10
        c.register(retry_after_seconds=60)
        assert c.snapshot()["strikes"] == 2


class TestExponentialBackoff:
    def test_strikes_progress_zero_to_six_with_clamp(self):
        """Simula 8 hits consecutivos dentro do cooldown ativo e confere:
        - strikes progridem 0,1,2,3,4,5,6,6 (clamp em `max_strikes`);
        - adjusted_cooldown = min(1800, 60 * 2**strikes);
        - após strikes >=5, o teto 1800s passa a dominar.
        """
        c, clock = _cooldown()
        base = 60
        expected_strikes_sequence = [0, 1, 2, 3, 4, 5, 6, 6]
        expected_adjusted = [
            min(1800, base * (2 ** s)) for s in expected_strikes_sequence
        ]
        # Valores esperados explícitos para documentar o clamp:
        assert expected_adjusted == [60, 120, 240, 480, 960, 1800, 1800, 1800]

        got_adjusted = []
        got_strikes = []
        for _ in range(8):
            adjusted = c.register(retry_after_seconds=base)
            got_adjusted.append(adjusted)
            got_strikes.append(c.snapshot()["strikes"])
            # Avança pouco para ficar sempre dentro do cooldown vigente.
            clock["t"] += 1

        assert got_strikes == expected_strikes_sequence
        assert got_adjusted == expected_adjusted

    def test_max_cooldown_clamp_respected_even_with_huge_retry_after(self):
        c, _ = _cooldown()
        adjusted = c.register(retry_after_seconds=10_000)
        # Mesmo sem strikes, o teto `max_cooldown_sec` limita o cooldown.
        assert adjusted == 1800


class TestConstructorGuards:
    def test_negative_or_zero_values_clamped(self):
        c = ChatRateLimitCooldown(
            default_cooldown_sec=0,
            max_cooldown_sec=-5,
            max_strikes=-1,
        )
        assert c.default_cooldown_sec >= 1
        assert c.max_cooldown_sec >= c.default_cooldown_sec
        assert c.max_strikes >= 0

    def test_retry_after_zero_falls_back_to_default(self):
        c, _ = _cooldown()
        # `0` é falsy → cai no default_cooldown_sec.
        adjusted = c.register(retry_after_seconds=0)
        assert adjusted == 240

    def test_retry_after_none_falls_back_to_default(self):
        c, _ = _cooldown()
        adjusted = c.register(retry_after_seconds=None)
        assert adjusted == 240
