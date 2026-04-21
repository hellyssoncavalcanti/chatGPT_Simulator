"""
Helpers para simular digitação humana de forma configurável e testável.

Este módulo é puro (sem dependências de Playwright), para facilitar testes
unitários e evolução do comportamento sem arriscar regressões no browser loop.
"""

from __future__ import annotations

from dataclasses import dataclass
import random


DEFAULT_NEARBY_KEYS = {
    "a": "sqwz",
    "b": "vghn",
    "c": "xdfv",
    "d": "serfcx",
    "e": "wsdr",
    "f": "drtgvc",
    "g": "ftyhbv",
    "h": "gyujnb",
    "i": "ujko",
    "j": "huikmn",
    "k": "jiolm",
    "l": "kop",
    "m": "njk",
    "n": "bhjm",
    "o": "iklp",
    "p": "ol",
    "q": "wa",
    "r": "edft",
    "s": "awedxz",
    "t": "rfgy",
    "u": "yhji",
    "v": "cfgb",
    "w": "qase",
    "x": "zsdc",
    "y": "tghu",
    "z": "asx",
}


@dataclass(frozen=True)
class HumanTypingProfile:
    """Parâmetros de simulação para typing realista."""

    base_delay_min: float = 0.01
    base_delay_max: float = 0.08
    punctuation_pause_min: float = 0.08
    punctuation_pause_max: float = 0.24
    newline_pause_min: float = 0.02
    newline_pause_max: float = 0.07
    typo_chance: float = 0.012
    typo_max_backspaces: int = 1
    hesitation_chance: float = 0.035
    hesitation_pause_min: float = 0.12
    hesitation_pause_max: float = 0.45

    @staticmethod
    def from_config(config_module) -> "HumanTypingProfile":
        def _f(name: str, default: float) -> float:
            try:
                value = float(getattr(config_module, name))
                return value
            except Exception:
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(getattr(config_module, name))
            except Exception:
                return default

        return HumanTypingProfile(
            base_delay_min=max(0.001, _f("HUMAN_TYPING_BASE_DELAY_MIN", 0.01)),
            base_delay_max=max(0.002, _f("HUMAN_TYPING_BASE_DELAY_MAX", 0.08)),
            punctuation_pause_min=max(0.005, _f("HUMAN_TYPING_PUNCT_PAUSE_MIN", 0.08)),
            punctuation_pause_max=max(0.01, _f("HUMAN_TYPING_PUNCT_PAUSE_MAX", 0.24)),
            newline_pause_min=max(0.005, _f("HUMAN_TYPING_NEWLINE_PAUSE_MIN", 0.02)),
            newline_pause_max=max(0.01, _f("HUMAN_TYPING_NEWLINE_PAUSE_MAX", 0.07)),
            typo_chance=min(0.2, max(0.0, _f("HUMAN_TYPING_TYPO_CHANCE", 0.012))),
            typo_max_backspaces=max(1, _i("HUMAN_TYPING_TYPO_MAX_BACKSPACES", 1)),
            hesitation_chance=min(0.4, max(0.0, _f("HUMAN_TYPING_HESITATION_CHANCE", 0.035))),
            hesitation_pause_min=max(0.01, _f("HUMAN_TYPING_HESITATION_PAUSE_MIN", 0.12)),
            hesitation_pause_max=max(0.02, _f("HUMAN_TYPING_HESITATION_PAUSE_MAX", 0.45)),
        )

    def normalized(self) -> "HumanTypingProfile":
        if self.base_delay_max < self.base_delay_min:
            return self.__class__(**{**self.__dict__, "base_delay_max": self.base_delay_min})
        return self


def delay_for_char(char: str, profile: HumanTypingProfile) -> float:
    p = profile.normalized()
    if char == "\n":
        return random.uniform(p.newline_pause_min, max(p.newline_pause_max, p.newline_pause_min))
    if char in {".", ",", ";", ":", "!", "?"}:
        return random.uniform(p.punctuation_pause_min, max(p.punctuation_pause_max, p.punctuation_pause_min))
    return random.uniform(p.base_delay_min, max(p.base_delay_max, p.base_delay_min))


def should_hesitate(profile: HumanTypingProfile) -> bool:
    return random.random() < max(0.0, min(1.0, profile.hesitation_chance))


def hesitation_delay(profile: HumanTypingProfile) -> float:
    return random.uniform(
        profile.hesitation_pause_min,
        max(profile.hesitation_pause_max, profile.hesitation_pause_min),
    )


def maybe_typo(char: str, profile: HumanTypingProfile) -> str:
    if not char or not char.isalpha():
        return ""
    if random.random() >= max(0.0, min(1.0, profile.typo_chance)):
        return ""
    opts = DEFAULT_NEARBY_KEYS.get(char.lower(), "")
    if not opts:
        return ""
    wrong = random.choice(opts)
    return wrong.upper() if char.isupper() else wrong
