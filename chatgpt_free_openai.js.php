<?php
@ini_set('display_errors', 0);
error_reporting(0);

$currentFileName = basename(__FILE__);
$phpSelf = basename($_SERVER['PHP_SELF'] ?? '');
$secFetchDest = strtolower($_SERVER['HTTP_SEC_FETCH_DEST'] ?? '');
$httpAccept = strtolower($_SERVER['HTTP_ACCEPT'] ?? '');
$requestedAsJs = (($_GET['as'] ?? '') === 'js');
$acceptsJs = (strpos($httpAccept, 'javascript') !== false);
$acceptsHtml = (strpos($httpAccept, 'text/html') !== false);

$isDirectFileUrl = ($phpSelf === $currentFileName);
$isScriptFetch = $requestedAsJs || ($secFetchDest === 'script') || ($acceptsJs && !$acceptsHtml);
$shouldRenderDirectPage = $isDirectFileUrl && !$isScriptFetch;
$action = $_GET['action'] ?? '';
$freeOpenAIPromptCreatorPrefix = 'puter_free_openai_';
$freeOpenAIDefaultPromptCreator = $freeOpenAIPromptCreatorPrefix . 'default';
$freeOpenAIDefaultSystemPrompt = <<<EOT
####################################################################
### ASSISTENTE CLÍNICO (PUTER FREE OPENAI) V1.0                  ###
####################################################################

IDIOMA
- Responder sempre em Português do Brasil.

CONTEXTO
- Sistema clínico de neuropediatria associado ao Dr. Hellysson Cavalcanti.
- Você pode responder diretamente quando tiver confiança.
- Quando faltar informação atualizada/externa, solicite pesquisa web no formato exigido.
- Quando faltar informação estruturada interna do sistema, solicite SQL no formato exigido.

REGRA CRÍTICA
- Nunca inventar dados clínicos.
- Nunca preencher lacunas com suposição.
- Em caso de dúvida clínica relevante, deixar explícita a incerteza.

FORMATO PARA SOLICITAR PESQUISA WEB (MÁX 3)
{
  "search_queries": [
    {
      "query": "termos objetivos para busca",
      "reason": "por que a pesquisa é necessária"
    }
  ]
}

FORMATO PARA SOLICITAR SQL (MÁX 3)
{
  "sql_queries": [
    {
      "query": "SELECT ...",
      "reason": "por que precisa consultar o banco"
    }
  ]
}

REGRA: NUNCA misturar `search_queries` e `sql_queries` na mesma resposta.

SE JÁ TIVER INFORMAÇÃO SUFICIENTE
- Responda normalmente em texto claro e objetivo.

SE RECEBER BLOCO [RESULTADOS_DE_BUSCA_WEB]
- Use somente as fontes fornecidas.
- Nunca invente URLs, PMIDs, DOIs ou títulos.
- Cite URLs quando disponíveis.
EOT;

function chatgpt_free_bootstrap_context_if_needed() {
    static $bootstrapped = false;
    if ($bootstrapped) return;
    $bootstrapped = true;

    date_default_timezone_set('America/Recife');
    $filename = 'config/config.php';if(file_exists($filename)){@include_once($filename);}elseif(file_exists("../".$filename)){@include_once("../".$filename);}elseif(file_exists("../../".$filename)){@include_once("../../".$filename);}elseif(file_exists("../../../".$filename)){@include_once("../../../".$filename);}
    $filename = 'scripts/login.php';if(file_exists($filename)){@include_once($filename);}elseif(file_exists("../".$filename)){@include_once("../".$filename);}elseif(file_exists("../../".$filename)){@include_once("../../".$filename);}elseif(file_exists("../../../".$filename)){@include_once("../../../".$filename);}
    $filename = 'scripts/func.inc.php';if(file_exists($filename)){@include_once($filename);}elseif(file_exists("../".$filename)){@include_once("../".$filename);}elseif(file_exists("../../".$filename)){@include_once("../../".$filename);}elseif(file_exists("../../../".$filename)){@include_once("../../../".$filename);}

    // IMPORTANTE: includes dentro de função carregam variáveis em escopo local.
    // Repassa para $GLOBALS para manter compatibilidade com o comportamento anterior.
    foreach ([
        'mysqli',
        'row_login_atual',
        'config',
        'hostname_conexao',
        'database_conexao',
        'username_conexao',
        'password_conexao'
    ] as $globalKey) {
        if (isset($$globalKey)) {
            $GLOBALS[$globalKey] = $$globalKey;
        }
    }
}

function chatgpt_free_get_mysql_connection_local() {
    global $mysqli, $config, $hostname_conexao, $database_conexao, $username_conexao, $password_conexao;
    if (isset($mysqli) && $mysqli instanceof mysqli && @$mysqli->ping()) {
        @$mysqli->set_charset("utf8mb4");
        return $mysqli;
    }
    $host = $config["mysql_host"] ?? $hostname_conexao ?? null;
    $user = $config["mysql_login"] ?? $username_conexao ?? null;
    $pass = $config["mysql_password"] ?? $password_conexao ?? null;
    $db   = $config["mysql_db"] ?? $database_conexao ?? null;
    $port = $config["mysql_port"] ?? 3306;
    if (!$host) return null;
    $con = new mysqli($host, $user, $pass, $db, $port);
    if ($con->connect_error) return null;
    @$con->set_charset("utf8mb4");
    return $con;
}

function chatgpt_free_parse_context($data) {
    $id_criador = isset($data['id_criador']) && is_numeric($data['id_criador']) ? intval($data['id_criador']) : (isset($_GET['id_criador']) && is_numeric($_GET['id_criador']) ? intval($_GET['id_criador']) : null);
    $id_paciente = isset($data['id_paciente']) && is_numeric($data['id_paciente']) ? intval($data['id_paciente']) : (isset($_GET['id_paciente']) && is_numeric($_GET['id_paciente']) ? intval($_GET['id_paciente']) : null);
    $id_atendimento = isset($data['id_atendimento']) && is_numeric($data['id_atendimento']) ? intval($data['id_atendimento']) : (isset($_GET['id_atendimento']) && is_numeric($_GET['id_atendimento']) ? intval($_GET['id_atendimento']) : null);
    $id_receita = isset($data['id_receita']) && is_numeric($data['id_receita']) ? intval($data['id_receita']) : (isset($_GET['id_receita']) && is_numeric($_GET['id_receita']) ? intval($_GET['id_receita']) : null);

    return [
      'id_criador' => $id_criador,
      'id_paciente' => $id_paciente,
      'id_atendimento' => $id_atendimento,
      'id_receita' => $id_receita
    ];
}

if ($action === 'get_prompt' || $action === 'save_prompt') {
    while (ob_get_level()) ob_end_clean();
    header('Content-Type: application/json; charset=utf-8');
    chatgpt_free_bootstrap_context_if_needed();
    @session_start();

    $db = chatgpt_free_get_mysql_connection_local();
    if (!$db) {
        echo json_encode(['success' => false, 'error' => 'Falha na conexão com banco de dados']);
        exit;
    }

    global $row_login_atual, $freeOpenAIDefaultPromptCreator, $freeOpenAIDefaultSystemPrompt, $freeOpenAIPromptCreatorPrefix;
    $id_criador_logado = isset($row_login_atual['id']) && is_numeric($row_login_atual['id'])
      ? intval($row_login_atual['id'])
      : (isset($_SESSION['id']) && is_numeric($_SESSION['id']) ? intval($_SESSION['id']) : null);
    if (!$id_criador_logado) {
        http_response_code(401);
        echo json_encode(['success' => false, 'error' => 'Utilizador não autenticado.']);
        exit;
    }

    $canEditSystemPrompt = function_exists('verifica_permissao')
      ? (verifica_permissao($db, $id_criador_logado, 'chatgpt_system_prompt', 'editar') ? true : false)
      : false;

    if ($action === 'get_prompt') {
        $system_prompt = $freeOpenAIDefaultSystemPrompt;
        if ($canEditSystemPrompt) {
            $sql = "SELECT conteudo FROM chatgpt_prompts WHERE tipo='system' AND id_criador LIKE '" . $db->real_escape_string($freeOpenAIPromptCreatorPrefix) . "%' ORDER BY (id_criador='" . $db->real_escape_string($freeOpenAIDefaultPromptCreator) . "') DESC, id DESC LIMIT 1";
            $r = $db->query($sql);
            if ($r && ($row = $r->fetch_assoc()) && !empty(trim((string)$row['conteudo']))) {
                $system_prompt = $row['conteudo'];
            }
        }
        echo json_encode([
            'success' => true,
            'system_prompt' => $system_prompt,
            'can_edit_system_prompt' => $canEditSystemPrompt
        ]);
        exit;
    }

    if (!$canEditSystemPrompt) {
        http_response_code(403);
        echo json_encode(['success' => false, 'error' => 'Sem permissão para editar prompt do sistema.']);
        exit;
    }

    $inputJSON = file_get_contents('php://input');
    $data = is_string($inputJSON) && $inputJSON !== '' ? json_decode($inputJSON, true) : [];
    if (!is_array($data)) $data = [];
    $tipo = trim((string)($data['tipo'] ?? 'system'));
    if ($tipo !== 'system') {
        echo json_encode(['success' => false, 'error' => 'Tipo inválido para este handler.']);
        exit;
    }
    $conteudo = trim((string)($data['conteudo'] ?? ''));
    if ($conteudo === '') $conteudo = $freeOpenAIDefaultSystemPrompt;
    $conteudo_esc = $db->real_escape_string($conteudo);
    $creator_esc = $db->real_escape_string($freeOpenAIDefaultPromptCreator);
    $db->query("DELETE FROM chatgpt_prompts WHERE tipo='system' AND id_criador='$creator_esc'");
    $ok = $db->query("INSERT INTO chatgpt_prompts (tipo, id_criador, conteudo) VALUES ('system', '$creator_esc', '$conteudo_esc')");
    echo json_encode(['success' => $ok ? true : false, 'error' => $ok ? null : $db->error]);
    exit;
}

if ($action === 'save_chat_history' || $action === 'get_chat_history' || $action === 'delete_chat_history') {
    while (ob_get_level()) ob_end_clean();
    header('Content-Type: application/json; charset=utf-8');
    chatgpt_free_bootstrap_context_if_needed();
    @session_start();

    $inputJSON = file_get_contents('php://input');
    $data = is_string($inputJSON) && $inputJSON !== '' ? json_decode($inputJSON, true) : [];
    if (!is_array($data)) $data = [];

    global $row_login_atual;
    $id_criador_logado = isset($row_login_atual['id']) && is_numeric($row_login_atual['id'])
      ? intval($row_login_atual['id'])
      : (isset($_SESSION['id']) && is_numeric($_SESSION['id']) ? intval($_SESSION['id']) : null);
    if (!$id_criador_logado) {
        http_response_code(401);
        echo json_encode(['success' => false, 'error' => 'Utilizador não autenticado.']);
        exit;
    }

    $ctx = chatgpt_free_parse_context($data);
    if (!$ctx['id_criador']) $ctx['id_criador'] = $id_criador_logado;

    $db = chatgpt_free_get_mysql_connection_local();
    if (!$db) {
        echo json_encode(['success' => false, 'error' => 'Falha na conexão com banco de dados']);
        exit;
    }

    @$db->query("ALTER TABLE chatgpt_chats ADD COLUMN chat_mode VARCHAR(120) NOT NULL DEFAULT 'assistant'");
    @$db->query("ALTER TABLE chatgpt_chats ADD COLUMN mensagens LONGTEXT NULL");
    $model_used = isset($data['model']) ? preg_replace('/\s+/', ' ', trim((string)$data['model'])) : '';
    $legacy_chat_mode = 'free_openai';
    $chat_mode_value = ($model_used !== '') ? $model_used : $legacy_chat_mode;
    $chat_mode_esc = $db->real_escape_string($chat_mode_value);
    $legacy_chat_mode_esc = $db->real_escape_string($legacy_chat_mode);

    if ($ctx['id_atendimento']) {
        $where_base = "id_atendimento = " . intval($ctx['id_atendimento']);
    } elseif ($ctx['id_receita']) {
        $where_base = "id_receita = " . intval($ctx['id_receita']) . " AND id_atendimento IS NULL";
    } elseif ($ctx['id_paciente']) {
        $where_base = "id_paciente = " . intval($ctx['id_paciente']) . " AND id_atendimento IS NULL AND id_receita IS NULL";
    } else {
        $where_base = "id_criador = " . intval($ctx['id_criador']) . " AND id_atendimento IS NULL AND id_receita IS NULL AND id_paciente IS NULL";
    }
    $where = "$where_base AND chat_mode = '$chat_mode_esc'";

    if ($action === 'get_chat_history') {
        $legacy_mode_condition = "(chat_mode = '$legacy_chat_mode_esc' OR chat_mode IS NULL OR chat_mode = '')";
        $sql = "SELECT mensagens FROM chatgpt_chats WHERE $where ORDER BY datetime_atualizacao DESC, id DESC LIMIT 1";
        $result = $db->query($sql);
        if ((!$result || $result->num_rows === 0) && $chat_mode_value !== $legacy_chat_mode) {
            $sql = "SELECT mensagens FROM chatgpt_chats WHERE $where_base AND $legacy_mode_condition ORDER BY datetime_atualizacao DESC, id DESC LIMIT 1";
            $result = $db->query($sql);
        }
        if ((!$result || $result->num_rows === 0) && $chat_mode_value === $legacy_chat_mode) {
            $sql = "SELECT mensagens FROM chatgpt_chats WHERE $where_base ORDER BY datetime_atualizacao DESC, id DESC LIMIT 1";
            $result = $db->query($sql);
        }
        if ($result && $result->num_rows > 0) {
            $row = $result->fetch_assoc();
            $messages = [];
            if (!empty($row['mensagens'])) {
                $decoded = json_decode($row['mensagens'], true);
                if (is_array($decoded)) $messages = $decoded;
            }
            echo json_encode(['success' => true, 'messages' => $messages]);
        } else {
            echo json_encode(['success' => true, 'messages' => []]);
        }
        exit;
    }

    if ($action === 'delete_chat_history') {
        $deletedRows = 0;
        $deleteSql = "DELETE FROM chatgpt_chats WHERE $where";
        $ok = $db->query($deleteSql);
        if ($ok) $deletedRows += (int)$db->affected_rows;

        if ($chat_mode_value !== $legacy_chat_mode) {
            $legacy_mode_condition = "(chat_mode = '$legacy_chat_mode_esc' OR chat_mode IS NULL OR chat_mode = '')";
            $deleteLegacySql = "DELETE FROM chatgpt_chats WHERE $where_base AND $legacy_mode_condition";
            $okLegacy = $db->query($deleteLegacySql);
            if ($okLegacy) $deletedRows += (int)$db->affected_rows;
        }

        if ($deletedRows > 0) {
            echo json_encode(['success' => true, 'deleted_rows' => $deletedRows]);
        } else {
            echo json_encode(['success' => false, 'error' => 'Nenhum histórico correspondente foi excluído.', 'deleted_rows' => 0]);
        }
        exit;
    }

    $messages = isset($data['messages']) && is_array($data['messages']) ? $data['messages'] : [];
    $messages_esc = $db->real_escape_string(json_encode($messages, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    $url_atual_esc = $db->real_escape_string($data['url_atual'] ?? ($_SERVER['HTTP_REFERER'] ?? ''));
    $titulo_esc = $db->real_escape_string('ConexaoVida IA - Free OpenAI');
    $id_chatgpt_label = 'Chat via Puter (free OpenAI)';
    $id_chatgpt_esc = $db->real_escape_string($id_chatgpt_label);
    $url_chatgpt_esc = $db->real_escape_string('');
    $sql_paciente = $ctx['id_paciente'] ? intval($ctx['id_paciente']) : "NULL";
    $sql_atendimento = $ctx['id_atendimento'] ? intval($ctx['id_atendimento']) : "NULL";
    $sql_receita = $ctx['id_receita'] ? intval($ctx['id_receita']) : "NULL";
    $id_criador = intval($ctx['id_criador']);

    $check = $db->query("SELECT id FROM chatgpt_chats WHERE $where LIMIT 1");
    if ($check && $check->num_rows > 0) {
        $row = $check->fetch_assoc();
        $id = intval($row['id']);
        $update_sql = "UPDATE chatgpt_chats SET
            mensagens = '$messages_esc',
            url_atual = '$url_atual_esc',
            titulo = '$titulo_esc',
            id_chatgpt = '$id_chatgpt_esc',
            url_chatgpt = '$url_chatgpt_esc',
            id_criador = $id_criador,
            id_paciente = $sql_paciente,
            id_atendimento = $sql_atendimento,
            id_receita = $sql_receita,
            chat_mode = '$chat_mode_esc'
            WHERE id = $id";
        $ok = $db->query($update_sql);
    } else {
        $insert_sql = "INSERT INTO chatgpt_chats
            (id_criador, id_paciente, id_atendimento, id_receita, url_atual, titulo, id_chatgpt, url_chatgpt, chat_mode, mensagens)
            VALUES
            ($id_criador, $sql_paciente, $sql_atendimento, $sql_receita, '$url_atual_esc', '$titulo_esc', '$id_chatgpt_esc', '$url_chatgpt_esc', '$chat_mode_esc', '$messages_esc')";
        $ok = $db->query($insert_sql);
    }

    if (!$ok) {
        echo json_encode(['success' => false, 'error' => $db->error]);
    } else {
        echo json_encode(['success' => true, 'saved_messages' => count($messages)]);
    }
    exit;
}

if ($shouldRenderDirectPage) {
    header("Content-Type: text/html; charset=UTF-8", true);
    date_default_timezone_set('America/Recife');
    //PREVENÇÃO DE CACHE AGRESSIVO (ESPECIALMENTE PARA SAFARI/MOBILE):
    header("Cache-Control: no-store, no-cache, must-revalidate, max-age=0");header("Cache-Control: post-check=0, pre-check=0", false);header("Pragma: no-cache");header("Expires: Wed, 11 Jan 1984 05:00:00 GMT"); // Uma data no passado, para evitar que os navegadores guardem o arquivo em cache.
    chatgpt_free_bootstrap_context_if_needed();

    $authorized = false;
    if (
        isset($row_login_atual['id']) &&
        function_exists('verifica_permissao') &&
        isset($mysqli)
    ) {
        $authorized = verifica_permissao($mysqli, $row_login_atual['id'], 'chatgpt_system_prompt', 'editar') ? true : false;
    }

    $selfPath = $_SERVER['PHP_SELF'] ?? $currentFileName;
    $selfJsUrl = htmlspecialchars($selfPath . '?as=js', ENT_QUOTES, 'UTF-8');
    ?>
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ChatGPT Free OpenAI - Painel</title>
  <style>
    body{margin:0;background:#f2f4f8;font-family:Arial,sans-serif}
    .wrap{max-width:980px;margin:30px auto;padding:0 16px}
    .card{background:#fff;border:1px solid #e6e9ef;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.06)}
    .head{padding:14px 16px;border-bottom:1px solid #eceff5;font-weight:700}
    .body{padding:16px}
    .denied{color:#a40000;background:#fff1f1;border:1px solid #ffd0d0;padding:12px;border-radius:8px}
    .prompt-box{margin-top:12px;border:1px solid #e5e7eb;border-radius:8px;padding:10px;background:#fafafa}
    .prompt-box textarea{width:100%;min-height:140px;font-family:monospace;font-size:12px}
    .prompt-actions{margin-top:8px;display:flex;gap:8px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="head">ChatGPT Free OpenAI (modo página)</div>
      <div class="body">
        <?php if ($authorized): ?>
          <div id="chatgpt-free-openai-page-root"></div>
          <script>
            window.__CHATGPT_FREE_OPENAI_MODE = 'page';
            window.__CHATGPT_FREE_OPENAI_CONTAINER = '#chatgpt-free-openai-page-root';
            window.__CHATGPT_FREE_OPENAI_CAN_EDIT_PROMPT = <?php echo $authorized ? 'true' : 'false'; ?>;
          </script>
          <script src="<?php echo $selfJsUrl; ?>"></script>
        <?php else: ?>
          <div class="denied">Você não possui permissão para abrir este handler diretamente.</div>
        <?php endif; ?>
      </div>
    </div>
  </div>
</body>
</html>
    <?php
    exit;
}

header('Content-Type: application/javascript; charset=utf-8');
?>
(function () {
  'use strict';

  if (window.__chatgptFreeOpenAIToastLoaded) return;
  window.__chatgptFreeOpenAIToastLoaded = true;

  var FILE_PREFIX = '[<?php echo $_SERVER['PHP_SELF']; ?>]';
  var PREFIX = 'chatgpt_free_openai_';
  var PUTER_SDK_URL = 'https://js.puter.com/v2/';
  var MAX_HISTORY = 12;
  var KEY_MODEL = PREFIX + 'selected_model';
  var KEY_MODEL_MANUAL = PREFIX + 'selected_model_manual';
  var KEY_WORKING_MODELS_CACHE = PREFIX + 'working_models_cache_v1';
  var RENDER_MODE = window.__CHATGPT_FREE_OPENAI_MODE === 'page' ? 'page' : 'toast';
  var DEFAULT_SYS_PROMPT = `<?php echo str_replace('`', '\`', $freeOpenAIDefaultSystemPrompt); ?>`;
  var activeSystemPrompt = DEFAULT_SYS_PROMPT;
  var canEditSystemPrompt = !!window.__CHATGPT_FREE_OPENAI_CAN_EDIT_PROMPT;

  function log() { console.log.apply(console, ['%c' + FILE_PREFIX + ' [LOG]', 'color:#1976d2;font-weight:bold'].concat([].slice.call(arguments))); }
  function warn() { console.warn.apply(console, ['%c' + FILE_PREFIX + ' [WARN]', 'color:#f57c00;font-weight:bold'].concat([].slice.call(arguments))); }
  function error() { console.error.apply(console, ['%c' + FILE_PREFIX + ' [ERROR]', 'color:#d32f2f;font-weight:bold'].concat([].slice.call(arguments))); }
  function normalizeError(err) {
    if (!err) return 'erro desconhecido';
    if (typeof err === 'string') return err;
    if (err.message) return err.message;
    try { return JSON.stringify(err); } catch (_) { return String(err); }
  }

  function extractFirstJsonObject(text) {
    var s = String(text || '').trim();
    if (!s) return null;
    try { return JSON.parse(s); } catch (_) {}
    var start = s.indexOf('{');
    var end = s.lastIndexOf('}');
    if (start >= 0 && end > start) {
      var maybe = s.slice(start, end + 1);
      try { return JSON.parse(maybe); } catch (_) {}
    }
    return null;
  }

  async function decideToolUseWithLLM(puter, model, prompt) {
    var decisionPrompt = [
      'Avalie a pergunta do usuário e decida se é necessário solicitar pesquisa web externa.',
      'Responda SOMENTE com JSON válido em UM dos formatos:',
      '{"search_queries":[{"query":"...","reason":"..."}]}',
      '{"sql_queries":[{"query":"...","reason":"..."}]}',
      '{"direct_answer":true}',
      'Regras: no máximo 3 queries; nunca misture search_queries com sql_queries.'
    ].join('\n');
    var messages = [
      { role: 'system', content: activeSystemPrompt },
      { role: 'user', content: decisionPrompt + '\n\nPergunta do usuário:\n' + String(prompt || '') }
    ];
    var raw = await puter.ai.chat(messages, { model: model });
    var parsed = extractFirstJsonObject(normalizeAssistantText(raw));
    if (!parsed || typeof parsed !== 'object') return { direct_answer: true };
    if (Array.isArray(parsed.search_queries) && parsed.search_queries.length) return { search_queries: parsed.search_queries.slice(0, 3) };
    if (Array.isArray(parsed.sql_queries) && parsed.sql_queries.length) return { sql_queries: parsed.sql_queries.slice(0, 3) };
    return { direct_answer: true };
  }

  function buildChatOptions(model, enableWebSearch) {
    var opts = { model: model };
    if (enableWebSearch) {
      // Mantém múltiplas chaves por compatibilidade com variações de SDK.
      opts.web_search = true;
      opts.search = true;
      opts.enable_search = true;
      opts.tools = [{ type: 'web_search' }];
    }
    return opts;
  }

  function normalizeWebSearchItems(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw;
    if (Array.isArray(raw.results)) return raw.results;
    if (Array.isArray(raw.items)) return raw.items;
    if (Array.isArray(raw.data)) return raw.data;
    return [];
  }

  async function fetchJsonWithTimeout(url, ms) {
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = null;
    try {
      if (ctrl) timer = setTimeout(function () { ctrl.abort(); }, ms || 6000);
      var res = await fetch(url, {
        method: 'GET',
        credentials: 'omit',
        signal: ctrl ? ctrl.signal : undefined
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function formatSearchItems(items) {
    return (items || []).slice(0, 5).map(function (it, idx) {
      var title = it.title || it.name || ('Resultado ' + (idx + 1));
      var url = it.url || it.link || '';
      var snippet = it.snippet || it.description || it.text || '';
      return '- ' + title + (url ? ' (' + url + ')' : '') + (snippet ? ' :: ' + snippet : '');
    }).join('\n');
  }

  async function buildFallbackWebContext(query) {
    var q = String(query || '').trim();
    if (!q) return '';
    var collected = [];

    try {
      var ddgUrl = 'https://api.duckduckgo.com/?q=' + encodeURIComponent(q) + '&format=json&no_html=1&skip_disambig=1';
      var ddg = await fetchJsonWithTimeout(ddgUrl, 7000);
      if (ddg && ddg.AbstractText) {
        collected.push({
          title: ddg.Heading || q,
          url: ddg.AbstractURL || '',
          snippet: ddg.AbstractText
        });
      }
      var topics = (ddg && Array.isArray(ddg.RelatedTopics)) ? ddg.RelatedTopics : [];
      topics.slice(0, 4).forEach(function (topic) {
        if (topic && topic.Text) {
          collected.push({
            title: topic.FirstURL ? topic.FirstURL.split('/').pop().replace(/_/g, ' ') : 'Relacionado',
            url: topic.FirstURL || '',
            snippet: topic.Text
          });
        } else if (topic && Array.isArray(topic.Topics)) {
          topic.Topics.slice(0, 2).forEach(function (nested) {
            if (!nested || !nested.Text) return;
            collected.push({
              title: nested.FirstURL ? nested.FirstURL.split('/').pop().replace(/_/g, ' ') : 'Relacionado',
              url: nested.FirstURL || '',
              snippet: nested.Text
            });
          });
        }
      });
    } catch (err) {
      warn('web_search:fallback_ddg_fail', normalizeError(err));
    }

    if (!collected.length) return '';
    return formatSearchItems(collected);
  }

  async function buildWebSearchContext(query, puter) {
    var q = String(query || '').trim();
    if (!q) return '';
    try {
      if (puter && puter.ai && typeof puter.ai.webSearch === 'function') {
        var ws = await puter.ai.webSearch(q);
        var items = normalizeWebSearchItems(ws).slice(0, 5);
        if (items.length) return formatSearchItems(items);
      }
      if (puter && puter.ai && typeof puter.ai.search === 'function') {
        var s = await puter.ai.search(q);
        var items2 = normalizeWebSearchItems(s).slice(0, 5);
        if (items2.length) return formatSearchItems(items2);
      }
    } catch (err) {
      warn('web_search:tool_call_fail', normalizeError(err));
    }
    return await buildFallbackWebContext(q);
  }

  async function loadActiveSystemPrompt() {
    try {
      var res = await fetch(window.location.pathname + '?action=get_prompt', { credentials: 'same-origin' });
      var data = await res.json();
      if (data && data.success && typeof data.system_prompt === 'string' && data.system_prompt.trim() !== '') {
        activeSystemPrompt = data.system_prompt;
      } else {
        activeSystemPrompt = DEFAULT_SYS_PROMPT;
      }
      canEditSystemPrompt = !!(data && data.can_edit_system_prompt);
      return activeSystemPrompt;
    } catch (err) {
      warn('prompt:load_fail', normalizeError(err));
      activeSystemPrompt = DEFAULT_SYS_PROMPT;
      return activeSystemPrompt;
    }
  }

  window.addEventListener('error', function (ev) { error('window.onerror', ev && ev.message ? ev.message : ev); });
  window.addEventListener('unhandledrejection', function (ev) { error('unhandledrejection', ev && ev.reason ? ev.reason : ev); });

  function loadPuterSdk() {
    log('loadPuterSdk:start');
    return new Promise(function (resolve, reject) {
      if (window.puter && window.puter.ai && typeof window.puter.ai.chat === 'function') {
        log('loadPuterSdk:already_loaded');
        resolve(window.puter);
        return;
      }

      var existing = document.querySelector('script[data-puter-sdk="1"]');
      if (existing) {
        log('loadPuterSdk:script_already_exists_waiting_load');
        existing.addEventListener('load', function () { resolve(window.puter); }, { once: true });
        existing.addEventListener('error', function () { reject(new Error('Falha ao carregar Puter SDK')); }, { once: true });
        return;
      }

      var script = document.createElement('script');
      script.src = PUTER_SDK_URL;
      script.async = true;
      script.defer = true;
      script.dataset.puterSdk = '1';
      script.onload = function () {
        if (window.puter && window.puter.ai && typeof window.puter.ai.chat === 'function') resolve(window.puter);
        else reject(new Error('Puter SDK carregado, mas API de chat indisponível.'));
      };
      script.onerror = function () { reject(new Error('Falha ao carregar Puter SDK')); };
      document.head.appendChild(script);
      log('loadPuterSdk:script_injected', PUTER_SDK_URL);
    });
  }

  function getContextFromUrl() {
    var p = new URLSearchParams(window.location.search || '');
    function readInt(name) {
      var v = p.get(name);
      return v && /^\d+$/.test(v) ? parseInt(v, 10) : null;
    }
    return {
      id_criador: readInt('id_criador'),
      id_paciente: readInt('id_paciente'),
      id_atendimento: readInt('id_atendimento'),
      id_receita: readInt('id_receita')
    };
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function renderMarkdown(text) {
    if (window.marked && typeof window.marked.parse === 'function') {
      return window.marked.parse(String(text || ''));
    }
    var html = escapeHtml(text || '');
    html = html.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/\n/g, '<br>');
    return html;
  }

  function createUI() {
    var style = document.createElement('style');
    style.textContent = [
      '.cfo-fab{position:fixed;right:18px;bottom:18px;width:56px;height:56px;border-radius:50%;',
      'border:none;background:#0b57d0;color:#fff;font-size:24px;cursor:pointer;z-index:99999;',
      'box-shadow:0 6px 24px rgba(0,0,0,.25)}',
      '.cfo-toast{position:fixed;right:18px;bottom:86px;width:360px;max-width:calc(100vw - 24px);height:500px;',
      'background:#fff;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.25);display:none;flex-direction:column;',
      'overflow:hidden;z-index:99999;border:1px solid #e7e7e7;font-family:Arial,sans-serif;position:relative}',
      '.cfo-page{width:100%;height:70vh;max-height:70vh;background:#fff;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.08);display:flex;flex-direction:column;',
      'overflow:hidden;border:1px solid #e7e7e7;font-family:Arial,sans-serif;position:relative}',
      '.cfo-header{background:#0b57d0;color:#fff;padding:10px 12px;font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px}',
      '#ow-menu-toggle{cursor:pointer;font-size:20px;background:none;border:none;color:#fff;line-height:1;padding:0;margin:0 6px 0 0}',
      '.cfo-model-wrap{padding:8px 10px;border-bottom:1px solid #ebedf0;background:#f7f9ff}',
      '.cfo-model-label{display:block;font-size:11px;color:#344;margin-bottom:4px}',
      '.cfo-model-select{width:100%;padding:7px;border:1px solid #ccd4e2;border-radius:8px;background:#fff;font-size:12px}',
      '.cfo-body{flex:1;overflow:auto;padding:10px;background:#f8f9fb}',
      '.cfo-msg{max-width:88%;padding:8px 10px;margin:0 0 8px;border-radius:10px;white-space:pre-wrap;line-height:1.35;font-size:13px}',
      '.cfo-msg-head{font-size:10px;font-weight:600;opacity:.65;margin:0 0 5px 0;text-transform:uppercase;letter-spacing:.03em}',
      '.cfo-user{margin-left:auto;background:#dbe9ff;color:#11326f}',
      '.cfo-assistant{background:#fff;color:#222;border:1px solid #ebebeb}',
      '.cfo-footer{display:flex;gap:8px;padding:10px;border-top:1px solid #eee;background:#fff}',
      '.cfo-attach-btn{width:40px;height:40px;border:1px solid #d3d7e3;border-radius:8px;background:#f4f7ff;cursor:pointer;font-size:18px;line-height:1}',
      '.cfo-input{flex:1;min-height:38px;max-height:90px;padding:8px;border:1px solid #d9d9d9;border-radius:8px;resize:vertical;font-size:13px}',
      '.cfo-send{border:none;border-radius:8px;background:#0b57d0;color:#fff;padding:0 12px;cursor:pointer;font-weight:600}',
      '.cfo-send[disabled]{opacity:.6;cursor:not-allowed}',
      '.cfo-attach-preview{display:none;flex-wrap:wrap;gap:6px;padding:6px 10px 0;background:#fff}',
      '.cfo-attach-preview.has-items{display:flex}',
      '.cfo-chip{display:inline-flex;align-items:center;gap:6px;border:1px solid #cdd5ea;background:#eef3ff;color:#1a3b77;border-radius:14px;padding:3px 8px;font-size:11px}',
      '.cfo-chip-x{cursor:pointer;font-weight:700}',
      '#ow-sidebar{position:absolute;top:0;left:0;width:0;height:100%;background:#fff;z-index:40;transition:width .25s;overflow:hidden;border-right:1px solid #e9edf3;box-shadow:2px 0 6px rgba(0,0,0,.08)}',
      '#ow-sidebar.open{width:90%}',
      '.sb-view{display:none;height:100%}',
      '.sb-view.active{display:block}',
      '.sb-content{padding:12px;height:100%;overflow:auto}',
      '.sb-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;font-weight:700}',
      '.sb-close-btn{border:none;background:none;font-size:18px;cursor:pointer}',
      '.sb-menu-item{padding:12px;border:1px solid #eef1f6;border-radius:8px;margin-bottom:8px;cursor:pointer;background:#fff}',
      '.sb-menu-item:hover{background:#f7f9ff}',
      '.ow-side-ta{width:100%;height:50vh;font-family:monospace;font-size:12px}',
      '.ow-side-actions{margin-top:8px;display:flex;gap:8px}',
      '@media (max-width:768px){.cfo-page{height:78vh;max-height:78vh}}'
    ].join('');
    document.head.appendChild(style);

    var rootHost = null;
    var fab = null;

    if (RENDER_MODE === 'page') {
      var targetSel = window.__CHATGPT_FREE_OPENAI_CONTAINER || '#chatgpt-free-openai-page-root';
      rootHost = document.querySelector(targetSel) || document.body;
    } else {
      fab = document.createElement('button');
      fab.className = 'cfo-fab';
      fab.type = 'button';
      fab.title = 'Abrir Chat';
      fab.textContent = '💬';
      document.body.appendChild(fab);
      rootHost = document.body;
    }

    var rootClass = (RENDER_MODE === 'page') ? 'cfo-page' : 'cfo-toast';
    var defaultDisplay = (RENDER_MODE === 'page') ? 'flex' : 'none';

    var panel = document.createElement('div');
    panel.className = rootClass;
    panel.style.display = defaultDisplay;
    panel.innerHTML = [
      '<div id="ow-sidebar">',
      '  <div id="sb-view-menu" class="sb-view active sb-content">',
      '    <div class="sb-head"><span>Menu IA</span><button type="button" class="sb-close-btn" id="ow-sidebar-close">×</button></div>',
      '    <div class="sb-menu-item" id="sb-reset-chat">🧹 Reiniciar chat</div>',
      '    <div class="sb-menu-item" id="sb-open-prompts">✏️ Personalizar IA</div>',
      '  </div>',
      '  <div id="sb-view-prompts" class="sb-view sb-content">',
      '    <div class="sb-head"><span>Prompt do sistema</span><button type="button" class="sb-close-btn" id="sb-back-menu">←</button></div>',
      '    <textarea id="cfo-system-prompt" class="ow-side-ta"></textarea>',
      '    <div class="ow-side-actions">',
      '      <button type="button" id="cfo-save-system-prompt">Salvar Prompt</button>',
      '      <button type="button" id="cfo-reset-system-prompt">Restaurar Padrão</button>',
      '    </div>',
      '  </div>',
      '</div>',
      '<div class="cfo-header"><button id="ow-menu-toggle" type="button">☰</button><span>Chat (Puter/OpenAI)</span></div>',
      '<div class="cfo-model-wrap">',
      '  <label class="cfo-model-label" for="cfo-model">Modelo LLM</label>',
      '  <select class="cfo-model-select" id="cfo-model"><option value="">Buscando modelos...</option></select>',
      '</div>',
      '<div class="cfo-body" id="cfo-body"></div>',
      '<div class="cfo-attach-preview" id="cfo-attach-preview"></div>',
      '<div class="cfo-footer">',
      '  <input type="file" id="cfo-file-input" multiple style="display:none" accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.csv,.txt,.json,.xml">',
      '  <button class="cfo-attach-btn" id="cfo-attach-btn" type="button" title="Anexar arquivos">📎</button>',
      '  <textarea class="cfo-input" id="cfo-input" placeholder="Digite sua pergunta..."></textarea>',
      '  <button class="cfo-send" id="cfo-send" type="button">Enviar</button>',
      '</div>'
    ].join('');

    rootHost.appendChild(panel);

    return {
      fab: fab,
      toast: panel,
      body: panel.querySelector('#cfo-body'),
      attachPreview: panel.querySelector('#cfo-attach-preview'),
      attachBtn: panel.querySelector('#cfo-attach-btn'),
      fileInput: panel.querySelector('#cfo-file-input'),
      input: panel.querySelector('#cfo-input'),
      send: panel.querySelector('#cfo-send'),
      model: panel.querySelector('#cfo-model'),
      menuToggle: panel.querySelector('#ow-menu-toggle'),
      sidebar: panel.querySelector('#ow-sidebar'),
      sidebarClose: panel.querySelector('#ow-sidebar-close'),
      sidebarMenuView: panel.querySelector('#sb-view-menu'),
      sidebarPromptView: panel.querySelector('#sb-view-prompts'),
      sidebarResetChat: panel.querySelector('#sb-reset-chat'),
      sidebarOpenPrompts: panel.querySelector('#sb-open-prompts'),
      sidebarBackMenu: panel.querySelector('#sb-back-menu'),
      promptEl: panel.querySelector('#cfo-system-prompt'),
      promptSaveBtn: panel.querySelector('#cfo-save-system-prompt'),
      promptResetBtn: panel.querySelector('#cfo-reset-system-prompt')
    };
  }

  function addMessage(container, text, role, metaLabel) {
    var el = document.createElement('div');
    el.className = 'cfo-msg ' + (role === 'user' ? 'cfo-user' : 'cfo-assistant');
    if (role === 'assistant') {
      if (metaLabel) {
        var head = document.createElement('div');
        head.className = 'cfo-msg-head';
        head.textContent = metaLabel;
        el.appendChild(head);
      }
      var body = document.createElement('div');
      body.innerHTML = renderMarkdown(text);
      el.appendChild(body);
    } else {
      el.textContent = text;
    }
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }

  function tryParseJsonString(raw) {
    if (typeof raw !== 'string') return null;
    var t = raw.trim();
    if (!t) return null;
    if (!((t[0] === '{' && t[t.length - 1] === '}') || (t[0] === '[' && t[t.length - 1] === ']'))) return null;
    try { return JSON.parse(t); } catch (_) { return null; }
  }

  function extractTextFromContentArray(contentArray) {
    if (!Array.isArray(contentArray)) return '';
    return contentArray.map(function (item) {
      if (!item) return '';
      if (typeof item === 'string') return item;
      if (typeof item.text === 'string') return item.text;
      if (item.text && typeof item.text.value === 'string') return item.text.value;
      if (item.type === 'text' && typeof item.value === 'string') return item.value;
      if (item.type === 'output_text' && typeof item.text === 'string') return item.text;
      return '';
    }).filter(Boolean).join('\n');
  }

  function extractAssistantText(payload) {
    if (!payload) return '';
    if (typeof payload === 'string') return payload;
    if (Array.isArray(payload)) {
      return payload.map(function (part) { return extractAssistantText(part); }).filter(Boolean).join('\n');
    }
    if (typeof payload !== 'object') return '';

    if (typeof payload.text === 'string') return payload.text;
    if (typeof payload.output_text === 'string') return payload.output_text;
    if (typeof payload.answer === 'string') return payload.answer;
    if (typeof payload.response === 'string') return payload.response;
    if (typeof payload.message === 'string') return payload.message;

    if (payload.message && typeof payload.message === 'object') {
      if (typeof payload.message.content === 'string') return payload.message.content;
      var fromMessageContent = extractTextFromContentArray(payload.message.content);
      if (fromMessageContent) return fromMessageContent;
      var nestedMessage = extractAssistantText(payload.message);
      if (nestedMessage) return nestedMessage;
    }

    if (Array.isArray(payload.content)) {
      var fromContentArray = extractTextFromContentArray(payload.content);
      if (fromContentArray) return fromContentArray;
    }
    if (Array.isArray(payload.choices) && payload.choices[0]) {
      var fromChoice = extractAssistantText(payload.choices[0].message || payload.choices[0].delta || payload.choices[0]);
      if (fromChoice) return fromChoice;
    }
    return '';
  }

  function normalizeAssistantText(result) {
    if (!result) return 'Sem resposta.';

    if (typeof result === 'string') {
      var parsed = tryParseJsonString(result);
      if (parsed) {
        var parsedText = extractAssistantText(parsed);
        if (parsedText) return parsedText;
      }
      return result;
    }

    var extracted = extractAssistantText(result);
    if (extracted) return extracted;

    if (typeof result.toString === 'function') {
      var maybeString = String(result.toString());
      var parsedMaybe = tryParseJsonString(maybeString);
      if (parsedMaybe) {
        var parsedMaybeText = extractAssistantText(parsedMaybe);
        if (parsedMaybeText) return parsedMaybeText;
      }
      if (maybeString && maybeString !== '[object Object]') return maybeString;
    }

    return 'Sem conteúdo textual na resposta da LLM.';
  }

  function parseModelCandidates(raw) {
    var out = [];

    if (Array.isArray(raw)) {
      raw.forEach(function (m) {
        if (typeof m === 'string') {
          out.push({ name: m, displayName: m });
        } else if (m && typeof m === 'object') {
          var name = m.id || m.name || m.model || m.slug || '';
          if (name) out.push({ name: String(name), displayName: String(m.displayName || m.label || name) });
        }
      });
    } else if (raw && typeof raw === 'object') {
      ['models', 'data', 'items', 'list'].some(function (k) {
        if (Array.isArray(raw[k])) {
          out = parseModelCandidates(raw[k]);
          return true;
        }
        return false;
      });

      if (!out.length && raw.id) out.push({ name: String(raw.id), displayName: String(raw.id) });
      if (!out.length && raw.name) out.push({ name: String(raw.name), displayName: String(raw.name) });
    }

    var dedupe = {};
    return out.filter(function (m) {
      if (!m.name || dedupe[m.name]) return false;
      dedupe[m.name] = true;
      return true;
    });
  }

  function scoreModelFreshness(name) {
    var n = String(name || '').toLowerCase();
    var score = 0;
    if (/latest|new|current|preview/.test(n)) score += 10000;
    if (/gpt-5|gpt5|o3|o4/.test(n)) score += 5000;

    var nums = n.match(/\d+(?:\.\d+)?/g) || [];
    nums.forEach(function (num, idx) {
      var v = parseFloat(num);
      if (!Number.isNaN(v)) score += v * Math.pow(10, Math.max(0, 2 - idx));
    });
    return score;
  }

  function chooseDefaultModel(models) {
    if (!models || !models.length) return '';
    var copy = models.slice();
    copy.sort(function (a, b) {
      return scoreModelFreshness(b.name) - scoreModelFreshness(a.name);
    });
    return copy[0].name;
  }

  async function fetchAvailableModels(puter) {
    var candidates = [];
    log('fetchAvailableModels:start');
    try {
      if (puter && puter.ai) {
        if (typeof puter.ai.models === 'function') candidates = parseModelCandidates(await puter.ai.models());
        if (!candidates.length && typeof puter.ai.listModels === 'function') candidates = parseModelCandidates(await puter.ai.listModels());
        if (!candidates.length && Array.isArray(puter.ai.models)) candidates = parseModelCandidates(puter.ai.models);
      }
    } catch (err) {
      warn('fetchAvailableModels:error', err);
    }

    if (!candidates.length) {
      candidates = [
        { name: 'gpt-5', displayName: 'gpt-5' },
        { name: 'gpt-5-mini', displayName: 'gpt-5-mini' },
        { name: 'gpt-4.1', displayName: 'gpt-4.1' },
        { name: 'gpt-4o-mini', displayName: 'gpt-4o-mini' }
      ];
    }

    log('fetchAvailableModels:done', candidates.map(function (m) { return m.name; }));
    return candidates;
  }

  function getWorkingModelsFromCache(models) {
    try {
      var raw = localStorage.getItem(KEY_WORKING_MODELS_CACHE);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.models) || !parsed.ts) return null;
      if ((Date.now() - parsed.ts) > (6 * 60 * 60 * 1000)) return null;
      var allowed = {};
      parsed.models.forEach(function (m) { allowed[m] = true; });
      return {
        ts: parsed.ts,
        models: models.filter(function (m) { return allowed[m.name]; })
      };
    } catch (_) {
      return null;
    }
  }

  function saveWorkingModelsCache(models) {
    try {
      localStorage.setItem(KEY_WORKING_MODELS_CACHE, JSON.stringify({
        ts: Date.now(),
        models: models.map(function (m) { return m.name; })
      }));
    } catch (_) {}
  }

  function withTimeout(promise, ms) {
    return Promise.race([
      promise,
      new Promise(function (_, reject) {
        setTimeout(function () { reject(new Error('timeout ' + ms + 'ms')); }, ms);
      })
    ]);
  }

  async function probeModelWithRetry(puter, modelName, attempts) {
    var lastErr = null;
    for (var i = 0; i < attempts; i += 1) {
      try {
        var probeResult = await withTimeout(
          puter.ai.chat([{ role: 'user', content: 'Responda apenas: OK' }], { model: modelName }),
          15000
        );
        var probeText = normalizeAssistantText(probeResult);
        if (probeText && String(probeText).trim() !== '') return true;
      } catch (err) {
        lastErr = err;
      }
    }
    if (lastErr) throw lastErr;
    throw new Error('modelo sem resposta útil');
  }

  async function filterWorkingModels(puter, models, onProgress) {
    if (!models || !models.length) return [];
    var fromCache = getWorkingModelsFromCache(models);
    var workingMap = {};
    var working = [];
    if (fromCache && fromCache.models.length) {
      fromCache.models.forEach(function (m) {
        workingMap[m.name] = m;
      });
      working = Object.keys(workingMap).map(function (k) { return workingMap[k]; });
      log('filterWorkingModels:cache_hit', working.map(function (m) { return m.name; }));
      if (typeof onProgress === 'function') onProgress(working.slice(), true);
    }

    var prioritized = models.slice().sort(function (a, b) {
      return scoreModelFreshness(b.name) - scoreModelFreshness(a.name);
    }).slice(0, 16);
    log('filterWorkingModels:start', prioritized.map(function (m) { return m.name; }));

    for (var i = 0; i < prioritized.length; i += 1) {
      var m = prioritized[i];
      if (workingMap[m.name]) continue;
      try {
        await probeModelWithRetry(puter, m.name, 2);
        workingMap[m.name] = m;
        working = Object.keys(workingMap).map(function (k) { return workingMap[k]; });
        log('filterWorkingModels:ok', m.name);
        if (typeof onProgress === 'function') onProgress(working.slice(), false, m);
      } catch (err) {
        warn('filterWorkingModels:fail', m.name, normalizeError(err));
      }
    }

    working = models.filter(function (m) { return !!workingMap[m.name]; });
    if (working.length) saveWorkingModelsCache(working);
    log('filterWorkingModels:done', working.map(function (m) { return m.name; }));
    return working;
  }

  function saveSelectedModel(value, isManual) {
    localStorage.setItem(KEY_MODEL, value || '');
    localStorage.setItem(KEY_MODEL_MANUAL, isManual ? '1' : '0');
  }

  function applyModelSelect(selectEl, models) {
    selectEl.innerHTML = '';

    var savedModel = localStorage.getItem(KEY_MODEL) || '';
    var manualModelSelection = localStorage.getItem(KEY_MODEL_MANUAL) === '1';
    var defaultLatest = chooseDefaultModel(models);

    if (!manualModelSelection || !savedModel || !models.some(function (m) { return m.name === savedModel; })) {
      savedModel = defaultLatest;
      saveSelectedModel(savedModel, false);
    }

    models.forEach(function (m) {
      var opt = document.createElement('option');
      opt.value = m.name;
      opt.textContent = m.displayName || m.name;
      if (m.name === savedModel) opt.selected = true;
      selectEl.appendChild(opt);
    });

    selectEl.onchange = function () {
      saveSelectedModel(selectEl.value || '', true);
    };

    return savedModel;
  }

  function renderModelSelectIncremental(selectEl, models, preferredModelName) {
    selectEl.innerHTML = '';
    var currentValue = selectEl.value || '';
    var savedValue = localStorage.getItem(KEY_MODEL) || '';
    var preferred = preferredModelName || savedValue || '';
    var selected = '';

    if (models.some(function (m) { return m.name === currentValue; })) selected = currentValue;
    else if (preferred && models.some(function (m) { return m.name === preferred; })) selected = preferred;
    else if (models.length) selected = models[0].name;

    models.forEach(function (m) {
      var opt = document.createElement('option');
      opt.value = m.name;
      opt.textContent = m.displayName || m.name;
      if (m.name === selected) opt.selected = true;
      selectEl.appendChild(opt);
    });

    if (!selectEl.dataset.cfoBound) {
      selectEl.onchange = function () {
        saveSelectedModel(selectEl.value || '', true);
      };
      selectEl.dataset.cfoBound = '1';
    }

    if (selected && selectEl.value !== selected) selectEl.value = selected;
    return selected;
  }

  function init() {
    log('init:start', { mode: RENDER_MODE, href: window.location.href });
    var ui = createUI();
    var open = false;
    var loading = false;
    var history = [];
    var shouldSendSystemPromptOnNextMessage = true;
    var pendingAttachments = [];
    var attachmentsSupported = null;
    var context = getContextFromUrl();
    var preferredSavedModel = localStorage.getItem(KEY_MODEL) || '';
    log('context:url', context);
    loadActiveSystemPrompt().then(function (promptTxt) {
      var promptEl = ui.promptEl;
      if (promptEl) {
        promptEl.value = promptTxt || DEFAULT_SYS_PROMPT;
        promptEl.disabled = !canEditSystemPrompt;
      }
      var saveBtn = ui.promptSaveBtn;
      if (saveBtn) {
        saveBtn.disabled = !canEditSystemPrompt;
        saveBtn.onclick = async function () {
          try {
            var val = (promptEl && promptEl.value ? promptEl.value : '').trim() || DEFAULT_SYS_PROMPT;
            var r = await fetch(window.location.pathname + '?action=save_prompt', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              credentials: 'same-origin',
              body: JSON.stringify({ tipo: 'system', conteudo: val })
            });
            var d = await r.json();
            if (!d || !d.success) throw new Error((d && d.error) || 'Falha ao salvar prompt');
            activeSystemPrompt = val;
            alert('Prompt salvo com sucesso.');
          } catch (err) {
            alert('Erro ao salvar prompt: ' + normalizeError(err));
          }
        };
      }
      var resetBtn = ui.promptResetBtn;
      if (resetBtn) {
        resetBtn.disabled = !canEditSystemPrompt;
        resetBtn.onclick = function () {
          if (promptEl) promptEl.value = DEFAULT_SYS_PROMPT;
        };
      }
      if (ui.menuToggle) ui.menuToggle.style.display = canEditSystemPrompt ? '' : 'none';
      if (ui.sidebar && !canEditSystemPrompt) ui.sidebar.style.display = 'none';
    });

    if (ui.menuToggle && ui.sidebar) {
      ui.menuToggle.onclick = function () {
        if (ui.sidebarMenuView && ui.sidebarPromptView) {
          ui.sidebarMenuView.classList.add('active');
          ui.sidebarPromptView.classList.remove('active');
        }
        ui.sidebar.classList.add('open');
      };
    }
    if (ui.sidebarClose && ui.sidebar) {
      ui.sidebarClose.onclick = function () { ui.sidebar.classList.remove('open'); };
    }
    if (ui.sidebarOpenPrompts && ui.sidebarMenuView && ui.sidebarPromptView) {
      ui.sidebarOpenPrompts.onclick = function () {
        ui.sidebarMenuView.classList.remove('active');
        ui.sidebarPromptView.classList.add('active');
      };
    }
    if (ui.sidebarBackMenu && ui.sidebarMenuView && ui.sidebarPromptView) {
      ui.sidebarBackMenu.onclick = function () {
        ui.sidebarPromptView.classList.remove('active');
        ui.sidebarMenuView.classList.add('active');
      };
    }

    async function resetChatContext() {
      var model = ui.model.value || localStorage.getItem(KEY_MODEL) || '';
      var response = await fetch(window.location.pathname + '?action=delete_chat_history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          id_criador: context.id_criador,
          id_paciente: context.id_paciente,
          id_atendimento: context.id_atendimento,
          id_receita: context.id_receita,
          model: model
        })
      });
      var payload = await response.json();
      if (!payload || !payload.success || !payload.deleted_rows) {
        throw new Error((payload && payload.error) || 'Falha ao excluir histórico no banco.');
      }
      history = [];
      shouldSendSystemPromptOnNextMessage = true;
      pendingAttachments = [];
      renderAttachments();
      ui.body.innerHTML = '';
      ui.input.value = '';
      if (ui.sidebar) ui.sidebar.classList.remove('open');
      alert('Chat reiniciado com sucesso. Histórico removido do banco de dados.');
    }

    if (ui.sidebarResetChat) {
      ui.sidebarResetChat.onclick = function () {
        var ok = window.confirm('Deseja reiniciar o chat e apagar o contexto atual?');
        if (!ok) return;
        resetChatContext().catch(function (err) {
          error('reset_chat:error', err);
          alert('Não foi possível reiniciar o chat: ' + normalizeError(err));
        });
      };
    }

    function serializePersistableHistory() {
      return history.filter(function (m) { return m && typeof m.content === 'string' && m.content.trim() !== ''; });
    }

    function renderAttachments() {
      ui.attachPreview.innerHTML = '';
      if (!pendingAttachments.length) {
        ui.attachPreview.classList.remove('has-items');
        return;
      }
      ui.attachPreview.classList.add('has-items');
      pendingAttachments.forEach(function (att, idx) {
        var chip = document.createElement('div');
        chip.className = 'cfo-chip';
        chip.innerHTML = '<span>' + att.name + '</span><span class="cfo-chip-x" data-idx="' + idx + '">×</span>';
        ui.attachPreview.appendChild(chip);
      });
    }

    function fileToDataUrl(file) {
      return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () { resolve(String(reader.result || '')); };
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }

    async function appendFiles(fileList) {
      var files = Array.prototype.slice.call(fileList || []);
      for (var i = 0; i < files.length; i += 1) {
        var file = files[i];
        try {
          var dataUrl = await fileToDataUrl(file);
          pendingAttachments.push({
            name: file.name || ('arquivo_' + (pendingAttachments.length + 1)),
            type: file.type || 'application/octet-stream',
            size: file.size || 0,
            dataUrl: dataUrl,
            file: file
          });
          log('attachment:add', { name: file.name, type: file.type, size: file.size });
        } catch (err) {
          warn('attachment:read_fail', file && file.name, err);
        }
      }
      renderAttachments();
    }

    async function uploadAttachmentsToPuter(puter, attachmentPayload) {
      var uploaded = [];
      if (!attachmentPayload || !attachmentPayload.length) return uploaded;
      if (!puter || !puter.fs || typeof puter.fs.write !== 'function') {
        throw new Error('Upload de arquivos não suportado pelo Puter neste ambiente.');
      }
      for (var i = 0; i < attachmentPayload.length; i += 1) {
        var att = attachmentPayload[i];
        if (!att || !att.file) continue;
        var safeName = String(att.name || ('arquivo_' + (i + 1))).replace(/[^a-zA-Z0-9._-]/g, '_');
        var tempPath = '~/.conexaovida_tmp_' + Date.now() + '_' + i + '_' + safeName;
        var written = await puter.fs.write(tempPath, att.file);
        uploaded.push({
          name: att.name || safeName,
          puter_path: (written && (written.path || written.fullPath || written.abspath)) || tempPath
        });
      }
      return uploaded;
    }

    async function cleanupUploadedFiles(puter, uploadedFiles) {
      if (!uploadedFiles || !uploadedFiles.length) return;
      if (!puter || !puter.fs || typeof puter.fs.delete !== 'function') return;
      for (var i = 0; i < uploadedFiles.length; i += 1) {
        var item = uploadedFiles[i];
        if (!item || !item.puter_path) continue;
        try {
          await puter.fs.delete(item.puter_path);
        } catch (err) {
          warn('attachment:cleanup_fail', item.puter_path, normalizeError(err));
        }
      }
    }

    async function persistHistory() {
      try {
        var snapshot = serializePersistableHistory();
        log('persistHistory:start', { count: snapshot.length, context: context });
        await fetch(window.location.pathname + '?action=save_chat_history', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
            body: JSON.stringify({
            url_atual: window.location.href,
            id_criador: context.id_criador,
            id_paciente: context.id_paciente,
            id_atendimento: context.id_atendimento,
            id_receita: context.id_receita,
            model: ui.model.value || localStorage.getItem(KEY_MODEL) || '',
            messages: snapshot
          })
        });
        log('persistHistory:ok');
      } catch (err) {
        warn('persistHistory:error', err);
      }
    }

    async function loadHistory() {
      try {
        var response = await fetch(window.location.pathname + '?action=get_chat_history', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({
            id_criador: context.id_criador,
            id_paciente: context.id_paciente,
            id_atendimento: context.id_atendimento,
            id_receita: context.id_receita,
            model: ui.model.value || localStorage.getItem(KEY_MODEL) || ''
          })
        });
        var payload = await response.json();
        var loaded = payload && Array.isArray(payload.messages) ? payload.messages : [];
        log('loadHistory:ok', { count: loaded.length, payload: payload });
        loaded.forEach(function (m) {
          if (!m || (m.role !== 'user' && m.role !== 'assistant')) return;
          var text = '';
          if (m.role === 'assistant') {
            text = normalizeAssistantText(m.content);
          } else {
            text = typeof m.content === 'string' ? m.content : '';
          }
          if (!text) return;
          history.push({ role: m.role, content: text });
          addMessage(ui.body, text, m.role);
        });
        if (loaded.length) shouldSendSystemPromptOnNextMessage = false;
        return loaded.length > 0;
      } catch (err) {
        warn('loadHistory:error', err);
        return false;
      }
    }

    function toggleToast() {
      if (RENDER_MODE === 'page') return;
      open = !open;
      ui.toast.style.display = open ? 'flex' : 'none';
      if (open) ui.input.focus();
    }

    if (ui.fab) ui.fab.addEventListener('click', toggleToast);

    ui.model.innerHTML = '<option value="">Validando modelos funcionais...</option>';
    loadPuterSdk()
      .then(function (puter) {
        return fetchAvailableModels(puter).then(function (models) {
          return filterWorkingModels(puter, models, function (partialWorking) {
            if (!partialWorking || !partialWorking.length) return;
            var partialActive = renderModelSelectIncremental(ui.model, partialWorking, preferredSavedModel);
            if (preferredSavedModel && partialActive === preferredSavedModel) {
              log('model:preferred_saved_available', preferredSavedModel);
            }
          });
        });
      })
      .then(function (models) {
        if (!models.length) {
          ui.model.innerHTML = '<option value="">Sem modelos funcionais</option>';
          addMessage(ui.body, 'Nenhum modelo funcional disponível no momento.', 'assistant');
          return;
        }
        var active = applyModelSelect(ui.model, models);
        log('model:active', active);
      })
      .catch(function (err) {
        ui.model.innerHTML = '<option value="">Erro ao carregar modelos</option>';
        addMessage(ui.body, 'Não foi possível listar modelos: ' + normalizeError(err), 'assistant');
        error('model:load_error', err);
      });

    async function sendMessage() {
      var prompt = ui.input.value.trim();
      if (!prompt || loading) return;

      loading = true;
      ui.send.disabled = true;
      log('send:start', { prompt: prompt, historyLen: history.length, attachments: pendingAttachments.length });
      addMessage(ui.body, prompt, 'user');
      history.push({ role: 'user', content: prompt });
      if (history.length > MAX_HISTORY) history = history.slice(history.length - MAX_HISTORY);
      ui.input.value = '';
      persistHistory();

      var puter = null;
      var uploadedFiles = [];
      try {
        puter = await loadPuterSdk();
        var model = ui.model.value || localStorage.getItem(KEY_MODEL) || 'gpt-5';
        var wantsWebSearch = false;
        var promptForModel = prompt;
        var toolDecision = await decideToolUseWithLLM(puter, model, prompt);
        if (toolDecision && Array.isArray(toolDecision.search_queries) && toolDecision.search_queries.length) {
          wantsWebSearch = true;
          var snippets = [];
          for (var qIdx = 0; qIdx < toolDecision.search_queries.length; qIdx += 1) {
            var sq = toolDecision.search_queries[qIdx];
            var qText = (sq && sq.query) ? String(sq.query) : '';
            if (!qText) continue;
            var webContext = await buildWebSearchContext(qText, puter);
            if (webContext) snippets.push('QUERY: ' + qText + '\n' + webContext);
          }
          if (snippets.length) {
            promptForModel = prompt + '\n\n[RESULTADOS_DE_BUSCA_WEB]\n' + snippets.join('\n\n') + '\n\nUse os resultados acima para responder objetivamente e cite URLs quando houver.';
            log('web_search:context_injected_by_llm_decision', toolDecision);
          } else {
            warn('web_search:no_context_from_tool');
          }
        } else if (toolDecision && Array.isArray(toolDecision.sql_queries) && toolDecision.sql_queries.length) {
          warn('tool_decision:sql_requested_but_not_implemented', toolDecision.sql_queries);
        }
        var requestHistory = history.slice();
        if (shouldSendSystemPromptOnNextMessage) {
          requestHistory.unshift({ role: 'system', content: activeSystemPrompt || DEFAULT_SYS_PROMPT });
        }
        var attachmentPayload = pendingAttachments.slice();
        if (attachmentPayload.length) {
          uploadedFiles = await uploadAttachmentsToPuter(puter, attachmentPayload);
          if (!uploadedFiles.length) throw new Error('Falha ao enviar anexos ao Puter.');
          requestHistory[requestHistory.length - 1] = {
            role: 'user',
            content: uploadedFiles.map(function (fileMeta) {
              return { type: 'file', puter_path: fileMeta.puter_path };
            }).concat([{ type: 'text', text: promptForModel }])
          };
        }
        if (!attachmentPayload.length) {
          requestHistory[requestHistory.length - 1] = { role: 'user', content: promptForModel };
        }

        log('send:calling_puter_ai_chat', { model: model, historyLen: requestHistory.length, attachmentCount: attachmentPayload.length, wantsWebSearch: wantsWebSearch });
        var result;
        try {
          result = await puter.ai.chat(requestHistory, buildChatOptions(model, wantsWebSearch));
          if (attachmentPayload.length) attachmentsSupported = true;
          if (wantsWebSearch) log('web_search:enabled_for_request');
        } catch (firstErr) {
          if (attachmentPayload.length) {
            throw firstErr;
          } else if (wantsWebSearch) {
            warn('web_search:primary_mode_fail_retrying_basic', normalizeError(firstErr));
            result = await puter.ai.chat(requestHistory, buildChatOptions(model, false));
          } else {
            throw firstErr;
          }
        }
        log('send:raw_result', result);
        var answer = normalizeAssistantText(result);
        history.push({ role: 'assistant', content: answer });
        shouldSendSystemPromptOnNextMessage = false;
        addMessage(ui.body, answer, 'assistant', model ? ('modelo: ' + model) : '');
        if (attachmentPayload.length) {
          pendingAttachments = [];
          renderAttachments();
          if (attachmentsSupported === true) log('attachments:supported');
        }
        persistHistory();
      } catch (err) {
        if (pendingAttachments.length) {
          attachmentsSupported = false;
          warn('attachments:not_supported_or_failed', normalizeError(err));
        }
        error('send:error', err);
        addMessage(ui.body, 'Erro ao consultar o chat: ' + normalizeError(err), 'assistant');
      } finally {
        await cleanupUploadedFiles(puter, uploadedFiles);
        loading = false;
        ui.send.disabled = false;
        log('send:finish');
      }
    }

    ui.send.addEventListener('click', sendMessage);
    ui.attachBtn.addEventListener('click', function () { ui.fileInput.click(); });
    ui.fileInput.addEventListener('change', function (ev) {
      appendFiles(ev.target.files);
      ev.target.value = '';
    });
    ui.attachPreview.addEventListener('click', function (ev) {
      var idx = ev.target && ev.target.dataset ? parseInt(ev.target.dataset.idx, 10) : -1;
      if (Number.isNaN(idx) || idx < 0) return;
      pendingAttachments.splice(idx, 1);
      renderAttachments();
    });
    ui.input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        sendMessage();
      }
    });
    ui.input.addEventListener('paste', function (ev) {
      var items = (ev.clipboardData && ev.clipboardData.items) ? ev.clipboardData.items : [];
      var files = [];
      for (var i = 0; i < items.length; i += 1) {
        if (items[i].kind === 'file') {
          var f = items[i].getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        ev.preventDefault();
        appendFiles(files);
      }
    });

    loadHistory().then(function (hasHistory) {
      if (!hasHistory) {
        addMessage(ui.body, 'Olá! Sou um chat em modo ' + (RENDER_MODE === 'page' ? 'página' : 'toast') + ' usando Puter. Como posso ajudar?', 'assistant');
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
