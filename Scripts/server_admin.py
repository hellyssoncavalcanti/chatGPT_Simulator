# =============================================================================
# server_admin.py — Blueprint: diagnóstico, scanner de erros e Claude Fix
# =============================================================================
#
# RESPONSABILIDADE:
#   Rotas de administração para leitura de erros conhecidos, varredura de
#   logs e encaminhamento de erros ao Claude Code para correção automática.
#   Sem dependência de estado mutável de server.py.
#
# ROTAS:
#   GET      /api/errors/known       — lista erros conhecidos (JSON)
#   GET      /api/errors/scan        — escaneia logs por erros novos
#   POST/GET /api/errors/claude_fix  — stream NDJSON de correção via Claude
# =============================================================================
from flask import Blueprint, jsonify, Response, stream_with_context
from pathlib import Path
import json
import os
import config
from error_scanner_helpers import (
    is_unwanted_snippet as _is_unwanted_snippet_impl,
    build_scan_match_entry as _build_scan_match_entry_impl,
    build_scan_error_entry as _build_scan_error_entry_impl,
    build_claude_fix_prompt as _build_claude_fix_prompt_impl,
    build_claude_fix_request_body as _build_claude_fix_request_body_impl,
    build_known_errors_missing_payload as _build_known_errors_missing_payload_impl,
    build_known_errors_loaded_payload as _build_known_errors_loaded_payload_impl,
    build_known_errors_error_payload as _build_known_errors_error_payload_impl,
    build_claude_fix_empty_stream_lines as _build_claude_fix_empty_stream_lines_impl,
    build_claude_fix_status_line as _build_claude_fix_status_line_impl,
    build_claude_fix_error_line as _build_claude_fix_error_line_impl,
    build_claude_fix_finish_line as _build_claude_fix_finish_line_impl,
)

bp = Blueprint("admin", __name__)

_SCRIPTS_DIR = Path(__file__).resolve().parent


@bp.route("/api/errors/known", methods=["GET"])
def api_errors_known():
    """
    Retorna a lista de erros conhecidos (Scripts/erros_conhecidos.json).
    Endpoint LEVE para polling do toast de monitor de erros — apenas leitura
    de arquivo JSON, sem execução do scanner.
    """
    json_path = _SCRIPTS_DIR / "erros_conhecidos.json"
    if not json_path.exists():
        return jsonify(_build_known_errors_missing_payload_impl(json_path)), 200
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return jsonify(_build_known_errors_loaded_payload_impl(data)), 200
    except Exception as e:
        return jsonify(_build_known_errors_error_payload_impl(e)), 500


@bp.route("/api/errors/scan", methods=["GET"])
def api_errors_scan():
    """
    Executa o log_scanner programaticamente e retorna apenas erros NOVOS
    (não casados com erros_conhecidos.json).
    Endpoint PESADO: deve ser chamado apenas sob demanda do usuário (botão).
    """
    try:
        from log_scanner import (
            get_latest_logs, scan_file, load_known_errors,
            CONTEXT_LINES as _CTX, MAX_MATCHES as _MAX,
        )
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"log_scanner indisponível: {e}"
        }), 500

    logs_dir = _SCRIPTS_DIR.parent / "logs"
    if not logs_dir.exists():
        return jsonify({
            "success": False, "error": "logs_dir_not_found",
            "path": str(logs_dir)
        }), 404

    try:
        known = load_known_errors()
        all_logs = get_latest_logs(logs_dir)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    new_errors = []
    scanned = []
    for system, log_path in all_logs.items():
        scanned.append(system)
        try:
            snippets = scan_file(
                log_path, context=_CTX, max_matches=_MAX, known_errors=known
            )
        except Exception as e:
            new_errors.append(_build_scan_error_entry_impl(system, log_path.name, e))
            continue
        for s in snippets:
            if _is_unwanted_snippet_impl(s):
                continue
            new_errors.append(_build_scan_match_entry_impl(system, log_path.name, s))

    return jsonify({
        "success": True,
        "new_errors": new_errors,
        "count": len(new_errors),
        "scanned_systems": scanned,
        "known_count": len(known),
    }), 200


@bp.route("/api/errors/claude_fix", methods=["POST", "GET"])
def api_errors_claude_fix():
    """
    Encaminha os erros novos detectados pelo log_scanner ao Claude Code
    (https://claude.ai/code) via /v1/chat/completions, instruindo-o a
    aplicar correções e abrir um PR no GitHub.
    Retorna o stream NDJSON do Claude em tempo real.
    """
    try:
        from log_scanner import (
            get_latest_logs, scan_file, load_known_errors,
            CONTEXT_LINES as _CTX, MAX_MATCHES as _MAX,
        )
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"log_scanner indisponível: {e}",
        }), 500

    logs_dir = _SCRIPTS_DIR.parent / "logs"
    known = load_known_errors() if logs_dir.exists() else []
    new_errors = []
    if logs_dir.exists():
        for system, log_path in get_latest_logs(logs_dir).items():
            try:
                snippets = scan_file(
                    log_path, context=_CTX, max_matches=_MAX, known_errors=known
                )
            except Exception:
                continue
            for s in snippets:
                if _is_unwanted_snippet_impl(s):
                    continue
                new_errors.append(_build_scan_match_entry_impl(system, log_path.name, s))

    if not new_errors:
        def _empty_stream():
            for ln in _build_claude_fix_empty_stream_lines_impl(len(known)):
                yield ln
        return Response(_empty_stream(), mimetype="application/x-ndjson")

    prompt = _build_claude_fix_prompt_impl(new_errors)

    target_url = os.environ.get(
        "AUTODEV_AGENT_CLAUDE_CODE_URL", "https://claude.ai/code"
    )
    claude_project = os.environ.get(
        "AUTODEV_AGENT_CLAUDE_CODE_PROJECT", "chatGPT_Simulator"
    )

    body = _build_claude_fix_request_body_impl(
        api_key=config.API_KEY,
        prompt=prompt,
        target_url=target_url,
        claude_project=claude_project,
    )

    http_port = int(getattr(config, "PORT", 3002)) + 1
    completions_url = f"http://127.0.0.1:{http_port}/v1/chat/completions"

    try:
        import requests as _req  # noqa: PLC0415
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"módulo 'requests' indisponível: {e}"
        }), 500

    @stream_with_context
    def proxy():
        yield _build_claude_fix_status_line_impl(len(new_errors))
        try:
            with _req.post(
                completions_url,
                json=body,
                stream=True,
                timeout=900,
                headers={"Authorization": f"Bearer {config.API_KEY}"},
            ) as r:
                for raw in r.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    line = raw.strip()
                    if not line:
                        continue
                    yield line + "\n"
        except Exception as e:
            yield _build_claude_fix_error_line_impl(e)
            yield _build_claude_fix_finish_line_impl()

    return Response(proxy(), mimetype="application/x-ndjson")
