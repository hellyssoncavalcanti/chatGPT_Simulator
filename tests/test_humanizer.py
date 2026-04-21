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
