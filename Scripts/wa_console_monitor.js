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
    clickTrackerOn: false,
    clickHandler: null,
    selectionHandler: null,
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
  const short = (s, n = 140) => {
    const t = norm(s);
    return t.length > n ? `${t.slice(0, n)}…` : t;
  };
  const nodeCssPath = (el) => {
    if (!el || !el.tagName) return '';
    const parts = [];
    let cur = el;
    let depth = 0;
    while (cur && cur.nodeType === 1 && depth < 5) {
      let part = cur.tagName.toLowerCase();
      const id = cur.getAttribute('id');
      const cls = (cur.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 2);
      if (id) part += `#${id}`;
      if (cls.length) part += `.${cls.join('.')}`;
      parts.unshift(part);
      cur = cur.parentElement;
      depth += 1;
    }
    return parts.join(' > ');
  };

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
    if (rect.width < 150 || rect.height < 100) return false;
    return true;
  };

  const pickPanel = () => {
    // Estratégia 1: data-testid (mais confiável no WA Web moderno)
    let root = document.querySelector('[data-testid="contact-info-drawer"]');

    // Estratégia 2: aria-label
    if (!root) {
      root = document.querySelector(
        'div[aria-label="Dados do contato"],div[aria-label="Contact info"],' +
        'aside[aria-label="Dados do contato"],aside[aria-label="Contact info"]'
      );
    }

    // Estratégia 3: heading textual
    if (!root) {
      const heading = Array.from(document.querySelectorAll('h1,h2,div[role="heading"],span[dir="auto"],span'))
        .find((n) => /dados do contato|contact info|informações do contato/i.test(norm(n.textContent)));
      if (heading) {
        const candidate = heading.closest('section,aside,div[role="dialog"],div[role="region"],div[class]');
        if (isPanelContainer(candidate)) root = candidate;
      }
    }

    // Estratégia 4: procura nó com telefone em container lateral
    if (!root) {
      const nodes = Array.from(document.querySelectorAll(
        '[data-testid="cell-frame-container"] span,' +
        '[data-testid="selectable-text"],' +
        'aside span[dir="auto"],' +
        'section span[dir="auto"]'
      )).filter((n) => maybePhone(n.textContent || ''));
      for (const n of nodes) {
        const candidate = n.closest('section,aside,div[role="dialog"],div[role="region"],div[class]');
        if (isPanelContainer(candidate)) {
          root = candidate;
          break;
        }
      }
    }

    const panelVisible = !!root;
    if (!root) return { panelVisible, profileName: '', profilePhone: '', rootTag: '' };

    const testid = root.getAttribute('data-testid') || '';
    const ariaLabel = root.getAttribute('aria-label') || '';
    const rootTag = `${root.tagName.toLowerCase()}` +
      (testid ? `[data-testid=${testid}]` : '') +
      (ariaLabel ? `[aria-label=${ariaLabel}]` : '') +
      (root.getAttribute('role') ? `[role=${root.getAttribute('role')}]` : '');

    const candidates = Array.from(root.querySelectorAll('h1,h2,div[role="heading"],span[dir="auto"],span[title]'));
    const noise = /dados do contato|contact info|informações do contato|mídia|media|silenciar|wa-wordmark|meta ai/i;

    let profileName = '';
    for (const c of candidates) {
      const txt = norm(c.getAttribute?.('title') || c.textContent || '');
      if (!txt || noise.test(txt)) continue;
      profileName = txt;
      break;
    }

    // Busca telefone: prioriza cell-frame-container, depois selectable-text, depois varredura ampla
    let profilePhone = '';
    const cellFrames = root.querySelectorAll('[data-testid="cell-frame-container"],[data-testid="cell-frame-secondary"]');
    for (const cf of cellFrames) {
      const p = maybePhone(norm(cf.textContent || ''));
      if (p) { profilePhone = p; break; }
    }
    if (!profilePhone) {
      for (const c of root.querySelectorAll('[data-testid="selectable-text"]')) {
        const p = maybePhone(norm(c.textContent || ''));
        if (p) { profilePhone = p; break; }
      }
    }
    if (!profilePhone) {
      for (const c of root.querySelectorAll('span,div,p')) {
        const p = maybePhone(norm(c.textContent || ''));
        if (p) { profilePhone = p; break; }
      }
    }

    return { panelVisible, profileName, profilePhone, rootTag };
  };

  const sidebarSample = () => {
    const out = [];
    const rows = document.querySelectorAll('#pane-side div[role="row"]');
    for (const row of rows) {
      const span = row.querySelector('span[title]');
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
        '  waMon.snap()                    -> snapshot manual',
        '  waMon.fullCapture("Nome")       -> *** FLUXO COMPLETO: sidebar→chat→painel→deepScan ***',
        '  waMon.deepPhoneScan()           -> varredura de TODOS os nós com telefone (com path+testid)',
        '  waMon.traceOpen("Nome")         -> tenta abrir chat por título e loga etapas',
        '  waMon.proveCapture("Nome")      -> prova guiada com confirm() para validar abertura/painel/captura',
        '  waMon.startClickTracker()       -> rastreia cliques (x,y,elemento,path curto)',
        '  waMon.stopClickTracker()        -> para rastreio de cliques',
        '  waMon.captureSelectionAndClose() -> captura texto selecionado + localizador e fecha painel',
        '  waMon.scanPhoneNodes()          -> lista nós visíveis com telefone (texto+path curto)',
        '  waMon.openContactPanel()        -> clica no header para abrir dados de contato',
        '  waMon.closePanel()              -> ESC',
        '  waMon.sidebar()                 -> amostra de títulos da sidebar',
        '  waMon.export()                  -> JSON compacto dos eventos',
        '  waMon.stop()                    -> para monitor',
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
    startClickTracker() {
      if (state.clickTrackerOn) return true;
      state.clickHandler = (ev) => {
        const target = ev.target;
        const item = {
          ts: now(),
          reason: 'click',
          x: ev.clientX,
          y: ev.clientY,
          button: ev.button,
          targetTag: (target?.tagName || '').toLowerCase(),
          targetText: short(target?.textContent || ''),
          targetTitle: short(target?.getAttribute?.('title') || ''),
          path: nodeCssPath(target),
        };
        pushEvent({
          ts: item.ts,
          reason: `click@${item.x},${item.y}`,
          headerTitle: item.targetTag,
          headerPhone: '',
          panelVisible: false,
          profileName: item.targetTitle || item.targetText || '',
          profilePhone: '',
          panelRoot: item.path || '',
        });
      };
      document.addEventListener('click', state.clickHandler, true);
      state.clickTrackerOn = true;
      console.log('[WA-MON] click tracker ON');
      return true;
    },
    stopClickTracker() {
      if (state.clickHandler) document.removeEventListener('click', state.clickHandler, true);
      state.clickTrackerOn = false;
      state.clickHandler = null;
      console.log('[WA-MON] click tracker OFF');
      return true;
    },
    captureSelectionAndClose() {
      const sel = window.getSelection?.();
      const txt = short(sel?.toString?.() || '', 220);
      const node = sel?.anchorNode?.nodeType === 3 ? sel.anchorNode.parentElement : sel?.anchorNode;
      const locator = nodeCssPath(node);
      const item = {
        ts: now(),
        selectedText: txt,
        locator,
      };
      console.log('[WA-MON] selected:', item);
      pushEvent({
        ts: item.ts,
        reason: 'selection:capture',
        headerTitle: '',
        headerPhone: '',
        panelVisible: false,
        profileName: item.selectedText || '',
        profilePhone: '',
        panelRoot: item.locator || '',
      });
      api.closePanel();
      return item;
    },
    scanPhoneNodes() {
      const out = [];
      const nodes = document.querySelectorAll('[data-testid="selectable-text"], span[dir="auto"], span, div, p');
      for (const n of nodes) {
        const txt = short(n.textContent || '', 120);
        const phone = maybePhone(txt);
        if (!phone) continue;
        const rect = n.getBoundingClientRect();
        if (!rect || rect.width < 8 || rect.height < 8) continue;
        out.push({
          phone,
          text: txt,
          path: nodeCssPath(n),
          x: Math.round(rect.left),
          y: Math.round(rect.top),
        });
        if (out.length >= 20) break;
      }
      console.log('[WA-MON] scanPhoneNodes:', out);
      pushEvent({
        ts: now(),
        reason: 'scanPhoneNodes',
        headerTitle: '',
        headerPhone: '',
        panelVisible: false,
        profileName: `${out.length} nodes`,
        profilePhone: out[0]?.phone || '',
        panelRoot: out[0]?.path || '',
      });
      return out;
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
    deepPhoneScan() {
      // Varredura completa de todos os nós que contêm telefone, com path + contexto
      console.log('[WA-MON] === DEEP PHONE SCAN ===');
      const results = [];
      const allNodes = document.querySelectorAll('*');
      for (const n of allNodes) {
        if (n.children.length > 0) continue; // só nós folha
        const txt = norm(n.textContent || '');
        const phone = maybePhone(txt);
        if (!phone) continue;
        const rect = n.getBoundingClientRect();
        if (!rect || rect.width < 1 || rect.height < 1) continue;
        const testid = n.closest('[data-testid]')?.getAttribute('data-testid') || '';
        const ariaLabel = n.closest('[aria-label]')?.getAttribute('aria-label') || '';
        const entry = {
          phone,
          text: short(txt, 80),
          tag: n.tagName.toLowerCase(),
          path: nodeCssPath(n),
          testid,
          ariaLabel,
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        };
        results.push(entry);
        console.log(`  [${results.length}] phone=${phone} testid=${testid || '-'} aria=${ariaLabel || '-'} path=${entry.path}`);
      }
      console.log(`[WA-MON] Total phone nodes encontrados: ${results.length}`);
      return results;
    },
    async fullCapture(targetTitle) {
      // Fluxo completo: clica sidebar → abre chat → abre painel → deep scan
      const target = norm(targetTitle);
      if (!target) { console.warn('[WA-MON] informe título'); return null; }
      console.log(`[WA-MON] === FULL CAPTURE para "${target}" ===`);

      // 1) Clica na sidebar
      const rows = Array.from(document.querySelectorAll('#pane-side div[role="row"]'));
      let found = false;
      for (const row of rows) {
        const span = row.querySelector('span[title]');
        const txt = norm(span?.getAttribute?.('title') || span?.textContent || '');
        if (txt === target) {
          row.scrollIntoView({ block: 'center' });
          const cell = row.querySelector('div[role="gridcell"]') || row;
          cell.click();
          found = true;
          console.log('[WA-MON] 1/4 Sidebar click OK');
          break;
        }
      }
      if (!found) { console.warn('[WA-MON] Título não encontrado na sidebar'); return null; }

      await new Promise(r => setTimeout(r, 1500));
      const hdr = pickHeader();
      console.log(`[WA-MON] 2/4 Header: title="${hdr.title}" phone="${hdr.phone}"`);
      if (hdr.phone) {
        console.log(`[WA-MON] Telefone já visível no header: ${hdr.phone}`);
        return { source: 'header', phone: hdr.phone, title: hdr.title };
      }

      // 3) Abre painel de contato
      const header = document.querySelector('#main header');
      if (header) header.click();
      console.log('[WA-MON] 3/4 Abrindo painel de contato...');
      await new Promise(r => setTimeout(r, 2000));

      const panel = pickPanel();
      console.log(`[WA-MON] Panel: visible=${panel.panelVisible} root=${panel.rootTag} name=${panel.profileName} phone=${panel.profilePhone}`);

      if (panel.profilePhone) {
        console.log(`[WA-MON] Telefone capturado via painel: ${panel.profilePhone}`);
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        return { source: 'panel', phone: panel.profilePhone, name: panel.profileName, root: panel.rootTag };
      }

      // 4) Deep scan como último recurso
      console.log('[WA-MON] 4/4 Painel não teve telefone, fazendo deep scan...');
      const deep = api.deepPhoneScan();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

      if (deep.length > 0) {
        console.log(`[WA-MON] Deep scan encontrou ${deep.length} nós com telefone. Primeiro: ${deep[0].phone}`);
        return { source: 'deepScan', phone: deep[0].phone, allPhones: deep };
      }

      console.warn('[WA-MON] Nenhum telefone encontrado por nenhuma estratégia.');
      return { source: 'none', phone: '' };
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
        const span = row.querySelector('span[title]');
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
    async proveCapture(targetTitle) {
      const target = norm(targetTitle);
      if (!target) {
        console.warn('[WA-MON] informe um título para proveCapture');
        return { ok: false, reason: 'missing_target' };
      }

      const out = {
        target,
        clicked: false,
        chatConfirmed: false,
        panelConfirmed: false,
        snapshotAfterOpen: null,
        snapshotAfterPanel: null,
      };

      console.log(`[WA-MON] Iniciando prova guiada para: "${target}"`);
      out.clicked = api.traceOpen(target);
      await new Promise((r) => setTimeout(r, 1200));
      emit('proveCapture:afterOpen');
      out.snapshotAfterOpen = snapshot('proveCapture:afterOpen');

      out.chatConfirmed = confirm(
        `O chat aberto no WhatsApp é realmente "${target}"?\\n\\n` +
        `Header atual: "${out.snapshotAfterOpen.headerTitle || '(vazio)'}"\\n\\n` +
        'Clique em OK se SIM, ou Cancel se NÃO.'
      );

      if (!out.chatConfirmed) {
        console.warn('[WA-MON] Usuário confirmou que o chat alvo NÃO abriu corretamente.');
        pushEvent({
          ts: now(),
          reason: 'proveCapture:user_chat_not_opened',
          headerTitle: out.snapshotAfterOpen.headerTitle || '',
          headerPhone: out.snapshotAfterOpen.headerPhone || '',
          panelVisible: out.snapshotAfterOpen.panelVisible || false,
          profileName: out.snapshotAfterOpen.profileName || '',
          profilePhone: out.snapshotAfterOpen.profilePhone || '',
          panelRoot: out.snapshotAfterOpen.panelRoot || '',
        });
        return { ok: false, ...out, reason: 'chat_not_opened' };
      }

      api.openContactPanel();
      await new Promise((r) => setTimeout(r, 1500));
      emit('proveCapture:afterPanelOpen');
      out.snapshotAfterPanel = snapshot('proveCapture:afterPanelOpen');

      out.panelConfirmed = confirm(
        'O painel "Dados do contato / Contact info" está visível na lateral direita?\\n\\n' +
        `Detecção automática: panel=${out.snapshotAfterPanel.panelVisible ? 'Y' : 'N'} ` +
        `| root=${out.snapshotAfterPanel.panelRoot || '-'}\\n\\n` +
        'Clique em OK se SIM, ou Cancel se NÃO.'
      );

      if (!out.panelConfirmed) {
        console.warn('[WA-MON] Usuário informou que painel de contato não ficou visível.');
        pushEvent({
          ts: now(),
          reason: 'proveCapture:user_panel_not_visible',
          headerTitle: out.snapshotAfterPanel.headerTitle || '',
          headerPhone: out.snapshotAfterPanel.headerPhone || '',
          panelVisible: out.snapshotAfterPanel.panelVisible || false,
          profileName: out.snapshotAfterPanel.profileName || '',
          profilePhone: out.snapshotAfterPanel.profilePhone || '',
          panelRoot: out.snapshotAfterPanel.panelRoot || '',
        });
        return { ok: false, ...out, reason: 'panel_not_visible' };
      }

      const finalPhone = out.snapshotAfterPanel.profilePhone || out.snapshotAfterPanel.headerPhone || '';
      const ok = !!finalPhone;
      console.log(
        `[WA-MON] Prova concluída | ok=${ok} | phone=${finalPhone || '-'} | profileName="${out.snapshotAfterPanel.profileName || '-'}"`
      );
      pushEvent({
        ts: now(),
        reason: ok ? 'proveCapture:success' : 'proveCapture:no_phone',
        headerTitle: out.snapshotAfterPanel.headerTitle || '',
        headerPhone: out.snapshotAfterPanel.headerPhone || '',
        panelVisible: out.snapshotAfterPanel.panelVisible || false,
        profileName: out.snapshotAfterPanel.profileName || '',
        profilePhone: out.snapshotAfterPanel.profilePhone || '',
        panelRoot: out.snapshotAfterPanel.panelRoot || '',
      });

      return { ok, ...out, phone: finalPhone, reason: ok ? 'success' : 'phone_not_found' };
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
      api.stopClickTracker();
      if (state.observer) state.observer.disconnect();
      if (state.timer) clearInterval(state.timer);
      delete window.waMon;
      console.log('[WA-MON] stopped');
    }
  };

  // Trigger log das funções expostas no waMon (baixo volume, útil para replay).
  for (const key of Object.keys(api)) {
    if (typeof api[key] !== 'function') continue;
    const original = api[key];
    api[key] = function(...args) {
      pushEvent({
        ts: now(),
        reason: `fn:${key}`,
        headerTitle: '',
        headerPhone: '',
        panelVisible: false,
        profileName: short(JSON.stringify(args || []), 120),
        profilePhone: '',
        panelRoot: '',
      });
      return original.apply(this, args);
    };
  }

  observe();
  state.timer = setInterval(() => emit('tick'), 2000);
  window.waMon = api;
  emit('start');
  api.help();
})();
