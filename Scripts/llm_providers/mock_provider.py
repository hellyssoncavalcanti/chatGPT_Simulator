import json
import threading

from .base import LLMProvider


class MockProvider(LLMProvider):
    """Zero-dependency stub for unit / CI tests.

    Emits the minimum event sequence expected by server.py:
      status → markdown → finish → None sentinel.

    Set ``SIMULATOR_LLM_PROVIDER=mock`` to activate.
    """

    def dispatch_task(self, task: dict) -> None:
        # Run in a thread so callers that launch _dispatch_chat_task in a
        # thread (stream mode) and callers that run it inline (block mode)
        # both work correctly.
        threading.Thread(target=self._respond, args=(task,), daemon=True).start()

    def _respond(self, task: dict) -> None:
        q = task["stream_queue"]
        try:
            q.put(json.dumps({"type": "status", "content": "MockProvider: processando..."}, ensure_ascii=False))
            q.put(json.dumps({"type": "markdown", "content": "MOCK"}, ensure_ascii=False))
            q.put(json.dumps({
                "type": "finish",
                "content": {
                    "title": "Mock Chat",
                    "url": task.get("url") or "",
                    "chromium_profile": task.get("effective_browser_profile") or "",
                },
            }, ensure_ascii=False))
        finally:
            q.put(None)

    def provider_name(self) -> str:
        return "mock"
