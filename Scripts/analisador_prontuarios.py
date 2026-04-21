#!/usr/bin/env python3
"""
analisador_prontuarios.py
Daemon de análise clínica via LLM.
- Roda na mesma máquina do ChatGPT Simulator (localhost:3003)
- Acessa o banco EXCLUSIVAMENTE via ?action=apiexec do PHP
- Auto-instala dependências faltantes no startup

CONFIGURAÇÃO:
  Todas as variáveis configuráveis estão centralizadas em config.py
  (prefixo ANALISADOR_*). Este script importa de lá via getattr() com
  fallback local — se algo faltar no config.py, o script continua rodando
  com os valores padrão definidos aqui.

  Para alterar qualquer parâmetro, edite APENAS o config.py:
    ANALISADOR_PHP_URL                – endpoint PHP remoto
    ANALISADOR_LLM_URL / _MODEL       – URL e modelo do Simulator local
    ANALISADOR_POLL_INTERVAL           – segundos entre ciclos
    ANALISADOR_MAX_TENTATIVAS          – máx retentativas por análise
    ANALISADOR_BATCH_SIZE              – registros por lote
    ANALISADOR_MIN_CHARS               – tamanho mínimo de texto válido
    ANALISADOR_TIMEOUT_PROCESSANDO_MIN – minutos antes de considerar travado
    ANALISADOR_PAUSA_MIN / _MAX        – pausa humana entre análises (seg)
    ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO – True/False: bloqueia em horário útil
    ANALISADOR_HORARIO_UTIL_INICIO/FIM – faixa de bloqueio (seg-sex, 24h)
    ANALISADOR_EMBEDDING_MODEL_NAME    – modelo de embeddings
    ANALISADOR_SEARCH_HABILITADA       – True/False: busca web ativa
    ANALISADOR_LLM_THROTTLE_MIN/MAX   – seg entre envios ao ChatGPT (anti rate-limit)
    ANALISADOR_LLM_RATE_LIMIT_RETRY_* – config de retry em rate limit
    (ver config.py para lista completa)

PROTEÇÃO CONTRA RATE LIMIT:
  Cada POST ao ChatGPT passa por _post_llm() que aplica throttle global
  (intervalo mínimo entre envios). Se o ChatGPT responder com mensagem
  de rate limit, _parse_json_llm() levanta ChatGPTRateLimitError, que
  faz o lote pausar e depois continuar no próximo item.

LÓGICA DE ORDENAÇÃO DA FILA:
  A query de pendentes unitários divide a fila em duas faixas pelo
  campo datetime_atendimento_inicio:
  1. <30 dias: ASC (mais antigos primeiro) — pacientes recentes cujas
     dúvidas o usuário pode precisar consultar em breve.
  2. >=30 dias: DESC (mais novos primeiro) — prontuários antigos onde
     a prioridade são os menos defasados.
  Toda a lógica roda no SQL via CASE WHEN (sem processamento local).
"""

# ─────────────────────────────────────────────────────────────
# AUTO-INSTALAÇÃO DE DEPENDÊNCIAS
# ─────────────────────────────────────────────────────────────
import os, re, sys, subprocess
import random


def _terminate_previous_same_server_instances(script_name: str) -> None:
    """Fecha processos antigos do mesmo servidor, incluindo janela CMD/.bat anterior."""
    if os.name != "nt":
        return

    current_pid = os.getpid()
    escaped_script = re.escape(script_name).replace("'", "''")
    extra_shell_tokens = {
        "analisador_prontuarios.py": [
            "1. start_apenas_analisador_prontuarios.bat",
            "1.start_apenas_analisador_prontuarios.bat",
            "start_apenas_analisador_prontuarios.bat",
        ],
    }
    shell_tokens = [script_name, *extra_shell_tokens.get(script_name.lower(), [])]
    shell_regex = "|".join(re.escape(token) for token in shell_tokens).replace("'", "''")

    ps_cmd_shells = (
        f"$self={current_pid}; "
        "$selfParent=(Get-CimInstance Win32_Process -Filter \"ProcessId = $self\" | Select-Object -ExpandProperty ParentProcessId); "
        "Get-CimInstance Win32_Process "
        "| Where-Object { "
        "($_.Name -match '^(cmd|powershell|pwsh)\\.exe$') -and "
        "($_.ProcessId -ne $selfParent) -and "
        "($_.CommandLine -match '(?i)(" + shell_regex + ")') "
        "} "
        "| Select-Object -ExpandProperty ProcessId"
    )

    ps_cmd_python = (
        f"$self={current_pid}; "
        "Get-CimInstance Win32_Process "
        "| Where-Object { "
        "($_.Name -match 'python|py') -and "
        "($_.ProcessId -ne $self) -and "
        "($_.CommandLine -match '(?i)" + escaped_script + "') "
        "} "
        "| Select-Object -ExpandProperty ProcessId"
    )
    ps_cmd_ancestors = (
        f"$p={current_pid}; "
        "while ($p -and $p -ne 0) { "
        "  $proc=Get-CimInstance Win32_Process -Filter (\"ProcessId = \" + $p); "
        "  if (-not $proc) { break }; "
        "  $pp=$proc.ParentProcessId; "
        "  if ($pp -and $pp -ne 0) { Write-Output $pp }; "
        "  $p=$pp "
        "}"
    )

    try:
        ancestors_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd_ancestors],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        protected_pids = {current_pid}
        protected_pids.update(
            int(pid_txt.strip())
            for pid_txt in (ancestors_proc.stdout or "").splitlines()
            if pid_txt.strip().isdigit()
        )

        shell_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd_shells],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        shell_targets = {
            int(pid_txt.strip())
            for pid_txt in (shell_proc.stdout or "").splitlines()
            if pid_txt.strip().isdigit()
        }
        for pid in sorted(shell_targets):
            if pid in protected_pids:
                continue
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            print(f"[BOOT] Janela CMD anterior do servidor foi finalizada (PID {pid}) para {script_name}.")

        py_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd_python],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        py_targets = {
            int(pid_txt.strip())
            for pid_txt in (py_proc.stdout or "").splitlines()
            if pid_txt.strip().isdigit()
        }
        for pid in sorted(py_targets):
            if pid in protected_pids:
                continue
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            print(f"[BOOT] Processo Python anterior finalizado (PID {pid}) para {script_name}.")
    except Exception as exc:
        print(f"[BOOT] Aviso: não foi possível substituir instâncias anteriores de {script_name}: {exc}")

def _ensure(pkg, import_name=None):
    """Verifica se pacote esta instalado via find_spec (sem importar o modulo).
    Evita carregamento lento de torch/transformers na inicializacao.
    """
    import importlib.util
    import_name = import_name or pkg
    if importlib.util.find_spec(import_name) is None:
        _pip_install_com_progresso(pkg)


def _pip_install_com_progresso(pkg):
    """Instala pacote pip com barra de progresso inline (sobrescreve a mesma linha)."""
    import threading, time as _t

    BAR    = 28
    ETAPAS = [("Collecting",5),("Downloading",35),("Installing",80),("Successfully",100)]
    pct    = [0]
    stop   = [False]

    def _write(p, suffix=""):
        filled = int(BAR * p / 100)
        bar    = '#' * filled + '-' * (BAR - filled)
        sys.stdout.write(f"\r[setup] {pkg} [{bar}] {p:3d}%{suffix}")
        sys.stdout.flush()

    def _pulsar():
        dots = [".  ",".. ","..."]
        i = 0
        while not stop[0]:
            _write(pct[0], " " + dots[i % 3])
            i += 1
            _t.sleep(0.35)

    _write(0)
    t = threading.Thread(target=_pulsar, daemon=True)
    t.start()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", pkg, "--progress-bar", "off"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        for line in proc.stdout:
            for kw, alvo in ETAPAS:
                if kw.lower() in line.lower() and alvo > pct[0]:
                    pct[0] = alvo
                    _write(pct[0])
                    break
        proc.wait()
    finally:
        stop[0] = True
        t.join(timeout=0.5)
    pct[0] = 100
    _write(100, " OK")
    sys.stdout.write("\n")
    sys.stdout.flush()




_ensure("requests")
_ensure("sentence-transformers", "sentence_transformers")
_ensure("numpy")
# html.parser e html são stdlib — sem instalação necessária

# ─────────────────────────────────────────────────────────────
# IMPORTS NORMAIS
# ─────────────────────────────────────────────────────────────
import time, json, logging, re, html as html_mod, requests, hashlib, shutil, os
import inspect
from collections import Counter
from html.parser import HTMLParser
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# IMPORTAÇÃO DE CONFIGURAÇÃO A PARTIR DO config.py
# ─────────────────────────────────────────────────────────────
# Todas as variáveis configuráveis vivem em config.py.
# Aqui usamos getattr(config, ..., fallback) para que o script
# continue funcionando mesmo se alguma variável for removida
# acidentalmente do config.py.
if 'config' not in sys.modules:
    import config

def _cfg(nome: str, fallback):
    """Lê uma variável do config.py; retorna fallback se não existir."""
    return getattr(config, nome, fallback)

DEBUG_LOG = _cfg("DEBUG_LOG", False)

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÃO (valores vindos de config.py → fallback local)
# ─────────────────────────────────────────────────────────────
PHP_URL        = _cfg("ANALISADOR_PHP_URL",       "https://conexaovida.org/scripts/js/chatgpt_integracao_criado_pelo_gemini.js.php")
API_KEY        = _cfg("API_KEY",                   "CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e")

LLM_URL        = _cfg("ANALISADOR_LLM_URL",       "http://127.0.0.1:3003/v1/chat/completions")
LLM_MODEL      = _cfg("ANALISADOR_LLM_MODEL",     "ChatGPT Simulator")
PROMPT_VERSION = _cfg("ANALISADOR_PROMPT_VERSION", "v16.1")

# Perfil Chromium a ser usado pelas chamadas deste analisador ao Simulator.
# Fallback "default" = compartilha a conta Plus do usuário humano.
# Configure ANALISADOR_BROWSER_PROFILE="segunda_chance" (ou outra chave em
# config.CHROMIUM_PROFILES) para usar uma conta dedicada sem disputar
# rate-limit com o uso manual do ChatGPT.
BROWSER_PROFILE = _cfg("ANALISADOR_BROWSER_PROFILE", "default")

TABELA         = _cfg("ANALISADOR_TABELA",         "chatgpt_atendimentos_analise")
POLL_INTERVAL  = _cfg("ANALISADOR_POLL_INTERVAL",  30)
MAX_TENTATIVAS = _cfg("ANALISADOR_MAX_TENTATIVAS", 3)
MIN_CHARS      = _cfg("ANALISADOR_MIN_CHARS",      80)
BATCH_SIZE     = _cfg("ANALISADOR_BATCH_SIZE",     10)

TIMEOUT_PROCESSANDO_MIN = _cfg("ANALISADOR_TIMEOUT_PROCESSANDO_MIN", 15)

# Filtro de horário útil (preserva limite de mensagens do ChatGPT Plus)
FILTRO_HORARIO_UTIL_ATIVO = _cfg("ANALISADOR_FILTRO_HORARIO_UTIL_ATIVO", False)
HORARIO_UTIL_INICIO       = _cfg("ANALISADOR_HORARIO_UTIL_INICIO",       7)
HORARIO_UTIL_FIM          = _cfg("ANALISADOR_HORARIO_UTIL_FIM",          19)

# Sentence-Transformers / Embeddings
EMBEDDING_MODEL_NAME = _cfg("ANALISADOR_EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
SIMILARIDADE_TOP_K   = _cfg("ANALISADOR_SIMILARIDADE_TOP_K",   5)
SIMILARIDADE_MIN     = _cfg("ANALISADOR_SIMILARIDADE_MIN",     0.40)

# Busca Web (enriquecimento de condutas com evidências)
SEARCH_URL           = _cfg("ANALISADOR_SEARCH_URL",          "http://127.0.0.1:3003/api/web_search")
UPTODATE_SEARCH_URL  = _cfg("ANALISADOR_UPTODATE_SEARCH_URL", "http://127.0.0.1:3003/api/uptodate_search")
SEARCH_MAX_QUERIES   = _cfg("ANALISADOR_SEARCH_MAX_QUERIES",  3)
SEARCH_TIMEOUT       = _cfg("ANALISADOR_SEARCH_TIMEOUT",      90)
SEARCH_HABILITADA    = _cfg("ANALISADOR_SEARCH_HABILITADA",   True)

# Throttle entre mensagens ao ChatGPT:
# desabilitado aqui para deixar o controle de pacing no ChatGPT Simulator/browser.py.
LLM_THROTTLE_MIN  = _cfg("ANALISADOR_LLM_THROTTLE_MIN", 0)  # segundos mínimos entre envios
LLM_THROTTLE_MAX  = _cfg("ANALISADOR_LLM_THROTTLE_MAX", 0)  # segundos máximos (aleatoriza)

# Retry com backoff quando ChatGPT retorna limite/erro de rate
LLM_RATE_LIMIT_RETRY_MAX     = _cfg("ANALISADOR_LLM_RATE_LIMIT_RETRY_MAX",     3)    # tentativas
LLM_RATE_LIMIT_RETRY_BASE_S  = _cfg("ANALISADOR_LLM_RATE_LIMIT_RETRY_BASE_S",  0)    # espera base (seg)
LLM_RATE_LIMIT_RETRY_MULT    = _cfg("ANALISADOR_LLM_RATE_LIMIT_RETRY_MULT",    2.0)  # multiplicador exponencial

# Endpoints PHP:
# - execute_sql: SELECT/SHOW/DESCRIBE (sem rate limiting com api_key valida)
# - api_exec:    CREATE/ALTER/INSERT/UPDATE/DELETE
PHP_ACTION_READ  = "execute_sql"
PHP_ACTION_WRITE = "api_exec"
PHP_KEY_FIELD    = "api_key"

# Nome do log com timestamp do momento de inicialização
_log_ts   = datetime.now().strftime("%d_%m_%Y-%H_%M_%S")
_log_file = f"logs/analisador_prontuarios-{_log_ts}.log"
# ─────────────────────────────────────────────────────────────


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),                              # CMD visível
        logging.FileHandler(_log_file, encoding="utf-8"),               # arquivo com timestamp
    ]
)
log = logging.getLogger("analisador")
log.info(f"📄 Log: {_log_file}")


def _headers_llm() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "X-Request-Source": "analisador_prontuarios.py",
    }


# ─────────────────────────────────────────────────────────────
# THROTTLE + RATE-LIMIT RETRY para chamadas ao ChatGPT
# ─────────────────────────────────────────────────────────────
# Garante intervalo mínimo entre envios e detecta resposta de
# rate limit ("chegou ao limite", "excesso de solicitações") para
# fazer retry com backoff exponencial.

_ultimo_envio_llm = 0.0   # timestamp do último POST ao LLM_URL

# Padrões de texto que indicam rate limit na resposta do ChatGPT
_RATE_LIMIT_PATTERNS = [
    "chegou ao limite",
    "excesso de solicitações",
    "tente novamente mais tarde",
    "rate limit",
    "too many requests",
]

def _aguardar_throttle_llm():
    """Espera o tempo restante do throttle antes de enviar a próxima mensagem ao ChatGPT."""
    global _ultimo_envio_llm
    if LLM_THROTTLE_MIN <= 0 and LLM_THROTTLE_MAX <= 0:
        return
    if _ultimo_envio_llm <= 0:
        return
    espera_alvo = random.uniform(LLM_THROTTLE_MIN, LLM_THROTTLE_MAX)
    decorrido = time.time() - _ultimo_envio_llm
    restante = espera_alvo - decorrido
    if restante > 0:
        log.info(f"  ⏱️  Throttle: aguardando {restante:.0f}s antes do próximo envio ao ChatGPT...")
        time.sleep(restante)

def _registrar_envio_llm():
    """Marca o timestamp do envio mais recente."""
    global _ultimo_envio_llm
    _ultimo_envio_llm = time.time()

def _resposta_eh_rate_limit(texto: str) -> bool:
    """Detecta se o texto da resposta do ChatGPT indica rate limit."""
    if not texto:
        return False
    texto_lower = texto.lower()
    return any(p in texto_lower for p in _RATE_LIMIT_PATTERNS)


class ChatGPTRateLimitError(RuntimeError):
    """Levantada quando o ChatGPT retorna uma resposta de rate limit."""
    pass


def _is_llm_connection_error(exc: BaseException) -> bool:
    """
    Detecta erros transitórios de conexão/stream com o ChatGPT Simulator.
    Inclui casos comuns quando o servidor reinicia durante uma análise.
    """
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
        return True

    texto = str(exc or "").lower()
    padroes = (
        "connection reset",
        "connection aborted",
        "connection broken",
        "remote end closed connection",
        "forçado o cancelamento de uma conexão existente pelo host remoto",
        "max retries exceeded",
        "failed to establish a new connection",
    )
    return any(p in texto for p in padroes)


def _aguardar_reconexao_llm(espera: int, tentativa: int, exc: BaseException):
    """Aguarda nova tentativa de conexão sem derrubar o daemon."""
    log.warning(
        f"  ⚠️ Conexão com ChatGPT Simulator interrompida "
        f"(tentativa #{tentativa}): {exc}"
    )
    if not verificar_llm():
        log.warning(
            "     🔌 Simulator aparenta estar offline/reiniciando. "
            "O analisador seguirá tentando reconectar automaticamente."
        )
    else:
        log.warning(
            "     🔄 Simulator respondeu no health-check; repetindo chamada para retomar o stream."
        )
    try:
        countdown(espera, "reconexão LLM")
    except Exception:
        time.sleep(espera)


def _post_llm(payload: dict, timeout: int = 300) -> requests.Response:
    """
    Wrapper para requests.post(LLM_URL) com throttle + retry em rate limit.
    Garante intervalo mínimo entre envios e, se o ChatGPT responder com
    mensagem de rate limit, faz retry com backoff exponencial.
    """
    ultimo_erro: Exception | None = None
    tentativa = 0
    while True:
        tentativa += 1
        try:
            _aguardar_throttle_llm()
            _registrar_envio_llm()
            resp = requests.post(
                LLM_URL,
                json=payload,
                headers=_headers_llm(),
                stream=True,
                timeout=timeout,
            )
            resp.raise_for_status()
            if tentativa > 1:
                log.info(f"  ✅ ChatGPT Simulator reconectado após {tentativa - 1} falha(s).")
            return resp
        except (requests.Timeout, requests.ConnectionError) as exc:
            ultimo_erro = exc
            espera = int(LLM_RATE_LIMIT_RETRY_BASE_S * (LLM_RATE_LIMIT_RETRY_MULT ** min(tentativa - 1, 6)))
            if espera <= 0:
                log.warning("  ⚠️ Reconexão imediata com LLM (sem delay configurado no analisador).")
                continue
            espera = min(180, espera)
            _aguardar_reconexao_llm(espera, tentativa, exc)
        except requests.HTTPError as exc:
            # Mantém comportamento atual para rate-limit/erro HTTP: quem chama
            # decide como tratar via fluxo de exceções já existente.
            ultimo_erro = exc
            raise
        except requests.RequestException as exc:
            ultimo_erro = exc
            raise
    # Nunca deve chegar aqui, mas por segurança:
    raise ChatGPTRateLimitError(f"Falha inesperada de conexão com ChatGPT Simulator: {ultimo_erro}")


def _verificar_rate_limit_no_markdown(markdown: str, tentativa_atual: int = 0):
    """
    Chamada após receber o markdown completo da LLM.
    Se detectar rate limit no texto, levanta ChatGPTRateLimitError.
    """
    if _resposta_eh_rate_limit(markdown):
        raise ChatGPTRateLimitError(
            f"ChatGPT retornou rate limit (detectado no texto da resposta). "
            f"Prévia: {markdown[:120]}"
        )


def _strip_code_fences(texto: str) -> str:
    """Remove cercas Markdown ```...``` mantendo apenas o conteúdo interno."""
    texto = (texto or "").strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\s*```$", "", texto)
    return texto.strip()


def _extrair_bloco_json(texto: str) -> str:
    """Extrai o primeiro objeto JSON aparente do texto retornado pela LLM."""
    texto = _strip_code_fences(texto)
    match = re.search(r'\{[\s\S]*\}', texto)
    return match.group().strip() if match else ""


def _normalizar_json_llm(raw_json: str) -> str:
    """
    Corrige problemas comuns de JSON quase-válido retornado por LLMs:
    - aspas tipográficas;
    - vírgulas faltando entre pares chave/valor consecutivos;
    - vírgulas faltando entre objetos de uma lista;
    - vírgulas sobrando antes de ] ou }.
    """
    texto = (raw_json or "").strip()
    if not texto:
        return ""

    texto = (
        texto
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
        .replace("`", '"')
    )

    # Escapa aspas internas não-escapadas dentro de valores string.
    # Exemplo comum de LLM: "titulo": "Expressive language delay ("late talking") in..."
    # JSON válido exigiria: \"late talking\".
    chars = []
    in_string = False
    escape = False
    n = len(texto)
    i = 0
    while i < n:
        ch = texto[i]
        if not in_string:
            chars.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        if escape:
            chars.append(ch)
            escape = False
            i += 1
            continue

        if ch == '\\':
            chars.append(ch)
            escape = True
            i += 1
            continue

        if ch == '"':
            # Se após aspas houver delimitador de fim de string JSON, encerra string.
            # Caso contrário, trata como aspas internas e escapa.
            j = i + 1
            while j < n and texto[j] in ' \t\r\n':
                j += 1
            next_ch = texto[j] if j < n else ''
            if next_ch in [',', '}', ']', ':', '']:
                chars.append('"')
                in_string = False
            else:
                chars.append('\\"')
            i += 1
            continue

        chars.append(ch)
        i += 1

    texto = ''.join(chars)

    # Ex.: "query": "..."   "reason": "..."
    texto = re.sub(r'("(?:(?:\\.|[^"\\])*)")(\s*)"([A-Za-z0-9_\-]+)"\s*:', r'\1,\2"\3":', texto)
    # Ex.: } {   ou   ] {
    texto = re.sub(r'([}\]])(\s*)(\{)', r'\1,\2', texto)
    # Ex.: } "outra_chave":
    texto = re.sub(r'([}\]])(\s*)"([A-Za-z0-9_\-]+)"\s*:', r'\1,\2"\3":', texto)
    # Remove trailing commas antes de fechar objeto/lista
    texto = re.sub(r',(\s*[}\]])', r'\1', texto)
    return texto


def _parse_json_llm(texto: str) -> dict:
    """
    Faz o parse de um objeto JSON retornado pela LLM com pequenas correções
    tolerantes a formatação imperfeita.
    """
    # Detecta rate limit antes de tentar parse (evita contar como "JSON inválido")
    _verificar_rate_limit_no_markdown(texto)

    candidato = _extrair_bloco_json(texto)
    if not candidato:
        raise ValueError("LLM não retornou bloco JSON.")

    try:
        return json.loads(candidato)
    except json.JSONDecodeError:
        candidato_normalizado = _normalizar_json_llm(candidato)
        return json.loads(candidato_normalizado)


def _json_parece_incompleto(texto: str) -> bool:
    """Heurística para identificar respostas JSON possivelmente truncadas/incompletas."""
    bruto = _strip_code_fences(texto or "").strip()
    if not bruto or not bruto.startswith('{'):
        return False

    depth_obj = 0
    depth_arr = 0
    in_string = False
    escape = False
    for ch in bruto:
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

    return in_string or depth_obj > 0 or depth_arr > 0 or not bruto.rstrip().endswith('}')


def _extrair_markdown_visivel_llm(texto: str) -> str:
    """
    Remove blocos/markers de raciocínio interno para identificar apenas a
    resposta visível já entregue pela LLM ao usuário.
    """
    bruto = texto or ""
    if not bruto.strip():
        return ""

    # Caso o bloco <think> ainda esteja aberto, consideramos que a resposta
    # visível ainda não começou.
    if "<think>" in bruto and "</think>" not in bruto:
        return ""

    sem_think = re.sub(r"<think>[\s\S]*?</think>", "", bruto, flags=re.IGNORECASE)
    return sem_think.strip()


def _salvar_debug_json_falha(id_atendimento: int | None, etapa: str, markdown: str, erro: Exception | str):
    """Salva artefatos para depurar falhas de parse JSON da resposta da LLM."""
    os.makedirs('logs/json_debug', exist_ok=True)
    ts = datetime.now().strftime('%d_%m_%Y-%H_%M_%S')
    prefix = f"{id_atendimento or 'sem_id'}-{etapa}-{ts}"
    path_md = os.path.join('logs', 'json_debug', f'{prefix}.md.txt')
    path_meta = os.path.join('logs', 'json_debug', f'{prefix}.meta.json')

    bruto = markdown or ''
    candidato = _extrair_bloco_json(bruto)
    normalizado = _normalizar_json_llm(candidato) if candidato else ''
    meta = {
        'id_atendimento': id_atendimento,
        'etapa': etapa,
        'erro': str(erro),
        'json_parece_incompleto': _json_parece_incompleto(bruto),
        'tem_bloco_json': bool(candidato),
        'tamanho_markdown': len(bruto),
        'tamanho_candidato_json': len(candidato),
        'tamanho_json_normalizado': len(normalizado),
        'markdown_preview': re.sub(r'\s+', ' ', _strip_code_fences(bruto))[:1200],
        'json_preview': re.sub(r'\s+', ' ', candidato)[:1200],
    }

    with open(path_md, 'w', encoding='utf-8') as f:
        f.write(bruto)
    with open(path_meta, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return path_md, path_meta, meta


def _reativar_esgotados_recuperaveis_no_startup():
    """Reprocessa automaticamente erros esgotados com alta chance de recuperação."""
    resultado = sql_exec(
        f"""
        UPDATE {TABELA}
        SET
            status = 'pendente',
            tentativas = 0,
            datetime_analise_iniciada = NULL,
            erro_msg = CONCAT(
                COALESCE(erro_msg, ''),
                ' | [AUTO-REQUEUE-RECOVERABLE] ',
                NOW(),
                ' recolocado em pendente para nova tentativa de parse/stream'
            )
        WHERE
            id_atendimento IS NOT NULL
            AND status = 'erro'
            AND tentativas >= {MAX_TENTATIVAS}
            AND COALESCE(erro_msg, '') NOT LIKE '%[AUTO-REQUEUE-RECOVERABLE]%'
            AND (
                LOWER(COALESCE(erro_msg, '')) LIKE '%simulador não retornou conteúdo markdown%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%llm não retornou json válido%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%llm não retornou bloco json%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%expecting '', delimiter%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%expecting property name enclosed in double quotes%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%unterminated string%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%invalid control character%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%extra data%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%unexpected keyword argument ''id_atendimento''%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%name ''idatendimento'' is not defined%'
            )
        """,
        reason="reativar_esgotados_recuperaveis"
    )
    afetados = resultado.get('affected_rows', 0)
    if afetados:
        log.warning(f"♻️ {afetados} registro(s) esgotado(s) com erro recuperável foram recolocados em pendente no startup.")
    return afetados


def _reativar_erros_conexao_no_startup():
    """
    No startup, trata erro de conexão como análise interrompida:
    volta para pendente e limpa datetime_analise_iniciada, respeitando MAX_TENTATIVAS.
    """
    resultado = sql_exec(
        f"""
        UPDATE {TABELA}
        SET
            status = 'pendente',
            datetime_analise_iniciada = NULL,
            erro_msg = CONCAT(
                COALESCE(erro_msg, ''),
                ' | [AUTO-REQUEUE-CONNECTION] ',
                NOW(),
                ' erro de conexão detectado no startup; reencaminhado para pendente'
            )
        WHERE
            id_atendimento IS NOT NULL
            AND status = 'erro'
            AND tentativas < {MAX_TENTATIVAS}
            AND COALESCE(erro_msg, '') NOT LIKE '%[AUTO-REQUEUE-CONNECTION]%'
            AND (
                LOWER(COALESCE(erro_msg, '')) LIKE '%connection broken%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%connectionreseterror%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%read timed out%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%max retries exceeded%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%failed to establish a new connection%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%remote end closed connection%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%forçado o cancelamento de uma conexão%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%nenhuma conexão pôde ser feita%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%winerror 10054%'
                OR LOWER(COALESCE(erro_msg, '')) LIKE '%winerror 10061%'
            )
        """,
        reason="reativar_erros_conexao_startup",
    )
    afetados = resultado.get("affected_rows", 0)
    if afetados:
        log.warning(
            f"♻️ {afetados} registro(s) com erro de conexão foram recolocados em pendente no startup (tentativas < {MAX_TENTATIVAS})."
        )
    return afetados


def _decode_json_string_fragment(value: str) -> str:
    """Decodifica um fragmento de string JSON sem perder caracteres UTF-8."""
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")


def _extrair_queries_pesquisa_fallback(markdown: str) -> list:
    """
    Extrai queries manualmente quando a LLM não entrega JSON estrito.
    Aceita objetos quase-JSON e listas em texto contendo campos query/reason.
    """
    texto = _strip_code_fences(markdown)
    if not texto:
        return []

    queries = []
    vistos = set()

    # Tenta localizar pares query/reason mesmo quando o JSON veio truncado ou sem vírgulas.
    pair_pattern = re.compile(
        r'"query"\s*:\s*"(?P<query>(?:\\.|[^"\\])*)"\s*,?\s*"reason"\s*:\s*"(?P<reason>(?:\\.|[^"\\])*)"',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pair_pattern.finditer(texto):
        query = re.sub(r"\s+", " ", _decode_json_string_fragment(match.group("query"))).strip()
        reason = re.sub(r"\s+", " ", _decode_json_string_fragment(match.group("reason"))).strip()
        if not query:
            continue
        chave = query.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        queries.append({"query": query, "reason": reason})
        if len(queries) >= SEARCH_MAX_QUERIES:
            return queries

    # Fallback mais simples: linhas em lista com query e motivo.
    line_pattern = re.compile(
        r'^\s*(?:[-*]|\d+[.)])\s*(?P<query>.+?)(?:\s+[—-]\s+|\s+\|\s+motivo:\s+)(?P<reason>.+?)\s*$',
        re.IGNORECASE | re.MULTILINE,
    )
    for match in line_pattern.finditer(texto):
        query = re.sub(r"\s+", " ", match.group("query")).strip(' "\'')
        reason = re.sub(r"\s+", " ", match.group("reason")).strip(' "\'')
        if not query:
            continue
        chave = query.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        queries.append({"query": query, "reason": reason})
        if len(queries) >= SEARCH_MAX_QUERIES:
            break

    return queries



# ─────────────────────────────────────────────────────────────
# CAMADA HTTP → PHP (único ponto de acesso ao banco)
# ─────────────────────────────────────────────────────────────
_WRITE_CMDS = {'CREATE', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'DROP', 'TRUNCATE', 'REPLACE'}

def sql_exec(query: str, reason: str | None = None) -> dict:
    """Roteia automaticamente:
    - SELECT/SHOW/DESCRIBE -> execute_sql (api_key no header+payload, sem rate limiting)
    - CREATE/ALTER/INSERT/UPDATE/DELETE -> api_exec
    """

    # Evita reason genérica: se não vier explícita, infere da função chamadora.
    if not (reason and str(reason).strip()):
        caller = "desconhecido"
        try:
            caller = inspect.stack()[1].function
        except Exception:
            pass
        reason = f"auto:{caller}"

    # Padroniza o reason com prefixo do módulo
    reason = f"[analisador_prontuarios.py] {reason}"

    # Debug: loga a query antes de enviar
    if DEBUG_LOG:
        log.info(f"[SQL] {reason} | {query}")

    first_word = query.strip().split()[0].upper() if query.strip() else ''
    is_write   = first_word in _WRITE_CMDS
    action     = PHP_ACTION_WRITE if is_write else PHP_ACTION_READ

    if is_write:
        payload = json.dumps(
            {"sql": query, PHP_KEY_FIELD: API_KEY},
            ensure_ascii=False
        ).encode("utf-8")
    else:
        payload = json.dumps(
            {"query": query, "reason": reason, PHP_KEY_FIELD: API_KEY},
            ensure_ascii=False
        ).encode("utf-8")

    resp = requests.post(
        f"{PHP_URL}?action={action}",
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-API-KEY":    API_KEY,
        },
        timeout=30,
    )
    resp.raise_for_status()

    raw = resp.content.decode("utf-8").strip()
    if not raw:
        raise RuntimeError(f"{action}: resposta vazia (HTTP {resp.status_code})")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            raise RuntimeError(f"{action}: resposta nao-JSON: {raw[:400]}")
        data = json.loads(match.group())

    if data.get("status") == "error":
        raise RuntimeError(f"{action} recusou: {data.get('message')} | SQL completo:\n{query}")
    if not data.get("success", True) and data.get("error"):
        raise RuntimeError(f"{action} recusou: {data.get('error')} | SQL completo:\n{query}")

    log.debug(f"sql_exec OK [{action}] rows={data.get('num_rows', data.get('count', data.get('affected_rows', '?')))}")
    return data


def esc(value) -> str:
    """Escape básico: barras e aspas simples."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")

def esc_str(value) -> str:
    """
    Escape + charset introducer _utf8mb4.
    Garante que o MySQL interprete a string como UTF-8
    independente do charset negociado na conexão.
    Ex: _utf8mb4'Criança com TEA'
    """
    return f"_utf8mb4'{esc(value)}'"



# ─────────────────────────────────────────────────────────────
# OPERAÇÕES DE BANCO
# ─────────────────────────────────────────────────────────────
def garantir_tabela():
    try:
        sql_exec(f"""
            CREATE TABLE IF NOT EXISTS {TABELA} (
                id                                      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY
                                                        COMMENT 'Chave primaria interna da tabela de analises.',
                id_atendimento                          LONGTEXT NULL
                                                        COMMENT 'ID do atendimento (unitário) ou lista textual de IDs dos atendimentos consolidados (síntese compilada do paciente).',
                datetime_atendimento_inicio             DATETIME NULL
                                                        COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.',
                datetime_ultima_atualizacao_atendimento DATETIME NULL
                                                        COMMENT 'COALESCE(datetime_atualizacao, datetime_consulta_fim) no momento da analise.',
                id_paciente                             VARCHAR(800) NOT NULL
                                                        COMMENT 'FK para membros.id do paciente.',
                id_criador                              LONGTEXT NULL
                                                        COMMENT 'FK para membros.id do profissional criador (unitário) ou descritor analise_compilada_paciente nas sínteses compiladas.',
                datetime_analise_criacao                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                        COMMENT 'Data/hora de insercao do registro.',
                datetime_analise_iniciada               DATETIME NULL
                                                        COMMENT 'Data/hora em que o analisador iniciou efetivamente o processamento desta analise.',
                datetime_analise_concluida              DATETIME NULL
                                                        COMMENT 'Data/hora em que a analise foi concluida com sucesso.',
                chat_id                                 VARCHAR(100) NULL
                                                        COMMENT 'ID da conversa no ChatGPT Simulator.',
                chat_url                                VARCHAR(500) NULL
                                                        COMMENT 'URL da conversa no ChatGPT para auditoria.',
                status                                  ENUM('pendente','processando','concluido','erro','cancelado') NOT NULL DEFAULT 'pendente'
                                                        COMMENT 'Ciclo de vida da analise. cancelado = descartado manualmente.',
                tentativas                              TINYINT UNSIGNED NOT NULL DEFAULT 0
                                                        COMMENT 'Contador de tentativas de analise.',
                erro_msg                                TEXT NULL
                                                        COMMENT 'Mensagem de erro da ultima tentativa falha.',
                -- escalares
                resumo_texto                            LONGTEXT NULL
                                                        COMMENT 'Resumo clinico em 2-3 frases.',
                gravidade_clinica                       VARCHAR(50) NULL
                                                        COMMENT 'Classificacao de gravidade: leve, moderada, grave.',
                idade_paciente_valor                    VARCHAR(20) NULL
                                                        COMMENT 'Valor numerico da idade do paciente.',
                idade_paciente_unidade                  VARCHAR(20) NULL
                                                        COMMENT 'Unidade da idade: anos, meses, dias.',
                seguimento_retorno_estimado             LONGTEXT NULL
                                                        COMMENT 'JSON completo do objeto seguimento_retorno_estimado retornado pelo LLM.',
                seguimento_observacao                   TEXT NULL
                                                        COMMENT 'Observacao legada sobre o seguimento clinico (descontinuado — mantido por compatibilidade).',
                -- arrays simples
                diagnosticos_citados                    LONGTEXT NULL
                                                        COMMENT 'JSON array de diagnosticos citados.',
                pontos_chave                            LONGTEXT NULL
                                                        COMMENT 'JSON array de achados clinicos relevantes.',
                mudancas_relevantes                     LONGTEXT NULL
                                                        COMMENT 'JSON array de mudancas clinicas relevantes.',
                eventos_comportamentais                 LONGTEXT NULL
                                                        COMMENT 'JSON array de eventos comportamentais.',
                sinais_nucleares                        LONGTEXT NULL
                                                        COMMENT 'JSON array de sinais nucleares.',
                terapias_referidas                      LONGTEXT NULL
                                                        COMMENT 'JSON array de terapias referidas.',
                exames_citados                          LONGTEXT NULL
                                                        COMMENT 'JSON array de exames citados.',
                pendencias_clinicas                     LONGTEXT NULL
                                                        COMMENT 'JSON array de pendencias clinicas.',
                condutas_no_prontuario                  LONGTEXT NULL
                                                        COMMENT 'JSON array de condutas ja registradas no prontuario.',
                -- arrays de objetos
                medicacoes_em_uso                       LONGTEXT NULL
                                                        COMMENT 'JSON array: {{nome, dose, posologia, desde, observacao}}.',
                medicacoes_iniciadas                    LONGTEXT NULL
                                                        COMMENT 'JSON array: {{nome, dose, posologia, data_relativa}}.',
                medicacoes_suspensas                    LONGTEXT NULL
                                                        COMMENT 'JSON array: {{nome, dose, posologia, motivo, periodo}}.',
                condutas_especificas_sugeridas          LONGTEXT NULL
                                                        COMMENT 'JSON array: {{conduta, justificativa, referencia, fonte}} — com suporte em literatura.',
                condutas_gerais_sugeridas               LONGTEXT NULL
                                                        COMMENT 'JSON array de condutas baseadas em boa pratica clinica, sem exigencia de referencia formal.',
                mensagens_acompanhamento                LONGTEXT NULL
                                                        COMMENT 'JSON object com tres mensagens WhatsApp para acompanhamento pos-consulta: {{mensagem_1_semana, mensagem_1_mes, mensagem_pre_retorno}}.',
                dados_json                              LONGTEXT NULL
                                                        COMMENT 'JSON completo retornado pelo LLM para auditoria e reprocessamento futuro.',
                -- metadados da analise
                modelo_llm                              VARCHAR(100) NULL
                                                        COMMENT 'Modelo LLM utilizado para gerar esta analise (ex: gpt-4o, claude-3-5-sonnet).',
                prompt_version                          VARCHAR(30) NULL
                                                        COMMENT 'Versao do prompt clinico utilizado. Facilita auditoria e reprocessamento por versao.',
                hash_prontuario                         CHAR(64) NULL
                                                        COMMENT 'Hash SHA-256 do conteudo bruto do prontuario analisado.',
                -- cdss / scoring
                score_risco                             TINYINT NULL
                                                        COMMENT 'Score numerico de risco clinico estimado pela analise: 1=baixo, 2=moderado, 3=alto.',
                alertas_clinicos                        LONGTEXT NULL
                                                        COMMENT 'JSON array de alertas clinicos identificados automaticamente (ex: regressao, polifarmacia, risco_alto).',
                casos_semelhantes                       LONGTEXT NULL
                                                        COMMENT 'JSON array com IDs de atendimentos clinicamente semelhantes identificados via busca semantica.',
                INDEX  idx_atendimento (id_atendimento),
                INDEX  idx_paciente    (id_paciente),
                INDEX  idx_status      (status),
                INDEX  idx_hash        (hash_prontuario)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, reason="garantir_tabela")
        log.info(f"✅ Tabela {TABELA} verificada/criada.")
    except RuntimeError as e:
        log.warning(f"⚠️  garantir_tabela: {e}")
        log.warning("   (Se a tabela já existir no banco, isso é inofensivo.)")


def garantir_coluna_dados_json():
    """
    Garante que a coluna dados_json existe em instâncias já criadas antes desta versão.
    Seguro executar múltiplas vezes — IF NOT EXISTS evita erro se já existir.
    Também invalida o cache de colunas para forçar releitura após o ALTER.
    """
    try:
        sql_exec(f"""
            ALTER TABLE {TABELA}
            ADD COLUMN IF NOT EXISTS dados_json LONGTEXT NULL
            COMMENT 'JSON completo retornado pelo LLM para auditoria e reprocessamento futuro.'
        """, reason="garantir_coluna_dados_json")
        log.info("✅ Coluna dados_json verificada/criada.")
    except RuntimeError as e:
        log.warning(f"⚠️  garantir_coluna_dados_json: {e}")
    finally:
        global _COLUNAS_TABELA
        _COLUNAS_TABELA = None   # invalida cache para incluir a coluna recém-garantida


def garantir_coluna_mensagens_acompanhamento():
    """
    Garante que a coluna mensagens_acompanhamento existe em instancias ja criadas antes desta versao.
    Seguro executar multiplas vezes -- IF NOT EXISTS evita erro se ja existir.
    Tambem invalida o cache de colunas para forcar releitura apos o ALTER.
    """
    try:
        sql_exec(f"""
            ALTER TABLE {TABELA}
            ADD COLUMN IF NOT EXISTS mensagens_acompanhamento LONGTEXT NULL
            COMMENT 'JSON object com tres mensagens WhatsApp para acompanhamento pos-consulta: {{mensagem_1_semana, mensagem_1_mes, mensagem_pre_retorno}}.'
        """, reason="garantir_coluna_mensagens_acompanhamento")
        log.info("✅ Coluna mensagens_acompanhamento verificada/criada.")
    except RuntimeError as e:
        log.warning(f"⚠️  garantir_coluna_mensagens_acompanhamento: {e}")
    finally:
        global _COLUNAS_TABELA
        _COLUNAS_TABELA = None   # invalida cache para incluir a coluna recem-garantida


def garantir_coluna_datetime_analise_iniciada():
    """
    Garante a coluna datetime_analise_iniciada em instâncias antigas do schema.
    Essa coluna marca o início efetivo do processamento pelo analisador e deve
    ser usada nas métricas de duração reais (início → conclusão).
    """
    try:
        sql_exec(f"""
            ALTER TABLE {TABELA}
            ADD COLUMN IF NOT EXISTS datetime_analise_iniciada DATETIME NULL
            COMMENT 'Data/hora em que o analisador iniciou efetivamente o processamento desta analise.'
            AFTER datetime_analise_criacao
        """, reason="garantir_coluna_datetime_analise_iniciada")
        log.info("✅ Coluna datetime_analise_iniciada verificada/criada.")
    except RuntimeError as e:
        log.warning(f"⚠️  garantir_coluna_datetime_analise_iniciada: {e}")
    finally:
        global _COLUNAS_TABELA
        _COLUNAS_TABELA = None


def garantir_colunas_v16():
    """
    Garante as novas colunas da versao V16 do schema (CDSS/RAG).
    Seguro executar multiplas vezes -- IF NOT EXISTS evita erro se ja existir.
    """
    global _COLUNAS_TABELA
    colunas_v16 = [
        ("modelo_llm",        "VARCHAR(100) NULL", "Modelo LLM utilizado para gerar esta analise."),
        ("prompt_version",    "VARCHAR(30) NULL",  "Versao do prompt clinico utilizado."),
        ("hash_prontuario",   "CHAR(64) NULL",     "Hash SHA-256 do conteudo bruto do prontuario analisado."),
        ("score_risco",       "TINYINT NULL",      "Score numerico de risco clinico: 1=baixo, 2=moderado, 3=alto."),
        ("alertas_clinicos",  "LONGTEXT NULL",     "JSON array de alertas clinicos identificados automaticamente."),
        ("casos_semelhantes", "LONGTEXT NULL",     "JSON array com IDs de atendimentos clinicamente semelhantes."),
    ]
    erros = []
    for coluna, tipo, comment in colunas_v16:
        try:
            sql_exec(f"""
                ALTER TABLE {TABELA}
                ADD COLUMN IF NOT EXISTS {coluna} {tipo}
                COMMENT '{comment}'
            """, reason=f"garantir_coluna_v16_{coluna}")
        except RuntimeError as e:
            erros.append(f"{coluna}: {e}")
    if erros:
        for err in erros:
            log.warning(f"\u26a0\ufe0f  garantir_colunas_v16: {err}")
    else:
        log.info("\u2705 Colunas V16 (CDSS/RAG) verificadas/criadas.")
    _COLUNAS_TABELA = None


def garantir_schema_analise_compilada_paciente():
    """
    Libera a própria chatgpt_atendimentos_analise para armazenar uma síntese
    longitudinal do paciente usando apenas id_paciente como localizador.
    """
    ajustes = [
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento LONGTEXT NULL COMMENT 'ID do atendimento (unitário) ou lista textual de IDs dos atendimentos consolidados (síntese compilada do paciente).'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador LONGTEXT NULL COMMENT 'FK para membros.id do profissional criador (unitário) ou descritor analise_compilada_paciente nas sínteses compiladas.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql, reason="garantir_schema_analise_compilada_paciente")
        except RuntimeError as e:
            msg = str(e).lower()
            if 'duplicate key name' in msg or 'already exists' in msg:
                continue
            log.warning(f"⚠️  garantir_schema_analise_compilada_paciente: {e}")

    global _COLUNAS_TABELA
    _COLUNAS_TABELA = None


def garantir_schema_analise_compilada_paciente():
    """
    Libera a própria chatgpt_atendimentos_analise para armazenar uma síntese
    longitudinal do paciente usando apenas id_paciente como localizador.
    """
    ajustes = [
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento LONGTEXT NULL COMMENT 'ID do atendimento (unitário) ou lista textual de IDs dos atendimentos consolidados (síntese compilada do paciente).'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador LONGTEXT NULL COMMENT 'FK para membros.id do profissional criador (unitário) ou descritor analise_compilada_paciente nas sínteses compiladas.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql, reason="garantir_schema_analise_compilada_paciente")
        except RuntimeError as e:
            msg = str(e).lower()
            if 'duplicate key name' in msg or 'already exists' in msg:
                continue
            log.warning(f"⚠️  garantir_schema_analise_compilada_paciente: {e}")

    global _COLUNAS_TABELA
    _COLUNAS_TABELA = None


def garantir_schema_analise_compilada_paciente():
    """
    Libera a própria chatgpt_atendimentos_analise para armazenar uma síntese
    longitudinal do paciente usando apenas id_paciente como localizador.
    """
    ajustes = [
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento LONGTEXT NULL COMMENT 'ID do atendimento (unitário) ou lista textual de IDs dos atendimentos consolidados (síntese compilada do paciente).'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador LONGTEXT NULL COMMENT 'FK para membros.id do profissional criador (unitário) ou descritor analise_compilada_paciente nas sínteses compiladas.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql, reason="garantir_schema_analise_compilada_paciente")
        except RuntimeError as e:
            msg = str(e).lower()
            if 'duplicate key name' in msg or 'already exists' in msg:
                continue
            log.warning(f"⚠️  garantir_schema_analise_compilada_paciente: {e}")

    global _COLUNAS_TABELA
    _COLUNAS_TABELA = None


def garantir_schema_analise_compilada_paciente():
    """
    Libera a própria chatgpt_atendimentos_analise para armazenar uma síntese
    longitudinal do paciente usando apenas id_paciente como localizador.
    """
    ajustes = [
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento LONGTEXT NULL COMMENT 'ID do atendimento (unitário) ou lista textual de IDs dos atendimentos consolidados (síntese compilada do paciente).'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador LONGTEXT NULL COMMENT 'FK para membros.id do profissional criador (unitário) ou descritor analise_compilada_paciente nas sínteses compiladas.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql, reason="garantir_schema_analise_compilada_paciente")
        except RuntimeError as e:
            msg = str(e).lower()
            if 'duplicate key name' in msg or 'already exists' in msg:
                continue
            log.warning(f"⚠️  garantir_schema_analise_compilada_paciente: {e}")

    global _COLUNAS_TABELA
    _COLUNAS_TABELA = None


def garantir_schema_analise_compilada_paciente():
    """
    Libera a própria chatgpt_atendimentos_analise para armazenar uma síntese
    longitudinal do paciente usando apenas id_paciente como localizador.
    """
    ajustes = [
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento LONGTEXT NULL COMMENT 'ID do atendimento (unitário) ou lista textual de IDs dos atendimentos consolidados (síntese compilada do paciente).'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador LONGTEXT NULL COMMENT 'FK para membros.id do profissional criador (unitário) ou descritor analise_compilada_paciente nas sínteses compiladas.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql, reason="garantir_schema_analise_compilada_paciente")
        except RuntimeError as e:
            msg = str(e).lower()
            if 'duplicate key name' in msg or 'already exists' in msg:
                continue
            log.warning(f"⚠️  garantir_schema_analise_compilada_paciente: {e}")

    global _COLUNAS_TABELA
    _COLUNAS_TABELA = None


def garantir_migracoes():
    """
    Aplica migrações pendentes da pasta Scripts/migrations e remove o arquivo
    SQL após sucesso completo. Assim, cada atualização roda apenas uma vez.
    Placeholders suportados no SQL: __TABELA__.
    """
    migrations_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
    if not os.path.isdir(migrations_dir):
        return

    migration_files = sorted(
        name for name in os.listdir(migrations_dir)
        if name.lower().endswith(".sql") and name.startswith("analisador_")
    )
    if not migration_files:
        return

    def _split_sql_statements(content: str) -> list[str]:
        cleaned = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
        lines = []
        for ln in cleaned.splitlines():
            stripped = ln.strip()
            if not stripped or stripped.startswith("--") or stripped.startswith("#"):
                continue
            lines.append(ln)
        merged = "\n".join(lines)
        return [stmt.strip() for stmt in merged.split(";") if stmt.strip()]

    for filename in migration_files:
        path = os.path.join(migrations_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                sql_content = f.read()
            sql_content = sql_content.replace("__TABELA__", TABELA)
            statements = _split_sql_statements(sql_content)
            if not statements:
                log.warning(f"⚠️ Migração sem comandos executáveis: {filename}")
                continue

            for idx, statement in enumerate(statements, 1):
                sql_exec(statement, reason=f"garantir_migracoes:{filename}:{idx}")

            os.remove(path)
            log.info(f"✅ Migração aplicada e removida: {filename}")
        except RuntimeError as e:
            log.warning(f"⚠️  Migração '{filename}': {e}")
        except Exception as e:
            log.warning(f"⚠️  Migração '{filename}' (erro inesperado): {e}")

    global _COLUNAS_TABELA
    _COLUNAS_TABELA = None   # força releitura do schema após migrações


# ─────────────────────────────────────────────────────────────
# SENTENCE-TRANSFORMERS — LAZY LOADING
# ─────────────────────────────────────────────────────────────
_EMBEDDING_MODEL = None

def _get_embedding_model():
    """Carrega o modelo sentence-transformers na primeira chamada (lazy)."""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        log.info(f"🧠 Carregando modelo de embeddings: {EMBEDDING_MODEL_NAME} ...")
        from sentence_transformers import SentenceTransformer
        _EMBEDDING_MODEL = SentenceTransformer(EMBEDDING_MODEL_NAME)
        log.info(f"✅ Modelo carregado ({_EMBEDDING_MODEL.get_sentence_embedding_dimension()} dimensões).")
    return _EMBEDDING_MODEL


# ─────────────────────────────────────────────────────────────
# TABELAS AUXILIARES (embeddings + casos semelhantes)
# ─────────────────────────────────────────────────────────────

def garantir_tabela_embeddings():
    """Garante que as tabelas de embeddings e casos semelhantes existem."""
    try:
        sql_exec("""
            CREATE TABLE IF NOT EXISTS chatgpt_embeddings_prontuario (
                id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                id_atendimento      INT(10)       NOT NULL COMMENT 'FK clinica_atendimentos.id',
                id_paciente         VARCHAR(800)  NOT NULL COMMENT 'FK membros.id',
                embedding_model     VARCHAR(100)  NOT NULL COMMENT 'Nome do modelo de embedding',
                embedding_dim       INT UNSIGNED  NOT NULL COMMENT 'Dimensao do vetor',
                embedding_vector    LONGTEXT      NOT NULL COMMENT 'JSON array com o vetor de embedding',
                hash_prontuario     CHAR(64)      NULL     COMMENT 'SHA-256 do texto do prontuario',
                datetime_criacao    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_paciente (id_paciente),
                INDEX idx_hash     (hash_prontuario)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, reason="garantir_tabela_embeddings")
        log.info("✅ Tabela chatgpt_embeddings_prontuario verificada/criada.")
    except RuntimeError as e:
        log.warning(f"⚠️  garantir_tabela_embeddings: {e}")

    try:
        sql_exec("""
            CREATE TABLE IF NOT EXISTS chatgpt_casos_semelhantes (
                id                       INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                id_atendimento_origem    INT(10)      NOT NULL COMMENT 'FK atendimento analisado',
                id_paciente_origem       VARCHAR(800) NOT NULL COMMENT 'FK paciente do atendimento analisado',
                id_atendimento_destino   INT(10)      NOT NULL COMMENT 'FK atendimento semelhante',
                id_paciente_destino      VARCHAR(800) NULL     COMMENT 'FK paciente do atendimento semelhante',
                embedding_model          VARCHAR(100) NOT NULL COMMENT 'Modelo usado no calculo',
                score_similaridade       FLOAT        NOT NULL COMMENT 'Cosine similarity 0-1',
                datetime_calculo         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_par (id_atendimento_origem, id_atendimento_destino),
                INDEX idx_origem  (id_atendimento_origem),
                INDEX idx_destino (id_atendimento_destino)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """, reason="garantir_tabela_casos_semelhantes")
        log.info("✅ Tabela chatgpt_casos_semelhantes verificada/criada.")
    except RuntimeError as e:
        log.warning(f"⚠️  garantir_tabela_casos_semelhantes: {e}")


# ─────────────────────────────────────────────────────────────
# GERAÇÃO DE EMBEDDING + BUSCA DE SIMILARES
# ─────────────────────────────────────────────────────────────

def gerar_embedding(texto: str) -> list:
    """Gera vetor de embedding para o texto usando sentence-transformers."""
    model = _get_embedding_model()
    vetor = model.encode(texto, show_progress_bar=False, normalize_embeddings=True)
    return vetor.tolist()


def salvar_embedding(id_atendimento: int, id_paciente: str, vetor: list, hash_pront: str):
    """Persiste o embedding no banco (INSERT ou UPDATE se já existir)."""
    dim = len(vetor)
    vetor_json = json.dumps(vetor, ensure_ascii=False)
    sql_exec(f"""
        INSERT INTO chatgpt_embeddings_prontuario
            (id_atendimento, id_paciente, embedding_model, embedding_dim, embedding_vector, hash_prontuario, datetime_criacao)
        VALUES
            ({int(id_atendimento)}, _utf8mb4'{esc(id_paciente)}', _utf8mb4'{esc(EMBEDDING_MODEL_NAME)}', {dim},
             _utf8mb4'{esc(vetor_json)}', _utf8mb4'{esc(hash_pront)}', NOW())
        ON DUPLICATE KEY UPDATE
            embedding_vector = VALUES(embedding_vector),
            embedding_model  = VALUES(embedding_model),
            embedding_dim    = VALUES(embedding_dim),
            hash_prontuario  = VALUES(hash_prontuario),
            datetime_criacao = NOW()
    """, reason="salvar_embedding")


def buscar_casos_semelhantes(id_atendimento: int, vetor_origem: list) -> list:
    """
    Busca embeddings existentes no banco, calcula cosine similarity e
    retorna os TOP_K mais semelhantes (acima de SIMILARIDADE_MIN).
    """
    import numpy as np

    try:
        resp = sql_exec(
            f"SELECT id_atendimento, id_paciente, embedding_vector "
            f"FROM chatgpt_embeddings_prontuario "
            f"WHERE id_atendimento != {int(id_atendimento)} "
            f"ORDER BY datetime_criacao DESC "
            f"LIMIT 500",
            reason="buscar_embeddings_similares"
        )
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao buscar embeddings existentes: {e}")
        return []

    rows = resp.get("data") or []
    if not rows:
        return []

    origem = np.array(vetor_origem, dtype=np.float32)

    resultados = []
    for r in rows:
        try:
            v = json.loads(r["embedding_vector"])
            destino = np.array(v, dtype=np.float32)
            # cosine similarity (vetores já normalizados pelo encode, mas por segurança)
            sim = float(np.dot(origem, destino) / (np.linalg.norm(origem) * np.linalg.norm(destino) + 1e-9))
            if sim >= SIMILARIDADE_MIN:
                resultados.append({
                    "id_atendimento_semelhante": int(r["id_atendimento"]),
                    "id_paciente":               r["id_paciente"],
                    "score_similaridade":         round(sim, 4),
                })
        except Exception:
            continue

    resultados.sort(key=lambda x: x["score_similaridade"], reverse=True)
    return resultados[:SIMILARIDADE_TOP_K]


def salvar_casos_semelhantes(id_atendimento: int, id_paciente: str, casos: list):
    """Salva cada par de caso semelhante na tabela auxiliar + atualiza coluna JSON na análise."""
    if not casos:
        return

    # 1. Tabela auxiliar — UPSERT compatível com o parser de segurança do PHP
    for caso in casos:
        id_dest   = int(caso["id_atendimento_semelhante"])
        id_pc_dst = esc(caso.get("id_paciente") or "")
        score     = float(caso["score_similaridade"])
        try:
            sql_exec(f"""
                INSERT INTO chatgpt_casos_semelhantes
                    (id_atendimento_origem, id_paciente_origem, id_atendimento_destino,
                     id_paciente_destino, embedding_model, score_similaridade, datetime_calculo)
                VALUES
                    ({int(id_atendimento)}, _utf8mb4'{esc(id_paciente)}', {id_dest},
                     _utf8mb4'{id_pc_dst}', _utf8mb4'{esc(EMBEDDING_MODEL_NAME)}', {score}, NOW())
                ON DUPLICATE KEY UPDATE
                    id_paciente_destino = VALUES(id_paciente_destino),
                    embedding_model = VALUES(embedding_model),
                    score_similaridade = VALUES(score_similaridade),
                    datetime_calculo = VALUES(datetime_calculo)
            """, reason="salvar_casos_semelhantes_upsert")
        except Exception as e:
            log.warning(f"  ⚠️ Erro ao salvar caso semelhante (dest={id_dest}): {e}")

    # 2. Atualiza coluna JSON na tabela principal (para consumo pelo PHP/frontend)
    try:
        casos_json = json.dumps(casos, ensure_ascii=False)
        sql_exec(f"""
            UPDATE {TABELA} SET
                casos_semelhantes = _utf8mb4'{esc(casos_json)}'
            WHERE id_atendimento = {int(id_atendimento)}
        """, reason="salvar_casos_semelhantes_json")
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao atualizar casos_semelhantes na análise: {e}")


def executar_pipeline_embedding(id_atendimento: int, id_paciente: str, texto: str, hash_pront: str):
    """
    Pipeline completo de embeddings:
      1. Gerar embedding do prontuário
      2. Salvar na tabela chatgpt_embeddings_prontuario
      3. Buscar casos semelhantes
      4. Salvar na tabela chatgpt_casos_semelhantes + coluna JSON
    """
    log.info(f"  🧠 Gerando embedding ({EMBEDDING_MODEL_NAME})...")
    vetor = gerar_embedding(texto)
    log.info(f"  📐 Embedding: {len(vetor)} dimensões")

    salvar_embedding(id_atendimento, id_paciente, vetor, hash_pront)
    log.info(f"  💾 Embedding salvo em chatgpt_embeddings_prontuario")

    log.info(f"  🔍 Buscando casos semelhantes...")
    casos = buscar_casos_semelhantes(id_atendimento, vetor)
    if casos:
        salvar_casos_semelhantes(id_atendimento, id_paciente, casos)
        for c in casos:
            log.info(f"     📌 Atendimento {c['id_atendimento_semelhante']} → score {c['score_similaridade']:.4f}")
    else:
        log.info(f"  🔍 Nenhum caso semelhante encontrado (min={SIMILARIDADE_MIN})")

    return casos


# ─────────────────────────────────────────────────────────────
# CACHE DE COLUNAS DA TABELA
# ─────────────────────────────────────────────────────────────

# Conjunto de colunas reais da tabela (populado na primeira chamada).
# Invalide atribuindo None quando a estrutura da tabela mudar.
_COLUNAS_TABELA = None


def _get_colunas_tabela(force=False):
    """
    Retorna o conjunto de nomes de colunas existentes na tabela.
    O resultado é mantido em memória para evitar consultas repetidas ao BD.
    Passe force=True para recarregar após uma alteração de schema.
    """
    global _COLUNAS_TABELA
    if _COLUNAS_TABELA is not None and not force:
        return _COLUNAS_TABELA
    try:
        resp = sql_exec(f"""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = '{TABELA}'
        """, reason="carregar_colunas_tabela")
        _COLUNAS_TABELA = {r["COLUMN_NAME"] for r in (resp.get("data") or [])}
        log.debug(f"[schema] {len(_COLUNAS_TABELA)} colunas carregadas para {TABELA}.")
    except Exception as e:
        log.warning(f"⚠️ _get_colunas_tabela: {e}")
        _COLUNAS_TABELA = set()
    return _COLUNAS_TABELA


# ─────────────────────────────────────────────────────────────
# MAPEAMENTOS PARA salvar_resultado DINÂMICO
# ─────────────────────────────────────────────────────────────

# Chaves do JSON cujo nome difere do nome real da coluna na tabela.
# Formato:  "chave_no_json": "nome_da_coluna_no_banco"
_ALIAS_JSON_PARA_COLUNA = {
    # V7 / formato legado
    "condutas_registradas_no_prontuario": "condutas_no_prontuario",
    # V16 -- novas chaves JSON para colunas SQL existentes
    "diagnosticos_mencionados":           "diagnosticos_citados",
    "sinais_sintomas":                    "sinais_nucleares",
    "mudancas_clinicas_relevantes":        "mudancas_relevantes",
    "medicamentos_em_uso":                "medicacoes_em_uso",
    "medicamentos_iniciados":             "medicacoes_iniciadas",
    "medicamentos_suspensos":             "medicacoes_suspensas",
    "terapias_citadas":                   "terapias_referidas",
    "exames_mencionados":                 "exames_citados",
    "condutas_registradas":               "condutas_no_prontuario",
    "condutas_sugeridas_llm":             "condutas_especificas_sugeridas",
    "resumo_clinico_objetivo":            "resumo_texto",
    "similaridade_casos":                 "casos_semelhantes",
}

# Chaves do JSON que são objetos e devem ser "explodidas" em sub-colunas.
# A chave-pai NÃO é salva diretamente — somente os campos expandidos abaixo.
# Formato:  "chave_json": [ ("coluna_destino", lambda valor: subcampo), ... ]
_EXPANSAO_CAMPOS = {
    # V7 formato: {"valor": 8, "unidade": "anos"}
    "idade_paciente": [
        ("idade_paciente_valor",   lambda v: v.get("valor")   if isinstance(v, dict) else None),
        ("idade_paciente_unidade", lambda v: v.get("unidade") if isinstance(v, dict) else None),
    ],
    # V16 formato: {"idade": 3, "sexo": "masculino"}
    "identificacao_paciente": [
        ("idade_paciente_valor",   lambda v: v.get("idade")   if isinstance(v, dict) else None),
        ("idade_paciente_unidade", lambda v: "anos"           if isinstance(v, dict) and v.get("idade") else None),
    ],
}


def _val_para_sql(val):
    """
    Converte qualquer valor Python para um fragmento SQL seguro com charset UTF-8.

    • None / string vazia  →  NULL
    • dict / list          →  _utf8mb4'<json_escapado>'
    • escalares            →  _utf8mb4'<str_escapado>'
    """
    if val is None:
        return "NULL"
    if isinstance(val, (dict, list)):
        return f"_utf8mb4'{esc(json.dumps(val, ensure_ascii=False))}'"
    texto = str(val).strip()
    if not texto:
        return "NULL"
    return f"_utf8mb4'{esc(texto)}'"



def _normalizar_motivo_esgotado(erro_msg: str) -> str:
    """Extrai um motivo legível/agrupável a partir do erro acumulado do registro."""
    texto = re.sub(r"\s+", " ", str(erro_msg or "")).strip(" |")
    if not texto:
        return "Sem mensagem de erro registrada"

    partes = [p.strip() for p in texto.split("|") if p.strip()]
    partes_validas = [
        p for p in partes
        if not p.startswith("[AUTO-RESET")
        and not p.startswith("[AUTO-RESET-STARTUP")
    ]
    motivo = (partes_validas[-1] if partes_validas else partes[-1] if partes else texto).strip()
    motivo = re.sub(r"\s+", " ", motivo)
    motivo_lower = motivo.lower()

    if "texto insuficiente após remoção de html" in motivo_lower:
        return "Prontuário ficou insuficiente após limpeza/remoção de HTML."

    if (
        "simulador não retornou conteúdo markdown" in motivo_lower
        or "llm não retornou conteúdo markdown" in motivo_lower
    ):
        return "LLM não retornou resposta final em markdown utilizável."

    if (
        "llm não retornou json válido" in motivo_lower
        or "llm não retornou bloco json" in motivo_lower
        or "expecting ',' delimiter" in motivo_lower
        or "expecting property name enclosed in double quotes" in motivo_lower
        or "unterminated string" in motivo_lower
        or "invalid control character" in motivo_lower
        or "extra data" in motivo_lower
    ):
        return "LLM retornou JSON inválido/malformado para o schema esperado."

    if "api_exec recusou" in motivo_lower or "execute_sql recusou" in motivo_lower:
        return "Falha de persistência/execução SQL ao salvar ou consultar a análise."

    if "simulador retornou erro" in motivo_lower:
        return "ChatGPT Simulator retornou erro durante a análise."

    return motivo[:180] + ("..." if len(motivo) > 180 else "")


def _agrupar_motivos_esgotados(rows: list[dict]) -> list[dict]:
    contador = Counter()
    for row in rows or []:
        motivo = _normalizar_motivo_esgotado((row or {}).get("erro_msg"))
        contador[motivo] += 1
    return [
        {"motivo": motivo, "total": total}
        for motivo, total in contador.most_common(5)
    ]


def buscar_pendentes() -> dict:
    stats = sql_exec(f"""
        SELECT
            COUNT(*) AS total_tabela,
            SUM(COALESCE(la.id_criador, '') = 'analise_compilada_paciente') AS total_analises_compiladas_paciente,
            SUM(COALESCE(la.id_criador, '') = 'analise_compilada_paciente' AND la.status IN ('pendente', 'erro') AND (la.status = 'pendente' OR la.tentativas < {MAX_TENTATIVAS})) AS total_compiladas_pendentes,
            SUM(
                COALESCE(la.id_criador, '') <> 'analise_compilada_paciente'
                AND la.status = 'concluido'
                AND NOT IFNULL(
                    COALESCE(
                        NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                        NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
                    ) > la.datetime_analise_concluida,
                    0
                )
            ) AS total_concluidos,
            SUM(COALESCE(la.id_criador, '') <> 'analise_compilada_paciente' AND la.status = 'pendente') AS total_pendentes,
            SUM(COALESCE(la.id_criador, '') <> 'analise_compilada_paciente' AND la.status = 'processando') AS total_processando,
            SUM(COALESCE(la.id_criador, '') <> 'analise_compilada_paciente' AND la.status = 'erro' AND la.tentativas < {MAX_TENTATIVAS}) AS total_erros,
            SUM(COALESCE(la.id_criador, '') <> 'analise_compilada_paciente' AND la.status = 'erro' AND la.tentativas >= {MAX_TENTATIVAS}) AS total_esgotados,
            SUM(
                COALESCE(la.id_criador, '') <> 'analise_compilada_paciente'
                AND la.status = 'concluido'
                AND IFNULL(
                    COALESCE(
                        NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                        NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
                    ) > la.datetime_analise_concluida,
                    0
                )
            ) AS total_desatualizados
        FROM {TABELA} la
        LEFT JOIN clinica_atendimentos ca ON ca.id = la.id_atendimento
    """, reason="estatisticas_pendentes")
    row = (stats.get("data") or [{}])[0]

    # Consulta principal de pendentes (unitários) – exclui compiladas e registros sem atendimento
    data = sql_exec(f"""
        SELECT
            la.id_atendimento       AS id,
            la.id_paciente,
            la.id_criador,
            la.datetime_atendimento_inicio,
            la.datetime_ultima_atualizacao_atendimento,
            la.chat_id              AS chat_id_anterior,
            la.chat_url             AS chat_url_anterior,
            LEFT(ca.consulta_conteudo, 60000)   AS consulta_conteudo,
            COALESCE(
                NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
            ) AS datetime_prontuario_atual
        FROM {TABELA} la
        LEFT JOIN clinica_atendimentos ca ON ca.id = la.id_atendimento
        WHERE
            la.status != 'processando'
            AND COALESCE(la.id_criador, '') NOT IN ('analise_compilada_paciente', '')
            AND la.id_atendimento IS NOT NULL
            AND (
                la.status = 'pendente'
                OR (la.status = 'erro' AND la.tentativas < {MAX_TENTATIVAS})
                OR (la.status = 'concluido'
                    AND COALESCE(
                            NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                            NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
                        ) > la.datetime_analise_concluida)
            )
        ORDER BY
            /* Faixa 1 (prioridade): atendimentos com menos de 30 dias — ASC (mais antigos primeiro,
               pois são pacientes recentes cujas dúvidas o usuário pode precisar consultar em breve).
               Faixa 2: atendimentos com 30+ dias — DESC (mais novos primeiro dentro dos antigos,
               pois os muito antigos dificilmente serão revisitados). */
            CASE WHEN la.datetime_atendimento_inicio >= DATE_SUB(NOW(), INTERVAL 30 DAY)
                 THEN 0 ELSE 1 END ASC,
            CASE WHEN la.datetime_atendimento_inicio >= DATE_SUB(NOW(), INTERVAL 30 DAY)
                 THEN la.datetime_atendimento_inicio END ASC,
            CASE WHEN la.datetime_atendimento_inicio < DATE_SUB(NOW(), INTERVAL 30 DAY)
                 THEN la.datetime_atendimento_inicio END DESC
        LIMIT {BATCH_SIZE}
    """, reason="listar_pendentes")

    pendentes = data.get("data", [])

    # Diagnóstico de pendentes unitários (mantido)
    total_pendentes_contagem = int(row.get("total_pendentes") or 0)
    if pendentes:
        ids_pendentes = [str(p.get("id")) for p in pendentes if p.get("id") is not None]
        log.info(f"   🔍 Pendentes retornados: {len(pendentes)} registros, IDs: {', '.join(ids_pendentes)}")
    elif total_pendentes_contagem > 0:
        log.warning(f"   ⚠️ {total_pendentes_contagem} pendente(s) contado(s), mas nenhum foi retornado pela query de listagem.")
        diag = sql_exec(f"""
            SELECT id_atendimento, id, status, tentativas, id_criador
            FROM {TABELA}
            WHERE status = 'pendente'
              AND COALESCE(id_criador, '') NOT IN ('analise_compilada_paciente', '')
            LIMIT 30
        """, reason="diagnostico_pendentes_nao_listados")
        diag_rows = diag.get("data", [])
        if diag_rows:
            log.info(f"      📋 Detalhes dos pendentes não listados (primeiros 30):")
            for r in diag_rows:
                log.info(f"         - id_atendimento={r.get('id_atendimento')}, id={r.get('id')}, status={r.get('status')}, tentativas={r.get('tentativas')}, id_criador={r.get('id_criador')}")
        else:
            log.info(f"      ℹ️ Nenhum registro pendente unitário encontrado na tabela (inconsistência?).")
    else:
        log.info("   ℹ️ Nenhum pendente unitário retornado (total_pendentes = 0)")

    # Busca erros esgotados
    esgotados_rows = sql_exec(f"""
        SELECT la.erro_msg
        FROM {TABELA} la
        WHERE
            COALESCE(la.id_criador, '') <> 'analise_compilada_paciente'
            AND la.status = 'erro'
            AND la.tentativas >= {MAX_TENTATIVAS}
        ORDER BY la.id DESC
        LIMIT 200
    """, reason="listar_esgotados")

    # Buscar sínteses compiladas de pacientes pendentes – inclui registros antigos (id_criador NULL e id_atendimento NULL)
    compiladas_pendentes = sql_exec(f"""
        SELECT la.id, la.id_paciente
        FROM {TABELA} la
        WHERE
            (
                la.id_criador = 'analise_compilada_paciente'
                OR (la.id_criador IS NULL AND la.id_atendimento IS NULL)
            )
            AND la.status IN ('pendente', 'erro')
            AND (la.status = 'pendente' OR la.tentativas < {MAX_TENTATIVAS})
        ORDER BY la.datetime_analise_criacao ASC
        LIMIT {BATCH_SIZE}
    """, reason="buscar_compiladas_pendentes")

    comp_pend = compiladas_pendentes.get("data", [])
    if comp_pend:
        ids_comp = [str(c.get("id")) for c in comp_pend if c.get("id") is not None]
        log.info(f"   🧬 Sínteses compiladas pendentes: {len(comp_pend)} registros, IDs: {', '.join(ids_comp)}")
    else:
        log.info("   ℹ️ Nenhuma síntese compilada pendente.")

    return {
        "pendentes":            pendentes,
        "compiladas_pendentes": comp_pend,
        "total_tabela":         int(row.get("total_tabela")         or 0),
        "total_analises_compiladas_paciente": int(row.get("total_analises_compiladas_paciente") or 0),
        "total_compiladas_pendentes": int(row.get("total_compiladas_pendentes") or 0),
        "total_concluidos":     int(row.get("total_concluidos")     or 0),
        "total_pendentes":      total_pendentes_contagem,
        "total_processando":    int(row.get("total_processando")    or 0),
        "total_erros":          int(row.get("total_erros")          or 0),
        "total_esgotados":      int(row.get("total_esgotados")      or 0),
        "motivos_esgotados":    _agrupar_motivos_esgotados(esgotados_rows.get("data", [])),
        "total_desatualizados": int(row.get("total_desatualizados") or 0),
    }

def enfileirar_atendimentos_antigos(id_paciente: str) -> int:
    """
    Verifica se o paciente tem atendimentos em clinica_atendimentos que
    ainda não foram incluídos na tabela de análises (chatgpt_atendimentos_analise).

    Para cada atendimento encontrado, insere um registro com status='pendente'
    para que o daemon analise automaticamente no próximo ciclo.

    Filtros aplicados (mesmos do buscar_pendentes):
      • consulta_tipo_arquivo = 'texto'
      • consulta_conteudo IS NOT NULL
      • LENGTH(consulta_conteudo) > MIN_CHARS

    Retorna o número de atendimentos enfileirados.
    """
    try:
        resp = sql_exec(f"""
            SELECT
                ca.id              AS id_atendimento,
                ca.id_paciente,
                ca.id_criador,
                ca.datetime_consulta_inicio
            FROM clinica_atendimentos ca
            LEFT JOIN {TABELA} la ON la.id_atendimento = ca.id
            WHERE ca.id_paciente = '{esc(id_paciente)}'
              AND la.id IS NULL
              AND ca.consulta_tipo_arquivo = 'texto'
              AND ca.consulta_conteudo IS NOT NULL
              AND LENGTH(ca.consulta_conteudo) > {MIN_CHARS}
            ORDER BY ca.datetime_consulta_inicio ASC
        """, reason="enfileirar_antigos")
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao buscar atendimentos antigos do paciente {id_paciente}: {e}")
        return 0

    atendimentos = resp.get("data") or []
    if not atendimentos:
        return 0

    valores_sql = []
    for at in atendimentos:
        id_at  = int(at["id_atendimento"])
        id_pac = esc(at.get("id_paciente") or id_paciente)
        id_cri = esc(at.get("id_criador") or "0")
        dt_ini = esc(at.get("datetime_consulta_inicio") or "0000-00-00 00:00:00")
        valores_sql.append(
            f"({id_at}, _utf8mb4'{id_pac}', _utf8mb4'{id_cri}', '{dt_ini}', 'pendente')"
        )

    if not valores_sql:
        return 0

    try:
        resultado_insert = sql_exec(f"""
            INSERT INTO {TABELA}
                (id_atendimento, id_paciente, id_criador, datetime_atendimento_inicio, status)
            VALUES
                {", ".join(valores_sql)}
        """)
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao salvar atendimentos antigos do paciente {id_paciente}: {e}")
        return 0

    enfileirados = int(resultado_insert.get("affected_rows") or 0)

    if enfileirados:
        log.info(f"  📥 {enfileirados} atendimento(s) antigo(s) do paciente {id_paciente} enfileirado(s) para análise")
    else:
        log.info(
            f"  ℹ️ Atendimentos antigos encontrados para o paciente {id_paciente}, "
            f"mas nenhum novo registro precisou ser inserido na tabela de análise."
        )

    return enfileirados




def _obter_datas_referencia_compilada(id_paciente: str):
    """
    Para a síntese compilada do paciente, retorna:
      - a data/hora de início de atendimento mais antiga já analisada;
      - a data/hora de última atualização mais recente já analisada.

    Ambas as referências são calculadas a partir das análises unitárias
    (exclui o próprio registro compilado).
    """
    try:
        resp = sql_exec(f"""
            SELECT
                MIN(NULLIF(datetime_atendimento_inicio, '0000-00-00 00:00:00')) AS dt_inicio_mais_antiga,
                MAX(NULLIF(datetime_ultima_atualizacao_atendimento, '0000-00-00 00:00:00')) AS dt_ultima_atualizacao_mais_nova
            FROM {TABELA}
            WHERE id_paciente = '{esc(id_paciente)}'
              AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
        """, reason="_obter_datas_referencia_compilada")
        data = resp.get("data") or []
        row = data[0] if data else {}
        return row.get('dt_inicio_mais_antiga'), row.get('dt_ultima_atualizacao_mais_nova')
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao obter datas de referência da síntese compilada do paciente {id_paciente}: {e}")
        return None, None
def contar_atendimentos_nao_concluidos_paciente(id_paciente: str) -> int:
    """Conta atendimentos *realmente pendentes de processamento* para o paciente.

    Regras:
    - Bloqueiam a síntese compilada apenas registros com status pendente/processando;
    - Apenas se o prontuário for processável pelo pipeline atual (mesmos filtros de fila);
    - Registros em erro (com ou sem retentativa) não bloqueiam a compilada.
    """
    try:
        resp = sql_exec(f"""
            SELECT COUNT(*) AS total
            FROM {TABELA} la
            INNER JOIN clinica_atendimentos ca ON ca.id = la.id_atendimento
            WHERE la.id_paciente = '{esc(id_paciente)}'
              AND COALESCE(la.id_criador, '') <> 'analise_compilada_paciente'
              AND la.status IN ('pendente', 'processando')
              AND ca.consulta_tipo_arquivo = 'texto'
              AND ca.consulta_conteudo IS NOT NULL
              AND LENGTH(ca.consulta_conteudo) > {MIN_CHARS}
              AND (
                  la.status IN ('pendente', 'processando')
                  OR (
                        la.status = 'concluido'
                        AND COALESCE(
                            NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                            NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
                        ) > la.datetime_analise_concluida
                    )
              )
        """, reason="contar_atendimentos_nao_concluidos_paciente")
        data = resp.get("data") or []
        if not data:
            return 0
        return int(data[0].get("total") or 0)
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao contar atendimentos não concluídos do paciente {id_paciente}: {e}")
        return 0


def listar_atendimentos_com_erro_paciente(id_paciente: str) -> list[dict]:
    """Lista atendimentos unitários do paciente que estão em erro.

    Usado para explicitar falhas no log e no contexto da síntese compilada,
    sem bloquear a compilação longitudinal.
    """
    try:
        data = sql_exec(f"""
            SELECT
                id_atendimento,
                tentativas,
                COALESCE(erro_msg, '') AS erro_msg
            FROM {TABELA}
            WHERE id_paciente = '{esc(id_paciente)}'
              AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
              AND status = 'erro'
            ORDER BY id DESC
        """, reason="listar_atendimentos_com_erro_paciente").get("data", [])
        return data or []
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao listar atendimentos com falha do paciente {id_paciente}: {e}")
        return []


def garantir_registro_compilado_paciente_pendente(id_paciente: str) -> int:
    """
    Garante a existência do registro da síntese longitudinal do paciente.

    O registro é criado/atualizado com status='pendente' somente depois que todos
    os atendimentos unitários do paciente já estiverem concluídos, para que a
    síntese do paciente possa entrar na fila com id_atendimento NULL.
    """
    dt_inicio_mais_antiga, dt_ultima_atualizacao_mais_nova = _obter_datas_referencia_compilada(id_paciente)
    dt_inicio_sql = esc_str(str(dt_inicio_mais_antiga)) if dt_inicio_mais_antiga else "NULL"
    dt_ultima_sql = esc_str(str(dt_ultima_atualizacao_mais_nova)) if dt_ultima_atualizacao_mais_nova else "NULL"
    dt_inicio_mais_antiga, dt_ultima_atualizacao_mais_nova = _obter_datas_referencia_compilada(id_paciente)
    dt_inicio_sql = esc_str(str(dt_inicio_mais_antiga)) if dt_inicio_mais_antiga else "NULL"
    dt_ultima_sql = esc_str(str(dt_ultima_atualizacao_mais_nova)) if dt_ultima_atualizacao_mais_nova else "NULL"

    existente = sql_exec(f"""
        SELECT id
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND id_criador = 'analise_compilada_paciente'
        ORDER BY id DESC
        LIMIT 1
    """, reason="buscar_registro_compilado_pendente").get("data", [])

    if existente:
        id_registro = int(existente[0]["id"])
        sql_exec(f"""
            UPDATE {TABELA} SET
                id_atendimento = NULL,
                datetime_atendimento_inicio = {dt_inicio_sql},
                datetime_ultima_atualizacao_atendimento = {dt_ultima_sql},
                id_criador = 'analise_compilada_paciente',
                status = 'pendente',
                erro_msg = NULL,
                chat_id = '',
                chat_url = '',
                modelo_llm = {esc_str(LLM_MODEL)},
                prompt_version = {esc_str(PROMPT_VERSION)}
            WHERE id = {id_registro}
        """, reason="atualizar_registro_compilado_pendente")
        return id_registro

    sql_exec(f"""
        INSERT INTO {TABELA}
            (id_atendimento, id_paciente, id_criador, datetime_atendimento_inicio,
             datetime_ultima_atualizacao_atendimento, status, tentativas, erro_msg,
             modelo_llm, prompt_version, chat_id, chat_url)
        VALUES
            (NULL, {esc_str(id_paciente)}, 'analise_compilada_paciente', {dt_inicio_sql},
             {dt_ultima_sql}, 'pendente', 0, NULL,
             {esc_str(LLM_MODEL)}, {esc_str(PROMPT_VERSION)}, '', '')
    """, reason="criar_registro_compilado_pendente")

    criado = sql_exec(f"""
        SELECT id
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND id_criador = 'analise_compilada_paciente'
        ORDER BY id DESC
        LIMIT 1
    """, reason="buscar_registro_compilado_pendente_criado").get("data", [])
    if not criado:
        raise RuntimeError(f"Falha ao criar registro compilado pendente do paciente {id_paciente}")
    return int(criado[0]["id"])

def marcar_processando(row: dict):
    idat = int(row["id"])
    dtp  = esc(row.get("datetime_prontuario_atual") or "")

    sql_exec(f"""
        UPDATE {TABELA} SET
            status                                  = 'processando',
            tentativas                              = tentativas + 1,
            datetime_analise_iniciada               = NOW(),
            datetime_ultima_atualizacao_atendimento = {f"'{dtp}'" if dtp else 'NULL'}
        WHERE id_atendimento = {idat}
    """, reason="marcar_processando_unitario")



def resetar_travados():
    """
    Detecta registros presos em 'processando' há mais de TIMEOUT_PROCESSANDO_MIN
    e os reverte para 'pendente', incrementando tentativas para rastreio.
    """
    resultado = sql_exec(
        f"""
        UPDATE {TABELA}
        SET    status    = 'pendente',
               datetime_analise_iniciada = NULL,
               erro_msg  = CONCAT(
                               COALESCE(erro_msg, ''),
                               ' | [AUTO-RESET] Travado em processando por mais de {TIMEOUT_PROCESSANDO_MIN} min em ',
                               NOW()
                           )
        WHERE  status = 'processando'
          AND  datetime_analise_criacao <= DATE_SUB(NOW(), INTERVAL {TIMEOUT_PROCESSANDO_MIN} MINUTE)
        """
    )
    afetados = resultado.get('affected_rows', 0)
    if afetados:
        log.warning(
            f"⚠️  {afetados} registro(s) travado(s) em 'processando' resetado(s) para 'pendente'."
        )
    return afetados


def resetar_analises_interrompidas_no_startup():
    """
    Ao subir o analisador, limpa datetime_analise_iniciada de registros que
    ficaram interrompidos sem conclusão válida na execução anterior.

    Isso evita que o sistema trate como "análise já iniciada" algo que será
    reprocessado do zero após uma parada/queda do processo Python.
    """
    resultado = sql_exec(
        f"""
        UPDATE {TABELA}
        SET
            status = CASE
                        WHEN status = 'processando' THEN 'pendente'
                        ELSE status
                     END,
            datetime_analise_iniciada = NULL,
            erro_msg = CONCAT(
                COALESCE(erro_msg, ''),
                ' | [AUTO-RESET-STARTUP] datetime_analise_iniciada zerado em ',
                NOW(),
                ' após interrupção anterior sem conclusão válida'
            )
        WHERE
            datetime_analise_iniciada IS NOT NULL
            AND (
                datetime_analise_concluida IS NULL
                OR datetime_analise_concluida = '0000-00-00 00:00:00'
            )
        """
    )
    afetados = resultado.get('affected_rows', 0)
    if afetados:
        log.warning(
            f"⚠️  {afetados} registro(s) com análise interrompida tiveram datetime_analise_iniciada resetado no startup."
        )
    return afetados


def _montar_sets_resultado(resultado: dict) -> list:
    colunas  = _get_colunas_tabela()
    chat_id  = esc(resultado.get("_chat_id")  or "")
    chat_url = esc(resultado.get("_chat_url") or "")
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sets = [
        f"status                    = 'concluido'",
        f"datetime_analise_concluida = '{now}'",
        f"erro_msg                   = NULL",
        f"datetime_analise_iniciada  = COALESCE(datetime_analise_iniciada, '{now}')",
        f"chat_id                    = '{chat_id}'",
        f"chat_url                   = '{chat_url}'",
    ]

    if "dados_json" in colunas:
        dados_publicos = {k: v for k, v in resultado.items() if not k.startswith("_")}
        sets.append(
            f"dados_json = _utf8mb4'{esc(json.dumps(dados_publicos, ensure_ascii=False))}'"
        )

    for chave_json, valor in resultado.items():
        if chave_json.startswith("_"):
            continue

        if chave_json in _EXPANSAO_CAMPOS:
            for coluna_dest, extrator in _EXPANSAO_CAMPOS[chave_json]:
                if coluna_dest in colunas:
                    sets.append(f"{coluna_dest} = {_val_para_sql(extrator(valor))}")
                else:
                    log.debug(f"  ↷ expansão ignorada: '{coluna_dest}' não existe.")
            continue

        coluna = _ALIAS_JSON_PARA_COLUNA.get(chave_json, chave_json)
        if coluna not in colunas:
            log.debug(f"  ↷ ignorado: coluna '{coluna}' não existe (chave: {chave_json})")
            continue

        sets.append(f"{coluna} = {_val_para_sql(valor)}")

    return sets


def salvar_resultado(id_atendimento: int, resultado: dict):
    """Persiste o resultado da análise unitária do atendimento."""
    sets = _montar_sets_resultado(resultado)
    sql = (
        f"UPDATE {TABELA} SET\n    "
        + ",\n    ".join(sets)
        + f"\nWHERE id_atendimento = {int(id_atendimento)}"
    )
    log.debug(f"[salvar_resultado] {len(sets)} campos → id_atendimento={id_atendimento}")
    sql_exec(sql, reason="salvar_resultado_unitario")


def buscar_maior_resumo_texto_paciente(id_paciente: str, id_atendimento_atual=None) -> str:
    """Retorna o maior resumo_texto concluído do paciente para fallback de evolução curta.

    Descarta resumos nulos, vazios ou que são apenas fallbacks gerados
    pelo sistema (padrão "consulta de YYYY-MM-DD HH:MM:SS:").
    """
    if not id_paciente:
        return ""

    filtro_atual = ""
    if id_atendimento_atual is not None:
        filtro_atual = f"AND id_atendimento <> {int(id_atendimento_atual)}"

    row = (sql_exec(f"""
        SELECT resumo_texto
        FROM {TABELA}
        WHERE id_paciente = '{esc(str(id_paciente))}'
          AND status = 'concluido'
          AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
          AND COALESCE(TRIM(resumo_texto), '') <> ''
          AND LOWER(TRIM(resumo_texto)) NOT REGEXP '^consulta de [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}:'
          {filtro_atual}
        ORDER BY CHAR_LENGTH(resumo_texto) DESC, datetime_analise_concluida DESC
        LIMIT 1
    """, reason="buscar_maior_resumo_texto_paciente").get("data") or [{}])[0]

    return str(row.get("resumo_texto") or "").strip()


def _montar_resumo_fallback(maior_resumo: str, dt_consulta: str, texto_consulta: str) -> str:
    """Monta resumo_fallback sem duplicar consulta do mesmo datetime.

    - Se ``maior_resumo`` já contém uma linha "Consulta de <dt_consulta>: ..."
      com o mesmo conteúdo → retorna ``maior_resumo`` inalterado.
    - Se contém a mesma data mas com conteúdo diferente → substitui a linha.
    - Se não contém → concatena no final.
    """
    sufixo = f"Consulta de {dt_consulta}: {texto_consulta}".strip()

    if not maior_resumo:
        return sufixo

    if not dt_consulta:
        return f"{maior_resumo}\n{sufixo}".strip()

    prefixo_dt = f"Consulta de {dt_consulta}:"
    prefixo_dt_lower = prefixo_dt.lower()
    linhas = maior_resumo.split("\n")
    idx_encontrado = None

    for idx, linha in enumerate(linhas):
        if linha.strip().lower().startswith(prefixo_dt_lower):
            idx_encontrado = idx
            break

    if idx_encontrado is None:
        # Datetime não presente — concatena normalmente
        return f"{maior_resumo}\n{sufixo}".strip()

    # Datetime já existe — compara conteúdo
    conteudo_existente = linhas[idx_encontrado].strip()[len(prefixo_dt):].strip()
    if conteudo_existente == texto_consulta.strip():
        # Mesmo conteúdo — nada a mudar
        return maior_resumo

    # Conteúdo mudou — substitui a linha
    linhas[idx_encontrado] = sufixo
    return "\n".join(linhas).strip()


def corrigir_erros_texto_insuficiente_no_startup():
    """
    Reprocessa erros legados de texto insuficiente sem chamar LLM:
    cria resumo_fallback e marca como concluído.
    """
    rows = sql_exec(f"""
        SELECT
            la.id_atendimento,
            la.id_paciente,
            la.erro_msg,
            LEFT(ca.consulta_conteudo, 60000) AS consulta_conteudo,
            COALESCE(
                NULLIF(ca.datetime_consulta_inicio, '0000-00-00 00:00:00'),
                NULLIF(la.datetime_atendimento_inicio, '0000-00-00 00:00:00'),
                NULLIF(ca.datetime_atualizacao, '0000-00-00 00:00:00'),
                NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
            ) AS datetime_base
        FROM {TABELA} la
        LEFT JOIN clinica_atendimentos ca ON ca.id = la.id_atendimento
        WHERE
            la.status = 'erro'
            AND COALESCE(la.id_criador, '') <> 'analise_compilada_paciente'
            AND (
                LOWER(COALESCE(la.erro_msg, '')) LIKE '%texto insuficiente após remoção de html%'
                OR LOWER(COALESCE(la.erro_msg, '')) LIKE '%prontuário ficou insuficiente após limpeza/remoção de html%'
            )
    """, reason="listar_erros_texto_insuficiente_startup").get("data", [])

    if not rows:
        return 0

    total = len(rows)
    log.info(f"♻️ Corrigindo {total} registro(s) com erro de texto insuficiente...")

    corrigidos = 0
    for i, row in enumerate(rows, 1):
        id_atendimento = int(row.get("id_atendimento") or 0)
        if not id_atendimento:
            continue

        id_paciente = str(row.get("id_paciente") or "")
        texto_curto = strip_html(row.get("consulta_conteudo") or "")
        dt_base = str(row.get("datetime_base") or "").strip()
        maior_resumo = buscar_maior_resumo_texto_paciente(id_paciente, id_atendimento)
        resumo_fallback = _montar_resumo_fallback(maior_resumo, dt_base, texto_curto)

        salvar_resultado(id_atendimento, {
            "resumo_texto": resumo_fallback,
            "observacoes_gerais": "Erro legado de texto insuficiente corrigido no startup sem chamada à LLM.",
            "pontos_chave": [],
            "condutas_sugeridas": [],
        })
        corrigidos += 1

        # Progresso inline a cada 5 registros ou no último
        if i % 5 == 0 or i == total:
            print(f"\r   ♻️ Texto insuficiente: {i}/{total} ({corrigidos} corrigidos)", end="", flush=True)

    if total:
        print()  # quebra linha após o progresso inline

    if corrigidos:
        log.warning(
            f"♻️ {corrigidos}/{total} registro(s) com erro 'Prontuário ficou insuficiente após limpeza/remoção de HTML' "
            "foram convertidos para resumo_fallback e marcados como concluídos no startup."
        )
    return corrigidos


def _stringify_compact(value) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value
    if isinstance(parsed, list):
        partes = []
        for item in parsed:
            if item in (None, "", [], {}):
                continue
            partes.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
        return "; ".join(partes)
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed or "").strip()


def _valor_compilado_para_prompt(value, max_chars: int = 1200):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value

    if isinstance(parsed, str):
        texto = re.sub(r"\s+", " ", parsed).strip()
        return texto[:max_chars]
    return parsed


def montar_texto_compilado_paciente(id_paciente: str):
    rows = sql_exec(f"""
        SELECT
            id_atendimento,
            id_paciente,
            id_criador,
            datetime_atendimento_inicio,
            resumo_texto,
            gravidade_clinica,
            diagnosticos_citados,
            pontos_chave,
            mudancas_relevantes,
            sinais_nucleares,
            eventos_comportamentais,
            terapias_referidas,
            exames_citados,
            pendencias_clinicas,
            condutas_no_prontuario,
            medicacoes_em_uso,
            medicacoes_iniciadas,
            medicacoes_suspensas,
            condutas_especificas_sugeridas,
            condutas_gerais_sugeridas,
            mensagens_acompanhamento
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND status = 'concluido'
          AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
        ORDER BY datetime_atendimento_inicio ASC, id_atendimento ASC
        LIMIT 50
    """, reason="carregar_analises_paciente_compilado").get("data", [])

    if not rows:
        return "", None

    historico = []
    datas = []
    campos_detalhe = [
        "gravidade_clinica",
        "diagnosticos_citados",
        "pontos_chave",
        "mudancas_relevantes",
        "sinais_nucleares",
        "eventos_comportamentais",
        "terapias_referidas",
        "exames_citados",
        "pendencias_clinicas",
        "condutas_no_prontuario",
        "medicacoes_em_uso",
        "medicacoes_iniciadas",
        "medicacoes_suspensas",
        "condutas_especificas_sugeridas",
        "condutas_gerais_sugeridas",
        "mensagens_acompanhamento",
    ]

    for idx, row in enumerate(rows, start=1):
        dt_at = row.get("datetime_atendimento_inicio") or ""
        if dt_at:
            datas.append(dt_at)

        item = {
            "ordem_cronologica": idx,
            "id_atendimento": row.get("id_atendimento"),
            "datetime_atendimento_inicio": dt_at or None,
            "resumo_texto": _valor_compilado_para_prompt(row.get("resumo_texto") or "", max_chars=1500),
        }

        for campo in campos_detalhe:
            valor = _valor_compilado_para_prompt(row.get(campo))
            if valor not in (None, "", [], {}):
                item[campo] = valor

        historico.append(item)

    payload_compilado = {
        "id_paciente": id_paciente,
        "total_atendimentos_analisados": len(historico),
        "periodo_primeiro_atendimento": datas[0] if datas else None,
        "periodo_ultimo_atendimento": datas[-1] if datas else None,
        "atendimentos": historico,
    }

    texto = (
        f"HISTÓRICO LONGITUDINAL COMPILADO DO PACIENTE {id_paciente}\n"
        "O conteúdo abaixo representa TODOS os atendimentos estruturados já concluídos deste paciente em ordem cronológica.\n"
        "Sua tarefa é consolidar o histórico longitudinal completo, identificando padrões persistentes, mudanças entre consultas, terapias, medicações, riscos, pendências e condutas.\n"
        "NÃO copie apenas o último atendimento; compare e sintetize o conjunto completo do histórico abaixo.\n\n"
        "DADOS COMPILADOS (JSON):\n"
        + json.dumps(payload_compilado, ensure_ascii=False, indent=2)
    )
    return texto, rows[-1]


def _listar_ids_atendimentos_compilados(id_paciente: str) -> str:
    rows = sql_exec(f"""
        SELECT id_atendimento
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND status = 'concluido'
          AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
        ORDER BY datetime_atendimento_inicio DESC, id DESC
        LIMIT 100
    """, reason="listar_ids_atendimentos_compilados").get("data", [])
    ids = []
    for row in rows:
        val = str(row.get("id_atendimento") or "").strip()
        if val:
            ids.append(val)
    return ",".join(ids)


def salvar_resultado_compilado_paciente(id_paciente: str, resultado: dict):
    id_registro = garantir_registro_compilado_paciente_pendente(id_paciente)

    sets = _montar_sets_resultado(resultado) + [
        "id_atendimento = NULL",  # síntese compilada não deve colidir com uq_atendimento de análises unitárias
        "datetime_atendimento_inicio = NULL",
        "datetime_ultima_atualizacao_atendimento = NULL",
        "id_criador = 'analise_compilada_paciente'",
        f"id_paciente = '{esc(id_paciente)}'",
    ]

    sql_exec(
        f"UPDATE {TABELA} SET\n    " + ",\n    ".join(sets) + f"\nWHERE id = {id_registro}",
        reason="salvar_resultado_compilado"
    )


def atualizar_analise_compilada_paciente(id_paciente: str):
    enfileirados = enfileirar_atendimentos_antigos(id_paciente)
    pendentes = contar_atendimentos_nao_concluidos_paciente(id_paciente)
    if pendentes > 0:
        try:
            garantir_registro_compilado_paciente_pendente(id_paciente)
        except Exception as e:
            log.warning(
                f"  ⚠️ Falha ao garantir registro pendente da síntese compilada do paciente {id_paciente}: {e}"
            )
        log.info(
            f"⏳ Síntese compilada adiada para paciente {id_paciente}: "
            f"{pendentes} atendimento(s) ainda não concluído(s)"
            + (f" ({enfileirados} recém-enfileirado(s))" if enfileirados else "")
            + "."
        )
        return

    texto_compilado, row_base = montar_texto_compilado_paciente(id_paciente)
    if not texto_compilado or not row_base:
        log.info(f"ℹ️  Sem histórico suficiente para compilar síntese do paciente {id_paciente}.")
        return

    if erros:
        linhas_erros = []
        for e in erros[:20]:
            msg = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(msg) > 600:
                msg = msg[:600] + "..."
            linhas_erros.append(
                f"- id_atendimento={e.get('id_atendimento')}, tentativas={e.get('tentativas')}, erro={msg or 'sem detalhe'}"
            )
        texto_compilado += (
            "\n\n" + ("=" * 70) + "\n\n"
            "ATENDIMENTOS COM FALHA DE ANÁLISE (não concluídos)\n"
            "Use estas falhas apenas como contexto operacional. Não invente dados clínicos ausentes.\n"
            + "\n".join(linhas_erros)
        )

    id_registro_compilado = garantir_registro_compilado_paciente_pendente(id_paciente)
    sql_exec(f"""
        UPDATE {TABELA} SET
            status = 'processando',
            tentativas = tentativas + 1,
            erro_msg = NULL,
            datetime_analise_iniciada = NOW(),
            datetime_analise_concluida = NULL,
            datetime_ultima_atualizacao_atendimento = NULL
        WHERE id = {id_registro_compilado}
    """, reason="marcar_registro_compilado_processando")

    log.info(f"🧬 Atualizando síntese compilada do paciente {id_paciente}...")
    contexto = ""
    try:
        contexto = buscar_contexto_clinico({
            "id_paciente": id_paciente,
            "id": row_base.get("id_atendimento"),
            "id_criador": row_base.get("id_criador"),
        }) or ""
    except Exception as e:
        log.warning(f"  ⚠️ Falha ao buscar contexto do paciente compilado {id_paciente}: {e}")

    resultado = analisar_prontuario(texto_compilado[:18000], contexto=contexto)
    try:
        resultado = executar_busca_evidencias(
            resultado,
            chat_url=resultado.get("_chat_url"),
            chat_id=resultado.get("_chat_id"),
        )
    except Exception as e:
        log.warning(f"  ⚠️ Enriquecimento da síntese compilada falhou (não fatal): {e}")

    try:
        if _grafo_clinico_esta_generico(resultado):
            resultado = gerar_dados_auxiliares_llm(
                resultado,
                chat_url=resultado.get("_chat_url"),
                chat_id=resultado.get("_chat_id"),
            )
    except Exception as e:
        log.warning(f"  ⚠️ Refinamento auxiliar da síntese compilada falhou (não fatal): {e}")

    salvar_resultado_compilado_paciente(id_paciente, resultado)
    log.info(f"✅ Síntese compilada do paciente {id_paciente} atualizada.")


def _stringify_compact(value) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value
    if isinstance(parsed, list):
        partes = []
        for item in parsed:
            if item in (None, "", [], {}):
                continue
            partes.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
        return "; ".join(partes)
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed or "").strip()


def montar_texto_compilado_paciente(id_paciente: str):
    rows = sql_exec(f"""
        SELECT
            id_atendimento,
            id_paciente,
            id_criador,
            datetime_atendimento_inicio,
            resumo_texto,
            gravidade_clinica,
            diagnosticos_citados,
            pontos_chave,
            mudancas_relevantes,
            sinais_nucleares,
            eventos_comportamentais,
            terapias_referidas,
            exames_citados,
            pendencias_clinicas,
            condutas_no_prontuario,
            medicacoes_em_uso,
            medicacoes_iniciadas,
            medicacoes_suspensas,
            condutas_especificas_sugeridas,
            condutas_gerais_sugeridas,
            mensagens_acompanhamento
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND status = 'concluido'
          AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
        ORDER BY datetime_atendimento_inicio DESC, id_atendimento DESC
        LIMIT 25
    """, reason="carregar_analises_paciente_compilado").get("data", [])

    if not rows:
        return "", None

    blocos = []
    for idx, row in enumerate(rows, start=1):
        linhas = [
            f"ATENDIMENTO #{idx}",
            f"id_atendimento: {row.get('id_atendimento')}",
            f"data_atendimento: {row.get('datetime_atendimento_inicio') or 'sem_data'}",
        ]
        if row.get("resumo_texto"):
            linhas.append(f"resumo_texto: {row['resumo_texto']}")

        for campo in [
            "gravidade_clinica",
            "diagnosticos_citados",
            "pontos_chave",
            "mudancas_relevantes",
            "sinais_nucleares",
            "eventos_comportamentais",
            "terapias_referidas",
            "exames_citados",
            "pendencias_clinicas",
            "condutas_no_prontuario",
            "medicacoes_em_uso",
            "medicacoes_iniciadas",
            "medicacoes_suspensas",
            "condutas_especificas_sugeridas",
            "condutas_gerais_sugeridas",
            "mensagens_acompanhamento",
        ]:
            val = _stringify_compact(row.get(campo))
            if val:
                linhas.append(f"{campo}: {val}")
        blocos.append("\n".join(linhas))

    erros = listar_atendimentos_com_erro_paciente(id_paciente)
    bloco_erros = ""
    if erros:
        linhas_erros = []
        for e in erros[:20]:
            msg = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(msg) > 600:
                msg = msg[:600] + "..."
            linhas_erros.append(
                f"- id_atendimento={e.get('id_atendimento')}, tentativas={e.get('tentativas')}, erro={msg or 'sem detalhe'}"
            )
        bloco_erros = (
            "\n\n" + ("=" * 70) + "\n\n"
            "ATENDIMENTOS COM FALHA DE ANÁLISE (não concluídos)\n"
            "Use estas falhas apenas como contexto operacional. Não invente dados clínicos ausentes.\n"
            + "\n".join(linhas_erros)
        )

    texto = (
        f"HISTÓRICO LONGITUDINAL COMPILADO DO PACIENTE {id_paciente}\n"
        "Os blocos abaixo representam análises estruturadas já concluídas deste paciente.\n"
        "Consolide o histórico completo do paciente, sintetizando padrões persistentes, mudanças relevantes, terapias, medicações, riscos, pendências e condutas, sem inventar dados.\n\n"
        + "\n\n" + ("\n\n" + ("=" * 70) + "\n\n").join(blocos)
        + bloco_erros
    )
    return texto, rows[0]


def salvar_resultado_compilado_paciente(id_paciente: str, resultado: dict):
    id_registro = garantir_registro_compilado_paciente_pendente(id_paciente)

    dt_inicio_mais_antiga, dt_ultima_atualizacao_mais_nova = _obter_datas_referencia_compilada(id_paciente)
    sets = [
        "id_atendimento = NULL",  # síntese compilada não deve colidir com uq_atendimento de análises unitárias
        f"datetime_atendimento_inicio = {esc_str(str(dt_inicio_mais_antiga)) if dt_inicio_mais_antiga else 'NULL'}",
        f"datetime_ultima_atualizacao_atendimento = {esc_str(str(dt_ultima_atualizacao_mais_nova)) if dt_ultima_atualizacao_mais_nova else 'NULL'}",
        "id_criador = 'analise_compilada_paciente'",
        f"id_paciente = '{esc(id_paciente)}'",
    ] + _montar_sets_resultado(resultado)

    sql_exec(
        f"UPDATE {TABELA} SET\n    " + ",\n    ".join(sets) + f"\nWHERE id = {id_registro}"
    )


def atualizar_analise_compilada_paciente(id_paciente: str):
    enfileirados = enfileirar_atendimentos_antigos(id_paciente)
    pendentes = contar_atendimentos_nao_concluidos_paciente(id_paciente)
    erros = listar_atendimentos_com_erro_paciente(id_paciente)
    if pendentes > 0:
        try:
            garantir_registro_compilado_paciente_pendente(id_paciente)
        except Exception as e:
            log.warning(
                f"  ⚠️ Falha ao garantir registro pendente da síntese compilada do paciente {id_paciente}: {e}"
            )
        log.info(
            f"⏳ Síntese compilada adiada para paciente {id_paciente}: "
            f"{pendentes} atendimento(s) ainda não concluído(s)"
            + (f" ({enfileirados} recém-enfileirado(s))" if enfileirados else "")
            + "."
        )
        return

    if erros:
        amostra = []
        for e in erros[:3]:
            detalhe = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(detalhe) > 180:
                detalhe = detalhe[:180] + "..."
            amostra.append(
                f"id_atendimento={e.get('id_atendimento')} (tentativas={e.get('tentativas')}): {detalhe or 'sem detalhe'}"
            )
        log.warning(
            f"  ⚠️ Paciente {id_paciente} possui {len(erros)} atendimento(s) com erro de análise. "
            f"A síntese compilada seguirá com os concluídos e levará contexto das falhas. "
            f"Amostra: {' | '.join(amostra)}"
        )

    texto_compilado, row_base = montar_texto_compilado_paciente(id_paciente)
    if not texto_compilado or not row_base:
        log.info(f"ℹ️  Sem histórico suficiente para compilar síntese do paciente {id_paciente}.")
        return

    id_registro_compilado = garantir_registro_compilado_paciente_pendente(id_paciente)
    sql_exec(f"""
        UPDATE {TABELA} SET
            status = 'processando',
            tentativas = tentativas + 1,
            erro_msg = NULL,
            datetime_analise_iniciada = NOW(),
            datetime_analise_concluida = NULL,
            datetime_ultima_atualizacao_atendimento = NULL
        WHERE id = {id_registro_compilado}
    """, reason="marcar_registro_compilado_processando")

    log.info(f"🧬 Atualizando síntese compilada do paciente {id_paciente}...")
    contexto = ""
    try:
        contexto = buscar_contexto_clinico({
            "id_paciente": id_paciente,
            "id": row_base.get("id_atendimento"),
            "id_criador": row_base.get("id_criador"),
        }) or ""
    except Exception as e:
        log.warning(f"  ⚠️ Falha ao buscar contexto do paciente compilado {id_paciente}: {e}")

    resultado = analisar_prontuario(texto_compilado[:18000], contexto=contexto)
    try:
        resultado = executar_busca_evidencias(
            resultado,
            chat_url=resultado.get("_chat_url"),
            chat_id=resultado.get("_chat_id"),
        )
    except Exception as e:
        log.warning(f"  ⚠️ Enriquecimento da síntese compilada falhou (não fatal): {e}")

    salvar_resultado_compilado_paciente(id_paciente, resultado)
    log.info(f"✅ Síntese compilada do paciente {id_paciente} atualizada.")


def _stringify_compact(value) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value
    if isinstance(parsed, list):
        partes = []
        for item in parsed:
            if item in (None, "", [], {}):
                continue
            partes.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
        return "; ".join(partes)
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed or "").strip()


def montar_texto_compilado_paciente(id_paciente: str):
    rows = sql_exec(f"""
        SELECT
            id_atendimento,
            id_paciente,
            id_criador,
            datetime_atendimento_inicio,
            resumo_texto,
            gravidade_clinica,
            diagnosticos_citados,
            pontos_chave,
            mudancas_relevantes,
            sinais_nucleares,
            eventos_comportamentais,
            terapias_referidas,
            exames_citados,
            pendencias_clinicas,
            condutas_no_prontuario,
            medicacoes_em_uso,
            medicacoes_iniciadas,
            medicacoes_suspensas,
            condutas_especificas_sugeridas,
            condutas_gerais_sugeridas,
            mensagens_acompanhamento
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND status = 'concluido'
          AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
        ORDER BY datetime_atendimento_inicio DESC, id_atendimento DESC
        LIMIT 25
    """, reason="carregar_analises_paciente_compilado").get("data", [])

    if not rows:
        return "", None

    blocos = []
    for idx, row in enumerate(rows, start=1):
        linhas = [
            f"ATENDIMENTO #{idx}",
            f"id_atendimento: {row.get('id_atendimento')}",
            f"data_atendimento: {row.get('datetime_atendimento_inicio') or 'sem_data'}",
        ]
        if row.get("resumo_texto"):
            linhas.append(f"resumo_texto: {row['resumo_texto']}")

        for campo in [
            "gravidade_clinica",
            "diagnosticos_citados",
            "pontos_chave",
            "mudancas_relevantes",
            "sinais_nucleares",
            "eventos_comportamentais",
            "terapias_referidas",
            "exames_citados",
            "pendencias_clinicas",
            "condutas_no_prontuario",
            "medicacoes_em_uso",
            "medicacoes_iniciadas",
            "medicacoes_suspensas",
            "condutas_especificas_sugeridas",
            "condutas_gerais_sugeridas",
            "mensagens_acompanhamento",
        ]:
            val = _stringify_compact(row.get(campo))
            if val:
                linhas.append(f"{campo}: {val}")
        blocos.append("\n".join(linhas))

    erros = listar_atendimentos_com_erro_paciente(id_paciente)
    bloco_erros = ""
    if erros:
        linhas_erros = []
        for e in erros[:20]:
            msg = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(msg) > 600:
                msg = msg[:600] + "..."
            linhas_erros.append(
                f"- id_atendimento={e.get('id_atendimento')}, tentativas={e.get('tentativas')}, erro={msg or 'sem detalhe'}"
            )
        bloco_erros = (
            "\n\n" + ("=" * 70) + "\n\n"
            "ATENDIMENTOS COM FALHA DE ANÁLISE (não concluídos)\n"
            "Use estas falhas apenas como contexto operacional. Não invente dados clínicos ausentes.\n"
            + "\n".join(linhas_erros)
        )

    texto = (
        f"HISTÓRICO LONGITUDINAL COMPILADO DO PACIENTE {id_paciente}\n"
        "Os blocos abaixo representam análises estruturadas já concluídas deste paciente.\n"
        "Consolide o histórico completo do paciente, sintetizando padrões persistentes, mudanças relevantes, terapias, medicações, riscos, pendências e condutas, sem inventar dados.\n\n"
        + "\n\n" + ("\n\n" + ("=" * 70) + "\n\n").join(blocos)
        + bloco_erros
    )
    return texto, rows[0]


def salvar_resultado_compilado_paciente(id_paciente: str, resultado: dict):
    id_registro = garantir_registro_compilado_paciente_pendente(id_paciente)

    dt_inicio_mais_antiga, dt_ultima_atualizacao_mais_nova = _obter_datas_referencia_compilada(id_paciente)
    sets = [
        "id_atendimento = NULL",  # síntese compilada não deve colidir com uq_atendimento de análises unitárias
        f"datetime_atendimento_inicio = {esc_str(str(dt_inicio_mais_antiga)) if dt_inicio_mais_antiga else 'NULL'}",
        f"datetime_ultima_atualizacao_atendimento = {esc_str(str(dt_ultima_atualizacao_mais_nova)) if dt_ultima_atualizacao_mais_nova else 'NULL'}",
        "id_criador = 'analise_compilada_paciente'",
        f"id_paciente = '{esc(id_paciente)}'",
    ] + _montar_sets_resultado(resultado)

    sql_exec(
        f"UPDATE {TABELA} SET\n    " + ",\n    ".join(sets) + f"\nWHERE id = {id_registro}"
    )


def atualizar_analise_compilada_paciente(id_paciente: str):
    enfileirados = enfileirar_atendimentos_antigos(id_paciente)
    pendentes = contar_atendimentos_nao_concluidos_paciente(id_paciente)
    if pendentes > 0:
        log.info(
            f"⏳ Síntese compilada adiada para paciente {id_paciente}: "
            f"{pendentes} atendimento(s) ainda não concluído(s)"
            + (f" ({enfileirados} recém-enfileirado(s))" if enfileirados else "")
            + "."
        )
        return

    texto_compilado, row_base = montar_texto_compilado_paciente(id_paciente)
    if not texto_compilado or not row_base:
        log.info(f"ℹ️  Sem histórico suficiente para compilar síntese do paciente {id_paciente}.")
        return

    id_registro_compilado = garantir_registro_compilado_paciente_pendente(id_paciente)
    sql_exec(f"""
        UPDATE {TABELA} SET
            status = 'processando',
            tentativas = tentativas + 1,
            erro_msg = NULL,
            datetime_analise_iniciada = NOW(),
            datetime_analise_concluida = NULL,
            datetime_ultima_atualizacao_atendimento = NULL
        WHERE id = {id_registro_compilado}
    """, reason="marcar_registro_compilado_processando")

    log.info(f"🧬 Atualizando síntese compilada do paciente {id_paciente}...")
    contexto = ""
    try:
        contexto = buscar_contexto_clinico({
            "id_paciente": id_paciente,
            "id": row_base.get("id_atendimento"),
            "id_criador": row_base.get("id_criador"),
        }) or ""
    except Exception as e:
        log.warning(f"  ⚠️ Falha ao buscar contexto do paciente compilado {id_paciente}: {e}")

    resultado = analisar_prontuario(texto_compilado[:18000], contexto=contexto)
    try:
        resultado = executar_busca_evidencias(
            resultado,
            chat_url=resultado.get("_chat_url"),
            chat_id=resultado.get("_chat_id"),
        )
    except Exception as e:
        log.warning(f"  ⚠️ Enriquecimento da síntese compilada falhou (não fatal): {e}")

    salvar_resultado_compilado_paciente(id_paciente, resultado)
    log.info(f"✅ Síntese compilada do paciente {id_paciente} atualizada.")


def _stringify_compact(value) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value
    if isinstance(parsed, list):
        partes = []
        for item in parsed:
            if item in (None, "", [], {}):
                continue
            partes.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
        return "; ".join(partes)
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed or "").strip()


def montar_texto_compilado_paciente(id_paciente: str):
    rows = sql_exec(f"""
        SELECT
            id_atendimento,
            id_paciente,
            id_criador,
            datetime_atendimento_inicio,
            resumo_texto,
            gravidade_clinica,
            diagnosticos_citados,
            pontos_chave,
            mudancas_relevantes,
            sinais_nucleares,
            eventos_comportamentais,
            terapias_referidas,
            exames_citados,
            pendencias_clinicas,
            condutas_no_prontuario,
            medicacoes_em_uso,
            medicacoes_iniciadas,
            medicacoes_suspensas,
            condutas_especificas_sugeridas,
            condutas_gerais_sugeridas,
            mensagens_acompanhamento
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND status = 'concluido'
          AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
        ORDER BY datetime_atendimento_inicio DESC, id_atendimento DESC
        LIMIT 25
    """, reason="carregar_analises_paciente_compilado").get("data", [])

    if not rows:
        return "", None

    blocos = []
    for idx, row in enumerate(rows, start=1):
        linhas = [
            f"ATENDIMENTO #{idx}",
            f"id_atendimento: {row.get('id_atendimento')}",
            f"data_atendimento: {row.get('datetime_atendimento_inicio') or 'sem_data'}",
        ]
        if row.get("resumo_texto"):
            linhas.append(f"resumo_texto: {row['resumo_texto']}")

        for campo in [
            "gravidade_clinica",
            "diagnosticos_citados",
            "pontos_chave",
            "mudancas_relevantes",
            "sinais_nucleares",
            "eventos_comportamentais",
            "terapias_referidas",
            "exames_citados",
            "pendencias_clinicas",
            "condutas_no_prontuario",
            "medicacoes_em_uso",
            "medicacoes_iniciadas",
            "medicacoes_suspensas",
            "condutas_especificas_sugeridas",
            "condutas_gerais_sugeridas",
            "mensagens_acompanhamento",
        ]:
            val = _stringify_compact(row.get(campo))
            if val:
                linhas.append(f"{campo}: {val}")
        blocos.append("\n".join(linhas))

    erros = listar_atendimentos_com_erro_paciente(id_paciente)
    bloco_erros = ""
    if erros:
        linhas_erros = []
        for e in erros[:20]:
            msg = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(msg) > 600:
                msg = msg[:600] + "..."
            linhas_erros.append(
                f"- id_atendimento={e.get('id_atendimento')}, tentativas={e.get('tentativas')}, erro={msg or 'sem detalhe'}"
            )
        bloco_erros = (
            "\n\n" + ("=" * 70) + "\n\n"
            "ATENDIMENTOS COM FALHA DE ANÁLISE (não concluídos)\n"
            "Use estas falhas apenas como contexto operacional. Não invente dados clínicos ausentes.\n"
            + "\n".join(linhas_erros)
        )

    texto = (
        f"HISTÓRICO LONGITUDINAL COMPILADO DO PACIENTE {id_paciente}\n"
        "Os blocos abaixo representam análises estruturadas já concluídas deste paciente.\n"
        "Consolide o histórico completo do paciente, sintetizando padrões persistentes, mudanças relevantes, terapias, medicações, riscos, pendências e condutas, sem inventar dados.\n\n"
        + "\n\n" + ("\n\n" + ("=" * 70) + "\n\n").join(blocos)
        + bloco_erros
    )
    return texto, rows[0]


def salvar_resultado_compilado_paciente(id_paciente: str, resultado: dict):
    id_registro = garantir_registro_compilado_paciente_pendente(id_paciente)

    dt_inicio_mais_antiga, dt_ultima_atualizacao_mais_nova = _obter_datas_referencia_compilada(id_paciente)
    sets = [
        "id_atendimento = NULL",  # síntese compilada não deve colidir com uq_atendimento de análises unitárias
        f"datetime_atendimento_inicio = {esc_str(str(dt_inicio_mais_antiga)) if dt_inicio_mais_antiga else 'NULL'}",
        f"datetime_ultima_atualizacao_atendimento = {esc_str(str(dt_ultima_atualizacao_mais_nova)) if dt_ultima_atualizacao_mais_nova else 'NULL'}",
        "id_criador = 'analise_compilada_paciente'",
        f"id_paciente = '{esc(id_paciente)}'",
    ] + _montar_sets_resultado(resultado)

    sql_exec(
        f"UPDATE {TABELA} SET\n    " + ",\n    ".join(sets) + f"\nWHERE id = {id_registro}"
    )


def atualizar_analise_compilada_paciente(id_paciente: str):
    enfileirados = enfileirar_atendimentos_antigos(id_paciente)
    pendentes = contar_atendimentos_nao_concluidos_paciente(id_paciente)
    if pendentes > 0:
        log.info(
            f"⏳ Síntese compilada adiada para paciente {id_paciente}: "
            f"{pendentes} atendimento(s) ainda não concluído(s)"
            + (f" ({enfileirados} recém-enfileirado(s))" if enfileirados else "")
            + "."
        )
        return

    texto_compilado, row_base = montar_texto_compilado_paciente(id_paciente)
    if not texto_compilado or not row_base:
        log.info(f"ℹ️  Sem histórico suficiente para compilar síntese do paciente {id_paciente}.")
        return

    id_registro_compilado = garantir_registro_compilado_paciente_pendente(id_paciente)
    sql_exec(f"""
        UPDATE {TABELA} SET
            status = 'processando',
            tentativas = tentativas + 1,
            erro_msg = NULL,
            datetime_analise_iniciada = NOW(),
            datetime_analise_concluida = NULL,
            datetime_ultima_atualizacao_atendimento = NULL
        WHERE id = {id_registro_compilado}
    """, reason="marcar_registro_compilado_processando")

    log.info(f"🧬 Atualizando síntese compilada do paciente {id_paciente}...")
    contexto = ""
    try:
        contexto = buscar_contexto_clinico({
            "id_paciente": id_paciente,
            "id": row_base.get("id_atendimento"),
            "id_criador": row_base.get("id_criador"),
        }) or ""
    except Exception as e:
        log.warning(f"  ⚠️ Falha ao buscar contexto do paciente compilado {id_paciente}: {e}")

    resultado = analisar_prontuario(texto_compilado[:18000], contexto=contexto)
    try:
        resultado = executar_busca_evidencias(
            resultado,
            chat_url=resultado.get("_chat_url"),
            chat_id=resultado.get("_chat_id"),
        )
    except Exception as e:
        log.warning(f"  ⚠️ Enriquecimento da síntese compilada falhou (não fatal): {e}")

    salvar_resultado_compilado_paciente(id_paciente, resultado)
    log.info(f"✅ Síntese compilada do paciente {id_paciente} atualizada.")


def _stringify_compact(value) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        parsed = value
    if isinstance(parsed, list):
        partes = []
        for item in parsed:
            if item in (None, "", [], {}):
                continue
            partes.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
        return "; ".join(partes)
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed or "").strip()


def montar_texto_compilado_paciente(id_paciente: str):
    rows = sql_exec(f"""
        SELECT
            id_atendimento,
            id_paciente,
            id_criador,
            datetime_atendimento_inicio,
            resumo_texto,
            gravidade_clinica,
            diagnosticos_citados,
            pontos_chave,
            mudancas_relevantes,
            sinais_nucleares,
            eventos_comportamentais,
            terapias_referidas,
            exames_citados,
            pendencias_clinicas,
            condutas_no_prontuario,
            medicacoes_em_uso,
            medicacoes_iniciadas,
            medicacoes_suspensas,
            condutas_especificas_sugeridas,
            condutas_gerais_sugeridas,
            mensagens_acompanhamento
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND status = 'concluido'
          AND COALESCE(id_criador, '') <> 'analise_compilada_paciente'
        ORDER BY datetime_atendimento_inicio DESC, id_atendimento DESC
        LIMIT 25
    """, reason="carregar_analises_paciente_compilado").get("data", [])

    if not rows:
        return "", None

    blocos = []
    for idx, row in enumerate(rows, start=1):
        linhas = [
            f"ATENDIMENTO #{idx}",
            f"id_atendimento: {row.get('id_atendimento')}",
            f"data_atendimento: {row.get('datetime_atendimento_inicio') or 'sem_data'}",
        ]
        if row.get("resumo_texto"):
            linhas.append(f"resumo_texto: {row['resumo_texto']}")

        for campo in [
            "gravidade_clinica",
            "diagnosticos_citados",
            "pontos_chave",
            "mudancas_relevantes",
            "sinais_nucleares",
            "eventos_comportamentais",
            "terapias_referidas",
            "exames_citados",
            "pendencias_clinicas",
            "condutas_no_prontuario",
            "medicacoes_em_uso",
            "medicacoes_iniciadas",
            "medicacoes_suspensas",
            "condutas_especificas_sugeridas",
            "condutas_gerais_sugeridas",
            "mensagens_acompanhamento",
        ]:
            val = _stringify_compact(row.get(campo))
            if val:
                linhas.append(f"{campo}: {val}")
        blocos.append("\n".join(linhas))

    texto = (
        f"HISTÓRICO LONGITUDINAL COMPILADO DO PACIENTE {id_paciente}\n"
        "Os blocos abaixo representam análises estruturadas já concluídas deste paciente.\n"
        "Consolide o histórico completo do paciente, sintetizando padrões persistentes, mudanças relevantes, terapias, medicações, riscos, pendências e condutas, sem inventar dados.\n\n"
        + "\n\n" + ("\n\n" + ("=" * 70) + "\n\n").join(blocos)
    )
    return texto, rows[0]


def salvar_resultado_compilado_paciente(id_paciente: str, resultado: dict):
    id_registro = garantir_registro_compilado_paciente_pendente(id_paciente)

    dt_inicio_mais_antiga, dt_ultima_atualizacao_mais_nova = _obter_datas_referencia_compilada(id_paciente)
    sets = [
        "id_atendimento = NULL",  # síntese compilada não deve colidir com uq_atendimento de análises unitárias
        f"datetime_atendimento_inicio = {esc_str(str(dt_inicio_mais_antiga)) if dt_inicio_mais_antiga else 'NULL'}",
        f"datetime_ultima_atualizacao_atendimento = {esc_str(str(dt_ultima_atualizacao_mais_nova)) if dt_ultima_atualizacao_mais_nova else 'NULL'}",
        "id_criador = 'analise_compilada_paciente'",
        f"id_paciente = '{esc(id_paciente)}'",
    ] + _montar_sets_resultado(resultado)

    sql_exec(
        f"UPDATE {TABELA} SET\n    " + ",\n    ".join(sets) + f"\nWHERE id = {id_registro}"
    )


def atualizar_analise_compilada_paciente(id_paciente: str):
    enfileirados = enfileirar_atendimentos_antigos(id_paciente)
    pendentes = contar_atendimentos_nao_concluidos_paciente(id_paciente)
    erros = listar_atendimentos_com_erro_paciente(id_paciente)
    if pendentes > 0:
        try:
            garantir_registro_compilado_paciente_pendente(id_paciente)
        except Exception as e:
            log.warning(
                f"  ⚠️ Falha ao garantir registro pendente da síntese compilada do paciente {id_paciente}: {e}"
            )
        log.info(
            f"⏳ Síntese compilada adiada para paciente {id_paciente}: "
            f"{pendentes} atendimento(s) ainda não concluído(s)"
            + (f" ({enfileirados} recém-enfileirado(s))" if enfileirados else "")
            + "."
        )
        return

    if erros:
        amostra = []
        for e in erros[:3]:
            detalhe = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(detalhe) > 180:
                detalhe = detalhe[:180] + "..."
            amostra.append(
                f"id_atendimento={e.get('id_atendimento')} (tentativas={e.get('tentativas')}): {detalhe or 'sem detalhe'}"
            )
        log.warning(
            f"  ⚠️ Paciente {id_paciente} possui {len(erros)} atendimento(s) com erro de análise. "
            f"A síntese compilada seguirá com os concluídos e levará contexto das falhas. "
            f"Amostra: {' | '.join(amostra)}"
        )

    texto_compilado, row_base = montar_texto_compilado_paciente(id_paciente)
    if not texto_compilado or not row_base:
        log.info(f"ℹ️  Sem histórico suficiente para compilar síntese do paciente {id_paciente}.")
        return

    id_registro_compilado = garantir_registro_compilado_paciente_pendente(id_paciente)
    sql_exec(f"""
        UPDATE {TABELA} SET
            status = 'processando',
            tentativas = tentativas + 1,
            erro_msg = NULL,
            datetime_analise_iniciada = NOW(),
            datetime_analise_concluida = NULL,
            datetime_ultima_atualizacao_atendimento = NULL
        WHERE id = {id_registro_compilado}
    """, reason="marcar_registro_compilado_processando")

    log.info(f"🧬 Atualizando síntese compilada do paciente {id_paciente}...")
    contexto = ""
    try:
        contexto = buscar_contexto_clinico({
            "id_paciente": id_paciente,
            "id": row_base.get("id_atendimento"),
            "id_criador": row_base.get("id_criador"),
        }) or ""
    except Exception as e:
        log.warning(f"  ⚠️ Falha ao buscar contexto do paciente compilado {id_paciente}: {e}")

    resultado = analisar_prontuario(texto_compilado[:18000], contexto=contexto)
    try:
        resultado = executar_busca_evidencias(
            resultado,
            chat_url=resultado.get("_chat_url"),
            chat_id=resultado.get("_chat_id"),
        )
    except Exception as e:
        log.warning(f"  ⚠️ Enriquecimento da síntese compilada falhou (não fatal): {e}")

    salvar_resultado_compilado_paciente(id_paciente, resultado)
    log.info(f"✅ Síntese compilada do paciente {id_paciente} atualizada.")




def salvar_erro(id_atendimento: int, msg: str):
    sql_exec(f"""
        UPDATE {TABELA} SET
            status   = 'erro',
            erro_msg = _utf8mb4'{esc(str(msg)[:1000])}'
        WHERE id_atendimento = {id_atendimento}
    """, reason="salvar_erro_unitario")


def limpar_tabelas_complementares(id_atendimento: int):
    """
    Remove dados antigos das tabelas complementares antes de uma (re)análise.
    Garante que não fiquem dados órfãos de análises anteriores.
    """
    tabelas = [
        "chatgpt_clinical_graph_edges",
        "chatgpt_clinical_graph_nodes",
        "chatgpt_alertas_clinicos",
        "chatgpt_casos_semelhantes",
        "chatgpt_embeddings_prontuario",
    ]
    for tabela in tabelas:
        try:
            resp = sql_exec(
                f"DELETE FROM {tabela} WHERE id_atendimento = {int(id_atendimento)}",
                reason=f"limpar_tabela_complementar_{tabela}"
            )
            afetados = resp.get("affected_rows", 0)
            if afetados:
                log.info(f"  🗑️  {tabela}: {afetados} registro(s) antigo(s) removido(s)")
        except Exception as e:
            log.debug(f"  ↷ limpar {tabela}: {e}")


def _normalizar_node(nd: dict) -> dict:
    """
    Normaliza campos de um node do grafo clínico para o formato esperado pelo PHP:
      id, tipo/node_tipo, valor/node_valor, normalizado/node_normalizado, contexto/node_contexto

    A LLM pode retornar variantes em inglês, abreviadas, etc.
    """
    tipo_bruto = str(
        nd.get("tipo") or nd.get("node_tipo") or nd.get("type") or nd.get("category") or ""
    ).strip()
    valor = str(
        nd.get("valor") or nd.get("node_valor") or nd.get("value") or nd.get("label") or nd.get("name") or nd.get("nome") or ""
    ).strip()
    normalizado = str(
        nd.get("normalizado") or nd.get("node_normalizado") or nd.get("normalized") or nd.get("normalised") or ""
    ).strip()
    contexto = str(
        nd.get("contexto") or nd.get("node_contexto") or nd.get("context") or nd.get("description") or nd.get("descricao") or ""
    ).strip()

    tipo_alias = {
        "patient": "paciente",
        "paciente": "paciente",
        "patient_name": "paciente",
        "diagnosis": "diagnostico",
        "diagnostico": "diagnostico",
        "diagnóstico": "diagnostico",
        "cid": "diagnostico",
        "symptom": "sintoma",
        "symptoms": "sintoma",
        "sintoma": "sintoma",
        "sinal": "sintoma",
        "sign": "sintoma",
        "medication": "medicamento",
        "medicine": "medicamento",
        "drug": "medicamento",
        "medicamento": "medicamento",
        "medicacao": "medicamento",
        "medicação": "medicamento",
        "therapy": "terapia",
        "terapia": "terapia",
        "exam": "exame",
        "test": "exame",
        "exame": "exame",
        "gene": "gene",
        "genetics": "gene",
        "genetica": "gene",
        "genética": "gene",
        "behavior": "comportamento",
        "behaviour": "comportamento",
        "comportamento": "comportamento",
        "conduct": "conduta",
        "plan": "conduta",
        "conduta": "conduta",
        "risk": "risco",
        "risco": "risco",
        "pending": "pendencia",
        "pendencia": "pendencia",
        "pendência": "pendencia",
    }
    tipo = tipo_alias.get(tipo_bruto.lower(), tipo_bruto.lower())

    if not normalizado and valor:
        normalizado = re.sub(r"[^a-z0-9]+", "_", valor.lower()).strip("_")

    campos_base = {
        "id", "node_id", "tipo", "node_tipo", "type", "category",
        "valor", "node_valor", "value", "label", "name", "nome",
        "normalizado", "node_normalizado", "normalized", "normalised",
        "contexto", "node_contexto", "context", "description", "descricao",
    }
    extras = []
    for k, v in nd.items():
        if k in campos_base or v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            extras.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        else:
            extras.append(f"{k}={v}")
    extras_txt = " | ".join(extras[:4])
    if extras_txt:
        contexto = f"{contexto} | {extras_txt}".strip(" |") if contexto else extras_txt

    node_id = str(nd.get("id") or nd.get("node_id") or "").strip()
    if not node_id and tipo and normalizado:
        node_id = f"{tipo}_{normalizado[:80]}"

    return {
        "id":           node_id,
        "tipo":         tipo,
        "valor":        valor,
        "normalizado":  normalizado,
        "contexto":     contexto,
    }


def _normalizar_edge(ed: dict) -> dict:
    """
    Normaliza campos de uma edge do grafo clínico para o formato esperado pelo PHP:
      node_origem, node_destino, relacao_tipo, relacao_contexto
    """
    return {
        "node_origem":      ed.get("node_origem") or ed.get("source") or ed.get("from") or ed.get("origem") or "",
        "node_destino":     ed.get("node_destino") or ed.get("target") or ed.get("to") or ed.get("destino") or "",
        "relacao_tipo":     ed.get("relacao_tipo") or ed.get("relation") or ed.get("type") or ed.get("tipo") or ed.get("relationship") or "",
        "relacao_contexto": ed.get("relacao_contexto") or ed.get("contexto") or ed.get("context") or ed.get("description") or ed.get("descricao") or "",
    }


def _deduplicar_nodes_grafo(nodes: list) -> list:
    dedup = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        chave = (
            str(node.get("tipo") or "").strip().lower(),
            str(node.get("normalizado") or node.get("valor") or "").strip().lower(),
        )
        if not chave[1]:
            continue
        if chave not in dedup:
            dedup[chave] = node
            continue
        existente = dedup[chave]
        if not existente.get("id") and node.get("id"):
            existente["id"] = node["id"]
        if not existente.get("contexto") and node.get("contexto"):
            existente["contexto"] = node["contexto"]
        elif node.get("contexto") and node["contexto"] not in str(existente.get("contexto") or ""):
            existente["contexto"] = f"{existente.get('contexto','')} | {node['contexto']}".strip(" |")
    return list(dedup.values())


def _primeiro_node_representativo(nodes: list):
    for tipo_prioritario in ("diagnostico", "medicamento", "terapia", "sintoma", "gene", "risco"):
        for node in nodes:
            if (node.get("tipo") or "").lower() == tipo_prioritario:
                return node
    return nodes[0] if nodes else None


def salvar_auxiliar(id_atendimento: int, id_paciente: str, resultado: dict):
    """
    Chama o endpoint PHP salvar_analise_auxiliar para popular tabelas auxiliares
    (alertas, grafo nodes/edges, casos semelhantes) com charset correto.

    Normaliza os campos do grafo para garantir compatibilidade com o PHP,
    independentemente dos nomes de campo que a LLM retorne.
    """
    # Monta payload com os campos que o PHP espera
    payload = {
        PHP_KEY_FIELD:       API_KEY,
        "id_atendimento":    int(id_atendimento),
        "id_paciente":       str(id_paciente),
    }

    # Mapeamento: chave esperada pelo PHP → possíveis nomes no JSON da LLM
    _ALIAS_AUXILIAR = {
        "alertas_clinicos":    ["alertas_clinicos", "alertas"],
        "grafo_clinico_nodes": ["grafo_clinico_nodes", "grafo_nodes", "clinical_graph_nodes", "nodes"],
        "grafo_clinico_edges": ["grafo_clinico_edges", "grafo_edges", "clinical_graph_edges", "edges"],
        "casos_semelhantes":   ["casos_semelhantes", "similaridade_casos"],
    }

    # Campos auxiliares: busca com fallback de alias
    for chave_php, aliases in _ALIAS_AUXILIAR.items():
        val = None
        for alias in aliases:
            val = resultado.get(alias)
            if val is not None:
                break
        if val is None:
            continue

        # Desserializa se vier como string JSON
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                pass

        # Normaliza campos do grafo para o formato PHP
        if chave_php == "grafo_clinico_nodes" and isinstance(val, list):
            val_original = val
            val = [_normalizar_node(nd) for nd in val if isinstance(nd, dict)]
            # Filtra nodes sem valor (o PHP faria isso de qualquer forma)
            val = [n for n in val if n.get("valor")]
            val = _deduplicar_nodes_grafo(val)
            node_original_log = _primeiro_node_representativo([
                _normalizar_node(nd) for nd in val_original if isinstance(nd, dict)
            ])
            node_normalizado_log = _primeiro_node_representativo(val)
            if node_original_log:
                log.info(f"  🔬 Node representativo LLM (normalizado): {json.dumps(node_original_log, ensure_ascii=False)[:300]}")
            if node_normalizado_log:
                log.info(f"  🔬 Node representativo salvo:           {json.dumps(node_normalizado_log, ensure_ascii=False)[:300]}")
            if not val:
                log.warning(f"  ⚠️ Todos os nodes do grafo ficaram sem 'valor' após normalização!")
                continue

        elif chave_php == "grafo_clinico_edges" and isinstance(val, list):
            val_original = val
            val = [_normalizar_edge(ed) for ed in val if isinstance(ed, dict)]
            if val_original:
                log.info(f"  🔬 Primeira edge LLM (original): {json.dumps(val_original[0], ensure_ascii=False)[:200]}")
                log.info(f"  🔬 Primeira edge (normalizada):   {json.dumps(val[0], ensure_ascii=False)[:200]}")

        payload[chave_php] = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)

    # Verifica se há algo para salvar
    tem_dados = {k: bool(payload.get(k)) for k in _ALIAS_AUXILIAR.keys()}
    log.info(f"  📦 Dados auxiliares: {' | '.join(f'{k}={v}' for k, v in tem_dados.items())}")

    if not any(tem_dados.values()):
        log.warning(f"  ⚠️ Nenhum dado auxiliar encontrado no resultado para ID={id_atendimento}")
        return

    try:
        resp = requests.post(
            f"{PHP_URL}?action=salvar_analise_auxiliar",
            json=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-API-KEY":    API_KEY,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            log.info(f"  📊 Tabelas auxiliares salvas (alertas/grafo/casos) para ID={id_atendimento}")
        else:
            log.warning(f"  ⚠️ salvar_auxiliar retornou erro: {data.get('error', '?')}")
    except Exception as e:
        log.warning(f"  ⚠️ salvar_auxiliar falhou (não fatal): {e}")



# ─────────────────────────────────────────────────────────────
# CONTEXTO CLÍNICO (paciente + profissional + hospital)
# ─────────────────────────────────────────────────────────────

def buscar_contexto_clinico(row: dict) -> str:
    """
    Monta bloco de contexto clínico a partir dos IDs do registro de análise.

    Busca:
      - Paciente  (id_paciente  → membros)
      - Profissional (id_criador → membros)
      - Hospital  (id_atendimento → clinica_atendimentos.id_hospital → hospitais)

    Retorna string formatada para ser inserida no user_content antes do prontuário,
    ou string vazia se nenhum dado for recuperado.
    """
    id_paciente    = row.get("id_paciente")
    id_criador     = row.get("id_criador")
    id_atendimento = row.get("id")          # id_atendimento no contexto do SELECT

    partes = []

    # ── Dados do(a) paciente ──────────────────────────────────
    if id_paciente:
        try:
            resp = sql_exec(
                f"SELECT nome, data_nascimento, sexo, mae_nome, telefone1, telefone2 "
                f"FROM membros WHERE id = '{esc(id_paciente)}' LIMIT 1",
                reason="contexto_paciente"
            )
            rows_p = resp.get("data") or []
            if rows_p:
                p = rows_p[0]
                partes.append("DADOS DO(A) PACIENTE:")
                partes.append(f"  Nome: {p.get('nome') or 'N/I'}")
                partes.append(f"  Data de nascimento: {p.get('data_nascimento') or 'N/I'}")
                partes.append(f"  Sexo: {p.get('sexo') or 'N/I'}")
                partes.append(f"  Nome da mãe: {p.get('mae_nome') or 'N/I'}")
                tel1 = p.get('telefone1') or ''
                tel2 = p.get('telefone2') or ''
                if tel1 or tel2:
                    telefones = ', '.join(filter(None, [tel1, tel2]))
                    partes.append(f"  Telefone(s): {telefones}")
        except Exception as e:
            log.warning(f"  ⚠️ Erro ao buscar dados do paciente (id={id_paciente}): {e}")

    # ── Dados do(a) profissional ──────────────────────────────
    if id_criador:
        try:
            resp = sql_exec(
                f"SELECT nome, nome_carimbo, data_nascimento, sexo "
                f"FROM membros WHERE id = '{esc(id_criador)}' LIMIT 1",
                reason="contexto_profissional"
            )
            rows_c = resp.get("data") or []
            if rows_c:
                c = rows_c[0]
                nome_prof = (c.get('nome_carimbo') or '').strip() or c.get('nome') or 'N/I'
                partes.append("")
                partes.append("DADOS DO(A) PROFISSIONAL QUE ATENDEU:")
                partes.append(f"  Nome: {nome_prof}")
                partes.append(f"  Data de nascimento: {c.get('data_nascimento') or 'N/I'}")
                partes.append(f"  Sexo: {c.get('sexo') or 'N/I'}")
        except Exception as e:
            log.warning(f"  ⚠️ Erro ao buscar dados do profissional (id={id_criador}): {e}")

    # ── Dados do hospital/clínica ─────────────────────────────
    if id_atendimento:
        try:
            resp = sql_exec(
                f"SELECT h.titulo "
                f"FROM clinica_atendimentos ca "
                f"INNER JOIN hospitais h ON h.id = ca.id_hospital "
                f"WHERE ca.id = {int(id_atendimento)} LIMIT 1",
                reason="contexto_hospital"
            )
            rows_h = resp.get("data") or []
            if rows_h and rows_h[0].get("titulo"):
                partes.append("")
                partes.append("LOCAL DO ATENDIMENTO:")
                partes.append(f"  {rows_h[0]['titulo']}")
        except Exception as e:
            log.warning(f"  ⚠️ Erro ao buscar dados do hospital (id_atendimento={id_atendimento}): {e}")

    if not partes:
        return ""

    return "\n".join(partes)


# ─────────────────────────────────────────────────────────────
# LIMPEZA DE HTML (stdlib — sem dependências extras)
# ─────────────────────────────────────────────────────────────

class _StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
    def handle_data(self, data):
        self._parts.append(data)

def strip_html(raw: str) -> str:
    p = _StripHTML()
    p.feed(html_mod.unescape(raw or ""))
    return re.sub(r'\s{3,}', '\n\n', " ".join(p._parts)).strip()


# ─────────────────────────────────────────────────────────────
# CAMADA LLM — chama o simulador local
# ─────────────────────────────────────────────────────────────
#================================================SYSTEM_PROMPT = INICIO ===============================================
SYSTEM_PROMPT = """[INICIO_TEXTO_COLADO]

Você é um assistente médico especializado em análise de prontuários clínicos.

Sua tarefa é analisar cuidadosamente o texto de um prontuário clínico e retornar um JSON estruturado contendo informações clínicas relevantes.

A resposta deve conter SOMENTE um JSON válido.

Não incluir markdown.
Não incluir comentários.
Não incluir explicações fora do JSON.

══════════════════════════════════════
OBJETIVO DA ANÁLISE
══════════════════════════════════════

Transformar texto clínico não estruturado em dados estruturados confiáveis.

Extrair de forma estruturada:

• diagnósticos mencionados
• idade do paciente
• sinais e sintomas
• eventos comportamentais
• mudanças clínicas relevantes
• medicamentos em uso
• medicamentos iniciados
• medicamentos suspensos
• terapias citadas
• exames mencionados
• pendências clínicas
• condutas registradas no prontuário
• condutas clínicas sugeridas
• estimativa de seguimento clínico
• resumo clínico objetivo

══════════════════════════════════════
PRINCÍPIO FUNDAMENTAL
══════════════════════════════════════

PRIORIDADE ABSOLUTA: EXTRAÇÃO FIEL DO TEXTO.

Nunca criar informação clínica inexistente.

Nunca completar lacunas clínicas com conhecimento médico.

Somente registrar dados explicitamente descritos no prontuário.

══════════════════════════════════════
PROTOCOLO DE LEITURA DO PRONTUÁRIO
══════════════════════════════════════

Durante a análise, siga mentalmente esta sequência:

1. Identificar idade do paciente.
2. Identificar diagnósticos mencionados.
3. Identificar sinais e sintomas relatados.
4. Identificar eventos comportamentais descritos.
5. Identificar mudanças clínicas desde consultas anteriores.
6. Identificar medicamentos em uso atual.
7. Identificar medicamentos iniciados recentemente.
8. Identificar medicamentos suspensos.
9. Identificar terapias citadas.
10. Identificar exames mencionados.
11. Identificar pendências clínicas.
12. Identificar condutas registradas no prontuário.
13. Identificar sinais nucleares do quadro clínico.
14. Gerar resumo clínico.

Somente após essa leitura estrutural gerar o JSON final.

══════════════════════════════════════
REGRAS CRÍTICAS
══════════════════════════════════════

1. Nunca inventar informações clínicas.

2. Não inferir:

• sintomas
• diagnósticos
• exames
• terapias
• medicamentos

3. Somente registrar dados explicitamente descritos no prontuário.

4. Se um campo não estiver presente no texto:

valor único → null
lista → []

5. Medicamentos devem preservar exatamente a posologia descrita.

Nunca normalizar doses.

Exemplos válidos:

0,25+0+0,75ml
1/4+1/4+1/2cp
0+0+5 à 10gts
1cp 12/12h
5mg 1x/dia

Registrar exatamente como descrito.

6. Quando houver múltiplas formulações do mesmo medicamento,
registrar como entradas separadas.

7. Se o prontuário mencionar medicamento sem dose clara,
registrar apenas o nome.

8. Nunca assumir dose padrão.

9. Nunca criar medicamento não citado.

10. Não modificar unidades (mg, ml, gotas, comprimidos).

══════════════════════════════════════
EXTRAÇÃO SEMÂNTICA DE MEDICAÇÕES
══════════════════════════════════════

A extração de medicações deve ser altamente sensível ao contexto textual.

Ao procurar medicamentos, verificar cuidadosamente trechos como:

• EM USO
• MEDICAÇÕES EM USO
• FEZ USO
• CD
• CONDUTA
• PRESCRIÇÃO
• ORIENTAÇÕES
• MANTER
• ASSOCIO
• INICIO
• INICIAR
• ELEVO
• AUMENTO
• REDUZO
• SUSPENDO
• RETIRO
• RODO
• TROCO
• MANTIDO
• INTRODUZIDO

Também procurar medicamentos quando aparecerem:

• em linhas corridas
• entre parênteses
• seguidos de dose
• seguidos de posologia
• seguidos de observações como “boa resposta”, “sem melhora”, “efeitos adversos”

Ao identificar um medicamento, procurar no mesmo trecho textual:

• nome
• dose/apresentação
• posologia
• início/período
• motivo de uso
• observação clínica
• motivo de suspensão, se houver

Se o medicamento aparecer abreviado ou com pequena variação ortográfica,
somente registrar se for claramente identificável no contexto textual.

É permitido reconhecer nomes de medicamentos que apareçam:

• por nome comercial
• por nome genérico
• por variação comum de escrita no prontuário

Exemplos frequentes em neuropediatria e saúde mental pediátrica:

• Ritalina / metilfenidato
• Risperidona / risperidona solução
• Clonidina
• Melatonina
• Atomoxetina / Atentah
• Periciazina / Neuleptil
• Imipramina
• Amitriptilina
• Aripiprazol
• Escitalopram
• Fluoxetina
• Sertralina
• Valproato / ácido valproico / Depakene / Depakote
• Levetiracetam / Keppra
• Clobazam / Frisium
• Carbamazepina
• Oxcarbazepina
• Topiramato

ATENÇÃO:
Esses exemplos servem apenas para melhorar a leitura semântica.
Nunca registrar um medicamento se ele não estiver realmente presente no texto.

══════════════════════════════════════
CLASSIFICAÇÃO DE MEDICAÇÕES
══════════════════════════════════════

Classificar medicamentos assim:

1. medicacoes_em_uso
   → medicações em uso atual no momento da consulta

2. medicacoes_iniciadas
   → medicações explicitamente introduzidas, associadas ou iniciadas nesta consulta ou em período recente claramente referido

3. medicacoes_suspensas
   → medicações explicitamente suspensas, retiradas, cessadas ou interrompidas

Se uma medicação estiver citada como em uso e também como recém-associada,
ela pode aparecer em:

• medicacoes_em_uso
e
• medicacoes_iniciadas

desde que isso esteja claramente sustentado pelo texto.

══════════════════════════════════════
CONSISTÊNCIA DE MEDICAÇÕES
══════════════════════════════════════

Antes de registrar um medicamento:

Verificar se ele realmente aparece no texto.

Se houver inconsistência entre dose e posologia:

priorizar exatamente o que está escrito no prontuário.

Nunca corrigir dose com conhecimento médico.

Se houver duas apresentações diferentes do mesmo medicamento:

registrar separadamente.

Se houver uma formulação alternativa indicada por “OU”:

registrar como item separado, mantendo a observação.

══════════════════════════════════════
DETECÇÃO DE REGRESSÃO CLÍNICA
══════════════════════════════════════

Se o texto mencionar:

• perda de habilidades
• piora comportamental
• aumento de agressividade
• perda de linguagem
• regressão do desenvolvimento
• perda de contato social
• perda de funcionalidade

Registrar essa informação em:

mudancas_relevantes
eventos_comportamentais
sinais_nucleares

conforme o contexto clínico.

══════════════════════════════════════
AVALIAÇÃO DE RESPOSTA ESPERADA
══════════════════════════════════════

Para medicamentos em uso, registrar:

tempo_resposta_estimado
parametros_monitorizacao
motivo_avaliacao

Essas informações devem ser coerentes com:

• medicamento citado
• objetivo terapêutico
• situação clínica descrita

Se não houver segurança suficiente, deixar esses subcampos vazios.

Nunca adicionar medicamento não citado.

══════════════════════════════════════
INFERÊNCIA PERMITIDA (SEGUIMENTO)
══════════════════════════════════════

Se o prontuário NÃO informar retorno clínico,
é permitido estimar um retorno provável.

Essa inferência é permitida apenas no campo:

seguimento_retorno_estimado

A estimativa deve considerar:

• farmacodinâmica do medicamento
• tempo de resposta terapêutica
• monitorização de efeitos adversos
• tempo usual de seguimento neuropediátrico
• início recente de tratamento
• necessidade de reavaliar conduta recente

A estimativa deve incluir:

• intervalo estimado
• data calendário estimada
• motivo clínico
• base clínica da estimativa
• parâmetros a serem avaliados
• nível de prioridade

Se houver medicação recém-iniciada ou recém-ajustada, priorizar o tempo típico necessário
para avaliar resposta e tolerabilidade inicial.

Se houver início de terapia ou necessidade de observar evolução comportamental,
considerar o tempo razoável para surgirem melhora, piora ou efeitos colaterais detectáveis.

REGRA OBRIGATÓRIA (DOSE-ALVO E RETORNO):

Sempre que o texto indicar retorno relacionado a “dose alvo”, “dose-alvo”,
“após atingir dose”, “quando alcançar dose”, “após titulação” ou equivalente,
você DEVE:

1) identificar no próprio prontuário qual medicação está sendo titulada;
2) identificar, no texto, o ritmo de ajuste e o ponto de dose-alvo (se existirem);
3) se faltar dado no prontuário, inferir o tempo médio plausível de titulação
   com base em prática clínica usual para essa medicação (sem inventar dose não citada);
4) converter “X semanas após dose alvo” em data calendário estimada;
5) preencher "data_estimada" no formato YYYY-MM-DD sempre que houver base temporal mínima.

Nesses casos, "data_estimada" não deve ficar vazia se existir referência temporal
suficiente no prontuário (data da consulta, ritmo de ajuste, marco de dose-alvo
ou janela usual de titulação). Explique o racional em "motivo_clinico" e "base_clinica".

══════════════════════════════════════
PRIORIZAÇÃO DO RETORNO
══════════════════════════════════════

O nível de prioridade deve considerar:

baixo
moderado
alto

Situações que aumentam prioridade:

• regressão clínica
• agressividade relevante
• início recente de medicação
• ajuste recente de dose
• sintomas neurológicos novos
• piora importante do comportamento
• necessidade de avaliar tolerabilidade medicamentosa

══════════════════════════════════════
CLASSIFICAÇÃO DE GRAVIDADE
══════════════════════════════════════

Classificar gravidade clínica apenas se houver evidência suficiente.

Valores possíveis:

leve
moderada
grave

Se não houver dados suficientes → null.

══════════════════════════════════════
CONDUTAS ESPECÍFICAS SUGERIDAS
══════════════════════════════════════

Podem ser sugeridas condutas adicionais baseadas em evidência científica.

Cada conduta deve conter:

conduta
justificativa
referencia
fonte

Fontes aceitáveis:

• PubMed
• AAP
• AACAP
• Cochrane
• WHO
• SBP
• CFM
• Ministério da Saúde

Nunca inventar PMID.

A referência deve ser coerente com:

• o medicamento
• a condição clínica
• a intervenção sugerida

Se não houver segurança sobre a referência, deixar referencia e fonte vazias ou não incluir a conduta.

══════════════════════════════════════
CONDUTAS GERAIS SUGERIDAS
══════════════════════════════════════

Condutas baseadas em boa prática clínica.

Podem incluir:

• orientações ao cuidador
• monitorização clínica
• sinais de alerta
• acompanhamento clínico
• observação da resposta a tratamento
• atenção a efeitos adversos

Evitar recomendações genéricas.

Devem ser coerentes com o quadro clínico descrito.

══════════════════════════════════════
VERIFICAÇÃO FINAL DE CONSISTÊNCIA
══════════════════════════════════════

Antes de responder, verificar:

• todos os medicamentos realmente aparecem no prontuário?
• doses estão exatamente iguais ao texto?
• nenhum diagnóstico foi criado?
• nenhum exame foi inventado?
• nenhuma terapia foi inventada?
• nenhuma conduta específica foi baseada em referência inadequada?
• o seguimento estimado é coerente com a medicação e o quadro clínico?

Se qualquer uma dessas situações ocorrer, remover ou corrigir a informação.

══════════════════════════════════════
FORMATO DO JSON
══════════════════════════════════════

{
  "diagnosticos_citados": [],

  "idade_paciente": {
    "valor": null,
    "unidade": null
  },

  "pontos_chave": [],

  "mudancas_relevantes": [],

  "eventos_comportamentais": [],

  "sinais_nucleares": [],

  "medicacoes_em_uso": [
    {
      "nome": "",
      "dose": "",
      "posologia": "",
      "desde": "",
      "observacao": "",

      "avaliacao_resposta_esperada": {
        "tempo_resposta_estimado": "",
        "parametros_monitorizacao": [],
        "motivo_avaliacao": ""
      }
    }
  ],

  "medicacoes_iniciadas": [
    {
      "nome": "",
      "dose": "",
      "posologia": "",
      "data_relativa": ""
    }
  ],

  "medicacoes_suspensas": [
    {
      "nome": "",
      "dose": "",
      "posologia": "",
      "motivo": "",
      "periodo": ""
    }
  ],

  "terapias_referidas": [],

  "exames_citados": [],

  "pendencias_clinicas": [],

  "condutas_registradas_no_prontuario": [],

  "seguimento_retorno_estimado": {
    "intervalo_estimado": "",
    "data_estimada": "",
    "motivo_clinico": "",
    "base_clinica": "",
    "parametros_a_avaliar": [],
    "nivel_prioridade": ""
  },

  "gravidade_clinica": null,

  "condutas_especificas_sugeridas": [
    {
      "conduta": "",
      "justificativa": "",
      "referencia": "",
      "fonte": "",

      "impacto_clinico_esperado": {
        "tempo_estimado_resposta": "",
        "objetivo_clinico": "",
        "indicadores_de_melhora": []
      }
    }
  ],

  "condutas_gerais_sugeridas": [
    {
      "descricao": "",
      "motivo_clinico": "",
      "sinais_alerta": [],
      "orientacao_cuidador": ""
    }
  ],

  "resumo_texto": ""
}

Responder SOMENTE com o JSON.

[FIM_TEXTO_COLADO]
"""
#================================================SYSTEM_PROMPT = FIM ===============================================

def buscar_prompt_db(id_atendimento: int | None = None):
    """Busca o prompt do analisador no banco.

    Prioridade:
      1) chatgpt_prompts.tipo='system' + escopo='analisador_prontuarios' +
         id_criador igual ao id_criador da linha de chatgpt_atendimentos_analise.
      2) fallback legado: id_criador='atendimentos_analise'.
    Retorna o conteudo se encontrado, ou None para usar o SYSTEM_PROMPT local.
    """
    regra_dose_alvo = """

REGRA OBRIGATÓRIA (DOSE-ALVO E RETORNO):
- Se houver “dose alvo”, “após dose-alvo”, “após atingir dose” ou equivalente no contexto de seguimento,
  identificar medicação e tempo de titulação (no prontuário ou por tempo médio plausível),
  e preencher "seguimento_retorno_estimado.data_estimada" em YYYY-MM-DD quando houver base temporal mínima.
- Se o intervalo estiver condicionado à dose-alvo, não deixar "data_estimada" vazia sem justificativa clínica explícita.
"""
    try:
        prompt_sql = (
            "SELECT p.conteudo "
            "FROM chatgpt_prompts p "
            "WHERE p.tipo='system' "
            "  AND p.escopo='analisador_prontuarios' "
            "  AND p.id_criador='atendimentos_analise' "
            "LIMIT 1"
        )
        if id_atendimento:
            prompt_sql = (
                "SELECT p.conteudo "
                "FROM chatgpt_prompts p "
                "WHERE p.tipo='system' "
                "  AND p.escopo='analisador_prontuarios' "
                "  AND p.id_criador = COALESCE(("
                "    SELECT CAST(a.id_criador AS CHAR) "
                "    FROM chatgpt_atendimentos_analise a "
                f"    WHERE a.id_atendimento = {int(id_atendimento)} "
                "    ORDER BY a.id DESC LIMIT 1"
                "  ), 'atendimentos_analise') "
                "ORDER BY CASE WHEN p.id_criador='atendimentos_analise' THEN 1 ELSE 0 END "
                "LIMIT 1"
            )

        try:
            data = sql_exec(prompt_sql, reason="buscar_prompt_analisador")
        except Exception:
            # Compatibilidade temporária: bancos ainda sem coluna escopo.
            data = sql_exec(
                "SELECT conteudo FROM chatgpt_prompts WHERE tipo='system' AND id_criador='atendimentos_analise' LIMIT 1",
                reason="buscar_prompt_analisador_legacy"
            )
        rows = data.get("data") or []
        if rows and rows[0].get("conteudo"):
            conteudo = rows[0]["conteudo"]
            if "[INICIO_TEXTO_COLADO]" not in conteudo:
                conteudo = "[INICIO_TEXTO_COLADO]" + conteudo + "[FIM_TEXTO_COLADO]"
            if "REGRA OBRIGATÓRIA (DOSE-ALVO E RETORNO)" not in conteudo:
                conteudo = conteudo.replace("[FIM_TEXTO_COLADO]", regra_dose_alvo + "\n[FIM_TEXTO_COLADO]")
            log.info("Prompt do analisador carregado do banco (chatgpt_prompts).")
            return conteudo
        log.info("Prompt do analisador nao encontrado no banco - usando prompt local.")
        return None
    except Exception as e:
        log.warning(f"Erro ao buscar prompt do banco: {e} - usando prompt local.")
        return None

def verificar_llm() -> bool:
    """Ping rápido no simulador local. Retorna True se disponível, False se não."""
    try:
        base_url = LLM_URL.replace("/v1/chat/completions", "")
        r = requests.get(
            f"{base_url}/health",
            timeout=5,
            headers={"User-Agent": "AnalisadorProntuarios/1.0"}
        )
        return r.status_code == 200
    except Exception:
        return False


def aguardar_llm_startup():
    """
    Aguarda o simulador local estar pronto no startup.
    Fica tentando INDEFINIDAMENTE (nunca desiste) — o daemon não tem
    utilidade sem o simulador, então espera pacientemente.
    """
    log.info(f"⏳ Aguardando ChatGPT Simulator em {LLM_URL} ...")
    tentativa = 0
    while True:
        tentativa += 1
        try:
            if verificar_llm():
                log.info("✅ ChatGPT Simulator disponível.")
                return
        except Exception:
            pass  # verificar_llm já trata exceções, mas por segurança
        if tentativa == 1:
            log.warning("⚠️  ChatGPT Simulator não encontrado. Aguardando...")
        # Exibe progresso a cada 10 tentativas (~30s)
        if tentativa % 10 == 0:
            log.info(f"   🔄 Tentativa #{tentativa}... ChatGPT Simulator ainda não respondeu.")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            raise


def analisar_prontuario(
    texto: str,
    chat_url: str = None,
    chat_id: str = None,
    contexto: str = "",
    id_atendimento: int | None = None,
) -> dict:
    """
    chat_url / chat_id: se fornecidos, o browser.py retoma a conversa existente
    em vez de abrir um novo chat — evita proliferação de chats no ChatGPT.
    contexto: bloco de texto com dados do paciente, profissional e hospital
    (inserido antes do prontuário para dar contexto ao LLM).
    id_atendimento: parâmetro opcional para compatibilidade com chamadas legadas.
    """
    # Monta bloco de contexto se disponível
    bloco_contexto = ""
    if contexto:
        bloco_contexto = (
            f"\n══════════════════════════════════════\n"
            f"CONTEXTO DO ATENDIMENTO\n"
            f"══════════════════════════════════════\n"
            f"{contexto}\n"
            f"══════════════════════════════════════\n\n"
        )

    user_content = (
        f"[INICIO_TEXTO_COLADO]Analise esta evolução clínica:\n\n"
        f"{bloco_contexto}"
        f"---\n{texto[:12000]}\n---\n"
        f"[FIM_TEXTO_COLADO]"
    )

    payload = {
        "model":   LLM_MODEL,
        "stream":  True,
        "messages": [
            {"role": "system", "content": buscar_prompt_db(id_atendimento=id_atendimento) or SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        "browser_profile": BROWSER_PROFILE,
    }

    # Retoma conversa existente se disponível
    if chat_url:
        payload["url"]    = chat_url
    if chat_id:
        payload["chatid"] = chat_id

    new_chat_id    = chat_id    # fallback: mantém o anterior se não vier novo
    new_chat_url   = chat_url
    new_chat_title = None
    markdown       = ""

    def _clean_simulator_log_for_local_view(raw: str) -> str:
        """Limpa redundâncias de remetente para exibição local no próprio remetente."""
        msg = str(raw or "").strip()
        if not msg:
            return ""
        # O analisador não usa screenshots; evita poluir o console.
        if "screenshot stream" in msg.lower():
            return ""
        # Remove prefixo repetido vindo do server stream.
        msg = re.sub(r"^\s*Remetente:\s*[^|]+\|\s*", "", msg, flags=re.IGNORECASE)
        # Remove remetente duplicado no logger do browser: [browser.py] [analisador...]
        msg = re.sub(r"(\[browser\.py\])\s+\[[^\]]+\]\s+", r"\1 ", msg, flags=re.IGNORECASE)
        return msg.strip()

    # Funcao auxiliar para progresso inline (sobrescreve a linha atual no CMD)
    def _inline(msg):
        largura_terminal = shutil.get_terminal_size((140, 20)).columns
        largura_util = max(30, largura_terminal - 2)
        linha = f"  {str(msg or '')}"
        sys.stdout.write('\r' + linha.ljust(largura_util))
        sys.stdout.flush()

    def _newline():
        sys.stdout.write('\n')
        sys.stdout.flush()

    def _inline_status(prefixo: str, msg: str):
        texto = re.sub(r"\s+", " ", str(msg or "")).strip()
        texto = re.sub(r"^\s*Remetente:\s*[^|]+\|\s*", "", texto, flags=re.IGNORECASE)
        cooldown_match = re.search(r"nova tentativa em\s*([0-9]{1,2}:[0-9]{2})", texto, flags=re.IGNORECASE)
        if cooldown_match:
            texto = f"Aguardando cooldown do ChatGPT | nova tentativa em {cooldown_match.group(1)}"
        if not texto:
            return

        largura_terminal = shutil.get_terminal_size((140, 20)).columns
        largura_util = max(30, largura_terminal - 6)
        mensagem = f"{prefixo} {texto}"
        if len(mensagem) > largura_util:
            mensagem = mensagem[:max(0, largura_util - 3)].rstrip() + "..."
        _inline(mensagem)

    tentativa_stream = 0
    while True:
        tentativa_stream += 1
        inline_active = False  # True quando ha uma linha inline aberta
        try:
            resp = _post_llm(payload)
            last_status = ""
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                t = obj.get("type")

                if t == "status":
                    msg = obj.get("content", "")
                    phase = str(obj.get("phase") or "").strip().lower()
                    wait_seconds = obj.get("wait_seconds")
                    if phase == "chat_rate_limit_cooldown" and wait_seconds is not None:
                        try:
                            wait_seconds = max(0, int(round(float(wait_seconds))))
                            mm, ss = divmod(wait_seconds, 60)
                            msg = f"Aguardando cooldown do ChatGPT | nova tentativa em {mm:02d}:{ss:02d}"
                        except Exception:
                            pass
                    if msg == last_status:
                        continue
                    last_status = msg
                    _inline_status('⏳', msg)
                    inline_active = True
                    continue

                if t == "log":
                    if inline_active:
                        _newline()
                        inline_active = False
                    cleaned_log = _clean_simulator_log_for_local_view(obj.get("content", ""))
                    if cleaned_log:
                        log.info(f"  🔧 {cleaned_log}")
                    continue

                if t == "chatid":
                    if inline_active:
                        _newline()
                        inline_active = False
                    new_chat_id = obj.get("content") or new_chat_id
                    log.info(f"  📎 chat_id: {new_chat_id}")
                    continue

                if t == "markdown":
                    markdown = obj.get("content", "")
                    markdown_visivel = _extrair_markdown_visivel_llm(markdown)
                    if markdown_visivel:
                        _inline_status('📝', f"Recebendo: {len(markdown_visivel)} chars...")
                    else:
                        _inline_status('⏳', "Pensando...")
                    inline_active = True
                    continue

                if t == "finish":
                    if inline_active:
                        _newline()
                        inline_active = False
                    fin = obj.get("content", {})
                    new_chat_url = fin.get("url") or new_chat_url
                    new_chat_title = fin.get("title") or new_chat_title
                    new_chat_id = fin.get("chat_id") or new_chat_id
                    # fallback: extrai chat_id da URL caso ainda não tenha sido recebido
                    if not new_chat_id and new_chat_url:
                        new_chat_id = new_chat_url.rstrip('/').split('/')[-1] or new_chat_id
                    log.info(f"  🔗 chat_url: {new_chat_url} | chat_id: {new_chat_id}")
                    continue

                if t == "error":
                    if inline_active:
                        _newline()
                        inline_active = False
                    raise RuntimeError(f"Simulador retornou erro: {obj.get('content')}")

            if inline_active:
                _newline()
                inline_active = False
            break
        except Exception as e:
            if inline_active:
                _newline()
            if tentativa_stream < 3:
                espera = min(10, 2 * tentativa_stream)
                log.warning(
                    f"  ⚠️ Falha no stream da LLM (tentativa {tentativa_stream}/3): {e}. "
                    f"Nova tentativa em {espera}s..."
                )
                time.sleep(espera)
                continue
            last_status = msg
            _inline_status('⏳', msg)
            inline_active = True
            continue

        if t == "log":
            if inline_active:
                _newline()
                inline_active = False
            cleaned_log = _clean_simulator_log_for_local_view(obj.get("content", ""))
            if cleaned_log:
                log.info(f"  🔧 {cleaned_log}")
            continue

        if t == "chatid":
            if inline_active:
                _newline()
                inline_active = False
            new_chat_id = obj.get("content") or new_chat_id
            log.info(f"  📎 chat_id: {new_chat_id}")
            continue

        if t == "markdown":
            markdown = obj.get("content", "")
            markdown_visivel = _extrair_markdown_visivel_llm(markdown)
            if markdown_visivel:
                _inline_status('📝', f"Recebendo: {len(markdown_visivel)} chars...")
            else:
                _inline_status('⏳', "Pensando...")
            inline_active = True
            continue

        if t == "finish":
            if inline_active:
                _newline()
                inline_active = False
            fin = obj.get("content", {})
            new_chat_url = fin.get("url") or new_chat_url
            new_chat_title = fin.get("title") or new_chat_title
            new_chat_id = fin.get("chat_id") or new_chat_id
            # fallback: extrai chat_id da URL caso ainda não tenha sido recebido
            if not new_chat_id and new_chat_url:
                new_chat_id = new_chat_url.rstrip('/').split('/')[-1] or new_chat_id
            log.info(f"  🔗 chat_url: {new_chat_url} | chat_id: {new_chat_id}")
            continue

        if t == "error":
            if inline_active:
                _newline()
                inline_active = False
            raise RuntimeError(f"Simulador retornou erro: {obj.get('content')}")

    if not markdown:
        raise ValueError("Simulador não retornou conteúdo markdown.")

    try:
        resultado = _parse_json_llm(markdown)
    except Exception as parse_err:
        path_md, path_meta, meta = _salvar_debug_json_falha(
            id_atendimento,
            "analise_principal",
            markdown,
            parse_err,
        )
        preview = re.sub(r"\s+", " ", _strip_code_fences(markdown))[:500]
        raise ValueError(
            f"LLM não retornou JSON válido ({parse_err}). "
            f"incompleto={meta['json_parece_incompleto']} | debug: {path_md} | {path_meta} | Prévia: {preview}"
        ) from parse_err

    resultado["_chat_id"]       = new_chat_id
    resultado["_chat_url"]       = new_chat_url
    resultado["_chat_title"]     = new_chat_title
    resultado["modelo_llm"]      = LLM_MODEL
    resultado["prompt_version"]  = PROMPT_VERSION
    resultado["hash_prontuario"] = hashlib.sha256(texto.encode('utf-8', errors='ignore')).hexdigest()
    return resultado


# ─────────────────────────────────────────────────────────────
# BUSCA WEB + ENRIQUECIMENTO DE CONDUTAS COM EVIDÊNCIAS
# ─────────────────────────────────────────────────────────────

def buscar_web(queries: list) -> list:
    """
    Chama o endpoint /api/web_search do ChatGPT Simulator.
    Cada query abre uma aba do Google, digita via type_realistic e scrapa resultados.

    Retorna lista de dicts: [{query, results: [{title, url, snippet}]}]
    """
    if not queries:
        return []

    try:
        resp = requests.post(
            SEARCH_URL,
            json={"queries": queries},
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            timeout=SEARCH_TIMEOUT * len(queries),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as e:
        log.warning(f"  ⚠️ Busca web falhou: {e}")
        return []


def buscar_uptodate(queries: list) -> list:
    """
    Chama o endpoint /api/uptodate_search do ChatGPT Simulator.
    Retorna lista de dicts: [{query, results: [{title, url, snippet}]}]
    """
    if not queries:
        return []

    try:
        resp = requests.post(
            UPTODATE_SEARCH_URL,
            json={"queries": queries},
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            timeout=SEARCH_TIMEOUT * len(queries),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as e:
        log.warning(f"  ⚠️ Busca UpToDate falhou: {e}")
        return []


def extrair_termos_busca(resultado: dict) -> list:
    """
    Extrai termos clínicos relevantes do resultado da análise e monta
    queries otimizadas para PubMed/Google Scholar.

    Fontes (em ordem de prioridade):
      1. diagnosticos_citados (lista nominal)
      2. grafo_clinico_nodes onde tipo=diagnostico/medicamento (dados tipados)
      3. medicacoes_em_uso / iniciadas / suspensas
      4. resumo_texto (último recurso)
    """
    def _ensure_list(val):
        if val is None:
            return []
        if isinstance(val, str):
            val = val.strip()
            if not val:
                return []
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            return [val] if len(val) > 2 else []
        if isinstance(val, list):
            return val
        return []

    def _extrair_nomes_med(lista_meds):
        nomes = []
        for m in lista_meds:
            if isinstance(m, dict):
                nome = (m.get("medicacao") or m.get("nome") or m.get("name") or "").strip()
                if nome:
                    nomes.append(nome)
            elif isinstance(m, str) and m.strip():
                nomes.append(m.strip())
        return nomes

    def _extrair_do_grafo(resultado, tipo_node):
        """Extrai valores de nodes do grafo clínico por tipo (diagnostico, medicamento, etc.)."""
        nodes = _ensure_list(resultado.get("grafo_clinico_nodes"))
        valores = []
        for nd in nodes:
            if isinstance(nd, dict):
                nd_tipo = (nd.get("tipo") or nd.get("node_tipo") or "").lower()
                nd_valor = (nd.get("valor") or nd.get("node_valor") or "").strip()
                if nd_tipo == tipo_node and nd_valor and len(nd_valor) > 2:
                    valores.append(nd_valor)
        return valores

    def _compactar_termo_busca(valor, max_chars=90):
        valor = re.sub(r"\s+", " ", str(valor or "")).strip(" ,;:-")
        if len(valor) <= max_chars:
            return valor
        corte = valor.rfind(" ", 0, max_chars + 1)
        if corte >= int(max_chars * 0.6):
            return valor[:corte].strip(" ,;:-")
        return valor[:max_chars].strip(" ,;:-")

    def _compactar_termo_busca(valor, max_chars=90):
        valor = re.sub(r"\s+", " ", str(valor or "")).strip(" ,;:-")
        if len(valor) <= max_chars:
            return valor
        corte = valor.rfind(" ", 0, max_chars + 1)
        if corte >= int(max_chars * 0.6):
            return valor[:corte].strip(" ,;:-")
        return valor[:max_chars].strip(" ,;:-")

    def _compactar_termo_busca(valor, max_chars=90):
        valor = re.sub(r"\s+", " ", str(valor or "")).strip(" ,;:-")
        if len(valor) <= max_chars:
            return valor
        corte = valor.rfind(" ", 0, max_chars + 1)
        if corte >= int(max_chars * 0.6):
            return valor[:corte].strip(" ,;:-")
        return valor[:max_chars].strip(" ,;:-")

    def _compactar_termo_busca(valor, max_chars=90):
        valor = re.sub(r"\s+", " ", str(valor or "")).strip(" ,;:-")
        if len(valor) <= max_chars:
            return valor
        corte = valor.rfind(" ", 0, max_chars + 1)
        if corte >= int(max_chars * 0.6):
            return valor[:corte].strip(" ,;:-")
        return valor[:max_chars].strip(" ,;:-")

    def _compactar_termo_busca(valor, max_chars=90):
        valor = re.sub(r"\s+", " ", str(valor or "")).strip(" ,;:-")
        if len(valor) <= max_chars:
            return valor
        corte = valor.rfind(" ", 0, max_chars + 1)
        if corte >= int(max_chars * 0.6):
            return valor[:corte].strip(" ,;:-")
        return valor[:max_chars].strip(" ,;:-")

    # ── 1. Fonte primária: listas nominais ────────────────────
    diagnosticos = _ensure_list(
        resultado.get("diagnosticos_citados") or resultado.get("diagnosticos_mencionados")
    )
    diags = []
    for d in diagnosticos:
        if isinstance(d, dict):
            val = (d.get("diagnostico") or d.get("nome") or d.get("descricao") or d.get("valor") or "").strip()
            if val:
                diags.append(val)
        elif isinstance(d, str) and d.strip():
            diags.append(d.strip())
    diags = diags[:3]

    # ── 2. Fallback: grafo clínico (nodes tipados) ────────────
    if not diags:
        diags = _extrair_do_grafo(resultado, "diagnostico")[:3]
        if diags:
            log.info(f"  🔄 Diagnósticos extraídos do grafo clínico: {diags}")

    # ── 3. Medicações ─────────────────────────────────────────
    medicacoes = _ensure_list(
        resultado.get("medicacoes_em_uso") or resultado.get("medicamentos_em_uso")
    )
    meds = _extrair_nomes_med(medicacoes)[:3]

    if not meds:
        # Tenta grafo
        meds_grafo = _extrair_do_grafo(resultado, "medicamento")
        if not meds_grafo:
            meds_grafo = _extrair_do_grafo(resultado, "medicacao")
        if meds_grafo:
            meds = meds_grafo[:3]
            log.info(f"  🔄 Medicações extraídas do grafo clínico: {meds}")

    if not meds:
        # Tenta iniciadas/suspensas
        for campo in ("medicacoes_iniciadas", "medicamentos_iniciados",
                       "medicacoes_suspensas", "medicamentos_suspensos"):
            extra = _ensure_list(resultado.get(campo))
            meds.extend(_extrair_nomes_med(extra))
            if meds:
                break
        meds = meds[:3]

    # ── 4. Último recurso: resumo_texto ───────────────────────
    if not diags and not meds:
        resumo = resultado.get("resumo_texto") or ""
        if isinstance(resumo, str) and len(resumo) > 20:
            diags.append(resumo[:100].strip())

    # Log de diagnóstico
    log.info(f"  🔬 extrair_termos: diagnosticos={len(diags)} | medicacoes={len(meds)}")
    if diags:
        log.info(f"     📋 Diagnósticos/termos: {[d[:60] for d in diags[:3]]}")
    if meds:
        log.info(f"     💊 Medicações: {meds[:3]}")

    if not diags and not meds:
        log.info(f"  🔍 Nenhum termo clínico extraído para busca.")
        return []

    # ── Extrai contexto adicional para queries mais específicas ──
    terapias = _ensure_list(resultado.get("terapias_referidas"))
    terapias_nomes = []
    for t in terapias:
        if isinstance(t, dict):
            nome = (t.get("terapia") or t.get("nome") or "").strip()
            if nome:
                terapias_nomes.append(nome)
        elif isinstance(t, str) and t.strip():
            terapias_nomes.append(t.strip())

    pendencias = _ensure_list(resultado.get("pendencias_clinicas"))
    pendencias_txt = []
    for p in pendencias:
        if isinstance(p, dict):
            txt = (p.get("pendencia") or p.get("descricao") or "").strip()
            if txt:
                pendencias_txt.append(txt)
        elif isinstance(p, str) and p.strip():
            pendencias_txt.append(p.strip())

    # Detecta se paciente está sem medicação
    sem_medicacao = not meds and not _ensure_list(resultado.get("medicacoes_iniciadas"))

    queries = []

    # Query 1: diagnóstico principal + contexto clínico específico
    if diags:
        # Monta query contextualizada com o cenário clínico
        contexto_extra = ""
        if sem_medicacao and terapias_nomes:
            contexto_extra = "behavioral therapy without medication"
        elif sem_medicacao:
            contexto_extra = "non-pharmacological treatment"
        elif meds:
            contexto_extra = f"{meds[0]} treatment"

        diag_base = _compactar_termo_busca(diags[0], max_chars=90)
        q1 = f"{diag_base} {contexto_extra} children site:pubmed.ncbi.nlm.nih.gov".strip()
        queries.append(q1)

    # Query 2: segundo diagnóstico/sintoma (se existir — busca mais específica)
    if len(diags) > 1:
        diag_sec = _compactar_termo_busca(diags[1], max_chars=90)
        q2 = f"{diag_sec} children treatment evidence site:pubmed.ncbi.nlm.nih.gov"
        queries.append(q2)
    elif meds:
        # Se não tem segundo diagnóstico, busca sobre o medicamento
        q2 = f"{meds[0]} children adverse effects systematic review site:pubmed.ncbi.nlm.nih.gov"
        queries.append(q2)

    # Query 3: terapia/pendência mais relevante (se houver)
    if len(queries) < SEARCH_MAX_QUERIES:
        # Procura sintomas no grafo para uma query mais específica
        sintomas_grafo = _extrair_do_grafo(resultado, "sintoma")
        if sintomas_grafo:
            diag_curto = _compactar_termo_busca(diags[0], max_chars=60)
            sintoma_curto = _compactar_termo_busca(sintomas_grafo[0], max_chars=60)
            q3 = f"{diag_curto} {sintoma_curto} pediatric management"
            queries.append(q3)
        elif terapias_nomes:
            diag_curto = _compactar_termo_busca(diags[0], max_chars=60)
            terapia_curta = _compactar_termo_busca(terapias_nomes[0], max_chars=50)
            q3 = f"{diag_curto} {terapia_curta} effectiveness children"
            queries.append(q3)

    return queries[:SEARCH_MAX_QUERIES]


def formatar_resultados_busca(resultados_web: list) -> str:
    """
    Formata os resultados da busca web em texto legível para a LLM.
    Se o scraper não encontrou resultados estruturados mas o raw_html está disponível,
    envia o HTML limpo para a LLM interpretar diretamente.
    """
    if not resultados_web:
        return ""

    blocos = []
    for res in resultados_web:
        if not res.get("success", True):
            continue
        query   = res.get("query", "")
        items   = res.get("results", [])
        raw_html = res.get("raw_html", "")

        if items:
            # Resultados estruturados encontrados
            bloco = f"Busca: \"{query}\"\n"
            for item in items:
                tipo = item.get("type", "organic")
                if tipo == "people_also_ask":
                    continue  # não relevante para evidência
                titulo  = item.get("title", "")
                url     = item.get("url", "")
                snippet = item.get("snippet", "")
                if tipo == "featured_snippet":
                    bloco += f"  ★ {snippet[:300]}\n"
                elif titulo:
                    bloco += f"  • {titulo}\n"
                    if url:
                        bloco += f"    URL: {url}\n"
                    if snippet:
                        bloco += f"    {snippet[:200]}\n"
            blocos.append(bloco)

        elif raw_html:
            # Fallback: scraper não encontrou resultados, mas temos o HTML da página
            log.info(f"  🔄 Sem resultados estruturados para \"{query}\" — usando HTML bruto como fallback")
            bloco = (
                f"Busca: \"{query}\"\n"
                f"[O scraper não conseguiu extrair resultados estruturados. "
                f"Segue o HTML da página de resultados do Google para interpretação direta.]\n\n"
                f"{raw_html[:10000]}\n"
            )
            blocos.append(bloco)

    return "\n".join(blocos)


PROMPT_GERAR_PESQUISA = """Com base no caso clínico estruturado abaixo, gere queries de pesquisa web realmente pensadas para ESTE paciente específico.

OBJETIVO:
- produzir até 3 queries de alta utilidade clínica para apoiar condutas, seguimento, monitorização, terapias e/ou segurança medicamentosa;
- considerar o perfil do paciente, gravidade, diagnóstico principal, comorbidades, genética, medicações, ausência de fala funcional, terapias e pendências;
- priorizar evidência científica e diretrizes realmente úteis para o caso concreto.

FORMATO OBRIGATÓRIO:
{
  "uptodate_queries": [
    {
      "query": "consulta médica apropriada para UpToDate",
      "reason": "por que pesquisar este tema médico no UpToDate é útil para este paciente"
    }
  ],
  "search_queries": [
    {
      "query": "consulta para Google/PubMed",
      "reason": "por que esta busca é útil para este paciente"
    }
  ]
}

REGRAS:
- Responder SOMENTE com JSON válido.
- Máximo de 3 queries por lista.
- Preferir inglês quando isso melhorar a busca científica.
- Você PODE usar `uptodate_queries` para temas médicos/clínicos em que o UpToDate tende a ter bons resultados.
- NÃO usar `uptodate_queries` para pesquisar pessoas, notícias, instituições, temas administrativos ou assuntos não médicos.
- Quando o tema for claramente médico, clínico, terapêutico, diagnóstico ou de monitorização, considere priorizar `uptodate_queries`.
- Quando fizer sentido, usar PubMed, diretrizes pediátricas, systematic review, guideline, consensus.
- Não inventar fatos que não estejam explícitos no caso.
- Não repetir queries redundantes.
"""

PROMPT_AUXILIAR_GRAFO_V18 = """ASSISTENTE DE ANÁLISE CLÍNICA DE PRONTUÁRIOS — PROMPT V18
Compatível com arquitetura SQL + RAG + CDSS

OBJETIVO
Transformar o caso clínico estruturado abaixo em dados auxiliares confiáveis e específicos,
compatíveis com:
- chatgpt_alertas_clinicos
- chatgpt_clinical_graph_nodes
- chatgpt_clinical_graph_edges

PRINCÍPIOS
- PRIORIDADE ABSOLUTA: extração fiel do texto/caso estruturado.
- Nunca inventar dados ausentes.
- Nunca criar diagnóstico, medicação, terapia ou exame sem base explícita.
- Se não houver informação suficiente, devolver lista vazia ou texto vazio.

FOCO PRINCIPAL DESTA ETAPA
Gerar um grafo clínico útil e ESPECÍFICO para o caso.

REGRAS PARA O GRAFO CLÍNICO
- Os nodes devem representar entidades clínicas concretas do caso, por exemplo:
  paciente, diagnóstico, sintoma, comportamento, medicamento, terapia, exame, pendência, risco, gene.
- Evitar nodes genéricos demais como "paciente", "tratamento", "consulta", "medicação" sem detalhamento.
- Preferir nodes específicos como:
  "TEA", "deficiência intelectual", "aripiprazol", "topiramato", "ausência de fala funcional",
  "avaliação genética pendente", "psicologia", "fácies dismórfica".
- O campo `normalizado` deve ser estável e curto.
- O campo `contexto` deve explicar por que aquele node existe no caso concreto.
- As edges devem ligar entidades reais do caso e usar relações semânticas específicas.

NOVA REGRA — MENSAGENS DE ACOMPANHAMENTO
Quando houver base suficiente no caso, as mensagens de acompanhamento devem ser humanas,
acolhedoras, personalizadas e não robóticas.

FORMATO DE RESPOSTA
Responder SOMENTE com JSON válido, sem markdown, sem explicações e sem campos extras.

SCHEMA OBRIGATÓRIO:
{
  "alertas_clinicos": [
    {
      "tipo_alerta": "",
      "descricao": "",
      "nivel_risco": ""
    }
  ],
  "grafo_clinico_nodes": [
    {
      "id": "",
      "tipo": "",
      "valor": "",
      "normalizado": "",
      "contexto": ""
    }
  ],
  "grafo_clinico_edges": [
    {
      "node_origem": "",
      "node_destino": "",
      "relacao_tipo": "",
      "contexto": ""
    }
  ],
  "mensagens_acompanhamento": {
    "mensagem_1_semana": "",
    "mensagem_1_mes": "",
    "mensagem_pre_retorno": ""
  }
}
"""


def _ensure_list_local(val):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _grafo_clinico_esta_generico(resultado: dict) -> bool:
    nodes = _ensure_list_local(
        resultado.get("grafo_clinico_nodes")
        or resultado.get("grafo_nodes")
        or resultado.get("nodes")
    )
    if not nodes:
        return True

    tipos_relevantes = {
        "diagnostico", "sintoma", "medicamento", "terapia",
        "exame", "pendencia", "risco", "gene", "comportamento",
    }
    nodes_relevantes = [
        n for n in nodes
        if isinstance(n, dict)
        and (n.get("valor") or n.get("node_valor"))
        and ((n.get("tipo") or n.get("node_tipo") or "").lower() in tipos_relevantes)
    ]
    return len(nodes_relevantes) < 2


def gerar_dados_auxiliares_llm(resultado: dict, chat_url: str = None, chat_id: str = None) -> dict:
    """
    Pede à própria LLM, na mesma conversa do caso, um refinamento dos campos
    auxiliares mais sensíveis a especificidade semântica (grafo, alertas e
    mensagens de acompanhamento), usando um prompt dedicado de estilo V18.
    """
    contexto_auxiliar = {
        chave: valor
        for chave, valor in {
            "resumo_texto": resultado.get("resumo_texto"),
            "gravidade_clinica": resultado.get("gravidade_clinica"),
            "score_risco": resultado.get("score_risco"),
            "diagnosticos_citados": resultado.get("diagnosticos_citados"),
            "pontos_chave": resultado.get("pontos_chave"),
            "mudancas_relevantes": resultado.get("mudancas_relevantes"),
            "eventos_comportamentais": resultado.get("eventos_comportamentais"),
            "sinais_nucleares": resultado.get("sinais_nucleares"),
            "medicacoes_em_uso": resultado.get("medicacoes_em_uso"),
            "medicacoes_iniciadas": resultado.get("medicacoes_iniciadas"),
            "medicacoes_suspensas": resultado.get("medicacoes_suspensas"),
            "terapias_referidas": resultado.get("terapias_referidas"),
            "exames_citados": resultado.get("exames_citados"),
            "pendencias_clinicas": resultado.get("pendencias_clinicas"),
            "condutas_no_prontuario": resultado.get("condutas_no_prontuario"),
            "condutas_especificas_sugeridas": resultado.get("condutas_especificas_sugeridas"),
            "condutas_gerais_sugeridas": resultado.get("condutas_gerais_sugeridas"),
            "seguimento_retorno_estimado": resultado.get("seguimento_retorno_estimado"),
        }.items()
        if valor not in (None, "", [], {})
    }

    if not contexto_auxiliar:
        return resultado

    user_content = (
        f"[INICIO_TEXTO_COLADO]\n"
        f"{PROMPT_AUXILIAR_GRAFO_V18}\n\n"
        f"CASO CLÍNICO ESTRUTURADO (JSON):\n"
        f"{json.dumps(contexto_auxiliar, ensure_ascii=False, indent=2)}\n"
        f"[FIM_TEXTO_COLADO]"
    )

    payload = {
        "model": LLM_MODEL,
        "stream": True,
        "messages": [
            {"role": "user", "content": user_content},
        ],
        "browser_profile": BROWSER_PROFILE,
    }
    if chat_url:
        payload["url"] = chat_url
    if chat_id:
        payload["chatid"] = chat_id

    try:
        log.info("  🕸️ Solicitando refinamento de grafo/alertas à LLM na mesma conversa...")
        resp = _post_llm(payload)

        markdown = ""
        last_chat_url = chat_url
        last_chat_id = chat_id
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "markdown":
                markdown = obj.get("content", "")
            elif t == "finish":
                fin = obj.get("content", {}) or {}
                last_chat_url = fin.get("url") or last_chat_url
                last_chat_id = fin.get("chat_id") or last_chat_id
            elif t == "error":
                log.warning(f"  ⚠️ Refinamento auxiliar: LLM retornou erro: {obj.get('content')}")
                return resultado

        if not markdown:
            log.warning("  ⚠️ Refinamento auxiliar: LLM não retornou conteúdo.")
            return resultado

        extra = _parse_json_llm(markdown)
        for chave in ("alertas_clinicos", "grafo_clinico_nodes", "grafo_clinico_edges", "mensagens_acompanhamento"):
            val = extra.get(chave)
            if val not in (None, "", [], {}):
                resultado[chave] = val
        if last_chat_url:
            resultado["_chat_url"] = last_chat_url
        if last_chat_id:
            resultado["_chat_id"] = last_chat_id

        log.info(
            "  🕸️ Refinamento auxiliar concluído: "
            f"nodes={len(_ensure_list_local(resultado.get('grafo_clinico_nodes')))} | "
            f"edges={len(_ensure_list_local(resultado.get('grafo_clinico_edges')))}"
        )
        return resultado

    except Exception as e:
        log.warning(f"  ⚠️ Refinamento auxiliar via LLM falhou: {e}")
        return resultado


def gerar_queries_pesquisa_llm(resultado: dict, chat_url: str = None, chat_id: str = None) -> dict:
    """
    Pede à própria LLM que proponha as queries/tópicos de pesquisa mais úteis
    para o paciente específico analisado.
    """
    contexto_pesquisa = {
        chave: valor
        for chave, valor in {
            "resumo_texto": resultado.get("resumo_texto"),
            "gravidade_clinica": resultado.get("gravidade_clinica"),
            "diagnosticos_citados": resultado.get("diagnosticos_citados"),
            "pontos_chave": resultado.get("pontos_chave"),
            "mudancas_relevantes": resultado.get("mudancas_relevantes"),
            "eventos_comportamentais": resultado.get("eventos_comportamentais"),
            "sinais_nucleares": resultado.get("sinais_nucleares"),
            "medicacoes_em_uso": resultado.get("medicacoes_em_uso"),
            "medicacoes_iniciadas": resultado.get("medicacoes_iniciadas"),
            "medicacoes_suspensas": resultado.get("medicacoes_suspensas"),
            "terapias_referidas": resultado.get("terapias_referidas"),
            "pendencias_clinicas": resultado.get("pendencias_clinicas"),
            "seguimento_retorno_estimado": resultado.get("seguimento_retorno_estimado"),
            "condutas_especificas_sugeridas": resultado.get("condutas_especificas_sugeridas"),
            "condutas_gerais_sugeridas": resultado.get("condutas_gerais_sugeridas"),
        }.items()
        if valor not in (None, "", [], {})
    }

    if not contexto_pesquisa:
        return {"search_queries": [], "uptodate_queries": []}

    user_content = (
        f"[INICIO_TEXTO_COLADO]\n"
        f"{PROMPT_GERAR_PESQUISA}\n\n"
        f"CASO CLÍNICO ESTRUTURADO (JSON):\n"
        f"{json.dumps(contexto_pesquisa, ensure_ascii=False, indent=2)}\n"
        f"[FIM_TEXTO_COLADO]"
    )

    payload = {
        "model": LLM_MODEL,
        "stream": True,
        "messages": [
            {"role": "user", "content": user_content},
        ],
        "browser_profile": BROWSER_PROFILE,
    }

    if chat_url:
        payload["url"] = chat_url
    if chat_id:
        payload["chatid"] = chat_id

    try:
        resp = _post_llm(payload)

        markdown = ""
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "markdown":
                markdown = obj.get("content", "")
            elif t == "error":
                log.warning(f"  ⚠️ Planejamento de pesquisa: LLM retornou erro: {obj.get('content')}")
                return {"search_queries": [], "uptodate_queries": []}

        if not markdown:
            log.warning("  ⚠️ Planejamento de pesquisa: LLM não retornou conteúdo.")
            return {"search_queries": [], "uptodate_queries": []}

        try:
            planejado = _parse_json_llm(markdown)
            itens_search = planejado.get("search_queries") or []
            itens_uptodate = planejado.get("uptodate_queries") or []
        except Exception as parse_err:
            itens_search = _extrair_queries_pesquisa_fallback(markdown)
            itens_uptodate = []
            if itens_search:
                log.info(f"  ℹ️ Planejamento de pesquisa: JSON fora do formato estrito; extração tolerante aplicada ({parse_err}).")
            else:
                preview = re.sub(r"\s+", " ", _strip_code_fences(markdown))[:500]
                log.warning(f"  ⚠️ Planejamento de pesquisa: não foi possível interpretar a resposta da LLM ({parse_err}). Prévia: {preview}")
                return {"search_queries": [], "uptodate_queries": []}

        def _normalizar_lista_queries(itens, label_log):
            queries = []
            vistos = set()
            for item in (itens or [])[:SEARCH_MAX_QUERIES]:
                if not isinstance(item, dict):
                    continue
                query = re.sub(r"\s+", " ", str(item.get("query") or "")).strip()
                reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
                if not query:
                    continue
                key = query.lower()
                if key in vistos:
                    continue
                vistos.add(key)
                queries.append(query)
                log.info(f"     🧠 {label_log}: {query}" + (f" | motivo: {reason}" if reason else ""))
            return queries[:SEARCH_MAX_QUERIES]

        search_queries = _normalizar_lista_queries(itens_search, "search")
        uptodate_queries = _normalizar_lista_queries(itens_uptodate, "uptodate")

        if search_queries or uptodate_queries:
            log.info(
                "  🧠 LLM planejou "
                f"{len(search_queries)} search_queries e {len(uptodate_queries)} uptodate_queries."
            )

        return {
            "search_queries": search_queries,
            "uptodate_queries": uptodate_queries,
        }

    except Exception as e:
        log.warning(f"  ⚠️ Planejamento de pesquisa via LLM falhou: {e}")
        return {"search_queries": [], "uptodate_queries": []}


PROMPT_ENRIQUECIMENTO = """Com base nos resultados de busca em literatura médica fornecidos abaixo, enriqueça as condutas clínicas sugeridas com referências reais.

REGRAS:
1. Use SOMENTE referências que apareçam nos resultados de busca fornecidos.
2. Nunca invente PMIDs, DOIs ou títulos de artigos.
3. Se um resultado de busca contiver um artigo relevante, extraia: título do artigo, URL do PubMed e a conclusão relevante.
4. Associe cada referência a uma conduta clínica concreta.
5. Se nenhum resultado for relevante para uma conduta, não force uma referência — deixe vazia.

Responda SOMENTE com um JSON válido no formato:
{
  "condutas_especificas_sugeridas": [
    {
      "conduta": "descrição da conduta",
      "justificativa": "por que é indicada",
      "referencia": "título do artigo ou diretriz encontrada na busca",
      "fonte": "URL do PubMed ou fonte"
    }
  ],
  "condutas_gerais_sugeridas": [
    {
      "descricao": "descrição da conduta geral",
      "motivo_clinico": "motivo",
      "sinais_alerta": [],
      "orientacao_cuidador": ""
    }
  ]
}
"""


def enriquecer_com_evidencias(resultado: dict, resultados_web: list,
                               chat_url: str = None, chat_id: str = None) -> dict:
    """
    Passo 2 do pipeline agêntico:
    Envia os resultados da busca web para a LLM (no mesmo chat) para
    enriquecer as condutas com referências reais.

    Retorna o resultado original com condutas atualizadas,
    ou o resultado inalterado se algo falhar.
    """
    texto_busca = formatar_resultados_busca(resultados_web)
    if not texto_busca:
        log.info("  🔍 Nenhum resultado de busca útil para enriquecimento.")
        return resultado

    # Monta resumo do caso para contextualizar a LLM
    raw_diags = resultado.get("diagnosticos_citados", [])
    raw_meds  = resultado.get("medicacoes_em_uso", [])
    resumo    = resultado.get("resumo_texto", "")

    # Garante que diags seja lista de strings
    if isinstance(raw_diags, str):
        try:
            raw_diags = json.loads(raw_diags)
        except (json.JSONDecodeError, ValueError):
            raw_diags = [raw_diags] if raw_diags.strip() else []
    if not isinstance(raw_diags, list):
        raw_diags = []
    diags_str = []
    for d in raw_diags:
        if isinstance(d, dict):
            diags_str.append(d.get("nome") or d.get("descricao") or d.get("valor") or str(d))
        else:
            diags_str.append(str(d))

    # Garante que meds seja lista de strings
    if isinstance(raw_meds, str):
        try:
            raw_meds = json.loads(raw_meds)
        except (json.JSONDecodeError, ValueError):
            raw_meds = [raw_meds] if raw_meds.strip() else []
    if not isinstance(raw_meds, list):
        raw_meds = []
    meds_str = []
    for m in raw_meds[:5]:
        if isinstance(m, dict):
            meds_str.append(m.get("nome") or m.get("name") or str(m))
        else:
            meds_str.append(str(m))

    resumo_caso = f"Diagnósticos: {', '.join(diags_str) if diags_str else 'N/I'}\n"
    if meds_str:
        resumo_caso += f"Medicações em uso: {', '.join(meds_str)}\n"
    if resumo:
        resumo_caso += f"Resumo clínico: {str(resumo)[:500]}\n"

    user_content = (
        f"[INICIO_TEXTO_COLADO]\n"
        f"{PROMPT_ENRIQUECIMENTO}\n\n"
        f"══════════════════════════════════════\n"
        f"CASO CLÍNICO\n"
        f"══════════════════════════════════════\n"
        f"{resumo_caso}\n"
        f"══════════════════════════════════════\n"
        f"RESULTADOS DE BUSCA EM LITERATURA MÉDICA\n"
        f"══════════════════════════════════════\n"
        f"{texto_busca}\n"
        f"══════════════════════════════════════\n"
        f"[FIM_TEXTO_COLADO]"
    )

    payload = {
        "model":   LLM_MODEL,
        "stream":  True,
        "messages": [
            {"role": "user", "content": user_content},
        ],
        "browser_profile": BROWSER_PROFILE,
    }

    # Retoma o mesmo chat para ter contexto da análise anterior
    if chat_url:
        payload["url"] = chat_url
    if chat_id:
        payload["chatid"] = chat_id

    try:
        resp = _post_llm(payload)

        new_chat_id = chat_id
        new_chat_url = chat_url
        markdown = ""
        last_status = ""
        inline_active = False

        def _clean_simulator_log_for_local_view(raw: str) -> str:
            msg = str(raw or "").strip()
            if not msg:
                return ""
            if "screenshot stream" in msg.lower():
                return ""
            msg = re.sub(r"^\s*Remetente:\s*[^|]+\|\s*", "", msg, flags=re.IGNORECASE)
            msg = re.sub(r"(\[browser\.py\])\s+\[[^\]]+\]\s+", r"\1 ", msg, flags=re.IGNORECASE)
            return msg.strip()

        def _inline(msg):
            largura_terminal = shutil.get_terminal_size((140, 20)).columns
            largura_util = max(30, largura_terminal - 2)
            linha = f"  {str(msg or '')}"
            sys.stdout.write('\r' + linha.ljust(largura_util))
            sys.stdout.flush()

        def _newline():
            sys.stdout.write('\n')
            sys.stdout.flush()

        def _inline_status(prefixo: str, msg: str):
            texto = re.sub(r"\s+", " ", str(msg or "")).strip()
            texto = re.sub(r"^\s*Remetente:\s*[^|]+\|\s*", "", texto, flags=re.IGNORECASE)
            cooldown_match = re.search(r"nova tentativa em\s*([0-9]{1,2}:[0-9]{2})", texto, flags=re.IGNORECASE)
            if cooldown_match:
                texto = f"Aguardando cooldown do ChatGPT | nova tentativa em {cooldown_match.group(1)}"
            if not texto:
                return

            largura_terminal = shutil.get_terminal_size((140, 20)).columns
            largura_util = max(30, largura_terminal - 6)
            mensagem = f"{prefixo} {texto}"
            if len(mensagem) > largura_util:
                mensagem = mensagem[:max(0, largura_util - 3)].rstrip() + "..."
            _inline(mensagem)

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "status":
                msg = obj.get("content", "")
                phase = str(obj.get("phase") or "").strip().lower()
                wait_seconds = obj.get("wait_seconds")
                if phase == "chat_rate_limit_cooldown" and wait_seconds is not None:
                    try:
                        wait_seconds = max(0, int(round(float(wait_seconds))))
                        mm, ss = divmod(wait_seconds, 60)
                        msg = f"Aguardando cooldown do ChatGPT | nova tentativa em {mm:02d}:{ss:02d}"
                    except Exception:
                        pass
                if msg == last_status:
                    continue
                last_status = msg
                _inline_status('⏳', msg)
                inline_active = True
            elif t == "log":
                if inline_active:
                    _newline()
                    inline_active = False
                cleaned_log = _clean_simulator_log_for_local_view(obj.get("content", ""))
                if cleaned_log:
                    log.info(f"  🔧 {cleaned_log}")
            elif t == "chatid":
                if inline_active:
                    _newline()
                    inline_active = False
                new_chat_id = obj.get("content") or new_chat_id
                log.info(f"  📎 chat_id: {new_chat_id}")
            elif t == "markdown":
                       markdown = obj.get("content", "")
                       markdown_visivel = _extrair_markdown_visivel_llm(markdown)
                       if markdown_visivel:
                           _inline_status('📝', f"Recebendo: {len(markdown_visivel)} chars...")
                       else:
                           _inline_status('⏳', "Pensando...")
                       inline_active = True
            elif t == "finish":
                if inline_active:
                    _newline()
                    inline_active = False
                fin = obj.get("content", {})
                new_chat_url = fin.get("url") or new_chat_url
                new_chat_id = fin.get("chat_id") or new_chat_id
                if not new_chat_id and new_chat_url:
                    new_chat_id = new_chat_url.rstrip('/').split('/')[-1] or new_chat_id
                log.info(f"  🔗 chat_url: {new_chat_url} | chat_id: {new_chat_id}")
            elif t == "error":
                if inline_active:
                    _newline()
                    inline_active = False
                log.warning(f"  ⚠️ Enriquecimento: LLM retornou erro: {obj.get('content')}")
                return resultado

        if inline_active:
            _newline()

        if not markdown:
            log.warning("  ⚠️ Enriquecimento: LLM não retornou conteúdo.")
            return resultado

        try:
            enriquecido = _parse_json_llm(markdown)
        except Exception as parse_err:
            path_md, path_meta, meta = _salvar_debug_json_falha(
                resultado.get('_id_atendimento'),
                "enriquecimento_evidencias",
                markdown,
                parse_err,
            )
            preview = re.sub(r"\s+", " ", _strip_code_fences(markdown))[:500]
            log.warning(
                f"  ⚠️ Enriquecimento: LLM não retornou JSON válido ({parse_err}). "
                f"incompleto={meta['json_parece_incompleto']} | debug: {path_md} | {path_meta} | Prévia: {preview}"
            )
            return resultado

        # Merge: substitui condutas apenas se a LLM retornou algo não-vazio
        condutas_esp = enriquecido.get("condutas_especificas_sugeridas")
        condutas_ger = enriquecido.get("condutas_gerais_sugeridas")

        if condutas_esp and isinstance(condutas_esp, list) and len(condutas_esp) > 0:
            resultado["condutas_especificas_sugeridas"] = condutas_esp
            log.info(f"  📚 {len(condutas_esp)} conduta(s) específica(s) enriquecida(s) com referências")

        if condutas_ger and isinstance(condutas_ger, list) and len(condutas_ger) > 0:
            resultado["condutas_gerais_sugeridas"] = condutas_ger
            log.info(f"  📚 {len(condutas_ger)} conduta(s) geral(is) enriquecida(s)")

        return resultado

    except Exception as e:
        log.warning(f"  ⚠️ Enriquecimento com evidências falhou: {e}")
        return resultado


def executar_busca_evidencias(resultado: dict, chat_url: str = None, chat_id: str = None) -> dict:
    """
    Pipeline completo de busca + enriquecimento:
      1. Pedir à LLM um plano estruturado com queries de UpToDate e web
      2. Priorizar UpToDate para temas clínicos, com fallback para web search
      3. Enviar os resultados encontrados para a LLM enriquecer condutas
    """
    if not SEARCH_HABILITADA:
        return resultado

    def _resumo_resultados(label: str, resultados: list) -> tuple:
        total_items = sum(
            len(r.get("results", [])) for r in resultados if r.get("success", True)
        )
        tem_html = any(r.get("raw_html") for r in resultados if r.get("success", True))
        log.info(
            f"  {label} {total_items} resultado(s) estruturado(s) | HTML bruto: {'sim' if tem_html else 'não'}"
        )
        return total_items, tem_html

    plano_pesquisa = gerar_queries_pesquisa_llm(resultado, chat_url=chat_url, chat_id=chat_id) or {}
    search_queries = list(plano_pesquisa.get("search_queries") or [])
    uptodate_queries = list(plano_pesquisa.get("uptodate_queries") or [])

    if not search_queries and not uptodate_queries:
        log.info("  🔄 Fallback: usando extração heurística de termos para montar as queries de web.")
        search_queries = extrair_termos_busca(resultado)

    if not search_queries and not uptodate_queries:
        log.info("  🔍 Nenhum termo clínico para busca — pulando enriquecimento.")
        return resultado

    resultados_busca = []

    if uptodate_queries:
        log.info(f"  🩺 Busca UpToDate prioritária: {len(uptodate_queries)} query(s)")
        for q in uptodate_queries:
            log.info(f"     🩺 {q}")

        resultados_uptodate = buscar_uptodate(uptodate_queries)
        total_items, tem_html = _resumo_resultados("🩺 UpToDate:", resultados_uptodate)

        if total_items > 0 or tem_html:
            resultados_busca.extend(resultados_uptodate)
        else:
            log.info("  🔄 UpToDate sem resultados úteis — acionando fallback para web search.")
            if not search_queries:
                search_queries = uptodate_queries[:SEARCH_MAX_QUERIES]

    if search_queries and not resultados_busca:
        log.info(f"  🌐 Busca web: {len(search_queries)} query(s)")
        for q in search_queries:
            log.info(f"     🔎 {q}")

        resultados_web = buscar_web(search_queries)
        total_items, tem_html = _resumo_resultados("🌐 Web:", resultados_web)
        if total_items > 0 or tem_html:
            resultados_busca.extend(resultados_web)

    if not resultados_busca:
        log.info("  🔍 Nenhum resultado — pulando enriquecimento.")
        return resultado

    log.info("  📚 Enviando resultados para LLM enriquecer condutas...")
    resultado = enriquecer_com_evidencias(resultado, resultados_busca, chat_url, chat_id)

    return resultado


# ─────────────────────────────────────────────────────────────
# LOOP PRINCIPAL
# ─────────────────────────────────────────────────────────────

# Intervalo anti-rate-limit agora é aplicado centralmente pelo servidor
# (`server.py::_wait_python_request_interval_if_needed`) para TODO pedido
# Python (não apenas analisador), com o intervalo dividido pela quantidade
# de perfis Chromium ativos em `config.CHROMIUM_PROFILES`. Os valores base
# continuam em `ANALISADOR_PAUSA_MIN/MAX` (config.py). Esta função virou
# um no-op mantido por compatibilidade histórica com os call-sites locais.
PAUSA_MIN = _cfg("ANALISADOR_PAUSA_MIN", 25)
PAUSA_MAX = _cfg("ANALISADOR_PAUSA_MAX", 60)


def _aguardar_intervalo_entre_analises(contexto: str = "próxima análise"):
    """
    No-op: o intervalo anti-rate-limit entre pedidos Python ao ChatGPT
    Simulator passou a ser enforçado no próprio servidor (`server.py`),
    aplicado a qualquer request_source Python e dividido pela quantidade
    de perfis ChatGPT ativos em `config.CHROMIUM_PROFILES`.
    """
    return

def processar_lote(pendentes: list):
    total = len(pendentes)
    pacientes_verificados = set()   # evita checar o mesmo paciente várias vezes no lote

    for i, row in enumerate(pendentes):
        idat            = row["id"]
        texto           = strip_html(row.get("consulta_conteudo") or "")
        dtp             = row.get("datetime_prontuario_atual", "")
        chat_id_prev    = row.get("chat_id_anterior")  or None
        chat_url_prev   = row.get("chat_url_anterior") or None

        eh_reanalise = bool(chat_url_prev)
        prefixo      = "♻️  Reanálise" if eh_reanalise else "▶"
        log.info(f"{prefixo} ID={idat} | Paciente={row['id_paciente']} | {len(texto)} chars | prontuário: {dtp}")
        if eh_reanalise:
            log.info(f"   ↩️  Retomando chat anterior: {chat_url_prev}")

        # Enfileira atendimentos antigos do mesmo paciente (1x por paciente por lote)
        id_pac = str(row.get("id_paciente", ""))
        if id_pac and id_pac not in pacientes_verificados:
            pacientes_verificados.add(id_pac)
            try:
                enfileirar_atendimentos_antigos(id_pac)
            except Exception as e:
                log.warning(f"  ⚠️ Erro ao enfileirar atendimentos antigos: {e}")

        marcar_processando(row)

        if len(texto) < MIN_CHARS:
            id_paciente = str(row.get("id_paciente") or "")
            dt_evolucao = str(
                row.get("datetime_consulta_inicio")
                or row.get("datetime_atendimento_inicio")
                or row.get("datetime_prontuario_atual")
                or ""
            ).strip()
            maior_resumo = buscar_maior_resumo_texto_paciente(id_paciente, idat)
            resumo_fallback = _montar_resumo_fallback(maior_resumo, dt_evolucao, texto)

            salvar_resultado(idat, {
                "resumo_texto": resumo_fallback,
                "observacoes_gerais": "Registro curto: fallback sem chamada ao ChatGPT Simulator.",
                "pontos_chave": [],
                "condutas_sugeridas": [],
            })
            log.warning(
                f"  ID={idat} sem conteúdo suficiente ({len(texto)} chars). "
                f"Análise LLM pulada; resumo_fallback salvo ({len(resumo_fallback)} chars)."
            )
            continue

        # Busca contexto clínico (paciente, profissional, hospital)
        try:
            contexto = buscar_contexto_clinico(row)
            if contexto:
                log.info(f"  📋 Contexto clínico recuperado ({len(contexto)} chars)")
        except Exception as e:
            contexto = ""
            log.warning(f"  ⚠️ Falha ao buscar contexto clínico: {e}")

        try:
            _aguardar_intervalo_entre_analises(f"ID={idat}")
            resultado = analisar_prontuario(texto, chat_url=chat_url_prev, chat_id=chat_id_prev, contexto=contexto,  id_atendimento=idat)

            # Passo 2: Busca web + enriquecimento de condutas com evidências
            try:
                chat_url_atual = resultado.get("_chat_url") or chat_url_prev
                chat_id_atual  = resultado.get("_chat_id")  or chat_id_prev
                resultado = executar_busca_evidencias(resultado, chat_url=chat_url_atual, chat_id=chat_id_atual)
            except Exception as e:
                log.warning(f"  ⚠️ Enriquecimento com evidências falhou (não fatal): {e}")

            try:
                if _grafo_clinico_esta_generico(resultado):
                    chat_url_aux = resultado.get("_chat_url") or chat_url_prev
                    chat_id_aux  = resultado.get("_chat_id") or chat_id_prev
                    resultado = gerar_dados_auxiliares_llm(resultado, chat_url=chat_url_aux, chat_id=chat_id_aux)
            except Exception as e:
                log.warning(f"  ⚠️ Refinamento auxiliar do grafo falhou (não fatal): {e}")

            salvar_resultado(idat, resultado)
            log.info(
                f"  ✅ ID={idat} concluído | "
                f"{len(resultado.get('pontos_chave', []))} pontos-chave | "
                f"{len(resultado.get('condutas_sugeridas', []))} condutas"
            )

            # Limpa dados antigos das tabelas complementares antes de re-inserir
            try:
                limpar_tabelas_complementares(idat)
            except Exception as e:
                log.warning(f"  ⚠️ Limpeza de tabelas complementares falhou (não fatal): {e}")

            # Tabelas auxiliares (alertas, grafo, casos) via endpoint PHP com charset correto
            try:
                salvar_auxiliar(idat, row["id_paciente"], resultado)
            except Exception as e:
                log.warning(f"  ⚠️ Tabelas auxiliares falharam (não fatal): {e}")

            # Pipeline de embeddings + similaridade (etapas 6-8 do CDSS)
            try:
                hash_pront = resultado.get("hash_prontuario") or hashlib.sha256(texto.encode('utf-8', errors='ignore')).hexdigest()
                executar_pipeline_embedding(idat, row["id_paciente"], texto, hash_pront)
            except Exception as e:
                log.warning(f"  ⚠️ Embedding/similaridade falhou (não fatal): {e}")

            try:
                atualizar_analise_compilada_paciente(row["id_paciente"])
            except Exception as e:
                log.warning(f"  ⚠️ Síntese compilada do paciente falhou (não fatal): {e}")

        except ChatGPTRateLimitError as rl:
            salvar_erro(idat, str(rl))
            espera = LLM_RATE_LIMIT_RETRY_BASE_S
            log.warning(
                f"  🚫 ID={idat} rate limit detectado: {rl}\n"
                f"     Aguardando {espera}s antes de continuar o lote..."
            )
            if espera <= 0:
                continue
            try:
                countdown(espera, "cooldown rate limit")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                time.sleep(espera)

        except Exception as e:
            salvar_erro(idat, str(e))
            log.error(f"  ❌ ID={idat} erro: {e}")

        # intervalo anti-rate-limit agora é aplicado no server.py para todo
        # pedido Python (dividido pelo número de perfis ChatGPT ativos).


def atualizar_analise_compilada_paciente(id_paciente: str):
    """
    Versão final: bloqueia síntese compilada apenas por atendimentos realmente pendentes
    e segue com compilação quando há somente erros, informando tais falhas no contexto.
    """
    enfileirados = enfileirar_atendimentos_antigos(id_paciente)
    pendentes = contar_atendimentos_nao_concluidos_paciente(id_paciente)
    erros = listar_atendimentos_com_erro_paciente(id_paciente)

    if pendentes > 0:
        try:
            garantir_registro_compilado_paciente_pendente(id_paciente)
        except Exception as e:
            log.warning(
                f"  ⚠️ Falha ao garantir registro pendente da síntese compilada do paciente {id_paciente}: {e}"
            )
        log.info(
            f"⏳ Síntese compilada adiada para paciente {id_paciente}: "
            f"{pendentes} atendimento(s) ainda não concluído(s)"
            + (f" ({enfileirados} recém-enfileirado(s))" if enfileirados else "")
            + "."
        )
        return

    if erros:
        amostra = []
        for e in erros[:3]:
            detalhe = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(detalhe) > 180:
                detalhe = detalhe[:180] + "..."
            amostra.append(
                f"id_atendimento={e.get('id_atendimento')} (tentativas={e.get('tentativas')}): {detalhe or 'sem detalhe'}"
            )
        log.warning(
            f"  ⚠️ Paciente {id_paciente} possui {len(erros)} atendimento(s) com erro de análise. "
            f"A síntese compilada seguirá com os concluídos e levará contexto das falhas. "
            f"Amostra: {' | '.join(amostra)}"
        )

    texto_compilado, row_base = montar_texto_compilado_paciente(id_paciente)
    if not texto_compilado or not row_base:
        log.info(f"ℹ️  Sem histórico suficiente para compilar síntese do paciente {id_paciente}.")
        return

    if erros:
        linhas_erros = []
        for e in erros[:20]:
            msg = re.sub(r"\s+", " ", str(e.get("erro_msg") or "")).strip()
            if len(msg) > 600:
                msg = msg[:600] + "..."
            linhas_erros.append(
                f"- id_atendimento={e.get('id_atendimento')}, tentativas={e.get('tentativas')}, erro={msg or 'sem detalhe'}"
            )
        texto_compilado += (
            "\n\n" + ("=" * 70) + "\n\n"
            "ATENDIMENTOS COM FALHA DE ANÁLISE (não concluídos)\n"
            "Use estas falhas apenas como contexto operacional. Não invente dados clínicos ausentes.\n"
            + "\n".join(linhas_erros)
        )

    id_registro_compilado = garantir_registro_compilado_paciente_pendente(id_paciente)
    sql_exec(f"""
        UPDATE {TABELA} SET
            status = 'processando',
            tentativas = tentativas + 1,
            erro_msg = NULL,
            datetime_analise_iniciada = NOW(),
            datetime_analise_concluida = NULL,
            datetime_ultima_atualizacao_atendimento = NULL
        WHERE id = {id_registro_compilado}
    """, reason="marcar_registro_compilado_processando")

    log.info(f"🧬 Atualizando síntese compilada do paciente {id_paciente}...")
    contexto = ""
    try:
        contexto = buscar_contexto_clinico({
            "id_paciente": id_paciente,
            "id": row_base.get("id_atendimento"),
            "id_criador": row_base.get("id_criador"),
        }) or ""
    except Exception as e:
        log.warning(f"  ⚠️ Falha ao buscar contexto do paciente compilado {id_paciente}: {e}")

    _aguardar_intervalo_entre_analises(f"síntese compilada paciente {id_paciente}")
    resultado = analisar_prontuario(texto_compilado[:18000], contexto=contexto)
    try:
        resultado = executar_busca_evidencias(
            resultado,
            chat_url=resultado.get("_chat_url"),
            chat_id=resultado.get("_chat_id"),
        )
    except Exception as e:
        log.warning(f"  ⚠️ Enriquecimento da síntese compilada falhou (não fatal): {e}")

    salvar_resultado_compilado_paciente(id_paciente, resultado)
    log.info(f"✅ Síntese compilada do paciente {id_paciente} atualizada.")


def countdown(segundos: int, motivo: str = "próximo ciclo"):
    """
    Exibe contagem regressiva inline no CMD usando \\r (sobrescreve a mesma linha).
    Não polui o log em arquivo — escreve direto no sys.stdout.
    NUNCA levanta exceção (exceto KeyboardInterrupt) — não pode matar o daemon.
    """
    try:
        for restante in range(segundos, 0, -1):
            try:
                sys.stdout.write(f"\r   ⏳ {motivo} em {restante:2d}s...   ")
                sys.stdout.flush()
            except Exception:
                pass  # stdout pode falhar em pipes/redirects — ignorar
            time.sleep(1)
        try:
            sys.stdout.write(f"\r{' ' * 50}\r")  # limpa a linha antes do próximo ciclo
            sys.stdout.flush()
        except Exception:
            pass
    except KeyboardInterrupt:
        try:
            sys.stdout.write("\n")
        except Exception:
            pass
        raise

def _em_horario_util() -> bool:
    """Retorna True se estamos em horário útil (seg-sex, HORARIO_UTIL_INICIO até HORARIO_UTIL_FIM)."""
    from datetime import datetime
    agora = datetime.now()
    dia_semana = agora.weekday()  # 0=seg … 6=dom
    return dia_semana < 5 and HORARIO_UTIL_INICIO <= agora.hour < HORARIO_UTIL_FIM

def main():
    log.info("🩺 Analisador de Prontuários iniciado.")
    log.info(f"   PHP    : {PHP_URL}")
    log.info(f"   LLM    : {LLM_URL}")
    log.info(f"   Tabela : {TABELA}")
    log.info(f"   Intervalo: {POLL_INTERVAL}s | Batch: {BATCH_SIZE} | Max retries: {MAX_TENTATIVAS}")

    aguardar_llm_startup()
    garantir_tabela()
    garantir_coluna_dados_json()                 # garante dados_json em tabelas pré-existentes
    garantir_coluna_mensagens_acompanhamento()   # garante mensagens_acompanhamento em tabelas pré-existentes
    garantir_coluna_datetime_analise_iniciada()  # garante datetime_analise_iniciada em tabelas pré-existentes
    garantir_colunas_v16()                       # garante colunas CDSS/RAG da V16 (modelo_llm, hash_prontuario, score_risco, etc.)
    garantir_schema_analise_compilada_paciente() # permite síntese longitudinal por id_paciente sem id_atendimento
    garantir_migracoes()                         # corrige tipos de colunas em tabelas pré-existentes
    garantir_tabela_embeddings()                 # garante tabelas de embeddings + casos semelhantes
    resetar_analises_interrompidas_no_startup()  # limpa inícios de análise sem conclusão válida após quedas/interrupções
    _reativar_erros_conexao_no_startup()         # erro de conexão vira pendente novamente (respeitando MAX_TENTATIVAS)
    _reativar_esgotados_recuperaveis_no_startup() # recoloca erros recuperáveis em pendente para nova tentativa
    corrigir_erros_texto_insuficiente_no_startup() # converte erros legados de texto insuficiente em resumo_fallback concluído

    llm_estava_fora = False   # rastreia se houve queda para logar a reconexão
    ciclo = 0
    while True:
        ciclo += 1
        try:
            # ── Verificação de saúde da LLM a cada ciclo ──────────────
            if not verificar_llm():
                if not llm_estava_fora:
                    log.warning(
                        f"⚠️  ChatGPT Simulator não está respondendo ({LLM_URL}).\n"
                        f"       O analisador continuará verificando a cada {POLL_INTERVAL}s.\n"
                        f"       Inicie o ChatGPT Simulator para retomar as análises."
                    )
                    llm_estava_fora = True
                else:
                    log.info(f"   🔄 ChatGPT Simulator ainda fora. Tentando novamente em {POLL_INTERVAL}s...")
                try:
                    countdown(POLL_INTERVAL, "reconexão com ChatGPT Simulator")
                except KeyboardInterrupt:
                    raise
                except Exception:
                    time.sleep(POLL_INTERVAL)
                continue

            # Se estava fora e voltou, loga a reconexão
            if llm_estava_fora:
                log.info("✅ ChatGPT Simulator reconectado! Retomando análises.")
                llm_estava_fora = False

            # ── Filtro de horário útil ────────────────────────────────
            # Evita consumir o limite de mensagens do ChatGPT Plus
            # durante o expediente, quando o usuário humano pode
            # precisar da interface.
            if FILTRO_HORARIO_UTIL_ATIVO and _em_horario_util():
                from datetime import datetime
                prox = datetime.now().replace(hour=HORARIO_UTIL_FIM, minute=0, second=0)
                restante = int((prox - datetime.now()).total_seconds())
                restante_min = max(restante, 0) // 60
                log.info(
                    f"   🏢 Horário útil ({HORARIO_UTIL_INICIO:02d}:00–{HORARIO_UTIL_FIM:02d}:00). "
                    f"Analisador em espera (~{restante_min} min restantes). "
                    f"Motivo: preservar limite de mensagens do ChatGPT Plus para uso humano."
                )
                try:
                    # Reavalia a cada 5 minutos para não bloquear por muito tempo
                    countdown(min(300, max(restante, POLL_INTERVAL)), "fim do horário útil")
                except KeyboardInterrupt:
                    raise
                except Exception:
                    time.sleep(min(300, max(restante, POLL_INTERVAL)))
                continue

            # ── Ciclo normal ──────────────────────────────────────────
            resetar_travados()
            resultado = buscar_pendentes()
            pendentes = resultado["pendentes"]
            compiladas_pendentes = resultado.get("compiladas_pendentes", [])

            log.info(f"── Ciclo #{ciclo} {'─' * 50}")
            log.info(f"   📊 Prontuários na fila : {resultado['total_tabela']}")
            log.info(f"   🧬 Análises compiladas   : {resultado['total_analises_compiladas_paciente']}")
            log.info(f"   ✅ Concluídos/atualizados : {resultado['total_concluidos']}")
            log.info(f"   🕐 Aguardando análise     : {resultado['total_pendentes']}")
            log.info(f"   🧬 Sínteses compiladas pendentes: {resultado['total_compiladas_pendentes']}")
            log.info(f"   🔄 Em processamento       : {resultado['total_processando']}")
            log.info(f"   🔁 Prontuários editados   : {resultado['total_desatualizados']}  (reanálise pendente)")
            log.info(f"   ❌ Com erro (c/ retentativa): {resultado['total_erros']}")
            log.info(f"   🚫 Esgotados (sem retentativa): {resultado['total_esgotados']}")
            for item in resultado.get("motivos_esgotados", []):
                log.info(f"      ↳ {item['total']}x motivo: {item['motivo']}")
            if compiladas_pendentes:
                log.info(f"   🧬 Sínteses compiladas pendentes: {len(compiladas_pendentes)}")
            else:
                log.info("   ℹ️ Nenhuma síntese compilada pendente.")

            if pendentes:
                log.info(f"   ▶  {len(pendentes)} prontuário(s) serão processados agora.")
                processar_lote(pendentes)
            elif compiladas_pendentes:
                log.info(f"   🧬 Nenhum prontuário individual pendente. Processando {len(compiladas_pendentes)} síntese(s) compilada(s)...")
                for comp in compiladas_pendentes:
                    id_pac = comp.get("id_paciente")
                    if not id_pac:
                        continue
                    try:
                        atualizar_analise_compilada_paciente(str(id_pac))
                    except ChatGPTRateLimitError as rl:
                        espera = LLM_RATE_LIMIT_RETRY_BASE_S
                        log.warning(
                            f"  🚫 Síntese compilada do paciente {id_pac} — rate limit: {rl}\n"
                            f"     Aguardando {espera}s antes de continuar..."
                        )
                        if espera <= 0:
                            continue
                        try:
                            countdown(espera, "cooldown rate limit")
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except Exception:
                            time.sleep(espera)
                    except Exception as e:
                        log.warning(f"  ⚠️ Síntese compilada do paciente {id_pac} falhou: {e}")
            else:
                log.info(f"   💤 Nenhum prontuário pendente. Próxima verificação em {POLL_INTERVAL}s.")





        except KeyboardInterrupt:
            raise  # Ctrl+C deve encerrar
        except Exception as e:
            log.error(f"🚨 Erro no loop: {e}")

        # Countdown protegido — NUNCA pode matar o daemon
        try:
            countdown(POLL_INTERVAL, "próximo ciclo")
        except KeyboardInterrupt:
            raise
        except Exception:
            # Se countdown falhar (stdout quebrado, etc.), aguarda sem display
            try:
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                raise
            except Exception:
                pass


if __name__ == "__main__":
    _terminate_previous_same_server_instances("analisador_prontuarios.py")
    while True:
        try:
            main()
        except KeyboardInterrupt:
            log.info("\n👋 Analisador encerrado pelo usuário (Ctrl+C).")
            break
        except SystemExit as e:
            # O analisador é daemon resiliente: não deve morrer por SystemExit
            # transitório (ex.: falha de conexão/ambiente). Só encerra com Ctrl+C.
            log.warning(f"⚠️ SystemExit capturado ({e}); mantendo daemon ativo.")
            log.info("🔄 Reiniciando main() em 30 segundos...")
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                log.info("\n👋 Analisador encerrado pelo usuário (Ctrl+C).")
                break
        except Exception as e:
            # Último recurso: NUNCA deixar o processo morrer
            log.critical(f"💀 Erro fatal inesperado: {e}")
            log.info("🔄 Reiniciando main() em 30 segundos...")
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                log.info("\n👋 Analisador encerrado pelo usuário (Ctrl+C).")
                break
