from security_state import SecurityState


def _state(now=1000.0, **kw):
    """Cria uma SecurityState com `now_func` determinístico (clock mutável)."""
    clock = {"t": now}
    kw.setdefault("rate_limit_window_sec", 60)
    kw.setdefault("rate_limit_per_min", 3)
    kw.setdefault("login_max_fails", 3)
    kw.setdefault("login_block_sec", 120)
    s = SecurityState(now_func=lambda: clock["t"], **kw)
    return s, clock


class TestRateLimit:
    def test_under_cap_returns_false(self):
        s, _ = _state()
        for _ in range(3):
            hit, _ = s.register_rate_limit_hit("1.2.3.4", "GET:/foo")
            assert hit is False

    def test_exceeding_cap_returns_true_with_retry(self):
        s, _ = _state()
        for _ in range(3):
            s.register_rate_limit_hit("1.2.3.4", "GET:/foo")
        hit, retry = s.register_rate_limit_hit("1.2.3.4", "GET:/foo")
        assert hit is True
        assert retry > 0

    def test_separate_keys_do_not_share_bucket(self):
        s, _ = _state()
        for _ in range(3):
            s.register_rate_limit_hit("1.2.3.4", "GET:/a")
        hit, _ = s.register_rate_limit_hit("1.2.3.4", "GET:/b")
        assert hit is False

    def test_old_hits_expire_with_window(self):
        s, clock = _state()
        for _ in range(3):
            s.register_rate_limit_hit("1.2.3.4", "GET:/foo")
        clock["t"] += 61  # passou da janela
        hit, _ = s.register_rate_limit_hit("1.2.3.4", "GET:/foo")
        assert hit is False


class TestLoginBruteforce:
    def test_under_threshold_not_blocked(self):
        s, _ = _state()
        assert s.register_login_failure("ip1") is False
        assert s.register_login_failure("ip1") is False
        blocked, _, _ = s.is_ip_blocked("ip1")
        assert blocked is False

    def test_hitting_threshold_blocks(self):
        s, _ = _state(login_max_fails=3)
        s.register_login_failure("ip1")
        s.register_login_failure("ip1")
        just_blocked = s.register_login_failure("ip1")
        assert just_blocked is True
        blocked, remaining, reason = s.is_ip_blocked("ip1")
        assert blocked is True
        assert remaining > 0
        assert reason == "bruteforce_login"

    def test_clear_resets_failure_count(self):
        s, _ = _state()
        s.register_login_failure("ip1")
        s.register_login_failure("ip1")
        s.clear_login_failures("ip1")
        # Após clear, a 1ª próxima falha não bloqueia.
        just_blocked = s.register_login_failure("ip1")
        assert just_blocked is False

    def test_block_expires_automatically(self):
        s, clock = _state(login_max_fails=2, login_block_sec=10)
        s.register_login_failure("ip1")
        s.register_login_failure("ip1")
        assert s.is_ip_blocked("ip1")[0] is True
        clock["t"] += 11
        blocked, remaining, _ = s.is_ip_blocked("ip1")
        assert blocked is False
        assert remaining == 0.0


class TestSnapshotAndConstructorGuards:
    def test_snapshot_reports_counts(self):
        s, _ = _state()
        s.register_rate_limit_hit("1.2.3.4", "a")
        s.register_login_failure("ip1")
        snap = s.snapshot()
        assert snap["rate_limit_keys"] == 1
        assert snap["tracked_login_ips"] == 1
        assert snap["blocked_ips"] == 0

    def test_negative_or_zero_thresholds_clamped(self):
        s = SecurityState(
            rate_limit_window_sec=0,
            rate_limit_per_min=-5,
            login_max_fails=0,
            login_block_sec=-1,
        )
        assert s.rate_limit_window_sec >= 1
        assert s.rate_limit_per_min >= 1
        assert s.login_max_fails >= 1
        assert s.login_block_sec >= 1

    def test_is_ip_blocked_unknown_ip(self):
        s, _ = _state()
        assert s.is_ip_blocked("nunca_viu") == (False, 0.0, "")
