"""Seletores críticos centralizados para o browser.py.

Este arquivo deve ser atualizado sempre que a UI do ChatGPT mudar.
LAST_VALIDATED_* documenta a última verificação manual/smoke.
"""
from __future__ import annotations

LAST_VALIDATED_DATE = "2026-04-21"
LAST_VALIDATED_COMMIT = "manual-local"

CRITICAL_SELECTORS = {
    "chat_input": [
        "#prompt-textarea",
        "textarea[placeholder*='Message']",
        "div[contenteditable='true'][data-testid='composer']",
    ],
    "send_button": [
        "button[data-testid='send-button']",
        "button[data-testid='composer-submit-button']",
        "button#composer-submit-button",
        "button[aria-label*='Enviar prompt']",
        "button[aria-label*='Send']",
        "button.composer-submit-btn",
        "button.composer-submit-button-color",
        "form button[class*='composer-submit']",
    ],
    "menu_button": [
        "button[data-testid='conversation-menu-button']",
        "button[aria-label*='More actions']",
    ],
    "sync_anchor": [
        "main",
        "div[data-message-author-role]",
    ],
    "download_link": [
        "a[href*='/files/']",
        "a[download]",
    ],
}


def selector_group(name: str) -> list[str]:
    return list(CRITICAL_SELECTORS.get(name, []))
