# =============================================================================
# server_observabilidade.py — Blueprint: fila, logs e saúde operacional
# =============================================================================
#
# RESPONSABILIDADE:
#   Rotas de observabilidade da fila interna (DLQ incluída) e streaming de
#   logs do servidor. Sem dependência de estado mutável de server.py —
#   apenas browser_queue (shared.py) e helpers puros (server_helpers.py).
#
# ROTAS:
#   GET  /api/queue/status        — snapshot da fila interna
#   GET  /api/queue/failed        — lista DLQ (tarefas com falha)
#   POST /api/queue/failed/retry  — reinsere item da DLQ na fila
#   GET  /api/logs/tail           — últimas N linhas do log
#   GET  /api/logs/stream         — stream SSE contínuo do log
# =============================================================================
from flask import Blueprint, request, jsonify, Response, stream_with_context
import config
import os
import time
from shared import browser_queue
from server_helpers import (
    safe_snapshot_stats as _safe_snapshot_stats_impl,
    extract_queue_failed_limit as _extract_queue_failed_limit_impl,
    extract_queue_failed_retry_index as _extract_queue_failed_retry_index_impl,
    resolve_logs_tail_lines_limit as _resolve_logs_tail_lines_limit_impl,
    parse_from_end_flag as _parse_from_end_flag_impl,
    build_log_stream_line_sse as _build_log_stream_line_sse_impl,
    build_log_stream_ping_sse as _build_log_stream_ping_sse_impl,
    build_log_stream_error_sse as _build_log_stream_error_sse_impl,
)

bp = Blueprint("observabilidade", __name__)


@bp.route("/api/queue/status", methods=["GET"])
def queue_status():
    """Observabilidade da fila interna server → browser. Requer auth padrão."""
    stats = _safe_snapshot_stats_impl(browser_queue)
    return jsonify({
        "success": True,
        "queue": {
            "qsize": int(browser_queue.qsize()),
            **stats
        }
    }), 200


@bp.route("/api/queue/failed", methods=["GET"])
def queue_failed():
    """DLQ: lista tarefas que falharam no browser loop."""
    limit = _extract_queue_failed_limit_impl(request.args.get("limit", 100))
    items = browser_queue.list_failed(limit=limit) if hasattr(browser_queue, "list_failed") else []
    return jsonify({"success": True, "failed": items, "count": len(items)}), 200


@bp.route("/api/queue/failed/retry", methods=["POST"])
def queue_failed_retry():
    """Reinsere item da DLQ na fila principal por índice."""
    data = request.get_json(silent=True) or {}
    idx = _extract_queue_failed_retry_index_impl(data)
    if not hasattr(browser_queue, "retry_failed"):
        return jsonify({"success": False, "error": "dlq_not_supported"}), 400
    retried = browser_queue.retry_failed(idx)
    if not retried:
        return jsonify({"success": False, "error": "invalid_index"}), 404
    return jsonify({"success": True, "task": retried}), 200


@bp.route("/api/logs/tail", methods=["GET"])
def logs_tail():
    """
    Retorna as últimas linhas do log atual do simulator.
    Ideal para polling leve no frontend (toast de observabilidade).
    """
    lines_limit = _resolve_logs_tail_lines_limit_impl(request.args.get("lines", 120))

    path = getattr(config, "LOG_PATH", "")
    if not path or not os.path.exists(path):
        return jsonify({"success": False, "error": "log_not_found", "path": path}), 404

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            chunk_size = 4096
            data = b""
            pos = file_size
            while pos > 0 and data.count(b"\n") <= lines_limit:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size) + data
            text = data.decode("utf-8", errors="replace")
            tail_lines = text.splitlines()[-lines_limit:]
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "path": path}), 500

    return jsonify({
        "success": True,
        "path": path,
        "lines": tail_lines,
        "line_count": len(tail_lines),
    }), 200


@bp.route("/api/logs/stream", methods=["GET"])
def logs_stream():
    """
    Stream SSE de logs para reduzir polling no frontend.
    query:
      - from_end=1|0 (default 1): inicia no fim do arquivo
    """
    path = getattr(config, "LOG_PATH", "")
    if not path or not os.path.exists(path):
        return jsonify({"success": False, "error": "log_not_found", "path": path}), 404

    from_end = _parse_from_end_flag_impl(request.args.get("from_end", "1"))

    @stream_with_context
    def generate():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if from_end:
                    f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        yield _build_log_stream_line_sse_impl(line, path)
                    else:
                        yield _build_log_stream_ping_sse_impl()
                        time.sleep(1.0)
        except GeneratorExit:
            return
        except Exception as e:
            yield _build_log_stream_error_sse_impl(e, path)

    return Response(generate(), mimetype="text/event-stream")
