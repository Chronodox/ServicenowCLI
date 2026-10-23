"""
Scrape everything useful from a ServiceNow page, in every frame.

For classic forms (UI16 / Next Experience), per field: technical name, label,
type, value, display value, mandatory, read-only, visible, choices, reference
table. Also the form's UI action buttons, related lists / list views (with
rows) and info/error messages. Pages without a classic form (Workspace,
Service Portal) still give a generic list of labelled inputs and buttons.

    from servicenow_smart_locator.scraper import scrape, summarize
    data = scrape(page)
    print(summarize(data))

CLI (uses the same browser profile as the inspector, so you stay logged in):

    python -m servicenow_smart_locator.scraper https://dev123.service-now.com INC0010001
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import Error as PlaywrightError, Page

from .smart_locator import SmartLocator

_JS_SCRAPE = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cleanLabel = s => clean(s).replace(/^[\s*]+|[\s*:]+$/g, '').trim();
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const byId = id => document.getElementById(id);
  const safe = (fn, d) => { try { const v = fn(); return (v === undefined || v === null) ? d : v; } catch (e) { return d; } };
  const gf = (window.g_form && typeof window.g_form.getValue === 'function') ? window.g_form : null;
  const out = { url: location.href, title: document.title, form: null, lists: [], messages: [], fields: [], buttons: [] };

  // ---- messages
  const mseen = new Set();
  for (const m of document.querySelectorAll('.outputmsg_error, .outputmsg_info, .outputmsg_warning, .outputmsg_success,' +
                                            '.notification-error, .notification-info, .notification-warning, .notification-success, [role=alert]')) {
    const t = clean(m.innerText); if (!t || !vis(m) || mseen.has(t)) continue; mseen.add(t);
    const c = m.className || '';
    out.messages.push({ type: /error/.test(c) ? 'error' : /warn/.test(c) ? 'warning' : /success/.test(c) ? 'success' : 'info', text: t.slice(0, 400) });
  }

  // ---- classic form
  const containers = [...document.querySelectorAll('[id^="element."]')];
  const formIds = new Set();
  if (containers.length || gf) {
    const table = safe(() => gf.getTableName(), '') || (containers[0] ? containers[0].id.split('.')[1] : '');
    const form = { table, sys_id: safe(() => gf.getUniqueValue(), ''), is_new_record: safe(() => gf.isNewRecord(), null),
                   fields: [], buttons: [] };
    const seen = new Set();
    for (const c of containers) {
      const full = c.id.slice('element.'.length);
      const dot = full.indexOf('.'); if (dot < 0 || seen.has(full)) continue; seen.add(full);
      const tbl = full.slice(0, dot), name = full.slice(dot + 1);
      const ids = ['sys_display.' + full, 'ni.' + full, 'sys_readonly.' + full, full];
      ids.forEach(i => formIds.add(i)); formIds.add('sys_original.' + full);
      const ctl = ids.map(byId).find(e => e && !(e.tagName === 'INPUT' && e.type === 'hidden')) || byId(full);
      let label = '';
      for (const id of [full, ...ids]) {
        const l = document.querySelector('label[for="' + CSS.escape(id) + '"]');
        if (l) { const sp = l.querySelector('.label-text'); label = cleanLabel(sp ? sp.textContent : l.textContent); if (label) break; }
      }
      const own = gf && tbl === table;
      const ui = own ? safe(() => gf.getGlideUIElement(name), null) : null;
      const hidden = byId(full), disp = byId('sys_display.' + full);
      const tag = ctl ? ctl.tagName.toLowerCase() : '';
      const type = (ui && ui.type) || (disp ? 'reference' : tag === 'select' ? 'choice' : tag === 'textarea' ? 'string (multi-line)'
                   : (ctl && ctl.type === 'checkbox') ? 'boolean' : 'string');
      const value = own ? safe(() => gf.getValue(name), hidden ? hidden.value : '') : (hidden ? hidden.value : (ctl ? ctl.value : ''));
      let display = value;
      if (disp) display = disp.value;
      else if (tag === 'select' && ctl.selectedIndex >= 0) display = clean(ctl.options[ctl.selectedIndex].text);
      else if (ctl && ctl.type === 'checkbox') display = ctl.checked ? 'true' : 'false';
      const status = byId('status.' + full);
      let mandatory = own ? safe(() => gf.isMandatory(name), null) : null;
      if (mandatory === null) mandatory = !!(status && status.getAttribute('mandatory') === 'true');
      const f = { name, table: tbl, label, type, value: String(value ?? ''), display_value: String(display ?? ''),
                  mandatory: !!mandatory,
                  readonly: !!(ctl && (ctl.readOnly || ctl.disabled || (ctl.id || '').startsWith('sys_readonly.'))),
                  visible: vis(c) };
      if (ui && ui.reference) f.reference = ui.reference;
      if (tag === 'select') f.choices = [...ctl.options].slice(0, 80).map(o => ({ value: o.value, label: clean(o.text) }));
      form.fields.push(f);
    }
    const bseen = new Set();
    for (const b of document.querySelectorAll('button[data-action-name], .form_action_button, button[id^="sysverb_"], input[id^="sysverb_"]')) {
      const action = b.getAttribute('data-action-name') || b.getAttribute('gsft_id') || b.id || '';
      const text = clean(b.innerText || b.value || b.getAttribute('aria-label') || '');
      const key = action || text; if (!key || bseen.has(key)) continue; bseen.add(key);
      form.buttons.push({ text, action_name: action, visible: vis(b) });
    }
    if (form.fields.length || form.buttons.length || gf) out.form = form;
  }

  // ---- related lists and list views
  const tseen = new Set();
  for (const t of document.querySelectorAll('table.list_table, table[id$="_table"]')) {
    if (tseen.has(t)) continue; tseen.add(t);
    const sec = t.closest('[tab_caption]');
    const title = sec ? sec.getAttribute('tab_caption') : (t.getAttribute('data-list-title') || clean(t.caption && t.caption.innerText) || t.id);
    const cols = [...t.querySelectorAll('thead th')].map(th => ({ name: th.getAttribute('name') || '',
                   label: cleanLabel(th.getAttribute('glide_label') || th.innerText) }));
    const keys = cols.map(c => c.label || c.name);
    const trs = [...t.querySelectorAll('tbody tr')].filter(tr => tr.querySelectorAll('td').length);
    const rows = trs.slice(0, 200).map(tr => {
      const cells = [...tr.querySelectorAll('td')].map(td => clean(td.innerText));
      if (cells.length !== keys.length) return cells;
      const o = {}; keys.forEach((k, i) => { if (k) o[k] = cells[i]; }); return o;
    });
    out.lists.push({ title: clean(title), id: t.id || '', related_list: !!sec, columns: keys.filter(Boolean),
                     row_count: trs.length, rows });
  }

  // ---- generic controls (Workspace, Portal, anything without a classic form), incl. shadow DOM
  const labelOf = el => {
    if (el.labels && el.labels.length) return cleanLabel(el.labels[0].textContent);
    const al = el.getAttribute('aria-label'); if (al) return cleanLabel(al);
    const root = el.getRootNode(); const lb = el.getAttribute('aria-labelledby');
    if (lb && root.getElementById) return cleanLabel(lb.split(/\s+/).map(i => root.getElementById(i)).filter(Boolean).map(e => e.textContent).join(' '));
    return cleanLabel(el.getAttribute('placeholder') || el.getAttribute('title') || '');
  };
  const btnNames = new Set((out.form ? out.form.buttons : []).map(b => b.text));
  const walk = root => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName === 'SNSL-INSPECTOR') continue;
      const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
      const isBtn = tag === 'button' || el.getAttribute('role') === 'button' || (tag === 'input' && ['submit', 'button'].includes(type));
      const isField = !isBtn && (tag === 'select' || tag === 'textarea' || (tag === 'input' && type !== 'hidden') ||
                                 ['textbox', 'combobox', 'checkbox', 'switch'].includes(el.getAttribute('role')));
      if (!(isBtn || isField) || !vis(el)) continue;
      if (isField) {
        if (formIds.has(el.id) || out.fields.length >= 300) continue;
        out.fields.push({ label: labelOf(el), name: el.getAttribute('name') || '', id: el.id || '', tag, type,
                          value: type === 'checkbox' ? String(el.checked) : String(el.value ?? ''),
                          required: !!(el.required || el.getAttribute('aria-required') === 'true'),
                          readonly: !!(el.readOnly || el.disabled) });
      } else {
        const text = clean(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '').slice(0, 80);
        if (!text || btnNames.has(text) || out.buttons.length >= 300) continue;
        btnNames.add(text);
        out.buttons.push({ text, id: el.id || '', action_name: el.getAttribute('data-action-name') || '' });
      }
    }
  };
  walk(document);
  return out;
}
"""


def scrape(page: Page) -> dict:
    """Scrape all frames of ``page``. Returns a JSON-serialisable dict."""
    sn = SmartLocator(page)
    frames: List[dict] = []
    for node in sn._frames(None):
        try:
            data = node.frame.evaluate(_JS_SCRAPE)
        except PlaywrightError:
            continue
        if not (data["form"] or data["lists"] or data["messages"] or data["fields"] or data["buttons"]):
            continue
        try:
            path = sn._frame_chain(node)[1]
            hidden = bool(node.path) and not node.frame.frame_element().is_visible()
        except PlaywrightError:
            path, hidden = [], False
        if hidden:
            continue
        data["frame_path"] = path
        frames.append(data)

    forms = [f["form"] for f in frames if f["form"]]
    return {
        "scraped_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "url": page.url,
        "title": _safe(page.title, ""),
        "summary": {
            "frames": len(frames),
            "forms": len(forms),
            "form_fields": sum(len(f["fields"]) for f in forms),
            "buttons": sum(len(f["buttons"]) for f in forms) + sum(len(f["buttons"]) for f in frames),
            "lists": sum(len(f["lists"]) for f in frames),
            "messages": sum(len(f["messages"]) for f in frames),
        },
        "frames": frames,
    }


def save(data: dict, path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def summarize(data: dict) -> str:
    """Readable text overview of a scrape result."""
    out = [f"{data['title']}  ({data['url']})"]
    for fr in data["frames"]:
        where = " > ".join(fr["frame_path"]) or "hoofdpagina"
        out.append(f"\n== frame: {where}")
        for m in fr["messages"]:
            out.append(f"  [{m['type']}] {m['text']}")
        form = fr["form"]
        if form:
            new = " (NIEUW record)" if form.get("is_new_record") else ""
            out.append(f"  Formulier: {form['table']}  sys_id={form['sys_id']}{new}")
            for f in form["fields"]:
                flags = "".join(["*" if f["mandatory"] else "", " ro" if f["readonly"] else "",
                                 " verborgen" if not f["visible"] else ""])
                val = f["display_value"] if f["display_value"] == f["value"] or not f["value"] \
                    else f"{f['display_value']}  [{f['value']}]"
                out.append(f"    {f['name']:<24} {f['label'][:28]:<28} {f['type'][:12]:<12} {flags:<14} = {val}")
            if form["buttons"]:
                out.append("  Knoppen: " + ", ".join(f"{b['text'] or '?'} ({b['action_name']})" for b in form["buttons"]))
        for lst in fr["lists"]:
            out.append(f"  Lijst '{lst['title']}': {lst['row_count']} rij(en), kolommen: {', '.join(lst['columns'])}")
            for r in lst["rows"][:5]:
                out.append("    - " + (", ".join(f"{k}={v}" for k, v in r.items()) if isinstance(r, dict) else " | ".join(r)))
        if fr["fields"]:
            out.append("  Overige velden: " + ", ".join(f["label"] or f["name"] or f["id"] for f in fr["fields"][:40]))
        if fr["buttons"]:
            out.append("  Overige knoppen: " + ", ".join(b["text"] for b in fr["buttons"][:40]))
    return "\n".join(out)


def _safe(fn, default):
    try:
        return fn()
    except PlaywrightError:
        return default


# ------------------------------------------------------------------- CLI
def main(argv: Optional[List[str]] = None) -> None:
    from playwright.sync_api import sync_playwright
    from .automation import ServiceNowBot, launch

    ap = argparse.ArgumentParser(prog="python -m servicenow_smart_locator.scraper",
                                 description="Scrape een ServiceNow-pagina naar JSON.")
    ap.add_argument("instance", help="https://<instance>.service-now.com")
    ap.add_argument("target", nargs="?", help="recordnummer (INC0010001), pad (incident_list.do) of volledige URL")
    ap.add_argument("--out", help="JSON-bestand (standaard: scrape_<doel>_<tijd>.json)")
    ap.add_argument("--polaris", action="store_true", help="open in Next Experience i.p.v. klassiek")
    ap.add_argument("--browser", choices=["chromium", "msedge", "chrome"], default="chromium")
    ap.add_argument("--profile", default=str(Path.home() / ".snsl_inspector_profile"))
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args(argv)

    with sync_playwright() as p:
        ctx, page = launch(p, args.browser, args.profile, args.headless)
        import os
        bot = ServiceNowBot(page, args.instance, ui="polaris" if args.polaris else "classic",
                            user=os.environ.get("SN_USER"), password=os.environ.get("SN_PASSWORD"))
        t = args.target or ""
        if t.startswith("http"):
            page.goto(t)
            bot.ensure_logged_in(t)
        elif t and "." not in t and "/" not in t:
            bot.open_record(t, allow_new=True)
        else:
            bot.goto(t or "")
        bot.sn.wait_until_ready()
        data = scrape(page)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = save(data, Path(args.out or f"scrape_{(t or 'pagina').replace('/', '_').replace('?', '_')[:40]}_{stamp}.json"))
        _print(summarize(data))
        print(f"\nOpgeslagen: {out.resolve()}")
        ctx.close()


def _print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
