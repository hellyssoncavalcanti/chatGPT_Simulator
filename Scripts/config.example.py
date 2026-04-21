# =============================================================================
# config.example.py — Template limpo versionado no repositório.
# =============================================================================
#
# Este arquivo é o gabarito SEM dados sensíveis. É copiado automaticamente para
# `Scripts/config.py` pelo `0. start.bat` quando o config.py real não existe
# (indicando uma instalação nova, ex.: quando o projeto é enviado a outro
# desenvolvedor). Após a primeira cópia, o `0. start.bat` NUNCA mais sobrescreve
# o config.py existente — edições locais persistem entre execuções.
#
# Para qualquer dado sensível (API key de produção, token GitHub, URLs
# específicas de clientes), prefira definir variáveis de ambiente com o prefixo
# apropriado em vez de hardcodar neste arquivo. Todos os defaults abaixo são
# sobreescrevíveis via env var.
#
# Ao enviar o projeto para outro dev:
#   1. Delete Scripts/config.py (o .example.py continua versionado).
#   2. Delete Scripts/sync_github_settings.ps1 (o .example.ps1 continua versionado).
#   3. Opcionalmente delete db/users/users.json para forçar admin/admin.
#   4. O `0. start.bat` recria tudo limpo ao ser executado pelo novo dev.
# =============================================================================
# -*- coding: utf-8 -*-
import os
import secrets
from pathlib import Path
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on", "sim"}:
        return True
    if normalized in {"0", "false", "no", "off", "nao", "não"}:
        return False
    return default


def _env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    values = [v.strip() for v in raw.split(",") if v.strip()]
    return values if values else list(default)


VERSION = "11.0"
PORT = _env_int("SIMULATOR_PORT", 3002)

# API key de autenticação do Simulator. Em instalação nova sem env var, gera uma
# chave aleatória única por processo — NÃO use em produção assim; defina
# SIMULATOR_API_KEY no ambiente ou edite este arquivo depois de copiado.
API_KEY = _env("SIMULATOR_API_KEY", "CVAPI_" + secrets.token_hex(16))

BASE_DIR = _env("SIMULATOR_BASE_DIR", str(Path(__file__).resolve().parent.parent))

DEBUG_LOG = _env_bool("SIMULATOR_DEBUG_LOG", False)

# Segurança HTTP/API
# Allowlist de origens CORS. Vazio = aceita apenas localhost/sessão autenticada.
# Para produção, defina SIMULATOR_CORS_ALLOWED_ORIGINS="https://seudominio.com,https://www.seudominio.com"
CORS_ALLOWED_ORIGINS = _env_csv("SIMULATOR_CORS_ALLOWED_ORIGINS", [])

# Allowlist de IPs confiáveis (camada opcional de defesa em profundidade).
# A autenticação primária é SEMPRE por API key — isto é só um bônus.
# Vazio = nenhuma restrição de IP quando API key / sessão válida for enviada.
ALLOWED_IPS = _env_csv("SIMULATOR_ALLOWED_IPS", ["127.0.0.1"])

SESSION_COOKIE_SECURE = _env_bool("SIMULATOR_SESSION_COOKIE_SECURE", False)
SESSION_COOKIE_SAMESITE = _env("SIMULATOR_SESSION_COOKIE_SAMESITE", "Lax")
SESSION_TTL_HOURS = _env_int("SIMULATOR_SESSION_TTL_HOURS", 24)
SECURITY_RATE_LIMIT_PER_MIN = _env_int("SIMULATOR_RATE_LIMIT_PER_MIN", 120)
SECURITY_LOGIN_MAX_FAILS = _env_int("SIMULATOR_LOGIN_MAX_FAILS", 8)
SECURITY_LOGIN_BLOCK_SEC = _env_int("SIMULATOR_LOGIN_BLOCK_SEC", 900)

REQUEST_TIMEOUT_SEC = _env_int(
    "REQUEST_TIMEOUT_SEC",
    _env_int("AUTODEV_AGENT_REQUEST_TIMEOUT", 900)
)
AUTODEV_AGENT_REQUEST_TIMEOUT = REQUEST_TIMEOUT_SEC

# GitHub sync — preencha via env var ou edite após a cópia inicial.
GITHUB_TOKEN = _env("CHATGPT_SIMULATOR_GITHUB_TOKEN", "")
GH_USER = _env("CHATGPT_SIMULATOR_GITHUB_USER", "")
GITHUB_REPO = _env("CHATGPT_SIMULATOR_GITHUB_REPO", "")
GITHUB_ORIGIN = _env("CHATGPT_SIMULATOR_GITHUB_ORIGIN", "")
GITHUB_BRANCH = _env("CHATGPT_SIMULATOR_GITHUB_BRANCH", "main")
GITHUB_LOCAL_DIR = _env("CHATGPT_SIMULATOR_DIR", BASE_DIR)
GITHUB_TASK_NAME = _env("CHATGPT_SIMULATOR_GITHUB_TASK_NAME", "chatGPT_Simulator_AutoSync")
GITHUB_SYNC_INTERVAL_MINUTES = _env_int("CHATGPT_SIMULATOR_GITHUB_SYNC_INTERVAL_MINUTES", 10)
GITHUB_CHAT_PROCESS_PATTERN = _env("CHATGPT_SIMULATOR_GITHUB_CHAT_PROCESS_PATTERN", r"Scripts\\main.py")
GITHUB_ANALYZER_PATTERN = _env("CHATGPT_SIMULATOR_GITHUB_ANALYZER_PATTERN", r"Scripts\\analisador_prontuarios.py")
GITHUB_REMOTE_PHP_API_KEY = _env("CHATGPT_SIMULATOR_REMOTE_PHP_API_KEY", "")


DIRS = {
    "certs": os.path.join(BASE_DIR, "certs"),
    "frontend": os.path.join(BASE_DIR, "frontend"),
    "db": os.path.join(BASE_DIR, "db"),
    "users": os.path.join(BASE_DIR, "db", "users"),
    "logs": os.path.join(BASE_DIR, "logs"),
    "profile": os.path.join(BASE_DIR, "chrome_profile"),
    "profile_segunda_chance": os.path.join(BASE_DIR, "chrome_profile_segunda_chance"),
    "temp": os.path.join(BASE_DIR, "temp"),
    "downloads": os.path.join(BASE_DIR, "downloads"),
}

CHATS_FILE = os.path.join(DIRS["db"], "history.json")
USERS_FILE = os.path.join(DIRS["users"], "users.json")
APP_DB_FILE = os.path.join(DIRS["db"], "app.db")
CERT_FILE = os.path.join(DIRS["certs"], "cert.pem")
KEY_FILE = os.path.join(DIRS["certs"], "key.pem")
FRONTEND_FILE = os.path.join(DIRS["frontend"], "index.html")

log_filename = datetime.now().strftime("simulator-%d_%m_%Y-%H_%M_%S.log")
LOG_PATH = os.path.join(DIRS["logs"], log_filename)

for d in DIRS.values(): os.makedirs(d, exist_ok=True)

# =============================================================================
# Perfis de navegador Chromium (para casos onde múltiplas contas ChatGPT Plus
# são utilizadas, ex.: uma para usuário humano e outra para o analisador).
# Cada chave mapeia para um diretório de perfil persistente independente.
# O `browser.py` abre contextos separados sob demanda e faz fallback para
# "default" quando a chave solicitada não existir.
# =============================================================================
CHROMIUM_PROFILES = {
    "default": DIRS["profile"],
    "segunda_chance": DIRS["profile_segunda_chance"],
}

# =============================================================================
# ANALISADOR DE PRONTUÁRIOS
# =============================================================================
ANALISADOR_PHP_URL        = _env("ANALISADOR_PHP_URL", "")
ANALISADOR_LLM_URL        = _env("ANALISADOR_LLM_URL", "http://127.0.0.1:3003/v1/chat/completions")
ANALISADOR_LLM_MODEL      = _env("ANALISADOR_LLM_MODEL", "ChatGPT Simulator")
ANALISADOR_PROMPT_VERSION = _env("ANALISADOR_PROMPT_VERSION", "v16.1")

# Perfil Chromium usado pelo analisador. "default" = mesma conta do humano.
# Use "segunda_chance" (ou outra chave de CHROMIUM_PROFILES) para conta dedicada.
ANALISADOR_BROWSER_PROFILE = _env("ANALISADOR_BROWSER_PROFILE", "default")

ANALISADOR_TABELA                   = _env("ANALISADOR_TABELA", "chatgpt_atendimentos_analise")
ANALISADOR_POLL_INTERVAL            = _env_int("ANALISADOR_POLL_INTERVAL", 30)
ANALISADOR_MAX_TENTATIVAS           = _env_int("ANALISADOR_MAX_TENTATIVAS", 3)
ANALISADOR_BATCH_SIZE               = _env_int("ANALISADOR_BATCH_SIZE", 10)
ANALISADOR_MIN_CHARS                = _env_int("ANALISADOR_MIN_CHARS", 80)
ANALISADOR_TIMEOUT_PROCESSANDO_MIN  = _env_int("ANALISADOR_TIMEOUT_PROCESSANDO_MIN", 15)

ANALISADOR_PAUSA_MIN = _env_int("ANALISADOR_PAUSA_MIN", 25)
ANALISADOR_PAUSA_MAX = _env_int("ANALISADOR_PAUSA_MAX", 60)
ANALISADOR_INTERVALO_ANTI_RATE_LIMIT_MULT = float(_env("ANALISADOR_INTERVALO_ANTI_RATE_LIMIT_MULT", "0.5"))

ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO = _env_bool("ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO", False)
ANALISADOR_HORARIO_UTIL_INICIO       = _env_int("ANALISADOR_HORARIO_UTIL_INICIO", 7)
ANALISADOR_HORARIO_UTIL_FIM          = _env_int("ANALISADOR_HORARIO_UTIL_FIM", 19)

ANALISADOR_EMBEDDING_MODEL_NAME = _env("ANALISADOR_EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
ANALISADOR_SIMILARIDADE_TOP_K   = _env_int("ANALISADOR_SIMILARIDADE_TOP_K", 5)
ANALISADOR_SIMILARIDADE_MIN     = float(_env("ANALISADOR_SIMILARIDADE_MIN", "0.40"))

ANALISADOR_SEARCH_URL          = _env("ANALISADOR_SEARCH_URL", "http://127.0.0.1:3003/api/web_search")
ANALISADOR_UPTODATE_SEARCH_URL = _env("ANALISADOR_UPTODATE_SEARCH_URL", "http://127.0.0.1:3003/api/uptodate_search")
ANALISADOR_SEARCH_MAX_QUERIES  = _env_int("ANALISADOR_SEARCH_MAX_QUERIES", 3)
ANALISADOR_SEARCH_TIMEOUT      = _env_int("ANALISADOR_SEARCH_TIMEOUT", 90)
ANALISADOR_SEARCH_HABILITADA   = _env_bool("ANALISADOR_SEARCH_HABILITADA", True)

ANALISADOR_LLM_THROTTLE_MIN         = _env_int("ANALISADOR_LLM_THROTTLE_MIN", 0)
ANALISADOR_LLM_THROTTLE_MAX         = _env_int("ANALISADOR_LLM_THROTTLE_MAX", 0)

ANALISADOR_LLM_RATE_LIMIT_RETRY_MAX     = _env_int("ANALISADOR_LLM_RATE_LIMIT_RETRY_MAX", 3)
ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S  = _env_int("ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S", 0)
ANALISADOR_LLM_RATE_LIMIT_RETRY_MULT    = float(_env("ANALISADOR_LLM_RATE_LIMIT_RETRY_MULT", "2.0"))

# =============================================================================
# SIMULAÇÃO HUMANA (digitação)
# =============================================================================
HUMAN_TYPING_BASE_DELAY_MIN = float(_env("SIMULATOR_HUMAN_TYPING_BASE_DELAY_MIN", "0.01"))
HUMAN_TYPING_BASE_DELAY_MAX = float(_env("SIMULATOR_HUMAN_TYPING_BASE_DELAY_MAX", "0.08"))
HUMAN_TYPING_PUNCT_PAUSE_MIN = float(_env("SIMULATOR_HUMAN_TYPING_PUNCT_PAUSE_MIN", "0.08"))
HUMAN_TYPING_PUNCT_PAUSE_MAX = float(_env("SIMULATOR_HUMAN_TYPING_PUNCT_PAUSE_MAX", "0.24"))
HUMAN_TYPING_NEWLINE_PAUSE_MIN = float(_env("SIMULATOR_HUMAN_TYPING_NEWLINE_PAUSE_MIN", "0.02"))
HUMAN_TYPING_NEWLINE_PAUSE_MAX = float(_env("SIMULATOR_HUMAN_TYPING_NEWLINE_PAUSE_MAX", "0.07"))
HUMAN_TYPING_TYPO_CHANCE = float(_env("SIMULATOR_HUMAN_TYPING_TYPO_CHANCE", "0.012"))
HUMAN_TYPING_TYPO_MAX_BACKSPACES = _env_int("SIMULATOR_HUMAN_TYPING_TYPO_MAX_BACKSPACES", 1)
HUMAN_TYPING_HESITATION_CHANCE = float(_env("SIMULATOR_HUMAN_TYPING_HESITATION_CHANCE", "0.035"))
HUMAN_TYPING_HESITATION_PAUSE_MIN = float(_env("SIMULATOR_HUMAN_TYPING_HESITATION_PAUSE_MIN", "0.12"))
HUMAN_TYPING_HESITATION_PAUSE_MAX = float(_env("SIMULATOR_HUMAN_TYPING_HESITATION_PAUSE_MAX", "0.45"))
