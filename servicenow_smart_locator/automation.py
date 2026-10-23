"""
ServiceNow automations on top of SmartLocator.

    bot = ServiceNowBot(page, "https://dev123.service-now.com", dry_run=False)
    print(bot.add_ci("INC0010001", "PC-0042"))                    # Configuration item field
    print(bot.add_ci("INC0010001", "PC-0042", mode="affected"))   # Affected CIs related list
    print(bot.update_record("INC0010001", {"State": "In Progress"}))

Or as plain-language commands (Dutch or English):

    run_commands(bot, ["voeg PC-0042 toe aan INC0010001",
                       "voeg LAPTOP-7 toe aan INC0010002 als affected ci",
                       "zet state van INC0010001 op In Progress"])

Safety:
* ``dry_run=True`` (the default) does everything except saving.
* After saving, the bot re-reads the record and checks the change is really there.
* A filled Configuration item is never overwritten unless ``overwrite=True``.
* Every action leaves a screenshot in ``automation_screenshots/``.

Only language-independent identifiers are used (field names such as
``cmdb_ci``, UI action names such as ``sysverb_update_and_stay``), so this
works whatever language the ServiceNow user interface is set to.
"""
from __future__ import annotations

import csv
import datetime as _dt
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError, Frame, Page

from .smart_locator import SmartLocator, SmartLocatorError

#: Record number prefix -> table
NUMBER_PREFIXES = {
    "INC": "incident", "RITM": "sc_req_item", "REQ": "sc_request", "SCTASK": "sc_task",
    "CHG": "change_request", "CTASK": "change_task", "PRB": "problem", "PTASK": "problem_task",
    "TASK": "task", "KB": "kb_knowledge",
}
#: Reference fields whose target table we know without asking g_form
REFERENCE_TABLES = {"cmdb_ci": "cmdb_ci", "ci_item": "cmdb_ci", "caller_id": "sys_user",
                    "assigned_to": "sys_user", "assignment_group": "sys_user_group", "opened_by": "sys_user"}
#: Display field used to look a record up by name in the Table API
DISPLAY_FIELDS = {"sys_user": "name", "sys_user_group": "name", "cmdb_ci": "name"}


class AutomationError(RuntimeError):
    pass


@dataclass
class ActionResult:
    ok: bool
    action: str
    record: str
    message: str
    dry_run: bool = False
    skipped: bool = False
    before: Dict[str, str] = field(default_factory=dict)
    after: Dict[str, str] = field(default_factory=dict)
    screenshot: Optional[str] = None
    seconds: float = 0.0

    @property
    def status(self) -> str:
        if not self.ok:
            return "FOUT"
        if self.skipped:
            return "OVERGESLAGEN"
        return "PROEFRUN" if self.dry_run else "OK"

    def __str__(self) -> str:
        return f"[{self.status}] {self.record}: {self.message}"


def table_for_number(number: str) -> str:
    m = re.match(r"^([A-Za-z]+)\d+$", number.strip())
    if not m or m.group(1).upper() not in NUMBER_PREFIXES:
        raise AutomationError(f"Onbekend nummerformaat '{number}'. Geef de tabel mee (table=...).")
    return NUMBER_PREFIXES[m.group(1).upper()]


def launch(p, browser: str = "chromium", profile: Optional[str] = None, headless: bool = False):
    """Start a browser with a persistent profile (keeps your ServiceNow / SSO login)."""
    profile = profile or str(Path.home() / ".snsl_inspector_profile")
    kw = {"headless": headless}
    if not headless:
        kw.update(no_viewport=True, args=["--start-maximized"])
    if browser != "chromium":
        kw["channel"] = browser
    try:
        ctx = p.chromium.launch_persistent_context(profile, **kw)
    except PlaywrightError as first:
        # Most common cause: the profile is still locked (another bot window, or a browser that crashed).
        lock = Path(profile) / "lockfile"   # Windows: held open while a browser uses the profile
        in_use = False
        if lock.exists():
            try:
                lock.unlink()               # stale lock left by a crashed browser
            except OSError:
                in_use = True               # really in use by a running browser
        ctx = None
        if not in_use:
            time.sleep(1.5)
            try:
                ctx = p.chromium.launch_persistent_context(profile, **kw)
            except PlaywrightError:
                ctx = None
        if ctx is None:
            raise AutomationError(browser_start_help(profile, in_use, str(first).splitlines()[0])) from None
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def browser_start_help(profile: str, in_use: bool, error: str = "") -> str:
    """Dutch explanation + fixes when the browser won't start."""
    lines = ["De browser start niet" + (f" ({error})" if error else "") + "."]
    if in_use:
        lines.append(f"Het browserprofiel {profile} is in gebruik door een ander venster.")
    lines += [
        "Probeer dit, in deze volgorde:",
        "  1. Sluit andere vensters van de bot (inspector, run_automation, een tweede sn.py) en probeer opnieuw.",
        "  2. Draai in de .venv van het project: je prompt moet met (.venv) beginnen. Anders:",
        "       .venv\\Scripts\\Activate.ps1      (of start met: .venv\\Scripts\\python.exe sn.py)",
        "  3. Achtergebleven browsers van de bot afsluiten (PowerShell):",
        '       Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*ms-playwright*" } | Stop-Process -Force',
        "  4. Helpt niets? Het profiel is beschadigd of door een andere Chromium-versie gemaakt. Zet het opzij",
        "     (je moet daarna één keer opnieuw inloggen):",
        f'       Rename-Item "{profile}" "{Path(profile).name}_oud"',
    ]
    return "\n".join(lines)


# =========================================================================== #
class ServiceNowBot:
    """High-level ServiceNow actions. See module docstring."""

    def __init__(self, page: Page, instance: str, *, dry_run: bool = True, ui: str = "classic",
                 timeout: float = 30_000, user: Optional[str] = None, password: Optional[str] = None,
                 screenshot_dir: Optional[str] = "automation_screenshots",
                 log: Callable[[str], None] = print, login_wait_s: float = 300):
        self.page = page
        self.instance = instance.rstrip("/")
        self.dry_run = dry_run
        self.ui = ui
        self.timeout = timeout
        self.user, self.password = user, password
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.log = log
        self.login_wait_s = login_wait_s
        self.sn = SmartLocator(page, timeout=timeout)
        self._dialogs: List[str] = []
        page.on("dialog", self._on_dialog)

    # ------------------------------------------------------------ navigation
    def url(self, path: str) -> str:
        path = path.lstrip("/")
        if self.ui == "polaris" and path:
            return f"{self.instance}/now/nav/ui/classic/params/target/{quote(path, safe='')}"
        return f"{self.instance}/{path}"

    def goto(self, path: str) -> None:
        self.page.goto(self.url(path), wait_until="domcontentloaded", timeout=self.timeout)
        if self.ensure_logged_in(path):
            self.page.goto(self.url(path), wait_until="domcontentloaded", timeout=self.timeout)
        self.sn.wait_until_ready(timeout=self.timeout)

    def ensure_logged_in(self, path: str = "") -> bool:
        """Log in when the login page shows. Returns True if a login happened."""
        if not self._login_frame():
            return False
        if self.user and self.password:
            fr = self._login_frame()
            fr.locator("#user_name").fill(self.user)
            fr.locator("#user_password").fill(self.password)
            fr.locator("#sysverb_login").click()
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(500)
            if self._login_frame():
                raise AutomationError("Inloggen mislukt: controleer gebruikersnaam/wachtwoord.")
            self.log("Ingelogd.")
            return True
        self.log("Log in in het browservenster (ook SSO is goed). Ik wacht...")
        deadline = time.monotonic() + self.login_wait_s
        while self._login_frame() or "login" in self.page.url.lower():
            if time.monotonic() > deadline:
                raise AutomationError("Niet op tijd ingelogd.")
            self.page.wait_for_timeout(1000)
        self.page.wait_for_load_state("domcontentloaded")
        self.log("Ingelogd.")
        return True

    def _login_frame(self) -> Optional[Frame]:
        for fr in self.page.frames:
            try:
                if fr.locator("#user_name").count() and fr.locator("#user_password").count():
                    return fr
            except PlaywrightError:
                continue
        return None

    def open_record(self, number: Optional[str] = None, table: Optional[str] = None,
                    sys_id: Optional[str] = None, allow_new: bool = False) -> Frame:
        """Open a record form by number or sys_id. Returns the frame holding g_form."""
        table = table or table_for_number(number)
        query = f"sys_id={sys_id}" if sys_id else f"sysparm_query=number={number}"
        self.goto(f"{table}.do?{query}")
        fr = self.gform()
        if not allow_new and fr.evaluate("() => g_form.isNewRecord()"):
            raise AutomationError(f"{number or sys_id} niet gevonden in tabel {table}.")
        return fr

    def gform(self) -> Frame:
        return self.sn.gform_frame(timeout=self.timeout)

    # -------------------------------------------------------------- fields
    def read(self, field_name: str) -> Tuple[str, str]:
        """(value, display value) of a field on the open form."""
        return tuple(self.gform().evaluate(r"""f => {
            const v = g_form.getValue(f) || '';
            const t = g_form.getTableName();
            const sd = document.getElementById('sys_display.' + t + '.' + f);
            if (sd) return [v, sd.value];
            const el = document.getElementById(t + '.' + f);
            if (el && el.tagName === 'SELECT' && el.selectedIndex >= 0) return [v, el.options[el.selectedIndex].text.trim()];
            let d = ''; try { d = g_form.getDisplayValue(f) || ''; } catch (e) {}
            return [v, d || v];
        }""", field_name))

    def has_field(self, field_name: str) -> bool:
        """True if the open form contains the field (also when hidden by a UI policy)."""
        return bool(self.gform().evaluate(r"""f => {
            const t = g_form.getTableName();
            if (document.getElementById('element.' + t + '.' + f) || document.getElementById(t + '.' + f)) return true;
            try { return typeof g_form.hasField === 'function' && g_form.hasField(f); } catch (e) { return false; }
        }""", field_name))

    def set_reference(self, field_name: str, display: str, ref_table: Optional[str] = None) -> str:
        """Type a value into a reference field like a user and make sure it resolves.

        Falls back to an exact lookup through the Table API (same session) when
        the autocomplete doesn't resolve to exactly ``display``.
        """
        fr = self.gform()
        ref_table = ref_table or fr.evaluate(
            "f => { try { return g_form.getGlideUIElement(f).reference || '' } catch (e) { return '' } }", field_name
        ) or REFERENCE_TABLES.get(field_name)
        # Check first (fast, clear error) when the Table API is allowed for this user.
        matches: Optional[List[dict]] = None
        if ref_table:
            try:
                matches = self.lookup(ref_table, display)
            except AutomationError:
                matches = None  # no API rights: rely on the UI only
        if matches is not None and not matches:
            raise AutomationError(f"'{display}' bestaat niet in {ref_table}.")
        if matches is not None and len(matches) > 1:
            raise AutomationError(f"'{display}' is niet uniek in {ref_table}: {len(matches)} records met die naam.")

        self.sn.fill(field_name, display)
        if self._wait_reference(field_name, display, 5):
            return self.read(field_name)[0]
        if not matches:
            raise AutomationError(f"'{display}' wordt niet herkend in veld {field_name}.")
        sys_id = matches[0]["sys_id"]
        fr = self.gform()
        fr.evaluate("a => g_form.setValue(a[0], a[1], a[2])", [field_name, sys_id, matches[0].get("name", display)])
        if not self._wait_reference(field_name, display, 3):
            raise AutomationError(f"Kon {field_name} niet op '{display}' zetten.")
        return sys_id

    def _wait_reference(self, field_name: str, display: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                v, d = self.read(field_name)
                if v and d.strip().lower() == display.strip().lower():
                    return True
            except PlaywrightError:
                pass
            self.page.wait_for_timeout(200)
        return False

    def _api_frame(self) -> Frame:
        """A frame with ServiceNow's session token (g_ck), so fetch() to the Table API works as the logged-in user."""
        for _ in range(2):
            for fr in sorted(self.page.frames, key=lambda f: f != self.page.main_frame):
                try:
                    if fr.evaluate("() => !!window.g_ck"):
                        return fr
                except PlaywrightError:
                    continue
            self.goto("incident.do?sys_id=-1")  # any classic form carries the token; nothing is saved
        raise AutomationError("Geen ServiceNow-sessie gevonden (ben je ingelogd?).")

    def api(self, table: str, query: str, fields: str, limit: int = 50, display: str = "true") -> List[dict]:
        """Table API GET as the logged-in user. Raises AutomationError when it is not allowed."""
        res = self._api_frame().evaluate(r"""async ([t, q, f, n, d]) => {
            const r = await fetch('/api/now/table/' + t + '?sysparm_display_value=' + d + '&sysparm_exclude_reference_link=true' +
                                  '&sysparm_limit=' + n + '&sysparm_query=' + encodeURIComponent(q) + '&sysparm_fields=' + f,
                                  {headers: {'Accept': 'application/json', 'X-UserToken': window.g_ck || ''}});
            if (!r.ok) return {error: r.status};
            return (await r.json()).result;
        }""", [table, query, fields, limit, display])
        if isinstance(res, dict):
            raise AutomationError(f"Table API gaf HTTP {res.get('error')} voor {table}"
                                  + (" (geen rechten?)" if res.get("error") in (401, 403) else "") + ".")
        return res

    def lookup(self, table: str, name: str, field_name: Optional[str] = None) -> List[dict]:
        """Exact-name lookup via the Table API, using the logged-in browser session."""
        f = field_name or DISPLAY_FIELDS.get(table, "name")
        res = self._api_frame().evaluate(r"""async ([table, f, name]) => {
            const q = encodeURIComponent(f + '=' + name);
            const r = await fetch('/api/now/table/' + table + '?sysparm_query=' + q + '&sysparm_fields=sys_id,' + f +
                                  '&sysparm_limit=10', {headers: {'Accept': 'application/json', 'X-UserToken': window.g_ck || ''}});
            if (!r.ok) return {error: r.status};
            return (await r.json()).result;
        }""", [table, f, name])
        if isinstance(res, dict) and "error" in res:
            raise AutomationError(f"Table API gaf HTTP {res['error']} voor {table} (rechten?).")
        return [dict(r, name=r.get(f, name)) for r in res]

    def api_get(self, table: str, query: str, fields: str = "sys_id") -> Optional[List[dict]]:
        """Table API GET with display values. Returns None when the API is not allowed."""
        try:
            res = self._api_frame().evaluate(r"""async ([t, q, f]) => {
                const r = await fetch('/api/now/table/' + t + '?sysparm_display_value=true&sysparm_limit=500&sysparm_query=' +
                                      encodeURIComponent(q) + '&sysparm_fields=' + f,
                                      {headers: {'Accept': 'application/json', 'X-UserToken': window.g_ck || ''}});
                return r.ok ? (await r.json()).result : null;
            }""", [table, query, fields])
        except PlaywrightError:
            return None
        return res

    # ------------------------------------------------------------- actions
    def run_action(self, action_name: str, fallbacks: Iterable[str] = ()) -> None:
        """Run a UI action (button, or gsftSubmit when it only lives in the context menu)
        and wait until the form has reloaded."""
        fr = self.gform()
        fr.evaluate("() => { window.__snslOld = true }")
        self._dialogs.clear()
        for action in (action_name, *fallbacks):
            try:
                self.sn.locate_button(action, timeout=300).locator.click()
                break
            except SmartLocatorError:
                if fr.evaluate("() => typeof window.gsftSubmit === 'function'"):
                    fr.evaluate("a => gsftSubmit(null, g_form.getFormElement(), a)", action)
                    break
        else:
            raise AutomationError(f"UI action {action_name} niet gevonden.")
        self._wait_reload()
        errors = self.messages("error")
        if errors:
            raise AutomationError("ServiceNow meldt: " + " | ".join(errors))

    def save(self) -> None:
        self.run_action("sysverb_update_and_stay", fallbacks=["sysverb_update"])

    def _wait_reload(self) -> None:
        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
            if self._dialogs:
                raise AutomationError("ServiceNow blokkeert opslaan: " + self._dialogs[-1])
            for fr in self.page.frames:
                try:
                    if fr.evaluate("() => !window.__snslOld && document.readyState === 'complete' && !!window.g_form"):
                        self.sn.wait_until_ready(timeout=self.timeout)
                        return
                except PlaywrightError:
                    continue
        raise AutomationError("Formulier is na opslaan niet opnieuw geladen (time-out).")

    def messages(self, kind: Optional[str] = None) -> List[str]:
        out = []
        for fr in self.page.frames:
            try:
                out += fr.evaluate(r"""k => [...document.querySelectorAll(
                    '.outputmsg_error, .outputmsg_info, .outputmsg_warning, .notification-error, .notification-info, .notification-warning')]
                    .filter(e => e.offsetParent !== null && (!k || e.className.includes(k)))
                    .map(e => e.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean)""", kind)
            except PlaywrightError:
                continue
        return list(dict.fromkeys(out))

    def _on_dialog(self, dialog) -> None:
        if dialog.type == "beforeunload":
            # "Leave page? Unsaved changes" - the bot navigates on purpose (e.g. after a dry run): leave.
            try:
                dialog.accept()
            except PlaywrightError:
                pass
            return
        self._dialogs.append(dialog.message)
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass

    def screenshot(self, name: str) -> Optional[str]:
        if not self.screenshot_dir:
            return None
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / f"{_dt.datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9_-]+', '_', name)}.png"
        try:
            self.page.screenshot(path=str(path))
            return str(path)
        except PlaywrightError:
            return None

    # ============================================================ recipes
    def add_ci(self, number: str, ci: str, mode: str = "field", overwrite: bool = False) -> ActionResult:
        """Add a PC / CI to a task record (incident, RITM, change, ...).

        mode="field":    set the Configuration item field (cmdb_ci)
        mode="affected": add a row to the Affected CIs related list (task_ci)
        """
        if mode not in ("field", "affected"):
            raise ValueError("mode moet 'field' of 'affected' zijn")
        return self._timed(f"add_ci:{mode}", number, lambda r: (
            self._add_ci_field(number, ci, overwrite, r) if mode == "field" else self._add_ci_affected(number, ci, r)))

    def _add_ci_field(self, number: str, ci: str, overwrite: bool, r: ActionResult) -> None:
        self.open_record(number)
        if not self.has_field("cmdb_ci"):
            table = self.gform().evaluate("() => g_form.getTableName()")
            r.ok, r.message = False, (f"Dit formulier ({table}) heeft geen veld Configuration item (cmdb_ci). "
                                      f"Gebruik --affected (of 'als affected ci') om de PC aan Affected CIs toe te voegen.")
            return
        _, before = self.read("cmdb_ci")
        r.before = {"cmdb_ci": before}
        if before.strip().lower() == ci.strip().lower():
            r.skipped, r.message = True, f"{ci} staat al in Configuration item."
            return
        if before and not overwrite:
            r.ok, r.message = False, (f"Configuration item is al gevuld met '{before}'. Gebruik overschrijven "
                                      f"(--overschrijven) of voeg toe als affected CI (--affected).")
            return
        self.set_reference("cmdb_ci", ci)
        if self.dry_run:
            r.dry_run, r.message = True, f"Configuration item ingevuld met {ci}, NIET opgeslagen (proefrun)."
            return
        self.save()
        _, after = self.read("cmdb_ci")
        r.after = {"cmdb_ci": after}
        if after.strip().lower() != ci.strip().lower():
            raise AutomationError(f"Na opslaan staat er '{after}' in Configuration item i.p.v. '{ci}'.")
        r.message = f"Configuration item: '{before or '-'}' -> '{after}' (opgeslagen en gecontroleerd)."

    def _add_ci_affected(self, number: str, ci: str, r: ActionResult) -> None:
        fr = self.open_record(number)
        sys_id = fr.evaluate("() => g_form.getUniqueValue()")
        existing = self.affected_cis(sys_id)
        r.before = {"affected_cis": ", ".join(existing)}
        if ci.lower() in (x.lower() for x in existing):
            r.skipped, r.message = True, f"{ci} staat al bij Affected CIs."
            return
        if self._click_affected_edit():
            return self._add_ci_slushbucket(number, ci, sys_id, r)
        # No "Edit..." button: create the task_ci record through its own form instead
        self.goto(f"task_ci.do?sys_id=-1&sysparm_query=task={sys_id}")
        self.set_reference("ci_item", ci)
        if self.dry_run:
            r.dry_run, r.message = True, (f"Affected CI-formulier (nieuwe regel voor Affected CIs) ingevuld met {ci}, "
                                          f"NIET opgeslagen (proefrun).")
            return
        self.run_action("sysverb_insert")
        self.open_record(number)
        after = self.affected_cis(sys_id)
        r.after = {"affected_cis": ", ".join(after)}
        if ci.lower() not in (x.lower() for x in after):
            raise AutomationError(f"{ci} staat na opslaan niet in Affected CIs.")
        r.message = f"{ci} toegevoegd aan Affected CIs (nu {len(after)}: {', '.join(after)})."

    # ------------------------------------------- Affected CIs via "Edit..."
    #: sys_id of the out-of-the-box "Edit..." UI action on related lists
    EDIT_M2M_ACTION = "7ca0a8d60a0a0b340080d6f48c880640"

    def _click_affected_edit(self) -> bool:
        """Click "Edit..." of the Affected CIs related list. False if the list has no such button."""
        js = r"""act => {
            const isEdit = b => b.getAttribute('data-action-name') === act || b.getAttribute('gsft_id') === act ||
                                /^(edit|bewerken)\s*(\.\.\.|…)$/i.test((b.innerText || b.value || b.textContent || '').trim());
            const btns = [...document.querySelectorAll('button, input[type=button], input[type=submit], a')].filter(isEdit);
            // the button belongs to the nearest ancestor that contains a list; that list must be task_ci
            const belongs = b => { for (let e = b.parentElement, i = 0; e && i < 15; e = e.parentElement, i++) {
                                     if (e.querySelector('[id*="task_ci"]')) return true;
                                     if (e.querySelector('table.list_table, table[id$="_table"]')) return false; }
                                   return false; };
            document.querySelectorAll('[data-snsl-target]').forEach(e => e.removeAttribute('data-snsl-target'));
            let target = btns.find(belongs);
            const lists = [...document.querySelectorAll('[id*="task_ci"]')].map(e => e.tagName.toLowerCase() + '#' + e.id);
            // only one Edit... on the page and an Affected CIs list exists: that's the one
            if (!target && btns.length === 1 && lists.length) target = btns[0];
            if (target) { target.setAttribute('data-snsl-target', 'edit'); return 'ok'; }
            return 'Edit...-knoppen: ' + btns.length + ', task_ci-elementen: ' + (lists.slice(0, 4).join(' ') || 'geen');
        }"""
        found = ""
        deadline = time.monotonic() + 10  # related lists often load a few seconds after the form
        while time.monotonic() < deadline:
            try:
                found = self.gform().evaluate(js, self.EDIT_M2M_ACTION)
            except PlaywrightError:
                found = "pagina laadde opnieuw"
            if found == "ok":
                break
            self.page.wait_for_timeout(500)
        if found != "ok":
            self.log(f"  Edit... bij Affected CIs niet gevonden ({found}); ik gebruik het Affected CI-formulier.")
            self._affected_route = "formulier"
            return False
        self._affected_route = "Edit..."
        fr = self.gform()
        fr.evaluate("() => { window.__snslOld = true }")
        fr.locator('[data-snsl-target="edit"]').click()
        return True

    def _slush_frame(self) -> Frame:
        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            for fr in self.page.frames:
                try:
                    if fr.locator("#select_0").count() and fr.locator("#select_1").count() \
                            and fr.locator("#add_to_collection_button").count():
                        return fr
                except PlaywrightError:
                    continue
            self.page.wait_for_timeout(250)
        raise AutomationError("Het Edit...-scherm (lijst met pijltjes) is niet geopend.")

    @staticmethod
    def _options(fr: Frame, sel: str) -> List[dict]:
        return fr.evaluate("s => [...document.querySelector(s).options].map(o => ({value: o.value, text: o.text.trim()}))", sel)

    def _slush_search(self, fr: Frame, text: str) -> dict:
        """Find exactly one option for ``text`` in the left list, typing in its search box."""
        want = text.strip().lower()

        def pick(opts):
            exact = [o for o in opts if o["text"].lower() == want]
            if len(exact) == 1:
                return exact[0], None
            if len(exact) > 1:
                return None, f"'{text}' staat {len(exact)} keer in de lijst (niet uniek)"
            word = re.compile(r"(?<![\w-])" + re.escape(want) + r"(?![\w-])")
            part = [o for o in opts if word.search(o["text"].lower())]
            if len(part) == 1:
                return part[0], None
            if len(part) > 1:
                return None, f"'{text}' past op {len(part)} regels: " + ", ".join(o["text"] for o in part[:8])
            return None, ""

        opts = self._options(fr, "#select_0")
        hit, err = pick(opts)
        if hit or err:
            return self._raise_or(hit, err)
        box = fr.locator('#_cmdb_ci, input[type="search"]').first
        box.fill("")
        box.press_sequentially(text, delay=40)
        for attempt in ("typen", "Enter"):
            if attempt == "Enter":
                url = fr.url
                box.press("Enter")
                if fr.url != url:
                    raise AutomationError("Enter in het zoekveld verliet het Edit...-scherm.")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                self.page.wait_for_timeout(300)
                opts = self._options(fr, "#select_0")
                hit, err = pick(opts)
                if hit or err:
                    return self._raise_or(hit, err)
        seen = ", ".join(o["text"] for o in opts[:10]) or "(lege lijst)"
        raise AutomationError(f"'{text}' niet gevonden in de linkerlijst na zoeken. Zichtbaar: {seen}")

    @staticmethod
    def _raise_or(hit, err):
        if err:
            raise AutomationError(err)
        return hit

    def _add_ci_slushbucket(self, number: str, ci: str, task_sys_id: str, r: ActionResult) -> None:
        fr = self._slush_frame()
        right = self._options(fr, "#select_1")
        if any(o["text"].lower() == ci.lower() for o in right):
            r.skipped, r.message = True, f"{ci} staat al bij Affected CIs."
            self._slush_leave(fr, "#sysverb_cancel")
            return
        opt = self._slush_search(fr, ci)
        fr.locator("#select_0").select_option(value=opt["value"])
        fr.locator("#add_to_collection_button").click()
        self.page.wait_for_timeout(300)
        if not any(o["value"] == opt["value"] for o in self._options(fr, "#select_1")):
            raise AutomationError(f"{opt['text']} kon niet naar de rechterlijst worden verplaatst.")
        if self.dry_run:
            r.dry_run = True
            r.message = f"{opt['text']} gevonden en naar rechts gezet in Edit..., NIET opgeslagen (proefrun, Cancel geklikt)."
            r.screenshot = self.screenshot(f"{number}_slushbucket")
            self._slush_leave(fr, "#sysverb_cancel")
            return
        self._slush_leave(fr, "#sysverb_save")
        after = self.affected_cis(task_sys_id)
        r.after = {"affected_cis": ", ".join(after)}
        if opt["text"].lower() not in (x.lower() for x in after):
            raise AutomationError(f"{opt['text']} staat na opslaan niet in Affected CIs.")
        r.message = f"{opt['text']} toegevoegd aan Affected CIs via Edit... (nu {len(after)}: {', '.join(after)})."

    def _slush_leave(self, fr: Frame, button: str) -> None:
        fr.evaluate("() => { window.__snslOld = true }")
        self._dialogs.clear()
        fr.locator(button).first.click()
        self._wait_reload()

    def affected_cis(self, task_sys_id: str) -> List[str]:
        """CI names in the Affected CIs list. Table API first, the related list on the form as fallback."""
        rows = self.api_get("task_ci", f"task={task_sys_id}", "ci_item")
        if rows is not None:
            vals = [(x["ci_item"]["display_value"] if isinstance(x.get("ci_item"), dict) else x.get("ci_item", ""))
                    for x in rows]
            return [v for v in vals if v]
        from .scraper import scrape  # local import: scraper imports this module for its CLI
        deadline = time.monotonic() + 10  # related lists may load seconds after the form
        while True:
            names, seen_list = [], False
            for fr in scrape(self.page)["frames"]:
                for lst in fr["lists"]:
                    if "task_ci" in lst["id"] or re.search(r"affected|getroffen", lst["title"], re.I):
                        seen_list = True
                        for row in lst["rows"]:
                            cells = list(row.values()) if isinstance(row, dict) else row
                            names += [c for c in cells if c]
            if seen_list or time.monotonic() > deadline:
                return names
            self.page.wait_for_timeout(500)

    # ------------------------------------------------------ tickets
    LIST_FIELDS = "sys_id,number,opened_at,sys_updated_on,short_description,state,priority,assigned_to,sys_class_name"

    def list_records(self, table: str, query: str, limit: int = 50) -> List[dict]:
        """Rows (display values) for a list, e.g. the group's open incidents."""
        return self.api(table, query, self.LIST_FIELDS, limit=limit)

    def journal(self, table: str, sys_id: str) -> List[dict]:
        """Work notes + comments, newest first: [{time, who, kind, text}]. Empty when not readable."""
        try:
            rows = self.api(table, f"sys_id={sys_id}", "comments_and_work_notes", limit=1)
        except AutomationError:
            return []
        return parse_journal(rows[0].get("comments_and_work_notes", "") if rows else "")

    def record_details(self, number: str) -> dict:
        """Everything on a ticket: open it in the browser and read the form, related lists and activity."""
        from .scraper import scrape  # local import: scraper imports this module
        self.open_record(number)
        data = scrape(self.page)
        fr = next((f for f in data["frames"] if f["form"]), None)
        if not fr:
            raise AutomationError(f"Formulier van {number} niet gevonden.")
        form = fr["form"]
        fields, variables = [], []
        for f in form["fields"]:
            if f["type"] in ("journal_input", "journal") or f["name"] in ("work_notes", "comments"):
                continue
            item = {"name": f["name"], "label": f["label"] or f["name"], "value": f["display_value"],
                    "visible": f["visible"], "type": f["type"]}
            (variables if re.match(r"^(VE|IO:|ni\.VE)", f["name"]) else fields).append(item)
        return {"number": number.upper(), "table": form["table"], "sys_id": form["sys_id"], "fields": fields,
                "variables": variables, "lists": [l for l in fr["lists"] if l["related_list"]],
                "messages": fr["messages"], "journal": self.journal(form["table"], form["sys_id"])}

    def _edit(self, number: str, action: str, apply: Callable[[ActionResult], Dict[str, str]],
              verify: Optional[Callable[[ActionResult], None]] = None) -> ActionResult:
        """Open a ticket, apply changes, then save (or stop in a dry run) and verify."""
        def work(r: ActionResult) -> None:
            self.open_record(number)
            wanted = apply(r)
            if r.skipped or not r.ok:
                return
            if self.dry_run:
                r.dry_run, r.message = True, "Ingevuld, NIET opgeslagen (proefrun): " + _changes(r.before, wanted)
                return
            self.save()
            if verify:
                verify(r)
            r.message = r.message or ("Opgeslagen en gecontroleerd: " + _changes(r.before, r.after or wanted))
        return self._timed(action, number, work)

    def add_journal(self, number: str, text: str, kind: str = "work_notes") -> ActionResult:
        """Add a work note (kind="work_notes") or a customer-visible comment (kind="comments")."""
        label = "work note" if kind == "work_notes" else "opmerking (zichtbaar voor melder)"

        def apply(r):
            if not self.has_field(kind):
                raise AutomationError(f"Dit formulier heeft geen veld {kind}.")
            self.gform().evaluate("a => g_form.setValue(a[0], a[1])", [kind, text])
            r.before = {label: ""}
            return {label: _short(text)}

        def verify(r):
            entries = self.journal(self.gform().evaluate("() => g_form.getTableName()"),
                                   self.gform().evaluate("() => g_form.getUniqueValue()"))
            if entries and not any(_norm(text)[:60] in _norm(e["text"]) for e in entries[:5]):
                raise AutomationError(f"De {label} staat na opslaan niet in de activiteiten.")
            r.message = f"{label.capitalize()} toegevoegd: {_short(text)}"
        return self._edit(number, f"journal:{kind}", apply, verify)

    def assign_to_me(self, number: str) -> ActionResult:
        def apply(r):
            me = self.gform().evaluate("() => window.g_user ? [g_user.userID, g_user.getFullName()] : null")
            if not me:
                raise AutomationError("Kon de ingelogde gebruiker niet bepalen (g_user).")
            r.before = {"assigned_to": self.read("assigned_to")[1]}
            if self.read("assigned_to")[0] == me[0]:
                r.skipped, r.message = True, f"Staat al op jouw naam ({me[1]})."
                return {}
            self.gform().evaluate("a => g_form.setValue('assigned_to', a[0], a[1])", me)
            r._me = me
            return {"assigned_to": me[1]}

        def verify(r):
            v, d = self.read("assigned_to")
            r.after = {"assigned_to": d}
            if v != r._me[0]:
                raise AutomationError(f"Assigned to is na opslaan '{d}'.")
        return self._edit(number, "assign", apply, verify)

    def set_state(self, number: str, state: str, reason: Optional[str] = None) -> ActionResult:
        """Change State by its label (e.g. 'In Progress', 'On Hold' + reason 'Awaiting Caller')."""
        def apply(r):
            r.before = {"state": self.read("state")[1]}
            wanted = {"state": state}
            if r.before["state"].lower() == state.lower() and not reason:
                r.skipped, r.message = True, f"State is al {r.before['state']}."
                return {}
            self.set_choice("state", state)
            if reason:
                self.set_choice("hold_reason", reason)
                wanted["hold_reason"] = reason
            return wanted

        def verify(r):
            r.after = {"state": self.read("state")[1]}
            if r.after["state"].lower() != state.lower():
                raise AutomationError(f"State is na opslaan '{r.after['state']}' i.p.v. '{state}'.")
        return self._edit(number, "state", apply, verify)

    #: default resolution code for 'oplossen'
    RESOLVE_CODE = "Solved (Permanently)"

    def resolve(self, number: str, notes: str, code: Optional[str] = None) -> ActionResult:
        """Incident: State Resolved + resolution code + notes. Catalog task: Closed Complete + work note."""
        def apply(r):
            table = self.gform().evaluate("() => g_form.getTableName()")
            r.before = {"state": self.read("state")[1]}
            if table == "incident":
                self.set_choice("state", "Resolved")
                self.set_choice("close_code", code or self.RESOLVE_CODE)
                self.gform().evaluate("t => g_form.setValue('close_notes', t)", notes)
                r._target = "Resolved"
                return {"state": "Resolved", "close_code": code or self.RESOLVE_CODE, "close_notes": _short(notes)}
            self.set_choice("state", "Closed Complete")
            target = "close_notes" if self.has_field("close_notes") else "work_notes"
            self.gform().evaluate("a => g_form.setValue(a[0], a[1])", [target, notes])
            r._target = "Closed Complete"
            return {"state": "Closed Complete", target: _short(notes)}

        def verify(r):
            r.after = {"state": self.read("state")[1]}
            if r.after["state"].lower() != r._target.lower():
                raise AutomationError(f"State is na opslaan '{r.after['state']}' i.p.v. '{r._target}'.")
        return self._edit(number, "resolve", apply, verify)

    # ------------------------------------------------- CI installeren
    #: Field on the task that holds its cost center (Amsterdam UMC: the RITM's u_cost_center)
    TASK_COST_CENTER_FIELD = "request_item.u_cost_center"

    def install_ci(self, ci: str, task_number: Optional[str] = None, *, ci_type: Optional[str] = None,
                   status: str = "Installed", cost_center_field: Optional[str] = None,
                   cost_center: Optional[Tuple[str, str]] = None) -> ActionResult:
        """Set a CI to Installed, optionally Type (e.g. Monitor) and the cost center of a task.

        ci_type=None: a peripheral whose model name contains "monitor" automatically gets Type = Monitor.
        """
        cc_field = cost_center_field or self.TASK_COST_CENTER_FIELD

        def work(r: ActionResult) -> None:
            cc = cost_center  # (sys_id, display), when the caller already knows it
            if task_number and not cc:
                self.open_record(task_number)
                cc = self.task_cost_center(cc_field)
                if not cc[0]:
                    raise AutomationError(f"{task_number} heeft geen cost center (veld {cc_field}).")
            cls, sys_id, name = self.find_ci(ci)
            self.goto(f"{cls}.do?sys_id={sys_id}")
            fr = self.gform()
            if fr.evaluate("() => g_form.isNewRecord()"):
                raise AutomationError(f"CI {ci} kon niet worden geopend ({cls}).")
            want_type = ci_type
            if want_type is None and cls == "cmdb_ci_peripheral" and self.has_field("type"):
                if "monitor" in self.read("model_id")[1].lower():
                    want_type = "Monitor"

            todo: List[Tuple[str, str]] = [("install_status", status)]
            if want_type:
                if not self.has_field("type"):
                    raise AutomationError(f"{name} ({cls}) heeft geen veld Type.")
                todo.append(("type", want_type))
            for f, label in todo:
                r.before[f] = self.read(f)[1]
            if cc:
                r.before["cost_center"] = self.read("cost_center")[1]

            changed = []
            for f, label in todo:
                if r.before[f].strip().lower() != label.lower():
                    self.set_choice(f, label)
                    changed.append(f)
            if cc and self.read("cost_center")[0] != cc[0]:
                if not self.has_field("cost_center"):
                    raise AutomationError(f"{name} ({cls}) heeft geen veld Cost center op het formulier.")
                fr.evaluate("a => g_form.setValue(a[0], a[1], a[2])", ["cost_center", cc[0], cc[1]])
                changed.append("cost_center")
            wanted = {f: label for f, label in todo}
            if cc:
                wanted["cost_center"] = cc[1]
            r.record = f"{name}" + (f" ({task_number})" if task_number else "")
            if not changed:
                r.skipped, r.message = True, f"{name} staat al goed: " + _fmt(r.before)
                return
            if self.dry_run:
                r.dry_run = True
                r.message = f"{name}: ingevuld, NIET opgeslagen (proefrun): " + _changes(r.before, wanted)
                return
            self.save()
            for f, label in todo:
                r.after[f] = self.read(f)[1]
                if r.after[f].strip().lower() != label.lower():
                    raise AutomationError(f"{f} is na opslaan '{r.after[f]}' i.p.v. '{label}'.")
            if cc:
                v, d = self.read("cost_center")
                r.after["cost_center"] = d
                if v != cc[0]:
                    raise AutomationError(f"Cost center is na opslaan '{d}' i.p.v. '{cc[1]}'.")
            r.message = f"{name} opgeslagen en gecontroleerd: " + _changes(r.before, r.after)

        return self._timed("install_ci", ci, work)

    def task_info(self, number: str) -> dict:
        """Overview of a task: description, cost center and its Affected CIs (with CI status)."""
        fr = self.open_record(number)
        table, sys_id = fr.evaluate("() => [g_form.getTableName(), g_form.getUniqueValue()]")
        info = {"number": number.upper(), "table": table, "sys_id": sys_id,
                "short_description": self.read("short_description")[1] if self.has_field("short_description") else "",
                "cost_center": ("", "")}
        try:
            info["cost_center"] = self.task_cost_center()
        except (AutomationError, PlaywrightError):
            pass
        rows = self.api_get_all("task_ci", f"task={sys_id}",
                                "ci_item,ci_item.sys_class_name,ci_item.install_status,ci_item.cost_center")
        if rows is not None:
            d = lambda row, f: (row.get(f) or {}).get("display_value", "") if isinstance(row.get(f), dict) else str(row.get(f, ""))
            info["affected"] = [{"name": d(x, "ci_item"), "class": d(x, "ci_item.sys_class_name"),
                                 "status": d(x, "ci_item.install_status"), "cost_center": d(x, "ci_item.cost_center")}
                                for x in rows]
        else:
            info["affected"] = [{"name": n, "class": "", "status": "?", "cost_center": "?"}
                                for n in self.affected_cis(sys_id)]
        return info

    def task_cost_center(self, field_name: Optional[str] = None) -> Tuple[str, str]:
        """(sys_id, display) of the cost center on the open task form; Table API as fallback."""
        f = field_name or self.TASK_COST_CENTER_FIELD
        value, display = self.read(f)
        if value and display and display != value:
            return value, display
        fr = self.gform()
        table, sys_id = fr.evaluate("() => [g_form.getTableName(), g_form.getUniqueValue()]")
        rows = self.api_get_all(table, f"sys_id={sys_id}", f)
        if rows:
            cell = rows[0].get(f) or {}
            if isinstance(cell, dict) and cell.get("value"):
                return cell["value"], cell.get("display_value") or cell["value"]
        return value, display or value

    def api_get_all(self, table: str, query: str, fields: str) -> Optional[List[dict]]:
        """Table API GET with sysparm_display_value=all (value + display per field)."""
        try:
            return self._api_frame().evaluate(r"""async ([t, q, f]) => {
                const r = await fetch('/api/now/table/' + t + '?sysparm_display_value=all&sysparm_limit=50&sysparm_query=' +
                                      encodeURIComponent(q) + '&sysparm_fields=' + f,
                                      {headers: {'Accept': 'application/json', 'X-UserToken': window.g_ck || ''}});
                return r.ok ? (await r.json()).result : null;
            }""", [table, query, fields])
        except PlaywrightError:
            return None

    def find_ci(self, ci: str) -> Tuple[str, str, str]:
        """(class table, sys_id, name) of a CI by name, or by stickernumber/asset tag/serial number."""
        for field_name in ("name", "u_stickernumber", "asset_tag", "serial_number"):
            try:
                rows = self._api_frame().evaluate(r"""async ([f, v]) => {
                    const r = await fetch('/api/now/table/cmdb_ci?sysparm_limit=5&sysparm_fields=sys_id,name,sys_class_name,' + f +
                                          '&sysparm_query=' + encodeURIComponent(f + '=' + v),
                                          {headers: {'Accept': 'application/json', 'X-UserToken': window.g_ck || ''}});
                    return r.ok ? (await r.json()).result : {error: r.status};
                }""", [field_name, ci])
            except PlaywrightError:
                rows = {"error": "browser"}
            if isinstance(rows, dict):
                if rows.get("error") in (401, 403):
                    raise AutomationError("Geen rechten op de Table API om de CI op te zoeken.")
                continue  # e.g. 400: field does not exist on this instance
            # ServiceNow may ignore an unknown field in a query and return anything: double-check
            rows = [x for x in rows if str(x.get(field_name, "")).strip().lower() == ci.strip().lower()]
            if len(rows) == 1:
                return rows[0]["sys_class_name"] or "cmdb_ci", rows[0]["sys_id"], rows[0]["name"]
            if len(rows) > 1:
                raise AutomationError(f"'{ci}' is niet uniek in de CMDB ({len(rows)} CI's op {field_name}).")
        raise AutomationError(f"CI '{ci}' niet gevonden in de CMDB (gezocht op naam, stickernummer, asset tag, serienummer).")

    def set_choice(self, field_name: str, label: str) -> None:
        """Pick a choice by its label (case-insensitive, exact) in the UI; g_form when the field is on a hidden tab."""
        fr = self.gform()
        value = fr.evaluate(r"""([f, want]) => {
            const el = document.getElementById(g_form.getTableName() + '.' + f);
            if (!el || el.tagName !== 'SELECT') return null;
            const o = [...el.options].find(o => o.text.trim().toLowerCase() === want.toLowerCase())
                   || [...el.options].find(o => o.value.toLowerCase() === want.toLowerCase());
            return o ? o.value : null;
        }""", [field_name, label])
        if value is None:
            raise AutomationError(f"Keuze '{label}' bestaat niet in veld {field_name}.")
        table = fr.evaluate("() => g_form.getTableName()")
        try:
            self.sn.locate_field(field_name, table, timeout=1500).locator.select_option(value=value)
        except SmartLocatorError:
            fr.evaluate("a => g_form.setValue(a[0], a[1])", [field_name, value])

    def update_record(self, number: str, values: Dict[str, str]) -> ActionResult:
        """Set one or more fields (by label or name) and save. Reference fields are resolved and verified."""
        def work(r: ActionResult) -> None:
            self.open_record(number)
            for f, v in values.items():
                r.before[f] = self._read_any(f)
                m = self.sn.locate_field(f)
                el_id = m.locator.get_attribute("id") or ""
                if el_id.startswith("sys_display."):
                    self.set_reference(el_id.rsplit(".", 1)[-1], v)
                else:
                    self.sn.fill(f, v)
            if self.dry_run:
                r.dry_run, r.message = True, "Ingevuld, NIET opgeslagen (proefrun): " + _changes(r.before, values)
                return
            self.save()
            for f, v in values.items():
                r.after[f] = self._read_any(f)
                if r.after[f].strip().lower() != str(v).strip().lower() and r.after[f] != str(v):
                    raise AutomationError(f"{f} is na opslaan '{r.after[f]}' i.p.v. '{v}'.")
            r.message = "Opgeslagen en gecontroleerd: " + _changes(r.before, r.after)
        return self._timed("update", number, work)

    def _save_debug_scrape(self, screenshot: str) -> None:
        """On failure: keep a scrape of the page next to the screenshot, for diagnosis."""
        try:
            from .scraper import save, scrape
            save(scrape(self.page), Path(screenshot).with_suffix(".json"))
        except Exception:  # never let diagnostics hide the real error
            pass

    def _read_any(self, label_or_name: str) -> str:
        loc = self.sn.locate_field(label_or_name).locator
        return loc.evaluate("e => e.tagName === 'SELECT' ? (e.options[e.selectedIndex] || {text: ''}).text.trim()"
                            " : e.type === 'checkbox' ? String(e.checked) : e.value")

    def _timed(self, action: str, record: str, work: Callable[[ActionResult], None]) -> ActionResult:
        r = ActionResult(ok=True, action=action, record=record, message="")
        t0 = time.monotonic()
        try:
            work(r)
        except (AutomationError, SmartLocatorError) as e:
            r.ok, r.message = False, str(e).split("\nFrame tree:")[0]
        except PlaywrightError as e:
            r.ok, r.message = False, f"Browserfout: {str(e).splitlines()[0]}"
        if not r.screenshot:
            r.screenshot = self.screenshot(f"{record}_{r.status}")
        if not r.ok and r.screenshot:
            self._save_debug_scrape(r.screenshot)
        r.seconds = round(time.monotonic() - t0, 1)
        self.log(str(r))
        return r


_JOURNAL_HEAD = re.compile(r"^(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4} \d{1,2}:\d{2}(?::\d{2})?) - (.+?) \(([^)]+)\)\s*$", re.M)


def parse_journal(text: str) -> List[dict]:
    """Split ServiceNow's journal text ('<time> - <who> (Work notes)\n<text>') into entries."""
    heads = list(_JOURNAL_HEAD.finditer(text or ""))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append({"time": h.group(1), "who": h.group(2), "kind": h.group(3), "text": text[h.end():end].strip()})
    if not heads and (text or "").strip():
        out.append({"time": "", "who": "", "kind": "", "text": text.strip()})
    return out


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip().lower()


def _short(t: str, n: int = 60) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _changes(before: dict, after: dict) -> str:
    return ", ".join(f"{k}: '{before.get(k, '') or '-'}' -> '{v}'" for k, v in after.items())


# =========================================================================== #
# Plain-language commands
# =========================================================================== #
_NUM = r"(?P<num>[A-Za-z]{2,6}\d{4,})"
_ADD = re.compile(
    r"^\s*(?:voeg|koppel|add|link|zet)\s+(?:de\s+|het\s+)?(?:(?:pc|ci|computer|laptop|device|apparaat|asset)\s+)?"
    r"(?P<ci>[^\s,]+)\s+(?:toe\s+)?(?:aan|to|bij|met|in|op)\s+(?:de\s+|het\s+)?"
    r"(?:melding|incident|ticket|call|record|request|wijziging|change|ritm)?\s*" + _NUM + r"(?P<rest>.*)$", re.I)
_INSTALL = re.compile(
    r"^\s*(?:installeer|install|installeren)\s+(?:de\s+|het\s+)?(?:(?:pc|ci|monitor|beeldscherm|scherm|computer|laptop)\s+)?"
    r"(?P<ci>[^\s,]+)\s+(?:voor|van|bij|uit|met|for|from)\s+(?:de\s+)?(?:melding|taak|task|ticket|sctask)?\s*"
    + _NUM + r"(?P<rest>.*)$", re.I)
_SET = re.compile(
    r"^\s*(?:zet|set|wijzig|change)\s+(?:het\s+veld\s+|veld\s+|field\s+)?(?P<field>.+?)\s+(?:van|of|in|on|bij)\s+"
    + _NUM + r"\s+(?:op|to|naar|=)\s+(?P<value>.+?)\s*$", re.I)


@dataclass
class Command:
    action: str            # "add_ci" | "update" | "install"
    number: str
    ci: str = ""
    mode: str = "field"
    field: str = ""
    value: str = ""
    text: str = ""


def parse_command(text: str) -> Command:
    """Understands e.g.
        voeg PC-0042 toe aan INC0010001
        koppel laptop LT-7 aan melding INC0010002 als affected ci
        add PC-0042 to INC0010001 as affected CI
        zet state van INC0010001 op In Progress
        installeer bs-26-1244 voor SCTASK0194511          (Status Installed + cost center van de taak)
        installeer monitor bs-26-1244 voor SCTASK0194511  (idem + Type Monitor)
    """
    m = _SET.match(text)
    if m:
        return Command("update", m.group("num").upper(), field=m.group("field").strip(),
                       value=m.group("value").strip().strip("'\""), text=text)
    m = _INSTALL.match(text)
    if m:
        is_monitor = re.search(r"monitor|beeldscherm|scherm", text, re.I)
        return Command("install", m.group("num").upper(), ci=m.group("ci").strip("'\""),
                       value="Monitor" if is_monitor else "", text=text)
    m = _ADD.match(text)
    if m and m.group("ci").lower() not in ("toe", "to", "aan", "bij"):
        mode = "affected" if re.search(r"affected|getroffen|lijst|related", m.group("rest"), re.I) else "field"
        return Command("add_ci", m.group("num").upper(), ci=m.group("ci").strip("'\""), mode=mode, text=text)
    raise AutomationError(f"Opdracht niet begrepen: '{text}'. Voorbeeld: 'voeg PC-0042 toe aan INC0010001'.")


def run_commands(bot: ServiceNowBot, commands: Iterable, *, default_mode: Optional[str] = None,
                 overwrite: bool = False) -> List[ActionResult]:
    """Run many commands (strings or Command objects); one failure never stops the rest."""
    results = []
    for c in commands:
        try:
            cmd = c if isinstance(c, Command) else parse_command(str(c))
        except AutomationError as e:
            results.append(ActionResult(False, "parse", str(c), str(e)))
            bot.log(str(results[-1]))
            continue
        if cmd.action == "install":
            results.append(bot.install_ci(cmd.ci, cmd.number, ci_type=cmd.value or None))
        elif cmd.action == "add_ci":
            mode = default_mode if (default_mode and cmd.mode == "field") else cmd.mode
            results.append(bot.add_ci(cmd.number, cmd.ci, mode=mode, overwrite=overwrite))
        else:
            results.append(bot.update_record(cmd.number, {cmd.field: cmd.value}))
    return results


def commands_from_csv(path: Path) -> List:
    """CSV/Excel-export with columns melding;pc[;modus]  or a single column 'opdracht'."""
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    out: List = []
    for row in rows:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if row.get("opdracht") or row.get("command"):
            out.append(row.get("opdracht") or row.get("command"))
            continue
        number = row.get("melding") or row.get("incident") or row.get("nummer") or row.get("number")
        ci = row.get("pc") or row.get("ci") or row.get("computer") or row.get("configuration item")
        if not (number and ci):
            continue
        modus = row.get("modus", "") or row.get("mode", "")
        if re.search(r"install", modus, re.I):
            out.append(Command("install", number.upper(), ci=ci, value="Monitor" if re.search(r"monitor|scherm", modus, re.I) else "",
                               text=f"installeer {ci} voor {number}"))
            continue
        mode = "affected" if re.search(r"affected|getroffen|lijst", modus, re.I) else "field"
        out.append(Command("add_ci", number.upper(), ci=ci, mode=mode, text=f"{ci} -> {number}"))
    return out


def write_results(results: List[ActionResult], path: Path) -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["status", "record", "actie", "bericht", "voor", "na", "seconden", "screenshot"])
        for r in results:
            w.writerow([r.status, r.record, r.action, r.message, _fmt(r.before), _fmt(r.after), r.seconds, r.screenshot or ""])
    return path


def append_results(results: List[ActionResult], path: Path) -> Path:
    """Append to a (daily) log CSV, writing the header only once."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        if new:
            w.writerow(["tijd", "status", "record", "actie", "bericht", "voor", "na", "seconden", "screenshot"])
        for r in results:
            w.writerow([_dt.datetime.now().strftime("%H:%M:%S"), r.status, r.record, r.action, r.message,
                        _fmt(r.before), _fmt(r.after), r.seconds, r.screenshot or ""])
    return path


def _fmt(d: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in d.items())


# =========================================================================== #
# CLI
# =========================================================================== #
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import os
    import sys
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(
        prog="python -m servicenow_smart_locator.automation",
        description="ServiceNow-automation. Standaard een PROEFRUN: er wordt niets opgeslagen tenzij --uitvoeren.",
        epilog='Voorbeelden:\n  "voeg PC-0042 toe aan INC0010001"\n  "voeg PC-0042 toe aan INC0010001 als affected ci"\n'
               '  "zet state van INC0010001 op In Progress"\n  "installeer bs-26-1244 voor SCTASK0194511"\n'
               '  --csv koppelingen.csv --uitvoeren',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("opdracht", nargs="*", help="een of meer opdrachten tussen aanhalingstekens")
    ap.add_argument("--instance", default=os.environ.get("SN_INSTANCE"), help="https://<instance>.service-now.com (of SN_INSTANCE)")
    ap.add_argument("--csv", help="CSV met kolommen melding;pc[;modus] of een kolom opdracht")
    ap.add_argument("--uitvoeren", action="store_true", help="echt opslaan (zonder dit: proefrun)")
    ap.add_argument("--affected", action="store_true", help="PC's toevoegen aan Affected CIs i.p.v. het Configuration item-veld")
    ap.add_argument("--overschrijven", action="store_true", help="een al gevuld Configuration item vervangen")
    ap.add_argument("--polaris", action="store_true", help="via Next Experience openen i.p.v. klassieke UI")
    ap.add_argument("--browser", choices=["chromium", "msedge", "chrome"], default="chromium")
    ap.add_argument("--profile", default=str(Path.home() / ".snsl_inspector_profile"))
    ap.add_argument("--headless", action="store_true", help="zonder zichtbaar venster (alleen als je al ingelogd bent)")
    args = ap.parse_args(argv)

    if not args.instance:
        ap.error("geef --instance of zet SN_INSTANCE")
    commands: List = list(args.opdracht)
    if args.csv:
        commands += commands_from_csv(Path(args.csv))
    if not commands:
        ap.error("geen opdracht of --csv opgegeven")

    def say(msg: str) -> None:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)

    say(f"{'UITVOEREN' if args.uitvoeren else 'PROEFRUN (er wordt niets opgeslagen)'}: {len(commands)} opdracht(en)\n")
    with sync_playwright() as p:
        ctx, page = launch(p, args.browser, args.profile, args.headless)
        bot = ServiceNowBot(page, args.instance, dry_run=not args.uitvoeren, ui="polaris" if args.polaris else "classic",
                            user=os.environ.get("SN_USER"), password=os.environ.get("SN_PASSWORD"), log=say)
        results = run_commands(bot, commands, default_mode="affected" if args.affected else None,
                               overwrite=args.overschrijven)
        ctx.close()

    out = write_results(results, Path(f"resultaten_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv"))
    counts = {s: sum(r.status == s for r in results) for s in ("OK", "PROEFRUN", "OVERGESLAGEN", "FOUT")}
    say("\n" + "  ".join(f"{k}: {v}" for k, v in counts.items() if v) + f"\nResultaten: {out.resolve()}")
    return 1 if counts["FOUT"] else 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
