import random

import pytest

import humanizer


def test_profile_from_config_uses_defaults_when_missing():
    class DummyCfg:
        pass

    p = humanizer.HumanTypingProfile.from_config(DummyCfg)
    assert p.base_delay_min > 0
    assert p.base_delay_max >= p.base_delay_min
    assert 0.0 <= p.typo_chance <= 0.2


def test_delay_for_char_punctuation_is_not_base_floor():
    p = humanizer.HumanTypingProfile(
        base_delay_min=0.01,
        base_delay_max=0.02,
        punctuation_pause_min=0.10,
        punctuation_pause_max=0.11,
    )
    value = humanizer.delay_for_char(".", p)
    assert 0.10 <= value <= 0.11


def test_maybe_typo_respects_non_alpha_and_probability_bounds():
    p_never = humanizer.HumanTypingProfile(typo_chance=0.0)
    assert humanizer.maybe_typo("a", p_never) == ""
    assert humanizer.maybe_typo("1", p_never) == ""

    p_always = humanizer.HumanTypingProfile(typo_chance=1.0)
    wrong = humanizer.maybe_typo("a", p_always)
    assert wrong
    assert wrong != "a"


# ─────────────────────────────────────────────────────────────
# Invariantes observáveis (Lote P0 passo 4):
# blindagem contra "assinatura robótica" na simulação humana.
# ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=False)
def seeded_rng():
    """Semente fixa para invariantes determinísticos por teste."""
    random.seed(1234)
    yield
    random.seed()


class TestDelayVariance:
    """Nunca cair em padrão constante (risco de detecção de bot)."""

    def test_many_base_chars_produce_variation(self, seeded_rng):
        p = humanizer.HumanTypingProfile(base_delay_min=0.01, base_delay_max=0.05)
        samples = [humanizer.delay_for_char("a", p) for _ in range(200)]
        unique3 = {round(s, 3) for s in samples}
        # Exigir pelo menos 5 valores distintos com 3 casas decimais em 200
        # amostras é um piso conservador contra "assinatura robótica".
        assert len(unique3) >= 5

    def test_no_consecutive_identical_delays_p95(self, seeded_rng):
        p = humanizer.HumanTypingProfile(base_delay_min=0.01, base_delay_max=0.05)
        samples = [humanizer.delay_for_char("a", p) for _ in range(400)]
        consecutive_identical = sum(
            1 for i in range(1, len(samples))
            if round(samples[i], 3) == round(samples[i - 1], 3)
        )
        # ≤5% de repetição imediata é aceitável; acima disso indica faixa
        # degenerada (min≈max) ou RNG determinístico não aleatorizado.
        assert consecutive_identical / (len(samples) - 1) <= 0.05

    def test_punctuation_delay_gte_base_floor(self, seeded_rng):
        p = humanizer.HumanTypingProfile(
            base_delay_min=0.01, base_delay_max=0.02,
            punctuation_pause_min=0.08, punctuation_pause_max=0.24,
        )
        punct_samples = [humanizer.delay_for_char(",", p) for _ in range(100)]
        assert min(punct_samples) >= p.base_delay_max

    def test_newline_window_respected(self, seeded_rng):
        p = humanizer.HumanTypingProfile(
            newline_pause_min=0.05, newline_pause_max=0.07,
        )
        samples = [humanizer.delay_for_char("\n", p) for _ in range(50)]
        assert all(0.05 <= s <= 0.07 for s in samples)


class TestDeterminismWithSeed:
    """Mesma semente → mesma sequência (útil para replay/debug)."""

    def test_delay_sequence_is_reproducible(self):
        p = humanizer.HumanTypingProfile(base_delay_min=0.01, base_delay_max=0.05)

        random.seed(42)
        seq_a = [humanizer.delay_for_char("a", p) for _ in range(20)]

        random.seed(42)
        seq_b = [humanizer.delay_for_char("a", p) for _ in range(20)]

        assert seq_a == seq_b

    def test_typo_sequence_is_reproducible(self):
        p = humanizer.HumanTypingProfile(typo_chance=0.5)

        random.seed(7)
        seq_a = [humanizer.maybe_typo("a", p) for _ in range(30)]

        random.seed(7)
        seq_b = [humanizer.maybe_typo("a", p) for _ in range(30)]

        assert seq_a == seq_b


class TestNormalizedProfile:
    def test_swapped_min_max_is_normalized(self):
        p = humanizer.HumanTypingProfile(
            base_delay_min=0.05, base_delay_max=0.01,
        )
        n = p.normalized()
        assert n.base_delay_max >= n.base_delay_min

    def test_normalized_does_not_lower_minimum(self):
        p = humanizer.HumanTypingProfile(
            base_delay_min=0.07, base_delay_max=0.03,
        )
        n = p.normalized()
        assert n.base_delay_min == 0.07  # piso preservado

    def test_delay_after_normalization_in_range(self, seeded_rng):
        p = humanizer.HumanTypingProfile(
            base_delay_min=0.05, base_delay_max=0.01,
        )
        val = humanizer.delay_for_char("a", p)
        # após normalize implícito em delay_for_char: [0.05, 0.05]
        assert abs(val - 0.05) < 1e-9


class TestTypoGeneratesNearbyKeys:
    def test_typo_always_from_default_nearby_map(self):
        p = humanizer.HumanTypingProfile(typo_chance=1.0)
        allowed = set(humanizer.DEFAULT_NEARBY_KEYS["a"])
        random.seed(3)
        for _ in range(50):
            w = humanizer.maybe_typo("a", p)
            assert w in allowed

    def test_typo_preserves_case(self):
        p = humanizer.HumanTypingProfile(typo_chance=1.0)
        random.seed(9)
        w = humanizer.maybe_typo("A", p)
        assert w.isupper()

    def test_typo_empty_for_char_without_mapping(self):
        p = humanizer.HumanTypingProfile(typo_chance=1.0)
        # ç não está no mapa default → sem sugestão de typo.
        assert humanizer.maybe_typo("ç", p) == ""


class TestHesitationProbability:
    def test_zero_chance_never_hesitates(self):
        p = humanizer.HumanTypingProfile(hesitation_chance=0.0)
        assert all(not humanizer.should_hesitate(p) for _ in range(200))

    def test_one_chance_always_hesitates(self):
        p = humanizer.HumanTypingProfile(hesitation_chance=1.0)
        assert all(humanizer.should_hesitate(p) for _ in range(200))

    def test_hesitation_delay_in_window(self, seeded_rng):
        p = humanizer.HumanTypingProfile(
            hesitation_pause_min=0.20, hesitation_pause_max=0.35,
        )
        samples = [humanizer.hesitation_delay(p) for _ in range(50)]
        assert all(0.20 <= s <= 0.35 for s in samples)
