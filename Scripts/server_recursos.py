# =============================================================================
# server_recursos.py — Blueprint: avatares, downloads e recursos estáticos
# =============================================================================
#
# RESPONSABILIDADE:
#   Proxy de downloads gerados pelo ChatGPT, servir avatares de usuário e
#   robots.txt. Sem dependência de estado mutável de server.py.
#
# ROTAS:
#   GET /api/user/avatar/<filename> — serve imagem de avatar do usuário
#   GET /api/downloads/<file_id>    — proxy sob demanda de arquivos ChatGPT
#   GET /robots.txt                 — instrui crawlers a não indexar
# =============================================================================
from flask import Blueprint, jsonify, make_response, send_from_directory, Response
import base64
import json
import queue
import time
import config
from shared import browser_queue, get_file_info
from server_helpers import resolve_download_content_type as _resolve_download_content_type_impl

bp = Blueprint("recursos", __name__)


@bp.route("/api/user/avatar/<filename>")
def get_avatar(filename):
    return send_from_directory(config.DIRS["users"], filename)


@bp.route("/api/downloads/<file_id>")
def serve_download(file_id):
    """
    Proxy sob demanda: busca o arquivo do ChatGPT via browser.py
    (usando cookies/auth do Playwright) e faz streaming para o cliente.
    Nenhum arquivo é armazenado permanentemente em disco.
    """
    info = get_file_info(file_id)
    if not info:
        return jsonify({"error": "Arquivo não registrado. O link pode ter expirado."}), 404

    # Atalho: payload já capturado em memória pelo browser.py (sem roundtrip ao ChatGPT).
    if info.get("payload_b64"):
        raw_bytes = base64.b64decode(info["payload_b64"])
        display_name = info.get("name") or file_id
        content_type = _resolve_download_content_type_impl(
            info.get("content_type"), display_name
        )

        resp = make_response(raw_bytes)
        resp.headers['Content-Type'] = content_type
        resp.headers['Content-Disposition'] = f'attachment; filename="{display_name}"'
        resp.headers['Content-Length'] = len(raw_bytes)
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    file_url = info["url"]
    file_name = info["name"]

    # Cria fila de resposta para esta requisição
    response_queue = queue.Queue()

    # Envia tarefa de download para browser.py
    browser_queue.put({
        "action": "DOWNLOAD_FILE",
        "file_url": file_url,
        "file_name": file_name,
        "stream_queue": response_queue,
    })

    # Aguarda resposta do browser.py (timeout: 60s)
    result_data = None
    error_msg = None
    deadline = time.time() + 60

    while time.time() < deadline:
        try:
            raw = response_queue.get(timeout=2)
            if raw is None:
                break  # Sentinel: browser.py terminou
            evt = json.loads(raw) if isinstance(raw, str) else raw
            evt_type = evt.get("type", "")
            content = evt.get("content", "")

            if evt_type == "file_data":
                result_data = content
                break
            elif evt_type == "error":
                error_msg = content
                break
        except queue.Empty:
            continue

    if error_msg:
        return jsonify({"error": error_msg}), 502

    if not result_data:
        return jsonify({"error": "Timeout ao baixar arquivo do ChatGPT."}), 504

    # Decodifica dados base64 e envia ao cliente
    raw_bytes = base64.b64decode(result_data["data_b64"])
    display_name = result_data.get("name", file_name)
    content_type = _resolve_download_content_type_impl(
        result_data.get("content_type"), display_name
    )

    resp = make_response(raw_bytes)
    resp.headers['Content-Type'] = content_type
    resp.headers['Content-Disposition'] = f'attachment; filename="{display_name}"'
    resp.headers['Content-Length'] = len(raw_bytes)
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@bp.route('/robots.txt')
def robots_txt():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")
