(() => {
  // ServiceNow Smart Locator - visual inspector overlay.
  // Injected into every frame. Hover = highlight + label, click = pick
  // (the click is swallowed so nothing gets submitted). F8 toggles.
  if (window.__snslInstalled) return;
  window.__snslInstalled = true;

  const IS_TOP = window === window.top;
  const COLORS = { field: '#2563eb', button: '#16a34a', element: '#6b7280', picked: '#f59e0b', scrape: '#7c3aed' };
  const KIND = { field: 'VELD', button: 'KNOP', element: 'ELEMENT', scrape: 'SCRAPE' };
  const FIELD_SEL = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]),' +
                    'select, textarea, [contenteditable="true"], [role=textbox], [role=combobox], [role=checkbox], [role=switch], [role=spinbutton]';
  const BTN_SEL = 'button, input[type=submit], input[type=button], input[type=reset], input[type=image], a[href],' +
                  '[role=button], [role=menuitem], [role=tab], [role=link], [role=option]';
  const IDRE = /^(sys_display\.|ni\.|sys_readonly\.)?([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$/;

  let on = false, host = null, sr = null, els = {}, lastDesc = null, seq = 0;

  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/^[\s*]+|[\s*:]+$/g, '').trim();
  const parentDeep = el => el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
  const closestDeep = (el, sel) => { for (; el; el = parentDeep(el)) if (el.nodeType === 1 && el.matches(sel)) return el; return null; };
  const byId = (root, id) => (root.getElementById ? root.getElementById(id) : null);
  const usable = e => e && !(e.tagName === 'INPUT' && e.type === 'hidden');
  const baseId = id => id.replace(/^(sys_display|ni|sys_readonly)\./, '');

  // ------------------------------------------------------------------ describe
  function labelText(lab) { const sp = lab.querySelector('.label-text'); return clean(sp ? sp.textContent : lab.textContent); }

  function labelFor(el) {
    const root = el.getRootNode();
    if (el.id) {
      const b = baseId(el.id);
      for (const id of [el.id, b, 'sys_display.' + b, 'ni.' + b, 'sys_readonly.' + b]) {
        let lab = null;
        try { lab = root.querySelector('label[for="' + CSS.escape(id) + '"]'); } catch (e) {}
        if (lab && labelText(lab)) return labelText(lab);
      }
    }
    if (el.labels && el.labels.length && clean(el.labels[0].textContent)) return labelText(el.labels[0]);
    const al = el.getAttribute('aria-label'); if (clean(al)) return clean(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = clean(lb.split(/\s+/).map(i => byId(root, i)).filter(Boolean).map(e => e.textContent).join(' ')); if (t) return t; }
    return clean(el.getAttribute('placeholder') || el.getAttribute('title') || '');
  }

  function controlForLabel(lab) {
    const id = lab.getAttribute('for');
    if (!id) return lab.querySelector(FIELD_SEL);
    const root = lab.getRootNode(), b = baseId(id);
    for (const c of [id, 'sys_display.' + b, b, 'ni.' + b, 'sys_readonly.' + b]) { const e = byId(root, c); if (usable(e)) return e; }
    return null;
  }

  function cssPath(el) {
    const parts = [];
    while (el && el.nodeType === 1) {
      const root = el.getRootNode();
      const seg = [];
      let cur = el;
      while (cur && cur.nodeType === 1) {
        if (cur.id && /^[^\s"'\\]+$/.test(cur.id)) {
          let n = 0; try { n = root.querySelectorAll('[id="' + cur.id + '"]').length; } catch (e) {}
          if (n === 1) { seg.unshift('[id="' + cur.id + '"]'); break; }
        }
        let s = cur.tagName.toLowerCase();
        const container = cur.parentElement || cur.parentNode;
        if (container && container.children) {
          const sib = [...container.children].filter(c => c.tagName === cur.tagName);
          if (sib.length > 1) s += ':nth-of-type(' + (sib.indexOf(cur) + 1) + ')';
        }
        seg.unshift(s);
        if (!cur.parentElement) break;
        cur = cur.parentElement;
      }
      parts.unshift(seg.join(' > '));
      el = root.host || null; // continue outside the shadow root
    }
    return parts.join(' ');
  }

  function describe(target) {
    if (!target || target.nodeType !== 1) return null;
    if (/^(IFRAME|FRAME|HTML|BODY)$/.test(target.tagName)) return null;
    const lab = closestDeep(target, 'label');
    const f = (lab && controlForLabel(lab)) || closestDeep(target, FIELD_SEL);
    if (f) {
      const m = IDRE.exec(f.id || '');
      const d = { kind: 'field', el: f, label: labelFor(f), table: m ? m[2] : '', field: m ? m[3] : '',
                  id: f.id || '', name: f.getAttribute('name') || '', tag: f.tagName.toLowerCase(),
                  type: (f.getAttribute('type') || '').toLowerCase(), role: f.getAttribute('role') || '' };
      const c = [];
      if (d.label) c.push(d.label);
      if (d.field) { c.push(d.field); c.push(d.table + '.' + d.field); }
      else if (/^[a-z][a-z0-9_]*$/.test(d.name)) c.push(d.name);
      d.candidates = [...new Set(c)];
      d.summary = [d.label || '(geen label)', d.field ? d.table + '.' + d.field : (d.name || d.id)].filter(Boolean).join('  ·  ');
      return d;
    }
    const b = closestDeep(target, BTN_SEL);
    if (b) {
      const action = b.getAttribute('data-action-name') || (/^sysverb_/.test(b.id) ? b.id : '') || b.getAttribute('gsft_id') || '';
      const text = clean(b.getAttribute('aria-label') || b.innerText || b.value || b.getAttribute('title') || '').slice(0, 60);
      const c = []; if (text) c.push(text); if (action) c.push(action);
      return { kind: 'button', el: b, text, action, id: b.id || '', name: b.getAttribute('name') || '',
               tag: b.tagName.toLowerCase(), candidates: [...new Set(c)],
               summary: [text || '(geen tekst)', action].filter(Boolean).join('  ·  ') };
    }
    const text = clean(target.innerText || '').slice(0, 60);
    return { kind: 'element', el: target, id: target.id || '', name: target.getAttribute('name') || '', text,
             tag: target.tagName.toLowerCase(), candidates: [],
             summary: target.tagName.toLowerCase() + (target.id ? '#' + target.id : '') + (text ? '  "' + text.slice(0, 40) + '"' : '') };
  }

  // ------------------------------------------------------------------ overlay
  const CSS_TEXT = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .vp   { position: fixed; inset: 0; border: 2px dashed rgba(37,99,235,.45); display: none; pointer-events: none; }
    .box  { position: fixed; display: none; pointer-events: none; border: 2px solid var(--c); border-radius: 3px;
            background: color-mix(in srgb, var(--c) 14%, transparent); }
    .tag  { position: fixed; display: none; pointer-events: none; max-width: min(560px, 92vw); overflow: hidden;
            text-overflow: ellipsis; white-space: nowrap; padding: 3px 8px; border-radius: 4px; color: #fff;
            background: var(--c); font: 600 12px/1.35 "Segoe UI", system-ui, sans-serif; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
    .tag b { font-weight: 800; letter-spacing: .04em; margin-right: 6px; opacity: .9; }
    .tag i { font-style: normal; opacity: .8; margin-left: 6px; }

    .panel { position: fixed; right: 16px; bottom: 16px; width: min(460px, calc(100vw - 32px)); pointer-events: auto;
             background: #0f172a; color: #e2e8f0; border-radius: 10px; box-shadow: 0 12px 32px rgba(0,0,0,.35);
             font: 13px/1.45 "Segoe UI", system-ui, sans-serif; overflow: hidden; }
    .hd { display: flex; align-items: center; gap: 8px; padding: 8px 10px; background: #1e293b; cursor: move; user-select: none; }
    .hd .t { flex: 1; font-weight: 700; font-size: 12.5px; letter-spacing: .02em; }
    .tg { border: 0; border-radius: 999px; padding: 3px 10px; font: 700 11px/1.4 "Segoe UI", system-ui, sans-serif; cursor: pointer;
          background: #334155; color: #cbd5e1; }
    .tg.on { background: #2563eb; color: #fff; }
    .mn { border: 0; background: transparent; color: #94a3b8; font-size: 16px; line-height: 1; cursor: pointer; padding: 0 4px; }
    .bd { padding: 10px 12px 12px; max-height: 55vh; overflow: auto; }
    .min .bd { display: none; }
    .hint { color: #94a3b8; font-size: 12.5px; }
    .hint kbd { background: #334155; color: #e2e8f0; border-radius: 4px; padding: 0 5px; font: 600 11px/1.6 Consolas, monospace; }
    .kind { display: inline-block; font: 800 10.5px/1.6 "Segoe UI", system-ui, sans-serif; letter-spacing: .06em; padding: 0 6px;
            border-radius: 4px; color: #fff; margin-right: 6px; vertical-align: 1px; }
    .name { font-weight: 600; font-size: 13.5px; }
    .where { color: #94a3b8; font: 12px/1.5 Consolas, monospace; margin: 4px 0 8px; word-break: break-all; }
    .line { display: flex; align-items: flex-start; gap: 6px; padding: 5px 0; border-top: 1px solid #1e293b; }
    .mark { width: 16px; flex: none; font-weight: 800; text-align: center; }
    .ok .mark { color: #4ade80; } .bad .mark { color: #f87171; } .bad code { color: #94a3b8; }
    .lc { flex: 1; min-width: 0; }
    code { display: block; font: 12.5px/1.45 Consolas, "Cascadia Mono", monospace; color: #f8fafc; word-break: break-all; }
    .note { color: #94a3b8; font-size: 11.5px; }
    .cp { flex: none; border: 1px solid #334155; background: #1e293b; color: #cbd5e1; border-radius: 5px; padding: 1px 8px;
          font: 600 11px/1.6 "Segoe UI", system-ui, sans-serif; cursor: pointer; }
    .cp:hover { background: #334155; }
    .sec { margin-top: 8px; color: #94a3b8; font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
    .err { color: #fca5a5; }
  `;

  function h(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  function ensureUI() {
    if (host && host.isConnected) return true;
    if (!document.documentElement) return false;
    host = document.createElement('snsl-inspector');
    host.style.cssText = 'all: initial; position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;';
    sr = host.attachShadow({ mode: 'closed' }); // closed: invisible to Playwright locators
    const st = h('style'); st.textContent = CSS_TEXT; sr.appendChild(st);
    els.vp = sr.appendChild(h('div', 'vp'));
    els.box = sr.appendChild(h('div', 'box'));
    els.tag = sr.appendChild(h('div', 'tag'));
    if (IS_TOP) buildPanel();
    document.documentElement.appendChild(host);
    applyOn();
    return true;
  }

  function buildPanel() {
    const p = els.panel = sr.appendChild(h('div', 'panel'));
    const hd = p.appendChild(h('div', 'hd'));
    hd.appendChild(h('span', 't', 'ServiceNow Smart Locator'));
    els.toggle = hd.appendChild(h('button', 'tg', 'UIT · F8'));
    els.toggle.onclick = () => window.__snslToggle && window.__snslToggle();
    const mn = hd.appendChild(h('button', 'mn', '–'));
    mn.title = 'Minimaliseren';
    mn.onclick = () => { p.classList.toggle('min'); mn.textContent = p.classList.contains('min') ? '+' : '–'; };
    els.body = p.appendChild(h('div', 'bd'));
    const hint = els.body.appendChild(h('div', 'hint'));
    hint.innerHTML = 'Druk op <kbd>F8</kbd> om te inspecteren. Beweeg over een veld of knop en <b>klik</b> erop: ' +
                     'je klik wordt niet uitgevoerd. Nog een keer <kbd>F8</kbd> (of <kbd>Esc</kbd>) om normaal te werken.<br>' +
                     '<kbd>F9</kbd> = hele pagina scrapen naar JSON.';
    // drag
    let dx = 0, dy = 0, drag = false;
    hd.addEventListener('mousedown', e => { if (e.target !== hd && !e.target.classList.contains('t')) return;
      drag = true; const r = p.getBoundingClientRect(); dx = e.clientX - r.left; dy = e.clientY - r.top; e.preventDefault(); });
    window.addEventListener('mousemove', e => { if (!drag) return;
      p.style.left = Math.max(0, e.clientX - dx) + 'px'; p.style.top = Math.max(0, e.clientY - dy) + 'px';
      p.style.right = 'auto'; p.style.bottom = 'auto'; }, true);
    window.addEventListener('mouseup', () => { drag = false; }, true);
  }

  function copy(text, btn) {
    const done = () => { btn.textContent = 'Gekopieerd'; setTimeout(() => (btn.textContent = 'Kopieer'), 1200); };
    const fallback = () => { const ta = h('textarea'); ta.value = text; sr.appendChild(ta); ta.select();
                             try { document.execCommand('copy'); } catch (e) {} ta.remove(); done(); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
  }

  function codeLine(cls, mark, code, note) {
    const l = h('div', 'line ' + cls);
    l.appendChild(h('span', 'mark', mark));
    const lc = l.appendChild(h('div', 'lc'));
    lc.appendChild(h('code', null, code));
    if (note) lc.appendChild(h('div', 'note', note));
    const b = l.appendChild(h('button', 'cp', 'Kopieer'));
    b.onclick = () => copy(code, b);
    return l;
  }

  function render(r) {
    if (!els.body) return;
    const b = els.body; b.textContent = '';
    const top = b.appendChild(h('div'));
    const k = top.appendChild(h('span', 'kind', KIND[r.kind] || r.kind));
    k.style.background = COLORS[r.kind] || COLORS.element;
    top.appendChild(h('span', 'name', r.title));
    b.appendChild(h('div', 'where', '⧉ ' + r.frame));
    if (r.error) b.appendChild(h('div', 'err', r.error));
    const good = (r.lines || []).filter(l => l.ok), bad = (r.lines || []).filter(l => !l.ok);
    if (good.length) { b.appendChild(h('div', 'sec', 'Smart Locator')); good.forEach(l => b.appendChild(codeLine('ok', '✓', l.code, l.note))); }
    if (r.raw) { b.appendChild(h('div', 'sec', 'Gewone Playwright')); b.appendChild(codeLine(r.raw_ok ? 'ok' : 'bad', r.raw_ok ? '✓' : '!', r.raw, r.raw_note)); }
    if (bad.length) { b.appendChild(h('div', 'sec', 'Werkt niet voor dit element')); bad.forEach(l => b.appendChild(codeLine('bad', '✗', l.code, l.note))); }
  }

  function place(d, color) {
    if (!d || !ensureUI()) return hide();
    const r = d.el.getBoundingClientRect();
    if (!r.width && !r.height) return hide();
    const c = color || COLORS[d.kind];
    for (const e of [els.box, els.tag]) e.style.setProperty('--c', c);
    Object.assign(els.box.style, { display: 'block', left: r.left - 2 + 'px', top: r.top - 2 + 'px', width: r.width + 4 + 'px', height: r.height + 4 + 'px' });
    els.tag.textContent = '';
    els.tag.appendChild(h('b', null, KIND[d.kind]));
    els.tag.appendChild(document.createTextNode(d.summary));
    if (!IS_TOP && window.name) els.tag.appendChild(h('i', null, '⧉ ' + window.name));
    els.tag.style.display = 'block';
    const th = els.tag.offsetHeight || 20;
    const y = r.top - th - 6 >= 0 ? r.top - th - 6 : Math.min(r.bottom + 6, innerHeight - th);
    els.tag.style.top = y + 'px';
    els.tag.style.left = Math.max(2, Math.min(r.left, innerWidth - els.tag.offsetWidth - 2)) + 'px';
  }

  function hide() { if (els.box) els.box.style.display = 'none'; if (els.tag) els.tag.style.display = 'none'; }

  function applyOn() {
    if (els.vp) els.vp.style.display = on ? 'block' : 'none';
    if (!on) { hide(); lastDesc = null; }
    if (els.toggle) { els.toggle.textContent = on ? 'AAN · F8' : 'UIT · F8'; els.toggle.classList.toggle('on', on); }
  }

  // ------------------------------------------------------------------ events
  const ours = e => host && e.composedPath().includes(host);
  const swallow = e => { if (!on || ours(e)) return; e.preventDefault(); e.stopImmediatePropagation(); };

  window.addEventListener('mousemove', e => {
    if (!on || ours(e)) return;
    lastDesc = describe(e.composedPath()[0]);
    lastDesc ? place(lastDesc) : hide();
  }, true);
  document.addEventListener('mouseleave', () => { if (on) hide(); }, true);
  window.addEventListener('scroll', () => { if (on && lastDesc) place(lastDesc); }, true);
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'dblclick', 'auxclick', 'submit'])
    window.addEventListener(t, swallow, true);
  window.addEventListener('click', e => {
    if (!on || ours(e)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    const d = describe(e.composedPath()[0]);
    if (d) pick(d);
  }, true);
  window.addEventListener('keydown', e => {
    if (e.key === 'F9') {
      e.preventDefault(); e.stopImmediatePropagation();
      window.__snslScrape && window.__snslScrape();
      return;
    }
    if (e.key === 'F8' || (on && e.key === 'Escape')) {
      e.preventDefault(); e.stopImmediatePropagation();
      window.__snslToggle && window.__snslToggle();
    }
  }, true);

  function pick(d) {
    const id = 'p' + Date.now().toString(36) + '_' + (seq++);
    d.el.setAttribute('data-snsl-pick', id);
    place(d, COLORS.picked);
    const { el, ...payload } = d;
    payload.pickId = id;
    payload.cssPath = cssPath(el);
    payload.frameName = IS_TOP ? '' : (window.name || '');
    if (IS_TOP) render({ kind: d.kind, title: d.summary, frame: '…', lines: [], raw: '' , error: 'Bezig met controleren…' });
    window.__snslPick && window.__snslPick(payload);
  }

  // ------------------------------------------------------------------ API for Python
  window.__snslSetOn = v => { on = !!v; ensureUI(); applyOn(); };
  window.__snslShow = r => { if (ensureUI()) render(r); };

  const boot = () => { ensureUI(); if (window.__snslState) window.__snslState().then(v => window.__snslSetOn(v), () => {}); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else boot();
})()
