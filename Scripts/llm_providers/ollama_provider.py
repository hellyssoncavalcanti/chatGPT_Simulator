import json
import os
import threading

import requests

from .base import LLMProvider

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2"


class OllamaProvider(LLMProvider):
    """Calls a local Ollama instance instead of the browser.

    Environment variables:
      OLLAMA_BASE_URL  — default: http://localhost:11434
      OLLAMA_MODEL     — default: llama3.2

    Set ``SIMULATOR_LLM_PROVIDER=ollama`` to activate.
    """

    def dispatch_task(self, task: dict) -> None:
        threading.Thread(target=self._run, args=(task,), daemon=True).start()

    def _run(self, task: dict) -> None:
        q = task["stream_queue"]
        message = task.get("message", "")
        base_url = os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        model = os.environ.get("OLLAMA_MODEL", _DEFAULT_MODEL)
        try:
            q.put(json.dumps({"type": "status", "content": f"OllamaProvider: enviando para {model}..."}, ensure_ascii=False))
            resp = requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": message}],
                    "stream": True,
                },
                stream=True,
                timeout=300,
            )
            resp.raise_for_status()

            full_content = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    full_content += delta
                    q.put(json.dumps({"type": "markdown", "content": full_content}, ensure_ascii=False))
                if chunk.get("done"):
                    break

            title = (message[:40] + "...") if len(message) > 40 else message
            q.put(json.dumps({
                "type": "finish",
                "content": {
                    "title": title,
                    "url": task.get("url") or "",
                    "chromium_profile": task.get("effective_browser_profile") or "",
                },
            }, ensure_ascii=False))
        except Exception as exc:
            q.put(json.dumps({"type": "error", "content": f"OllamaProvider erro: {exc}"}, ensure_ascii=False))
        finally:
            q.put(None)

    def provider_name(self) -> str:
        return "ollama"
