from .base import LLMProvider


class PlaywrightProvider(LLMProvider):
    """Delegates to the existing browser_queue → browser.py pipeline.

    This is the production default: behaviour is identical to what server.py
    did before the provider abstraction was introduced.
    """

    def dispatch_task(self, task: dict) -> None:
        # Import here so the module can be loaded without Playwright installed
        # (e.g. during unit tests that choose a different provider).
        from shared import browser_queue  # noqa: PLC0415
        browser_queue.put(task)

    def provider_name(self) -> str:
        return "playwright"
