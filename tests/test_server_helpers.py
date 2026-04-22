import json
from collections import deque

import pytest

import server_helpers as sh


class TestFormatWaitSeconds:
    def test_zero_returns_00_00(self):
        assert sh.format_wait_seconds(0) == "00:00"

    def test_under_minute(self):
        assert sh.format_wait_seconds(45) == "00:45"

    def test_exact_minute_boundary(self):
        assert sh.format_wait_seconds(60) == "01:00"

    def test_multi_minute(self):
        assert sh.format_wait_seconds(125) == "02:05"

    def test_negative_clamps_to_zero(self):
        assert sh.format_wait_seconds(-10) == "00:00"

    def test_float_is_rounded(self):
        # 2.6 arredonda para 3s → "00:03"
        assert sh.format_wait_seconds(2.6) == "00:03"
        assert sh.format_wait_seconds(2.4) == "00:02"

    def test_invalid_input_returns_zero(self):
        assert sh.format_wait_seconds(None) == "00:00"
        assert sh.format_wait_seconds("abc") == "00:00"

    def test_large_values(self):
        # 3725s = 62m05s → "62:05" (sem rolling para horas; o cliente mostra minutos).
        assert sh.format_wait_seconds(3725) == "62:05"


class TestQueueStatusPayload:
    def _loads(self, s):
        return json.loads(s)

    def test_basic_shape(self):
        out = self._loads(sh.queue_status_payload(30, 2, 5, "analisador"))
        assert out["type"] == "status"
        assert out["phase"] == "server_python_queue_wait"
        assert out["queue_position"] == 2
        assert out["queue_size"] == 5
        assert out["sender"] == "analisador"

    def test_content_includes_formatted_wait_and_position(self):
        out = self._loads(sh.queue_status_payload(125, 2, 5, "analisador"))
        assert "02:05" in out["content"]
        assert "2/5" in out["content"]

    def test_wait_seconds_rounded_to_one_decimal(self):
        out = self._loads(sh.queue_status_payload(12.345, 1, 3, "x"))
        assert out["wait_seconds"] == 12.3

    def test_negative_wait_clamped_to_zero(self):
        out = self._loads(sh.queue_status_payload(-5, 1, 1, "x"))
        assert out["wait_seconds"] == 0.0
        assert "00:00" in out["content"]

    def test_invalid_wait_becomes_zero(self):
        out = self._loads(sh.queue_status_payload("NaN", 1, 2, "x"))
        assert out["wait_seconds"] == 0.0

    def test_total_zero_is_displayed_as_at_least_one(self):
        # max(1, total) impede divisão por zero no texto; queue_size original permanece 0.
        out = self._loads(sh.queue_status_payload(10, 0, 0, "x"))
        assert out["queue_size"] == 0
        assert "0/1" in out["content"]

    def test_utf8_preserved(self):
        # ensure_ascii=False; o emoji ⏳ precisa sobreviver sem escape.
        s = sh.queue_status_payload(0, 1, 1, "x")
        assert "⏳" in s

    def test_json_is_valid(self):
        s = sh.queue_status_payload(10, 3, 7, "analisador")
        assert isinstance(json.loads(s), dict)


class TestPruneOldAttempts:
    def test_removes_entries_outside_window(self):
        dq = deque([100.0, 105.0, 110.0])
        removed = sh.prune_old_attempts(dq, window_sec=10, now=120.0)
        # window = [110, 120]; 100 e 105 saem.
        assert removed == 2
        assert list(dq) == [110.0]

    def test_keeps_all_when_inside_window(self):
        dq = deque([115.0, 116.0, 118.0])
        removed = sh.prune_old_attempts(dq, window_sec=10, now=120.0)
        assert removed == 0
        assert list(dq) == [115.0, 116.0, 118.0]

    def test_empty_deque_is_noop(self):
        dq: deque[float] = deque()
        assert sh.prune_old_attempts(dq, window_sec=10, now=100.0) == 0
        assert list(dq) == []

    def test_removes_all_when_all_are_stale(self):
        dq = deque([1.0, 2.0, 3.0])
        removed = sh.prune_old_attempts(dq, window_sec=5, now=1000.0)
        assert removed == 3
        assert list(dq) == []

    def test_now_func_hook_is_used_when_now_absent(self):
        dq = deque([90.0, 100.0])
        sh.prune_old_attempts(dq, window_sec=5, now_func=lambda: 110.0)
        # cutoff = 105; ambos saem.
        assert list(dq) == []

    def test_accepts_list_as_fallback(self):
        # Aceitar lista facilita testes; prod usa deque.
        lst = [0.0, 1.0, 100.0]
        removed = sh.prune_old_attempts(lst, window_sec=10, now=105.0)
        assert removed == 2
        assert lst == [100.0]


class TestCountActiveChatgptProfiles:
    def test_none_returns_one(self):
        assert sh.count_active_chatgpt_profiles(None) == 1

    def test_empty_dict_returns_one(self):
        assert sh.count_active_chatgpt_profiles({}) == 1

    def test_single_profile(self):
        assert sh.count_active_chatgpt_profiles({"default": "/some/path"}) == 1

    def test_two_profiles(self):
        assert sh.count_active_chatgpt_profiles({
            "default": "/a", "segunda_chance": "/b"
        }) == 2

    def test_many_profiles(self):
        m = {f"p{i}": f"/path{i}" for i in range(5)}
        assert sh.count_active_chatgpt_profiles(m) == 5

    def test_non_mapping_without_len_returns_one(self):
        class NoLen:
            pass
        # Nosso contrato: se o argumento não tem len(), voltar ao default seguro.
        assert sh.count_active_chatgpt_profiles(NoLen()) == 1

    def test_list_is_treated_like_mapping_length(self):
        # O código histórico só chamava len(); aceitamos qualquer sized.
        assert sh.count_active_chatgpt_profiles(["default", "segunda_chance"]) == 2
