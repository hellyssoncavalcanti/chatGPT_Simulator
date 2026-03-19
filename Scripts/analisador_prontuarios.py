#!/usr/bin/env python3
"""
analisador_prontuarios.py
Daemon de análise clínica via LLM.
- Roda na mesma máquina do ChatGPT Simulator (localhost:3003)
- Acessa o banco EXCLUSIVAMENTE via ?action=apiexec do PHP
- Auto-instala dependências faltantes no startup
"""

# ─────────────────────────────────────────────────────────────
# AUTO-INSTALAÇÃO DE DEPENDÊNCIAS
# ─────────────────────────────────────────────────────────────
import sys, subprocess
import random

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
import time, json, logging, re, html as html_mod, requests, hashlib, shutil, textwrap
from html.parser import HTMLParser
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────────────────────
PHP_URL   = "https://conexaovida.org/scripts/js/chatgpt_integracao_criado_pelo_gemini.js.php"
API_KEY       = "CVAPI_2b9c80c2abf94a76baf8b3e68d89cb7e"  # ← underscore após CVAPI

LLM_URL   = "http://127.0.0.1:3003/v1/chat/completions"
LLM_MODEL      = "ChatGPT Simulator"
PROMPT_VERSION = "v16.1"  # v16.1: + busca web + enriquecimento de condutas com evidências

TABELA         = "chatgpt_atendimentos_analise"
POLL_INTERVAL  = 30
MAX_TENTATIVAS = 3
MIN_CHARS      = 80
BATCH_SIZE     = 10

TIMEOUT_PROCESSANDO_MIN = 15  # minutos antes de considerar travado

# Sentence-Transformers / Embeddings
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"    # modelo leve, 384 dim
SIMILARIDADE_TOP_K   = 5                       # quantos casos semelhantes retornar
SIMILARIDADE_MIN     = 0.40                    # score mínimo p/ considerar semelhante

# Busca Web (enriquecimento de condutas com evidências)
SEARCH_URL           = "http://127.0.0.1:3003/api/web_search"  # endpoint local do Simulator
SEARCH_MAX_QUERIES   = 3                       # máximo de queries por prontuário
SEARCH_TIMEOUT       = 90                      # timeout por chamada (o browser precisa digitar)
SEARCH_HABILITADA    = True                    # False para desabilitar sem remover código

# Endpoints PHP:
# - execute_sql: SELECT/SHOW/DESCRIBE (sem rate limiting com api_key valida)
# - api_exec:    CREATE/ALTER/INSERT/UPDATE/DELETE
PHP_ACTION_READ  = "execute_sql"
PHP_ACTION_WRITE = "api_exec"
PHP_KEY_FIELD    = "api_key"

# Nome do log com timestamp do momento de inicialização
_log_ts   = datetime.now().strftime("%H_%M_%S-%d_%m_%Y")
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



# ─────────────────────────────────────────────────────────────
# CAMADA HTTP → PHP (único ponto de acesso ao banco)
# ─────────────────────────────────────────────────────────────
_WRITE_CMDS = {'CREATE', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'DROP', 'TRUNCATE', 'REPLACE'}

def sql_exec(query: str, reason: str = "analisador_prontuarios") -> dict:
    """Roteia automaticamente:
    - SELECT/SHOW/DESCRIBE -> execute_sql (api_key no header+payload, sem rate limiting)
    - CREATE/ALTER/INSERT/UPDATE/DELETE -> api_exec
    """
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
                id_atendimento                          INT(10) NULL
                                                        COMMENT 'FK para clinica_atendimentos.id. NULL = sintese compilada do paciente.',
                datetime_atendimento_inicio             DATETIME NULL
                                                        COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.',
                datetime_ultima_atualizacao_atendimento DATETIME NULL
                                                        COMMENT 'COALESCE(datetime_atualizacao, datetime_consulta_fim) no momento da analise.',
                id_paciente                             VARCHAR(800) NOT NULL
                                                        COMMENT 'FK para membros.id do paciente.',
                id_criador                              VARCHAR(10) NULL
                                                        COMMENT 'FK para membros.id do profissional criador. NULL = sintese compilada do paciente.',
                datetime_analise_criacao                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                        COMMENT 'Data/hora de insercao do registro.',
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
                                                        COMMENT 'Hash SHA-256 do conteudo bruto do prontuario. Detecta alteracoes e evita reprocessamento desnecessario.',
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
                INDEX  idx_hash        (hash_prontuario),
                UNIQUE KEY uq_atendimento (id_atendimento)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
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
        """)
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
        """)
        log.info("✅ Coluna mensagens_acompanhamento verificada/criada.")
    except RuntimeError as e:
        log.warning(f"⚠️  garantir_coluna_mensagens_acompanhamento: {e}")
    finally:
        global _COLUNAS_TABELA
        _COLUNAS_TABELA = None   # invalida cache para incluir a coluna recem-garantida


def garantir_colunas_v16():
    """
    Garante as novas colunas da versao V16 do schema (CDSS/RAG).
    Seguro executar multiplas vezes -- IF NOT EXISTS evita erro se ja existir.
    """
    global _COLUNAS_TABELA
    colunas_v16 = [
        ("modelo_llm",        "VARCHAR(100) NULL", "Modelo LLM utilizado para gerar esta analise."),
        ("prompt_version",    "VARCHAR(30) NULL",  "Versao do prompt clinico utilizado."),
        ("hash_prontuario",   "CHAR(64) NULL",     "Hash SHA-256 do conteudo bruto do prontuario."),
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
            """)
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
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento INT(10) NULL COMMENT 'FK para clinica_atendimentos.id. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador VARCHAR(10) NULL COMMENT 'FK para membros.id do profissional criador. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql)
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
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento INT(10) NULL COMMENT 'FK para clinica_atendimentos.id. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador VARCHAR(10) NULL COMMENT 'FK para membros.id do profissional criador. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql)
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
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento INT(10) NULL COMMENT 'FK para clinica_atendimentos.id. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador VARCHAR(10) NULL COMMENT 'FK para membros.id do profissional criador. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql)
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
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento INT(10) NULL COMMENT 'FK para clinica_atendimentos.id. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador VARCHAR(10) NULL COMMENT 'FK para membros.id do profissional criador. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql)
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
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_atendimento INT(10) NULL COMMENT 'FK para clinica_atendimentos.id. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN datetime_atendimento_inicio DATETIME NULL COMMENT 'Data/hora de inicio do atendimento clinico. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} MODIFY COLUMN id_criador VARCHAR(10) NULL COMMENT 'FK para membros.id do profissional criador. NULL = sintese compilada do paciente.'",
        f"ALTER TABLE {TABELA} ADD INDEX idx_paciente (id_paciente)",
    ]
    for sql in ajustes:
        try:
            sql_exec(sql)
        except RuntimeError as e:
            msg = str(e).lower()
            if 'duplicate key name' in msg or 'already exists' in msg:
                continue
            log.warning(f"⚠️  garantir_schema_analise_compilada_paciente: {e}")

    global _COLUNAS_TABELA
    _COLUNAS_TABELA = None


def garantir_migracoes():
    """
    Aplica migrações de schema em tabelas pré-existentes.
    Seguro executar múltiplas vezes — MODIFY COLUMN é idempotente no MySQL.
    Invalida o cache de colunas ao final para refletir as mudanças.

    Migrações incluídas:
      • seguimento_retorno_estimado  VARCHAR(100) → LONGTEXT
        Motivo: o campo recebe o objeto JSON completo de seguimento, que pode
        facilmente ultrapassar os 100 caracteres do tipo original.
    """
    migracoes = [
        (
            "seguimento_retorno_estimado VARCHAR(100) → LONGTEXT",
            f"""
            ALTER TABLE {TABELA}
            MODIFY COLUMN seguimento_retorno_estimado LONGTEXT NULL
            COMMENT 'JSON completo do objeto seguimento_retorno_estimado retornado pelo LLM.'
            """
        ),
    ]

    for descricao, sql in migracoes:
        try:
            sql_exec(sql)
            log.info(f"✅ Migração aplicada: {descricao}")
        except RuntimeError as e:
            log.warning(f"⚠️  Migração '{descricao}': {e}")
        except Exception as e:
            log.warning(f"⚠️  Migração '{descricao}' (erro inesperado): {e}")

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
                UNIQUE KEY uq_atendimento (id_atendimento),
                INDEX idx_paciente (id_paciente),
                INDEX idx_hash     (hash_prontuario)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
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
        """)
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
    """)


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
            """)
        except Exception as e:
            log.warning(f"  ⚠️ Erro ao salvar caso semelhante (dest={id_dest}): {e}")

    # 2. Atualiza coluna JSON na tabela principal (para consumo pelo PHP/frontend)
    try:
        casos_json = json.dumps(casos, ensure_ascii=False)
        sql_exec(f"""
            UPDATE {TABELA} SET
                casos_semelhantes = _utf8mb4'{esc(casos_json)}'
            WHERE id_atendimento = {int(id_atendimento)}
        """)
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
        """)
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



def buscar_pendentes() -> dict:
    stats = sql_exec(f"""
        SELECT
            COUNT(*)                                                        AS total_tabela,
            SUM(la.status = 'concluido'
                AND NOT (
                    COALESCE(
                        NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                        NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
                    ) > la.datetime_analise_concluida
                ))                                                          AS total_concluidos,
            SUM(la.status = 'pendente')                                     AS total_pendentes,
            SUM(la.status = 'processando')                                  AS total_processando,
            SUM(la.status = 'erro' AND la.tentativas < {MAX_TENTATIVAS})    AS total_erros,
            SUM(la.status = 'erro' AND la.tentativas >= {MAX_TENTATIVAS})   AS total_esgotados,
            SUM(la.status = 'concluido'
                AND COALESCE(
                        NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                        NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
                    ) > la.datetime_analise_concluida)                      AS total_desatualizados
        FROM {TABELA} la
        INNER JOIN clinica_atendimentos ca ON ca.id = la.id_atendimento
    """)
    row = (stats.get("data") or [{}])[0]

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
        INNER JOIN clinica_atendimentos ca ON ca.id = la.id_atendimento
        WHERE
            la.status != 'processando'
            AND ca.consulta_tipo_arquivo = 'texto'
            AND ca.consulta_conteudo IS NOT NULL
            AND LENGTH(ca.consulta_conteudo) > {MIN_CHARS}
            AND (
                la.status = 'pendente'
                OR (la.status = 'erro' AND la.tentativas < {MAX_TENTATIVAS})
                OR (la.status = 'concluido'
                    AND COALESCE(
                            NULLIF(ca.datetime_atualizacao,  '0000-00-00 00:00:00'),
                            NULLIF(ca.datetime_consulta_fim, '0000-00-00 00:00:00')
                        ) > la.datetime_analise_concluida)
            )
        ORDER BY la.datetime_analise_criacao ASC
        LIMIT {BATCH_SIZE}
    """)

    return {
        "pendentes":            data.get("data", []),
        "total_tabela":         int(row.get("total_tabela")         or 0),
        "total_concluidos":     int(row.get("total_concluidos")     or 0),
        "total_pendentes":      int(row.get("total_pendentes")      or 0),
        "total_processando":    int(row.get("total_processando")    or 0),
        "total_erros":          int(row.get("total_erros")          or 0),
        "total_esgotados":      int(row.get("total_esgotados")      or 0),
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
            ON DUPLICATE KEY UPDATE
                id_atendimento = id_atendimento
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


def contar_atendimentos_nao_concluidos_paciente(id_paciente: str) -> int:
    """Conta atendimentos do paciente que ainda não chegaram ao status concluído."""
    try:
        resp = sql_exec(f"""
            SELECT COUNT(*) AS total
            FROM {TABELA}
            WHERE id_paciente = '{esc(id_paciente)}'
              AND id_atendimento IS NOT NULL
              AND status IN ('pendente', 'processando', 'erro')
        """, reason="contar_atendimentos_nao_concluidos_paciente")
        data = resp.get("data") or []
        if not data:
            return 0
        return int(data[0].get("total") or 0)
    except Exception as e:
        log.warning(f"  ⚠️ Erro ao contar atendimentos não concluídos do paciente {id_paciente}: {e}")
        return 0


def garantir_registro_compilado_paciente_pendente(id_paciente: str) -> int:
    """
    Garante a existência do registro da síntese longitudinal do paciente.

    O registro é criado/atualizado com status='pendente' somente depois que todos
    os atendimentos unitários do paciente já estiverem concluídos, para que a
    síntese do paciente possa entrar na fila com id_atendimento NULL.
    """
    existente = sql_exec(f"""
        SELECT id
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND id_atendimento IS NULL
          AND id_criador IS NULL
        ORDER BY id DESC
        LIMIT 1
    """, reason="buscar_registro_compilado_pendente").get("data", [])

    if existente:
        id_registro = int(existente[0]["id"])
        sql_exec(f"""
            UPDATE {TABELA} SET
                id_atendimento = NULL,
                datetime_atendimento_inicio = NULL,
                datetime_ultima_atualizacao_atendimento = NULL,
                id_criador = NULL,
                status = 'pendente',
                erro_msg = NULL,
                chat_id = '',
                chat_url = '',
                modelo_llm = {esc_str(LLM_MODEL)},
                prompt_version = {esc_str(PROMPT_VERSION)}
            WHERE id = {id_registro}
        """)
        return id_registro

    sql_exec(f"""
        INSERT INTO {TABELA}
            (id_atendimento, id_paciente, id_criador, datetime_atendimento_inicio,
             datetime_ultima_atualizacao_atendimento, status, tentativas, erro_msg,
             modelo_llm, prompt_version, chat_id, chat_url)
        VALUES
            (NULL, {esc_str(id_paciente)}, NULL, NULL,
             NULL, 'pendente', 0, NULL,
             {esc_str(LLM_MODEL)}, {esc_str(PROMPT_VERSION)}, '', '')
    """, reason="criar_registro_compilado_pendente")

    criado = sql_exec(f"""
        SELECT id
        FROM {TABELA}
        WHERE id_paciente = '{esc(id_paciente)}'
          AND id_atendimento IS NULL
          AND id_criador IS NULL
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
            datetime_ultima_atualizacao_atendimento = {f"'{dtp}'" if dtp else 'NULL'}
        WHERE id_atendimento = {idat}
    """)



def resetar_travados():
    """
    Detecta registros presos em 'processando' há mais de TIMEOUT_PROCESSANDO_MIN
    e os reverte para 'pendente', incrementando tentativas para rastreio.
    """
    resultado = sql_exec(
        f"""
        UPDATE {TABELA}
        SET    status    = 'pendente',
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


def _montar_sets_resultado(resultado: dict) -> list:
    colunas  = _get_colunas_tabela()
    chat_id  = esc(resultado.get("_chat_id")  or "")
    chat_url = esc(resultado.get("_chat_url") or "")
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sets = [
        f"status                    = 'concluido'",
        f"datetime_analise_concluida = '{now}'",
        f"erro_msg                   = NULL",
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


def salvar_resultado(idatendimento: int, resultado: dict):
    """Persiste o resultado da análise unitária do atendimento."""
    sets = _montar_sets_resultado(resultado)
    sql = (
        f"UPDATE {TABELA} SET\n    "
        + ",\n    ".join(sets)
        + f"\nWHERE id_atendimento = {int(idatendimento)}"
    )
    log.debug(f"[salvar_resultado] {len(sets)} campos → id_atendimento={idatendimento}")
    sql_exec(sql)


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
          AND id_atendimento IS NOT NULL
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

    sets = [
        "id_atendimento = NULL",
        "datetime_atendimento_inicio = NULL",
        "datetime_ultima_atualizacao_atendimento = NULL",
        "id_criador = NULL",
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
          AND id_atendimento IS NOT NULL
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

    sets = [
        "id_atendimento = NULL",
        "datetime_atendimento_inicio = NULL",
        "datetime_ultima_atualizacao_atendimento = NULL",
        "id_criador = NULL",
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
          AND id_atendimento IS NOT NULL
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

    sets = [
        "id_atendimento = NULL",
        "datetime_atendimento_inicio = NULL",
        "datetime_ultima_atualizacao_atendimento = NULL",
        "id_criador = NULL",
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
          AND id_atendimento IS NOT NULL
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

    sets = [
        "id_atendimento = NULL",
        "datetime_atendimento_inicio = NULL",
        "datetime_ultima_atualizacao_atendimento = NULL",
        "id_criador = NULL",
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
          AND id_atendimento IS NOT NULL
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

    sets = [
        "id_atendimento = NULL",
        "datetime_atendimento_inicio = NULL",
        "datetime_ultima_atualizacao_atendimento = NULL",
        "id_criador = NULL",
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




def salvar_erro(idatendimento: int, msg: str):
    sql_exec(f"""
        UPDATE {TABELA} SET
            status   = 'erro',
            erro_msg = _utf8mb4'{esc(str(msg)[:1000])}'
        WHERE id_atendimento = {idatendimento}
    """)


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
                f"DELETE FROM {tabela} WHERE id_atendimento = {int(id_atendimento)}"
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
    return {
        "id":           nd.get("id") or nd.get("node_id") or "",
        "tipo":         nd.get("tipo") or nd.get("node_tipo") or nd.get("type") or nd.get("category") or "",
        "valor":        nd.get("valor") or nd.get("node_valor") or nd.get("value") or nd.get("label") or nd.get("name") or nd.get("nome") or "",
        "normalizado":  nd.get("normalizado") or nd.get("node_normalizado") or nd.get("normalized") or nd.get("normalised") or "",
        "contexto":     nd.get("contexto") or nd.get("node_contexto") or nd.get("context") or nd.get("description") or nd.get("descricao") or "",
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


def salvar_auxiliar(idatendimento: int, id_paciente: str, resultado: dict):
    """
    Chama o endpoint PHP salvar_analise_auxiliar para popular tabelas auxiliares
    (alertas, grafo nodes/edges, casos semelhantes) com charset correto.

    Normaliza os campos do grafo para garantir compatibilidade com o PHP,
    independentemente dos nomes de campo que a LLM retorne.
    """
    # Monta payload com os campos que o PHP espera
    payload = {
        PHP_KEY_FIELD:       API_KEY,
        "id_atendimento":    int(idatendimento),
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
            # Log de diagnóstico: mostra primeiro node antes/depois
            if val_original:
                log.info(f"  🔬 Primeiro node LLM (original): {json.dumps(val_original[0], ensure_ascii=False)[:200]}")
                log.info(f"  🔬 Primeiro node (normalizado):   {json.dumps(val[0], ensure_ascii=False)[:200]}")
            # Filtra nodes sem valor (o PHP faria isso de qualquer forma)
            val = [n for n in val if n.get("valor")]
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
        log.warning(f"  ⚠️ Nenhum dado auxiliar encontrado no resultado para ID={idatendimento}")
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
            log.info(f"  📊 Tabelas auxiliares salvas (alertas/grafo/casos) para ID={idatendimento}")
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

def buscar_prompt_db():
    """Busca o prompt do analisador no banco via PHP (id_criador='atendimentos_analise').
    Retorna o conteudo se encontrado, ou None para usar o SYSTEM_PROMPT local."""
    try:
        data = sql_exec(
            "SELECT conteudo FROM chatgpt_prompts WHERE tipo='system' AND id_criador='atendimentos_analise' LIMIT 1",
            reason="buscar_prompt_analisador"
        )
        rows = data.get("data") or []
        if rows and rows[0].get("conteudo"):
            conteudo = rows[0]["conteudo"]
            if "[INICIO_TEXTO_COLADO]" not in conteudo:
                conteudo = "[INICIO_TEXTO_COLADO]" + conteudo + "[FIM_TEXTO_COLADO]"
            log.info("Prompt do analisador carregado do banco (chatgpt_prompts).")
            return conteudo
        log.info("Prompt 'atendimentos_analise' nao encontrado no banco - usando prompt local.")
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


def analisar_prontuario(texto: str, chat_url: str = None, chat_id: str = None, contexto: str = "") -> dict:
    """
    chat_url / chat_id: se fornecidos, o browser.py retoma a conversa existente
    em vez de abrir um novo chat — evita proliferação de chats no ChatGPT.
    contexto: bloco de texto com dados do paciente, profissional e hospital
    (inserido antes do prontuário para dar contexto ao LLM).
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
            {"role": "system", "content": buscar_prompt_db() or SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
    }

    # Retoma conversa existente se disponível
    if chat_url:
        payload["url"]    = chat_url
    if chat_id:
        payload["chatid"] = chat_id

    resp = requests.post(
        LLM_URL,
        json=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        stream=True,
        timeout=300,
    )
    resp.raise_for_status()

    new_chat_id    = chat_id    # fallback: mantém o anterior se não vier novo
    new_chat_url   = chat_url
    new_chat_title = None
    markdown       = ""

    # Funcao auxiliar para progresso inline (sobrescreve a linha atual no CMD)
    def _inline(msg):
        sys.stdout.write(f'\r  {msg:<55}')
        sys.stdout.flush()

    def _newline():
        sys.stdout.write('\n')
        sys.stdout.flush()

    def _log_wrapped(prefixo: str, msg: str):
        """
        Loga mensagens longas em múltiplas linhas reais para evitar que o
        próximo progresso inline (\r) sobrescreva o trecho final quando o
        terminal fizer quebra visual automática.
        """
        texto = re.sub(r"\s+", " ", str(msg or "")).strip()
        if not texto:
            return

        largura_terminal = shutil.get_terminal_size((140, 20)).columns
        largura_util = max(50, largura_terminal - 36)  # reserva espaço do timestamp/logger
        linhas = textwrap.wrap(
            texto,
            width=largura_util,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [texto]

        for linha in linhas:
            log.info(f"  {prefixo} {linha}")

    last_status = ""
    inline_active = False  # True quando ha uma linha inline aberta
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
            if msg == last_status:
                continue
            last_status = msg
            is_progress = '%' in msg
            if is_progress:
                # Progresso com %: escreve inline sobrescrevendo a linha
                _inline(f'⏳ {msg}')
                inline_active = True
            else:
                if inline_active:
                    _newline()
                    inline_active = False
                _log_wrapped('⏳', msg)

        elif t == "log":
            if inline_active:
                _newline()
                inline_active = False
            log.info(f"  🔧 {obj.get('content', '').strip()}")

        elif t == "chatid":
            if inline_active:
                _newline()
                inline_active = False
            new_chat_id = obj.get("content") or new_chat_id
            log.info(f"  📎 chat_id: {new_chat_id}")

        elif t == "markdown":
            markdown = obj.get("content", "")
            # Progresso de recepcao: inline sobrescrevendo a linha
            _inline(f'📝 Recebendo: {len(markdown)} chars...')
            inline_active = True

        elif t == "finish":
            if inline_active:
                _newline()
                inline_active = False
            fin = obj.get("content", {})
            new_chat_url   = fin.get("url")     or new_chat_url
            new_chat_title = fin.get("title")   or new_chat_title
            new_chat_id    = fin.get("chat_id") or new_chat_id
            # fallback: extrai chat_id da URL caso ainda não tenha sido recebido
            if not new_chat_id and new_chat_url:
                new_chat_id = new_chat_url.rstrip('/').split('/')[-1] or new_chat_id
            log.info(f"  🔗 chat_url: {new_chat_url} | chat_id: {new_chat_id}")

        elif t == "error":
            if inline_active:
                _newline()
                inline_active = False
            raise RuntimeError(f"Simulador retornou erro: {obj.get('content')}")

    if not markdown:
        raise ValueError("Simulador não retornou conteúdo markdown.")

    match = re.search(r'\{[\s\S]*\}', markdown)
    if not match:
        raise ValueError(f"LLM não retornou JSON válido: {markdown[:200]}")

    resultado = json.loads(match.group())
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
  "search_queries": [
    {
      "query": "consulta para Google/PubMed",
      "reason": "por que esta busca é útil para este paciente"
    }
  ]
}

REGRAS:
- Responder SOMENTE com JSON válido.
- Máximo de 3 queries.
- Preferir inglês quando isso melhorar a busca científica.
- Quando fizer sentido, usar PubMed, diretrizes pediátricas, systematic review, guideline, consensus.
- Não inventar fatos que não estejam explícitos no caso.
- Não repetir queries redundantes.
"""


def gerar_queries_pesquisa_llm(resultado: dict, chat_url: str = None, chat_id: str = None) -> list:
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
        return []

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
    }

    if chat_url:
        payload["url"] = chat_url
    if chat_id:
        payload["chatid"] = chat_id

    try:
        resp = requests.post(
            LLM_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            stream=True,
            timeout=300,
        )
        resp.raise_for_status()

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
                return []

        if not markdown:
            log.warning("  ⚠️ Planejamento de pesquisa: LLM não retornou conteúdo.")
            return []

        match = re.search(r'\{[\s\S]*\}', markdown)
        if not match:
            log.warning("  ⚠️ Planejamento de pesquisa: LLM não retornou JSON válido.")
            return []

        planejado = json.loads(match.group())
        itens = planejado.get("search_queries") or []
        queries = []
        query_labels = set()
        for item in itens[:SEARCH_MAX_QUERIES]:
            if not isinstance(item, dict):
                continue
            query = re.sub(r"\s+", " ", str(item.get("query") or "")).strip()
            reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()
            if not query:
                continue
            key = query.lower()
            if key in query_labels:
                continue
            query_labels.add(key)
            queries.append(query)
            log.info(f"     🧠 {query}" + (f" | motivo: {reason}" if reason else ""))

        if queries:
            log.info(f"  🧠 LLM planejou {len(queries)} query(s) de pesquisa específicas para o paciente.")

        return queries[:SEARCH_MAX_QUERIES]

    except Exception as e:
        log.warning(f"  ⚠️ Planejamento de pesquisa via LLM falhou: {e}")
        return []


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
    }

    # Retoma o mesmo chat para ter contexto da análise anterior
    if chat_url:
        payload["url"] = chat_url
    if chat_id:
        payload["chatid"] = chat_id

    try:
        resp = requests.post(
            LLM_URL,
            json=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            stream=True,
            timeout=300,
        )
        resp.raise_for_status()

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
                log.warning(f"  ⚠️ Enriquecimento: LLM retornou erro: {obj.get('content')}")
                return resultado

        if not markdown:
            log.warning("  ⚠️ Enriquecimento: LLM não retornou conteúdo.")
            return resultado

        match = re.search(r'\{[\s\S]*\}', markdown)
        if not match:
            log.warning(f"  ⚠️ Enriquecimento: LLM não retornou JSON válido.")
            return resultado

        enriquecido = json.loads(match.group())

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
      1. Extrair termos de busca do resultado da análise
      2. Buscar no Google via /api/web_search
      3. Enviar resultados para a LLM enriquecer condutas
    """
    if not SEARCH_HABILITADA:
        return resultado

    # 1. Pede à LLM para planejar queries específicas do paciente
    queries = gerar_queries_pesquisa_llm(resultado, chat_url=chat_url, chat_id=chat_id)
    if not queries:
        log.info("  🔄 Fallback: usando extração heurística de termos para montar as queries.")
        queries = extrair_termos_busca(resultado)
    if not queries:
        log.info("  🔍 Nenhum termo clínico para busca — pulando enriquecimento.")
        return resultado

    log.info(f"  🌐 Busca web: {len(queries)} query(s)")
    for q in queries:
        log.info(f"     🔎 {q}")

    # 2. Busca no Google
    resultados_web = buscar_web(queries)
    total_items = sum(
        len(r.get("results", [])) for r in resultados_web if r.get("success", True)
    )
    tem_html = any(r.get("raw_html") for r in resultados_web if r.get("success", True))
    log.info(f"  🌐 {total_items} resultado(s) estruturado(s) | HTML bruto: {'sim' if tem_html else 'não'}")

    if total_items == 0 and not tem_html:
        log.info("  🔍 Nenhum resultado — pulando enriquecimento.")
        return resultado

    # 3. Enriquecer condutas via LLM (Passo 2)
    log.info(f"  📚 Enviando resultados para LLM enriquecer condutas...")
    resultado = enriquecer_com_evidencias(resultado, resultados_web, chat_url, chat_id)

    return resultado


# ─────────────────────────────────────────────────────────────
# LOOP PRINCIPAL
# ─────────────────────────────────────────────────────────────

# Intervalo de pausa entre análises (segundos)
PAUSA_MIN = 15   # mínimo humano razoável após leitura
PAUSA_MAX = 45   # máximo antes de parecer inatividade

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
            salvar_erro(idat, "Texto insuficiente após remoção de HTML.")
            log.warning(f"  ID={idat} ignorado: texto muito curto.")
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
            resultado = analisar_prontuario(texto, chat_url=chat_url_prev, chat_id=chat_id_prev, contexto=contexto)

            # Passo 2: Busca web + enriquecimento de condutas com evidências
            try:
                chat_url_atual = resultado.get("_chat_url") or chat_url_prev
                chat_id_atual  = resultado.get("_chat_id")  or chat_id_prev
                resultado = executar_busca_evidencias(resultado, chat_url=chat_url_atual, chat_id=chat_id_atual)
            except Exception as e:
                log.warning(f"  ⚠️ Enriquecimento com evidências falhou (não fatal): {e}")

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

        except Exception as e:
            salvar_erro(idat, str(e))
            log.error(f"  ❌ ID={idat} erro: {e}")

        if i < total - 1:
            pausa = int(random.uniform(PAUSA_MIN, PAUSA_MAX))
            log.info(f"  ⏸  Pausa antes do próximo prontuário...")
            countdown(pausa, "próximo prontuário")


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
    garantir_colunas_v16()                       # garante colunas CDSS/RAG da V16 (modelo_llm, hash_prontuario, score_risco, etc.)
    garantir_schema_analise_compilada_paciente() # permite síntese longitudinal por id_paciente sem id_atendimento
    garantir_migracoes()                         # corrige tipos de colunas em tabelas pré-existentes
    garantir_tabela_embeddings()                 # garante tabelas de embeddings + casos semelhantes

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

            # ── Ciclo normal ──────────────────────────────────────────
            resetar_travados()
            resultado = buscar_pendentes()
            pendentes = resultado["pendentes"]

            log.info(f"── Ciclo #{ciclo} {'─' * 50}")
            log.info(f"   📊 Prontuários na fila : {resultado['total_tabela']}")
            log.info(f"   ✅ Concluídos/atualizados : {resultado['total_concluidos']}")
            log.info(f"   🕐 Aguardando análise     : {resultado['total_pendentes']}")
            log.info(f"   🔄 Em processamento       : {resultado['total_processando']}")
            log.info(f"   🔁 Prontuários editados   : {resultado['total_desatualizados']}  (reanálise pendente)")
            log.info(f"   ❌ Com erro (c/ retentativa): {resultado['total_erros']}")
            log.info(f"   🚫 Esgotados (sem retentativa): {resultado['total_esgotados']}")

            if pendentes:
                log.info(f"   ▶  {len(pendentes)} prontuário(s) serão processados agora.")
                processar_lote(pendentes)
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
    while True:
        try:
            main()
        except KeyboardInterrupt:
            log.info("\n👋 Analisador encerrado pelo usuário (Ctrl+C).")
            break
        except SystemExit:
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
