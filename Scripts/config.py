# =============================================================================
# config.py — Configurações centrais do ChatGPT Simulator
# =============================================================================
#
# RESPONSABILIDADE:
#   Define TODAS as constantes configuráveis do sistema. É o ponto único de
#   configuração — os demais módulos importam daqui. Se uma variável não for
#   encontrada aqui (ex.: foi removida por engano), cada módulo tem um
#   fallback local para não quebrar.
#
# RELAÇÕES:
#   • Importado por: main.py, server.py, browser.py, auth.py, storage.py,
#                    utils.py, analisador_prontuarios.py
#
# CONSTANTES PRINCIPAIS (Simulator):
#   VERSION       — versão atual do sistema
#   PORT          — porta HTTPS principal (3002); HTTP auxiliar = PORT+1 (3003)
#   API_KEY       — chave de autenticação usada pelo analisador e pelo PHP
#   BASE_DIR      — raiz do projeto em disco
#   DIRS          — dicionário com todos os subdiretórios criados automaticamente
#   CHATS_FILE    — JSON com histórico de chats
#   USERS_FILE    — JSON com usuários e senhas
#   CERT_FILE     — certificado TLS autoassinado
#   KEY_FILE      — chave privada TLS
#
# CONSTANTES DO ANALISADOR DE PRONTUÁRIOS:
#   ANALISADOR_PHP_URL                — endpoint PHP remoto
#   ANALISADOR_LLM_URL / _MODEL      — URL e modelo do Simulator local
#   ANALISADOR_PROMPT_VERSION         — versão do prompt de análise
#   ANALISADOR_TABELA                 — tabela SQL de análises
#   ANALISADOR_POLL_INTERVAL          — segundos entre ciclos
#   ANALISADOR_MAX_TENTATIVAS         — máx retentativas por análise
#   ANALISADOR_BATCH_SIZE             — registros por lote
#   ANALISADOR_MIN_CHARS              — mínimo de caracteres válidos
#   ANALISADOR_TIMEOUT_PROCESSANDO_MIN— minutos antes de considerar travado
#   ANALISADOR_PAUSA_MIN / _MAX       — intervalo de pausa humana (seg)
#   ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO — True/False: bloqueia em horário útil
#   ANALISADOR_HORARIO_UTIL_INICIO/FIM   — faixa de bloqueio (seg-sex, 24h)
#   ANALISADOR_EMBEDDING_MODEL_NAME   — modelo de embeddings
#   ANALISADOR_SIMILARIDADE_TOP_K     — quantos casos semelhantes retornar
#   ANALISADOR_SIMILARIDADE_MIN       — score mínimo de similaridade
#   ANALISADOR_SEARCH_URL             — endpoint de busca web
#   ANALISADOR_UPTODATE_SEARCH_URL    — endpoint de busca UpToDate
#   ANALISADOR_SEARCH_MAX_QUERIES     — máx queries por prontuário
#   ANALISADOR_SEARCH_TIMEOUT         — timeout por busca (seg)
#   ANALISADOR_SEARCH_HABILITADA      — True/False: busca web ativa
#   ANALISADOR_LLM_THROTTLE_MIN/MAX  — seg mínimos/máximos entre envios ao ChatGPT
#   ANALISADOR_LLM_RATE_LIMIT_RETRY_MAX  — tentativas em rate limit
#   ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S — espera base (seg) no rate limit
#   ANALISADOR_LLM_RATE_LIMIT_RETRY_MULT  — multiplicador exponencial
# =============================================================================
# -*- coding: utf-8 -*-
import os
from datetime import datetime


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

VERSION = "11.0"
PORT = _env_int("SIMULATOR_PORT", 3002)
API_KEY = _env("SIMULATOR_API_KEY", "CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e")
BASE_DIR = _env(
    "SIMULATOR_BASE_DIR",
    r"C:\chatgpt_simulator" if os.name == "nt" else os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

# Debug: exibe todas as queries SQL no console (útil para auditoria)
DEBUG_LOG = _env_bool("SIMULATOR_DEBUG_LOG", False)   # Altere para True para ativar o debug

# Timeout universal (segundos) para requisições longas de automações Python.
# Prioridade:
#   1) REQUEST_TIMEOUT_SEC
#   2) AUTODEV_AGENT_REQUEST_TIMEOUT (legado)
#   3) default 900s
REQUEST_TIMEOUT_SEC = _env_int(
    "REQUEST_TIMEOUT_SEC",
    _env_int("AUTODEV_AGENT_REQUEST_TIMEOUT", 900)
)
# Alias explícito para manter compatibilidade com scripts legados.
AUTODEV_AGENT_REQUEST_TIMEOUT = REQUEST_TIMEOUT_SEC

# GitHub sync (compatibilidade com scripts legados que liam credenciais daqui)
GITHUB_TOKEN = _env("CHATGPT_SIMULATOR_GITHUB_TOKEN", "")
GH_USER = _env("CHATGPT_SIMULATOR_GITHUB_USER", "")
GITHUB_REPO = _env("CHATGPT_SIMULATOR_GITHUB_REPO", "chatGPT_Simulator")
GITHUB_BRANCH = _env("CHATGPT_SIMULATOR_GITHUB_BRANCH", "main")


DIRS = {
    "certs": os.path.join(BASE_DIR, "certs"),
    "frontend": os.path.join(BASE_DIR, "frontend"),
    "db": os.path.join(BASE_DIR, "db"),
    "users": os.path.join(BASE_DIR, "db", "users"), # Novo diretório
    "logs": os.path.join(BASE_DIR, "logs"),
    "profile": os.path.join(BASE_DIR, "chrome_profile"),
    "temp": os.path.join(BASE_DIR, "temp"),
    "downloads": os.path.join(BASE_DIR, "downloads")
}

CHATS_FILE = os.path.join(DIRS["db"], "history.json")
USERS_FILE = os.path.join(DIRS["users"], "users.json") # Arquivo de usuários
CERT_FILE = os.path.join(DIRS["certs"], "cert.pem")
KEY_FILE = os.path.join(DIRS["certs"], "key.pem")
FRONTEND_FILE = os.path.join(DIRS["frontend"], "index.html")

log_filename = datetime.now().strftime("simulator-%d_%m_%Y-%H_%M_%S.log")
LOG_PATH = os.path.join(DIRS["logs"], log_filename)

for d in DIRS.values(): os.makedirs(d, exist_ok=True)

# =============================================================================
# ANALISADOR DE PRONTUÁRIOS — configurações centralizadas
# =============================================================================
# Os módulos que usam estas variáveis importam via getattr(config, ..., fallback)
# para não quebrar caso alguma seja removida acidentalmente deste arquivo.

# Conexão e identidade
ANALISADOR_PHP_URL        = "https://conexaovida.org/scripts/js/chatgpt_integracao_criado_pelo_gemini.js.php"
ANALISADOR_LLM_URL        = "http://127.0.0.1:3003/v1/chat/completions"
ANALISADOR_LLM_MODEL      = "ChatGPT Simulator"
ANALISADOR_PROMPT_VERSION  = "v16.1"  # v16.1: + busca web + enriquecimento com evidências

# Banco e loop
ANALISADOR_TABELA                   = "chatgpt_atendimentos_analise"
ANALISADOR_POLL_INTERVAL            = 30    # segundos entre ciclos
ANALISADOR_MAX_TENTATIVAS           = 3     # máx retentativas por análise
ANALISADOR_BATCH_SIZE               = 10    # registros por lote
ANALISADOR_MIN_CHARS                = 80    # mínimo de caracteres válidos
ANALISADOR_TIMEOUT_PROCESSANDO_MIN  = 15    # minutos antes de considerar travado

# Pausa humana entre análises individuais do lote
ANALISADOR_PAUSA_MIN = 630   # ~10 minutos (seg)
ANALISADOR_PAUSA_MAX = 800   # ~13 minutos (seg)

# Filtro de horário útil (preserva limite de mensagens do ChatGPT Plus)
ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO = False  # True para bloquear em horário útil
ANALISADOR_HORARIO_UTIL_INICIO       = 7     # 07:00 (seg-sex)
ANALISADOR_HORARIO_UTIL_FIM          = 19    # 19:00 (exclusivo)

# Sentence-Transformers / Embeddings
ANALISADOR_EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
ANALISADOR_SIMILARIDADE_TOP_K   = 5
ANALISADOR_SIMILARIDADE_MIN     = 0.40

# Busca Web (enriquecimento de condutas com evidências)
ANALISADOR_SEARCH_URL          = "http://127.0.0.1:3003/api/web_search"
ANALISADOR_UPTODATE_SEARCH_URL = "http://127.0.0.1:3003/api/uptodate_search"
ANALISADOR_SEARCH_MAX_QUERIES  = 3
ANALISADOR_SEARCH_TIMEOUT      = 90    # seg (browser precisa digitar)
ANALISADOR_SEARCH_HABILITADA   = True  # False para desabilitar sem remover código

# Throttle entre mensagens ao ChatGPT (evita "excesso de solicitações")
ANALISADOR_LLM_THROTTLE_MIN         = 8    # seg mínimos entre envios ao ChatGPT
ANALISADOR_LLM_THROTTLE_MAX         = 15   # seg máximos (aleatoriza entre MIN e MAX)

# Retry com backoff quando ChatGPT retorna rate limit
ANALISADOR_LLM_RATE_LIMIT_RETRY_MAX     = 3    # tentativas antes de desistir
ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S  = 60   # espera base (seg) no 1.º rate limit
ANALISADOR_LLM_RATE_LIMIT_RETRY_MULT    = 2.0  # multiplicador exponencial
