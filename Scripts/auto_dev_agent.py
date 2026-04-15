#!/usr/bin/env python3
# =============================================================================
# auto_dev_agent.py — Agente Autônomo de Desenvolvimento Contínuo
# =============================================================================
#
# RESPONSABILIDADE:
#   Agir como um desenvolvedor sênior autônomo que monitora continuamente o
#   ChatGPT_Simulator, detecta erros em tempo real, consulta o ChatGPT (via
#   browser.py → endpoint /v1/chat/completions) para obter diagnósticos e
#   correções, aplica alterações localmente com segurança e propõe melhorias
#   contínuas de arquitetura, performance e robustez.
#
# FLUXO GERAL DE CADA CICLO:
#   1. Health-check do Simulator (server.py + browser.py).
#   2. Descoberta multiplataforma de serviços ativos do ecossistema.
#   3. Leitura dos logs recentes e detecção de incidentes (erros/warnings).
#   4. Leitura do código-fonte relevante (com foco em arquivos com erros).
#   5. Construção de contexto estruturado para o ChatGPT.
#   6. Consulta ao ChatGPT via browser.py:
#        a) Quando há erros  → pede diagnóstico + plano de correção.
#        b) Periodicamente    → pede sugestões de melhoria contínua.
#   7. Parsing da resposta JSON (actions: edit_file | create_file | shell | note).
#   8. Backup dos arquivos afetados.
#   9. Execução segura das ações propostas (políticas de segurança aplicadas).
#  10. Validação das alterações (py_compile de todos os .py do projeto).
#  11. Rollback automático em caso de falha na validação.
#  12. Commit + push automáticos em caso de sucesso (branch configurável).
#  13. Registro detalhado de tudo em logs/auto_dev_agent-*.log.
#  14. Sleep e repetição indefinida — mesmo em caso de exceção fatal no ciclo.
#
# PRINCÍPIOS DE SEGURANÇA:
#   • Arquivos protegidos (certs/, db/, .git/, logs/, config.py)
#     jamais são modificados.
#   • Comandos destrutivos (rm -rf, git reset --hard, shutdown, mkfs, dd, …)
#     são bloqueados por allowlist explícita.
#   • Toda edição cria backup em temp/agent_backups/<timestamp>/ antes de atuar.
#   • Toda falha de validação dispara rollback imediato.
#   • O agente nunca se auto-modifica sem validação adicional (dry-run).
#   • Limite de ações por ciclo (MAX_ACTIONS_PER_CYCLE) para evitar drift.
#
# VARIÁVEIS DE AMBIENTE SUPORTADAS (todas opcionais, com defaults sensatos):
#   AUTODEV_AGENT_SIMULATOR_URL    — URL do endpoint /v1/chat/completions
#   AUTODEV_AGENT_CODEX_URL        — URL do Codex/ChatGPT para o agente usar
#   AUTODEV_AGENT_MODEL            — nome lógico do modelo ("ChatGPT Simulator")
#   AUTODEV_AGENT_API_KEY          — override da API_KEY do config.py
#   AUTODEV_AGENT_CYCLE_SEC        — intervalo entre ciclos (default 120s)
#   AUTODEV_AGENT_SUGGESTION_SEC   — intervalo entre rodadas proativas (default 600s)
#   AUTODEV_AGENT_REQUEST_TIMEOUT  — timeout HTTP por pergunta (default 900s)
#   AUTODEV_AGENT_CONTEXT_CHARS    — tamanho máx. do contexto enviado (default 28000)
#   AUTODEV_AGENT_MAX_ACTIONS      — ações máx. aplicadas por ciclo (default 5)
#   AUTODEV_AGENT_AUTOCOMMIT       — "1" para commit automático (default "1")
#   AUTODEV_AGENT_AUTOPUSH         — "1" para push automático (default "0")
#   AUTODEV_AGENT_AUTOFIX          — "1" para aplicar patches (default "1")
#   AUTODEV_AGENT_BRANCH           — nome do branch de trabalho (default atual)
#   AUTODEV_AGENT_REUSE_CHAT       — "1" reutiliza mesma conversa (default "1")
#
# =============================================================================
from __future__ import annotations

import json
import logging
import os
import platform
import atexit
import random
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import requests
except Exception as _exc:
    print(f"[auto_dev_agent] ❌ 'requests' indisponível: {_exc}", file=sys.stderr)
    raise

try:
    import config  # type: ignore
except Exception:
    config = None  # type: ignore


# =============================================================================
# CONSTANTES DE CAMINHO E AMBIENTE
# =============================================================================
ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "Scripts"
LOGS_DIR = ROOT_DIR / "logs"
TEMP_DIR = ROOT_DIR / "temp"
BACKUP_DIR = TEMP_DIR / "agent_backups"
STATE_FILE = TEMP_DIR / "auto_dev_agent_state.json"
LOCK_FILE = TEMP_DIR / "auto_dev_agent.lock"

for _d in (LOGS_DIR, TEMP_DIR, BACKUP_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

_LOG_TS = datetime.now().strftime("%d_%m_%Y-%H_%M_%S")
AGENT_LOG = LOGS_DIR / f"auto_dev_agent-{_LOG_TS}.log"

IS_WINDOWS = platform.system().lower().startswith("win")
IS_LINUX = platform.system().lower() == "linux"
IS_MAC = platform.system().lower() == "darwin"


def _env(name: str, default: str, legacy: Optional[str] = None) -> str:
    """Lê variável de ambiente com fallback para um nome legado."""
    if name in os.environ:
        return os.environ[name]
    if legacy and legacy in os.environ:
        return os.environ[legacy]
    return default


def _env_bool(name: str, default: bool, legacy: Optional[str] = None) -> bool:
    raw = _env(name, "1" if default else "0", legacy).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


# =============================================================================
# CONFIGURAÇÃO — URLs, Modelo, API Key
# =============================================================================
SIMULATOR_URL = _env(
    "AUTODEV_AGENT_SIMULATOR_URL",
    "http://127.0.0.1:3003/v1/chat/completions",
    "AUTON_AGENT_SIMULATOR_URL",
)
SIMULATOR_HEALTH_URL = SIMULATOR_URL.replace("/v1/chat/completions", "/health")

# Codex cloud: endpoint do ChatGPT Codex. Default aponta para a home do
# Codex cloud (https://chatgpt.com/codex/cloud), onde ficam o composer e o
# seletor de ambiente/repositório.
CODEX_URL = _env("AUTODEV_AGENT_CODEX_URL", "https://chatgpt.com/codex/cloud")
# Repositório/ambiente a ser selecionado no dropdown antes do paste.
# Deve bater com o texto exibido na lista de ambientes do Codex.
CODEX_REPO = _env("AUTODEV_AGENT_CODEX_REPO", "hellyssoncavalcanti/chatGPT_Simulator")
# Reuso de conversa Codex entre rodadas do mesmo ciclo (chat_id separado
# do chat regular).
CODEX_REUSE_CHAT = _env_bool("AUTODEV_AGENT_CODEX_REUSE_CHAT", True)
# Compat legado: o prefixo "MAX REASONING" é injetado EXCLUSIVAMENTE no
# browser.py (fluxo Codex). Mantemos esta constante como no-op apenas para
# evitar NameError caso algum template antigo ainda referencie este nome.
CODEX_MAX_REASONING_PREFIX = ""
# Quando False (padrão), o agente NÃO bloqueia novos forwards enquanto uma
# tarefa anterior do Codex parece pendente. O Codex suporta filas paralelas.
CODEX_BLOCK_WHILE_PENDING = _env_bool("AUTODEV_AGENT_CODEX_BLOCK_WHILE_PENDING", False)
# Janela mínima (segundos) sem pedir novo trabalho ao Codex após um forward
# bem-sucedido. Durante esse período, o agente assume que a tarefa anterior
# ainda está sendo executada no Codex (elaborando o PR) e não envia nada
# novo, mesmo que detecte sugestões/falhas pendentes.
CODEX_MIN_WAIT_SEC = int(_env("AUTODEV_AGENT_CODEX_MIN_WAIT_SEC", "600"))
# Janela máxima (segundos) para aguardar conclusão antes de desbloquear
# novos forwards mesmo sem evidência de conclusão — evita ficar preso
# indefinidamente se o Codex travar ou se o PR for fechado manualmente.
CODEX_MAX_WAIT_SEC = int(_env("AUTODEV_AGENT_CODEX_MAX_WAIT_SEC", "2400"))

SIMULATOR_MODEL = _env("AUTODEV_AGENT_MODEL", "ChatGPT Simulator", "AUTON_AGENT_MODEL")
_CFG_API_KEY = getattr(config, "API_KEY", "") if config else ""
API_KEY = _env("AUTODEV_AGENT_API_KEY", _CFG_API_KEY, "AUTON_AGENT_API_KEY")

# =============================================================================
# CONFIGURAÇÃO — Ciclos e Temporização
# =============================================================================
CYCLE_INTERVAL_SEC = int(_env("AUTODEV_AGENT_CYCLE_SEC", "120", "AUTON_AGENT_CYCLE_SEC"))
SUGGESTION_INTERVAL_SEC = int(
    _env("AUTODEV_AGENT_SUGGESTION_SEC", "600", "AUTON_AGENT_SUGGESTION_SEC")
)
REQUEST_TIMEOUT_SEC = int(_env("AUTODEV_AGENT_REQUEST_TIMEOUT", "900"))
STREAM_IDLE_TIMEOUT_SEC = int(_env("AUTODEV_AGENT_STREAM_IDLE_SEC", "180"))
STARTUP_WAIT_SEC = int(_env("AUTODEV_AGENT_STARTUP_WAIT_SEC", "30"))
HEALTH_LOG_THROTTLE_SEC = 30.0
HEALTHCHECK_RETRIES = max(1, int(_env("AUTODEV_AGENT_HEALTH_RETRIES", "2")))
HEALTHCHECK_RETRY_DELAY_SEC = max(1, int(_env("AUTODEV_AGENT_HEALTH_RETRY_DELAY_SEC", "2")))
AUTOSTART_SIMULATOR_CMD = _env("AUTODEV_AGENT_AUTOSTART_CMD", "").strip()
AUTOSTART_COOLDOWN_SEC = max(10, int(_env("AUTODEV_AGENT_AUTOSTART_COOLDOWN_SEC", "180")))

# Intervalo humano entre pedidos ao ChatGPT (alinha com analisador_prontuarios)
_CFG_PAUSA_MIN = int(getattr(config, "ANALISADOR_PAUSA_MIN", 25) or 25) if config else 25
_CFG_PAUSA_MAX = int(getattr(config, "ANALISADOR_PAUSA_MAX", 60) or 60) if config else 60
AUTODEV_CHAT_PAUSA_MIN_SEC = int(_env("AUTODEV_CHAT_PAUSA_MIN_SEC", str(_CFG_PAUSA_MIN)))
AUTODEV_CHAT_PAUSA_MAX_SEC = int(_env("AUTODEV_CHAT_PAUSA_MAX_SEC", str(_CFG_PAUSA_MAX)))
if AUTODEV_CHAT_PAUSA_MAX_SEC < AUTODEV_CHAT_PAUSA_MIN_SEC:
    AUTODEV_CHAT_PAUSA_MAX_SEC = AUTODEV_CHAT_PAUSA_MIN_SEC

# =============================================================================
# CONFIGURAÇÃO — Contexto e Limites
# =============================================================================
MAX_CONTEXT_CHARS = int(_env("AUTODEV_AGENT_CONTEXT_CHARS", "28000", "AUTON_AGENT_CONTEXT_CHARS"))
MAX_FILE_LINES_IN_CONTEXT = int(_env("AUTODEV_AGENT_MAX_FILE_LINES", "400"))
MAX_LOG_LINES_PER_FILE = int(_env("AUTODEV_AGENT_MAX_LOG_LINES", "120"))
MAX_LOG_FILES = int(_env("AUTODEV_AGENT_MAX_LOG_FILES", "6"))
MAX_INCIDENTS_IN_CONTEXT = 60

# Quantas vezes o agente pede ao ChatGPT/Codex para TRANSFORMAR uma sugestão
# em ações concretas (edit_file/create_file) quando o plano inicial só tem
# notas ou quando as ações concretas falharam neste ciclo.
MAX_CODEX_FORWARD_ATTEMPTS = int(_env("AUTODEV_AGENT_CODEX_FORWARD_MAX", "2"))

# =============================================================================
# CONFIGURAÇÃO — Segurança e Políticas
# =============================================================================
ENABLE_AUTOFIX = _env_bool("AUTODEV_AGENT_AUTOFIX", True, "AUTON_AGENT_UNSAFE")
ENABLE_AUTOCOMMIT = _env_bool("AUTODEV_AGENT_AUTOCOMMIT", True)
ENABLE_AUTOPUSH = _env_bool("AUTODEV_AGENT_AUTOPUSH", False)
REUSE_CHAT_CONVERSATION = _env_bool("AUTODEV_AGENT_REUSE_CHAT", True)

# Encapsulamento para colagem via clipboard — evita que browser.py digite
# caractere a caractere (ganho de ordens de grandeza em velocidade).
# Quando habilitado, o conteúdo de cada mensagem vai entre os marcadores
# [INICIO_TEXTO_COLADO] ... [FIM_TEXTO_COLADO] que browser.py reconhece e
# injeta no composer via Ctrl+V.
USE_PASTE_MARKERS = _env_bool("AUTODEV_AGENT_USE_PASTE_MARKERS", True)
PASTE_MARKER_START = "[INICIO_TEXTO_COLADO]"
PASTE_MARKER_END = "[FIM_TEXTO_COLADO]"

MAX_EDIT_SIZE_BYTES = int(_env("AUTODEV_AGENT_MAX_EDIT_BYTES", "200000"))
MAX_ACTIONS_PER_CYCLE = int(_env("AUTODEV_AGENT_MAX_ACTIONS", "5"))
MAX_RETRY_ATTEMPTS = int(_env("AUTODEV_AGENT_MAX_RETRIES", "2"))
MAX_SHELL_TIMEOUT_SEC = int(_env("AUTODEV_AGENT_SHELL_TIMEOUT", "180"))
STREAM_REQUEST_RETRIES = int(_env("AUTODEV_AGENT_STREAM_RETRIES", "2"))

GIT_BRANCH = _env("AUTODEV_AGENT_BRANCH", "").strip()
GIT_REMOTE = _env("AUTODEV_AGENT_REMOTE", "origin")
COMMIT_PREFIX = _env("AUTODEV_AGENT_COMMIT_PREFIX", "[auto-dev-agent]")

# Extensões editáveis — outras são tratadas como read-only
EDITABLE_EXTENSIONS = {".py", ".md", ".bat", ".txt", ".json", ".ini", ".cfg", ".yml", ".yaml"}

# Caminhos proibidos (nunca ler nem escrever)
BLOCKED_PATH_SEGMENTS = [
    ".git/", ".git\\",
    "certs/", "certs\\",
    "db/", "db\\",
    "logs/", "logs\\",
    "chrome_profile/", "chrome_profile\\",
    "__pycache__/", "__pycache__\\",
    ".venv/", ".venv\\",
    "node_modules/", "node_modules\\",
    "temp/agent_backups/",
]

# Arquivos protegidos contra edição (negócio crítico)
PROTECTED_FILES = {
    "Scripts/config.py",  # Configurações críticas — só altera sob supervisão
}

# O próprio agente só pode se auto-modificar quando AUTODEV_AGENT_SELF_EDIT=1
ALLOW_SELF_EDIT = _env_bool("AUTODEV_AGENT_SELF_EDIT", False)
SELF_FILE_REL = "Scripts/auto_dev_agent.py"

# Padrões destrutivos em comandos de shell (bloqueia antes de executar)
FORBIDDEN_SHELL_PATTERNS = [
    r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\b",
    r"\brm\s+-[a-z]*f[a-z]*r[a-z]*\b",
    r"\brmdir\s+/s\b",
    r"\bdel\s+/[fsq]\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bmkfs\b",
    r"\bformat\s+[a-z]:\b",
    r"\bdd\s+if=",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+push\s+--force\b",
    r"\bgit\s+push\s+-f\b",
    r":\(\)\s*\{\s*:\|\:&\s*\};:",
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+DATABASE\b",
    r"\bTRUNCATE\s+TABLE\b",
    r"\bchmod\s+-R\s+777\b",
    r"\bkill\s+-9\s+1\b",
]

# Padrões regex de erro/warning nos logs
ERROR_PATTERNS = [
    r"Traceback \(most recent call last\)",
    r"\bERROR\b",
    r"\bException\b",
    r"\bexception\b",
    r"\bERRO\b",
    r"\bErro\b",
    r"\bFALHA\b",
    r"\bFalha\b",
    r"\bFATAL\b",
    r"\bFatal\b",
    r"\bConnectionResetError\b",
    r"\bConnectionRefusedError\b",
    r"\bTimeoutError\b",
    r"\bSegmentation fault\b",
    r"\bModuleNotFoundError\b",
    r"\bImportError\b",
    r"\bSyntaxError\b",
    r"\bTypeError\b",
    r"\bKeyError\b",
    r"\bAttributeError\b",
    r"\bValueError\b",
    r"\bRuntimeError\b",
    r"\bPlaywrightError\b",
    r"\bcannot access local variable\b",
]

WARN_PATTERNS = [
    r"\bWARNING\b",
    r"\bWarning\b",
    r"\bAviso\b",
    r"\btimeout\b",
    r"\brate.?limit\b",
    r"\bthrottle\b",
    r"\bretry\b",
    r"\bdeprecated\b",
    r"\bexcesso de solicita",
]

# Linhas informativas conhecidas que não devem virar incidente mesmo contendo
# termos ambíguos (ex.: "timeout" no nome da branch).
BENIGN_INCIDENT_PATTERNS = [
    r"sem commits novos em relacao ao base\. pulando\.",
    r"processando branch 'codex/",
]

_LOG_LEVEL_TAG_RE = re.compile(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]", re.IGNORECASE)


# =============================================================================
# LOGGER
# =============================================================================
def _setup_logger() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("auto_dev_agent")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        fh = logging.FileHandler(AGENT_LOG, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:
        print(f"[auto_dev_agent] ❌ Falha ao abrir log file: {exc}", file=sys.stderr)
    class _AnsiColorFormatter(logging.Formatter):
        RESET = "\033[0m"
        LEVEL_COLORS = {
            logging.DEBUG: "\033[90m",
            logging.INFO: "\033[96m",
            logging.WARNING: "\033[93m",
            logging.ERROR: "\033[91m",
            logging.CRITICAL: "\033[95m",
        }

        def format(self, record: logging.LogRecord) -> str:
            text = super().format(record)
            stream = getattr(sys, "stdout", None)
            is_tty = bool(stream and hasattr(stream, "isatty") and stream.isatty())
            if os.environ.get("NO_COLOR") or (not is_tty):
                return text
            color = self.LEVEL_COLORS.get(record.levelno, "")
            if not color:
                return text
            return f"{color}{text}{self.RESET}"

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_AnsiColorFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(sh)
    return logger


LOGGER = _setup_logger()


def log(msg: str, level: int = logging.INFO) -> None:
    LOGGER.log(level, msg)


# =============================================================================
# DATACLASSES
# =============================================================================
@dataclass
class Incident:
    """Erro ou warning detectado em um arquivo de log."""
    level: str          # "error" | "warning"
    source: str         # caminho relativo do log
    line: str           # linha do log (stripped)
    when_utc: str       # ISO 8601 UTC


@dataclass
class AgentState:
    """Estado persistente entre ciclos."""
    chat_id: Optional[str] = None       # id da conversa com ChatGPT (reuso)
    chat_url: Optional[str] = None      # URL da conversa ativa
    codex_chat_id: Optional[str] = None # id da conversa ATIVA no Codex
    codex_chat_url: Optional[str] = None# URL da conversa Codex (chatgpt.com/codex/c/...)
    # Tarefa Codex pendente (ainda em execução); o agente evita pedir
    # novo trabalho ao Codex enquanto houver uma tarefa destas em aberto.
    codex_pending_task_url: Optional[str] = None
    codex_pending_started_at: float = 0.0
    last_suggestion_ts: float = 0.0     # timestamp da última rodada proativa
    cycles_total: int = 0               # total de ciclos executados
    cycles_with_errors: int = 0         # ciclos que detectaram incidentes
    cycles_with_fixes: int = 0          # ciclos que aplicaram correções
    total_actions: int = 0              # total de ações aplicadas
    last_services_signature: str = ""   # última assinatura do mapa de serviços


@dataclass
class ActionResult:
    """Resultado da execução de uma ação proposta pelo ChatGPT."""
    action_type: str
    ok: bool
    description: str
    details: str = ""
    changed_files: List[str] = field(default_factory=list)


_AGENT_STATE = AgentState()
_last_autostart_attempt = 0.0


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _release_single_instance_lock() -> None:
    try:
        if LOCK_FILE.exists():
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            if int(data.get("pid") or 0) == os.getpid():
                LOCK_FILE.unlink()
    except Exception:
        pass


def _ensure_single_instance_lock() -> None:
    """Evita duas instâncias do auto_dev_agent concorrendo no mesmo repo."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        existing_pid = int(data.get("pid") or 0)
        if existing_pid and existing_pid != os.getpid() and _pid_is_running(existing_pid):
            raise RuntimeError(
                f"Já existe outra instância ativa do auto_dev_agent (pid={existing_pid})."
            )
        try:
            LOCK_FILE.unlink()
        except Exception:
            pass

    payload = {
        "pid": os.getpid(),
        "started_at_utc": _now_utc_iso(),
    }
    LOCK_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    atexit.register(_release_single_instance_lock)


def _load_state() -> None:
    global _AGENT_STATE
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _AGENT_STATE = AgentState(**{k: v for k, v in data.items() if hasattr(AgentState, k)})
            log(f"📦 Estado anterior carregado (ciclos={_AGENT_STATE.cycles_total}, chat_id={_AGENT_STATE.chat_id!s})")
    except Exception as exc:
        log(f"⚠️ Falha ao carregar state: {exc}", logging.WARNING)


def _save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(_AGENT_STATE.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"⚠️ Falha ao persistir state: {exc}", logging.WARNING)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# SEGURANÇA — validação de paths e comandos
# =============================================================================
def _normalize_rel_path(path_str: str) -> Optional[Path]:
    """Normaliza um caminho relativo ao ROOT_DIR; retorna None se inválido."""
    if not path_str or not isinstance(path_str, str):
        return None
    raw = path_str.strip().replace("\\", "/")
    # Remove prefixos absolutos do workspace, se vierem
    try:
        root_abs = str(ROOT_DIR).replace("\\", "/")
        if raw.startswith(root_abs + "/"):
            raw = raw[len(root_abs) + 1:]
    except Exception:
        pass
    if raw.startswith("/"):
        raw = raw.lstrip("/")
    # Bloqueia escape de diretório
    parts = [p for p in raw.split("/") if p]
    if any(p == ".." for p in parts):
        return None
    candidate = ROOT_DIR.joinpath(*parts)
    # Garante que o alvo fique dentro de ROOT_DIR
    try:
        candidate.resolve().relative_to(ROOT_DIR.resolve())
    except Exception:
        return None
    return candidate


def is_path_blocked(rel_path: str) -> bool:
    """True quando o path cai em segmento bloqueado."""
    low = rel_path.lower().replace("\\", "/")
    for seg in BLOCKED_PATH_SEGMENTS:
        if seg.lower().replace("\\", "/") in low:
            return True
    return False


def is_path_protected(rel_path: str) -> bool:
    low = rel_path.replace("\\", "/").lower()
    for prot in PROTECTED_FILES:
        if low == prot.lower():
            return True
    if low == SELF_FILE_REL.lower() and not ALLOW_SELF_EDIT:
        return True
    return False


def is_path_editable(rel_path: str) -> Tuple[bool, str]:
    """Retorna (pode_editar, motivo)."""
    if is_path_blocked(rel_path):
        return False, f"path bloqueado por política: {rel_path}"
    if is_path_protected(rel_path):
        return False, f"arquivo protegido: {rel_path}"
    ext = Path(rel_path).suffix.lower()
    if ext and ext not in EDITABLE_EXTENSIONS:
        return False, f"extensão não editável ({ext}): {rel_path}"
    return True, "ok"


def command_is_safe(command: str) -> Tuple[bool, str]:
    """Retorna (seguro, motivo) aplicando a allowlist de padrões destrutivos."""
    if not command or not isinstance(command, str):
        return False, "comando vazio"
    cmd = command.strip()
    if len(cmd) > 4000:
        return False, "comando excede 4000 chars"
    low = cmd.lower()
    for pat in FORBIDDEN_SHELL_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return False, f"bloqueado por padrão destrutivo: {pat}"
    return True, "ok"


# =============================================================================
# DESCOBERTA DE SERVIÇOS — multiplataforma
# =============================================================================
# Nomes lógicos → padrão a buscar em argv/CommandLine (substring case-insensitive)
SERVICE_MARKERS: Dict[str, List[str]] = {
    "main":                  ["scripts/main.py", "scripts\\main.py", "/main.py"],
    "browser_worker":        ["scripts/browser.py", "scripts\\browser.py", "/browser.py"],
    "analisador_prontuarios": ["analisador_prontuarios.py"],
    "acompanhamento_whatsapp": ["acompanhamento_whatsapp.py"],
    "auto_dev_agent":        ["auto_dev_agent.py"],
}


def _discover_services_posix() -> Dict[str, List[int]]:
    """Descoberta via /proc (Linux) ou `ps` (macOS/BSD)."""
    result: Dict[str, List[int]] = {k: [] for k in SERVICE_MARKERS}
    # Tenta /proc primeiro
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        try:
            for entry in proc_dir.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
                except Exception:
                    continue
                if not cmdline:
                    continue
                cmdline_norm = cmdline.replace("\x00", " ").lower()
                for name, markers in SERVICE_MARKERS.items():
                    for m in markers:
                        if m.lower() in cmdline_norm:
                            result[name].append(int(entry.name))
                            break
            return result
        except Exception:
            pass
    # Fallback: ps
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid,command"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            pid = int(parts[0])
            cmd = parts[1].lower()
            for name, markers in SERVICE_MARKERS.items():
                if any(m.lower() in cmd for m in markers):
                    result[name].append(pid)
    except Exception:
        pass
    return result


def _discover_services_windows() -> Dict[str, List[int]]:
    """Descoberta via PowerShell/WMI em Windows."""
    result: Dict[str, List[int]] = {k: [] for k in SERVICE_MARKERS}
    ps_base = (
        "Get-CimInstance Win32_Process "
        "| Select-Object -Property ProcessId, CommandLine "
        "| ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_base],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=20,
        )
        data = json.loads(proc.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        for row in data:
            cmdline = (row.get("CommandLine") or "").lower()
            pid = row.get("ProcessId")
            if not cmdline or not isinstance(pid, int):
                continue
            for name, markers in SERVICE_MARKERS.items():
                if any(m.lower() in cmdline for m in markers):
                    result[name].append(pid)
                    break
    except Exception:
        pass
    return result


def discover_active_services() -> Dict[str, List[int]]:
    if IS_WINDOWS:
        svc = _discover_services_windows()
    else:
        svc = _discover_services_posix()

    # browser_worker roda como THREAD dentro do processo main.py; em muitos
    # ambientes ele não aparece como processo separado no ps/WMI. Para evitar
    # falso "OFF", aplica heurística: se main.py está ativo e o /health do
    # Simulator responde 200, considera o browser worker funcional no mesmo PID.
    if not svc.get("browser_worker") and svc.get("main") and _simulator_health_quick():
        svc["browser_worker"] = list(svc.get("main") or [])

    # Deduplica e ordena
    return {k: sorted(set(v)) for k, v in svc.items()}


def _simulator_health_quick(timeout_sec: float = 1.5) -> bool:
    """Ping curto no /health sem efeitos colaterais (sem autostart/retries longos)."""
    try:
        r = requests.get(SIMULATOR_HEALTH_URL, timeout=max(0.5, float(timeout_sec)))
        return r.status_code == 200
    except Exception:
        return False


def log_active_services_snapshot(svc_map: Dict[str, List[int]]) -> None:
    """Registra a lista de serviços ativos — SEMPRE logado a cada ciclo.

    A descoberta real acontece em collect_runtime_context() a cada ciclo,
    então esta função apenas renderiza o snapshot atual. Mantemos também a
    assinatura anterior em _AGENT_STATE para detectar mudanças (∆), mas a
    linha é emitida em todo ciclo para dar visibilidade contínua do estado
    do ambiente.
    """
    signature = json.dumps(svc_map, sort_keys=True, ensure_ascii=False)
    changed = signature != _AGENT_STATE.last_services_signature
    _AGENT_STATE.last_services_signature = signature
    parts = []
    for name, pids in svc_map.items():
        parts.append(f"{name}={pids}" if pids else f"{name}=OFF")
    marker = " (mudou)" if changed else ""
    log("🛰️ Serviços ativos: " + " | ".join(parts) + marker)


# =============================================================================
# LEITURA DE LOGS E DETECÇÃO DE INCIDENTES
# =============================================================================
def _tail_lines(path: Path, max_lines: int) -> List[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        # Leitura bloco final do arquivo para evitar carregar tudo
        size = path.stat().st_size
        read_bytes = min(size, 256_000)
        with path.open("rb") as f:
            if size > read_bytes:
                f.seek(size - read_bytes)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-max_lines:]
    except Exception:
        return []


def _recent_log_files(max_files: int) -> List[Path]:
    if not LOGS_DIR.exists():
        return []
    try:
        files = [
            p for p in LOGS_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in {".log", ".txt"}
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:max_files]
    except Exception:
        return []


def _scan_incidents(lines: Iterable[str], source: str) -> List[Incident]:
    out: List[Incident] = []
    for raw in lines:
        low = raw.lower()
        if any(re.search(p, low) for p in BENIGN_INCIDENT_PATTERNS):
            continue

        level_match = _LOG_LEVEL_TAG_RE.search(raw)
        declared_level = (level_match.group(1).lower() if level_match else "")
        if declared_level in {"error", "critical"}:
            out.append(Incident("error", source, raw.strip()[:1000], _now_utc_iso()))
            continue
        if declared_level == "warning":
            out.append(Incident("warning", source, raw.strip()[:1000], _now_utc_iso()))
            continue

        if any(re.search(p, raw) or re.search(p, low) for p in ERROR_PATTERNS):
            out.append(Incident("error", source, raw.strip()[:1000], _now_utc_iso()))
        elif declared_level not in {"info", "debug"} and any(
            re.search(p, raw) or re.search(p, low) for p in WARN_PATTERNS
        ):
            out.append(Incident("warning", source, raw.strip()[:1000], _now_utc_iso()))
    return out


def _extract_traceback_files(lines: Iterable[str]) -> List[str]:
    """Extrai paths `File "..."` de tracebacks, úteis para priorizar leitura."""
    files: List[str] = []
    pat = re.compile(r'File "([^"]+)"')
    for line in lines:
        for m in pat.finditer(line):
            files.append(m.group(1))
    # Deduplica mantendo ordem
    seen: set = set()
    out: List[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# =============================================================================
# LEITURA DE CÓDIGO-FONTE
# =============================================================================
def list_project_files() -> List[Tuple[str, int]]:
    """Retorna (rel_path, num_linhas) dos arquivos relevantes do projeto."""
    results: List[Tuple[str, int]] = []
    for ext in EDITABLE_EXTENSIONS:
        for p in ROOT_DIR.rglob(f"*{ext}"):
            try:
                rel = p.relative_to(ROOT_DIR).as_posix()
            except Exception:
                continue
            if is_path_blocked(rel):
                continue
            try:
                num = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
            except Exception:
                num = 0
            results.append((rel, num))
    results.sort()
    return results


def read_source_file(rel_path: str, max_lines: Optional[int] = None) -> Optional[str]:
    """Lê o conteúdo do arquivo; aplica truncagem de linhas se solicitado."""
    target = _normalize_rel_path(rel_path)
    if target is None:
        return None
    if is_path_blocked(rel_path):
        return None
    if not target.exists() or not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if max_lines and max_lines > 0:
        lines = text.splitlines()
        if len(lines) > max_lines:
            head = lines[: max_lines // 2]
            tail = lines[-max_lines // 2 :]
            omitted = len(lines) - len(head) - len(tail)
            text = "\n".join(head) + f"\n... [{omitted} linhas omitidas] ...\n" + "\n".join(tail)
    return text


def map_tracebacks_to_project_files(tb_files: Iterable[str]) -> List[str]:
    """Converte paths absolutos de tracebacks para rel paths do projeto."""
    rels: List[str] = []
    root_abs = str(ROOT_DIR.resolve()).replace("\\", "/").lower()
    for f in tb_files:
        norm = f.replace("\\", "/")
        low = norm.lower()
        if low.startswith(root_abs + "/"):
            rel = norm[len(root_abs) + 1:]
            if rel and not is_path_blocked(rel):
                rels.append(rel)
        else:
            # Pode ser path relativo já
            base = Path(norm).name
            if base:
                for candidate in SCRIPTS_DIR.glob(base):
                    try:
                        rels.append(candidate.relative_to(ROOT_DIR).as_posix())
                    except Exception:
                        continue
    # Dedup mantendo ordem
    seen: set = set()
    out = []
    for r in rels:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# =============================================================================
# CONSTRUÇÃO DO CONTEXTO ENVIADO AO ChatGPT
# =============================================================================
def collect_runtime_context() -> Dict[str, Any]:
    """Monta dict com: serviços, incidentes, excertos de logs, métricas."""
    incidents: List[Incident] = []
    log_excerpts: List[Dict[str, str]] = []
    tb_files_raw: List[str] = []

    total_chars = 0
    for log_file in _recent_log_files(MAX_LOG_FILES):
        lines = _tail_lines(log_file, MAX_LOG_LINES_PER_FILE)
        if not lines:
            continue
        file_incidents = _scan_incidents(lines, log_file.name)
        incidents.extend(file_incidents)
        tb_files_raw.extend(_extract_traceback_files(lines))
        excerpt = "\n".join(lines)
        block = {"file": log_file.name, "tail": excerpt}
        approx = len(excerpt) + len(log_file.name) + 32
        if total_chars + approx > MAX_CONTEXT_CHARS // 2:
            # Corta para não ultrapassar orçamento de logs (metade do contexto)
            remaining = max(0, (MAX_CONTEXT_CHARS // 2) - total_chars - 64)
            if remaining > 0:
                block["tail"] = excerpt[-remaining:]
                log_excerpts.append(block)
                total_chars += len(block["tail"]) + len(log_file.name)
            break
        log_excerpts.append(block)
        total_chars += approx

    # Arquivos do projeto (mapa resumido)
    files = list_project_files()
    file_index = [{"path": p, "lines": n} for p, n in files]

    payload: Dict[str, Any] = {
        "timestamp": _now_utc_iso(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "python": sys.version.split(" ", 1)[0],
        },
        "root_dir": str(ROOT_DIR),
        "services": discover_active_services(),
        "incidents": [inc.__dict__ for inc in incidents[-MAX_INCIDENTS_IN_CONTEXT:]],
        "log_excerpts": log_excerpts,
        "file_index": file_index[:80],  # top-80 files
        "traceback_files": map_tracebacks_to_project_files(tb_files_raw)[:12],
        "metrics": {
            "cycles_total": _AGENT_STATE.cycles_total,
            "cycles_with_errors": _AGENT_STATE.cycles_with_errors,
            "cycles_with_fixes": _AGENT_STATE.cycles_with_fixes,
            "total_actions": _AGENT_STATE.total_actions,
        },
    }
    return payload


def select_relevant_source_files(context: Dict[str, Any], budget_chars: int) -> Dict[str, str]:
    """Seleciona arquivos-fonte relevantes a incluir no prompt.

    Prioriza:
      1) Arquivos aparecendo em tracebacks recentes.
      2) Arquivos citados nos incidentes (ex.: [browser.py], [server.py], etc.).
      3) Arquivos core do sistema (main, server, browser, shared, utils, storage, auth).
      4) O próprio auto_dev_agent.py (só como referência, nunca para editar).
    """

    def _incident_file_hints() -> List[str]:
        hints: List[str] = []
        for inc in (context.get("incidents") or []):
            if not isinstance(inc, dict):
                continue
            line = str(inc.get("line") or "")
            low = line.lower()
            if "[browser.py]" in low:
                hints.append("Scripts/browser.py")
            if "[server.py]" in low:
                hints.append("Scripts/server.py")
            if "[storage.py]" in low:
                hints.append("Scripts/storage.py")
            if "[main.py]" in low:
                hints.append("Scripts/main.py")
            if "[auto_dev_agent.py]" in low:
                hints.append("Scripts/auto_dev_agent.py")
            if "[analisador_prontuarios.py]" in low:
                hints.append("Scripts/analisador_prontuarios.py")
        # Dedup mantendo ordem
        out: List[str] = []
        seen = set()
        for h in hints:
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out

    selected: Dict[str, str] = {}
    budget = max(2000, budget_chars)
    picks: List[str] = []

    picks.extend(context.get("traceback_files", []))
    picks.extend(_incident_file_hints())
    core = [
        "Scripts/main.py", "Scripts/server.py", "Scripts/browser.py", "Scripts/shared.py",
        "Scripts/utils.py", "Scripts/storage.py", "Scripts/auth.py",
    ]
    for c in core:
        if c not in picks:
            picks.append(c)

    for rel in picks:
        if rel in selected:
            continue
        if is_path_blocked(rel):
            continue
        content = read_source_file(rel, max_lines=MAX_FILE_LINES_IN_CONTEXT)
        if not content:
            continue
        snippet = content
        # Orçamento dinâmico: usa até metade do restante por arquivo
        allowed = min(len(snippet), max(1500, budget // 2))
        if len(snippet) > allowed:
            head = snippet[: allowed // 2]
            tail = snippet[-allowed // 2 :]
            snippet = head + "\n... [conteúdo truncado] ...\n" + tail
        if len(snippet) > budget:
            break
        selected[rel] = snippet
        budget -= len(snippet)
        if budget < 1500:
            break
    return selected


# =============================================================================
# COMUNICAÇÃO COM O ChatGPT via browser.py (server.py /v1/chat/completions)
# =============================================================================
_last_health_log = 0.0


def _maybe_autostart_simulator() -> None:
    """Tenta subir o Simulator automaticamente quando indisponível."""
    global _last_autostart_attempt
    if not AUTOSTART_SIMULATOR_CMD:
        return
    now = time.time()
    remaining = AUTOSTART_COOLDOWN_SEC - (now - _last_autostart_attempt)
    if remaining > 0:
        return
    _last_autostart_attempt = now
    try:
        subprocess.Popen(
            AUTOSTART_SIMULATOR_CMD,
            cwd=str(ROOT_DIR),
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(
            f"🚀 Auto-start acionado para o Simulator com comando: "
            f"{AUTOSTART_SIMULATOR_CMD!r}"
        )
    except Exception as exc:
        log(f"⚠️ Falha no auto-start do Simulator: {exc}", logging.WARNING)


def simulator_is_ready() -> bool:
    """Health-check do Simulator. Faz log throttle para não poluir."""
    global _last_health_log
    for _ in range(HEALTHCHECK_RETRIES):
        try:
            r = requests.get(SIMULATOR_HEALTH_URL, timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(HEALTHCHECK_RETRY_DELAY_SEC)

    _maybe_autostart_simulator()
    now = time.time()
    if now - _last_health_log >= HEALTH_LOG_THROTTLE_SEC:
        log(f"⏳ Simulator indisponível em {SIMULATOR_HEALTH_URL}; aguardando...")
        _last_health_log = now
    return False


def _llm_headers() -> Dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "X-Request-Source": "auto_dev_agent.py",
    }
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


SYSTEM_PROMPT_BASE = textwrap.dedent("""\
    Você é um engenheiro de software sênior atuando como AGENTE AUTÔNOMO
    responsável pela manutenção contínua do projeto "ChatGPT_Simulator".

    Seu papel:
      • Analisar o estado atual do sistema (logs, serviços, código-fonte).
      • Diagnosticar erros com precisão quando existirem.
      • Propor alterações MÍNIMAS, SEGURAS e CIRÚRGICAS.
      • Sugerir melhorias contínuas de robustez, performance e observabilidade.

    REGRAS OBRIGATÓRIAS DE RESPOSTA:
      1) Responda APENAS com JSON válido. Sem markdown. Sem prosa fora do JSON.
      2) Nunca proponha comandos destrutivos (rm -rf, git reset --hard,
         shutdown, format, dd, DROP TABLE, kill -9 1, chmod 777 -R).
      3) NUNCA edite arquivos protegidos:
         - Scripts/config.py
         - qualquer caminho em .git/, certs/, db/, logs/, chrome_profile/
      4) Em edições use 'search/replace' com trechos EXATOS do código atual.
         Inclua contexto suficiente para que 'search' seja único no arquivo.
      5) Prefira correções pequenas a refatorações grandes.
      6) Se não houver nada seguro a fazer, retorne lista 'actions' vazia
         e uma 'analysis' explicando o motivo.

    FORMATO EXATO:
    {
      "analysis": "texto curto com o diagnóstico/raciocínio",
      "should_forward_to_codex": false,
      "actions": [
        {
          "type": "edit_file",
          "file": "Scripts/server.py",
          "description": "explica o que muda e por quê",
          "search": "trecho exato presente no arquivo hoje",
          "replace": "texto que substituirá o search"
        },
        {
          "type": "create_file",
          "file": "Scripts/novo_modulo.py",
          "description": "por que criar este arquivo",
          "content": "conteúdo completo do novo arquivo"
        },
        {
          "type": "shell",
          "command": "python -m py_compile Scripts/server.py",
          "description": "validação rápida"
        },
        {
          "type": "note",
          "content": "observação ou recomendação para humanos"
        }
      ]
    }

    REGRAS PARA "should_forward_to_codex":
      • Campo OBRIGATÓRIO em TODA resposta.
      • true  = o auto_dev_agent.py DEVE encaminhar este caso ao Codex para
                tentar implementação concreta (quando você só trouxe diagnóstico,
                notas, ou quando as ações propostas tendem a falhar sem contexto
                adicional de execução no Codex).
      • true  = também quando houver sugestão de melhoria/alteração de código
                (ex.: edit_file/create_file ou recomendação equivalente) e você
                quiser que o agente realmente tente implementar no Codex.
      • false = não encaminhar ao Codex neste ciclo.
    """)

def _build_user_prompt(context: Dict[str, Any],
                       objective: str,
                       source_files: Dict[str, str],
                       prior_attempt: Optional[Dict[str, Any]] = None) -> str:
    ctx_for_prompt = {
        k: v for k, v in context.items()
        if k in {"timestamp", "platform", "services", "incidents",
                 "log_excerpts", "file_index", "traceback_files", "metrics"}
    }
    # Compactação: deixa o JSON legível mas limita fatia de logs
    ctx_json = json.dumps(ctx_for_prompt, ensure_ascii=False)

    parts: List[str] = []
    parts.append(f"OBJETIVO DESTE CICLO:\n{objective}\n")
    parts.append("CONTEXTO DE EXECUÇÃO (JSON):\n" + ctx_json + "\n")

    if source_files:
        parts.append("CÓDIGO-FONTE RELEVANTE:\n")
        for rel, content in source_files.items():
            parts.append(f"--- BEGIN FILE: {rel} ---\n{content}\n--- END FILE: {rel} ---\n")

    if prior_attempt:
        parts.append("RESULTADO DA TENTATIVA ANTERIOR (FALHOU):\n" +
                     json.dumps(prior_attempt, ensure_ascii=False) + "\n")
        parts.append("Reavalie: proponha um plano DIFERENTE que corrija o motivo da falha.")

    parts.append("Responda apenas com JSON no formato especificado.")
    return "\n".join(parts)


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # remove fences tipo ```json ... ```
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _extract_json_object(text: str) -> Optional[dict]:
    """Extrai o objeto JSON mais provável de plano dentro de texto misto.

    Estratégia:
      1) tenta parse direto;
      2) tenta parse após marcadores como "RESPOSTA:";
      3) varre candidatos com json.JSONDecoder().raw_decode() a partir de '{';
      4) ranqueia candidatos privilegiando schema do plano (analysis/actions/forward).
    """
    if not text:
        return None
    raw = _strip_code_fences(text)
    decoder = json.JSONDecoder()

    def _score_candidate(data: dict) -> int:
        score = 0
        if isinstance(data.get("analysis"), str):
            score += 3
        if isinstance(data.get("actions"), list):
            score += 4
        if "should_forward_to_codex" in data:
            score += 4
        if "type" in data and "file" in data:
            score -= 2
        return score

    candidates: List[dict] = []

    # Tentativa direta
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            candidates.append(data)
    except Exception:
        pass

    # Tenta parse após marcadores frequentes.
    marker_candidates = ["\nRESPOSTA:", "\nResposta:", "\nJSON:", "responda apenas com json"]
    lower_raw = raw.lower()
    for marker in marker_candidates:
        pos = lower_raw.rfind(marker.lower())
        if pos == -1:
            continue
        chunk = raw[pos + len(marker):].strip()
        if not chunk:
            continue
        try:
            data = json.loads(chunk)
            if isinstance(data, dict):
                candidates.append(data)
                continue
        except Exception:
            pass
        brace = chunk.find("{")
        if brace != -1:
            try:
                obj, _end = decoder.raw_decode(chunk[brace:])
                if isinstance(obj, dict):
                    candidates.append(obj)
            except Exception:
                pass

    # Varrida geral por objetos JSON possíveis.
    start = raw.find("{")
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(raw[start:])
            if isinstance(obj, dict):
                candidates.append(obj)
        except Exception:
            pass
        start = raw.find("{", start + 1)

    if not candidates:
        return None
    candidates.sort(key=_score_candidate, reverse=True)
    return candidates[0]


def _normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza o JSON de plano para contrato mínimo esperado pelo agente."""
    if not isinstance(plan.get("actions"), list):
        plan["actions"] = []
    if "analysis" not in plan:
        plan["analysis"] = ""
    if "should_forward_to_codex" not in plan:
        # Fallback estrutural (sem palavras-chave): se vier sem ações executáveis
        # (edit/create/shell), assume que deve encaminhar ao Codex.
        actionable_types = {"edit_file", "create_file", "shell"}
        change_types = {"edit_file", "create_file"}
        has_actionable = any(
            isinstance(a, dict) and str(a.get("type", "")).lower().strip() in actionable_types
            for a in (plan.get("actions") or [])
        )
        has_change_intent = any(
            isinstance(a, dict) and str(a.get("type", "")).lower().strip() in change_types
            for a in (plan.get("actions") or [])
        )
        # Na ausência do campo obrigatório, privilegia encaminhar quando há
        # intenção explícita de alteração de código OU quando não há ações
        # executáveis locais.
        plan["should_forward_to_codex"] = has_change_intent or (not has_actionable)
        plan["_forward_autofilled"] = True
    else:
        plan["should_forward_to_codex"] = bool(plan.get("should_forward_to_codex"))
        plan["_forward_autofilled"] = False
    return plan


def _log_plan_decision(plan: Dict[str, Any], origin: str) -> None:
    """Loga análise completa + decisão de ações/forward para facilitar leitura."""
    analysis = str(plan.get("analysis") or "").strip()
    actions = plan.get("actions") or []
    should_forward = bool(plan.get("should_forward_to_codex"))
    if bool(plan.get("_forward_autofilled")):
        log(
            f"ℹ️ [{origin}] should_forward_to_codex ausente no JSON; decisão foi auto-preenchida.",
            logging.INFO,
        )

    log(f"🧠 [{origin}] analysis completo:\n{analysis or '(vazio)'}")
    if actions:
        action_types = [
            str(a.get("type", "unknown"))
            for a in actions if isinstance(a, dict)
        ]
        log(
            f"🛠️ [{origin}] ações sugeridas: {len(actions)} "
            f"({', '.join(action_types) if action_types else 'sem tipo'})"
        )
    else:
        log(f"🛠️ [{origin}] nenhuma ação sugerida (actions=[]).")

    decision_text = "ENCAMINHAR para Codex" if should_forward else "NÃO encaminhar para Codex"
    log(f"🔁 [{origin}] should_forward_to_codex={should_forward} → {decision_text}")


def _plan_has_change_intent(plan: Dict[str, Any]) -> bool:
    """Detecta intenção de mudança sem depender de busca por palavras-chave."""
    for action in plan.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("type", "")).lower().strip() in {"edit_file", "create_file"}:
            return True
    return False


def _should_forward_plan_to_codex(plan: Dict[str, Any],
                                  results: List[ActionResult],
                                  changed: List[str]) -> bool:
    """Decide forward com base no sinal do LLM + fallback estrutural robusto."""
    if changed:
        return False
    if bool(plan.get("should_forward_to_codex")):
        return True
    # Fallback robusto: se o plano já trouxe intenção concreta de alteração
    # mas não houve nenhuma mudança aplicada, encaminha ao Codex.
    if _plan_has_change_intent(plan):
        has_failed_change = any(
            (not r.ok) and r.action_type in {"edit_file", "create_file"}
            for r in results
        )
        if has_failed_change:
            return True
    return False


def _stream_chat_completion(
    body: Dict[str, Any],
    label: str = "ChatGPT Simulator",
    apply_chat_spacing: bool = True,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Envia mensagem em modo streaming e coleta (markdown_final, chat_id, url).

    Usa stream=True para evitar timeouts longos do servidor (que pode aguardar
    cooldown de rate-limit) e tolera pausas até STREAM_IDLE_TIMEOUT_SEC.

    Agora também emite logs ao longo do ciclo de vida da requisição — envio,
    status, mensagens do browser, chat_id/url, progresso da resposta e fim
    ou erro — espelhando o padrão usado por analisador_prontuarios.py para
    dar ao usuário visibilidade em tempo real no CMD do agente.
    """
    body = dict(body)
    body["stream"] = True
    markdown_buf: str = ""
    chat_id: Optional[str] = None
    chat_url: Optional[str] = None
    error_msg: Optional[str] = None

    # Tamanho aproximado do payload em chars (útil para correlacionar com
    # o tempo de envio/colagem no browser).
    try:
        payload_chars = len(str(body.get("message") or ""))
        if not payload_chars and isinstance(body.get("messages"), list):
            payload_chars = sum(
                len(str(m.get("content") or "")) for m in body["messages"]
            )
    except Exception:
        payload_chars = 0

    reuse_hint = ""
    if body.get("chat_id") or body.get("url"):
        ref = str(body.get("chat_id") or body.get("url") or "")[-12:]
        reuse_hint = f" [continuando ...{ref}]"

    log(f"📤 Enviando pedido a {label} "
        f"(~{payload_chars} chars){reuse_hint}...")

    if apply_chat_spacing:
        _wait_chat_spacing_if_needed(label)

    resp = None
    post_error: Optional[Exception] = None
    backoff_base = 5
    for attempt in range(1, max(1, STREAM_REQUEST_RETRIES) + 1):
        try:
            resp = requests.post(
                SIMULATOR_URL,
                headers=_llm_headers(),
                json=body,
                stream=True,
                timeout=(30, STREAM_IDLE_TIMEOUT_SEC),
            )
            post_error = None
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            post_error = exc
            if attempt >= max(1, STREAM_REQUEST_RETRIES):
                break
            wait_s = backoff_base * (2 ** (attempt - 1))
            log(
                f"⚠️ erro ao consultar ChatGPT ({label}) [tentativa {attempt}/"
                f"{max(1, STREAM_REQUEST_RETRIES)}]: {exc}. "
                f"Backoff exponencial: aguardando {wait_s}s...",
                logging.WARNING,
            )
            time.sleep(wait_s)
    if resp is None:
        raise RuntimeError(f"falha ao consultar ChatGPT após retries: {post_error}")
    try:
        resp.raise_for_status()
    except Exception as exc:
        detail = ""
        try:
            detail = resp.text[:1200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {resp.status_code} no Simulator: {exc} | {detail}")

    log(f"📡 Conexão com {label} aberta (HTTP {resp.status_code}); "
        f"aguardando eventos do stream...")

    # Controle de verbosidade para eventos repetitivos (status/markdown).
    last_status_logged: str = ""
    last_markdown_size_reported = -1
    last_markdown_report_ts = 0.0
    MARKDOWN_REPORT_MIN_STEP = 1024   # chars
    MARKDOWN_REPORT_MIN_INTERVAL = 3  # segundos
    stream_verbose = "codex" in (label or "").lower()
    inline_status_open = False
    inline_last_len = 0
    stream = getattr(sys, "stdout", None)
    def _clean_browser_prefix(text: str) -> str:
        s = (text or "").strip()
        s = re.sub(r"^\s*Remetente:\s*[^|]+\|\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"(\[browser\.py\])\s+\[[^\]]+\]\s+", r"\1 ", s, flags=re.IGNORECASE)
        return s

    def _normalize_status(text: str) -> str:
        s = (text or "").strip()
        s = re.sub(r"^\s*Remetente:\s*[^|]+\|\s*", "", s, flags=re.IGNORECASE)
        cooldown_match = re.search(r"nova tentativa em\s*([0-9]{1,2}:[0-9]{2})", s, flags=re.IGNORECASE)
        if cooldown_match:
            return f"Aguardando cooldown do ChatGPT | nova tentativa em {cooldown_match.group(1)}"
        return s

    def _print_inline_status(text: str) -> None:
        nonlocal inline_status_open, inline_last_len
        try:
            rendered = f"   ⏳ status: {_normalize_status(text)[:220]}"
            clear_pad = max(0, inline_last_len - len(rendered))
            sys.stdout.write("\r" + rendered + (" " * (clear_pad + 8)))
            sys.stdout.flush()
            inline_status_open = True
            inline_last_len = len(rendered)
        except Exception:
            log(f"   ⏳ status: {_normalize_status(text)[:220]}")

    def _print_inline_markdown(size: int) -> None:
        nonlocal inline_status_open, inline_last_len
        try:
            rendered = f"   📝 recebendo resposta: {size} chars..."
            clear_pad = max(0, inline_last_len - len(rendered))
            sys.stdout.write("\r" + rendered + (" " * (clear_pad + 8)))
            sys.stdout.flush()
            inline_status_open = True
            inline_last_len = len(rendered)
        except Exception:
            log(f"   📝 recebendo resposta: {size} chars...")

    def _close_inline_status() -> None:
        nonlocal inline_status_open, inline_last_len
        if inline_status_open:
            try:
                sys.stdout.write("\r" + " " * (inline_last_len + 16) + "\r")
                sys.stdout.flush()
            except Exception:
                pass
            finally:
                inline_status_open = False
                inline_last_len = 0

    started = time.time()
    last_event = started
    for raw_line in resp.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue
        if not raw_line.strip():
            # Checa idle timeout manual
            if time.time() - last_event > STREAM_IDLE_TIMEOUT_SEC:
                raise TimeoutError("stream idle timeout aguardando eventos do Simulator")
            continue
        last_event = time.time()
        try:
            msg = json.loads(raw_line)
        except Exception:
            continue
        t = msg.get("type")
        c = msg.get("content")

        if t == "status":
            text = (str(c) if c is not None else "").strip()
            if text and text != last_status_logged:
                if stream_verbose:
                    _close_inline_status()
                    log(f"   ⏳ {_normalize_status(text)[:320]}")
                else:
                    _print_inline_status(text)
                last_status_logged = text

        elif t == "log":
            text = (str(c) if c is not None else "").strip()
            if text and "screenshot stream" not in text.lower():
                _close_inline_status()
                log(f"   🔧 {_clean_browser_prefix(text)[:320]}")

        elif t == "chat_id" and isinstance(c, str):
            _close_inline_status()
            chat_id = c
            log(f"   📎 chat_id: {c}")

        elif t == "chat_meta" and isinstance(c, dict):
            _close_inline_status()
            chat_url = c.get("url") or chat_url
            chat_id = c.get("chat_id") or chat_id
            if c.get("url"):
                log(f"   🔗 chat_url: {c.get('url')}")

        elif t == "markdown" and isinstance(c, str):
            markdown_buf = c
            size = len(c)
            now = time.time()
            if (
                size == 0
                or size - last_markdown_size_reported >= MARKDOWN_REPORT_MIN_STEP
                or (size != last_markdown_size_reported
                    and now - last_markdown_report_ts >= MARKDOWN_REPORT_MIN_INTERVAL)
            ):
                if stream_verbose:
                    _close_inline_status()
                    log(f"   📝 recebendo resposta: {size} chars...")
                else:
                    _print_inline_markdown(size)
                last_markdown_size_reported = size
                last_markdown_report_ts = now

        elif t == "finish" and isinstance(c, dict):
            _close_inline_status()
            chat_url = c.get("url") or chat_url
            total_chars = len(markdown_buf)
            elapsed = time.time() - started
            log(f"   ✅ resposta concluída: {total_chars} chars "
                f"em {elapsed:.1f}s | url={chat_url}")

        elif t == "error":
            _close_inline_status()
            error_msg = str(c)
            log(f"   ❌ erro recebido: {error_msg[:260]}", logging.WARNING)
            # Detecta rate limit e aplica cooldown ANTES de abortar
            is_rl, retry_after, reason = _parse_rate_limit(c)
            if is_rl:
                _apply_rate_limit_cooldown(retry_after, reason)
            break

        # Hard timeout total
        if time.time() - started > REQUEST_TIMEOUT_SEC:
            raise TimeoutError("timeout total excedido aguardando resposta do ChatGPT")

    if error_msg and not markdown_buf:
        raise RuntimeError(f"ChatGPT retornou erro: {error_msg}")
    _close_inline_status()
    return markdown_buf, chat_id, chat_url


def _wrap_for_paste(text: str) -> str:
    """Encapsula texto nos marcadores que browser.py reconhece como 'colar via Ctrl+V'.

    Quando o texto vai entre [INICIO_TEXTO_COLADO] e [FIM_TEXTO_COLADO],
    browser.py injeta o bloco inteiro no clipboard e executa Ctrl+V — várias
    ordens de grandeza mais rápido que digitação caractere a caractere.

    Regras:
      • Respeita USE_PASTE_MARKERS (env AUTODEV_AGENT_USE_PASTE_MARKERS).
      • Não re-encapsula texto que já contenha os marcadores (idempotente).
      • Ignora entradas vazias.
    """
    if not text:
        return text
    if not USE_PASTE_MARKERS:
        return text
    # Idempotência: se já veio encapsulado, não duplica
    if PASTE_MARKER_START in text and PASTE_MARKER_END in text:
        return text
    return f"{PASTE_MARKER_START}{text}{PASTE_MARKER_END}"


# =============================================================================
# RATE LIMIT — cooldown compartilhado entre ciclos
# =============================================================================
_rate_limit_lock = threading.Lock()
_rate_limit_until_ts: float = 0.0
_last_rate_limit_log_ts: float = 0.0
_chat_spacing_lock = threading.Lock()
_last_chat_request_ts: float = 0.0
_chat_spacing_backoff_level: int = 0


def _apply_rate_limit_cooldown(retry_after: Optional[float], reason: str = "") -> None:
    """Marca cooldown global para evitar consultas durante rate limit do ChatGPT."""
    global _rate_limit_until_ts, _last_rate_limit_log_ts
    wait = 240.0  # default conservador (4 min) — alinhado com server.py
    try:
        if retry_after is not None:
            wait = max(30.0, float(retry_after))
    except Exception:
        pass
    new_ts = time.time() + wait
    with _rate_limit_lock:
        if new_ts > _rate_limit_until_ts:
            _rate_limit_until_ts = new_ts
    now = time.time()
    if now - _last_rate_limit_log_ts >= 10:
        log(f"🧊 Rate-limit do ChatGPT detectado: cooldown de {int(wait)}s "
            f"({reason.strip()[:160]})", logging.WARNING)
        _last_rate_limit_log_ts = now


def _rate_limit_remaining() -> float:
    with _rate_limit_lock:
        return max(0.0, _rate_limit_until_ts - time.time())


def _wait_chat_spacing_if_needed(label: str = "ChatGPT") -> None:
    """Impõe intervalo humano entre requisições ao ChatGPT neste agente."""
    global _last_chat_request_ts, _chat_spacing_backoff_level
    pause_min = max(0, int(AUTODEV_CHAT_PAUSA_MIN_SEC))
    pause_max = max(pause_min, int(AUTODEV_CHAT_PAUSA_MAX_SEC))

    with _chat_spacing_lock:
        now = time.time()
        if _last_chat_request_ts <= 0:
            _last_chat_request_ts = now
            return

        target_gap = random.uniform(pause_min, pause_max)
        elapsed = now - _last_chat_request_ts
        remaining = target_gap - elapsed
        if remaining > 0:
            backoff_multiplier = 2 ** min(3, max(0, _chat_spacing_backoff_level))
            adjusted_wait = min(300.0, remaining * backoff_multiplier)
            log(
                f"⏸️  Intervalo anti-rate-limit ({label}, backoff x{backoff_multiplier}): "
                f"aguardando {int(adjusted_wait)}s "
                f"(alvo {int(target_gap)}s, já decorridos {int(elapsed)}s).",
                logging.INFO,
            )
            time.sleep(adjusted_wait)
            now = time.time()
            _chat_spacing_backoff_level = min(6, _chat_spacing_backoff_level + 1)
        else:
            # Se a janela já foi respeitada naturalmente, zera o backoff.
            _chat_spacing_backoff_level = 0

        _last_chat_request_ts = now


def _looks_like_false_positive_rate_limit(msg: str) -> bool:
    """True se a 'mensagem' de rate-limit parece ter vindo de sidebar/página inteira.

    Sinais de falso positivo do detector do browser.py:
      • mensagem muito longa (>500 chars): banners reais são curtos;
      • contém rótulos de UI (ex.: 'Novo chat', 'Busca em chats');
      • não contém nenhuma frase de ação típica de rate limit.
    """
    if not msg:
        return False
    m = msg.lower()
    if len(m) > 500:
        return True
    ui_labels = ("novo chat", "busca em chats", "biblioteca", "relatórios de chats",
                 "new chat", "search chats", "library")
    if any(label in m for label in ui_labels):
        action_words = ("aguarde", "minuto", "minute", "wait", "try again",
                        "exceeded", "reached", "limit reached")
        if not any(a in m for a in action_words):
            return True
    return False


def _parse_rate_limit(payload: Any) -> Tuple[bool, Optional[float], str]:
    """Detecta erros de rate limit em objetos/strings. Retorna (is_rl, retry_after, motivo).

    Também filtra falsos positivos do detector heurístico do browser.py — quando
    a "mensagem" é o texto da sidebar inteira (sem frases de ação típicas), o
    payload é tratado como erro genérico (não aplica cooldown global).
    """
    if payload is None:
        return False, None, ""
    if isinstance(payload, dict):
        code = str(payload.get("code", "") or "").lower()
        msg = str(payload.get("message", "") or "")
        retry = payload.get("retry_after_seconds") or payload.get("retry_after")
        combined = f"{code} {msg}".lower()
        looks_rl = (
            ("rate" in code and "limit" in code)
            or "rate_limit" in combined
            or "too_many_requests" in combined
            or "excesso de solicita" in combined
        )
        if looks_rl and _looks_like_false_positive_rate_limit(msg):
            log(f"⚠️ Payload 'rate_limit' parece falso positivo (ignorado): "
                f"{msg[:160]!r}", logging.WARNING)
            return False, None, ""
        if looks_rl:
            try:
                retry_f = float(retry) if retry is not None else None
            except Exception:
                retry_f = None
            return True, retry_f, msg or code
        return False, None, ""
    text = str(payload)
    t = text.lower()
    if "rate_limit" in t or "rate limit" in t or "excesso de solicita" in t:
        if _looks_like_false_positive_rate_limit(text):
            log(f"⚠️ Texto 'rate_limit' parece falso positivo (ignorado): "
                f"{text[:160]!r}", logging.WARNING)
            return False, None, ""
        retry_match = re.search(r"retry_after[^0-9]*([0-9]+(?:\.[0-9]+)?)", t)
        retry_f = float(retry_match.group(1)) if retry_match else None
        return True, retry_f, text[:200]
    return False, None, ""


def ask_chatgpt_for_plan(context: Dict[str, Any],
                         objective: str,
                         source_files: Dict[str, str],
                         prior_attempt: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Consulta o ChatGPT e devolve um plano (dict) com 'analysis' + 'actions'."""
    if not simulator_is_ready():
        return None

    # Respeita cooldown de rate-limit: não envia nada enquanto estiver em cooldown.
    remaining = _rate_limit_remaining()
    if remaining > 0:
        log(f"⏸️  Em cooldown de rate-limit ({int(remaining)}s restantes); pulando consulta.",
            logging.INFO)
        return None

    user_prompt = _build_user_prompt(context, objective, source_files, prior_attempt)
    # Consolida system + user num ÚNICO bloco encapsulado.
    # Isso garante que todo o payload seja colado via Ctrl+V em UMA operação,
    # sem texto "entre blocos" que cairia na digitação realista do browser.py.
    combined = (
        "=== INSTRUÇÕES DO SISTEMA ===\n"
        f"{SYSTEM_PROMPT_BASE}\n"
        "=== FIM DAS INSTRUÇÕES DO SISTEMA ===\n\n"
        f"{user_prompt}"
    )
    wrapped = _wrap_for_paste(combined)
    body: Dict[str, Any] = {
        "model": SIMULATOR_MODEL,
        "message": wrapped,  # server.py aceita 'message' (string) OU 'messages' (array)
        "messages": [
            {"role": "user", "content": wrapped},
        ],
        "temperature": 0.2,
        "request_source": "auto_dev_agent.py",
    }
    # Reuso de conversa (continua o mesmo chat entre ciclos)
    if REUSE_CHAT_CONVERSATION:
        if _AGENT_STATE.chat_id:
            body["chat_id"] = _AGENT_STATE.chat_id
        if _AGENT_STATE.chat_url:
            body["url"] = _AGENT_STATE.chat_url
    # Se um Codex URL foi configurado explicitamente e ainda não há conversa,
    # usa ele como URL inicial.
    if CODEX_URL and not body.get("url"):
        body["url"] = CODEX_URL
        body["origin_url"] = CODEX_URL

    try:
        markdown, chat_id, chat_url = _stream_chat_completion(
            body, label="ChatGPT Simulator (diagnóstico)"
        )
    except Exception as exc:
        log(f"❌ Falha na comunicação com Simulator/ChatGPT: {exc}", logging.ERROR)
        return None

    # Atualiza conversa ativa para próximos ciclos
    if REUSE_CHAT_CONVERSATION:
        if chat_id and chat_id != _AGENT_STATE.chat_id:
            _AGENT_STATE.chat_id = chat_id
        if chat_url and chat_url != _AGENT_STATE.chat_url:
            _AGENT_STATE.chat_url = chat_url
        _save_state()

    plan = _extract_json_object(markdown or "")
    if not plan:
        log("⚠️ ChatGPT respondeu sem JSON válido — resposta ignorada neste ciclo.",
            logging.WARNING)
        return None
    plan = _normalize_plan(plan)
    _log_plan_decision(plan, "ChatGPT")
    return plan


# =============================================================================
# BACKUP E ROLLBACK DE ARQUIVOS
# =============================================================================
class FileBackup:
    """Snapshot dos arquivos antes de modificá-los, para rollback atômico."""

    def __init__(self) -> None:
        self.session_dir = BACKUP_DIR / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._originals: Dict[str, Optional[bytes]] = {}

    def snapshot(self, rel_path: str) -> None:
        if rel_path in self._originals:
            return
        target = _normalize_rel_path(rel_path)
        if target is None:
            return
        try:
            if target.exists() and target.is_file():
                data = target.read_bytes()
                self._originals[rel_path] = data
                # Mesma estrutura de diretórios dentro do backup
                dest = self.session_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            else:
                self._originals[rel_path] = None  # arquivo não existia
        except Exception as exc:
            log(f"⚠️ Falha ao snapshotar {rel_path}: {exc}", logging.WARNING)

    def rollback_all(self) -> List[str]:
        restored: List[str] = []
        for rel_path, data in self._originals.items():
            target = _normalize_rel_path(rel_path)
            if target is None:
                continue
            try:
                if data is None:
                    # Arquivo foi criado pelo agente — remover
                    if target.exists():
                        target.unlink()
                        restored.append(f"rm {rel_path}")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    restored.append(f"restored {rel_path}")
            except Exception as exc:
                log(f"⚠️ Rollback falhou em {rel_path}: {exc}", logging.WARNING)
        return restored

    @property
    def changed_files(self) -> List[str]:
        return list(self._originals.keys())


# =============================================================================
# EXECUÇÃO DE AÇÕES PROPOSTAS
# =============================================================================
def run_shell(command: str, timeout: int = MAX_SHELL_TIMEOUT_SEC,
              cwd: Optional[Path] = None) -> Tuple[int, str]:
    safe, reason = command_is_safe(command)
    if not safe:
        return 2, f"blocked by safety policy: {reason}"
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd or ROOT_DIR),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "")[-6000:]
    except subprocess.TimeoutExpired as exc:
        return 124, f"timeout após {timeout}s: {exc}"
    except Exception as exc:
        return 1, f"exceção: {exc}"


def _apply_edit_file(action: Dict[str, Any], backup: FileBackup) -> ActionResult:
    rel = str(action.get("file") or action.get("file_path") or "").strip()
    description = str(action.get("description", ""))
    search = action.get("search")
    replace = action.get("replace")
    if not rel or not isinstance(search, str) or not isinstance(replace, str):
        return ActionResult("edit_file", False, description,
                            "campos 'file', 'search' e 'replace' são obrigatórios")

    ok, reason = is_path_editable(rel)
    if not ok:
        return ActionResult("edit_file", False, description, reason)

    target = _normalize_rel_path(rel)
    if target is None or not target.exists():
        return ActionResult("edit_file", False, description, f"arquivo inexistente: {rel}")

    try:
        original = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return ActionResult("edit_file", False, description, f"falha ao ler: {exc}")

    count = original.count(search)
    if count == 0:
        return ActionResult("edit_file", False, description,
                            "padrão 'search' não encontrado no arquivo atual")
    if count > 1:
        return ActionResult("edit_file", False, description,
                            f"padrão 'search' aparece {count} vezes; exige contexto único")

    new_content = original.replace(search, replace, 1)
    if len(new_content.encode("utf-8")) > MAX_EDIT_SIZE_BYTES:
        return ActionResult("edit_file", False, description,
                            f"arquivo resultante excede {MAX_EDIT_SIZE_BYTES} bytes")

    backup.snapshot(rel)
    try:
        target.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        return ActionResult("edit_file", False, description, f"falha ao escrever: {exc}")
    return ActionResult("edit_file", True, description, "edição aplicada", changed_files=[rel])


def _apply_create_file(action: Dict[str, Any], backup: FileBackup) -> ActionResult:
    rel = str(action.get("file") or action.get("file_path") or "").strip()
    description = str(action.get("description", ""))
    content = action.get("content")
    if not rel or not isinstance(content, str):
        return ActionResult("create_file", False, description,
                            "campos 'file' e 'content' são obrigatórios")
    ok, reason = is_path_editable(rel)
    if not ok:
        return ActionResult("create_file", False, description, reason)
    if len(content.encode("utf-8")) > MAX_EDIT_SIZE_BYTES:
        return ActionResult("create_file", False, description,
                            f"content excede {MAX_EDIT_SIZE_BYTES} bytes")
    target = _normalize_rel_path(rel)
    if target is None:
        return ActionResult("create_file", False, description, f"path inválido: {rel}")
    if target.exists():
        return ActionResult("create_file", False, description,
                            f"arquivo já existe: {rel} (use edit_file)")
    backup.snapshot(rel)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except Exception as exc:
        return ActionResult("create_file", False, description, f"falha ao criar: {exc}")
    return ActionResult("create_file", True, description, "arquivo criado", changed_files=[rel])


def _apply_shell(action: Dict[str, Any]) -> ActionResult:
    cmd = str(action.get("command", "")).strip()
    description = str(action.get("description", ""))
    if not cmd:
        return ActionResult("shell", False, description, "comando vazio")
    code, out = run_shell(cmd)
    ok = code == 0
    return ActionResult("shell", ok, description, f"exit={code} out={out[-1500:]}")


def _apply_note(action: Dict[str, Any]) -> ActionResult:
    content = str(action.get("content") or action.get("note") or action.get("description") or "")
    log(f"📝 Nota do ChatGPT: {content}")
    return ActionResult("note", True, "nota registrada", content)


def execute_plan(plan: Dict[str, Any], backup: FileBackup) -> List[ActionResult]:
    results: List[ActionResult] = []
    actions = plan.get("actions") or []
    if not isinstance(actions, list):
        return [ActionResult("invalid", False, "plano sem lista 'actions'", "")]
    for action in actions[:MAX_ACTIONS_PER_CYCLE]:
        if not isinstance(action, dict):
            continue
        typ = str(action.get("type", "note")).lower().strip()
        try:
            if typ == "edit_file":
                if not ENABLE_AUTOFIX:
                    results.append(ActionResult("edit_file", False,
                                                str(action.get("description", "")),
                                                "AUTODEV_AGENT_AUTOFIX=0"))
                    continue
                results.append(_apply_edit_file(action, backup))
            elif typ == "create_file":
                if not ENABLE_AUTOFIX:
                    results.append(ActionResult("create_file", False,
                                                str(action.get("description", "")),
                                                "AUTODEV_AGENT_AUTOFIX=0"))
                    continue
                results.append(_apply_create_file(action, backup))
            elif typ == "shell":
                results.append(_apply_shell(action))
            elif typ == "note":
                results.append(_apply_note(action))
            else:
                results.append(ActionResult(typ, False,
                                            str(action.get("description", "")),
                                            f"tipo de ação desconhecido: {typ}"))
        except Exception as exc:
            results.append(ActionResult(typ, False,
                                        str(action.get("description", "")),
                                        f"exceção: {exc}"))
    return results


# =============================================================================
# VALIDAÇÃO PÓS-APLICAÇÃO
# =============================================================================
def validate_python_syntax() -> Tuple[bool, List[str]]:
    """Compila todos os .py editáveis. Retorna (ok, lista_de_erros)."""
    import py_compile
    errors: List[str] = []
    for p in ROOT_DIR.rglob("*.py"):
        try:
            rel = p.relative_to(ROOT_DIR).as_posix()
        except Exception:
            continue
        if is_path_blocked(rel):
            continue
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{rel}: {exc.msg.strip()}")
        except Exception as exc:
            errors.append(f"{rel}: {exc}")
    return (len(errors) == 0), errors


def validate_changes(changed: Iterable[str]) -> Tuple[bool, Dict[str, Any]]:
    report: Dict[str, Any] = {"py_compile": None, "changed": list(changed)}
    ok, errs = validate_python_syntax()
    report["py_compile"] = {"ok": ok, "errors": errs}
    return ok, report


# =============================================================================
# GIT — COMMIT + PUSH AUTOMÁTICOS
# =============================================================================
def _git_current_branch() -> Optional[str]:
    code, out = run_shell("git rev-parse --abbrev-ref HEAD", timeout=15)
    if code == 0:
        return out.strip().splitlines()[-1].strip() or None
    return None


def _git_has_changes() -> bool:
    code, out = run_shell("git status --porcelain", timeout=30)
    return code == 0 and bool(out.strip())


def _build_commit_message(plan: Dict[str, Any], results: List[ActionResult]) -> str:
    analysis = (plan.get("analysis") or "").strip()
    summary = analysis.splitlines()[0][:90] if analysis else "ajuste automático"
    body_lines = []
    for r in results:
        icon = "✅" if r.ok else "⚠️"
        desc = (r.description or "").strip()
        body_lines.append(f"{icon} [{r.action_type}] {desc}")
    body = "\n".join(body_lines)
    return f"{COMMIT_PREFIX} {summary}\n\n{body}"


def git_commit_and_maybe_push(plan: Dict[str, Any], results: List[ActionResult]) -> None:
    if not ENABLE_AUTOCOMMIT:
        return
    if not _git_has_changes():
        return

    branch_target = GIT_BRANCH or _git_current_branch() or ""
    if branch_target and branch_target != (_git_current_branch() or ""):
        code, out = run_shell(f"git checkout {branch_target}", timeout=30)
        if code != 0:
            log(f"⚠️ Falha ao checkout em {branch_target}: {out}", logging.WARNING)
            return

    run_shell("git add -A", timeout=30)
    commit_msg = _build_commit_message(plan, results)
    # Escreve mensagem em arquivo temporário para suportar quebras de linha
    msg_file = TEMP_DIR / f"commit_msg_{int(time.time())}.txt"
    try:
        msg_file.write_text(commit_msg, encoding="utf-8")
        code, out = run_shell(f'git commit -F "{msg_file.as_posix()}"', timeout=60)
        if code != 0:
            log(f"⚠️ git commit falhou: {out}", logging.WARNING)
            return
        log(f"📦 Commit efetuado: {commit_msg.splitlines()[0]}")
    finally:
        try:
            msg_file.unlink()
        except Exception:
            pass

    if ENABLE_AUTOPUSH:
        branch = GIT_BRANCH or _git_current_branch() or ""
        if not branch:
            log("⚠️ Branch atual indeterminado; push abortado.", logging.WARNING)
            return
        # Retry com backoff exponencial
        delays = [0, 2, 4, 8, 16]
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)
            code, out = run_shell(f"git push -u {GIT_REMOTE} {branch}", timeout=120)
            if code == 0:
                log(f"🚀 Push OK em {GIT_REMOTE}/{branch}")
                return
            log(f"⚠️ Push tentativa {attempt} falhou: {out[-400:]}", logging.WARNING)
        log("❌ Push abortado após retries.", logging.ERROR)


# =============================================================================
# ORQUESTRAÇÃO DE CICLOS
# =============================================================================
def _objective_for_cycle(has_errors: bool, incidents_summary: str) -> str:
    if has_errors:
        return (
            "Diagnosticar e corrigir erros de execução detectados recentemente "
            "nos logs do sistema, preservando comportamento existente. "
            "Foque em correções cirúrgicas de causa-raiz.\n"
            f"Principais sintomas:\n{incidents_summary}"
        )
    return (
        "Analisar o estado do sistema e propor UMA melhoria pequena, segura e "
        "incremental de robustez, performance, observabilidade (logs/métricas) "
        "ou qualidade de código. Se já estiver tudo estável e não houver algo "
        "claramente valioso, retorne lista 'actions' vazia com analysis explicando."
    )


def _summarize_incidents(context: Dict[str, Any]) -> str:
    incs = context.get("incidents") or []
    if not incs:
        return "(nenhum incidente recente)"
    lines = []
    for inc in incs[-10:]:
        lines.append(f"- [{inc.get('level')}] {inc.get('source')}: {inc.get('line','')[:180]}")
    return "\n".join(lines)


def _collect_pending_suggestions(results: List[ActionResult],
                                 plan: Dict[str, Any]) -> List[str]:
    """Reúne texto útil do plano para reenviar ao Codex como pedido de implementação.

    Fonte (em ordem):
      1) 'analysis' do plano (diagnóstico do ChatGPT),
      2) actions do tipo 'note' (recomendações humanas),
      3) 'description' das ações concretas que falharam (edit_file/create_file/shell),
         junto com o 'details' retornado (motivo da falha) — isso permite que o
         ChatGPT entenda POR QUE falhou e reescreva 'search' com trecho correto.
    """
    pieces: List[str] = []
    analysis = str(plan.get("analysis") or "").strip()
    if analysis:
        pieces.append(f"Diagnóstico anterior: {analysis}")
    # Notas explícitas
    for action in plan.get("actions", []) or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("type", "")).lower() == "note":
            note_text = str(action.get("content") or action.get("note") or
                            action.get("description") or "").strip()
            if note_text:
                pieces.append(f"Sugestão: {note_text}")
    # Ações concretas que falharam (forneça motivo para que o ChatGPT corrija)
    for r in results:
        if not r.ok and r.action_type in {"edit_file", "create_file", "shell"}:
            desc = (r.description or "").strip()
            det = (r.details or "").strip()
            head = f"Ação {r.action_type} falhou"
            if desc:
                head += f" — {desc}"
            if det:
                head += f" (motivo: {det[:240]})"
            pieces.append(head)
    return pieces


def _codex_task_looks_pending() -> Tuple[bool, str]:
    """Verifica se a última tarefa enviada ao Codex ainda está em execução.

    Regra:
      • sem tarefa pendente registrada → não está pendente.
      • elapsed < CODEX_MIN_WAIT_SEC → SIM, está pendente (janela mínima de
        espera para dar tempo do Codex elaborar o PR).
      • CODEX_MIN_WAIT_SEC ≤ elapsed < CODEX_MAX_WAIT_SEC → consulta o git
        remoto: se houver commits novos em qualquer branch (exceto a do
        próprio agente/commits que já estavam antes), assumimos que o
        Codex finalizou. Caso contrário, ainda pendente.
      • elapsed ≥ CODEX_MAX_WAIT_SEC → desbloqueia (timeout), com aviso.

    Retorna (is_pending, reason_message).
    """
    started_at = _AGENT_STATE.codex_pending_started_at or 0.0
    task_url = _AGENT_STATE.codex_pending_task_url or ""
    if not started_at or not task_url:
        return False, ""

    elapsed = max(0.0, time.time() - started_at)
    if elapsed < CODEX_MIN_WAIT_SEC:
        restante = int(CODEX_MIN_WAIT_SEC - elapsed)
        return True, (
            f"tarefa Codex em andamento há {int(elapsed)}s "
            f"(janela mínima {CODEX_MIN_WAIT_SEC}s, faltam ~{restante}s) "
            f"→ {task_url}"
        )

    if elapsed >= CODEX_MAX_WAIT_SEC:
        log(
            f"⏰ Tarefa Codex excedeu janela máxima ({int(elapsed)}s ≥ "
            f"{CODEX_MAX_WAIT_SEC}s); desbloqueando novos forwards. "
            f"URL={task_url}",
            logging.WARNING,
        )
        _clear_codex_pending_task(reason="timeout")
        return False, ""

    # Janela de verificação: consulta git remoto para evidência de conclusão.
    finished, evidence = _codex_pending_probe_remote(started_at)
    if finished:
        log(f"✅ Codex finalizou tarefa anterior ({evidence}); desbloqueado.")
        _clear_codex_pending_task(reason="finished")
        return False, ""

    restante = int(CODEX_MAX_WAIT_SEC - elapsed)
    return True, (
        f"tarefa Codex ainda em execução após {int(elapsed)}s "
        f"(sem commits remotos novos; desiste em ~{restante}s) → {task_url}"
    )


def _codex_pending_probe_remote(started_at: float) -> Tuple[bool, str]:
    """True se há commits remotos novos em qualquer branch desde started_at.

    Estratégia: git fetch --all --quiet, depois for-each-ref listando o
    committerdate de cada refs/remotes. Se alguma for mais nova que
    started_at e não for commit do próprio agente (commit message com o
    COMMIT_PREFIX), consideramos como atividade do Codex.
    """
    try:
        run_shell("git fetch --all --quiet --prune", timeout=60)
    except Exception:
        return False, ""

    cmd = (
        'git for-each-ref --sort=-committerdate refs/remotes '
        '--format="%(committerdate:unix)|%(refname:short)|%(contents:subject)" '
        '--count=30'
    )
    code, out = run_shell(cmd, timeout=30)
    if code != 0 or not out:
        return False, ""

    for line in out.splitlines():
        line = line.strip().strip('"')
        if not line or "|" not in line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        try:
            ts = int(parts[0])
        except Exception:
            continue
        refname = parts[1].strip()
        subject = parts[2].strip()
        if ts <= int(started_at):
            continue
        # Ignora HEAD e a própria branch do agente (commits que o agente
        # acabou de fazer não indicam que o Codex terminou).
        agent_branch = GIT_BRANCH or ""
        if agent_branch and agent_branch in refname:
            continue
        # Ignora commits cujo subject começa com o prefixo do agente.
        if COMMIT_PREFIX and subject.startswith(COMMIT_PREFIX.strip()):
            continue
        return True, f"commit em {refname} @ {ts}: {subject[:80]}"
    return False, ""


def _record_codex_pending_task(chat_url: Optional[str]) -> None:
    """Registra que há uma tarefa Codex em execução (para gate do próximo forward)."""
    if not chat_url:
        return
    # Só considera URLs que realmente indicam uma tarefa Codex em execução.
    if "/codex/cloud/tasks/" not in chat_url and "/codex/" not in chat_url:
        return
    _AGENT_STATE.codex_pending_task_url = chat_url
    _AGENT_STATE.codex_pending_started_at = time.time()
    _save_state()
    log(f"📌 Tarefa Codex registrada como pendente: {chat_url} "
        f"(min {CODEX_MIN_WAIT_SEC}s / max {CODEX_MAX_WAIT_SEC}s)")


def _clear_codex_pending_task(reason: str = "") -> None:
    if not _AGENT_STATE.codex_pending_task_url and not _AGENT_STATE.codex_pending_started_at:
        return
    _AGENT_STATE.codex_pending_task_url = None
    _AGENT_STATE.codex_pending_started_at = 0.0
    _save_state()


def forward_to_codex(context: Dict[str, Any],
                     source_files: Dict[str, str],
                     pending_suggestions: List[str],
                     original_objective: str) -> Optional[Dict[str, Any]]:
    """Envia um pedido de IMPLEMENTAÇÃO ao ChatGPT (via browser.py = Codex) com
    base nas sugestões pendentes e motivos de falha. Pede resposta com ações
    concretas (edit_file/create_file) usando trechos EXATOS do código fornecido.
    """
    if not pending_suggestions:
        return None
    if not simulator_is_ready():
        return None

    # Gate opcional: quando habilitado, bloqueia novos forwards enquanto
    # houver tarefa anterior pendente. Por padrão, DESABILITADO para permitir
    # pedidos simultâneos ao Codex.
    is_pending, reason = _codex_task_looks_pending()
    if is_pending and CODEX_BLOCK_WHILE_PENDING:
        log(f"⏸️  Codex-forward pulado: {reason}")
        return None
    if is_pending and not CODEX_BLOCK_WHILE_PENDING:
        log(
            "ℹ️ Tarefa Codex anterior ainda parece pendente, "
            "mas envio paralelo está habilitado; encaminhando novo pedido."
        )

    suggestions_block = "\n".join(f"- {s}" for s in pending_suggestions[:20])
    codex_prompt = (
        "/plan\n"
        "=== INSTRUÇÕES DO SISTEMA ===\n"
        f"{SYSTEM_PROMPT_BASE}\n"
        "=== FIM DAS INSTRUÇÕES DO SISTEMA ===\n\n"
        "OBJETIVO DESTE CICLO (IMPLEMENTAÇÃO CONCRETA):\n"
        f"{original_objective}\n\n"
        "Você já produziu um diagnóstico/sugestões na rodada anterior. "
        "Agora CONVERTA essas sugestões em ações CONCRETAS do tipo "
        "'edit_file' ou 'create_file'. REGRAS:\n"
        "  • Use 'search' com trecho EXATO do código-fonte abaixo — "
        "copie caractere por caractere, preservando indentação.\n"
        "  • 'search' deve ser ÚNICO no arquivo (inclua contexto suficiente).\n"
        "  • Respeite arquivos protegidos (Scripts/config.py).\n"
        "  • NÃO responda apenas com 'note'. Se honestamente não houver como "
        "implementar com segurança, retorne actions=[] com analysis justificando.\n\n"
        "SUGESTÕES/FALHAS PENDENTES:\n"
        f"{suggestions_block}\n\n"
        "CÓDIGO-FONTE RELEVANTE (use trechos daqui em 'search'):\n"
    )
    for rel, content in (source_files or {}).items():
        codex_prompt += f"--- BEGIN FILE: {rel} ---\n{content}\n--- END FILE: {rel} ---\n"
    codex_prompt += "\nResponda APENAS com JSON no formato especificado."

    # IMPORTANTE: NÃO injeta prefixo de "MAX REASONING" aqui.
    # Esse ajuste é aplicado somente no browser.py no momento do paste.
    wrapped = _wrap_for_paste(codex_prompt)
    # IMPORTANTE: o forward vai para o CODEX (chatgpt.com/codex/cloud), nunca
    # para a conversa regular. Mantemos chat_id/url do Codex SEPARADOS dos
    # do chat regular para evitar contaminação entre as duas sessões do
    # browser.py. Enviamos também o codex_repo para que browser.py selecione
    # o ambiente/repositório correto no dropdown antes do paste.
    codex_target_url = CODEX_URL or "https://chatgpt.com/codex/cloud"
    body: Dict[str, Any] = {
        "model": SIMULATOR_MODEL,
        "message": wrapped,
        "messages": [{"role": "user", "content": wrapped}],
        "temperature": 0.2,
        "request_source": "auto_dev_agent.py/codex",
        "origin_url": codex_target_url,
        "codex_repo": CODEX_REPO,
    }
    if CODEX_REUSE_CHAT and _AGENT_STATE.codex_chat_id and _AGENT_STATE.codex_chat_url \
            and _AGENT_STATE.codex_chat_url.startswith(codex_target_url):
        # Continua a MESMA conversa Codex já iniciada.
        body["chat_id"] = _AGENT_STATE.codex_chat_id
        body["url"] = _AGENT_STATE.codex_chat_url
    else:
        # Força o browser.py a abrir/navegar para o Codex (nova conversa).
        body["url"] = codex_target_url

    log(f"🔁 Forward-to-Codex ({codex_target_url}): pedindo implementação "
        f"concreta de {len(pending_suggestions)} sugestão(ões)/falha(s)...")

    markdown = ""
    chat_id = None
    chat_url = None
    last_exc: Optional[Exception] = None
    for codex_try in range(1, 3):
        try:
            markdown, chat_id, chat_url = _stream_chat_completion(
                body,
                label="ChatGPT Codex (implementação)",
                apply_chat_spacing=False,
            )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            exc_txt = str(exc)
            composer_missing = (
                "Composer do Codex não encontrado" in exc_txt
                or "placeholder /plan ausente" in exc_txt
            )
            if composer_missing and codex_try < 2:
                log("⚠️ Composer do Codex ausente; limpando sessão Codex e tentando reconexão.",
                    logging.WARNING)
                _AGENT_STATE.codex_chat_id = None
                _AGENT_STATE.codex_chat_url = None
                _save_state()
                body.pop("chat_id", None)
                body["url"] = codex_target_url
                time.sleep(3)
                continue
            break
    if last_exc is not None:
        log(f"❌ Forward-to-Codex falhou: {last_exc}", logging.ERROR)
        return None

    # Persiste estado da conversa ATIVA no Codex (isolada do chat regular).
    if CODEX_REUSE_CHAT:
        # Só adota a URL se ela realmente for do Codex; caso contrário, o
        # browser.py caiu no chat regular (falha de navegação) e não queremos
        # guardar essa URL como Codex.
        if chat_url and chat_url.startswith(codex_target_url):
            _AGENT_STATE.codex_chat_url = chat_url
            if chat_id:
                _AGENT_STATE.codex_chat_id = chat_id
            _save_state()
        else:
            if chat_url:
                log(f"⚠️ Forward-to-Codex: URL retornada ({chat_url}) NÃO é do "
                    f"Codex ({codex_target_url}). Descartando chat_id Codex "
                    f"para forçar nova navegação na próxima rodada.",
                    logging.WARNING)
            _AGENT_STATE.codex_chat_id = None
            _AGENT_STATE.codex_chat_url = None
            _save_state()

    plan = _extract_json_object(markdown or "")
    if not plan:
        log("⚠️ Forward-to-Codex: resposta sem JSON válido.", logging.WARNING)
        _record_codex_pending_task(chat_url)
        return None

    codex_flow_status = str(plan.get("codex_flow_status") or "").strip().lower()
    if codex_flow_status in {"final_controls_clicked", "final_controls_detected"}:
        _clear_codex_pending_task(reason=codex_flow_status)
    else:
        _record_codex_pending_task(chat_url)

    plan = _normalize_plan(plan)
    _log_plan_decision(plan, "Codex")
    return plan


def run_single_cycle() -> None:
    _AGENT_STATE.cycles_total += 1
    context = collect_runtime_context()
    log_active_services_snapshot(context["services"])

    has_errors = any(i.get("level") == "error" for i in context.get("incidents", []))
    time_for_suggestion = (time.time() - _AGENT_STATE.last_suggestion_ts) >= SUGGESTION_INTERVAL_SEC

    if not has_errors and not time_for_suggestion:
        log("⌛ Nenhum incidente e intervalo de sugestão não atingido; pulando consulta.")
        _save_state()
        return

    if has_errors:
        _AGENT_STATE.cycles_with_errors += 1
    if time_for_suggestion:
        _AGENT_STATE.last_suggestion_ts = time.time()

    objective = _objective_for_cycle(has_errors, _summarize_incidents(context))

    # Orçamento do prompt: ~50% para código, deixando 50% para contexto+overhead
    src_budget = max(4000, MAX_CONTEXT_CHARS // 2)
    source_files = select_relevant_source_files(context, src_budget)

    prior_attempt: Optional[Dict[str, Any]] = None
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 2):  # 1 tentativa + N retries
        plan = ask_chatgpt_for_plan(context, objective, source_files, prior_attempt)
        if not plan:
            log(f"⚠️ Sem plano do ChatGPT na tentativa {attempt}.", logging.WARNING)
            break

        actions = plan.get("actions") or []
        if not actions:
            should_forward_empty = _should_forward_plan_to_codex(plan, [], [])
            if ENABLE_AUTOFIX and should_forward_empty:
                log("ℹ️ Plano sem actions, mas com indicação de melhoria; encaminhando ao Codex.")
                pending = _collect_pending_suggestions([], plan)
                if not pending:
                    pending = ["Converter a análise em ações concretas edit_file/create_file."]
                forward_attempts = 0
                changed: List[str] = []
                results: List[ActionResult] = []
                while pending and forward_attempts < MAX_CODEX_FORWARD_ATTEMPTS:
                    forward_attempts += 1
                    codex_plan = forward_to_codex(context, source_files, pending, objective)
                    if not codex_plan:
                        break
                    codex_actions = codex_plan.get("actions") or []
                    if not codex_actions:
                        log(f"💭 Codex (tentativa {forward_attempts}): sem ações.")
                        break
                    codex_backup = FileBackup()
                    codex_results = execute_plan(codex_plan, codex_backup)
                    codex_changed = codex_backup.changed_files
                    if codex_changed:
                        ok, report = validate_changes(codex_changed)
                        if not ok:
                            log(f"🛑 Codex: validação falhou "
                                f"({report['py_compile']['errors'][:3]}) — rollback.",
                                logging.WARNING)
                            codex_backup.rollback_all()
                            pending = _collect_pending_suggestions(codex_results, codex_plan)
                            pending.append(
                                "Validação py_compile falhou após aplicar o plano anterior; "
                                "ajuste os trechos exatos e tente novamente."
                            )
                            continue
                        log(f"✅ Codex: validação OK em {len(codex_changed)} arquivo(s).")
                        results.extend(codex_results)
                        changed = codex_changed
                        plan = codex_plan
                        break
                    pending = _collect_pending_suggestions(codex_results, codex_plan)

                if changed and any(r.ok for r in results):
                    _AGENT_STATE.cycles_with_fixes += 1
                    _AGENT_STATE.total_actions += sum(1 for r in results if r.ok)
                    git_commit_and_maybe_push(plan, results)
                _save_state()
                return
            _save_state()
            return

        backup = FileBackup()
        results = execute_plan(plan, backup)
        changed = backup.changed_files

        # Se alguma edição aplicou, valida
        if changed:
            ok, report = validate_changes(changed)
            if not ok:
                log(f"🛑 Validação falhou: {report['py_compile']['errors'][:3]}",
                    logging.WARNING)
                restored = backup.rollback_all()
                log(f"↩️ Rollback: {restored}")
                # Prepara contexto para próxima tentativa com feedback
                prior_attempt = {
                    "attempt": attempt,
                    "plan_analysis": plan.get("analysis", ""),
                    "applied_results": [r.__dict__ for r in results],
                    "validation": report,
                }
                continue  # nova tentativa
            else:
                log(f"✅ Validação OK em {len(changed)} arquivo(s) alterado(s).")

        # Se nenhuma mudança de código foi aplicada mas há sugestões úteis
        # (notas / actions que falharam), ENCAMINHA para o Codex pedindo
        # implementação concreta — esse é o "loop autônomo" de fato.
        should_forward_to_codex = _should_forward_plan_to_codex(plan, results, changed)
        if not changed and ENABLE_AUTOFIX and should_forward_to_codex:
            if not bool(plan.get("should_forward_to_codex")) and _plan_has_change_intent(plan):
                log("ℹ️ Forward forçado por fallback estrutural: plano trouxe edit/create,"
                    " mas nada foi aplicado neste ciclo.")
            pending = _collect_pending_suggestions(results, plan)
            forward_attempts = 0
            while pending and forward_attempts < MAX_CODEX_FORWARD_ATTEMPTS:
                forward_attempts += 1
                codex_plan = forward_to_codex(context, source_files,
                                              pending, objective)
                if not codex_plan:
                    break
                codex_actions = codex_plan.get("actions") or []
                if not codex_actions:
                    log(f"💭 Codex (tentativa {forward_attempts}): sem ações.")
                    break
                codex_backup = FileBackup()
                codex_results = execute_plan(codex_plan, codex_backup)
                codex_changed = codex_backup.changed_files
                if codex_changed:
                    ok, report = validate_changes(codex_changed)
                    if not ok:
                        log(f"🛑 Codex: validação falhou "
                            f"({report['py_compile']['errors'][:3]}) — rollback.",
                            logging.WARNING)
                        codex_backup.rollback_all()
                        # Realimenta motivo do erro para próxima rodada
                        pending = _collect_pending_suggestions(codex_results, codex_plan)
                        pending.append(
                            "Validação py_compile falhou após aplicar o plano anterior; "
                            "ajuste os trechos exatos e tente novamente."
                        )
                        continue
                    log(f"✅ Codex: validação OK em {len(codex_changed)} arquivo(s).")
                    results.extend(codex_results)
                    changed = codex_changed
                    plan = codex_plan  # usa este plano para commit message
                    break
                # Sem mudanças: pode ter falhado de novo — prepara próxima rodada
                pending = _collect_pending_suggestions(codex_results, codex_plan)
                if not pending:
                    break
        elif not changed and ENABLE_AUTOFIX and not should_forward_to_codex:
            log("ℹ️ Plano sinalizou should_forward_to_codex=false; sem encaminhar ao Codex.")

        # Resumo do ciclo
        ok_count = sum(1 for r in results if r.ok)
        err_count = sum(1 for r in results if not r.ok)
        _AGENT_STATE.total_actions += ok_count
        if changed and ok_count:
            _AGENT_STATE.cycles_with_fixes += 1
        log(f"🧩 Ciclo {_AGENT_STATE.cycles_total}: ações_ok={ok_count} "
            f"falhas={err_count} alterações={len(changed)}")

        # Commit/push se mudou alguma coisa
        if changed and ok_count:
            git_commit_and_maybe_push(plan, results)

        _save_state()
        return  # terminou o ciclo

    _save_state()


def _sleep_with_countdown(total_seconds: int, suggestion_remaining_sec: Optional[int] = None) -> None:
    """Pausa entre ciclos com countdown INLINE no stdout.

    Estratégia:
      1. Logamos UMA linha inicial informando o horário absoluto (hora local
         do Windows) em que a próxima conferência irá ocorrer, além do
         intervalo até lá — isso fica persistido no arquivo de log.
      2. Em seguida atualizamos a MESMA linha do console a cada segundo
         usando '\\r', escrevendo direto em sys.stdout (sem passar pelo
         logger, que quebraria a linha).
      3. Ao final, emitimos '\\n' para que o próximo log do ciclo comece
         em uma linha limpa, sem colidir com o resto do countdown.

    Se stdout não aceitar escrita direta, nos limitamos à linha inicial
    logada (que já cumpre o requisito mínimo de citar o horário do próximo
    ciclo) e caímos para um time.sleep silencioso.
    """
    if total_seconds <= 0:
        return

    stream = sys.stdout
    try:
        stream_ok = stream is not None and hasattr(stream, "write")
    except Exception:
        stream_ok = False

    def _fmt(remaining: int) -> str:
        mm, ss = divmod(remaining, 60)
        if mm >= 60:
            hh, mm = divmod(mm, 60)
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return f"{mm:02d}:{ss:02d}"

    # Horário absoluto local (do Windows host) em que o próximo ciclo ocorrerá.
    start_mono = time.time()
    next_cycle_dt = datetime.now() + timedelta(seconds=total_seconds)
    next_hhmmss = next_cycle_dt.strftime("%H:%M:%S")

    suggestion_remaining_sec = (
        max(0, int(suggestion_remaining_sec))
        if isinstance(suggestion_remaining_sec, (int, float))
        else None
    )
    suggestion_hint = ""
    if suggestion_remaining_sec is not None:
        suggestion_hint = (
            f" | próxima sugestão em {_fmt(suggestion_remaining_sec)}"
            if suggestion_remaining_sec > 0
            else " | sugestão já elegível"
        )

    # 1) Linha logada inicial — persistida no log em arquivo e suficiente
    #    caso o terminal não suporte '\r'.
    log(
        f"⏳ Próximo ciclo às {next_hhmmss} "
        f"(em {_fmt(total_seconds)}, total {total_seconds}s){suggestion_hint}"
    )

    if not stream_ok:
        time.sleep(total_seconds)
        return

    end_at = start_mono + total_seconds
    wrote_inline = False
    try:
        while True:
            remaining = int(round(end_at - time.time()))
            if remaining <= 0:
                break

            label = _fmt(remaining)
            inline_suggestion = ""
            if suggestion_remaining_sec is not None:
                elapsed_sec = max(0, int(round(time.time() - start_mono)))
                sug_remaining = max(0, suggestion_remaining_sec - elapsed_sec)
                inline_suggestion = (
                    f" | sugestão em {_fmt(sug_remaining)}"
                    if sug_remaining > 0
                    else " | sugestão elegível"
                )
            line = f"⏳ Próximo ciclo às {next_hhmmss} (em {label}){inline_suggestion}"
            try:
                # Padding generoso para sobrescrever restos da iteração anterior.
                stream.write("\r" + line + " " * 20)
                try:
                    stream.flush()
                except Exception:
                    pass
                wrote_inline = True
            except Exception:
                # Sem stream utilizável; apenas dorme o tempo restante.
                time.sleep(max(1, remaining))
                return

            time.sleep(1.0)
    except KeyboardInterrupt:
        raise
    finally:
        # Limpa a linha do countdown e quebra linha para que o próximo log
        # comece em uma linha nova e não "cole" com o último frame inline.
        if wrote_inline:
            try:
                stream.write("\r" + " " * 80 + "\r")
                try:
                    stream.flush()
                except Exception:
                    pass
            except Exception:
                pass


def wait_for_simulator() -> None:
    started = time.time()
    while not simulator_is_ready():
        if STARTUP_WAIT_SEC <= 0:
            return
        if time.time() - started > STARTUP_WAIT_SEC:
            log("⏱️ Timeout aguardando Simulator; seguirei em modo monitor.",
                logging.WARNING)
            return
        time.sleep(3)


def main_loop() -> None:
    _ensure_single_instance_lock()
    log("🚀 AutoDevAgent iniciando")
    log(f"📄 Log: {AGENT_LOG}")
    log(f"🔗 Simulator URL: {SIMULATOR_URL}")
    log(f"🔗 Codex URL: {CODEX_URL or '(novo chat a cada ciclo de conversa)'}")
    log(f"🧭 Plataforma: {platform.system()} {platform.release()} | Python {sys.version.split(' ',1)[0]}")
    log(f"⚙️ AUTOFIX={ENABLE_AUTOFIX} AUTOCOMMIT={ENABLE_AUTOCOMMIT} AUTOPUSH={ENABLE_AUTOPUSH}")
    if AUTOSTART_SIMULATOR_CMD:
        log(
            "🩺 Auto-start do Simulator habilitado "
            f"(cooldown={AUTOSTART_COOLDOWN_SEC}s)."
        )
    if not API_KEY:
        log("⚠️ API_KEY ausente; requisições podem retornar 401.", logging.WARNING)

    _load_state()
    wait_for_simulator()

    while True:
        started = time.time()
        try:
            run_single_cycle()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log("❌ Exceção no ciclo: " + repr(exc), logging.ERROR)
            log(traceback.format_exc(), logging.ERROR)

        elapsed = time.time() - started
        sleep_for = max(10, CYCLE_INTERVAL_SEC - int(elapsed))
        suggestion_remaining = max(
            0,
            SUGGESTION_INTERVAL_SEC - int(time.time() - (_AGENT_STATE.last_suggestion_ts or 0.0)),
        )
        _sleep_with_countdown(sleep_for, suggestion_remaining_sec=suggestion_remaining)


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    while True:
        try:
            main_loop()
        except KeyboardInterrupt:
            log("🛑 Encerrado por KeyboardInterrupt")
            break
        except Exception as exc:
            log(f"❌ Erro fatal: {exc}", logging.ERROR)
            log(traceback.format_exc(), logging.ERROR)
            # Preserva o processo para permitir inspeção; exit(1) se CI
            if os.environ.get("AUTODEV_AGENT_EXIT_ON_FATAL", "0") == "1":
                sys.exit(1)
            log("🔄 Reiniciando AutoDevAgent em 30 segundos...")
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                log("🛑 Encerrado por KeyboardInterrupt")
                break
