"""Pure helpers to classify the origin of a `/v1/chat/completions` request.

Kept free of Flask/HTTP imports so the rate-limit gating logic that depends
on them (`server._wait_python_request_interval_if_needed`, analyzer priority,
Python FIFO queue) can be exercised by offline tests without loading the
full server module.
"""

from __future__ import annotations


def is_python_chat_request(source_hint_norm: str) -> bool:
    """Whether the request was issued by a local Python script.

    Matches any `request_source` that ends in `.py`, contains a `.py/`
    suffix (e.g. `script.py/worker-3`), or uses the explicit
    `python:<label>` convention.
    """
    src = (source_hint_norm or "").strip().lower()
    return src.endswith(".py") or ".py/" in src or src.startswith("python:")


def is_codex_chat_request(source_hint_norm: str, url: str, origin_url: str) -> bool:
    """Whether the request targets ChatGPT Codex Cloud.

    Codex traffic is exempt from the Python anti-rate-limit interval because
    it does not share the ChatGPT Plus quota of regular chat profiles.
    """
    hay = " ".join([
        str(source_hint_norm or "").lower(),
        str(url or "").lower(),
        str(origin_url or "").lower(),
    ])
    return ("codex" in hay) or ("/codex/cloud" in hay) or ("/codex/" in hay)
