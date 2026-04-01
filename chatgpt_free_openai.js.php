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

if ($action === 'save_chat_history' || $action === 'get_chat_history') {
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

    @$db->query("ALTER TABLE chatgpt_chats ADD COLUMN chat_mode VARCHAR(20) NOT NULL DEFAULT 'assistant'");
    @$db->query("ALTER TABLE chatgpt_chats ADD COLUMN mensagens LONGTEXT NULL");
    $chat_mode_esc = $db->real_escape_string('free_openai');

    if ($ctx['id_atendimento']) {
        $where = "id_atendimento = " . intval($ctx['id_atendimento']) . " AND chat_mode = '$chat_mode_esc'";
    } elseif ($ctx['id_receita']) {
        $where = "id_receita = " . intval($ctx['id_receita']) . " AND id_atendimento IS NULL AND chat_mode = '$chat_mode_esc'";
    } elseif ($ctx['id_paciente']) {
        $where = "id_paciente = " . intval($ctx['id_paciente']) . " AND id_atendimento IS NULL AND id_receita IS NULL AND chat_mode = '$chat_mode_esc'";
    } else {
        $where = "id_criador = " . intval($ctx['id_criador']) . " AND id_atendimento IS NULL AND id_receita IS NULL AND id_paciente IS NULL AND chat_mode = '$chat_mode_esc'";
    }

    if ($action === 'get_chat_history') {
        $sql = "SELECT mensagens FROM chatgpt_chats WHERE $where ORDER BY datetime_atualizacao DESC, id DESC LIMIT 1";
        $result = $db->query($sql);
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

    $messages = isset($data['messages']) && is_array($data['messages']) ? $data['messages'] : [];
    $messages_esc = $db->real_escape_string(json_encode($messages, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    $url_atual_esc = $db->real_escape_string($data['url_atual'] ?? ($_SERVER['HTTP_REFERER'] ?? ''));
    $titulo_esc = $db->real_escape_string('ConexaoVida IA - Free OpenAI');
    $id_chatgpt_esc = $db->real_escape_string('free_openai');
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

  function log() { console.log.apply(console, ['%c' + FILE_PREFIX + ' [LOG]', 'color:#1976d2;font-weight:bold'].concat([].slice.call(arguments))); }
  function warn() { console.warn.apply(console, ['%c' + FILE_PREFIX + ' [WARN]', 'color:#f57c00;font-weight:bold'].concat([].slice.call(arguments))); }
  function error() { console.error.apply(console, ['%c' + FILE_PREFIX + ' [ERROR]', 'color:#d32f2f;font-weight:bold'].concat([].slice.call(arguments))); }
  function normalizeError(err) {
    if (!err) return 'erro desconhecido';
    if (typeof err === 'string') return err;
    if (err.message) return err.message;
    try { return JSON.stringify(err); } catch (_) { return String(err); }
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
      'overflow:hidden;z-index:99999;border:1px solid #e7e7e7;font-family:Arial,sans-serif}',
      '.cfo-page{width:100%;height:70vh;max-height:70vh;background:#fff;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.08);display:flex;flex-direction:column;',
      'overflow:hidden;border:1px solid #e7e7e7;font-family:Arial,sans-serif}',
      '.cfo-header{background:#0b57d0;color:#fff;padding:10px 12px;font-weight:700;font-size:14px}',
      '.cfo-model-wrap{padding:8px 10px;border-bottom:1px solid #ebedf0;background:#f7f9ff}',
      '.cfo-model-label{display:block;font-size:11px;color:#344;margin-bottom:4px}',
      '.cfo-model-select{width:100%;padding:7px;border:1px solid #ccd4e2;border-radius:8px;background:#fff;font-size:12px}',
      '.cfo-body{flex:1;overflow:auto;padding:10px;background:#f8f9fb}',
      '.cfo-msg{max-width:88%;padding:8px 10px;margin:0 0 8px;border-radius:10px;white-space:pre-wrap;line-height:1.35;font-size:13px}',
      '.cfo-user{margin-left:auto;background:#dbe9ff;color:#11326f}',
      '.cfo-assistant{background:#fff;color:#222;border:1px solid #ebebeb}',
      '.cfo-footer{display:flex;gap:8px;padding:10px;border-top:1px solid #eee;background:#fff}',
      '.cfo-input{flex:1;min-height:38px;max-height:90px;padding:8px;border:1px solid #d9d9d9;border-radius:8px;resize:vertical;font-size:13px}',
      '.cfo-send{border:none;border-radius:8px;background:#0b57d0;color:#fff;padding:0 12px;cursor:pointer;font-weight:600}',
      '.cfo-send[disabled]{opacity:.6;cursor:not-allowed}',
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
      '<div class="cfo-header">Chat (Puter/OpenAI)</div>',
      '<div class="cfo-model-wrap">',
      '  <label class="cfo-model-label" for="cfo-model">Modelo LLM</label>',
      '  <select class="cfo-model-select" id="cfo-model"><option value="">Buscando modelos...</option></select>',
      '</div>',
      '<div class="cfo-body" id="cfo-body"></div>',
      '<div class="cfo-footer">',
      '  <textarea class="cfo-input" id="cfo-input" placeholder="Digite sua pergunta..."></textarea>',
      '  <button class="cfo-send" id="cfo-send" type="button">Enviar</button>',
      '</div>'
    ].join('');

    rootHost.appendChild(panel);

    return {
      fab: fab,
      toast: panel,
      body: panel.querySelector('#cfo-body'),
      input: panel.querySelector('#cfo-input'),
      send: panel.querySelector('#cfo-send'),
      model: panel.querySelector('#cfo-model')
    };
  }

  function addMessage(container, text, role) {
    var el = document.createElement('div');
    el.className = 'cfo-msg ' + (role === 'user' ? 'cfo-user' : 'cfo-assistant');
    if (role === 'assistant') el.innerHTML = renderMarkdown(text);
    else el.textContent = text;
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }

  function normalizeAssistantText(result) {
    function extractTextFromContentArray(contentArray) {
      if (!Array.isArray(contentArray)) return '';
      return contentArray.map(function (item) {
        if (!item) return '';
        if (typeof item === 'string') return item;
        if (typeof item.text === 'string') return item.text;
        if (item.text && typeof item.text.value === 'string') return item.text.value;
        if (item.type === 'text' && typeof item.value === 'string') return item.value;
        return '';
      }).filter(Boolean).join('\n');
    }

    if (typeof result === 'string') return result;
    if (!result) return 'Sem resposta.';
    if (typeof result.message === 'string') return result.message;
    if (result.message && typeof result.message === 'object') {
      if (typeof result.message.content === 'string') return result.message.content;
      var fromMessageContent = extractTextFromContentArray(result.message.content);
      if (fromMessageContent) return fromMessageContent;
    }
    if (typeof result.text === 'string') return result.text;
    if (Array.isArray(result.choices) && result.choices[0] && result.choices[0].message && typeof result.choices[0].message.content === 'string') {
      return result.choices[0].message.content;
    }
    if (Array.isArray(result.content)) {
      var fromRootContent = extractTextFromContentArray(result.content);
      if (fromRootContent) return fromRootContent;
    }
    if (typeof result.toString === 'function') {
      var maybeString = String(result.toString());
      if (maybeString && maybeString !== '[object Object]') return maybeString;
    }
    return JSON.stringify(result, null, 2);
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
      return models.filter(function (m) { return allowed[m.name]; });
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

  async function filterWorkingModels(puter, models, onProgress) {
    if (!models || !models.length) return [];
    var fromCache = getWorkingModelsFromCache(models);
    if (fromCache && fromCache.length) {
      log('filterWorkingModels:cache_hit', fromCache.map(function (m) { return m.name; }));
      if (typeof onProgress === 'function') onProgress(fromCache.slice(), true);
      return fromCache;
    }

    var prioritized = models.slice().sort(function (a, b) {
      return scoreModelFreshness(b.name) - scoreModelFreshness(a.name);
    }).slice(0, 10);
    var working = [];
    log('filterWorkingModels:start', prioritized.map(function (m) { return m.name; }));

    for (var i = 0; i < prioritized.length; i += 1) {
      var m = prioritized[i];
      try {
        var probeResult = await withTimeout(
          puter.ai.chat([{ role: 'user', content: 'Responda apenas: OK' }], { model: m.name }),
          15000
        );
        var probeText = normalizeAssistantText(probeResult);
        if (probeText && String(probeText).trim() !== '') {
          working.push(m);
          log('filterWorkingModels:ok', m.name);
          if (typeof onProgress === 'function') onProgress(working.slice(), false, m);
        } else {
          warn('filterWorkingModels:empty_response', m.name);
        }
      } catch (err) {
        warn('filterWorkingModels:fail', m.name, normalizeError(err));
      }
    }

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
    var history = [
      { role: 'system', content: 'Você é um assistente útil. Responda em português do Brasil de forma objetiva.' }
    ];
    var context = getContextFromUrl();
    var preferredSavedModel = localStorage.getItem(KEY_MODEL) || '';
    log('context:url', context);

    function serializePersistableHistory() {
      return history.filter(function (m) { return m && m.role !== 'system' && typeof m.content === 'string' && m.content.trim() !== ''; });
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
            id_receita: context.id_receita
          })
        });
        var payload = await response.json();
        var loaded = payload && Array.isArray(payload.messages) ? payload.messages : [];
        log('loadHistory:ok', { count: loaded.length, payload: payload });
        loaded.forEach(function (m) {
          if (!m || (m.role !== 'user' && m.role !== 'assistant')) return;
          var text = typeof m.content === 'string' ? m.content : '';
          if (!text) return;
          history.push({ role: m.role, content: text });
          addMessage(ui.body, text, m.role);
        });
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
        if (active) addMessage(ui.body, 'Modelo ativo: ' + active, 'assistant');
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
      log('send:start', { prompt: prompt, historyLen: history.length });
      addMessage(ui.body, prompt, 'user');
      history.push({ role: 'user', content: prompt });
      if (history.length > MAX_HISTORY) history = [history[0]].concat(history.slice(history.length - (MAX_HISTORY - 1)));
      ui.input.value = '';
      persistHistory();

      try {
        var puter = await loadPuterSdk();
        var model = ui.model.value || localStorage.getItem(KEY_MODEL) || 'gpt-5';
        log('send:calling_puter_ai_chat', { model: model, historyLen: history.length });
        var result = await puter.ai.chat(history, { model: model });
        log('send:raw_result', result);
        var answer = normalizeAssistantText(result);
        history.push({ role: 'assistant', content: answer });
        addMessage(ui.body, answer, 'assistant');
        persistHistory();
      } catch (err) {
        error('send:error', err);
        addMessage(ui.body, 'Erro ao consultar o chat: ' + normalizeError(err), 'assistant');
      } finally {
        loading = false;
        ui.send.disabled = false;
        log('send:finish');
      }
    }

    ui.send.addEventListener('click', sendMessage);
    ui.input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        sendMessage();
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
