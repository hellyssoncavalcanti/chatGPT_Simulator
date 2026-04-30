import os

from .base import LLMProvider


def get_provider() -> LLMProvider:
    """Return the active LLMProvider based on ``SIMULATOR_LLM_PROVIDER``.

    Default is ``playwright`` (production).  Set the env-var to ``mock``
    for tests or ``ollama`` for a local model.
    """
    name = os.environ.get("SIMULATOR_LLM_PROVIDER", "playwright").lower().strip()

    if name == "mock":
        from .mock_provider import MockProvider  # noqa: PLC0415
        return MockProvider()

    if name == "ollama":
        from .ollama_provider import OllamaProvider  # noqa: PLC0415
        return OllamaProvider()

    if name != "playwright":
        import warnings
        warnings.warn(
            f"SIMULATOR_LLM_PROVIDER={name!r} não reconhecido — usando 'playwright'.",
            stacklevel=2,
        )

    from .playwright_provider import PlaywrightProvider  # noqa: PLC0415
    return PlaywrightProvider()
