<?php
@ini_set('display_errors', 0);
error_reporting(0);

$currentFileName = basename(__FILE__);
$phpSelf = basename($_SERVER['PHP_SELF'] ?? '');
$secFetchDest = strtolower($_SERVER['HTTP_SEC_FETCH_DEST'] ?? '');
$requestedAsJs = (($_GET['as'] ?? '') === 'js');

$isDirectFileUrl = ($phpSelf === $currentFileName);
$isScriptFetch = $requestedAsJs || ($secFetchDest === 'script');
$shouldRenderDirectPage = $isDirectFileUrl && !$isScriptFetch;

function include_first_existing(array $paths): void {
    foreach ($paths as $p) {
        if (is_file($p)) {
            @include_once($p);
            return;
        }
    }
}

if ($shouldRenderDirectPage) {
    header('Content-Type: text/html; charset=UTF-8');
    date_default_timezone_set('America/Recife');

    include_first_existing([
        'config/config.php',
        '../config/config.php',
        '../../config/config.php',
        '../../../config/config.php',
    ]);
    include_first_existing([
        'scripts/login.php',
        '../scripts/login.php',
        '../../scripts/login.php',
        '../../../scripts/login.php',
    ]);
    include_first_existing([
        'scripts/func.inc.php',
        '../scripts/func.inc.php',
        '../../scripts/func.inc.php',
        '../../../scripts/func.inc.php',
    ]);

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

  var PUTER_SDK_URL = 'https://js.puter.com/v2/';
  var MAX_HISTORY = 12;
  var PREFIX = 'chatgpt_free_openai_';
  var KEY_MODEL = PREFIX + 'selected_model';
  var KEY_MODEL_MANUAL = PREFIX + 'selected_model_manual';
  var RENDER_MODE = window.__CHATGPT_FREE_OPENAI_MODE === 'page' ? 'page' : 'toast';

  function loadPuterSdk() {
    return new Promise(function (resolve, reject) {
      if (window.puter && window.puter.ai && typeof window.puter.ai.chat === 'function') {
        resolve(window.puter);
        return;
      }

      var existing = document.querySelector('script[data-puter-sdk="1"]');
      if (existing) {
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
    });
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
      '.cfo-page{width:100%;min-height:70vh;background:#fff;border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.08);display:flex;flex-direction:column;',
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
      '.cfo-send[disabled]{opacity:.6;cursor:not-allowed}'
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
    el.textContent = text;
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }

  function normalizeAssistantText(result) {
    if (typeof result === 'string') return result;
    if (!result) return 'Sem resposta.';
    if (typeof result.message === 'string') return result.message;
    if (typeof result.text === 'string') return result.text;
    if (Array.isArray(result.choices) && result.choices[0] && result.choices[0].message && typeof result.choices[0].message.content === 'string') {
      return result.choices[0].message.content;
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
    try {
      if (puter && puter.ai) {
        if (typeof puter.ai.models === 'function') candidates = parseModelCandidates(await puter.ai.models());
        if (!candidates.length && typeof puter.ai.listModels === 'function') candidates = parseModelCandidates(await puter.ai.listModels());
        if (!candidates.length && Array.isArray(puter.ai.models)) candidates = parseModelCandidates(puter.ai.models);
      }
    } catch (err) {
      console.warn('[chatgpt_free_openai] Falha ao listar modelos automaticamente:', err);
    }

    if (!candidates.length) {
      candidates = [
        { name: 'gpt-5', displayName: 'gpt-5' },
        { name: 'gpt-5-mini', displayName: 'gpt-5-mini' },
        { name: 'gpt-4.1', displayName: 'gpt-4.1' },
        { name: 'gpt-4o-mini', displayName: 'gpt-4o-mini' }
      ];
    }

    return candidates;
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

  function init() {
    var ui = createUI();
    var open = false;
    var loading = false;
    var history = [
      { role: 'system', content: 'Você é um assistente útil. Responda em português do Brasil de forma objetiva.' }
    ];

    function toggleToast() {
      if (RENDER_MODE === 'page') return;
      open = !open;
      ui.toast.style.display = open ? 'flex' : 'none';
      if (open) ui.input.focus();
    }

    if (ui.fab) ui.fab.addEventListener('click', toggleToast);

    loadPuterSdk()
      .then(function (puter) { return fetchAvailableModels(puter); })
      .then(function (models) {
        if (!models.length) {
          ui.model.innerHTML = '<option value="">Sem modelos</option>';
          return;
        }
        var active = applyModelSelect(ui.model, models);
        if (active) addMessage(ui.body, 'Modelo ativo: ' + active, 'assistant');
      })
      .catch(function (error) {
        ui.model.innerHTML = '<option value="">Erro ao carregar modelos</option>';
        addMessage(ui.body, 'Não foi possível listar modelos: ' + (error && error.message ? error.message : 'erro desconhecido'), 'assistant');
      });

    async function sendMessage() {
      var prompt = ui.input.value.trim();
      if (!prompt || loading) return;

      loading = true;
      ui.send.disabled = true;
      addMessage(ui.body, prompt, 'user');
      history.push({ role: 'user', content: prompt });
      if (history.length > MAX_HISTORY) history = [history[0]].concat(history.slice(history.length - (MAX_HISTORY - 1)));
      ui.input.value = '';

      try {
        var puter = await loadPuterSdk();
        var model = ui.model.value || localStorage.getItem(KEY_MODEL) || 'gpt-5';
        var result = await puter.ai.chat(history, { model: model });
        var answer = normalizeAssistantText(result);
        history.push({ role: 'assistant', content: answer });
        addMessage(ui.body, answer, 'assistant');
      } catch (error) {
        addMessage(ui.body, 'Erro ao consultar o chat: ' + (error && error.message ? error.message : 'erro desconhecido'), 'assistant');
      } finally {
        loading = false;
        ui.send.disabled = false;
      }
    }

    ui.send.addEventListener('click', sendMessage);
    ui.input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        sendMessage();
      }
    });

    addMessage(ui.body, 'Olá! Sou um chat em modo ' + (RENDER_MODE === 'page' ? 'página' : 'toast') + ' usando Puter. Como posso ajudar?', 'assistant');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
