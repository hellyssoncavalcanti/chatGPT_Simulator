# =============================================================================
# browser.py — Controlador Playwright do ChatGPT Simulator
# =============================================================================
#
# RESPONSABILIDADE:
#   Gerencia o navegador Chromium via Playwright (assíncrono). Consome tarefas
#   da browser_queue despachadas pelo server.py e executa as ações correspondentes
#   no navegador: enviar mensagens ao ChatGPT, ler respostas, sincronizar
#   histórico, gerenciar menus, realizar pesquisas no Google e controlar abas.
#
# RELAÇÕES:
#   • Importa: config, shared (browser_queue), utils
#   • Consome tarefas de: server.py (via browser_queue.put)
#   • Produz resultados em: stream_queue por tarefa (lida pelo server.py)
#
# AÇÕES SUPORTADAS (campo "action" na tarefa):
#   CHAT      — envia mensagem e retorna resposta em streaming
#   SYNC      — scrape completo do histórico de um chat
#   GET_MENU  — lê opções do menu de contexto de um chat
#   EXEC_MENU — clica em uma opção do menu (ex: Excluir, Renomear)
#   SEARCH    — abre Google, pesquisa e retorna resultados estruturados
#   UPTODATE_SEARCH — abre UpToDate Search e retorna resultados estruturados
#   STOP      — encerra o loop principal
#
# MECANISMO DE PASTE:
#   Texto entre [INICIO_TEXTO_COLADO]...[FIM_TEXTO_COLADO] é colado via
#   clipboard (Ctrl+V) — rápido como humano. Texto fora dos marcadores
#   é digitado caractere a caractere via type_realistic().
# =============================================================================
import asyncio
import base64
import contextvars
import json
import random
import time
import re
import os
import queue
import sys
import contextvars
import requests
from playwright.async_api import async_playwright
import config
from shared import browser_queue, register_file
from utils import log as file_log
from markdownify import markdownify as md

# ─────────────────────────────────────────────────────────────
# CAPTURA CONFIGURAÇÃO DE DEBUG (que é estabelecida no arquivo "config.py").
# ─────────────────────────────────────────────────────────────
# Verifica se config já foi importado; se não, importa
if 'config' not in sys.modules:
    import config

# Tenta importar DEBUG_LOG do módulo config já carregado
try:
    DEBUG_LOG = config.DEBUG_LOG
except AttributeError:
    DEBUG_LOG = False  # fallback se a variável não existir no config
    print("⚠️ DEBUG_LOG não encontrado no config.py. Usando False como padrão.")

# Semáforo para limitar número de abas simultâneas (evita travar o PC)
MAX_TABS = 5
tab_semaphore = asyncio.Semaphore(MAX_TABS)

SCREENSHOT_STREAM_INTERVAL_SEC = 2.0
SCREENSHOT_STREAM_JPEG_QUALITY = 45
SCREENSHOT_STREAM_MAX_BYTES = 300_000
SCREENSHOT_STREAM_LOG_MIN_DELTA_KB = 3.0
SCREENSHOT_STREAM_LOG_MAX_SILENCE_SEC = 60.0
_SCREENSHOT_INLINE_LAST_LEN = 0
_SCREENSHOT_INLINE_LAST_MSG = ""
_SCREENSHOT_LOG_STATE = {}
_CURRENT_TASK_SENDER = contextvars.ContextVar("current_task_sender", default="usuario_remoto")

def emit_log(q, msg):
    sender = _CURRENT_TASK_SENDER.get()
    prefix = f"[browser.py] [{sender}] "
    if q:
        q.put(json.dumps({"type": "log", "content": f"{prefix}{msg}"}) + "\n")
    file_log("browser.py", f"[{sender}] {msg}")


class RateLimitDetected(Exception):
    """Sinaliza que o ChatGPT exibiu banner/toast de rate-limit ou equivalente.

    Permite que o caminho de setup do chat (ex.: aguardar textarea carregar)
    distinga um timeout genérico de um rate-limit real, para que o agente
    receba um evento 'rate_limit' em vez de uma falha de 'Timeout ...
    waiting for locator("#prompt-textarea")'.
    """

    def __init__(self, message: str = "", retry_after_seconds: int = 240):
        super().__init__(message or "Excesso de solicitações")
        self.message = message or "Excesso de solicitações"
        self.retry_after_seconds = int(retry_after_seconds or 240)

def emit_event(q, type_, content):
    if q:
        # Cria o dicionário e garante que o dumps mantenha tudo em uma linha
        # O \n final é estritamente o separador do stream
        payload = json.dumps({"type": type_, "content": content}, separators=(',', ':'))
        q.put(payload + "\n")


def _extract_task_sender(task: dict | None) -> str:
    """Resolve sender label attached to the queued task."""
    if not isinstance(task, dict):
        return "usuario_remoto"
    sender = (
        task.get("sender")
        or task.get("request_source")
        or task.get("remetente")
        or ""
    )
    sender = str(sender or "").strip()
    return sender or "usuario_remoto"

async def close_ephemeral_pages(context, baseline_pages, q=None, keep_pages=None):
    """
    Fecha abas criadas durante uma tarefa (popups/abas órfãs), preservando
    apenas as abas de baseline e as explicitamente mantidas em keep_pages.
    """
    try:
        baseline_ids = {id(p) for p in (baseline_pages or [])}
        keep_ids = {id(p) for p in (keep_pages or []) if p is not None}
        for p in list(getattr(context, "pages", []) or []):
            if id(p) in baseline_ids or id(p) in keep_ids:
                continue
            try:
                await p.close()
            except Exception as e:
                emit_log(q, f"⚠️ Falha ao fechar aba efêmera: {e}")
    except Exception as e:
        emit_log(q, f"⚠️ Limpeza de abas efêmeras falhou: {e}")


def _is_known_orphan_tab_url(url: str) -> bool:
    if not url:
        return False
    u = url.strip().lower()
    if "residenciapediatrica.com.br/content/pdf/" in u:
        return True
    return False


async def cleanup_known_orphan_tabs(context, q=None):
    """
    Remove abas persistentes/restauradas que não fazem parte do fluxo do worker
    (ex.: PDF externo que reaparece após restauração de sessão do Chromium).
    """
    try:
        for p in list(getattr(context, "pages", []) or []):
            url = ""
            try:
                url = (p.url or "").strip()
            except Exception:
                url = ""
            if _is_known_orphan_tab_url(url):
                emit_log(q, f"🧹 Fechando aba órfã conhecida: {url[:120]}")
                try:
                    await p.close()
                except Exception as close_err:
                    emit_log(q, f"⚠️ Falha ao fechar aba órfã conhecida: {close_err}")
    except Exception as e:
        emit_log(q, f"⚠️ Falha na limpeza de abas órfãs conhecidas: {e}")

async def _get_window_state(page):
    try:
        session = await page.context.new_cdp_session(page)
        info = await session.send("Browser.getWindowForTarget")
        bounds = info.get("bounds", {}) or {}
        state = bounds.get("windowState") or bounds.get("state") or "normal"
        return session, info.get("windowId"), state
    except Exception:
        return None, None, None


async def _set_window_state(page, state: str) -> bool:
    session, window_id, current_state = await _get_window_state(page)
    if not session or not window_id:
        return False
    if current_state == state:
        return True
    try:
        await session.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": state},
        })
        return True
    except Exception:
        return False


async def _preserve_minimized_if_needed(page, keep_minimized: bool | None = None):
    _session, _window_id, state = await _get_window_state(page)
    if keep_minimized is None:
        keep_minimized = (state == "minimized")
    if keep_minimized and state != "minimized":
        await _set_window_state(page, "minimized")
        return "minimized"
    return state


async def _get_context_window_state(context):
    for candidate in list(getattr(context, "pages", []) or []):
        try:
            _session, _window_id, state = await _get_window_state(candidate)
            if state:
                return state
        except Exception:
            continue
    return None


async def _should_keep_context_minimized(context) -> bool:
    state = await _get_context_window_state(context)
    return state == "minimized"


async def _emit_browser_screenshot(page, q, label: str = "browser"):
    global _SCREENSHOT_LAST_LOG
    if not q:
        return
    try:
        raw = await page.screenshot(
            type="jpeg",
            quality=SCREENSHOT_STREAM_JPEG_QUALITY,
            caret="hide",
            animations="disabled",
            scale="css",
        )
        if not raw:
            return
        if len(raw) > SCREENSHOT_STREAM_MAX_BYTES:
            return
        kb = len(raw) / 1024
        now = time.time()
        state_key = f"{label}|{page.url or ''}"
        prev = _SCREENSHOT_LOG_STATE.get(state_key) or {}
        prev_kb = float(prev.get("kb", -1))
        prev_ts = float(prev.get("ts", 0))
        should_log = (
            prev_kb < 0
            or abs(kb - prev_kb) >= SCREENSHOT_STREAM_LOG_MIN_DELTA_KB
            or (now - prev_ts) >= SCREENSHOT_STREAM_LOG_MAX_SILENCE_SEC
        )
        if should_log:
            msg = f"📸 Screenshot stream [{label}]: {kb:.1f} KB — {page.url}"
            emit_log(q, msg)
            _SCREENSHOT_LOG_STATE[state_key] = {"kb": kb, "ts": now}
        emit_event(q, "screenshot", {
            "label": label,
            "format": "jpeg",
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "url": page.url,
            "captured_at": int(time.time()),
        })
    except Exception:
        return


async def _stream_browser_screenshots(page, q, stop_event: asyncio.Event, label: str = "browser"):
    if not q:
        return
    try:
        await _emit_browser_screenshot(page, q, label=label)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=SCREENSHOT_STREAM_INTERVAL_SEC)
            except asyncio.TimeoutError:
                await _emit_browser_screenshot(page, q, label=label)
    except asyncio.CancelledError:
        raise


def _composer_state_script():
    return r"""() => {
        const composerRoot = document.querySelector('form')
            || document.querySelector('[data-testid="composer"]')
            || document.querySelector('main');
        const ta = document.querySelector('#prompt-textarea');
        const sendBtn = document.querySelector('button[data-testid="send-button"]');
        const stopBtn = document.querySelector('button[aria-label="Stop generating"], button[data-testid="stop-button"]');
        const attachmentNodes = Array.from(document.querySelectorAll(
            'button[aria-label="Remove file"], [data-testid*="attachment"], [data-testid*="file-preview"], [data-testid*="composer-attachment"], [data-testid*="upload-preview"], [data-testid*="file-chip"]'
        ));

        // Detecção da nova funcionalidade do ChatGPT: "Colagens grandes agora viram anexos"
        // Quando o ChatGPT converte texto colado em anexo, cria um card/chip na área do composer.
        // Detectamos via múltiplas estratégias:
        const pasteAsAttachmentNodes = [];
        if (composerRoot) {
            // Estratégia 1: texto "Exibir no campo de texto" / "Show in text field"
            const allComposerEls = Array.from(composerRoot.querySelectorAll('button, a, [role="button"], span, div'));
            for (const el of allComposerEls) {
                const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (txt.includes('exibir no campo de texto') || txt.includes('show in text field')
                    || txt.includes('exibir no campo') || txt.includes('show in text')) {
                    pasteAsAttachmentNodes.push(el);
                }
            }
            // Estratégia 2: cards de anexo com ícone de arquivo (svg + botão fechar)
            if (pasteAsAttachmentNodes.length === 0) {
                const candidates = composerRoot.querySelectorAll('[class*="group"], [class*="attach"], [class*="block"], [class*="chip"], [class*="file"], [class*="paste"]');
                for (const c of candidates) {
                    const inner = (c.innerText || '').toLowerCase();
                    if ((inner.includes('exibir no campo') || inner.includes('show in text'))
                        && c.closest('form, [data-testid="composer"], main')) {
                        pasteAsAttachmentNodes.push(c);
                    }
                }
            }
            // Estratégia 3: qualquer novo elemento que apareceu no composer e contém SVG
            //   (ícone de documento) + botão de fechar — típico de um card de anexo
            if (pasteAsAttachmentNodes.length === 0) {
                const composerDivs = composerRoot.querySelectorAll('div');
                for (const d of composerDivs) {
                    if (d.querySelector('svg') && d.querySelector('button[aria-label]')
                        && d.offsetHeight > 20 && d.offsetHeight < 120
                        && !d.querySelector('#prompt-textarea')) {
                        const ariaLabel = (d.querySelector('button[aria-label]')?.getAttribute('aria-label') || '').toLowerCase();
                        if (ariaLabel.includes('remov') || ariaLabel.includes('delet') || ariaLabel.includes('close')
                            || ariaLabel.includes('exclu') || ariaLabel.includes('fechar')) {
                            pasteAsAttachmentNodes.push(d);
                        }
                    }
                }
            }
        }

        const allAttachmentNodes = [...attachmentNodes, ...pasteAsAttachmentNodes];
        const attachmentTitles = allAttachmentNodes
            .map((node) => (node.innerText || node.getAttribute('aria-label') || '').trim())
            .filter(Boolean)
            .slice(0, 6);
        const busyNodes = Array.from(document.querySelectorAll('[aria-busy="true"], progress, [data-testid*="uploading"], [data-testid*="spinner"], svg.animate-spin'));
        const textValue = ta ? ((ta.innerText || ta.value || '').trim()) : '';
        const textLength = textValue.length;
        const sendVisible = !!(sendBtn && sendBtn.offsetParent !== null);
        const sendEnabled = !!(sendBtn && sendVisible && !sendBtn.disabled && sendBtn.getAttribute('aria-disabled') !== 'true');
        const stopVisible = !!(stopBtn && stopBtn.offsetParent !== null);
        const textReady = textLength > 0;
        const attachmentCount = allAttachmentNodes.length;
        const hasAttachments = attachmentCount > 0;

        // Detecção adicional: se sendBtn está habilitado mas não há texto nem anexos detectados,
        // verifica se o ChatGPT aceitou conteúdo (possível anexo não detectado pelos seletores)
        const sendEnabledNoContent = sendEnabled && !textReady && !hasAttachments;

        const uploading = busyNodes.some((node) => {
            if (!node) return false;
            const txt = (node.innerText || node.getAttribute?.('aria-label') || '').toLowerCase();
            return !txt || txt.includes('upload') || txt.includes('carreg') || txt.includes('process') || txt.includes('analys');
        });
        return {
            textLength,
            textReady,
            hasAttachments: hasAttachments || sendEnabledNoContent,
            attachmentCount: hasAttachments ? attachmentCount : (sendEnabledNoContent ? 1 : 0),
            attachmentTitles,
            sendVisible,
            sendEnabled,
            stopVisible,
            uploading,
            ariaBusy: !!(ta && ta.getAttribute('aria-busy') === 'true'),
            composerVisible: !!(composerRoot && composerRoot.offsetParent !== null),
            pasteAsAttachment: pasteAsAttachmentNodes.length > 0 || sendEnabledNoContent,
        };
    }"""


async def _get_composer_state(page):
    try:
        return await page.evaluate(_composer_state_script())
    except Exception:
        return {
            'textLength': 0,
            'textReady': False,
            'hasAttachments': False,
            'attachmentCount': 0,
            'attachmentTitles': [],
            'sendVisible': False,
            'sendEnabled': False,
            'stopVisible': False,
            'uploading': False,
            'ariaBusy': False,
            'composerVisible': False,
        }


async def _wait_for_composer_ready(page, q=None, timeout: float = 20.0):
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        state = await _get_composer_state(page)
        last_state = state
        has_payload = bool(state.get('textReady') or state.get('hasAttachments'))
        if has_payload and state.get('sendEnabled') and not state.get('uploading'):
            return state
        await asyncio.sleep(0.25)

    if q and last_state:
        emit_log(
            q,
            "⚠️ Composer não ficou pronto a tempo; tentando enviar assim mesmo "
            f"(texto={last_state.get('textLength')}, anexos={last_state.get('attachmentCount')}, "
            f"sendEnabled={last_state.get('sendEnabled')}, uploading={last_state.get('uploading')})."
        )
    return last_state or {}


async def _submit_prompt(page, q=None, timeout: float = 12.0) -> bool:
    state = await _wait_for_composer_ready(page, q=q, timeout=timeout)
    if q and state.get('hasAttachments') and not state.get('textReady'):
        emit_log(q,
                 f"ChatGPT converteu a cola em {state.get('attachmentCount')} anexo(s); enviando pelo botão.")

    submit_attempts = [
        ('click', lambda: page.locator('button[data-testid="send-button"]').first.click(timeout=2000)),
        ('force_click', lambda: page.locator('button[data-testid="send-button"]').first.click(timeout=2000, force=True)),
        ('dom_click', lambda: page.evaluate("""() => {
            const btn = document.querySelector('button[data-testid=\"send-button\"]');
            if (!btn) return false;
            btn.click();
            return true;
        }""")),
        ('enter', lambda: page.keyboard.press('Enter')),
        ('mod_enter', lambda: page.keyboard.press('Control+Enter')),
    ]

    for label, submitter in submit_attempts:
        try:
            await submitter()
        except Exception as exc:
            emit_log(q, f"Tentativa de envio '{label}' falhou: {exc}")
            continue

        verify_deadline = time.time() + 4.0
        while time.time() < verify_deadline:
            current = await _get_composer_state(page)
            if current.get('stopVisible'):
                return True
            if not current.get('sendEnabled') and (current.get('ariaBusy') or current.get('uploading')):
                return True
            if (state.get('textReady') or state.get('hasAttachments')) and not current.get('textReady') and not current.get('hasAttachments'):
                return True
            await asyncio.sleep(0.2)

    return False


async def _dismiss_rate_limit_modal_if_any(page, q=None):
    """Fecha modal de rate-limit que pode interceptar cliques no composer."""
    try:
        handled = await page.evaluate("""() => {
            const modal = document.querySelector('#modal-conversation-history-rate-limit, [data-testid="modal-conversation-history-rate-limit"]');
            if (!modal) return false;
            const buttons = Array.from(modal.querySelectorAll('button'));
            const target = buttons.find((b) => {
                const txt = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                return txt.includes('entendido') || txt.includes('ok') || txt.includes('fechar') || txt.includes('close');
            });
            if (target) {
                target.click();
                return true;
            }
            return false;
        }""")
        if handled:
            emit_log(q, "ℹ️ Modal de rate-limit detectado e fechado antes da digitação.")
            await asyncio.sleep(0.2)
            return
        await page.keyboard.press("Escape")
    except Exception:
        return


def _response_looks_incomplete_json(markdown_text: str) -> bool:
    texto = (markdown_text or '').strip()
    if not texto:
        return False

    if texto.startswith('```'):
        texto = re.sub(r'^```(?:json)?\s*', '', texto, flags=re.IGNORECASE)
        texto = re.sub(r'\s*```$', '', texto)
    texto = texto.strip()
    if not texto.startswith('{'):
        return False

    depth_obj = 0
    depth_arr = 0
    in_string = False
    escape = False
    for ch in texto:
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == '{':
            depth_obj += 1
        elif ch == '}':
            depth_obj -= 1
        elif ch == '[':
            depth_arr += 1
        elif ch == ']':
            depth_arr -= 1

    return in_string or depth_obj > 0 or depth_arr > 0 or not texto.rstrip().endswith('}')


def _response_requests_followup_actions(markdown_text: str) -> bool:
    """
    Detecta respostas intermediárias que normalmente exigem rodada adicional
    (ex.: sql_queries/search_queries/json de ferramenta) antes da resposta final.
    """
    texto = (markdown_text or "").strip().lower()
    if not texto:
        return False

    if texto.startswith("```"):
        texto = re.sub(r'^```(?:json)?\s*', '', texto, flags=re.IGNORECASE)
        texto = re.sub(r'\s*```$', '', texto)
        texto = texto.strip().lower()

    hints = (
        '"sql_queries"', "'sql_queries'", "sql_queries",
        '"search_queries"', "'search_queries'", "search_queries",
        '"queries_sql"', "'queries_sql'", "queries_sql",
        '"tool_name"', '"tool_calls"', '"function_call"',
    )
    return any(h in texto for h in hints)


async def smart_input(page, message, q=None, activityts=None):
    import re

    selector = "#prompt-textarea"
    await _dismiss_rate_limit_modal_if_any(page, q=q)
    await page.wait_for_selector(selector, timeout=10000)
    try:
        await page.click(selector, timeout=5000)
    except Exception:
        await _dismiss_rate_limit_modal_if_any(page, q=q)
        await page.click(selector, timeout=5000)
    await asyncio.sleep(0.3)

    start_marker = "[INICIO_TEXTO_COLADO]"
    end_marker   = "[FIM_TEXTO_COLADO]"

    async def _paste_clipboard(text, label='Colando'):
        """Cola texto via clipboard (Ctrl+V) -- rapido como um humano.
        Usa navigator.clipboard.writeText + Ctrl+V via Playwright.
        Normaliza \r\n -> \n antes de escrever no clipboard."""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        total = len(text)
        emit_log(q, f'{label}: {total} chars via clipboard...')
        if q:
            emit_event(q, 'status', f'{label}... 0%')
        if activityts:
            activityts[0] = time.time()

        # Escreve o texto no clipboard via JS
        await page.evaluate("(t) => navigator.clipboard.writeText(t)", text)
        await asyncio.sleep(0.1)

        # Foca o textarea e simula Ctrl+V
        ta_found = await page.evaluate("""
            () => {
                const ta = document.getElementById('prompt-textarea')
                        || document.querySelector('#prompt-textarea');
                if (!ta) return false;
                ta.focus();
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(ta);
                range.collapse(false);
                sel.removeAllRanges();
                sel.addRange(range);
                return true;
            }
        """)
        if not ta_found:
            raise RuntimeError('prompt-textarea nao encontrado para colar')

        await page.keyboard.press('Control+V')
        await asyncio.sleep(0.3)

        # Verifica se colou corretamente ou se o ChatGPT converteu a cola em anexo.
        # A conversão para anexo pode demorar um instante, então faz polling com retry.
        state = await _get_composer_state(page)
        inserted = int(state.get('textLength') or 0)

        if inserted == 0:
            # Texto não apareceu no textarea — pode ser conversão em anexo.
            # Aguarda até 5s com polling para detectar o anexo criado automaticamente.
            for _retry in range(10):
                if state.get('hasAttachments') or state.get('pasteAsAttachment'):
                    break
                await asyncio.sleep(0.5)
                state = await _get_composer_state(page)
                inserted = int(state.get('textLength') or 0)
                if inserted > 0:
                    break
                # Fallback: se o sendBtn está habilitado mas não há texto,
                # o ChatGPT aceitou o conteúdo como anexo (mesmo sem detectar card)
                if state.get('sendEnabled') and not state.get('textReady'):
                    emit_log(q, f"{label}: Send habilitado sem texto — provável anexo não detectado pelos seletores.")
                    state['hasAttachments'] = True
                    break

            if inserted == 0 and (state.get('hasAttachments') or state.get('pasteAsAttachment') or state.get('sendEnabled')):
                emit_log(q, f"{label}: ChatGPT converteu a cola em anexo ({state.get('attachmentCount', '?')} item(ns)).")
                inserted = total

        if activityts:
            activityts[0] = time.time()
        if q:
            emit_event(q, 'status', f'{label}... 100%')
        return inserted

    if start_marker in message and end_marker in message:
        pattern = re.compile(
            r'(\[INICIO_TEXTO_COLADO\].*?\[FIM_TEXTO_COLADO\])',
            re.DOTALL
        )
        segments = pattern.split(message)

        for segment in segments:
            if not segment:
                continue
            is_block = segment.startswith(start_marker) and segment.endswith(end_marker)
            if is_block:
                inner = segment[len(start_marker):-len(end_marker)]
                if inner.strip():
                    emit_log(q, f'Colando bloco ({len(inner)} chars)...')
                    txt = inner.replace('\r\n', '\n').replace('\r', '\n')
                    paste_chunk_size = 3500
                    paste_chunks = [txt[i:i + paste_chunk_size] for i in range(0, len(txt), paste_chunk_size)] or ['']
                    total = 0
                    paste_became_attachment = False
                    for chunk_index, paste_chunk in enumerate(paste_chunks, start=1):
                        # Se um chunk anterior já virou anexo, o ChatGPT já tem o conteúdo.
                        # Colar mais chunks geraria anexos duplicados — pula os restantes.
                        if paste_became_attachment:
                            total += len(paste_chunk)
                            continue
                        label = 'Colando' if len(paste_chunks) == 1 else f'Colando parte {chunk_index}/{len(paste_chunks)}'
                        try:
                            # Tenta colar via clipboard (Ctrl+V) -- rápido, mas em sub-blocos para evitar anexos automáticos.
                            inserted_now = await _paste_clipboard(paste_chunk, label)
                            # Detecta se este chunk virou anexo (0 chars no textarea mas retornou total)
                            check_state = await _get_composer_state(page)
                            if int(check_state.get('textLength') or 0) == 0 and (check_state.get('hasAttachments') or check_state.get('pasteAsAttachment') or check_state.get('sendEnabled')):
                                paste_became_attachment = True
                                # Contabiliza todos os chars restantes como "colados via anexo"
                                remaining = sum(len(paste_chunks[i]) for i in range(chunk_index, len(paste_chunks)))
                                total += inserted_now + remaining
                                emit_log(q, f"ChatGPT converteu todo o bloco em anexo. Pulando {len(paste_chunks) - chunk_index} chunk(s) restante(s).")
                                continue
                        except Exception as clipboard_err:
                            # Fallback: chunks com execCommand se clipboard falhar
                            emit_log(q, f'Clipboard falhou ({clipboard_err}), usando fallback por chunks...')
                            CHUNK_SIZE = 300
                            js_inject = """(text) => {
                                const ta = document.getElementById('prompt-textarea')
                                         || document.querySelector('#prompt-textarea');
                                if (!ta) throw new Error('prompt-textarea nao encontrado');
                                if (ta.isContentEditable) {
                                    ta.focus();
                                    const sel = window.getSelection();
                                    const range = document.createRange();
                                    range.selectNodeContents(ta);
                                    range.collapse(false);
                                    sel.removeAllRanges();
                                    sel.addRange(range);
                                    document.execCommand('insertText', false, text);
                                    return ta.innerText.length;
                                }
                                const setter = Object.getOwnPropertyDescriptor(
                                    window.HTMLTextAreaElement.prototype, 'value').set;
                                setter.call(ta, (ta.value || '') + text);
                                ta.dispatchEvent(new InputEvent('input', {
                                    bubbles: true, cancelable: true, inputType: 'insertText', data: text
                                }));
                                return ta.value.length;
                            }"""
                            total_chars = len(paste_chunk)
                            inserted_now = 0
                            while inserted_now < total_chars:
                                chunk = paste_chunk[inserted_now:inserted_now + CHUNK_SIZE]
                                await page.evaluate(js_inject, chunk)
                                inserted_now += len(chunk)
                                pct = int(inserted_now / total_chars * 100)
                                if q: emit_event(q, 'status', f'Colando (fallback)... {pct}%')
                                if activityts: activityts[0] = time.time()
                                await asyncio.sleep(0.08)
                        total += inserted_now if inserted_now else len(paste_chunk)
                        await asyncio.sleep(0.15)
                    expected_len = len(txt)
                    state_after_paste = await _get_composer_state(page)
                    if total < expected_len * 0.9 and not state_after_paste.get('hasAttachments') and not state_after_paste.get('sendEnabled'):
                        emit_log(q, f'Aviso: colados {total} de ~{len(inner)} chars')
                    elif state_after_paste.get('hasAttachments') or paste_became_attachment:
                        emit_log(q,
                                 f"Bloco aceito como anexo(s): {state_after_paste.get('attachmentCount', '?')} item(ns).")
                    elif total < expected_len * 0.9 and state_after_paste.get('sendEnabled'):
                        emit_log(q,
                                 f"Bloco aceito (send habilitado, provavel anexo): {total} de ~{len(inner)} chars")
                    await asyncio.sleep(0.3)
            else:
                if segment.strip():
                    if activityts:
                        activityts[0] = time.time()
                    await type_realistic(page, segment, q)
    else:
        await type_realistic(page, message, q)


async def type_realistic(page, text, q=None):
    total = len(text)
    last_status_time = time.time()
    for i, char in enumerate(text):
        if char == '\n':
            await page.keyboard.down("Shift")
            await page.keyboard.press("Enter")
            await page.keyboard.up("Shift")
            await asyncio.sleep(random.uniform(0.01, 0.05))
        else:
            await page.keyboard.type(char)
            # Variabilidade ajustada: 10ms a 80ms
            await asyncio.sleep(random.uniform(0.01, 0.08))

        # --- KEEP-ALIVE: Emite status a cada 2 segundos ---
        current_time = time.time()
        if q and (current_time - last_status_time) >= 2.0:
            emit_event(q, "status", f"Digitando... {int((i+1)/total*100)}%")
            last_status_time = current_time  # Reseta o cronômetro


async def _clear_input(page, q=None):
    """Limpa qualquer texto residual no input do ChatGPT antes de digitar."""
    try:
        cleared = await page.evaluate("""() => {
            const ta = document.getElementById('prompt-textarea')
                     || document.querySelector('#prompt-textarea');
            if (!ta) return false;

            if (ta.isContentEditable) {
                if (!ta.innerText.trim()) return false; // já vazio
                ta.focus();
                // Seleciona tudo e deleta
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                // Fallback: limpa innerHTML diretamente se ainda sobrou algo
                if (ta.innerText.trim()) {
                    ta.innerHTML = '';
                    ta.dispatchEvent(new InputEvent('input', { bubbles: true }));
                }
            } else {
                if (!ta.value) return false; // já vazio
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                setter.call(ta, '');
                ta.dispatchEvent(new InputEvent('input', { bubbles: true }));
            }
            return true;
        }""")
        if cleared:
            emit_log(q, "🧹 Input limpo (havia texto residual).")
    except Exception as e:
        emit_log(q, f"⚠️ Falha ao limpar input: {e}")


RATE_LIMIT_BANNER_JS = """() => {
    const selectors = [
        '[role="dialog"]',
        '[role="alert"]',
        '[role="alertdialog"]',
        '[aria-live="polite"]',
        '[aria-live="assertive"]',
        '[data-testid*="rate" i]',
        '[data-testid*="limit" i]',
        '[data-testid*="toast" i]',
        '[class*="Toast" i]',
        '[class*="toast" i]',
        '[class*="Banner" i]',
        '[class*="banner" i]',
        '[class*="RateLimit" i]',
        '[class*="rate-limit" i]',
        'main [data-message-author-role="assistant"] .text-token-text-error',
        'main [data-message-author-role="assistant"] [class*="error" i]'
    ];
    const candidates = Array.from(document.querySelectorAll(selectors.join(',')));
    candidates.sort((a, b) => ((a.innerText || '').length - (b.innerText || '').length));
    const hit = candidates.find(el => {
        const txt = (el.innerText || '').trim().toLowerCase();
        if (!txt || txt.length < 10 || txt.length > 800) return false;
        const hasPt = txt.includes('excesso de solicita') && txt.includes('aguarde');
        const hasEn = (
            (txt.includes('too many requests') && (txt.includes('minute') || txt.includes('wait') || txt.includes('try again'))) ||
            (txt.includes('rate limit') && (txt.includes('exceed') || txt.includes('wait') || txt.includes('minute'))) ||
            (txt.includes("you've reached") && (txt.includes('limit') || txt.includes('message'))) ||
            (txt.includes('message limit') && txt.includes('reach'))
        );
        return hasPt || hasEn;
    });
    if (!hit) {
        return { detected: false, message: '' };
    }
    return { detected: true, message: (hit.innerText || '').trim().slice(0, 600) };
}"""


async def _detect_rate_limit_banner(page) -> dict:
    """Retorna {'detected': bool, 'message': str}. Nunca levanta."""
    try:
        state = await page.evaluate(RATE_LIMIT_BANNER_JS)
        if isinstance(state, dict) and state.get("detected"):
            return {"detected": True, "message": (state.get("message") or "").strip()}
    except Exception:
        pass
    return {"detected": False, "message": ""}


async def wait_for_chat_ready(page, url: str, q=None, timeout: int = 30) -> bool:
    """
    Aguarda o ChatGPT terminar de carregar um chat existente.
    Usa múltiplos sinais — o primeiro que confirmar encerra a espera.

    Se detectar banner de rate-limit (antes ou depois do timeout do
    textarea), levanta `RateLimitDetected` em vez de retornar False para
    que o caller possa emitir o evento 'rate_limit' adequado.
    """
    emit_log(q, "⏳ Aguardando chat carregar completamente...")
    deadline = asyncio.get_event_loop().time() + timeout

    # 0. Pré-check: já há banner de rate-limit? (evita esperar 10s em vão)
    pre_rl = await _detect_rate_limit_banner(page)
    if pre_rl["detected"]:
        emit_log(q, f"⛔ Rate-limit detectado antes do carregamento: {pre_rl['message'][:220]}")
        raise RateLimitDetected(pre_rl["message"])

    # 1. Textarea presente — pré-requisito mínimo
    try:
        await page.wait_for_selector("#prompt-textarea", timeout=10_000)
    except Exception:
        # Antes de declarar falha genérica, confere se é rate-limit (causa comum
        # de sumiço do composer). Isso transforma um "Timeout waiting for
        # #prompt-textarea" em um erro 'rate_limit' claro para o agente.
        post_rl = await _detect_rate_limit_banner(page)
        if post_rl["detected"]:
            emit_log(q, f"⛔ Rate-limit detectado (textarea indisponível): {post_rl['message'][:220]}")
            raise RateLimitDetected(post_rl["message"])
        emit_log(q, "❌ prompt-textarea não encontrado.")
        return False

    # 2. Mensagens carregadas (histórico hidratado) — só exige se for chat com histórico
    if "/c/" in url:
        try:
            await page.wait_for_selector("[data-message-author-role]", timeout=15_000)
        except Exception:
            try:
                await page.wait_for_selector("article", timeout=5_000)
            except Exception:
                emit_log(q, "⚠️ Sem mensagens (chat novo ou vazio). Continuando...")

    # 3. Poll com múltiplos sinais — o mais robusto
    JS_CHECK = """() => {
        // Sinal 1: textarea habilitado
        const ta = document.querySelector('#prompt-textarea');
        if (!ta || ta.disabled) return { ready: false, signal: 'textarea_disabled' };

        // Sinal 2: se houver "Stop generating", ainda está processando
        const stopBtn = document.querySelector(
            'button[aria-label="Stop generating"], button[data-testid="stop-button"]'
        );
        if (stopBtn) {
            return { ready: false, signal: 'generating' };
        }

        // Sinal 3: send-button existe e não está disabled/aria-disabled
        const btn = document.querySelector('button[data-testid="send-button"]');
        if (btn) {
            const ariaDisabled = btn.getAttribute('aria-disabled');
            if (!btn.disabled && ariaDisabled !== 'true') {
                return { ready: true, signal: 'send_button_enabled' };
            }
            // No ChatGPT o botão costuma ficar desabilitado quando o input está vazio.
            // Isso NÃO significa que a página não está pronta.
            return { ready: true, signal: 'send_button_disabled_but_idle' };
        }

        // Sinal 4: fallback — sem send-button e sem "Stop", mas textarea habilitado
        return { ready: true, signal: 'no_send_button_fallback' };
    }"""

    attempt = 0
    while asyncio.get_event_loop().time() < deadline:
        attempt += 1
        try:
            result = await page.evaluate(JS_CHECK)
            if result and result.get("ready"):
                emit_log(q, f"✅ Chat pronto (sinal: {result.get('signal')}, tentativa #{attempt})")
                return True
        except Exception as e:
            emit_log(q, f"⚠️ Erro no poll #{attempt}: {e}")

        await asyncio.sleep(0.4)

    emit_log(q, f"⚠️ Timeout após {attempt} tentativas. Continuando mesmo assim...")
    return False



def clean_html(html_content):
    if not html_content: return ""
    html = html_content.replace('<span class="result-streaming-cursor"></span>', '')
    html = re.sub(r'href="/cdn/assets/[^"]+"', 'href="#"', html)
    html = re.sub(r'src="/cdn/assets/[^"]+"', 'src=""', html)
    # Preserva <a> de download antes de remover buttons
    # ChatGPT às vezes embute download links dentro de divs/buttons
    download_links = re.findall(
        r'<a\s+[^>]*href="([^"]*(?:/backend-api/files/|/backend-api/conversation/[^"]*/interpreter/download|files\.oaiusercontent\.com|sandbox:/)[^"]*)"[^>]*>([^<]*)</a>',
        html, re.IGNORECASE
    )
    # Também captura links com atributo download
    download_links += re.findall(
        r'<a\s+[^>]*download[^>]*href="([^"]+)"[^>]*>([^<]*)</a>',
        html, re.IGNORECASE
    )
    download_links += re.findall(
        r'<a\s+[^>]*href="([^"]+)"[^>]*download[^>]*>([^<]*)</a>',
        html, re.IGNORECASE
    )
    # Deduplica
    seen_hrefs = set()
    unique_dl = []
    for href, text in download_links:
        if href not in seen_hrefs and not href.startswith('#'):
            seen_hrefs.add(href)
            unique_dl.append((href, text))
    download_links = unique_dl

    # Remove <button>…</button> mas PRESERVA <img> e <a> que estiverem dentro deles.
    # Motivo: ChatGPT envolve previews de imagens e cards de arquivo em <button>,
    # e uma remoção ingênua faria sumir a imagem e o link de download da mensagem.
    def _strip_button_keep_media(match):
        inner = match.group(1) or ''
        imgs = re.findall(r'<img[^>]*>', inner, flags=re.IGNORECASE)
        anchors = re.findall(r'<a[^>]*>.*?</a>', inner, flags=re.IGNORECASE | re.DOTALL)
        return ''.join(imgs) + ''.join(anchors)
    html = re.sub(r'<button[^>]*>(.*?)</button>', _strip_button_keep_media, html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<div class="flex gap-1.*?</div>', '', html, flags=re.DOTALL)
    # Reinsere links de download que podem ter sido perdidos
    for href, text in download_links:
        display = text.strip() or href.split('/')[-1]
        if display and href not in html:
            html += f'\n<p>Arquivo: <a href="{href}" download>{display}</a></p>'
    return html


async def _read_last_assistant_snapshot(page):
    """
    Lê o último balão do assistant diretamente do DOM, sem usar Locator.inner_html().

    Motivo:
    - Locator.inner_html() aguarda a existência do elemento e pode estourar timeout
      quando o ChatGPT ainda não materializou o balão do assistant no DOM.
    - Durante respostas longas, streaming, tools/browsing ou mudanças de layout,
      o balão pode ser recriado e alguns locators ficam instáveis.

    Retorna sempre um dict com `html` e `text`; se não houver mensagem ainda,
    retorna strings vazias.
    """
    try:
        snapshot = await page.evaluate("""() => {
            const nodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
            if (!nodes.length) {
                return { html: '', text: '' };
            }

            const preferred =
                [...nodes].reverse().find((node) => {
                    if (!node) return false;
                    const txt = (node.innerText || '').trim();
                    const html = (node.innerHTML || '').trim();
                    return !!txt || !!html;
                }) || nodes[nodes.length - 1];

            return {
                html: preferred?.innerHTML || '',
                text: (preferred?.innerText || '').trim(),
            };
        }""")
    except Exception:
        return {"html": "", "text": ""}

    if not isinstance(snapshot, dict):
        return {"html": "", "text": ""}

    return {
        "html": snapshot.get("html") or "",
        "text": snapshot.get("text") or "",
    }


async def _read_assistant_snapshot_after_baseline(page, baseline_count: int):
    """
    Lê o primeiro balão do assistant criado após o baseline_count.
    Evita juntar duas respostas quando, por qualquer motivo, duas perguntas
    acabam sendo enviadas em sequência no mesmo chat.
    """
    try:
        snapshot = await page.evaluate("""(baseline) => {
            const nodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
            if (!nodes.length) return { html: '', text: '' };
            let preferred = null;
            if (Number.isInteger(baseline) && baseline >= 0 && baseline < nodes.length) {
                preferred = nodes[baseline];
            }
            if (!preferred) {
                preferred =
                    [...nodes].reverse().find((node) => {
                        if (!node) return false;
                        const txt = (node.innerText || '').trim();
                        const html = (node.innerHTML || '').trim();
                        return !!txt || !!html;
                    }) || nodes[nodes.length - 1];
            }
            return {
                html: preferred?.innerHTML || '',
                text: (preferred?.innerText || '').trim(),
            };
        }""", int(max(0, baseline_count or 0)))
    except Exception:
        return {"html": "", "text": ""}
    if not isinstance(snapshot, dict):
        return {"html": "", "text": ""}
    return {
        "html": snapshot.get("html") or "",
        "text": snapshot.get("text") or "",
    }


async def _get_assistant_message_count(page) -> int:
    try:
        n = await page.evaluate("""() => {
            return document.querySelectorAll('[data-message-author-role="assistant"]').length || 0;
        }""")
        return int(n or 0)
    except Exception:
        return 0


def _resolve_chatgpt_download_url(raw_url: str) -> str:
    """Normaliza URL de download do ChatGPT para forma absoluta."""
    if raw_url.startswith("/"):
        return f"https://chatgpt.com{raw_url}"
    return raw_url


async def _detect_and_register_files(page, markdown_text, q=None, allow_click_fallback=True, scan_page_fallback=True):
    """
    Detecta links de download no markdown da resposta do ChatGPT e
    registra as URLs originais em shared.file_registry para proxy
    sob demanda (sem baixar o arquivo agora).
    Retorna o markdown com URLs reescritas para /api/downloads/<file_id>.
    """
    # Padrões de URL de download do ChatGPT:
    # [filename](url) onde url é:
    #   /backend-api/files/.../download
    #   /backend-api/conversation/<id>/interpreter/download?... (UI atual de planilhas)
    #   https://files.oaiusercontent.com/...
    #   sandbox:/mnt/data/...
    link_pattern = re.compile(
        r'\[([^\]]+)\]\(((?:https?://(?:files\.oaiusercontent\.com|cdn-uploads\.[^)]+)|/backend-api/files/[^)]+|/backend-api/conversation/[^)]+/interpreter/download[^)]*|sandbox:/[^)]+))\)'
    )

    # Padrão secundário: qualquer link markdown cujo texto termina com extensão de arquivo comum
    file_ext_link_pattern = re.compile(
        r'\[([^\]]*\.(?:xlsx|xls|csv|pdf|docx|doc|pptx|ppt|zip|rar|json|xml|txt|png|jpg|jpeg|gif|svg))\]\(([^)]+)\)',
        re.IGNORECASE
    )

    matches = list(link_pattern.finditer(markdown_text))
    # Se o padrão principal não encontrar nada, tenta o padrão secundário (por extensão de arquivo)
    if not matches and scan_page_fallback:
        matches = list(file_ext_link_pattern.finditer(markdown_text))
    if not matches:
        # Fallback: tenta detectar links de download na página via múltiplos seletores
        try:
            page_links = await page.evaluate("""() => {
                const links = [];
                const seen = new Set();
                // Seletor 1: links com /backend-api/files/
                document.querySelectorAll('a[href*="/backend-api/files/"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (href && text && !seen.has(href)) { seen.add(href); links.push({href, text}); }
                });
                // Seletor 1b: endpoint de download do interpreter (cards de planilha atuais)
                document.querySelectorAll('a[href*="/backend-api/conversation/"][href*="/interpreter/download"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (href && !href.startsWith('#') && !seen.has(href)) {
                        seen.add(href);
                        links.push({href, text: text || href.split('/').pop()});
                    }
                });
                // Seletor 2: links com files.oaiusercontent.com
                document.querySelectorAll('a[href*="files.oaiusercontent.com"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (href && text && !seen.has(href)) { seen.add(href); links.push({href, text}); }
                });
                // Seletor 3: links com atributo download
                document.querySelectorAll('a[download]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || a.getAttribute('download') || '').trim();
                    if (href && !href.startsWith('#') && !seen.has(href)) { seen.add(href); links.push({href, text: text || href.split('/').pop()}); }
                });
                // Seletor 4: botões/links em cards de arquivo do code interpreter
                // ChatGPT renderiza como <a> dentro de containers com data-testid ou classes específicas
                document.querySelectorAll('[data-testid*="file"] a, .sandbox-result a, .code-output a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (href && text && !seen.has(href)) { seen.add(href); links.push({href, text}); }
                });
                // Seletor 5: qualquer <a> cujo texto ou href termina com extensão de arquivo comum
                // (captura padrões novos do ChatGPT que não usam as URLs tradicionais)
                const fileExts = /\.(xlsx|xls|csv|pdf|docx|doc|pptx|ppt|zip|rar|json|xml|txt|png|jpg|jpeg|gif|svg)$/i;
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    const dl = a.getAttribute('download') || '';
                    if ((fileExts.test(text) || fileExts.test(dl) || fileExts.test(href))
                        && href && !href.startsWith('#') && !href.startsWith('javascript:')
                        && !seen.has(href)) {
                        seen.add(href);
                        links.push({href, text: text || dl || href.split('/').pop()});
                    }
                });
                return links;
            }""")
            if page_links:
                for pl in page_links:
                    safe_name = re.sub(r'[^\w.\-]', '_', pl['text']) or "file"
                    file_id = f"{int(time.time() * 1000)}_{safe_name}"
                    full_url = _resolve_chatgpt_download_url(pl['href'])
                    register_file(file_id, full_url, pl['text'])
                    markdown_text += f"\n\n📎 Arquivo: [{pl['text']}](/api/downloads/{file_id})"
                    emit_log(q, f"📎 Arquivo registrado (da página): {pl['text']} → {file_id}")
        except Exception as e:
            emit_log(q, f"⚠️ Erro ao detectar links na página: {e}")

        # Fallback extra: varre cards de arquivo do ChatGPT (div.group.my-4.w-full.rounded-2xl)
        # e cruza com metadados capturados via network listener (_install_conversation_file_capture).
        # Para cada card encontrado, anexa o nome do arquivo + preview (se houver) + link de download
        # (resolvido via API da conversa, quando disponível).
        try:
            cards = await _scan_file_cards(page)
            if cards:
                captured = list(getattr(page, "_captured_files", []) or [])
                cap_by_name = {}
                for cf in captured:
                    nm = (cf.get("name") or "").strip().lower()
                    if nm:
                        cap_by_name.setdefault(nm, cf)

                for card in cards:
                    name = (card.get("name") or "").strip()
                    if not name:
                        continue
                    if f"]({name})" in markdown_text or f"/{name}" in markdown_text:
                        # Já referenciado como link
                        continue

                    # Tenta resolver download URL via conversation API capture
                    matched = cap_by_name.get(name.lower())
                    download_url = ""
                    if matched:
                        download_url = (matched.get("url") or "").strip()
                        chatgpt_fid = (matched.get("file_id") or "").strip()
                        if not download_url and chatgpt_fid and chatgpt_fid.startswith("file-"):
                            try:
                                resolved = await page.evaluate("""async (fid) => {
                                    try {
                                        const r = await fetch('/backend-api/files/' + fid + '/download',
                                            { credentials: 'include' });
                                        if (!r.ok) return null;
                                        const d = await r.json();
                                        return d.download_url || '';
                                    } catch (e) { return null; }
                                }""", chatgpt_fid)
                                if resolved:
                                    download_url = resolved
                            except Exception:
                                pass

                    safe = re.sub(r'[^\w.\-]', '_', name) or "file"
                    file_id = f"card_{int(time.time() * 1000)}_{safe}"
                    if download_url:
                        register_file(file_id, download_url, name)
                        markdown_text += f"\n\n📎 Arquivo: [{name}](/api/downloads/{file_id})"
                        emit_log(q, f"📎 Card de arquivo registrado: {name} → {file_id}")
                    else:
                        # Mesmo sem URL resolvida, exibe o nome para o usuário saber que o arquivo existe
                        markdown_text += f"\n\n📎 Arquivo: **{name}** (URL indisponível — clique manualmente no ChatGPT para baixar)"
                        emit_log(q, f"⚠️ Card de arquivo sem URL resolvida: {name}")

                    # Preview image do card (geralmente base64 data URI)
                    preview = (card.get("preview") or "").strip()
                    if preview and preview not in markdown_text:
                        markdown_text += f"\n\n![{name}]({preview})"
        except Exception as e:
            emit_log(q, f"⚠️ Erro ao varrer file cards: {e}")

        # Fallback 2: clica em elementos de download do code interpreter para capturar via auto-download
        if not page_links and allow_click_fallback:
            try:
                clicked = await _click_chatgpt_download_elements(page, q)
                if clicked:
                    await asyncio.sleep(2)  # aguarda downloads serem capturados pelo handler _on_download
            except Exception as e:
                emit_log(q, f"⚠️ Erro ao clicar elementos de download: {e}")

        return markdown_text

    result = markdown_text
    for m in matches:
        display_name = m.group(1).strip()
        raw_url = m.group(2).strip()

        safe_name = re.sub(r'[^\w.\-]', '_', display_name) or "file"
        file_id = f"{int(time.time() * 1000)}_{safe_name}"

        if raw_url.startswith("sandbox:"):
            # Sandbox URL: tenta resolver para URL real na página
            try:
                real_url = await page.evaluate("""(filename) => {
                    const anchors = Array.from(document.querySelectorAll('a[href*="/backend-api/files/"]'));
                    for (const a of anchors) {
                        if (a.textContent.includes(filename)) return a.href;
                    }
                    // Fallback: qualquer <a> com download que contenha o filename
                    const dlLinks = Array.from(document.querySelectorAll('a[download]'));
                    for (const a of dlLinks) {
                        if (a.textContent.includes(filename) || (a.getAttribute('download') || '').includes(filename))
                            return a.href;
                    }
                    return null;
                }""", raw_url.split('/')[-1])
                if real_url:
                    raw_url = real_url
                else:
                    emit_log(q, f"⚠️ Sandbox URL sem link real: {display_name}")
                    continue
            except Exception:
                emit_log(q, f"⚠️ Não foi possível resolver sandbox URL: {display_name}")
                continue

        full_url = _resolve_chatgpt_download_url(raw_url)
        register_file(file_id, full_url, display_name)
        result = result.replace(m.group(0), f"[{display_name}](/api/downloads/{file_id})")
        emit_log(q, f"📎 Arquivo registrado: {display_name} → {file_id}")

    return result


def _install_conversation_file_capture(page, q=None):
    """
    Instala um listener de respostas de rede que captura metadados de arquivos
    a partir das APIs internas do ChatGPT. Muitos arquivos gerados por
    code_interpreter / canvas não aparecem como <a> ou <button> com href,
    então interceptar o JSON da conversa é a fonte mais confiável.

    Captura:
      • /backend-api/conversation/<id> (JSON completo da conversa — contém
        attachments/metadata com file_id e filename)
      • /backend-api/files/<id>/download (resposta com URL pré-assinada para
        download efetivo — pode aparecer na aba Network quando o usuário clica)

    Os arquivos capturados ficam em page._captured_files como dicts
    {file_id, name, url} para consumo posterior pelo fluxo de SYNC / resposta.
    """
    try:
        page._captured_files = []
        seen_ids = set()

        def _maybe_add(file_id: str, name: str, url: str = "", message_id: str = ""):
            key = (file_id or "") + "|" + (name or "") + "|" + (url or "") + "|" + (message_id or "")
            if not key or key in seen_ids:
                return
            seen_ids.add(key)
            page._captured_files.append({
                "file_id": file_id or "",
                "name": name or "",
                "url": url or "",
                "message_id": message_id or "",
            })

        async def _on_response(response):
            try:
                url = response.url or ""
            except Exception:
                return
            try:
                # Caso 1: GET /backend-api/conversation/<id> → JSON completo da conversa
                if "/backend-api/conversation/" in url and "/prepare" not in url and "/interrupt" not in url:
                    try:
                        ct = (response.headers.get("content-type") or "").lower()
                    except Exception:
                        ct = ""
                    if "json" in ct:
                        try:
                            data = await response.json()
                        except Exception:
                            return
                        mapping = (data or {}).get("mapping") or {}
                        for node in mapping.values():
                            msg = (node or {}).get("message") or {}
                            meta = msg.get("metadata") or {}
                            # Formato 1: metadata.attachments = [{id, name, ...}]
                            for att in (meta.get("attachments") or []):
                                _maybe_add(
                                    file_id=str(att.get("id") or ""),
                                    name=str(att.get("name") or att.get("file_name") or ""),
                                    url="",
                                    message_id=str(msg.get("id") or "")
                                )
                            # Formato 2: metadata.aggregate_result.messages[].results[].files[]
                            agg = meta.get("aggregate_result") or {}
                            for sub in (agg.get("messages") or []):
                                for res in (sub.get("results") or []):
                                    for f in (res.get("files") or []):
                                        _maybe_add(
                                            file_id=str(f.get("id") or f.get("sandbox_path") or ""),
                                            name=str(f.get("name") or f.get("file_name") or ""),
                                            url=str(f.get("url") or ""),
                                            message_id=str(msg.get("id") or "")
                                        )
                            # Formato 3: content.parts[].asset_pointer (file-service://file-xxx)
                            content = msg.get("content") or {}
                            for part in (content.get("parts") or []):
                                if isinstance(part, dict):
                                    ap = part.get("asset_pointer") or ""
                                    if isinstance(ap, str) and ap.startswith("file-service://"):
                                        fid = ap.replace("file-service://", "")
                                        _maybe_add(
                                            file_id=fid,
                                            name=str(part.get("metadata", {}).get("filename") or ""),
                                            url="",
                                            message_id=str(msg.get("id") or "")
                                        )
                # Caso 2: GET /backend-api/files/<id>/download → JSON com {download_url, file_name}
                elif "/backend-api/files/" in url and "/download" in url:
                    try:
                        ct = (response.headers.get("content-type") or "").lower()
                    except Exception:
                        ct = ""
                    if "json" in ct:
                        try:
                            data = await response.json()
                        except Exception:
                            return
                        dl = (data or {}).get("download_url") or ""
                        nm = (data or {}).get("file_name") or ""
                        # Extrai file_id do próprio URL: /backend-api/files/{file_id}/download
                        mfid = re.search(r'/backend-api/files/([^/]+)/download', url)
                        fid = mfid.group(1) if mfid else ""
                        if dl or nm or fid:
                            _maybe_add(file_id=fid, name=nm, url=dl)
                # Caso 3: GET /backend-api/conversation/<id>/interpreter/download?... (UI atual)
                elif "/backend-api/conversation/" in url and "/interpreter/download" in url:
                    # Extrai metadados úteis direto da query string para não depender
                    # de um JSON específico na resposta (que pode vir como blob/binário).
                    try:
                        from urllib.parse import urlparse, parse_qs, unquote
                        parsed = urlparse(url)
                        qd = parse_qs(parsed.query or "")
                        message_id = (qd.get("message_id") or [""])[0]
                        sandbox_path = (qd.get("sandbox_path") or [""])[0]
                        sandbox_path = unquote(sandbox_path or "")
                        guessed_name = os.path.basename(sandbox_path) if sandbox_path else ""
                        # Usa o próprio endpoint autenticado como URL sob demanda.
                        # O proxy /api/downloads buscará via Playwright com credentials.
                        if message_id or guessed_name:
                            fid = f"interpreter:{message_id}:{sandbox_path}" if (message_id or sandbox_path) else ""
                            _maybe_add(file_id=fid, name=guessed_name, url=url, message_id=message_id)
                    except Exception:
                        pass
            except Exception as e:
                emit_log(q, f"⚠️ Erro ao inspecionar resposta de rede: {e}")

        page.on("response", lambda resp: asyncio.create_task(_on_response(resp)))
    except Exception as e:
        emit_log(q, f"⚠️ Falha ao instalar captura de conversa: {e}")


async def _register_captured_files(page, q=None):
    """
    Lê page._captured_files (preenchido por _install_conversation_file_capture),
    resolve URLs de download ainda não resolvidas (via /backend-api/files/{id}/download)
    e registra cada arquivo em shared.file_registry.

    Retorna uma lista de dicts:
      {file_id_local, name, message_id}
    para os arquivos efetivamente registrados para proxy.
    """
    registered = []
    try:
        captured = list(getattr(page, "_captured_files", []) or [])
    except Exception:
        captured = []
    if not captured:
        return registered

    chat_id_for_interpreter = ""
    try:
        page_url = page.url or ""
        m_chat = re.search(r"/c/([A-Za-z0-9\-]+)", page_url)
        if m_chat:
            chat_id_for_interpreter = m_chat.group(1)
    except Exception:
        chat_id_for_interpreter = ""

    for item in captured:
        chatgpt_file_id = (item.get("file_id") or "").strip()
        message_id = (item.get("message_id") or "").strip()
        name = (item.get("name") or "").strip() or (chatgpt_file_id or "file")
        url = (item.get("url") or "").strip()

        # Se não temos URL mas temos file_id, tenta resolver via API
        if not url and chatgpt_file_id and chatgpt_file_id.startswith("file-"):
            try:
                resolved = await page.evaluate("""async (fid) => {
                    try {
                        const resp = await fetch('/backend-api/files/' + fid + '/download', {
                            credentials: 'include'
                        });
                        if (!resp.ok) return null;
                        const data = await resp.json();
                        return { url: data.download_url || '', name: data.file_name || '' };
                    } catch (e) { return null; }
                }""", chatgpt_file_id)
                if resolved:
                    url = resolved.get("url") or url
                    if not name or name == chatgpt_file_id:
                        name = resolved.get("name") or name
            except Exception:
                pass

        if not url:
            # Fallback para UI nova: monta endpoint interpreter/download quando
            # temos message_id + sandbox_path mas sem URL explícita.
            sandbox_like = chatgpt_file_id.startswith("/mnt/data/") or chatgpt_file_id.startswith("mnt/data/")
            if (not sandbox_like) and chatgpt_file_id and ("/mnt/data/" in chatgpt_file_id):
                sandbox_like = True
            if (not url) and chat_id_for_interpreter and message_id and sandbox_like:
                sandbox_path = chatgpt_file_id if chatgpt_file_id.startswith("/") else f"/{chatgpt_file_id}"
                from urllib.parse import quote
                url = (
                    f"https://chatgpt.com/backend-api/conversation/{chat_id_for_interpreter}"
                    f"/interpreter/download?message_id={quote(message_id)}&sandbox_path={quote(sandbox_path)}"
                )
                if not name:
                    name = os.path.basename(sandbox_path) or "file"

        if not url:
            continue

        safe = re.sub(r'[^\w.\-]', '_', name) or "file"
        local_file_id = f"conv_{int(time.time() * 1000)}_{safe}"
        register_file(local_file_id, url, name)
        registered.append({
            "file_id_local": local_file_id,
            "name": name,
            "message_id": message_id
        })
        emit_log(q, f"📎 Arquivo (via conversation API): {name} → {local_file_id}")

    return registered


async def _scan_file_cards(page):
    """
    Varre o DOM procurando pelos "cards" de arquivo que o ChatGPT novo renderiza
    para planilhas, canvases e outros artefatos do code interpreter.

    Estrutura típica observada (abril/2026):
        <div class="group my-4 w-full rounded-2xl corner-superellipse/1.1 ...">
          <div class="... border-b">                       ← cabeçalho
            <div class="... ps-1">
              <div class="rounded-md p-1 ...">
                <svg>...</svg>
              </div>
              <div class="truncate text-sm font-medium">NOME.ext</div>  ← filename
            </div>
            <div class="text-token-text-secondary ...">
              <button class="... rounded-full p-1"> ... </button>     ← expand/fullscreen
              <button class="... rounded-full p-1"> ... </button>     ← download
            </div>
          </div>
          <div class="... bg-token-bg-tertiary ...">                  ← preview
            ...
            <img src="data:image/png;base64,...">
          </div>
        </div>

    Como o filename NÃO fica dentro de <a>, nenhum dos seletores tradicionais
    o encontra. Esta função extrai:
      - name    : nome do arquivo (do div.truncate.text-sm.font-medium)
      - preview : src do <img> dentro do card (pode ser data: base64 ou URL)

    Retorna lista de dicts {name, preview, card_index}.
    """
    try:
        return await page.evaluate("""() => {
            const fileExts = /\\.(xlsx|xls|csv|pdf|docx|doc|pptx|ppt|zip|rar|json|xml|txt|png|jpg|jpeg|gif|svg)$/i;
            const selectors = [
                'div.group.my-4.w-full.rounded-2xl',
                'div[class*="corner-superellipse"]'
            ];
            const seen = new Set();
            const seenRows = new Set();
            const results = [];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach((card, idx) => {
                    if (seen.has(card)) return;
                    seen.add(card);

                    // Tenta encontrar o filename via seletor direto;
                    // fallback: qualquer texto dentro do card que termine com extensão conhecida.
                    let name = '';
                    const nameEl = card.querySelector('div.truncate.text-sm.font-medium');
                    if (nameEl) {
                        name = (nameEl.textContent || '').trim();
                    }
                    if (!name || !fileExts.test(name)) {
                        const text = (card.innerText || '').trim();
                        const m = text.match(/[\\w\\-. ]+\\.(xlsx|xls|csv|pdf|docx|doc|pptx|ppt|zip|rar|json|xml|txt|png|jpg|jpeg|gif|svg)/i);
                        if (m) name = m[0].trim();
                    }
                    if (!name) return;

                    // Preview image (base64 ou URL)
                    let preview = '';
                    const imgEl = card.querySelector('img[src]');
                    if (imgEl) preview = imgEl.getAttribute('src') || '';

                    // Identifica a qual mensagem o card pertence (assistant-turn)
                    // subindo na árvore até achar um [data-message-id].
                    let messageId = '';
                    let turnIndex = -1;
                    let node = card.parentElement;
                    while (node) {
                        if (node.getAttribute && node.getAttribute('data-message-id')) {
                            messageId = node.getAttribute('data-message-id') || '';
                            break;
                        }
                        node = node.parentElement;
                    }
                    // Índice do turn (1-based) dentro da conversa
                    if (messageId) {
                        const allMsgs = Array.from(document.querySelectorAll('[data-message-author-role]'));
                        turnIndex = allMsgs.findIndex(el => el.getAttribute('data-message-id') === messageId);
                    }

                    const rowKey = `${messageId || ''}|${name}|${preview || ''}`;
                    if (seenRows.has(rowKey)) return;
                    seenRows.add(rowKey);

                    results.push({
                        name: name,
                        preview: preview,
                        card_index: idx,
                        message_id: messageId,
                        turn_index: turnIndex
                    });
                });
            });
            return results;
        }""")
    except Exception:
        return []


async def _click_chatgpt_download_elements(page, q=None):
    """
    Detecta e clica elementos de download de arquivo do ChatGPT code interpreter.
    Isso dispara o evento 'download' do Playwright que é capturado por _on_download.
    Retorna True se algum elemento foi clicado.
    """
    try:
        # Procura elementos clicáveis que representam downloads de arquivo do code interpreter
        download_elements = await page.evaluate("""() => {
            const results = [];
            // Padrão 1: links com texto contendo extensões de arquivo comuns
            const fileExts = /\.(xlsx|xls|csv|pdf|docx|doc|pptx|ppt|zip|rar|json|xml|txt|png|jpg|jpeg|gif|svg)$/i;
            document.querySelectorAll('a').forEach((a, i) => {
                const text = (a.textContent || '').trim();
                const href = a.getAttribute('href') || '';
                const dl = a.getAttribute('download') || '';
                if ((fileExts.test(text) || fileExts.test(dl) || fileExts.test(href)) && !href.startsWith('#')) {
                    results.push({index: i, text: text || dl || href.split('/').pop(), selector: 'a'});
                }
            });
            // Padrão 2: botões dentro da última resposta do assistant que contenham texto de arquivo
            const lastMsg = [...document.querySelectorAll('[data-message-author-role="assistant"]')].pop();
            if (lastMsg) {
                lastMsg.querySelectorAll('button, [role="button"]').forEach((btn, i) => {
                    const text = (btn.textContent || '').trim();
                    if (fileExts.test(text)) {
                        results.push({index: i, text, selector: 'button_in_last'});
                    }
                });
            }

            // Padrão 3: cards de arquivo recentes (UI nova), onde os botões podem ser ícones.
            // IMPORTANTE: filtrar SOMENTE botões com forte indicação de download para
            // não clicar em abas (ex.: Resumo/Atendimentos) e gerar erro de overlay.
            const cardSelectors = [
                'div.group.my-4.w-full.rounded-2xl',
                'div[class*="corner-superellipse"]'
            ];
            const seenCards = new Set();
            cardSelectors.forEach(sel => {
                document.querySelectorAll(sel).forEach((card, cardIdx) => {
                    if (seenCards.has(card)) return;
                    seenCards.add(card);

                    const headerText = (card.innerText || '').trim();
                    const m = headerText.match(/[\\w\\-. ]+\\.(xlsx|xls|csv|pdf|docx|doc|pptx|ppt|zip|rar|json|xml|txt|png|jpg|jpeg|gif|svg)/i);
                    if (!m) return;
                    const filename = m[0].trim();

                    const buttons = card.querySelectorAll('button, [role="button"]');
                    buttons.forEach((btn, btnIdx) => {
                        const disabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true';
                        if (disabled) return;
                        const textLower = (btn.textContent || '').trim().toLowerCase();
                        const classLower = String(btn.className || '').toLowerCase();
                        const isTabLike = /resumo|atendimentos|overview|table|tabela|chart|gráfico/.test(textLower)
                            || classLower.includes('border-t-2');
                        if (isTabLike) return;

                        const looksIconButton = classLower.includes('rounded-full')
                            || !!btn.querySelector('svg');
                        const label = (
                            (btn.getAttribute('aria-label') || '') + ' ' +
                            (btn.getAttribute('title') || '') + ' ' +
                            (btn.getAttribute('data-testid') || '') + ' ' +
                            (btn.textContent || '')
                        ).toLowerCase();
                        const explicitDownload = /(download|baixar|file-download|icon-download|transferir|save-file|file-save)/.test(label);
                        const likelyHeaderIcon = looksIconButton && btnIdx < 3;
                        if (!(explicitDownload || likelyHeaderIcon)) return;
                        results.push({
                            cardIndex: cardIdx,
                            buttonIndex: btnIdx,
                            text: filename,
                            selector: 'file_card_btn',
                            cardSelector: sel
                        });
                    });
                });
            });
            return results;
        }""")

        if not download_elements:
            return False

        clicked = False
        context = page.context
        for el in download_elements:
            try:
                baseline_page_ids = {id(p) for p in list(getattr(context, "pages", []) or [])}
                if el['selector'] == 'button_in_last':
                    last_msg = page.locator('[data-message-author-role="assistant"]').last
                    btn = last_msg.locator('button, [role="button"]').nth(el['index'])
                    await btn.click(timeout=2000, force=True)
                elif el['selector'] == 'file_card_btn':
                    cards = page.locator(el.get('cardSelector') or 'div.group.my-4.w-full.rounded-2xl')
                    card = cards.nth(el.get('cardIndex', 0))
                    btn = card.locator('button, [role="button"]').nth(el.get('buttonIndex', 0))
                    await btn.click(timeout=2000, force=True)
                else:
                    link = page.locator('a').nth(el['index'])
                    await link.click(timeout=2000, force=True)
                emit_log(q, f"📎 Clicou elemento de download: {el['text']}")
                clicked = True
                await asyncio.sleep(0.8)

                # Alguns links de PDF abrem em nova aba persistente; fecha qualquer
                # aba criada pelo clique para evitar acúmulo.
                for maybe_new in list(getattr(context, "pages", []) or []):
                    if id(maybe_new) in baseline_page_ids or maybe_new == page:
                        continue
                    try:
                        opened_url = (maybe_new.url or "").strip()
                    except Exception:
                        opened_url = ""
                    try:
                        await maybe_new.close()
                        emit_log(q, f"🧹 Aba aberta por download foi fechada: {opened_url[:120]}")
                    except Exception as close_err:
                        emit_log(q, f"⚠️ Falha ao fechar aba aberta por download: {close_err}")
            except Exception as e:
                detalhe = str(e).splitlines()[0][:220]
                emit_log(q, f"ℹ️ Clique de download ignorado '{el['text']}': {detalhe}")

        return clicked
    except Exception as e:
        emit_log(q, f"⚠️ Erro ao detectar elementos de download clicáveis: {e}")
        return False

async def get_chat_title(page):
    try:
        title = await page.evaluate("""() => {
            const active = document.querySelector('nav a.bg-token-sidebar-surface-tertiary');
            return active ? active.innerText : document.title;
        }""")
        return title
    except: return "Novo Chat"

async def scrape_full_chat(page):
    try:
        # Tenta o seletor moderno primeiro, depois alternativas
        try:
            await page.wait_for_selector('[data-message-author-role]', timeout=6000)
        except:
            try:
                await page.wait_for_selector('section[data-turn]', timeout=3000)
            except:
                try:
                    await page.wait_for_selector('article', timeout=3000)
                except:
                    pass  # Continua mesmo sem encontrar — os fallbacks do JS tentam tudo

        msgs = await page.evaluate("""() => {
            // Helper: remove buttons mas preserva <img> e <a> que estejam dentro deles
            function stripButtonsKeepMedia(html) {
                return html.replace(/<button[^>]*>([\\s\\S]*?)<\\/button>/gi, function(match, inner) {
                    var imgs = inner.match(/<img[^>]*>/gi) || [];
                    var anchors = inner.match(/<a[^>]*>[\\s\\S]*?<\\/a>/gi) || [];
                    return imgs.concat(anchors).join('');
                });
            }

            // ── Estratégia 1: div[data-message-author-role] (layout 2025+) ──
            const roleDivs = document.querySelectorAll('[data-message-author-role]');
            if (roleDivs.length > 0) {
                return Array.from(roleDivs).map(el => {
                    const role = el.getAttribute('data-message-author-role') || 'user';
                    const messageId = el.getAttribute('data-message-id') || '';

                    let contentEl;
                    if (role === 'assistant') {
                        contentEl = el.querySelector('.markdown')
                                 || el.querySelector('.prose')
                                 || el;
                    } else {
                        contentEl = el.querySelector('.whitespace-pre-wrap')
                                 || el;
                    }

                    let html = contentEl.innerHTML || '';
                    html = stripButtonsKeepMedia(html);
                    return { role, content: html, message_id: messageId };
                }).filter(m => m.content && m.content.trim().length > 0);
            }

            // ── Estratégia 2: article (layout legacy) ──
            const articles = Array.from(document.querySelectorAll('article'));
            if (articles.length > 0) {
                return articles.map(art => {
                    const roleEl = art.querySelector('[data-message-author-role]');
                    const role   = roleEl
                        ? roleEl.getAttribute('data-message-author-role')
                        : (art.querySelector('.markdown') ? 'assistant' : 'user');
                    const messageId = roleEl ? (roleEl.getAttribute('data-message-id') || '') : '';

                    let contentEl;
                    if (role === 'assistant') {
                        contentEl = art.querySelector('.markdown')
                                 || art.querySelector('[data-message-author-role="assistant"]');
                    } else {
                        contentEl = art.querySelector('.whitespace-pre-wrap')
                                 || art.querySelector('[data-message-author-role="user"]');
                    }
                    if (!contentEl) contentEl = art;

                    let html = contentEl.innerHTML || '';
                    html = stripButtonsKeepMedia(html);
                    return { role, content: html, message_id: messageId };
                }).filter(m => m.content && m.content.trim().length > 0);
            }

            // ── Estratégia 3: section[data-turn] (layout ChatGPT 2025 alternativo) ──
            const sections = document.querySelectorAll('section[data-turn]');
            if (sections.length > 0) {
                return Array.from(sections).map(sec => {
                    const role = sec.getAttribute('data-turn') || 'user';
                    const messageRoleEl = sec.querySelector('[data-message-author-role]');
                    const messageId = messageRoleEl ? (messageRoleEl.getAttribute('data-message-id') || '') : '';
                    let contentEl;
                    if (role === 'assistant') {
                        contentEl = sec.querySelector('.markdown')
                                 || sec.querySelector('.prose')
                                 || sec.querySelector('[data-message-author-role="assistant"]');
                    } else {
                        contentEl = sec.querySelector('.whitespace-pre-wrap')
                                 || sec.querySelector('[data-message-author-role="user"]');
                    }
                    if (!contentEl) return null;
                    let html = contentEl.innerHTML || '';
                    html = stripButtonsKeepMedia(html);
                    return { role, content: html, message_id: messageId };
                }).filter(m => m && m.content && m.content.trim().length > 0);
            }

            return [];
        }""")
        return msgs or []
    except Exception as e:
        emit_log(None, f"scrape_full_chat erro: {e}")
        return []

async def upload_files(page, file_paths):
    if not file_paths: return False
    try:
        await page.set_input_files("input[type='file']", file_paths)
        try: await page.wait_for_selector("button[aria-label='Remove file']", timeout=10000)
        except: await asyncio.sleep(5)
        return True
    except: return False

async def check_for_dialogs(page, q=None):
    try:
        dialog = page.locator('div[role="dialog"]').first
        if await dialog.is_visible():
            text = await dialog.inner_text()
            if "Copiar link" in text or "Compartilhar" in text:
                emit_log(q, "ℹ️ Modal detectado. Fechando...")
                close_btn = dialog.locator('button[aria-label="Fechar"], button:has-text("Close")').first
                if await close_btn.is_visible(): await close_btn.click()
                else: await page.keyboard.press("Escape")
                return True
            return True
    except: pass
    return False

async def open_sidebar_menu(page, url, q=None):
    try:
        if "/c/" not in url: return []

        # Extrai o UUID corretamente, mesmo se a URL for de projeto
        chat_uuid = url.split("/c/")[1].split("?")[0]

        # Tiramos o "nav" do seletor para que ele ache o chat tanto na barra lateral quanto no centro da página (Projetos)
        link_selector = f'a[href*="{chat_uuid}"]'

        if 'check_for_dialogs' in globals():
            await check_for_dialogs(page, q)

        # Se o menu já estiver aberto na tela, pega logo e devolve
        if await page.is_visible('div[role="menu"]'):
            return await page.evaluate("""() => {
                const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
                return items.map(el => el.textContent.trim()).filter(t => t.length > 0);
            }""")

        try:
            link_locator = page.locator(link_selector).first
            await link_locator.wait_for(state="attached", timeout=8000)
            await link_locator.scroll_into_view_if_needed()
            await link_locator.hover(force=True)
            await asyncio.sleep(0.5)

            menu_btn = link_locator.locator('button[aria-haspopup="menu"]').first
            if not await menu_btn.is_visible():
                menu_btn = link_locator.locator("xpath=..").locator('button[aria-haspopup="menu"]').first

            if await menu_btn.count() > 0:
                await menu_btn.click(force=True)

                emit_log(q, "Aguardando div[role='menu']...")
                await page.wait_for_selector('div[role="menu"]', timeout=5000)
                await asyncio.sleep(0.5) # Aguarda a animação

                # Executa o seu JS original (Aprimorado com textContent)
                options = await page.evaluate("""() => {
                    const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
                    return items.map(el => el.textContent.trim()).filter(t => t.length > 0);
                }""")

                emit_log(q, f"Opções encontradas: {options}")
                print(f"\n[DEBUG CMD] Menu lido com sucesso via JS: {options}\n")
                return options
        except Exception as e:
            emit_log(q, "❌ Chat ou menu não encontrado.")
            print(f"[DEBUG CMD] Erro ao buscar link_locator: {e}")
            return []

        return []
    except Exception as e:
        emit_log(q, f"❌ Erro menu: {e}")
        return []

async def execute_menu_option(page, option_text, url, new_name=None, q=None):
    try:
        # 1. Encontra o link do chat ativo na tela (já foi carregado via page.goto na handle_menu_task)
        chat_id = url.rstrip('/').split('/')[-1]
        chat_link = page.locator(f'a[href*="{chat_id}"]').first

        await chat_link.wait_for(state="attached", timeout=10000)
        await chat_link.scroll_into_view_if_needed()
        await chat_link.hover(force=True)
        await asyncio.sleep(0.5)

        # 2. Abre o menu (3 pontinhos)
        menu_btn = chat_link.locator('button[aria-haspopup="menu"]').first
        if not await menu_btn.is_visible():
            menu_btn = chat_link.locator("xpath=..").locator('button[aria-haspopup="menu"]').first

        if await menu_btn.count() > 0:
            await menu_btn.click(force=True)
            await page.wait_for_selector('div[role="menu"]', timeout=5000)
            await asyncio.sleep(0.5)
        else:
            emit_log(q, "❌ Botão de menu (3 pontos) não encontrado na interface.")
            return False

        emit_log(q, f"🖱️ Executando remotamente: {option_text}")

        # 3. Executa a Ação Desejada
        if "Renomear" in option_text:
            rename_btn = page.locator('div[role="menu"] [role="menuitem"]:has-text("Renomear"), div[role="menu"] [role="menuitem"]:has-text("Rename")').first
            if await rename_btn.is_visible():
                await rename_btn.click(force=True)
                await asyncio.sleep(0.5)
                emit_log(q, f"✏️ Renomeando para: {new_name}")
                try:
                    # Captura qualquer input que surgir na tela
                    input_locator = page.locator("input[type='text']").first
                    if await input_locator.is_visible():
                        await input_locator.click(force=True)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await type_realistic(page, new_name)
                        await page.keyboard.press("Enter")
                        emit_log(q, "✅ Renomeado com sucesso na OpenAI.")
                        return True
                except Exception as ex:
                    emit_log(q, f"❌ Erro ao digitar novo nome: {ex}")
            else:
                emit_log(q, "❌ Opção Renomear não visível no menu remoto.")

        elif "Excluir" in option_text:
            delete_btn = page.locator('div[role="menu"] [role="menuitem"]:has-text("Excluir"), div[role="menu"] [role="menuitem"]:has-text("Delete")').first
            if await delete_btn.is_visible():
                await delete_btn.click(force=True)
                emit_log(q, "🗑️ Confirmando exclusão...")

                # Busca o botão vermelho de confirmação
                confirm_btn = page.locator('button.btn-danger, button[data-testid="confirm-delete-chat-button"]').first
                await confirm_btn.wait_for(timeout=3000)

                if await confirm_btn.is_visible():
                    await confirm_btn.click(force=True)
                    await asyncio.sleep(3)
                    emit_log(q, "✅ Chat excluído com sucesso na OpenAI.")
                    return True
            else:
                emit_log(q, "❌ Opção Excluir não visível no menu remoto.")

        return False

    except Exception as e:
        emit_log(q, f"❌ Erro exec: {e}")
        return False

# --- TAREFAS ASSÍNCRONAS ---

async def handle_menu_task(context, task):
    q = task.get('stream_queue')
    keep_minimized = await _should_keep_context_minimized(context)
    page = await context.new_page()
    await _preserve_minimized_if_needed(page, keep_minimized=keep_minimized)
    try:
        url = task.get('url')
        action = task.get('action')

        if not url:
            raise ValueError("URL do chat não fornecida.")

        emit_log(q, f"Abrindo chat diretamente: {url}")
        await page.goto(url, wait_until="domcontentloaded")

        # --- TRAVA DE CARREGAMENTO (O SEGREDO ESTÁ AQUI) ---
        # Extrai o ID e obriga o script a esperar o elemento existir na tela antes de continuar
        chat_id = url.rstrip('/').split('/')[-1]
        chat_link = page.locator(f'a[href*="{chat_id}"]').first

        emit_log(q, "Aguardando interface estabilizar...")
        await chat_link.wait_for(state="visible", timeout=20000)
        await asyncio.sleep(1) # Pausa extra para os scripts do ChatGPT terminarem de rodar

        # Execução das ações
        if action == 'GET_MENU':
            options = await open_sidebar_menu(page, url, q)
            emit_event(q, "menu_result", {"success": True, "options": options})

        elif action == 'EXEC_MENU':
            opt = task.get('option')
            nn = task.get('new_name')
            success = await execute_menu_option(page, opt, url, nn, q)
            emit_event(q, "exec_result", {"success": success})

    except Exception as e:
        emit_log(q, f"Erro Menu: {e}")
        if action == 'GET_MENU':
            emit_event(q, "menu_result", {"success": False, "error": str(e)})
        else:
            emit_event(q, "exec_result", {"success": False, "error": str(e)})
    finally:
        await page.close()
        if q: q.put(None)

async def handle_sync_task(context, task):
    q       = task.get('stream_queue')
    keep_minimized = await _should_keep_context_minimized(context)
    page    = await context.new_page()
    await _preserve_minimized_if_needed(page, keep_minimized=keep_minimized)
    # Captura de downloads disparados durante SYNC (cards de arquivo sem URL explícita)
    # Sem persistir em disco: preferimos payload em memória (ou URL fallback).
    _sync_auto_downloads = []
    _sync_download_tasks = []

    def _on_sync_download(download):
        async def _save():
            try:
                suggested = download.suggested_filename or "download"
                dl_url = getattr(download, "url", None)
                if callable(dl_url):
                    dl_url = dl_url()

                payload_b64 = None
                try:
                    tmp_path = await download.path()
                    if tmp_path and os.path.isfile(tmp_path):
                        with open(tmp_path, "rb") as f:
                            payload_b64 = base64.b64encode(f.read()).decode("ascii")
                except Exception:
                    payload_b64 = None

                if not payload_b64 and not dl_url:
                    emit_log(q, f"⚠️ [SYNC] Download sem payload/URL disponível: {suggested}")
                    return

                safe = re.sub(r"[^\w.\-]", "_", suggested)
                file_id = f"sync_{int(time.time() * 1000)}_{safe}"
                _sync_auto_downloads.append({
                    "name": suggested,
                    "file_id": file_id,
                    "url": dl_url or "",
                    "payload_b64": payload_b64,
                    "content_type": "application/octet-stream"
                })
                emit_log(q, f"⬇️ [SYNC] Auto-download capturado: {suggested}")
            except Exception as e:
                emit_log(q, f"⚠️ [SYNC] Erro no auto-download: {e}")
        _sync_download_tasks.append(asyncio.create_task(_save()))

    page.on("download", _on_sync_download)
    # Captura metadados de arquivos diretamente das respostas da API da conversa.
    # Isso é mais confiável do que clicar em cards, já que a UI nova do ChatGPT
    # frequentemente renderiza arquivos como botões sem href.
    _install_conversation_file_capture(page, q)
    try:
        url    = task.get('url')
        chat_id = task.get('chat_id')
        emit_log(q, f"🔄 Sync iniciado para {url}")
        await page.goto(url, wait_until='domcontentloaded')
        await _preserve_minimized_if_needed(page, keep_minimized=keep_minimized)

        # Aguarda React renderizar as mensagens
        try:
            await page.wait_for_selector('[data-message-author-role]', timeout=15000)
        except:
            try:
                await page.wait_for_selector('section[data-turn]', timeout=5000)
            except:
                try:
                    await page.wait_for_selector('article', timeout=3000)
                except:
                    await asyncio.sleep(3)

        # Limpa rascunho residual
        await _clear_input(page, q)

        # Verifica chat deletado
        error_banner = page.locator('div:has-text("Unable to load conversation")').first
        if await error_banner.is_visible():
            emit_log(q, "Chat não encontrado.")
            emit_event(q, 'syncresult', {'success': False, 'error': 'chatnotfound'})
            return

        # ✅ Rola o CONTAINER correto do ChatGPT (não a window)
        JS_SCROLL = """async () => {
            // Encontra o div scrollável real do chat
            const container = document.querySelector('main [class*="overflow-y-auto"]')
                            || document.querySelector('main')
                            || document.documentElement;

            // Vai ao topo para forçar carga de msgs antigas
            container.scrollTop = 0;
            await new Promise(r => setTimeout(r, 800));

            // Desce progressivamente para forçar renderização lazy
            const step = Math.ceil(container.scrollHeight / 6);
            for (let i = 0; i < 6; i++) {
                container.scrollTop += step;
                await new Promise(r => setTimeout(r, 400));
            }
            // Garante que chegou ao final
            container.scrollTop = container.scrollHeight;
            await new Promise(r => setTimeout(r, 800));
            return container.scrollHeight;
        }"""
        await page.evaluate(JS_SCROLL)
        await asyncio.sleep(1)

        # Scrape principal
        msgs = await scrape_full_chat(page)

        # Fallback 1: se veio vazio, tenta mais uma vez após scroll extra
        if not msgs:
            emit_log(q, "⚠️ Scrape vazio — tentando scroll extra...")
            await page.evaluate("document.documentElement.scrollTop = 999999")
            await asyncio.sleep(2)
            msgs = await scrape_full_chat(page)

        # Fallback 2: força modo print (remove overflow:hidden, display:none, etc.)
        if not msgs:
            emit_log(q, "⚠️ Scrape vazio — tentando modo print...")
            try:
                await page.emulate_media(media='print')
                await asyncio.sleep(1)
                msgs = await scrape_full_chat(page)
                # Restaura modo screen
                await page.emulate_media(media='screen')
            except Exception as e_print:
                emit_log(q, f"⚠️ Fallback print falhou: {e_print}")

        # Fallback 3: section[data-turn] (layout ChatGPT 2025 alternativo)
        if not msgs:
            emit_log(q, "⚠️ Scrape vazio — tentando section[data-turn]...")
            try:
                msgs = await page.evaluate("""() => {
                    // Helper: remove buttons mas preserva <img> e <a> que estejam dentro deles
                    function stripButtonsKeepMedia(html) {
                        return html.replace(/<button[^>]*>([\\s\\S]*?)<\\/button>/gi, function(match, inner) {
                            var imgs = inner.match(/<img[^>]*>/gi) || [];
                            var anchors = inner.match(/<a[^>]*>[\\s\\S]*?<\\/a>/gi) || [];
                            return imgs.concat(anchors).join('');
                        });
                    }
                    const sections = document.querySelectorAll('section[data-turn]');
                    if (!sections.length) return [];
                    return Array.from(sections).map(sec => {
                        const role = sec.getAttribute('data-turn') || 'user';
                        let contentEl;
                        if (role === 'assistant') {
                            contentEl = sec.querySelector('.markdown')
                                     || sec.querySelector('.prose')
                                     || sec.querySelector('[data-message-author-role="assistant"]');
                        } else {
                            contentEl = sec.querySelector('.whitespace-pre-wrap')
                                     || sec.querySelector('[data-message-author-role="user"]');
                        }
                        if (!contentEl) return null;
                        let html = contentEl.innerHTML || '';
                        html = stripButtonsKeepMedia(html);
                        return { role, content: html };
                    }).filter(m => m && m.content && m.content.trim().length > 0);
                }""")
                msgs = msgs or []
                if msgs:
                    emit_log(q, f"✅ section[data-turn] encontrou {len(msgs)} mensagens")
            except Exception as e_sec:
                emit_log(q, f"⚠️ Fallback section[data-turn] falhou: {e_sec}")

        emit_log(q, f"✅ Encontradas {len(msgs)} mensagens.")

        # Converte HTML → Markdown
        for m in msgs:
            clean = clean_html(m['content'])
            if m['role'] == 'assistant':
                m['content'] = md(clean, heading_style='ATX').strip()
            else:
                m['content'] = md(clean).strip()
            m['content'] = m['content'].replace('\u200b', '').replace('\xa0', ' ')
            m['content'] = m['content'].replace('\\_', '_').replace('\\*', '*')  # ✅ FIX — era omitido aqui

        # Varre TODOS os file cards do ChatGPT (em todas as mensagens) e injeta
        # o nome do arquivo + preview na mensagem CORRETA — cada card conhece seu
        # turn_index via data-message-id. Isso recupera imagens e downloads que
        # a conversão HTML→markdown (via markdownify) eventualmente descarta, já
        # que esses cards usam <div> dentro de <p> (HTML inválido) + botões sem
        # href nem <a>.
        try:
            cards_all = await _scan_file_cards(page)
            if cards_all:
                msg_idx_by_id = {}
                for i, m in enumerate(msgs):
                    mid = (m.get("message_id") or "").strip()
                    if mid:
                        msg_idx_by_id[mid] = i

                captured = list(getattr(page, "_captured_files", []) or [])
                cap_by_name = {}
                for cf in captured:
                    nm = (cf.get("name") or "").strip().lower()
                    if nm:
                        cap_by_name.setdefault(nm, cf)

                for card in cards_all:
                    name = (card.get("name") or "").strip()
                    if not name:
                        continue
                    turn_index = -1
                    card_msg_id = (card.get("message_id") or "").strip()
                    if card_msg_id and card_msg_id in msg_idx_by_id:
                        turn_index = msg_idx_by_id[card_msg_id]
                    if turn_index < 0:
                        turn_index = card.get("turn_index", -1)
                    if turn_index < 0 or turn_index >= len(msgs):
                        # Sem âncora de mensagem → anexa na última assistant
                        assistant_indices = [i for i, m in enumerate(msgs) if m.get('role') == 'assistant']
                        turn_index = assistant_indices[-1] if assistant_indices else -1
                    if turn_index < 0:
                        continue

                    target_content = msgs[turn_index].get('content') or ''
                    if name in target_content and ('/api/downloads/' in target_content or '![' in target_content):
                        # Já presente
                        continue

                    # Resolve URL de download via conversation API capture
                    matched = cap_by_name.get(name.lower())
                    download_url = ""
                    card_message_id = (card.get("message_id") or "").strip()
                    if matched:
                        download_url = (matched.get("url") or "").strip()
                        chatgpt_fid = (matched.get("file_id") or "").strip()
                        if not download_url and chatgpt_fid and chatgpt_fid.startswith("file-"):
                            try:
                                resolved = await page.evaluate("""async (fid) => {
                                    try {
                                        const r = await fetch('/backend-api/files/' + fid + '/download',
                                            { credentials: 'include' });
                                        if (!r.ok) return null;
                                        const d = await r.json();
                                        return d.download_url || '';
                                    } catch (e) { return null; }
                                }""", chatgpt_fid)
                                if resolved:
                                    download_url = resolved
                            except Exception:
                                pass
                    # Fallback determinístico para UI nova do interpreter:
                    # quando temos chat_id + message_id do card, monta URL canônica
                    # /interpreter/download usando sandbox_path padrão /mnt/data/<nome>.
                    if not download_url and chat_id and card_message_id:
                        try:
                            from urllib.parse import quote
                            sandbox_path = f"/mnt/data/{name}"
                            download_url = (
                                f"https://chatgpt.com/backend-api/conversation/{chat_id}"
                                f"/interpreter/download?message_id={quote(card_message_id)}&sandbox_path={quote(sandbox_path)}"
                            )
                        except Exception:
                            download_url = ""

                    append_parts = []

                    # Link de download (se resolvido)
                    if download_url:
                        safe = re.sub(r'[^\w.\-]', '_', name) or "file"
                        file_id = f"card_{int(time.time() * 1000)}_{safe}"
                        register_file(file_id, download_url, name)
                        append_parts.append(f"📎 Arquivo: [{name}](/api/downloads/{file_id})")
                        emit_log(q, f"📎 [SYNC] Card registrado: {name} (msg #{turn_index + 1})")
                    else:
                        append_parts.append(f"📎 Arquivo: **{name}**")
                        emit_log(q, f"⚠️ [SYNC] Card sem URL resolvida: {name} (msg #{turn_index + 1})")

                    # Preview (data: ou URL externa) — reinjeta como markdown image
                    preview = (card.get("preview") or "").strip()
                    if preview and preview not in target_content:
                        append_parts.append(f"![{name}]({preview})")

                    if append_parts:
                        msgs[turn_index]['content'] = (target_content + "\n\n" + "\n\n".join(append_parts)).strip()
        except Exception as e_cards:
            emit_log(q, f"⚠️ Falha ao varrer file cards no SYNC: {e_cards}")

        # Garante persistência de links de download também no fluxo de SYNC:
        # quando o ChatGPT renderiza "cards" de arquivo sem URL visível no markdown,
        # tentamos detectar/registrar os links na página e anexá-los na resposta da IA.
        # A detecção por DOM (Seletores 1-5) varre a página INTEIRA,
        # por isso basta rodar na última mensagem — os links encontrados são anexados a ela.
        # Para mensagens anteriores que mencionam arquivos, fazemos um segundo passo
        # para injetar os links registrados no local correto.
        try:
            for i, m in enumerate(msgs):
                if m.get('role') != 'assistant':
                    continue
                updated = await _detect_and_register_files(
                    page,
                    m.get('content') or '',
                    q,
                    allow_click_fallback=False,
                    scan_page_fallback=False
                )
                msgs[i]['content'] = updated
        except Exception as e_files:
            emit_log(q, f"⚠️ Falha ao detectar links de download durante SYNC: {e_files}")

        # Fallback extra 1: usa metadados de arquivos capturados via network listener
        # (_install_conversation_file_capture). Este é o caminho preferido para a UI
        # nova do ChatGPT, que não expõe URLs em <a> ou atributos HTML.
        try:
            captured_registered = await _register_captured_files(page, q)
            if captured_registered:
                msg_idx_by_id = {
                    (m.get("message_id") or "").strip(): i
                    for i, m in enumerate(msgs)
                    if (m.get("message_id") or "").strip()
                }
                assistant_indices = [i for i, m in enumerate(msgs) if m.get('role') == 'assistant']

                def _find_target_for_name(name: str) -> int:
                    n = (name or "").strip().lower()
                    if not n:
                        return assistant_indices[-1] if assistant_indices else -1
                    for idx in reversed(assistant_indices):
                        txt = (msgs[idx].get('content') or '').lower()
                        if n in txt:
                            return idx
                    return assistant_indices[-1] if assistant_indices else -1

                appended_count = 0
                for reg in captured_registered:
                    file_id_local = reg.get("file_id_local") or ""
                    name = reg.get("name") or "arquivo"
                    msg_id = (reg.get("message_id") or "").strip()

                    target_ai_idx = msg_idx_by_id.get(msg_id, -1) if msg_id else -1
                    if target_ai_idx < 0:
                        target_ai_idx = _find_target_for_name(name)
                    if target_ai_idx < 0:
                        continue

                    base = msgs[target_ai_idx].get('content') or ''
                    existing_ids = set(re.findall(r'/api/downloads/([A-Za-z0-9_\-\.]+)', base))
                    if file_id_local in existing_ids:
                        continue
                    link_line = f"📎 Arquivo: [{name}](/api/downloads/{file_id_local})"
                    if link_line in base:
                        continue

                    msgs[target_ai_idx]['content'] = (base + "\n\n" + link_line).strip()
                    appended_count += 1

                if appended_count:
                    emit_log(q, f"📎 [SYNC] {appended_count} link(s) de download (network) anexado(s) nas mensagens corretas")
        except Exception as e_cap:
            emit_log(q, f"⚠️ Falha ao anexar arquivos capturados via network: {e_cap}")

        # Fallback extra 2: alguns cards novos do ChatGPT não expõem URL no HTML/markdown
        # nem geram tráfego de rede até que o usuário clique. Tentamos clicar nos
        # elementos de download para disparar o evento Playwright "download".
        try:
            has_resolved_download_links = any(
                (m.get('role') == 'assistant') and (
                    '/api/downloads/' in (m.get('content') or '')
                )
                for m in msgs
            )
            has_unresolved_downloads = any(
                (m.get('role') == 'assistant') and ('URL indisponível' in (m.get('content') or ''))
                for m in msgs
            )
            if (has_unresolved_downloads or not has_resolved_download_links) and not _sync_auto_downloads:
                clicked = await _click_chatgpt_download_elements(page, q)
                if clicked:
                    await asyncio.sleep(2)
        except Exception as e_click_dl:
            emit_log(q, f"⚠️ Falha ao clicar cards de download no SYNC: {e_click_dl}")

        if _sync_download_tasks:
            await asyncio.gather(*_sync_download_tasks, return_exceptions=True)
        elif _sync_auto_downloads:
            await asyncio.sleep(1)
        if _sync_auto_downloads:
            assistant_indices = [i for i, m in enumerate(msgs) if m.get('role') == 'assistant']
            target_ai_idx = -1
            if assistant_indices:
                file_names = [str(dl.get("name") or "").strip().lower() for dl in _sync_auto_downloads]

                def _score_msg_for_download(idx: int) -> int:
                    txt = (msgs[idx].get('content') or '').lower()
                    if not txt:
                        return 0
                    score = 0
                    if '📎 arquivo:' in txt or '/api/downloads/' in txt:
                        score += 5
                    if 'arquivo:' in txt or 'planilha' in txt or 'download' in txt:
                        score += 2
                    for nm in file_names:
                        if nm and nm in txt:
                            score += 10
                    return score

                scored = [(idx, _score_msg_for_download(idx)) for idx in assistant_indices]
                scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
                if scored and scored[0][1] > 0:
                    target_ai_idx = scored[0][0]
                else:
                    target_ai_idx = assistant_indices[-1]  # fallback: última resposta da IA

            if target_ai_idx >= 0:
                extra_links = []
                for dl in _sync_auto_downloads:
                    register_file(
                        dl["file_id"],
                        dl["url"],
                        dl["name"],
                        payload_b64=dl.get("payload_b64"),
                        content_type=dl.get("content_type")
                    )
                    extra_links.append(f"📎 Arquivo: [{dl['name']}](/api/downloads/{dl['file_id']})")
                if extra_links:
                    base = msgs[target_ai_idx].get('content') or ''
                    existing = set(
                        re.findall(r'/api/downloads/([A-Za-z0-9_\-]+)', base)
                    )
                    novos = [
                        link for link, dl in zip(extra_links, _sync_auto_downloads)
                        if dl.get("file_id") not in existing
                    ]
                    if novos:
                        msgs[target_ai_idx]['content'] = (base + "\n\n" + "\n".join(novos)).strip()
                    emit_log(
                        q,
                        f"📎 [SYNC] {len(novos)} link(s) de download anexado(s) à msg assistant #{target_ai_idx + 1}"
                    )

        # Higieniza placeholders de "URL indisponível" quando já existe link resolvido
        # para o mesmo arquivo na mesma mensagem.
        try:
            for i, m in enumerate(msgs):
                if m.get('role') != 'assistant':
                    continue
                base = m.get('content') or ''
                linked_names = set(re.findall(r'📎\s*Arquivo:\s*\[([^\]]+)\]\(/api/downloads/', base))
                if not linked_names:
                    continue
                cleaned = base
                for nm in linked_names:
                    esc = re.escape(nm.strip())
                    cleaned = re.sub(
                        rf'\n*📎\s*Arquivo:\s*\*\*{esc}\*\*\s*\(URL indisponível[^\n]*\)',
                        '',
                        cleaned,
                        flags=re.IGNORECASE
                    )
                    cleaned = re.sub(
                        rf'\n*📎\s*Arquivo:\s*{esc}\s*\(URL indisponível[^\n]*\)',
                        '',
                        cleaned,
                        flags=re.IGNORECASE
                    )
                if cleaned != base:
                    msgs[i]['content'] = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        except Exception as e_cleanup:
            emit_log(q, f"⚠️ Falha ao limpar placeholders de URL indisponível: {e_cleanup}")

        title = await get_chat_title(page)
        emit_event(q, 'syncresult', {
            'success': True, 'messages': msgs, 'title': title, 'chat_id': chat_id
        })

    except Exception as e:
        emit_log(q, f"Erro Sync: {e}")
        emit_event(q, 'syncresult', {'success': False, 'error': str(e)})
    finally:
        await page.close()
        if q:
            q.put(None)




async def watchdog_page(page, q, stop_event: asyncio.Event,
                        check_interval: int = 15,
                        activity_ts: list = None):
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(asyncio.sleep(check_interval), timeout=check_interval + 1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            return
        if stop_event.is_set():
            return
        if activity_ts and (time.time() - activity_ts[0]) < check_interval:
            continue
        try:
            await asyncio.wait_for(page.evaluate("1"), timeout=20.0)
        except Exception as e:
            emit_event(q, "error", f"⏱️ Watchdog: aba não respondeu ({e}). Abortando.")
            stop_event.set()
            return



def _parse_google_raw_html(raw_html: str, query: str = "") -> list:
    """
    Fallback bruto: extrai resultados do Google via regex no HTML cru.
    Não depende de classes CSS — procura padrões estruturais que o Google
    usa independentemente do tema/layout:
      - <h3...>TÍTULO</h3> dentro de <a href="https://...">
      - Snippet na div.VwiC3b mais próxima após o h3
    Retorna lista no mesmo formato das estratégias JS.
    """
    from html.parser import HTMLParser
    import html as html_mod

    def _strip_tags(s):
        """Remove tags HTML e normaliza espaços."""
        clean = re.sub(r'<[^>]+>', ' ', s)
        clean = html_mod.unescape(clean).strip()
        return re.sub(r'\s+', ' ', clean)

    def _is_site_name(text):
        """Detecta se o texto é nome de site/domínio em vez de snippet real."""
        low = text.lower()
        if len(text) < 60 and any(x in low for x in ['.gov', '.com', '.org', '.edu', 'institutes of health', 'wikipedia']):
            return True
        if text.count('.') > 2 and len(text) < 80:
            return True
        return False

    items = []
    seen = set()

    # Padrão: <a href="URL">...<h3>TÍTULO</h3>
    pattern_h3 = re.compile(
        r'<a[^>]+href="(https?://(?!(?:www\.)?google\.com/(?:search|url|imgres|maps))[^"]+)"[^>]*>'
        r'[^<]*<h3[^>]*>([^<]+)</h3>',
        re.IGNORECASE | re.DOTALL
    )

    for m in pattern_h3.finditer(raw_html):
        url   = html_mod.unescape(m.group(1)).strip()
        title = html_mod.unescape(m.group(2)).strip()

        if not title or len(title) < 5:
            continue
        if url in seen:
            continue
        seen.add(url)

        # Procura snippet na janela após o h3 (5000 chars cobre bem o gap)
        after_h3 = raw_html[m.end():m.end() + 5000]
        snippet = ""

        # Tenta VwiC3b (classe padrão de snippet do Google)
        snip_match = re.search(
            r'class="VwiC3b[^"]*"[^>]*>(.*?)</div>',
            after_h3,
            re.DOTALL | re.IGNORECASE
        )
        if snip_match:
            candidate = _strip_tags(snip_match.group(1))
            if len(candidate) > 30 and not _is_site_name(candidate):
                snippet = candidate[:300]

        # Fallback: data-sncf="1" container
        if not snippet:
            snip_match2 = re.search(
                r'data-sncf="1"[^>]*>(.*?)</div>',
                after_h3,
                re.DOTALL | re.IGNORECASE
            )
            if snip_match2:
                candidate = _strip_tags(snip_match2.group(1))
                if len(candidate) > 30 and not _is_site_name(candidate):
                    snippet = candidate[:300]

        # Fallback: texto longo em <span> após o h3
        if not snippet:
            snip_spans = re.findall(
                r'<(?:span|em)[^>]*>([^<]{40,500})</(?:span|em)>',
                after_h3[:3000],
                re.IGNORECASE
            )
            for s in snip_spans:
                candidate = html_mod.unescape(s).strip()
                if len(candidate) > 40 and not _is_site_name(candidate) and 'Traduzir' not in candidate:
                    snippet = candidate[:300]
                    break

        items.append({
            "position": len(items) + 1,
            "title":    title,
            "url":      url,
            "snippet":  snippet,
            "type":     "organic"
        })

        if len(items) >= 10:
            break

    return items


def _parse_uptodate_raw_html(raw_html: str, query: str = "") -> list:
    """
    Fallback bruto para a busca do UpToDate quando os seletores JS não
    retornarem itens. Extrai os cards principais da lista de resultados.
    """
    import html as html_mod

    def _clean(text):
        text = re.sub(r'<[^>]+>', ' ', text or '')
        text = html_mod.unescape(text).strip()
        return re.sub(r'\s+', ' ', text)

    items = []
    seen = set()
    pattern = re.compile(
        r'<li[^>]+class="[^"]*search-result-list-item[^"]*"[^>]*>.*?'
        r'<a[^>]+href="(?P<href>/[^"#?][^"]*)"[^>]+class="[^"]*searchResultLink[^"]*"[^>]*>'
        r'(?P<title>.*?)</a>'
        r'(?P<tail>.*?)'
        r'</li>',
        re.IGNORECASE | re.DOTALL
    )

    for match in pattern.finditer(raw_html):
        href = html_mod.unescape(match.group('href') or '').strip()
        title = _clean(match.group('title'))
        tail = match.group('tail') or ''
        if not href or not title:
            continue

        url = href if href.startswith('http') else f'https://www.uptodate.com{href}'
        if url in seen:
            continue
        seen.add(url)

        snippet_match = re.search(
            r'<div[^>]+class="[^"]*snippet[^"]*"[^>]*>(.*?)</div>',
            tail,
            re.IGNORECASE | re.DOTALL
        )
        snippet = _clean(snippet_match.group(1))[:400] if snippet_match else ''

        subhits = []
        for sm in re.finditer(
            r'<a[^>]+class="[^"]*search-result-subhit-link[^"]*"[^>]*>(.*?)</a>',
            tail,
            re.IGNORECASE | re.DOTALL
        ):
            subhit = _clean(sm.group(1))
            if subhit:
                subhits.append(subhit)

        li_tag = match.group(0).split('>', 1)[0]
        class_match = re.search(r'class="([^"]+)"', li_tag, re.IGNORECASE)
        class_name = class_match.group(1) if class_match else ''
        item_type = 'topic'
        if 'ICG' in class_name:
            item_type = 'pathway'
        elif 'LAB' in class_name:
            item_type = 'lab'
        elif 'medical' in class_name:
            item_type = 'medical'

        items.append({
            "position": len(items) + 1,
            "title": title,
            "url": url,
            "snippet": snippet,
            "type": item_type,
            "subhits": subhits[:6],
            "query": query,
        })

        if len(items) >= 12:
            break

    return items


async def handle_search_task(context, task):
    """
    Ação SEARCH — abre Google, digita a query com typing realista,
    scrapa os resultados orgânicos e retorna JSON estruturado.

    Emite:
      • log      — progresso
      • status   — etapas ("Pesquisando...", "Aguardando resultados...")
      • searchresult — resultado final (success, query, results[])
    """
    async with tab_semaphore:
        sender_token = None
        sender = _extract_task_sender(task)
        if sender:
            sender_token = _CURRENT_TASK_SENDER.set(sender)
        q    = task.get('stream_queue')
        query = (task.get('query') or '').strip()
        baseline_pages = list(getattr(context, "pages", []) or [])
        page = None
        try:
            if not query:
                emit_event(q, 'searchresult', {
                    'success': False, 'query': '', 'error': 'Query vazia'
                })
                return

            page = await context.new_page()
            emit_log(q, f"🔍 Pesquisando no Google: {query}")

            # ── 1. Abre o Google ──────────────────────────────────────
            await page.goto('https://www.google.com', wait_until='domcontentloaded')
            await asyncio.sleep(random.uniform(0.8, 1.5))

            # ── 2. Aceita cookies/consent se aparecer ─────────────────
            try:
                consent_selectors = [
                    'button#L2AGLb',                            # "Aceitar tudo" (PT/EN)
                    'button:has-text("Aceitar tudo")',
                    'button:has-text("Accept all")',
                    'button:has-text("Rejeitar tudo")',         # fallback: rejeitar
                    'button:has-text("Reject all")',
                ]
                for sel in consent_selectors:
                    btn = page.locator(sel).first
                    try:
                        if await btn.is_visible(timeout=1500):
                            await btn.click()
                            await asyncio.sleep(0.5)
                            break
                    except:
                        continue
            except:
                pass

            # ── 3. Foca no campo de busca e digita ────────────────────
            search_input = page.locator('textarea[name="q"], input[name="q"]').first
            try:
                await search_input.wait_for(state='visible', timeout=5000)
            except:
                # Fallback: tenta clicar no body e usar Tab
                await page.click('body')
                await asyncio.sleep(0.3)

            await search_input.click()
            await asyncio.sleep(random.uniform(0.2, 0.5))

            emit_event(q, 'status', f'Digitando busca...')
            await type_realistic(page, query, q)
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # ── 4. Pressiona Enter ────────────────────────────────────
            await page.keyboard.press('Enter')
            emit_event(q, 'status', 'Aguardando resultados do Google...')

            # ── 5. Aguarda resultados carregarem ──────────────────────
            try:
                await page.wait_for_selector('#search, #rso, #botstuff', timeout=15000)
            except:
                # Pode ser CAPTCHA ou página lenta
                await asyncio.sleep(3)

            await asyncio.sleep(random.uniform(1.0, 2.0))

            # ── 6. Scrapa resultados orgânicos ────────────────────────
            # Estratégia 1: N54PNb (layout Google 2025)
            # Estratégia 2: h3 walk-up (fallback CSS)
            # Estratégia 3: raw HTML regex (fallback bruto)
            results = await page.evaluate("""() => {
                const items = [];
                const seen = new Set();

                // Featured snippet
                const feat = document.querySelector('.hgKElc, .IZ6rdc, [data-attrid="wa:/description"], .kno-rdesc span, .LGOjhe');
                if (feat && feat.innerText.trim().length > 20) {
                    items.push({
                        position: 0,
                        title: '★ Resposta em destaque',
                        url: '',
                        snippet: feat.innerText.trim().substring(0, 500),
                        type: 'featured_snippet'
                    });
                }

                // ── Estratégia 1: N54PNb (container Google 2025) ──
                let pos = 1;
                document.querySelectorAll('.N54PNb').forEach(container => {
                    if (pos > 10) return;
                    const h3 = container.querySelector('h3');
                    if (!h3) return;
                    const title = h3.innerText.trim();
                    if (!title || title.length < 3) return;

                    const linkEl = container.querySelector('a.zReHs[href], a[href^="http"]');
                    if (!linkEl) return;
                    const url = linkEl.href || '';
                    if (!url || url.includes('google.com/search')) return;
                    if (seen.has(url)) return;
                    seen.add(url);

                    let snippet = '';
                    const snipEl = container.querySelector('.VwiC3b');
                    if (snipEl) snippet = snipEl.innerText.trim().substring(0, 300);

                    items.push({ position: pos++, title, url, snippet, type: 'organic' });
                });

                // ── Estratégia 2 (fallback CSS): h3 walk-up ──
                if (items.filter(i => i.type === 'organic').length === 0) {
                    pos = 1;
                    const area = document.querySelector('#rso, #search');
                    if (area) {
                        area.querySelectorAll('h3').forEach(h3 => {
                            if (pos > 10) return;
                            const title = h3.innerText.trim();
                            if (!title || title.length < 3) return;

                            const linkEl = h3.closest('a[href^="http"]');
                            if (!linkEl) return;
                            const url = linkEl.href || '';
                            if (!url || url.includes('google.com/search')) return;
                            if (seen.has(url)) return;
                            seen.add(url);

                            let snippet = '';
                            let walker = h3;
                            for (let i = 0; i < 8 && walker; i++) {
                                walker = walker.parentElement;
                                if (!walker) break;
                                const s = walker.querySelector('.VwiC3b');
                                if (s) { snippet = s.innerText.trim().substring(0, 300); break; }
                            }

                            items.push({ position: pos++, title, url, snippet, type: 'organic' });
                        });
                    }
                }

                // People Also Ask
                const paa = document.querySelectorAll(
                    '[jsname="Cpkphb"] [data-q], [data-sgrd] [role="heading"], .related-question-pair'
                );
                if (paa.length > 0) {
                    const qs = [];
                    paa.forEach((el, i) => { if (i < 4) qs.push(el.getAttribute('data-q') || el.innerText.trim()); });
                    if (qs.length) items.push({ position: 99, title: 'Perguntas relacionadas', url: '', snippet: qs.join(' | '), type: 'people_also_ask' });
                }

                return items;
            }""")

            # ── Estratégia 3 (fallback bruto): raw HTML com regex ──
            if not results or all(r.get('type') != 'organic' for r in results):
                emit_log(q, "⚠️ Seletores CSS não encontraram resultados — tentando fallback via raw HTML...")
                try:
                    raw_html = await page.content()
                    results = _parse_google_raw_html(raw_html, query)
                    if results:
                        emit_log(q, f"✅ Fallback raw HTML: {len(results)} resultados extraídos")
                except Exception as e_raw:
                    emit_log(q, f"⚠️ Fallback raw HTML falhou: {e_raw}")

            emit_log(q, f"✅ {len(results)} resultados encontrados para: {query}")
            emit_event(q, 'searchresult', {
                'success': True,
                'query':   query,
                'results': results,
                'count':   len(results)
            })

        except Exception as e:
            emit_log(q, f"❌ Erro na busca Google: {e}")
            emit_event(q, 'searchresult', {
                'success': False,
                'query':   query,
                'error':   str(e)
            })
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass
            await close_ephemeral_pages(context, baseline_pages, q=q)
            if q:
                q.put(None)
            if sender_token is not None:
                _CURRENT_TASK_SENDER.reset(sender_token)


async def handle_uptodate_search_task(context, task):
    """
    Ação UPTODATE_SEARCH — abre a busca do UpToDate, pesquisa o termo e
    retorna os resultados estruturados encontrados na listagem principal.
    """
    async with tab_semaphore:
        sender_token = None
        sender = _extract_task_sender(task)
        if sender:
            sender_token = _CURRENT_TASK_SENDER.set(sender)
        q = task.get('stream_queue')
        query = (task.get('query') or '').strip()
        baseline_pages = list(getattr(context, "pages", []) or [])
        page = None
        try:
            if not query:
                emit_event(q, 'searchresult', {
                    'success': False, 'query': '', 'error': 'Query vazia'
                })
                return

            page = await context.new_page()
            emit_log(q, f"🩺 Pesquisando no UpToDate: {query}")

            await page.goto('https://www.uptodate.com/contents/search', wait_until='domcontentloaded')
            await asyncio.sleep(random.uniform(1.0, 1.8))

            search_input = page.locator('#tbSearch, input.searchTerm, input[type="search"]').first
            await search_input.wait_for(state='visible', timeout=15000)
            await search_input.click()
            await asyncio.sleep(random.uniform(0.2, 0.5))

            try:
                await page.locator('#clearSearch').click(timeout=1000)
                await asyncio.sleep(0.2)
            except Exception:
                pass

            emit_event(q, 'status', 'Digitando busca no UpToDate...')
            await type_realistic(page, query, q)
            await asyncio.sleep(random.uniform(0.3, 0.8))

            submitted = False
            submit_selectors = [
                '.newsearch-submit',
                'span.newsearch-submit',
                '[aria-label="Submit search"]',
            ]
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                await page.keyboard.press('Enter')

            emit_event(q, 'status', 'Aguardando resultados do UpToDate...')
            try:
                await page.wait_for_selector(
                    '#searchresults, #search-results-container, .search-result-list-item',
                    timeout=20000
                )
            except Exception:
                await asyncio.sleep(4)

            await asyncio.sleep(random.uniform(1.0, 2.0))

            results = await page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('#search-results-container .search-result-list-item'));
                const items = [];
                const seen = new Set();
                let pos = 1;

                const detectType = (li) => {
                    const cls = li.className || '';
                    if (cls.includes('ICG')) return 'pathway';
                    if (cls.includes('LAB')) return 'lab';
                    if (cls.includes('medical')) return 'medical';
                    return 'topic';
                };

                for (const li of nodes) {
                    if (pos > 12) break;
                    const link = li.querySelector('a.searchResultLink[href]');
                    if (!link) continue;

                    const title = (link.innerText || '').trim();
                    const href = link.getAttribute('href') || '';
                    if (!title || !href) continue;

                    const url = new URL(href, window.location.origin).href;
                    if (seen.has(url)) continue;
                    seen.add(url);

                    const snippetEl = li.querySelector('.snippet');
                    const snippet = snippetEl ? (snippetEl.innerText || '').trim().substring(0, 400) : '';
                    const subhits = Array.from(li.querySelectorAll('.search-result-subhit-link'))
                        .map(el => (el.innerText || '').trim())
                        .filter(Boolean)
                        .slice(0, 6);

                    items.push({
                        position: pos++,
                        title,
                        url,
                        snippet,
                        type: detectType(li),
                        subhits,
                    });
                }

                return items;
            }""")

            raw_html = ""
            if not results:
                emit_log(q, "⚠️ Seletores CSS não encontraram resultados no UpToDate — tentando fallback via raw HTML...")
                try:
                    raw_html = await page.content()
                    results = _parse_uptodate_raw_html(raw_html, query)
                    if results:
                        emit_log(q, f"✅ Fallback raw HTML UpToDate: {len(results)} resultados extraídos")
                except Exception as e_raw:
                    emit_log(q, f"⚠️ Fallback raw HTML UpToDate falhou: {e_raw}")

            emit_log(q, f"✅ {len(results)} resultado(s) UpToDate encontrados para: {query}")
            emit_event(q, 'searchresult', {
                'success': True,
                'query': query,
                'results': results,
                'count': len(results),
                'source': 'uptodate',
                'raw_html': raw_html[:30000] if raw_html else '',
            })

        except Exception as e:
            emit_log(q, f"❌ Erro na busca UpToDate: {e}")
            emit_event(q, 'searchresult', {
                'success': False,
                'query': query,
                'error': str(e),
                'source': 'uptodate',
            })
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            await close_ephemeral_pages(context, baseline_pages, q=q)
            if q:
                q.put(None)
            if sender_token is not None:
                _CURRENT_TASK_SENDER.reset(sender_token)


async def handle_chat_task(context, task):
    async with tab_semaphore:
        sender_token = None
        sender = _extract_task_sender(task)
        if sender:
            sender_token = _CURRENT_TASK_SENDER.set(sender)
        q          = task.get('stream_queue')
        sender     = _extract_task_sender(task)
        sender_token = _CURRENT_TASK_SENDER.set(sender)
        stop_event = asyncio.Event()
        activityts = [time.time()]
        page = None
        try:
            emit_log(q, "Iniciando tarefa CHAT no browser.")
            page = await context.new_page()
            watchdog_task = asyncio.create_task(
                watchdog_page(page, q, stop_event,
                              check_interval=15,
                              activity_ts=activityts)  # ✅ era activityts=, corrigido para activity_ts=
            )
            try:
                await asyncio.wait_for(
                    handle_chat_task_inner(task, page, q, stop_event, activityts),
                    timeout=660
                )
            except asyncio.TimeoutError:
                emit_event(q, 'error', 'Timeout externo 660s — tarefa abortada.')
            finally:
                stop_event.set()
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception as e:
            emit_log(q, f'ERRO Chat: {e}')
            emit_event(q, 'error', f'Falha no navegador: {str(e)}')
        finally:
            emit_log(q, 'Finalizando tarefa.')
            if page:
                try:
                    await page.close()
                except:
                    pass
            if q:
                q.put(None)
            _CURRENT_TASK_SENDER.reset(sender_token)


def _is_codex_url(url: str) -> bool:
    """Detecta se uma URL aponta para o Codex (chatgpt.com/codex...)."""
    if not url:
        return False
    u = str(url).lower()
    return ("chatgpt.com/codex" in u)


def _build_locator_from_chatgpt(raw: str) -> str:
    """Extrai um seletor CSS simples de uma resposta do ChatGPT."""
    txt = (raw or "").strip()
    if not txt:
        return ""
    try:
        data = json.loads(txt)
        if isinstance(data, dict):
            sel = str(data.get("locator") or data.get("selector") or "").strip()
            if sel:
                return sel
            cands = data.get("selectors") or []
            if isinstance(cands, list):
                for item in cands:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
    except Exception:
        pass

    m = re.search(r'([#.\[\]:="\w\- >]+)', txt)
    return (m.group(1).strip() if m else "")


async def _sugerir_locator_placeholder_via_chatgpt(page, q, contexto: str) -> str:
    """
    Captura HTML atual e pergunta ao ChatGPT qual seletor deve ser usado quando
    placeholder vital não é localizado.
    """
    try:
        raw_html = await page.content()
    except Exception as e_html:
        emit_log(q, f"⚠️ Codex auto-heal: falha ao capturar HTML para diagnóstico: {e_html}")
        return ""

    html_amostra = (raw_html or "")[:60000]
    if not html_amostra.strip():
        emit_log(q, "⚠️ Codex auto-heal: HTML vazio; não foi possível pedir sugestão de locator.")
        return ""

    llm_url = getattr(config, "ANALISADOR_LLM_URL", "http://127.0.0.1:3003/v1/chat/completions")
    model = getattr(config, "ANALISADOR_LLM_MODEL", "ChatGPT Simulator")
    api_key = getattr(config, "API_KEY", "")
    prompt = (
        "Você é especialista em Playwright. Receberá um HTML e deve sugerir UM locator CSS robusto "
        "para o elemento vital descrito. Responda SOMENTE em JSON válido: "
        '{"locator":"<css-selector>","justificativa":"curta"}. '
        f"Contexto do elemento vital: {contexto}.\n\nHTML:\n{html_amostra}"
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "stream": False,
    }

    try:
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        content = (
            (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        )
        locator = _build_locator_from_chatgpt(content)
        if locator:
            emit_log(q, f"🛠️ Codex auto-heal: locator sugerido pelo ChatGPT: {locator}")
        else:
            emit_log(q, "⚠️ Codex auto-heal: ChatGPT não retornou locator utilizável.")
        return locator
    except Exception as e:
        emit_log(q, f"⚠️ Codex auto-heal: erro ao consultar ChatGPT para locator: {e}")
        return ""


async def _codex_wait_for_composer(page, q, timeout_ms: int = 20000):
    """Aguarda o composer do Codex aparecer. Retorna o handle do elemento."""
    # O composer Codex é um textarea/contenteditable cujo placeholder começa
    # com 'Faça uma pergunta' (pt-BR) ou 'Ask' / 'Describe' (en-US). Fazemos
    # busca por JS para aceitar múltiplas formas.
    js = """() => {
        const candidates = Array.from(document.querySelectorAll(
            'textarea, [contenteditable="true"], [contenteditable=""]'
        ));
        const hit = candidates.find(el => {
            const ph = (el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '').toLowerCase();
            const txt = (el.innerText || el.textContent || '').toLowerCase();
            // O placeholder do Codex contém "/plan" no final
            if (ph.includes('/plan')) return true;
            if (ph.startsWith('fa\\u00e7a uma pergunta')) return true;
            if (ph.startsWith('ask ')) return true;
            if (ph.startsWith('describe ')) return true;
            return false;
        });
        if (!hit) return null;
        // Marca elemento para querySelector posterior
        hit.setAttribute('data-autodev-codex-composer', '1');
        return true;
    }"""
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            ok = await page.evaluate(js)
            if ok:
                return '[data-autodev-codex-composer="1"]'
        except Exception:
            pass
        await asyncio.sleep(0.4)

    emit_log(q, "⚠️ Placeholder do composer Codex não detectado; iniciando auto-heal via HTML + ChatGPT...")
    locator_sugerido = await _sugerir_locator_placeholder_via_chatgpt(
        page,
        q,
        contexto="composer do Codex (textarea/contenteditable para enviar tarefa)"
    )
    if locator_sugerido:
        try:
            ok = await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.setAttribute('data-autodev-codex-composer', '1');
                    return true;
                }""",
                locator_sugerido,
            )
            if ok:
                emit_log(q, "✅ Codex auto-heal: composer localizado com seletor sugerido e script ajustado automaticamente.")
                return '[data-autodev-codex-composer="1"]'
        except Exception as e:
            emit_log(q, f"⚠️ Codex auto-heal: seletor sugerido falhou na validação: {e}")

    raise RuntimeError('Composer do Codex não encontrado (placeholder /plan ausente, inclusive após auto-heal)')


async def _codex_select_repo(page, q, repo: str) -> bool:
    """Abre o dropdown de ambientes/repositórios do Codex e seleciona `repo`.

    Retorna True se a seleção foi confirmada (checkmark apareceu ao lado do item).
    """
    if not repo:
        return False
    # 1) Encontra e clica no botão de seleção (o que exibe o repo atual). O
    # texto pode estar truncado (ex.: "hellyssoncavalcanti/ch..."), então
    # procuramos qualquer botão visível cujo texto contenha '/' e esteja
    # próximo ao composer.
    open_js = """(repo) => {
        const [owner, name] = repo.split('/');
        const ownerPrefix = owner ? owner.slice(0, 6).toLowerCase() : '';
        const buttons = Array.from(document.querySelectorAll('button'));
        // Heurística: botão que já exibe um "owner/..." (pode estar truncado)
        const hit = buttons.find(b => {
            if (b.offsetParent === null) return false; // invisível
            const t = (b.innerText || '').trim().toLowerCase();
            if (!t) return false;
            if (t.includes('/') && (t.includes(ownerPrefix) || t.includes(owner.toLowerCase()))) return true;
            // Botão que mostra só "Selecionar ambiente" / "Choose environment"
            if (t.includes('selecion') && t.includes('ambiente')) return true;
            if (t.includes('choose') && (t.includes('environment') || t.includes('repo'))) return true;
            return false;
        });
        if (!hit) return false;
        hit.scrollIntoView({block: 'center'});
        hit.click();
        return true;
    }"""
    opened = False
    for _try in range(3):
        try:
            opened = await page.evaluate(open_js, repo)
        except Exception:
            opened = False
        if opened:
            break
        await asyncio.sleep(0.5)
    if not opened:
        emit_log(q, f'⚠️ Codex: botão de ambiente/repo não localizado para "{repo}".')
        return False
    await asyncio.sleep(0.6)

    # 2) Digita o nome do repo no campo de busca do dropdown.
    search_js = """(repo) => {
        const inputs = Array.from(document.querySelectorAll(
            'input[placeholder], input[type="search"], input[type="text"]'
        ));
        const hit = inputs.find(el => {
            if (el.offsetParent === null) return false;
            const ph = (el.getAttribute('placeholder') || '').toLowerCase();
            return ph.includes('ambient') || ph.includes('repo') || ph.includes('search') || ph.includes('buscar');
        });
        if (!hit) return false;
        hit.focus();
        hit.value = '';
        hit.dispatchEvent(new Event('input', {bubbles: true}));
        // Type per-char simulation via InputEvent for React controlled inputs
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(hit, repo);
        hit.dispatchEvent(new Event('input', {bubbles: true}));
        return true;
    }"""
    try:
        await page.evaluate(search_js, repo)
    except Exception:
        pass
    await asyncio.sleep(0.6)

    # 3) Clica na opção cujo texto bate com `repo` (ou começa com ele).
    pick_js = """(repo) => {
        const target = repo.toLowerCase();
        const targetShort = target.split('/').slice(-1)[0];
        // Candidatos: qualquer elemento clicável listado no dropdown aberto.
        const nodes = Array.from(document.querySelectorAll(
            '[role="option"], [role="menuitem"], li, button, a, div'
        ));
        const visible = nodes.filter(n => {
            if (!n.offsetParent) return false;
            const t = (n.innerText || n.textContent || '').trim().toLowerCase();
            return t === target || t.startsWith(target) || t.includes('/' + targetShort);
        });
        // Pega o elemento mais específico (menor texto) para evitar pegar
        // um container que contenha vários repos.
        visible.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
        const hit = visible[0];
        if (!hit) return false;
        hit.scrollIntoView({block: 'center'});
        hit.click();
        return true;
    }"""
    picked = False
    for _try in range(4):
        try:
            picked = await page.evaluate(pick_js, repo)
        except Exception:
            picked = False
        if picked:
            break
        await asyncio.sleep(0.4)
    if not picked:
        emit_log(q, f'⚠️ Codex: opção "{repo}" não localizada no dropdown.')
        # Fecha o dropdown clicando fora
        try:
            await page.keyboard.press('Escape')
        except Exception:
            pass
        return False
    emit_log(q, f'✅ Codex: ambiente/repo selecionado: {repo}')
    await asyncio.sleep(0.5)
    return True


async def _codex_paste_message(page, composer_selector: str, message: str, q, activityts):
    """Cola a mensagem no composer do Codex via clipboard.

    Reutiliza os mesmos marcadores [INICIO_TEXTO_COLADO]...[FIM_TEXTO_COLADO]
    enviados pelo agente (apenas descarta os marcadores antes de colar).
    """
    start_marker = "[INICIO_TEXTO_COLADO]"
    end_marker = "[FIM_TEXTO_COLADO]"
    text = message or ""
    if start_marker in text and end_marker in text:
        i = text.find(start_marker) + len(start_marker)
        j = text.rfind(end_marker)
        text = text[i:j]
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # Clica no composer para focar.
    try:
        await page.click(composer_selector, timeout=5000)
    except Exception:
        # Fallback via JS
        await page.evaluate(
            "(sel) => { const el = document.querySelector(sel); if (el) el.focus(); }",
            composer_selector,
        )
    await asyncio.sleep(0.2)

    paste_chunk_size = 3500
    chunks = [text[i:i + paste_chunk_size] for i in range(0, len(text), paste_chunk_size)] or ['']
    total = len(text)
    emit_log(q, f'Codex: colando bloco ({total} chars) em {len(chunks)} parte(s)...')
    for idx, chunk in enumerate(chunks, 1):
        emit_log(q, f'Codex: colando parte {idx}/{len(chunks)}: {len(chunk)} chars via clipboard...')
        if activityts:
            activityts[0] = time.time()
        try:
            await page.evaluate("(t) => navigator.clipboard.writeText(t)", chunk)
        except Exception as clip_err:
            emit_log(q, f'Codex: clipboard.writeText falhou ({clip_err}); usando execCommand fallback.')
            await page.evaluate(
                """(args) => {
                    const el = document.querySelector(args.sel);
                    if (!el) throw new Error('composer codex ausente');
                    el.focus();
                    if (el.isContentEditable) {
                        document.execCommand('insertText', false, args.text);
                    } else {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(el, (el.value || '') + args.text);
                        el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: args.text}));
                    }
                }""",
                {"sel": composer_selector, "text": chunk},
            )
            await asyncio.sleep(0.1)
            continue
        await asyncio.sleep(0.1)
        # Foca e Ctrl+V
        await page.evaluate(
            "(sel) => { const el = document.querySelector(sel); if (el) el.focus(); }",
            composer_selector,
        )
        await page.keyboard.press('Control+V')
        await asyncio.sleep(0.2)


async def _codex_submit(page, q) -> bool:
    """Clica no botão de envio do Codex (seta para cima / Enviar)."""
    submit_js = """() => {
        const buttons = Array.from(document.querySelectorAll('button'));
        // Procura botão de submit (aria-label com Send/Enviar, ou tipo submit, próximo ao composer)
        const composer = document.querySelector('[data-autodev-codex-composer="1"]');
        let closestForm = null;
        if (composer) {
            closestForm = composer.closest('form') || composer.parentElement?.closest('[class*="composer" i]');
        }
        const scoped = closestForm ? Array.from(closestForm.querySelectorAll('button')) : buttons;
        const hit = scoped.find(b => {
            if (b.disabled) return false;
            if (b.offsetParent === null) return false;
            const al = (b.getAttribute('aria-label') || '').toLowerCase();
            const tt = (b.getAttribute('title') || '').toLowerCase();
            if (al.includes('send') || al.includes('enviar') || al.includes('submit')) return true;
            if (tt.includes('send') || tt.includes('enviar')) return true;
            // Botão com ícone de seta-para-cima: muitas vezes tem data-testid=send-button
            const dt = (b.getAttribute('data-testid') || '').toLowerCase();
            if (dt.includes('send')) return true;
            return false;
        }) || scoped.find(b => b.type === 'submit' && !b.disabled);
        if (!hit) return false;
        hit.scrollIntoView({block: 'center'});
        hit.click();
        return true;
    }"""
    for _try in range(5):
        try:
            ok = await page.evaluate(submit_js)
        except Exception:
            ok = False
        if ok:
            return True
        # Fallback: Ctrl+Enter no composer
        try:
            await page.keyboard.press('Control+Enter')
            return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


async def _codex_wait_and_click_pr_controls(page, q, timeout_s: int = 900) -> tuple[bool, str]:
    """Mantém a aba do Codex aberta até surgir 'Criar PR'/'Atualizar Branch' e clica.

    Retorna (clicked, label) com label em minúsculas quando clicado.
    """
    emit_log(q, "Codex: aguardando botões finais ('Criar PR'/'Atualizar Branch')...")
    selector_js = """() => {
        const alvos = ['criar pr', 'atualizar branch'];
        const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (!style) return false;
            if (style.visibility === 'hidden' || style.display === 'none') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };
        const candidates = Array.from(document.querySelectorAll('button, a, div[role="button"], span, summary'));
        for (const el of candidates) {
            if (!isVisible(el) || el.disabled) continue;
            const txt = ((el.innerText || el.textContent || '') + '').trim().toLowerCase().replace(/\\s+/g, ' ');
            if (!txt) continue;
            const alvo = alvos.find(a => txt === a || txt.includes(a));
            if (!alvo) continue;
            try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
            try { el.click(); return {clicked: true, label: alvo}; } catch (_) {}
            try {
                el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                return {clicked: true, label: alvo};
            } catch (_) {}
        }
        return {clicked: false, label: ''};
    }"""

    deadline = time.time() + max(10, int(timeout_s or 0))
    last_status_emit = 0.0
    while time.time() < deadline:
        if hasattr(page, "is_closed") and page.is_closed():
            return False, ""
        try:
            result = await page.evaluate(selector_js)
        except Exception:
            result = {"clicked": False, "label": ""}
        if isinstance(result, dict) and result.get("clicked"):
            label = str(result.get("label") or "").strip().lower()
            emit_log(q, f"✅ Codex: botão detectado e clicado automaticamente ({label or 'ação final'}).")
            return True, label

        now = time.time()
        if now - last_status_emit >= 5.0:
            remaining = max(0, int(deadline - now))
            emit_event(q, "status", f"Codex: aguardando botão de PR/Branch... {remaining}s")
            last_status_emit = now
        await asyncio.sleep(1.0)

    emit_log(q, "⚠️ Codex: timeout aguardando botão 'Criar PR'/'Atualizar Branch'.")
    return False, ""


async def handle_codex_task_inner(task, page, q, stop_event, activityts=None):
    """Fluxo dedicado ao Codex (chatgpt.com/codex/cloud):
      1) Navega para a Codex URL.
      2) Seleciona o ambiente/repositório correto.
      3) Cola a mensagem no composer (Ctrl+V).
      4) Submete e captura a URL da tarefa criada.
    """
    url = task.get('url') or 'https://chatgpt.com/codex/cloud'
    msg = task.get('message') or ''
    codex_repo = task.get('codex_repo')

    emit_log(q, f'Codex: abrindo {url}')
    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    await asyncio.sleep(1.2)

    if stop_event.is_set():
        raise RuntimeError('Watchdog sinalizou falha antes do composer Codex.')

    # Seletor do composer — marca o elemento para reuso nos passos seguintes.
    composer_sel = await _codex_wait_for_composer(page, q, timeout_ms=20000)

    if codex_repo:
        try:
            await _codex_select_repo(page, q, codex_repo)
        except Exception as exc:
            emit_log(q, f'⚠️ Codex: erro ao selecionar repo "{codex_repo}": {exc}')
        # Depois de selecionar o repo o composer pode ter sido re-renderizado;
        # refaz a marcação.
        try:
            composer_sel = await _codex_wait_for_composer(page, q, timeout_ms=10000)
        except Exception:
            pass

    if stop_event.is_set():
        raise RuntimeError('Watchdog sinalizou falha antes do paste Codex.')

    await _codex_paste_message(page, composer_sel, msg, q, activityts)
    await asyncio.sleep(0.4)

    submitted = await _codex_submit(page, q)
    if not submitted:
        emit_event(q, 'error', 'Codex: não foi possível clicar em Enviar.')
        return

    emit_log(q, 'Codex: mensagem enviada. Aguardando criação da tarefa...')
    # Aguarda redirecionamento para /codex/cloud/tasks/<id>
    task_url = None
    deadline = time.time() + 25.0
    while time.time() < deadline:
        cur = (page.url or '')
        m = re.search(r"https://chatgpt\.com/codex/cloud/tasks/([A-Za-z0-9_\-]+)", cur)
        if m:
            task_url = cur
            break
        await asyncio.sleep(0.5)

    if task_url:
        emit_log(q, f'Codex: tarefa criada → {task_url}')
        emit_event(q, 'chat_meta', {
            'chat_id': task.get('chat_id'),
            'url': task_url,
            'source': 'codex_task_url',
        })
    else:
        emit_log(q, 'Codex: URL da tarefa não detectada em 25s (submissão pode ter sido aceita mesmo assim).')

    # Mantém a aba aberta até aparecer uma das ações finais do Codex para
    # concluir o fluxo de PR/branch antes de encerrar a tarefa no worker.
    await _codex_wait_and_click_pr_controls(page, q, timeout_s=900)

    # Resposta curta confirmando submissão — o agente parseia JSON; devolvemos
    # um plano vazio com analysis explicando que a tarefa Codex foi enfileirada.
    confirmation = (
        '{"analysis": "Tarefa enviada ao ChatGPT Codex ('
        + (task_url or url)
        + '). A implementação concreta será feita pelo Codex em background; '
        + 'o agente consultará o PR resultante em ciclos futuros.", "actions": []}'
    )
    emit_event(q, 'markdown', confirmation)
    emit_event(q, 'finish', {'url': task_url or url, 'title': 'Codex Task'})


async def handle_chat_task_inner(task, page, q, stop_event: asyncio.Event, activityts: list = None):
    # Roteia para o fluxo dedicado do Codex quando a URL aponta para ele.
    _url_for_routing = task.get('url') or ''
    if _is_codex_url(_url_for_routing):
        await handle_codex_task_inner(task, page, q, stop_event, activityts)
        return

    url    = task.get('url')
    chat_id = task.get('chat_id')
    msg    = task.get('message')
    atts   = task.get('attachment_paths')

    # Handler para capturar downloads automáticos do ChatGPT (code interpreter, etc.)
    # Sem salvar em disco: payload em memória (ou URL fallback).
    _auto_downloads = []
    _download_tasks = []
    last_chat_meta_url = None

    async def emit_chat_meta_if_ready():
        nonlocal last_chat_meta_url
        try:
            current_url = (page.url or "").strip()
        except Exception:
            return
        if not current_url:
            return

        m = re.search(r"https://chatgpt\.com/(?:g/[^/]+/)?c/([A-Za-z0-9\-]+)", current_url)
        if not m:
            return

        canonical_url = m.group(0)
        if canonical_url == last_chat_meta_url:
            return

        payload = {
            "chat_id": chat_id,
            "url": canonical_url,
            "browser_chat_id": m.group(1),
            "source": "browser_url",
        }
        emit_event(q, "chat_meta", payload)
        emit_log(q, f"🔗 Chat URL detectada e enviada via stream: {canonical_url}")
        last_chat_meta_url = canonical_url

    def _on_download(download):
        async def _save():
            try:
                suggested = download.suggested_filename or "download"
                dl_url = getattr(download, "url", None)
                if callable(dl_url):
                    dl_url = dl_url()

                payload_b64 = None
                try:
                    tmp_path = await download.path()
                    if tmp_path and os.path.isfile(tmp_path):
                        with open(tmp_path, "rb") as f:
                            payload_b64 = base64.b64encode(f.read()).decode("ascii")
                except Exception:
                    payload_b64 = None

                if not payload_b64 and not dl_url:
                    emit_log(q, f"⚠️ Auto-download sem payload/URL disponível: {suggested}")
                    return

                safe = re.sub(r"[^\w.\-]", "_", suggested)
                file_id = f"{int(time.time() * 1000)}_{safe}"
                _auto_downloads.append({
                    "name": suggested,
                    "file_id": file_id,
                    "url": dl_url or "",
                    "payload_b64": payload_b64,
                    "content_type": "application/octet-stream"
                })
                emit_log(q, f"⬇️ Auto-download capturado: {suggested}")
            except Exception as e:
                emit_log(q, f"⚠️ Erro no auto-download: {e}")
        _download_tasks.append(asyncio.create_task(_save()))

    page.on("download", _on_download)
    # Captura metadados de arquivos diretamente das respostas da API da conversa.
    # Necessário para que `_register_captured_files` possa anexar links de download
    # em mensagens onde a UI nova do ChatGPT não expõe <a> nem href.
    _install_conversation_file_capture(page, q)

    if url and url != 'None':
        emit_log(q, f'Abrindo chat existente: {url}')
        await page.goto(url, wait_until='domcontentloaded')
        await emit_chat_meta_if_ready()
    else:
        emit_log(q, 'Iniciando nova aba de chat...')
        await page.goto('https://chatgpt.com', wait_until='domcontentloaded')
        await asyncio.sleep(2)

    if stop_event.is_set():
        raise RuntimeError('Watchdog sinalizou falha antes do input.')

    if not url or url == 'None':
        try:
            project_loc = page.locator('a[href*="conexaovida"][href*="project"]').first
            found = False
            try:
                await project_loc.wait_for(state='attached', timeout=5000)
                if await project_loc.is_visible():
                    found = True
            except:
                found = False
            if found:
                emit_log(q, "Projeto 'ConexaoVida' encontrado. Criando novo chat no projeto...")
                await project_loc.click()
                await page.wait_for_selector('#prompt-textarea', timeout=10000)
                await asyncio.sleep(1)
                await emit_chat_meta_if_ready()
            else:
                print("DICA: Projeto ConexaoVida não encontrado na barra lateral.")
        except Exception:
            pass
    else:
        try:
            await wait_for_chat_ready(page, url, q, timeout=30)
        except RateLimitDetected as rle:
            emit_event(q, "error", {
                "code": "rate_limit",
                "message": rle.message,
                "retry_after_seconds": rle.retry_after_seconds,
            })
            return

    if stop_event.is_set():
        raise RuntimeError('Watchdog sinalizou falha após carregamento da página.')

    await _clear_input(page, q)

    # Baseline da última resposta já existente no chat ANTES de enviar a nova pergunta.
    # Isso evita vazar resposta antiga (ex.: pergunta manual do desenvolvedor) como se
    # fosse resposta da solicitação atual.
    baseline_markdown = ""
    baseline_assistant_count = 0
    try:
        baseline_snapshot = await _read_last_assistant_snapshot(page)
        baseline_html = clean_html(baseline_snapshot.get("html", ""))
        baseline_markdown = md(baseline_html, heading_style="ATX").strip()
        baseline_markdown = baseline_markdown.replace("\\_", "_").replace("\\*", "*")
        baseline_assistant_count = await _get_assistant_message_count(page)
    except Exception:
        baseline_markdown = ""
        baseline_assistant_count = 0

    if atts:
        emit_event(q, 'status', f'Anexando {len(atts)} arquivo(s)...')
        await upload_files(page, atts)

    emit_event(q, 'status', 'Digitando...')
    try:
        await page.click('#prompt-textarea', timeout=2000)
    except:
        pass

    if msg:
        if activityts:
            activityts[0] = time.time()
        await smart_input(page, msg, q, activityts=activityts)
        if activityts:
            activityts[0] = time.time()

    await asyncio.sleep(1)

    if stop_event.is_set():
        raise RuntimeError("Watchdog sinalizou falha após digitação.")

    emit_event(q, "status", "Enviando...")
    sent = False
    try:
        btn = page.locator('button[data-testid="send-button"]').first
        if await btn.is_visible() and not await btn.is_disabled():
            await btn.click()
            sent = True
    except:
        pass

    if not sent:
        await page.keyboard.press("Enter")
    await emit_chat_meta_if_ready()

    emit_event(q, "status", "Aguardando resposta...")

    # Inicia streaming de screenshots em background durante a resposta
    screenshot_stop = asyncio.Event()
    screenshot_task = asyncio.create_task(
        _stream_browser_screenshots(page, q, screenshot_stop, label="chat")
    )

    start_time  = time.time()
    started     = False
    last_html   = baseline_markdown
    last_status_text = ""
    stuck_count = 0
    loop_count  = 0
    idle_ready_count = 0
    chat_error_reload_count = 0
    max_chat_error_reloads = 2
    response_started = False

    while True:
        await emit_chat_meta_if_ready()

        if stop_event.is_set():
            emit_event(q, "error", "⚠️ Aba travada detectada durante recepção da resposta.")
            break

        loop_count += 1

        rate_limit_state = await page.evaluate("""() => {
            // Restringe candidatos a elementos que realmente são banners/toasts/diálogos,
            // evitando matchar itens da sidebar cujo título mencione "rate limit".
            const selectors = [
                '[role="dialog"]',
                '[role="alert"]',
                '[role="alertdialog"]',
                '[aria-live="polite"]',
                '[aria-live="assertive"]',
                '[data-testid*="rate" i]',
                '[data-testid*="limit" i]',
                '[data-testid*="toast" i]',
                '[class*="Toast" i]',
                '[class*="toast" i]',
                '[class*="Banner" i]',
                '[class*="banner" i]',
                '[class*="RateLimit" i]',
                '[class*="rate-limit" i]',
                'main [data-message-author-role="assistant"] .text-token-text-error',
                'main [data-message-author-role="assistant"] [class*="error" i]'
            ];
            const candidates = Array.from(document.querySelectorAll(selectors.join(',')));
            // Ordena por tamanho do texto (ASC) — prefere o banner mais específico.
            candidates.sort((a, b) => ((a.innerText || '').length - (b.innerText || '').length));
            const hit = candidates.find(el => {
                const txt = (el.innerText || '').trim().toLowerCase();
                if (!txt || txt.length < 10 || txt.length > 800) return false;
                const hasPt = txt.includes('excesso de solicita') && txt.includes('aguarde');
                const hasEn = (
                    (txt.includes('too many requests') && (txt.includes('minute') || txt.includes('wait') || txt.includes('try again'))) ||
                    (txt.includes('rate limit') && (txt.includes('exceed') || txt.includes('wait') || txt.includes('minute'))) ||
                    (txt.includes('you\\'ve reached') && (txt.includes('limit') || txt.includes('message'))) ||
                    (txt.includes('message limit') && txt.includes('reach'))
                );
                return hasPt || hasEn;
            });
            if (!hit) {
                return { detected: false, message: '' };
            }
            const msg = (hit.innerText || '').trim().slice(0, 600);
            return { detected: true, message: msg };
        }""")

        if rate_limit_state.get("detected"):
            rate_limit_msg = (rate_limit_state.get("message") or "Excesso de solicitações").strip()
            emit_log(q, f"⛔ Rate-limit detectado no ChatGPT: {rate_limit_msg[:220]}")
            emit_event(q, "error", {
                "code": "rate_limit",
                "message": rate_limit_msg,
                "retry_after_seconds": 240
            })
            break

        chat_error_state = await page.evaluate("""() => {
            const retryBtn = document.querySelector('button[data-testid="regenerate-thread-error-button"]');
            const errorCard = document.querySelector('[data-message-author-role="assistant"] .text-token-text-error');
            if (!retryBtn && !errorCard) {
                return { hasError: false, message: '' };
            }

            const msgNode = (errorCard || retryBtn?.closest('[data-message-author-role="assistant"]'))?.querySelector('.markdown p, .markdown, p');
            const message = (msgNode?.innerText || errorCard?.innerText || '').trim();
            const lowered = message.toLowerCase();
            const isLikelyInternalError =
                lowered.includes('cannot read properties of undefined')
                || lowered.includes('something went wrong')
                || lowered.includes('ocorreu um erro')
                || lowered.includes('erro inesperado')
                || lowered.includes('undefined');
            return {
                hasError: !!(retryBtn || errorCard),
                hasRetry: !!retryBtn,
                message,
                isLikelyInternalError,
            };
        }""")

        if chat_error_state.get("hasError") and chat_error_state.get("isLikelyInternalError"):
            err_msg = (chat_error_state.get("message") or "erro interno do ChatGPT").strip()
            emit_log(q, f"⚠️ Erro detectado no ChatGPT: {err_msg[:220]}")
            if chat_error_reload_count < max_chat_error_reloads:
                chat_error_reload_count += 1
                current_url = page.url
                emit_event(q, "status", f"Erro interno do ChatGPT detectado. Recarregando página ({chat_error_reload_count}/{max_chat_error_reloads})...")
                try:
                    await page.goto(current_url, wait_until='domcontentloaded', timeout=30_000)
                    try:
                        await wait_for_chat_ready(page, current_url, q, timeout=30)
                    except RateLimitDetected as rle:
                        emit_event(q, "error", {
                            "code": "rate_limit",
                            "message": rle.message,
                            "retry_after_seconds": rle.retry_after_seconds,
                        })
                        return
                    await asyncio.sleep(1)
                    started = False
                    last_status_text = ""
                    stuck_count = 0
                    idle_ready_count = 0
                    start_time = time.time()
                    continue
                except Exception as reload_err:
                    emit_log(q, f"⚠️ Falha ao recarregar chat após erro interno: {reload_err}")
            else:
                emit_event(q, "error", f"Falha no ChatGPT após {max_chat_error_reloads} recarga(s): {err_msg[:300]}")
                break

        status_txt = await page.evaluate("""() => {
            const asstMsgs = document.querySelectorAll('div[data-message-author-role="assistant"]');
            if (asstMsgs.length > 0) {
                const lastAsst = asstMsgs[asstMsgs.length - 1];
                const details = lastAsst.querySelectorAll('details');
                if (details.length > 0) return details[details.length - 1].innerText.trim();
            }
            const targets = Array.from(document.querySelectorAll('div, span'));
            const bad = ["Plus","Team","Enterprise","Upgrade","GPT-4","admin","ChatGPT","Send message"];
            const el = targets.find(t => {
                const txt = t.innerText;
                if (!txt) return false;
                const lower = txt.toLowerCase();
                // "Thought for 1m 8s" é metadado pós-resposta (não indica geração ativa).
                if (/^thought for\s+\d+/i.test(txt.trim()) || /^pensou por\s+\d+/i.test(txt.trim())) {
                    return false;
                }
                const isStatus = lower.includes('pesquisando') || lower.includes('searching') ||
                                 lower.includes('buscando')    || lower.includes('browsing')  ||
                                 lower.includes('procurando')  || lower.includes('checking')  ||
                                 lower.includes('verificando') || lower.includes('consultando')||
                                 lower.includes('navegando')   || lower.includes('looking up') ||
                                 lower.includes('thinking')    || lower.includes('pensando')  ||
                                 lower.includes('analisando')  || lower.includes('analyzing') ||
                                 lower.includes('trabalhando') || lower.includes('working')   ||
                                 lower.includes('lendo')       || lower.includes('reading');
                const isUiChip = lower.includes('pensamento estendido') || lower.includes('extended thinking');
                return isStatus && !isUiChip && !bad.some(b => txt.includes(b)) && t.offsetHeight > 0 && txt.length < 150;
            });
            return el ? el.innerText.trim() : null;
        }""")

        gen_state = await page.evaluate("""() => {
            const stopBtn = document.querySelector('button[aria-label="Stop generating"], button[data-testid="stop-button"]');
            const sendBtn = document.querySelector('button[data-testid="send-button"]');
            const ta = document.querySelector('#prompt-textarea');
            const stopVisible = !!(stopBtn && stopBtn.offsetParent !== null);
            // sendDisabled isoladamente não indica geração:
            // quando o composer está vazio, o botão "Enviar" costuma ficar desabilitado
            // mesmo sem resposta em andamento.
            const sendDisabled = !!(sendBtn && (sendBtn.disabled || sendBtn.getAttribute('aria-disabled') === 'true'));
            const textareaHasText = !!(ta && ((ta.innerText || '').trim().length > 0));
            const textareaBusy = !!(ta && ta.getAttribute('aria-busy') === 'true');
            return { stopVisible, sendDisabled, textareaHasText, textareaBusy };
        }""")

        if status_txt:
            emit_event(q, "status", status_txt)
            last_status_text = status_txt
            started     = True
            stuck_count = 0
            idle_ready_count = 0
        elif not started and loop_count % 10 == 0:
            emit_event(q, "status", "Aguardando resposta...")

        is_gen = bool(
            gen_state.get('stopVisible')
            or gen_state.get('textareaBusy')
            or (gen_state.get('sendDisabled') and gen_state.get('textareaHasText'))
        )
        if is_gen:
            started     = True
            stuck_count = 0
            idle_ready_count = 0

        assistant_snapshot = await _read_assistant_snapshot_after_baseline(page, baseline_assistant_count)
        assistant_count = await _get_assistant_message_count(page)
        has_new_assistant_msg = assistant_count > baseline_assistant_count
        curr_html = clean_html(assistant_snapshot.get("html", ""))
        curr_text = re.sub(r"\s+", " ", assistant_snapshot.get("text", "")).strip()

        markdown_text = md(curr_html, heading_style="ATX").strip()
        markdown_text = markdown_text.replace("\\_", "_").replace("\\*", "*")

        plain_markdown = re.sub(r"\s+", " ", markdown_text).strip()
        visible_status_txt = curr_text
        if visible_status_txt and plain_markdown:
            if visible_status_txt.endswith(plain_markdown):
                visible_status_txt = visible_status_txt[:-len(plain_markdown)].strip()
            elif visible_status_txt == plain_markdown:
                visible_status_txt = ""

        # Só reaproveita o texto visível do balão como "status" enquanto ainda não
        # há resposta markdown consolidada. Depois que o markdown começa a surgir,
        # o texto do balão já representa a resposta final e não deve vazar no CMD
        # como se fosse pensamento da LLM.
        if (
            not status_txt
            and visible_status_txt
            and not plain_markdown
            and visible_status_txt != last_status_text
        ):
            emit_event(q, "status", visible_status_txt[:800])
            last_status_text = visible_status_txt
            started = True
            stuck_count = 0
            idle_ready_count = 0

        if markdown_text != last_html:
            # Ignora conteúdo pré-existente do chat; só começa a streamar quando
            # houver novo balão de assistant após o envio atual.
            if not response_started and not has_new_assistant_msg:
                last_html = markdown_text
                await asyncio.sleep(0.3)
                continue

            # Skip "Pensando"/"Thinking" indicators — these are the ChatGPT
            # extended-thinking label, not actual response content.
            stripped_md = markdown_text.strip().lower()
            if stripped_md in ("pensando", "thinking"):
                if not response_started:
                    emit_event(q, "status", "Pensando")
                last_html = markdown_text
                await asyncio.sleep(0.3)
                continue

            # Skip stale content: if the new snapshot matches or contains the
            # baseline (pre-existing) response, the ChatGPT UI temporarily
            # showed old content in the new assistant node.  Ignore it.
            if baseline_markdown and len(baseline_markdown) > 50:
                norm_base = re.sub(r"\s+", " ", baseline_markdown).strip()[:300]
                norm_curr = re.sub(r"\s+", " ", markdown_text).strip()[:300]
                if norm_base and norm_curr.startswith(norm_base):
                    last_html = markdown_text
                    await asyncio.sleep(0.3)
                    continue

            response_started = True
            if not started:
                emit_event(q, "status", "Recebendo...")
                started = True
            emit_event(q, "markdown", markdown_text)
            last_html   = markdown_text
            last_status_text = ""
            stuck_count = 0
            idle_ready_count = 0
        else:
            stuck_count += 1

        if not is_gen and not status_txt:
            idle_ready_count += 1
            if len(last_html) > 0:
                incomplete_json = _response_looks_incomplete_json(last_html)
                needs_followup = _response_requests_followup_actions(last_html)
                max_stuck = 120 if needs_followup else 24
                max_idle = 40 if needs_followup else 10
                if (not incomplete_json) and stuck_count > max_stuck and idle_ready_count > max_idle:
                    break
            else:
                # Evita encerrar cedo demais quando o ChatGPT demora para começar
                # a emitir markdown visível (ex.: requests longas, tools, busy UI).
                # Já existe timeout global de 600s no loop.
                if time.time() - start_time > 300 and idle_ready_count > 20:
                    emit_log(q, "⏳ Sem markdown visível por 300s; encerrando leitura desta tarefa.")
                    break
        else:
            idle_ready_count = 0

        if time.time() - start_time > 600: break
        await asyncio.sleep(0.3)

    # Para o streaming de screenshots
    screenshot_stop.set()
    screenshot_task.cancel()
    try:
        await screenshot_task
    except (asyncio.CancelledError, Exception):
        pass

    # Após resposta completa: registrar URLs de arquivos para proxy sob demanda
    changed = False
    if markdown_text:
        try:
            rewritten = await _detect_and_register_files(page, markdown_text, q)
            if rewritten != markdown_text:
                markdown_text = rewritten
                changed = True
        except Exception as e:
            emit_log(q, f"⚠️ Falha ao registrar arquivos: {e}")

    # Arquivos capturados via network listener (conversation API) — este é o caminho
    # mais confiável para cards de arquivo da UI nova, que não expõem href em HTML.
    try:
        captured_registered = await _register_captured_files(page, q)
        if captured_registered:
            existing_ids = set(re.findall(r'/api/downloads/([A-Za-z0-9_\-\.]+)', markdown_text or ''))
            novos = []
            for reg in captured_registered:
                file_id_local = reg.get("file_id_local") or ""
                name = reg.get("name") or "arquivo"
                if file_id_local in existing_ids:
                    continue
                novos.append(f"📎 Arquivo: [{name}](/api/downloads/{file_id_local})")
            if novos:
                markdown_text = ((markdown_text or '') + "\n\n" + "\n".join(novos)).strip()
                changed = True
    except Exception as e:
        emit_log(q, f"⚠️ Falha ao anexar arquivos capturados (network): {e}")

    # Auto-downloads capturados pelo Playwright: aguarda tarefas pendentes e registra para proxy
    if _download_tasks:
        await asyncio.gather(*_download_tasks, return_exceptions=True)
    elif _auto_downloads:
        await asyncio.sleep(1)  # compatibilidade: pequena espera quando já houver itens
    for dl in _auto_downloads:
        register_file(
            dl["file_id"],
            dl["url"],
            dl["name"],
            payload_b64=dl.get("payload_b64"),
            content_type=dl.get("content_type")
        )
        link_md = f"\n\n📎 Arquivo: [{dl['name']}](/api/downloads/{dl['file_id']})"
        if dl["file_id"] not in markdown_text:
            markdown_text += link_md
            changed = True
        emit_log(q, f"📎 Auto-download registrado: {dl['name']} → {dl['file_id']}")

    if changed:
        emit_event(q, "markdown", markdown_text)

    final_title = await get_chat_title(page)
    final_url   = page.url
    await emit_chat_meta_if_ready()
    emit_event(q, "finish", {"chat_id": chat_id, "title": final_title, "url": final_url})  # ✅ era chat_id, corrigido para chat_id

async def handle_download_file(context, task):
    """
    Baixa um arquivo do ChatGPT sob demanda (proxy puro).
    Usa o contexto do browser (com cookies/auth) para fazer fetch
    da URL original e retorna os bytes via stream_queue.
    """
    q = task.get('stream_queue')
    file_url = task.get('file_url', '')
    file_name = task.get('file_name', 'download')

    page = None
    try:
        if file_url.startswith("local:"):
            # Auto-download: arquivo já salvo em disco pelo Playwright
            local_path = file_url[6:]  # remove "local:" prefix
            if os.path.isfile(local_path):
                with open(local_path, "rb") as f:
                    data = f.read()
                emit_event(q, "file_data", {
                    "name": file_name,
                    "size": len(data),
                    "data_b64": __import__('base64').b64encode(data).decode('ascii')
                })
            else:
                emit_event(q, "error", f"Arquivo local não encontrado: {local_path}")
            return

        page = await context.new_page()
        await page.goto("https://chatgpt.com", wait_until='domcontentloaded', timeout=15000)
        await asyncio.sleep(1)

        file_log("browser.py", f"⬇️ Download sob demanda: {file_name} de {file_url[:100]}")

        file_data = await page.evaluate("""async (url) => {
            try {
                const resp = await fetch(url, { credentials: 'include' });
                if (!resp.ok) return { error: resp.status + ' ' + resp.statusText };
                const buf = await resp.arrayBuffer();
                const bytes = Array.from(new Uint8Array(buf));
                const ct = resp.headers.get('content-type') || 'application/octet-stream';
                return { data: bytes, content_type: ct };
            } catch(e) { return { error: e.message }; }
        }""", file_url)

        if not file_data or file_data.get('error'):
            err = file_data.get('error', 'Resposta vazia') if file_data else 'Resposta vazia'
            emit_event(q, "error", f"Falha ao baixar: {err}")
            return

        raw_bytes = bytes(file_data['data'])
        emit_event(q, "file_data", {
            "name": file_name,
            "size": len(raw_bytes),
            "content_type": file_data.get('content_type', 'application/octet-stream'),
            "data_b64": __import__('base64').b64encode(raw_bytes).decode('ascii')
        })
        file_log("browser.py", f"✅ Download concluído: {file_name} ({len(raw_bytes)} bytes)")

    except Exception as e:
        file_log("browser.py", f"❌ Erro no download: {e}")
        emit_event(q, "error", f"Erro no download: {str(e)}")
    finally:
        if page:
            try: await page.close()
            except: pass
        if q:
            q.put(None)


# --- LOOP PRINCIPAL ASYNC ---
async def browser_loop_async():
    file_log("browser.py", "⚡ Iniciando Loop Async (Playwright)...")
    async with async_playwright() as p:
        
        # Função interna para iniciar o browser evitando repetição de código
        async def start_browser():
            b = await p.chromium.launch_persistent_context(
                config.DIRS["profile"],
                headless=False,
                accept_downloads=True,
                args=["--start-maximized", "--disable-blink-features=AutomationControlled", "--disable-infobars"],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport=None
            )
            try:
                dp = await b.new_page()
                await dp.goto("https://chatgpt.com")
            except: pass
            await cleanup_known_orphan_tabs(b)
            return b

        # Inicia pela primeira vez
        browser = await start_browser()
        file_log("browser.py", "🟢 Async Worker Online. Aguardando tarefas...")

        try:
            while True:
                try:
                    loop = asyncio.get_running_loop()
                    task = await loop.run_in_executor(None, browser_queue.get)
                    
                    if task.get('action') == 'STOP': break
                    await cleanup_known_orphan_tabs(browser)
                    
                    # =======================================================
                    # AUTO-RECOVERY: TESTA SE O BROWSER AINDA ESTÁ VIVO
                    # =======================================================
                    try:
                        # 1. Se o usuário fechou todas as abas, consideramos fechado
                        if len(browser.pages) == 0:
                            raise Exception("Sem abas")
                        
                        # 2. Faz um "Ping" real no Chromium. Se ele foi fechado no X, isso vai dar erro na hora!
                        await browser.pages[0].evaluate("1")
                        
                    except Exception:
                        file_log("browser.py", "⚠️ Navegador fechado ou desconectado detectado! Recriando...")
                        try: await browser.close()
                        except: pass
                        
                        # Reabre o navegador usando a função interna
                        browser = await start_browser()
                        file_log("browser.py", "✅ Navegador reaberto com sucesso!")
                    # =======================================================

                    action = task.get('action', 'CHAT')
                    
                    if action in ['GET_MENU', 'EXEC_MENU']:
                        asyncio.create_task(handle_menu_task(browser, task))
                    elif action == 'SYNC':
                        asyncio.create_task(handle_sync_task(browser, task))
                    elif action == 'SEARCH':
                        asyncio.create_task(handle_search_task(browser, task))
                    elif action == 'UPTODATE_SEARCH':
                        asyncio.create_task(handle_uptodate_search_task(browser, task))
                    elif action == 'DOWNLOAD_FILE':
                        asyncio.create_task(handle_download_file(browser, task))
                    else:
                        asyncio.create_task(handle_chat_task(browser, task))
                        
                except Exception as e:
                    print(f"Erro no loop principal: {e}")
                    await asyncio.sleep(1)
        finally:
            try:
                await browser.close()
                file_log("browser.py", "🛑 Contexto Chromium encerrado corretamente.")
            except Exception as close_err:
                file_log("browser.py", f"⚠️ Falha ao encerrar Chromium com elegância: {close_err}")

def browser_loop():
    # Wrapper para rodar o loop async dentro da Thread do main.py
    asyncio.run(browser_loop_async())


# =============================================================================
# MODO STANDALONE — python browser.py search "query aqui"
# =============================================================================
# Permite testar a busca Google (e futuramente outras ações) sem precisar
# subir o server.py. Abre o Playwright com o mesmo perfil persistente,
# executa a ação e imprime o resultado no terminal.
# =============================================================================

async def _standalone_search(queries: list, action: str = 'SEARCH'):
    """Executa buscas standalone (sem server.py)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            config.DIRS["profile"],
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled", "--disable-infobars"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport=None,
        )
        try:
            action_label = 'UpToDate' if action == 'UPTODATE_SEARCH' else 'Google'
            for i, query_str in enumerate(queries):
                q = queue.Queue()
                task = {'action': action, 'query': query_str, 'stream_queue': q}

                print(f"\n{'─' * 60}")
                print(f"🔍 [{i+1}/{len(queries)}] {query_str} ({action_label})")
                print(f"{'─' * 60}")

                if action == 'UPTODATE_SEARCH':
                    await handle_uptodate_search_task(browser, task)
                else:
                    await handle_search_task(browser, task)

                # Drena a fila e exibe resultado
                result_data = None
                while True:
                    try:
                        raw = q.get_nowait()
                    except queue.Empty:
                        break
                    if raw is None:
                        break
                    try:
                        msg = json.loads(raw)
                    except:
                        continue

                    t = msg.get('type')
                    if t == 'log':
                        print(f"  📋 {msg.get('content', '')}")
                    elif t == 'status':
                        print(f"  ⏳ {msg.get('content', '')}")
                    elif t == 'searchresult':
                        result_data = msg.get('content', {})

                if not result_data:
                    print("  ❌ Nenhum resultado retornado.")
                    continue

                if not result_data.get('success'):
                    print(f"  ❌ Erro: {result_data.get('error')}")
                    continue

                items = result_data.get('results', [])
                print(f"\n  ✅ {len(items)} resultado(s):\n")
                for item in items:
                    tipo = item.get('type', 'organic')
                    pos  = item.get('position', '?')
                    if tipo == 'featured_snippet':
                        print(f"  ★ DESTAQUE")
                        print(f"    {item['snippet'][:250]}")
                    elif tipo == 'people_also_ask':
                        print(f"  ❓ Perguntas relacionadas")
                        print(f"    {item['snippet'][:250]}")
                    else:
                        print(f"  [{pos}] {item['title']}")
                        print(f"      {item['url']}")
                        if item.get('snippet'):
                            print(f"      {item['snippet'][:180]}")
                    print()

                if i < len(queries) - 1:
                    await asyncio.sleep(random.uniform(2, 4))
        finally:
            await browser.close()


def _cli():
    """Ponto de entrada CLI — executa ações standalone."""
    import sys

    usage = (
        "Uso:\n"
        "  python browser.py search \"metilfenidato efeitos adversos\"\n"
        "  python browser.py search \"query 1\" \"query 2\" \"query 3\"\n"
        "  python browser.py uptodate_search \"acute heart failure\"\n"
    )

    if len(sys.argv) < 3:
        print(usage)
        sys.exit(1)

    action = sys.argv[1].lower()

    if action == 'search':
        queries = sys.argv[2:]
        print(f"🌐 Modo standalone — {len(queries)} busca(s) no Google")
        asyncio.run(_standalone_search(queries))
    elif action == 'uptodate_search':
        queries = sys.argv[2:]
        print(f"🩺 Modo standalone — {len(queries)} busca(s) no UpToDate")
        asyncio.run(_standalone_search(queries, action='UPTODATE_SEARCH'))
    else:
        print(f"Ação desconhecida: '{action}'\n")
        print(usage)
        sys.exit(1)


if __name__ == '__main__':
    _cli()
