# =============================================================================
# server_usuario.py — Blueprint: autenticação leve e gestão de perfil
# =============================================================================
#
# RESPONSABILIDADE:
#   Rotas de usuário que dependem apenas de `auth`, helpers puros e Flask.
#   login_route permanece em server.py (usa _audit_event / _is_ip_blocked).
#
# ROTAS:
#   POST /logout                      — encerra sessão
#   GET  /api/user/info               — dados do usuário logado
#   POST /api/user/update_password    — troca de senha
#   POST /api/user/upload_avatar      — faz upload e redimensiona avatar
# =============================================================================
from flask import Blueprint, request, jsonify
import os
import auth
import config
from server_helpers import resolve_avatar_filename as _resolve_avatar_filename_impl

try:
    from PIL import Image as _Image
    _HAS_PIL = True
except ImportError:
    _Image = None  # type: ignore[assignment,misc]
    _HAS_PIL = False

bp = Blueprint("usuario", __name__)


@bp.route("/logout", methods=["POST"])
def logout_route():
    token = request.cookies.get('session_token')
    auth.logout(token)
    resp = jsonify({"success": True})
    resp.set_cookie('session_token', '', expires=0)
    return resp


@bp.route("/api/user/info", methods=["GET"])
def user_info():
    token = request.cookies.get('session_token')
    info = auth.get_user_info(token)
    if info:
        return jsonify(info)
    return jsonify({"error": "No session"}), 401


@bp.route("/api/user/update_password", methods=["POST"])
def update_pass():
    data = request.get_json() or {}
    user = auth.check_session(request)
    if user and auth.change_password(user, data.get("new_password")):
        return jsonify({"success": True})
    return jsonify({"success": False})


@bp.route("/api/user/upload_avatar", methods=["POST"])
def upload_avatar():
    user = auth.check_session(request)
    if not user:
        return jsonify({"success": False}), 401
    if 'file' not in request.files:
        return jsonify({"success": False})
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False})
    if file:
        filename, err = _resolve_avatar_filename_impl(file.filename, user)
        if err:
            return jsonify({"success": False, "error": err})
        save_path = os.path.join(config.DIRS["users"], filename)
        try:
            if _HAS_PIL:
                img = _Image.open(file)
                img.thumbnail((150, 150))
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(save_path, quality=85, optimize=True)
            else:
                file.save(save_path)
            auth.update_avatar(user, filename)
            return jsonify({"success": True, "avatar": filename})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
