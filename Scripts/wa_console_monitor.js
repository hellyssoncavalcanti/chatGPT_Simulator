/*
 * WhatsApp Web - Monitor de diagnóstico de abertura de chat + dados de contato
 * Uso no Console do Chrome em https://web.whatsapp.com
 *
 * Objetivo:
 * - Diagnosticar por que o chat não abre (header vazio).
 * - Confirmar se o painel "Dados do contato" aparece e onde está o telefone.
 * - Limitar volume de logs para facilitar compartilhamento.
 */
(() => {
  const CFG = {
    maxEvents: 120,        // buffer circular máximo
    emitEveryMs: 500,      // throttle de snapshots automáticos
    maxSidebarTitles: 8,   // quantos títulos incluir no resumo
    compact: true,         // payload enxuto
  };

  const state = {
    startedAt: new Date().toISOString(),
    events: [],
    observer: null,
    timer: null,
    lastEmitTs: 0,
    lastHash: '',
  };

  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const onlyDigits = (s) => String(s || '').replace(/\D/g, '');
  const maybePhone = (s) => {
    const m = String(s || '').match(/\+?\d[\d\s()\-]{7,}/);
    if (!m) return '';
    const d = onlyDigits(m[0]);
    return d.length >= 10 && d.length <= 15 ? d : '';
  };

  const now = () => new Date().toISOString();

  const pickHeader = () => {
    const header = document.querySelector('#main header');
    if (!header) return { title: '', phone: '' };

    const titleEl = header.querySelector('span[title], span[dir="auto"]');
    const title = norm(titleEl?.getAttribute?.('title') || titleEl?.textContent || '');

    let phone = '';
    for (const el of header.querySelectorAll('span[title], span[dir="auto"], span')) {
      const txt = norm(el.getAttribute?.('title') || el.textContent || '');
      const p = maybePhone(txt);
      if (p) {
        phone = p;
        break;
      }
    }
    return { title, phone };
  };

  const isPanelContainer = (el) => {
    if (!el) return false;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role === 'menu' || role === 'menuitem') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 240 || rect.height < 220) return false;
    // Painel de contato normalmente ocupa lado direito da viewport.
    if (rect.left < window.innerWidth * 0.45) return false;
    return true;
  };

  const pickPanel = () => {
    const heading = Array.from(document.querySelectorAll('h1,h2,div[role="heading"]'))
      .find((n) => /^(dados do contato|contact info)$/i.test(norm(n.textContent)));

    let root = null;
    if (heading) {
      const candidate = heading.closest('section,aside,div[role="dialog"],div[role="region"]');
      if (isPanelContainer(candidate)) root = candidate;
    }

    if (!root) {
      const direct = document.querySelector('div[aria-label="Dados do contato"],div[aria-label="Contact info"],aside[aria-label="Dados do contato"],aside[aria-label="Contact info"]');
      if (isPanelContainer(direct)) root = direct;
    }

    const panelVisible = !!root;
    if (!root) return { panelVisible, profileName: '', profilePhone: '', rootTag: '' };

    const rootTag = `${root.tagName.toLowerCase()}${root.getAttribute('role') ? `[role=${root.getAttribute('role')}]` : ''}`;

    const candidates = Array.from(root.querySelectorAll('h1,h2,div[role="heading"],span[dir="auto"],span[title]'));
    const noise = /dados do contato|contact info|mídia|media|silenciar|wa-wordmark|meta ai/i;

    let profileName = '';
    for (const c of candidates) {
      const txt = norm(c.getAttribute?.('title') || c.textContent || '');
      if (!txt || noise.test(txt)) continue;
      profileName = txt;
      break;
    }

    let profilePhone = '';
    for (const c of root.querySelectorAll('span,div,p')) {
      const txt = norm(c.textContent || '');
      const p = maybePhone(txt);
      if (p) {
        profilePhone = p;
        break;
      }
    }

    return { panelVisible, profileName, profilePhone, rootTag };
  };

  const sidebarSample = () => {
    const out = [];
    const rows = document.querySelectorAll('#pane-side div[role="row"]');
    for (const row of rows) {
      const span = row.querySelector('div._ak8q span[title], span[title]');
      const t = norm(span?.getAttribute?.('title') || span?.textContent || '');
      if (!t) continue;
      out.push(t);
      if (out.length >= CFG.maxSidebarTitles) break;
    }
    return out;
  };

  const snapshot = (reason) => {
    const h = pickHeader();
    const p = pickPanel();
    return {
      ts: now(),
      reason,
      headerTitle: h.title,
      headerPhone: h.phone,
      panelVisible: p.panelVisible,
      profileName: p.profileName,
      profilePhone: p.profilePhone,
      panelRoot: p.rootTag,
    };
  };

  const compactHash = (x) => [x.headerTitle, x.headerPhone, x.panelVisible, x.profileName, x.profilePhone, x.panelRoot].join('|');

  const pushEvent = (evt) => {
    state.events.push(evt);
    if (state.events.length > CFG.maxEvents) state.events.splice(0, state.events.length - CFG.maxEvents);
    const line = CFG.compact
      ? `[WA-MON] ${evt.ts} | ${evt.reason} | hdr="${evt.headerTitle || '-'}" | hPhone=${evt.headerPhone || '-'} | panel=${evt.panelVisible ? 'Y' : 'N'} | pName="${evt.profileName || '-'}" | pPhone=${evt.profilePhone || '-'} | root=${evt.panelRoot || '-'}`
      : evt;
    console.log(line);
  };

  const emit = (reason) => {
    const t = Date.now();
    if (t - state.lastEmitTs < CFG.emitEveryMs && reason !== 'manual') return;
    state.lastEmitTs = t;
    const s = snapshot(reason);
    const h = compactHash(s);
    if (h === state.lastHash && reason !== 'manual') return;
    state.lastHash = h;
    pushEvent(s);
  };

  const observe = () => {
    state.observer = new MutationObserver(() => emit('dom'));
    state.observer.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: false });
  };

  const api = {
    help() {
      console.log([
        'Comandos:',
        '  waMon.snap()                 -> snapshot manual',
        '  waMon.traceOpen("Nome")      -> tenta abrir chat por título e loga etapas',
        '  waMon.openContactPanel()      -> clica no header para abrir dados de contato',
        '  waMon.closePanel()            -> ESC',
        '  waMon.sidebar()               -> amostra de títulos da sidebar',
        '  waMon.export()                -> JSON compacto dos eventos',
        '  waMon.stop()                  -> para monitor',
      ].join('\n'));
    },
    snap() { emit('manual'); },
    sidebar() {
      const sample = sidebarSample();
      console.log('[WA-MON] sidebar_sample:', sample);
      return sample;
    },
    closePanel() {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      emit('closePanel');
    },
    openContactPanel() {
      const header = document.querySelector('#main header');
      if (!header) {
        console.warn('[WA-MON] header não encontrado');
        return false;
      }
      header.click();
      emit('openContactPanel');
      return true;
    },
    traceOpen(targetTitle) {
      const target = norm(targetTitle);
      if (!target) {
        console.warn('[WA-MON] informe um título');
        return false;
      }
      const rows = Array.from(document.querySelectorAll('#pane-side div[role="row"]'));
      let clicked = false;
      for (const row of rows) {
        const span = row.querySelector('div._ak8q span[title], span[title]');
        const txt = norm(span?.getAttribute?.('title') || span?.textContent || '');
        if (!txt) continue;
        if (txt === target) {
          row.scrollIntoView({ block: 'center' });
          row.click();
          clicked = true;
          break;
        }
      }
      emit(clicked ? 'traceOpen:clicked' : 'traceOpen:not-found');
      return clicked;
    },
    export() {
      const payload = {
        startedAt: state.startedAt,
        totalEvents: state.events.length,
        events: state.events,
      };
      const json = JSON.stringify(payload);
      console.log(`[WA-MON] export size=${json.length} chars, events=${state.events.length}`);
      return payload;
    },
    stop() {
      if (state.observer) state.observer.disconnect();
      if (state.timer) clearInterval(state.timer);
      delete window.waMon;
      console.log('[WA-MON] stopped');
    }
  };

  observe();
  state.timer = setInterval(() => emit('tick'), 2000);
  window.waMon = api;
  emit('start');
  api.help();
})();
