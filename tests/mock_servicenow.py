"""
A tiny fake ServiceNow instance for testing the scraper and automations
without touching a real instance.

    python tests/mock_servicenow.py          -> http://127.0.0.1:8765   (login admin / admin)

It mimics what matters for the automations:
* login.do with #user_name / #user_password / #sysverb_login and a session cookie
* classic forms: incident.do?sysparm_query=number=..., ?sys_id=..., task_ci.do?sys_id=-1&sysparm_query=task=...
  - UI16 element ids (element.*, sys_display.*, sys_readonly.*), labels, mandatory markers
  - a g_form object, g_ck token, gsftSubmit() and reference autocomplete via AJAX
  - unknown number -> a NEW record form (like ServiceNow)
  - "Save" only exists in the context menu (gsftSubmit), the visible button is "Update"
  - mandatory / invalid reference -> alert(), like ServiceNow
  - Affected CIs related list on incident
* /api/now/table/<table> (needs the X-UserToken header, like the real one)
* /now/nav/ui/classic/params/target/<url> -> Next Experience shell with gsft_main inside shadow DOM

CIs: PC-0042, PC-0043, LAPTOP-7 and two CIs both named PC-DUP (to test "not unique").
INC0010001 has no CI; INC0010002 has PC-0043 (field + Affected CIs).
"""
from __future__ import annotations

import copy
import html
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

TOKEN = "mocktoken123"
SEED = {
    "cmdb_ci": {"ci1": {"name": "PC-0042"}, "ci2": {"name": "PC-0043"}, "ci3": {"name": "LAPTOP-7"},
                "ci4": {"name": "PC-DUP"}, "ci5": {"name": "PC-DUP"},
                "ci6": {"name": "BS-26-1241"}, "ci7": {"name": "BS-26-12410"},
                "ci8": {"name": "BS-26-1244", "sys_class_name": "cmdb_ci_peripheral", "install_status": "6", "type": "",
                        "model_id": "mod1", "cost_center": "cc1253"},
                "ci9": {"name": "pc-26-4455", "sys_class_name": "cmdb_ci_computer", "install_status": "6",
                        "u_stickernumber": "PC-26-4455", "model_id": "mod2", "cost_center": "cc1253"}},
    "cmn_cost_center": {"cc1004": {"name": "1004"}, "cc1253": {"name": "1253"}},
    "cmdb_model": {"mod1": {"name": "Dell Pro Plus P2425 Monitor"}, "mod2": {"name": "Dell Pro Micro Plus QBM1250"}},
    "sys_user": {"u_admin": {"name": "System Administrator", "user_name": "admin"},
                 "u_kim": {"name": "Kim de Vries"}, "u_caller": {"name": "Sam Jansen"}},
    "sys_user_group": {"grp1": {"name": "Werkplekapparatuur (AMC)"}, "grp2": {"name": "Servicedesk"}},
    "incident": {
        "inc1": {"number": "INC0010001", "short_description": "Printer on fire", "state": "1", "cmdb_ci": "",
                 "priority": "3", "assignment_group": "grp1", "assigned_to": "", "caller_id": "u_caller",
                 "description": "De printer op kamer A2-243 staat in brand.\nGraag snel komen.",
                 "opened_at": "2026-10-22 09:57:37", "sys_updated_on": "2026-10-22 10:01:32"},
        "inc2": {"number": "INC0010002", "short_description": "Mail not working", "state": "2", "cmdb_ci": "ci2",
                 "priority": "2", "assignment_group": "grp1", "assigned_to": "u_kim", "caller_id": "u_caller",
                 "opened_at": "2026-10-22 11:16:38", "sys_updated_on": "2026-10-22 11:26:22"},
        "inc3": {"number": "INC0010003", "short_description": "", "state": "1", "cmdb_ci": "", "priority": "4",
                 "assignment_group": "grp1", "assigned_to": "u_admin",
                 "opened_at": "2026-10-21 08:00:00", "sys_updated_on": "2026-10-21 08:05:00"},
        "inc4": {"number": "INC0010004", "short_description": "Andere groep", "state": "1", "cmdb_ci": "", "priority": "3",
                 "assignment_group": "grp2", "assigned_to": "", "opened_at": "2026-10-22 12:00:00",
                 "sys_updated_on": "2026-10-22 12:00:00"},
        "inc5": {"number": "INC0010005", "short_description": "Al opgelost", "state": "6", "cmdb_ci": "", "priority": "3",
                 "assignment_group": "grp1", "assigned_to": "u_kim", "opened_at": "2026-10-20 12:00:00",
                 "sys_updated_on": "2026-10-20 15:00:00", "close_code": "Solved (Permanently)", "close_notes": "klaar"},
    },
    "sc_task": {"sct1": {"number": "SCTASK0010001", "short_description": "Standaard werkplek leveren", "state": "1",
                         "request_item.u_cost_center": "cc1004", "assignment_group": "grp1", "assigned_to": "",
                         "priority": "3", "opened_at": "2026-10-22 16:04:21", "sys_updated_on": "2026-10-22 16:05:12"},
                "sct2": {"number": "SCTASK0010002", "short_description": "Zonder kostenplaats", "state": "1",
                         "request_item.u_cost_center": "", "assignment_group": "grp1", "assigned_to": "u_admin",
                         "priority": "3", "opened_at": "2026-10-22 15:48:05", "sys_updated_on": "2026-10-22 15:56:00"}},
    "journal": [{"element_id": "inc1", "element": "comments", "value": "Hij rookt nog steeds!",
                 "created_by": "Sam Jansen", "created_on": "2026-10-22 10:01:32"}],
    "task_ci": {"tci1": {"task": "inc2", "ci_item": "ci2"}},
}
FORMS = {
    "incident": [
        dict(name="number", label="Number", type="string", readonly=True),
        dict(name="caller_id", label="Caller", type="reference", ref="sys_user"),
        dict(name="short_description", label="Short description", type="string", mandatory=True),
        dict(name="description", label="Description", type="text"),
        dict(name="state", label="State", type="choice",
             choices=[("1", "New"), ("2", "In Progress"), ("3", "On Hold"), ("6", "Resolved"), ("8", "Canceled")]),
        dict(name="hold_reason", label="On hold reason", type="choice",
             choices=[("", "-- None --"), ("1", "Awaiting Caller"), ("4", "Awaiting Vendor")]),
        dict(name="priority", label="Priority", type="choice",
             choices=[("1", "1 - Critical"), ("2", "2 - High"), ("3", "3 - Moderate"), ("4", "4 - Low")]),
        dict(name="assignment_group", label="Assignment group", type="reference", ref="sys_user_group"),
        dict(name="assigned_to", label="Assigned to", type="reference", ref="sys_user"),
        dict(name="cmdb_ci", label="Configuration item", type="reference", ref="cmdb_ci"),
        dict(name="close_code", label="Resolution code", type="choice",
             choices=[("", "-- None --"), ("Solved (Permanently)", "Solved (Permanently)"),
                      ("Solved Remotely (Permanently)", "Solved Remotely (Permanently)")]),
        dict(name="close_notes", label="Resolution notes", type="text"),
        dict(name="comments", label="Additional comments (Customer visible)", type="journal"),
        dict(name="work_notes", label="Work notes", type="journal"),
    ],
    # Catalog Task: like the real thing, NO Configuration item field, only the Affected CIs list
    "sc_task": [
        dict(name="number", label="Number", type="string", readonly=True),
        dict(name="short_description", label="Short description", type="string", mandatory=True),
        dict(name="state", label="State", type="choice", choices=[("1", "Open"), ("2", "Work in Progress"), ("3", "Closed Complete")]),
        dict(name="request_item.u_cost_center", label="Cost Center", type="reference", ref="cmn_cost_center", readonly=True),
        dict(name="assignment_group", label="Assignment group", type="reference", ref="sys_user_group"),
        dict(name="assigned_to", label="Assigned to", type="reference", ref="sys_user"),
        dict(name="comments", label="Additional comments (Customer visible)", type="journal"),
        dict(name="work_notes", label="Work notes", type="journal"),
    ],
    # CI forms; cost_center sits on another (hidden) form section, like at Amsterdam UMC
    "cmdb_ci_peripheral": [
        dict(name="name", label="Name", type="string", mandatory=True),
        dict(name="model_id", label="Model ID", type="reference", ref="cmdb_model", readonly=True),
        dict(name="type", label="Type", type="choice", choices=[("", "-- None --"), ("Camera", "Camera"), ("monitor", "Monitor")]),
        dict(name="install_status", label="Status", type="choice",
             choices=[("", "-- None --"), ("6", "In Stock"), ("1", "Installed"), ("7", "Retired")]),
        dict(name="cost_center", label="Cost center", type="reference", ref="cmn_cost_center", hidden=True),
    ],
    "cmdb_ci_computer": [
        dict(name="name", label="Name", type="string", mandatory=True),
        dict(name="u_stickernumber", label="Stickernumber", type="string"),
        dict(name="install_status", label="Status", type="choice",
             choices=[("", "-- None --"), ("6", "In Stock"), ("1", "Installed"), ("10", "Installed - Exception")]),
        dict(name="cost_center", label="Cost center", type="reference", ref="cmn_cost_center", hidden=True),
    ],
    "task_ci": [
        dict(name="task", label="Task", type="reference", ref="task", readonly=True),
        dict(name="ci_item", label="Configuration Item", type="reference", ref="cmdb_ci", mandatory=True),
    ],
}
DISPLAY = {"cmdb_ci": "name", "incident": "number", "sc_task": "number"}
TASK_TABLES = ("incident", "sc_task")
# like UI policies: fields that become mandatory for a state
COND_MANDATORY = {"incident": [("state", "3", ["hold_reason"]), ("state", "6", ["close_code", "close_notes"])]}
INACTIVE_STATES = {"incident": ("6", "7", "8"), "sc_task": ("3", "4", "7")}
ME = "u_admin"
MY_GROUPS = ("grp1",)
CI_CLASSES = ("cmdb_ci_peripheral", "cmdb_ci_computer")
REFS = {("task_ci", "ci_item"): "cmdb_ci", ("task_ci", "task"): "task", ("cmdb_ci", "cost_center"): "cmn_cost_center",
        ("cmdb_ci", "model_id"): "cmdb_model", ("sc_task", "request_item.u_cost_center"): "cmn_cost_center",
        ("incident", "cmdb_ci"): "cmdb_ci", ("task", "assigned_to"): "sys_user", ("task", "caller_id"): "sys_user",
        ("task", "assignment_group"): "sys_user_group", ("task", "cmdb_ci"): "cmdb_ci"}
CHOICE_LABELS = {"install_status": {"6": "In Stock", "1": "Installed", "10": "Installed - Exception", "7": "Retired"},
                 "type": {"monitor": "Monitor", "Camera": "Camera"}}
STORE = {c: "cmdb_ci" for c in CI_CLASSES}  # CI classes live in one table, like the CMDB hierarchy

LOGIN_HTML = """<!doctype html><html><head><title>Log in | ServiceNow</title></head><body>
<h2>ServiceNow (mock)</h2>{error}
<form method="post" action="/login.do">
  <input type="hidden" name="redirect" value="{redirect}">
  <label for="user_name">User name</label> <input id="user_name" name="user_name"><br>
  <label for="user_password">Password</label> <input id="user_password" name="user_password" type="password"><br>
  <button id="sysverb_login" type="submit">Log in</button>
</form></body></html>"""

SHELL_HTML = """<!doctype html><html><head><title>ServiceNow</title></head><body>
<macroponent-f51912f4c700201072b211d4d8c26010></macroponent-f51912f4c700201072b211d4d8c26010>
<script>
  const root = document.querySelector('macroponent-f51912f4c700201072b211d4d8c26010').attachShadow({mode: 'open'});
  root.innerHTML = '<sn-polaris-header></sn-polaris-header><sn-canvas-main></sn-canvas-main>';
  root.querySelector('sn-polaris-header').attachShadow({mode: 'open'}).innerHTML =
    '<input aria-label="Search" placeholder="Search"><button aria-label="All">All</button>';
  const c = root.querySelector('sn-canvas-main').attachShadow({mode: 'open'});
  const f = document.createElement('iframe');
  f.id = 'gsft_main'; f.name = 'gsft_main'; f.title = 'Main Content'; f.src = %s;
  f.style.cssText = 'width:1000px;height:650px;border:0';
  c.appendChild(f);
</script></body></html>"""

FORM_JS = r"""
window.g_ck = '%(token)s';
window.g_user = {userID: 'u_admin', userName: 'admin', getFullName: () => 'System Administrator'};
const META = %(meta)s;
const $ = id => document.getElementById(id);
const fid = n => META.table + '.' + n;
window.g_form = {
  getTableName: () => META.table, getUniqueValue: () => META.sys_id, isNewRecord: () => META.is_new,
  getFormElement: () => $(META.table + '.do'),
  _f: n => META.fields.find(f => f.name === n),
  getValue(n) { const e = $(fid(n)); return e ? e.value : ''; },
  getDisplayValue(n) { const d = $('sys_display.' + fid(n)); return d ? d.value : this.getValue(n); },
  setValue(n, v, dv) { const e = $(fid(n)); if (e) e.value = v; const d = $('sys_display.' + fid(n));
                       if (d) { d.value = dv !== undefined ? dv : v; d.classList.remove('ref_invalid'); } },
  isMandatory(n) { const f = this._f(n); return !!(f && f.mandatory); },
  getGlideUIElement(n) { const f = this._f(n); return f ? {fieldName: n, type: f.type, reference: f.ref || '',
                         mandatory: !!f.mandatory, label: f.label} : null; },
};
for (const f of META.fields.filter(f => f.type === 'reference' && !f.readonly)) {
  const d = $('sys_display.' + fid(f.name)), h = $(fid(f.name));
  d.addEventListener('change', () => {
    h.value = ''; const v = d.value.trim(); if (!v) return;
    setTimeout(async () => {   // autocomplete AJAX round trip
      const r = await fetch('/api/now/table/' + f.ref + '?sysparm_query=' + encodeURIComponent('name=' + v),
                            {headers: {'X-UserToken': g_ck, 'X-Mock-Autocomplete': '1'}});
      const res = (await r.json()).result;
      if (res.length === 1) { h.value = res[0].sys_id; d.value = res[0].name; d.classList.remove('ref_invalid'); }
      else d.classList.add('ref_invalid');
    }, 300);
  });
}
// Like ServiceNow: warn when leaving a form with unsaved changes
let dirty = false, submitting = false;
document.addEventListener('change', () => { dirty = true; }, true);
const _setValue = g_form.setValue.bind(g_form);
g_form.setValue = (...a) => { dirty = true; return _setValue(...a); };
window.addEventListener('beforeunload', e => { if (dirty && !submitting) { e.preventDefault(); e.returnValue = ''; } });
window.gsftSubmit = async (ctrl, form, action) => {
  submitting = true;
  const values = {};
  for (const f of META.fields) if (!f.readonly || f.type === 'reference') values[f.name] = g_form.getValue(f.name);
  const need = new Set(META.fields.filter(f => f.mandatory).map(f => f.name));
  for (const [f, v, req] of META.rules) if (values[f] === v) req.forEach(r => need.add(r));
  const missing = META.fields.filter(f => need.has(f.name) && !values[f.name]).map(f => f.label);
  if (missing.length) { submitting = false; alert('The following mandatory fields are not filled in: ' + missing.join(', ')); return false; }
  for (const f of META.fields.filter(f => f.type === 'reference' && !f.readonly))
    if ($('sys_display.' + fid(f.name)).value && !values[f.name]) { submitting = false; alert('Invalid reference: ' + f.label); return false; }
  if (action === 'sysverb_insert') {
    const r = await fetch('/mock/insert', {method: 'POST', body: JSON.stringify({table: META.table, values})});
    location.href = (await r.json()).redirect;
  } else {
    await fetch('/mock/save', {method: 'POST', body: JSON.stringify({table: META.table, sys_id: META.sys_id, values})});
    location.href = '/' + META.table + '.do?sys_id=' + META.sys_id + '&sysparm_msg=' + encodeURIComponent('Record updated');
  }
};
"""


class MockServiceNow:
    def __init__(self, port: int = 0):
        self.db = copy.deepcopy(SEED)
        self.api_enabled = True  # False = user has no REST rights (autocomplete still works)
        self.edit_button = True  # related list shows "Edit..." (slushbucket)
        self.search_mode = "keyup"  # slushbucket search: "keyup" (live) or "enter"
        self.related_delay_ms = 0  # >0: related lists appear later (like deferred loading in ServiceNow)
        self.lock = threading.Lock()
        mock = self

        class Handler(_Handler):
            server_mock = mock

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> "MockServiceNow":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()

    # helpers for tests
    def record(self, number: str) -> dict:
        return next(dict(r, sys_id=k, table=t) for t in TASK_TABLES for k, r in self.db[t].items() if r["number"] == number)

    incident = record

    def task_table(self, sys_id: str) -> str:
        return next((t for t in TASK_TABLES if sys_id in self.db[t]), "incident")

    def ci_name(self, sys_id: str) -> str:
        return self.db["cmdb_ci"].get(sys_id, {}).get("name", "")

    def affected(self, number: str) -> list:
        sid = self.record(number)["sys_id"]
        return [self.ci_name(r["ci_item"]) for r in self.db["task_ci"].values() if r["task"] == sid]

    def resolve(self, table: str, row: dict, path: str):
        """(value, display) of a (dot-walked) field, like the Table API."""
        base = "cmdb_ci" if table.startswith("cmdb_ci") else table
        task_like = table in TASK_TABLES or table == "task"
        if path in ("comments", "work_notes", "comments_and_work_notes") and task_like:
            text = self.journal_text(row.get("sys_id", ""), path)
            return text, text
        if path in row or "." not in path:
            v = str(row.get(path, ""))
            if path == "active" and task_like and "active" not in row:
                v = "false" if row.get("state") in INACTIVE_STATES.get(row.get("sys_class_name") or table, ()) else "true"
            ref = REFS.get((base, path)) or (REFS.get(("task", path)) if task_like else None)
            if ref:
                return v, self.display(ref, v)
            if path in ("opened_at", "sys_updated_on") and v:
                d, t = v.split(" ")
                return v, f"{d[8:10]}/{d[5:7]}/{d[0:4]} {t}"
            form = FORMS.get(row.get("sys_class_name") or table, [])
            choices = next((dict(f["choices"]) for f in form if f["name"] == path and f.get("choices")), None)
            if path == "sys_class_name":
                return v, {"incident": "Incident", "sc_task": "Catalog Task"}.get(v, v)
            return v, (choices.get(v, v) if choices else CHOICE_LABELS.get(path, {}).get(v, v))
        head, rest = path.split(".", 1)
        ref = REFS.get((base, head)) or (REFS.get(("task", head)) if task_like else None)
        if not ref or not row.get(head):
            return "", ""
        target_table = self.task_table(row[head]) if ref == "task" else ref
        target = self.db.get(STORE.get(target_table, target_table), {}).get(row[head], {})
        return self.resolve(target_table, dict(target), rest)

    def journal_text(self, sys_id: str, which: str) -> str:
        kinds = ("comments", "work_notes") if which == "comments_and_work_notes" else (which,)
        entries = [j for j in self.db["journal"] if j["element_id"] == sys_id and j["element"] in kinds]
        label = {"work_notes": "Work notes", "comments": "Additional comments"}
        out = []
        for j in sorted(entries, key=lambda j: j["created_on"], reverse=True):
            d, t = j["created_on"].split(" ")
            out.append(f"{d[8:10]}/{d[5:7]}/{d[0:4]} {t} - {j['created_by']} ({label[j['element']]})\n{j['value']}\n")
        return "\n".join(out)

    def display(self, ref_table: str, sys_id: str) -> str:
        if ref_table == "task":
            ref_table = self.task_table(sys_id)
        return self.db.get(ref_table, {}).get(sys_id, {}).get(DISPLAY.get(ref_table, "name"), "")


_OP = re.compile(r"^([A-Za-z0-9_.]+?)(ISNOTEMPTY|ISEMPTY|STARTSWITH|LIKE|!=|=)(.*)$")


def _parse_query(q: str):
    """Encoded query -> ([[cond, cond-or, ...], ...], (order field, desc))."""
    groups, order = [], None
    for part in q.split("^"):
        if not part or part == "EQ":
            continue
        if part.startswith("ORDERBYDESC"):
            order = (part[11:], True)
        elif part.startswith("ORDERBY"):
            order = (part[7:], False)
        elif part.startswith("OR") and groups and _OP.match(part[2:]):
            groups[-1].append(part[2:])
        else:
            groups.append([part])
    return groups, order


def _cond(m, table, row, cond) -> bool:
    mt = _OP.match(cond)
    if not mt:
        return True  # ServiceNow ignores what it cannot parse
    field, op, want = mt.groups()
    have = m.resolve(table, row, field)[0].lower()
    if want == "javascript:gs.getUserID()":
        wants = [ME]
    elif want == "javascript:getMyGroups()":
        wants = list(MY_GROUPS)
    else:
        wants = [want]
    wants = [w.lower() for w in wants]
    if op == "ISEMPTY":
        return have == ""
    if op == "ISNOTEMPTY":
        return have != ""
    if op == "LIKE":
        return any(w in have for w in wants)
    if op == "STARTSWITH":
        return any(have.startswith(w) for w in wants)
    if op == "!=":
        return have not in wants
    return have in wants


class _Handler(BaseHTTPRequestHandler):
    server_mock: MockServiceNow

    def log_message(self, *a):
        pass

    # --------------------------------------------------------------- utils
    def _send(self, code=200, body="", ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _logged_in(self) -> bool:
        return "JSESSIONID=ok" in (self.headers.get("Cookie") or "")

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/login.do":
            return self._send(body=LOGIN_HTML.format(error="", redirect=html.escape(qs.get("redirect", "/"))))
        if not self._logged_in():
            if u.path.startswith("/api/"):
                return self._json({"error": "not authenticated"}, 401)
            return self._send(302, "", headers={"Location": "/login.do?redirect=" + quote(self.path, safe="")})
        if u.path == "/":
            return self._send(body="<!doctype html><title>ServiceNow (mock)</title><h1>Welkom bij de mock-instance</h1>"
                                   '<p><a href="/incident.do?sysparm_query=number=INC0010001">INC0010001</a></p>')
        if u.path.startswith("/now/nav/ui/classic/params/target/"):
            target = "/" + unquote(u.path[len("/now/nav/ui/classic/params/target/"):]).lstrip("/")
            return self._send(body=SHELL_HTML % json.dumps(target))
        if u.path == "/sys_m2m_template.do":
            return self._send(body=self._m2m(qs.get("sysparm_collectionID", "")))
        if u.path == "/mock/ci_search":
            q = qs.get("q", "").lower()
            with self.server_mock.lock:
                res = [{"sys_id": k, "name": v["name"]} for k, v in sorted(self.server_mock.db["cmdb_ci"].items(),
                                                                         key=lambda kv: kv[1]["name"]) if q in v["name"].lower()]
            return self._json({"result": res[:100]})
        if u.path.startswith("/api/now/table/"):
            return self._api(u.path.rsplit("/", 1)[-1], qs)
        table = u.path.strip("/").removesuffix(".do")
        if table in FORMS:
            return self._form(table, qs)
        self._send(404, "<h1>Not found</h1>")

    def _api(self, table, qs):
        if self.headers.get("X-UserToken") != TOKEN:
            return self._json({"error": "invalid token"}, 401)
        m = self.server_mock
        if not m.api_enabled and not self.headers.get("X-Mock-Autocomplete"):
            return self._json({"error": "forbidden"}, 403)
        groups, order = _parse_query(qs.get("sysparm_query", ""))
        mode = qs.get("sysparm_display_value", "false")
        fields = [f for f in qs.get("sysparm_fields", "").split(",") if f]
        out = []
        with m.lock:
            if table == "task":
                source = [(sid, dict(r, sys_class_name=t)) for t in TASK_TABLES for sid, r in m.db[t].items()]
            else:
                source = list(m.db.get(STORE.get(table, table), {}).items())
            rows = []
            for sid, rec in source:
                row = dict(rec, sys_id=sid)
                row.setdefault("sys_class_name", "cmdb_ci_computer" if table == "cmdb_ci" else table)
                if all(any(_cond(m, table, row, c) for c in g) for g in groups):
                    rows.append(row)
            if order:
                rows.sort(key=lambda r: str(r.get(order[0], "")), reverse=order[1])
            for row in rows:
                res = {}
                for f in (fields or list(row)):
                    v, d = m.resolve(table, row, f)
                    res[f] = {"value": v, "display_value": d} if mode == "all" else (d if mode == "true" else v)
                out.append(res)
        self._json({"result": out[: int(qs.get("sysparm_limit", 1000))]})

    def _form(self, table, qs):
        m = self.server_mock
        sys_id = qs.get("sys_id", "")
        query = dict(c.split("=", 1) for c in qs.get("sysparm_query", "").split("^") if "=" in c)
        rec = None
        with m.lock:
            recs = m.db[STORE.get(table, table)]
            if sys_id and sys_id != "-1":
                rec = recs.get(sys_id)
            elif "number" in query:
                sys_id = next((k for k, r in recs.items() if r.get("number") == query["number"]), "")
                rec = recs.get(sys_id)
            is_new = rec is None
            if is_new:
                sys_id = uuid.uuid4().hex[:12]
                rec = {f["name"]: query.get(f["name"], "") for f in FORMS[table]}
                if "number" in rec:
                    rec["number"] = ""
            rec = dict(rec)
        self._send(body=self._render(table, sys_id, rec, is_new, qs.get("sysparm_msg", "")))

    def _render(self, table, sys_id, rec, is_new, msg):
        m, e = self.server_mock, html.escape
        title = rec.get("number") or table
        parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(title)} | {table}</title>",
                 "<style>body{font-family:Arial;font-size:13px;margin:12px}.form-group{margin:6px 0}"
                 "label{display:inline-block;width:150px}.ref_invalid{background:#fdd}"
                 ".outputmsg_info{background:#e8f4fd;padding:6px;margin-bottom:8px}"
                 "table.list_table{border-collapse:collapse;margin-top:6px}"
                 "table.list_table td,table.list_table th{border:1px solid #ccc;padding:3px 8px}</style></head><body>"]
        if msg:
            parts.append(f'<div class="outputmsg_info"><span class="outputmsg_text">{e(msg)}</span></div>')
        parts.append(f'<form id="{table}.do" name="{table}.do" onsubmit="return false"><div class="form_header">'
                     f"<b>{e(table)}</b> {e(rec.get('number', ''))} ")
        actions = [("sysverb_insert", "Submit")] if is_new else [("sysverb_update", "Update")]
        for action, text in actions:
            parts.append(f'<button type="button" class="form_action_button btn" id="{action}" data-action-name="{action}" '
                         f"onclick=\"gsftSubmit(this, this.form, '{action}')\">{text}</button> ")
        parts.append("</div>")
        for f in FORMS[table]:
            n, full = f["name"], f"{table}.{f['name']}"
            val = rec.get(n, "")
            star = (f'<span id="status.{full}" mandatory="true" class="required-marker">*</span>'
                    if f.get("mandatory") else "")
            hide = ' style="display:none"' if f.get("hidden") else ""
            parts.append(f'<div id="element.{full}" class="form-group"{hide}>{star}')
            if f["type"] == "reference":
                ro = " readonly" if f.get("readonly") else ""
                parts.append(f'<label for="sys_display.{full}"><span class="label-text">{e(f["label"])}</span></label>'
                             f'<input type="hidden" id="{full}" name="{full}" value="{e(val)}">'
                             f'<input id="sys_display.{full}" name="sys_display.{full}" value="{e(m.display(f["ref"], val))}"{ro}>')
            elif f["type"] == "choice":
                opts = "".join(f'<option value="{v}"{" selected" if v == val else ""}>{e(t)}</option>' for v, t in f["choices"])
                parts.append(f'<label for="{full}"><span class="label-text">{e(f["label"])}</span></label>'
                             f'<select id="{full}" name="{full}">{opts}</select>')
            elif f["type"] in ("text", "journal"):
                parts.append(f'<label for="{full}"><span class="label-text">{e(f["label"])}</span></label>'
                             f'<textarea id="{full}" name="{full}">{e(val) if f["type"] == "text" else ""}</textarea>')
            elif f.get("readonly"):
                parts.append(f'<label for="sys_readonly.{full}"><span class="label-text">{e(f["label"])}</span></label>'
                             f'<input id="sys_readonly.{full}" readonly value="{e(val)}">'
                             f'<input type="hidden" id="{full}" value="{e(val)}">')
            else:
                parts.append(f'<label for="{full}"><span class="label-text">{e(f["label"])}</span></label>'
                             f'<input type="hidden" id="sys_original.{full}" value="{e(val)}">'
                             f'<input id="{full}" name="{full}" value="{e(val)}">')
            parts.append("</div>")
        parts.append("</form>")
        if table in TASK_TABLES and not is_new:
            rows = [r for r in m.db["task_ci"].values() if r["task"] == sys_id]
            body = "".join(f'<tr class="list_row"><td>{e(m.ci_name(r["ci_item"]))}</td></tr>' for r in rows)
            rl = ('<div id="related_lists_wrapper"><div class="tabs2_section" tab_caption="Affected CIs" '
                         f'id="{table}.task_ci.task"><h4>Affected CIs</h4>'
                         + (f'<button type="button" class="list_header_button" data-action-name="7ca0a8d60a0a0b340080d6f48c880640" '
                            f'onclick="location.href=\'/sys_m2m_template.do?sysparm_collectionID={sys_id}\'">Edit...</button>'
                            if m.edit_button else '')
                         + f'<table class="list_table" id="{table}.task_ci.task_table"><thead><tr>'
                         '<th name="ci_item" glide_label="Configuration Item">Configuration Item</th></tr></thead>'
                         f"<tbody>{body}</tbody></table></div></div>")
            if m.related_delay_ms:
                parts.append(f'<div id="rl_holder"></div><script>setTimeout(() => document.getElementById("rl_holder")'
                             f'.innerHTML = {json.dumps(rl)}, {m.related_delay_ms});</script>')
            else:
                parts.append(rl)
        meta = {"table": table, "sys_id": sys_id, "is_new": is_new, "fields": FORMS[table],
                "rules": COND_MANDATORY.get(table, [])}
        parts.append(f"<script>{FORM_JS % {'token': TOKEN, 'meta': json.dumps(meta)}}</script></body></html>")
        return "".join(parts)

    def _m2m(self, task):
        m = self.server_mock
        with m.lock:
            table = m.task_table(task)
            cur = [(r["ci_item"], m.ci_name(r["ci_item"])) for r in m.db["task_ci"].values() if r["task"] == task]
        right = "".join(f'<option value="{k}">{html.escape(n)}</option>' for k, n in cur)
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Edit Members | CIs Affected</title></head><body>
<form id="sys_m2m_template.do" action="/sys_m2m_template.do" method="get">
<table id="filters_table"><tr><td>-- choose field --</td></tr></table>
<button type="button" id="_run">Run Filter</button>
<table><tr><td>
  <input type="search" id="_cmdb_ci" name="_cmdb_ci" aria-label="Collection: Search Available List"><br>
  <select id="select_0" name="select_0" multiple size="8" aria-label="Collection" style="width:220px"></select>
</td><td>
  <button type="button" id="add_to_collection_button" aria-label="Add selected options to the Affected CIs List listbox">&gt;</button><br>
  <button type="button" id="remove_from_collection_button" aria-label="Remove selected options from the Affected CIs List listbox">&lt;</button>
</td><td>
  <select id="select_1" name="select_1" multiple size="8" aria-label="Affected CIs List" style="width:220px">{right}</select>
</td></tr></table>
<button type="button" id="slush_back">Back</button>
<button type="button" id="sysverb_cancel">Cancel</button>
<button type="button" id="sysverb_save">Save</button>
</form>
<script>
const TASK = {json.dumps(task)}, BACK = '/{table}.do?sys_id=' + TASK, MODE = {json.dumps(m.search_mode)};
const left = document.getElementById('select_0'), right = document.getElementById('select_1');
const box = document.getElementById('_cmdb_ci');
let timer = null;
async function load(q) {{
  const r = await fetch('/mock/ci_search?q=' + encodeURIComponent(q));
  const have = new Set([...right.options].map(o => o.value));
  left.innerHTML = '';
  const all = (await r.json()).result;
  for (const c of (q ? all.slice(0, 100) : all.slice(-3)))
    if (!have.has(c.sys_id)) left.add(new Option(c.name, c.sys_id));
}}
load('');
if (MODE === 'keyup') box.addEventListener('keyup', () => {{ clearTimeout(timer); timer = setTimeout(() => load(box.value), 400); }});
box.addEventListener('keydown', e => {{ if (e.key === 'Enter') {{ e.preventDefault(); load(box.value); }} }});
const move = (a, b) => {{ for (const o of [...a.selectedOptions]) {{ o.selected = false; b.add(o); }} }};
document.getElementById('add_to_collection_button').onclick = () => move(left, right);
document.getElementById('remove_from_collection_button').onclick = () => move(right, left);
document.getElementById('sysverb_cancel').onclick = () => location.href = BACK;
document.getElementById('slush_back').onclick = () => location.href = BACK;
document.getElementById('sysverb_save').onclick = async () => {{
  await fetch('/mock/m2m', {{method: 'POST', body: JSON.stringify({{task: TASK, ci_ids: [...right.options].map(o => o.value)}})}});
  location.href = BACK + '&sysparm_msg=' + encodeURIComponent('Affected CIs updated');
}};
</script></body></html>"""

    # --------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        m = self.server_mock
        if u.path == "/login.do":
            form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
            if form.get("user_name") == "admin" and form.get("user_password") == "admin":
                return self._send(302, "", headers={"Location": form.get("redirect") or "/",
                                                     "Set-Cookie": "JSESSIONID=ok; Path=/"})
            return self._send(body=LOGIN_HTML.format(error="<p class='outputmsg_error'>User name or password invalid</p>",
                                                     redirect=html.escape(form.get("redirect", "/"))))
        if not self._logged_in():
            return self._json({"error": "not authenticated"}, 401)
        data = json.loads(self._body() or b"{}")
        with m.lock:
            if u.path == "/mock/save":
                rec = m.db[STORE.get(data["table"], data["table"])][data["sys_id"]]
                form = FORMS[data["table"]]
                for f in form:
                    text = data["values"].get(f["name"], "")
                    if f["type"] == "journal" and text.strip():
                        m.db["journal"].append({"element_id": data["sys_id"], "element": f["name"], "value": text,
                                                "created_by": "System Administrator", "created_on": "2026-10-22 16:30:00"})
                names = {f["name"] for f in form if not f.get("readonly") and f["type"] != "journal"}
                rec.update({k: v for k, v in data["values"].items() if k in names and k != "number"})
                if "sys_updated_on" in rec:
                    rec["sys_updated_on"] = "2026-10-22 16:30:00"
                return self._json({"ok": True})
            if u.path == "/mock/m2m":
                task = data["task"]
                keep = {k: r for k, r in m.db["task_ci"].items() if r["task"] != task}
                for ci in data["ci_ids"]:
                    keep[uuid.uuid4().hex[:12]] = {"task": task, "ci_item": ci}
                m.db["task_ci"] = keep
                return self._json({"ok": True})
            if u.path == "/mock/insert":
                sid = uuid.uuid4().hex[:12]
                m.db[data["table"]][sid] = data["values"]
                if data["table"] == "task_ci":
                    task = data["values"]["task"]
                    redirect = f"/{m.task_table(task)}.do?sys_id={task}&sysparm_msg=" + quote("Affected CI added")
                else:
                    redirect = f"/{data['table']}.do?sys_id={sid}"
                return self._json({"sys_id": sid, "redirect": redirect})
        self._json({"error": "unknown"}, 404)


if __name__ == "__main__":
    srv = MockServiceNow(8765).start()
    print(f"Mock ServiceNow draait op {srv.url}  (login: admin / admin). Ctrl+C om te stoppen.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
