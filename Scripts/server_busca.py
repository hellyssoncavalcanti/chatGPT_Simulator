# =============================================================================
# server_busca.py — Blueprint: página de documentação e teste da busca web
# =============================================================================
#
# RESPONSABILIDADE:
#   Rota de diagnóstico/documentação da pesquisa web via Playwright.
#   Autenticação gerida internamente (session cookie ou api_key na URL),
#   pois before_request em server.py lista '/api/web_search/test' como
#   self_auth_route (sem redirect automático para login).
#
# ROTAS:
#   GET /api/web_search/test  — doc HTML + teste interativo / busca JSON
# =============================================================================
from flask import Blueprint, request, jsonify, Response
import queue
import config
import auth
from shared import browser_queue
from server_helpers import (
    extract_web_search_test_params as _extract_web_search_test_params_impl,
    build_web_search_test_task as _build_web_search_test_task_impl,
    build_web_search_test_stream_response as _build_web_search_test_stream_response_impl,
    build_web_search_test_timeout_payload as _build_web_search_test_timeout_payload_impl,
    build_web_search_test_no_response_payload as _build_web_search_test_no_response_payload_impl,
)

bp = Blueprint("busca", __name__)


@bp.route('/api/web_search/test', methods=['GET'])
def api_web_search_test():
    if not auth.check_auth(request):
        return Response("""<!DOCTYPE html><html><head><meta charset="utf-8"><title>🔐 Acesso Negado</title>
        <style>body{font-family:system-ui;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a2e;color:#e0e0e0;margin:0}
        .box{text-align:center;padding:40px;background:#16213e;border-radius:12px;max-width:500px}
        h1{color:#ff6b6b}a{color:#00d4ff}code{background:#0f3460;padding:2px 8px;border-radius:4px;font-size:13px}</style></head>
        <body><div class="box"><h1>🔐 Acesso Negado</h1>
        <p>Esta página requer autenticação.</p>
        <p><b>Opção 1:</b> Faça login em <a href="/">https://localhost:3002</a> e acesse novamente.</p>
        <p><b>Opção 2:</b> Adicione a API key na URL:<br><code>/api/web_search/test?api_key=SUA_CHAVE</code></p>
        </div></body></html>""", mimetype='text/html', status=401)

    query, api_key = _extract_web_search_test_params_impl(request.args)

    # Se não tem query, retorna a página de documentação + teste
    if not query:
        return Response(f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>🔍 Pesquisa Web — ChatGPT Simulator</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:system-ui,-apple-system,sans-serif;max-width:960px;margin:0 auto;padding:20px;background:#1a1a2e;color:#e0e0e0}}
  h1{{color:#00d4ff;margin-bottom:5px}}
  .subtitle{{color:#888;font-size:14px;margin-bottom:25px}}
  .tabs{{display:flex;gap:0;margin-bottom:0;border-bottom:2px solid #0f3460}}
  .tab{{padding:12px 24px;cursor:pointer;background:#16213e;color:#888;border:none;font-size:14px;font-weight:500;border-radius:8px 8px 0 0;transition:all .2s}}
  .tab:hover{{color:#ccc}}
  .tab.active{{background:#0f3460;color:#00d4ff;border-bottom:2px solid #00d4ff;margin-bottom:-2px}}
  .panel{{display:none;background:#0f3460;padding:24px;border-radius:0 0 8px 8px}}
  .panel.active{{display:block}}
  input[type=text]{{width:100%;padding:12px;font-size:16px;border:1px solid #333;border-radius:8px;background:#16213e;color:#fff}}
  button{{padding:12px 24px;font-size:16px;background:#00d4ff;color:#000;border:none;border-radius:8px;cursor:pointer;margin-top:10px;font-weight:600}}
  button:hover{{background:#00b8d4}}
  button:disabled{{background:#555;cursor:wait}}
  #resultado{{margin-top:20px;max-height:500px;overflow-y:auto}}
  .item{{margin:10px 0;padding:12px;background:#16213e;border-radius:6px;border-left:3px solid #00d4ff}}
  .item a{{color:#00d4ff;text-decoration:none}}
  .item a:hover{{text-decoration:underline}}
  .snippet{{color:#aaa;font-size:13px;margin-top:6px}}
  .status{{color:#ffd700;font-style:italic;padding:10px}}
  code{{background:#16213e;padding:2px 6px;border-radius:4px;font-size:13px;color:#00d4ff}}
  pre{{background:#16213e;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5;border:1px solid #333}}
  pre code{{background:none;padding:0;color:#e0e0e0}}
  .key{{color:#ff9800}}
  .str{{color:#4caf50}}
  .comment{{color:#666}}
  h2{{color:#00d4ff;margin-top:30px;font-size:18px}}
  h3{{color:#ccc;margin-top:20px;font-size:15px}}
  p{{line-height:1.7}}
  .warn{{background:rgba(255,152,0,0.1);border-left:3px solid #ff9800;padding:12px;border-radius:4px;margin:15px 0}}
  .ok{{background:rgba(76,175,80,0.1);border-left:3px solid #4caf50;padding:12px;border-radius:4px;margin:15px 0}}
  table{{width:100%;border-collapse:collapse;margin:15px 0}}
  th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid #333}}
  th{{color:#00d4ff;font-size:13px}}
  td{{font-size:13px}}
  td code{{font-size:12px}}
</style></head><body>
<h1>🔍 Pesquisa Web — ChatGPT Simulator</h1>
<div class="subtitle">Documentação da API de busca no Google via Playwright + Teste interativo</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('docs')">📖 Documentação</div>
  <div class="tab" onclick="switchTab('test')">🧪 Testar Busca</div>
  <div class="tab" onclick="switchTab('integration')">🔌 Integração</div>
  <div class="tab" onclick="switchTab('llm')">🤖 Modo LLM</div>
</div>

<!-- ═══ ABA 1: DOCUMENTAÇÃO ═══ -->
<div class="panel active" id="panel-docs">
<h2>Como funciona</h2>
<p>O sistema abre o Google no navegador Chromium via Playwright, digita a query com timing humano realista,
aguarda os resultados carregarem e extrai título, URL e snippet de cada resultado orgânico.</p>

<div class="ok">✅ Resultados reais do Google — não usa APIs pagas, nem scraping headless detectável.</div>

<h2>Endpoint</h2>
<table>
  <tr><th>Método</th><th>URL</th><th>Autenticação</th></tr>
  <tr><td><code>POST</code></td><td><code>/api/web_search</code></td><td>Bearer Token ou api_key</td></tr>
  <tr><td><code>GET</code></td><td><code>/api/web_search/test?q=...</code></td><td>Session cookie ou api_key</td></tr>
</table>

<h2>Request (POST)</h2>
<pre><code>POST /api/web_search
Content-Type: application/json
Authorization: Bearer <span class="key">SUA_API_KEY</span>

{{
  <span class="str">"queries"</span>: [
    <span class="str">"metilfenidato efeitos adversos crianças"</span>,
    <span class="str">"risperidone autism pediatric guidelines site:pubmed.ncbi.nlm.nih.gov"</span>
  ]
}}</code></pre>

<h2>Response</h2>
<pre><code>{{
  <span class="str">"success"</span>: true,
  <span class="str">"results"</span>: [
    {{
      <span class="str">"success"</span>: true,
      <span class="str">"query"</span>: <span class="str">"metilfenidato efeitos adversos crianças"</span>,
      <span class="str">"count"</span>: 10,
      <span class="str">"results"</span>: [
        {{
          <span class="str">"position"</span>: 1,
          <span class="str">"title"</span>: <span class="str">"Methylphenidate for children and adolescents..."</span>,
          <span class="str">"url"</span>: <span class="str">"https://pubmed.ncbi.nlm.nih.gov/36971690/"</span>,
          <span class="str">"snippet"</span>: <span class="str">"Our updated meta-analyses suggest that..."</span>,
          <span class="str">"type"</span>: <span class="str">"organic"</span>
        }}
      ]
    }}
  ]
}}</code></pre>

<h2>Tipos de resultado</h2>
<table>
  <tr><th>type</th><th>Descrição</th></tr>
  <tr><td><code>organic</code></td><td>Resultado orgânico do Google (título + URL + snippet)</td></tr>
  <tr><td><code>featured_snippet</code></td><td>Resposta em destaque (caixa de resposta direta do Google)</td></tr>
  <tr><td><code>people_also_ask</code></td><td>Perguntas relacionadas ("As pessoas também perguntam")</td></tr>
</table>

<h2>Limites</h2>
<table>
  <tr><th>Parâmetro</th><th>Valor</th></tr>
  <tr><td>Máx. queries por request</td><td>5 (recomendado: 1-3)</td></tr>
  <tr><td>Máx. resultados por query</td><td>10</td></tr>
  <tr><td>Timeout por query</td><td>~60s (o browser precisa digitar)</td></tr>
  <tr><td>Concorrência</td><td>1 aba por query (sequencial)</td></tr>
</table>

<div class="warn">⚠️ Cada busca abre uma aba real no Chromium. Evite buscas desnecessárias para não sobrecarregar o browser.</div>
</div>

<!-- ═══ ABA 2: TESTAR BUSCA ═══ -->
<div class="panel" id="panel-test">
<h2>Teste interativo</h2>
<p>Digite uma busca e veja os resultados em tempo real. O Chromium vai abrir uma aba do Google, digitar e scrapear.</p>
<input type="text" id="q" placeholder="Ex: metilfenidato efeitos adversos crianças site:pubmed.ncbi.nlm.nih.gov" autofocus>
<button id="btn-buscar" onclick="buscar()">🔎 Buscar no Google</button>
<div id="resultado"></div>
</div>

<!-- ═══ ABA 3: INTEGRAÇÃO ═══ -->
<div class="panel" id="panel-integration">
<h2>Exemplo Python</h2>
<pre><code><span class="comment"># Busca simples</span>
import requests

resp = requests.post(
    <span class="str">"http://127.0.0.1:3003/api/web_search"</span>,
    json={{<span class="str">"queries"</span>: [<span class="str">"TDAH tratamento crianças guidelines"</span>]}},
    headers={{
        <span class="str">"Content-Type"</span>: <span class="str">"application/json"</span>,
        <span class="str">"Authorization"</span>: <span class="str">"Bearer <span class="key">SUA_API_KEY</span>"</span>
    }},
    timeout=90
)
data = resp.json()
for res in data[<span class="str">"results"</span>]:
    for item in res.get(<span class="str">"results"</span>, []):
        print(f"{{item[<span class="str">'title'</span>]}} — {{item[<span class="str">'url'</span>]}}")</code></pre>

<h2>Exemplo JavaScript (fetch)</h2>
<pre><code><span class="comment">// Busca via fetch (frontend)</span>
const resp = await fetch(<span class="str">'/api/web_search'</span>, {{
    method: <span class="str">'POST'</span>,
    headers: {{
        <span class="str">'Content-Type'</span>: <span class="str">'application/json'</span>,
        <span class="str">'Authorization'</span>: <span class="str">'Bearer SUA_API_KEY'</span>
    }},
    body: JSON.stringify({{
        queries: [<span class="str">'risperidona crianças autismo posologia'</span>]
    }})
}});
const data = await resp.json();
console.log(data.results);</code></pre>

<h2>Exemplo cURL</h2>
<pre><code>curl -X POST http://127.0.0.1:3003/api/web_search \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <span class="key">SUA_API_KEY</span>" \\
  -d '{{"queries": ["metilfenidato site:pubmed.ncbi.nlm.nih.gov"]}}'</code></pre>
</div>

<!-- ═══ ABA 4: MODO LLM ═══ -->
<div class="panel" id="panel-llm">
<h2>Como a LLM solicita pesquisa web</h2>
<p>Quando a LLM precisa de informações externas (bulas, guidelines, artigos), ela retorna
um JSON especial em vez de texto. O sistema detecta automaticamente e executa a busca.</p>

<h3>Formato que a LLM retorna:</h3>
<pre><code>{{
  <span class="str">"search_queries"</span>: [
    {{
      <span class="str">"query"</span>: <span class="str">"methylphenidate children adverse effects systematic review site:pubmed.ncbi.nlm.nih.gov"</span>,
      <span class="str">"reason"</span>: <span class="str">"buscar revisão sistemática sobre efeitos adversos do metilfenidato em crianças"</span>
    }}
  ]
}}</code></pre>

<h3>Fluxo completo:</h3>
<div class="ok">
1️⃣ Usuário pergunta → LLM decide que precisa buscar na web<br>
2️⃣ LLM retorna JSON com <code>search_queries</code><br>
3️⃣ Frontend detecta o JSON automaticamente<br>
4️⃣ Frontend chama <code>POST /api/web_search</code><br>
5️⃣ Browser abre Google, digita, scrapa resultados<br>
6️⃣ Resultados são formatados e enviados de volta à LLM<br>
7️⃣ LLM responde ao usuário usando os resultados reais
</div>

<h3>Boas práticas para queries médicas:</h3>
<table>
  <tr><th>Objetivo</th><th>Exemplo de query</th></tr>
  <tr><td>Artigos PubMed</td><td><code>methylphenidate ADHD children site:pubmed.ncbi.nlm.nih.gov</code></td></tr>
  <tr><td>Guidelines pediátricas</td><td><code>ADHD pediatric treatment guidelines AAP 2024</code></td></tr>
  <tr><td>Bula ANVISA</td><td><code>clonidina bula profissional anvisa posologia pediátrica</code></td></tr>
  <tr><td>Interações</td><td><code>risperidone valproate interaction children</code></td></tr>
  <tr><td>Revisão sistemática</td><td><code>melatonin autism sleep systematic review</code></td></tr>
</table>

<h3>Regras importantes:</h3>
<div class="warn">
• <b>SQL e pesquisa web NÃO se misturam</b> — nunca <code>sql_queries</code> e <code>search_queries</code> juntos<br>
• Quando retornar <code>search_queries</code>, não escrever NENHUM texto fora do JSON<br>
• Máximo recomendado: 3 queries por solicitação<br>
• Após receber os resultados, a LLM deve citar as fontes ao responder
</div>
</div>

<script>
function switchTab(id) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('panel-' + id).classList.add('active');
}}

async function buscar() {{
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const r = document.getElementById('resultado');
  const btn = document.getElementById('btn-buscar');
  btn.disabled = true;
  btn.textContent = '⏳ Buscando...';
  r.innerHTML = '<div class="status">⏳ Buscando no Google via Playwright... (o browser vai abrir uma aba, digitar e scrapear)</div>';
  try {{
    const resp = await fetch('/api/web_search', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{queries: [q], api_key: '{api_key or config.API_KEY}'}})
    }});
    const data = await resp.json();
    if (!data.success) {{ r.innerHTML = '<div class="status">❌ ' + JSON.stringify(data) + '</div>'; return; }}
    let html = '';
    for (const res of data.results || []) {{
      if (!res.success && res.error) {{ html += '<div class="status">❌ ' + res.error + '</div>'; continue; }}
      html += '<div class="status">✅ Query: "' + res.query + '" — ' + (res.count || 0) + ' resultado(s)</div>';
      for (const item of res.results || []) {{
        if (item.type === 'people_also_ask') {{
          html += '<div class="item">❓ <b>Perguntas relacionadas</b><div class="snippet">' + item.snippet + '</div></div>';
        }} else if (item.type === 'featured_snippet') {{
          html += '<div class="item">★ <b>Destaque</b><div class="snippet">' + item.snippet + '</div></div>';
        }} else {{
          html += '<div class="item">[' + item.position + '] <a href="' + item.url + '" target="_blank">' + item.title + '</a>';
          if (item.snippet) html += '<div class="snippet">' + item.snippet + '</div>';
          html += '</div>';
        }}
      }}
    }}
    r.innerHTML = html || '<div class="status">Nenhum resultado.</div>';
  }} catch(e) {{ r.innerHTML = '<div class="status">❌ Erro: ' + e.message + '</div>'; }}
  finally {{ btn.disabled = false; btn.textContent = '🔎 Buscar no Google'; }}
}}
document.getElementById('q')?.addEventListener('keydown', e => {{ if (e.key === 'Enter') buscar(); }});
</script></body></html>""", mimetype='text/html')

    # Se recebeu ?q=..., executa a busca diretamente (retorna JSON)
    q = queue.Queue()
    browser_queue.put(_build_web_search_test_task_impl(query, q))

    try:
        while True:
            raw_msg = q.get(timeout=90)
            if raw_msg is None:
                break
            payload, status_code = _build_web_search_test_stream_response_impl(raw_msg, query)
            if payload is not None:
                return jsonify(payload), status_code
    except queue.Empty:
        return jsonify(_build_web_search_test_timeout_payload_impl(query)), 504

    return jsonify(_build_web_search_test_no_response_payload_impl(query))
