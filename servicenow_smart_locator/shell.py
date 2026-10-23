"""
Interactieve opdrachtregel voor ServiceNow.

    python sn.py                     # start; browser blijft open en ingelogd
    python sn.py lever SCTASK0194511 bs-26-1244 pc-26-4455           # één opdracht (proefrun) en klaar
    python sn.py --echt lever SCTASK0194511 bs-26-1244 pc-26-4455    # één opdracht, echt opslaan

Instellingen (instance, Next Experience, browser) staan in instellingen.ini.
Alles wat de bot doet, komt in logboek/<datum>.csv.
"""
from __future__ import annotations

import cmd
import configparser
import datetime as _dt
import os
import re
import shlex
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from playwright.sync_api import Error as PlaywrightError

from .automation import (NUMBER_PREFIXES, ActionResult, AutomationError, ServiceNowBot, append_results,
                         commands_from_csv, launch, parse_command, run_commands)
from .smart_locator import SmartLocatorError

SETTINGS_FILE = "instellingen.ini"
DEFAULT_SETTINGS = """\
[servicenow]
# Adres van jullie ServiceNow
instance = {instance}
# polaris = Next Experience (zoals jullie het zien), classic = oude UI16
ui = polaris
# chromium, msedge (geïnstalleerde Edge) of chrome
browser = chromium
# ja = vraag bevestiging voordat er ECHT iets wordt opgeslagen
bevestigen = ja
# proef of echt: waarmee de opdrachtregel start
start_modus = proef
# sys_id van jullie toewijzingsgroep voor de lijsten (leeg = al jouw groepen)
groep =
# resolution code die 'oplossen' gebruikt
oplos_code = Solved (Permanently)
# maximaal aantal regels in een lijst
max_regels = 50
# wat 'i' en 'v' standaard tonen: alles (alle open tickets) of vrij (alleen niet toegewezen)
lijst_standaard = alles

[lijsten]
# Eigen lijsten toevoegen:  naam = tabel | encoded query   (kopieer de query uit de URL van een lijst)
# onsite = incident | active=true^assignment_group=...^ORDERBYDESCopened_at
"""

_COLORS = {"OK": "32", "PROEFRUN": "33", "OVERGESLAGEN": "90", "FOUT": "31", "ECHT": "1;31", "PROEF": "1;33",
           "kop": "1;36", "dim": "90", "titel": "1;97", "nummer": "1", "label": "90",
           "nieuw": "96", "bezig": "94", "wacht": "35", "klaar": "90",
           "p1": "1;31", "p2": "31", "p3": "33"}
_NUMBER = re.compile(r"^([A-Za-z]{2,6})(\d{5,})$")


def color(text: str, kind: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{_COLORS.get(kind, '0')}m{text}\033[0m"


def state_kind(state: str) -> str:
    """Colour for a ticket state: new, busy, waiting or done."""
    s = (state or "").lower()
    if s.startswith(("new", "open", "nieuw")):
        return "nieuw"
    if "progress" in s or s.startswith(("assigned", "active")):
        return "bezig"
    if "hold" in s or "pending" in s or "await" in s:
        return "wacht"
    if s.startswith(("resolved", "closed", "cancel", "complete")):
        return "klaar"
    return ""


def clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()


def is_number(token: str) -> bool:
    m = _NUMBER.match(token)
    return bool(m and m.group(1).upper() in NUMBER_PREFIXES)


def default_lists(groep: str = "", extra: Optional[dict] = None) -> dict:
    """name -> (table, encoded query, title). 'groep' = sys_id of the assignment group."""
    grp = f"assignment_group={groep}" if groep else "assignment_group=javascript:getMyGroups()"
    lists = {
        "incidenten": ("incident", f"active=true^state!=6^{grp}^ORDERBYDESCopened_at", "Open incidenten van de groep"),
        "verzoeken": ("sc_task", f"active=true^{grp}^ORDERBYDESCopened_at", "Open service requests (catalog tasks) van de groep"),
        "mijn": ("task", "active=true^assigned_to=javascript:gs.getUserID()^ORDERBYDESCsys_updated_on", "Mijn open tickets"),
        "vrij": ("task", f"active=true^{grp}^assigned_toISEMPTY^sys_class_name=incident^ORsys_class_name=sc_task"
                         "^ORDERBYDESCopened_at", "Niet toegewezen tickets van de groep"),
    }
    for name, spec in (extra or {}).items():
        if "|" in spec:
            table, query = (x.strip() for x in spec.split("|", 1))
            lists[name.strip().lower()] = (table, query, f"Lijst '{name}'")
    return lists


def load_settings(folder: Path = Path("."), ask: Callable[[str], str] = input) -> configparser.SectionProxy:
    path = folder / SETTINGS_FILE
    if not path.exists():
        url = ask("Adres van jullie ServiceNow (bijv. https://amsterdamumc.service-now.com): ").strip().rstrip("/")
        path.write_text(DEFAULT_SETTINGS.format(instance=url or "https://dev12345.service-now.com"), encoding="utf-8")
        print(f"Instellingen opgeslagen in {path.resolve()}")
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    cp.read(path, encoding="utf-8")
    return cp["servicenow"]


class SnShell(cmd.Cmd):
    """De opdrachtregel. Kan ook met een bestaande bot gebruikt worden (tests)."""

    intro_text = "Typ 'menu' voor het menu of 'help' voor alle opdrachten."

    def __init__(self, bot: ServiceNowBot, *, confirm: bool = True, log_dir: Path = Path("logboek"),
                 out: Callable[[str], None] = print, ask: Callable[[str], str] = input,
                 relaunch: Optional[Callable[[], ServiceNowBot]] = None, lists: Optional[dict] = None,
                 max_rows: int = 50, default_filter: str = "alles", clear: Callable[[], None] = clear_screen):
        super().__init__()
        self.clear = clear
        self.lists = lists or default_lists()
        self.max_rows = max_rows
        self.default_filter = default_filter  # what plain 'i' / 'v' show: "vrij" (unassigned) or "alles"
        self.listing: List[dict] = []
        self.listing_cmd: Optional[str] = None
        self.bot = bot
        self.confirm = confirm
        self.log_dir = Path(log_dir)
        self.out = out
        self.ask = ask
        self.relaunch = relaunch
        self.task: Optional[str] = None
        self.task_cc: Optional[Tuple[str, str]] = None
        self.last: List[ActionResult] = []
        self.bot.log = self._log
        self._update_prompt()

    # --------------------------------------------------------------- basics
    @property
    def live(self) -> bool:
        return not self.bot.dry_run

    def _update_prompt(self) -> None:
        mode = color("ECHT ", "ECHT") if self.live else color("PROEF", "PROEF")
        task = color(self.task, "kop") if self.task else color("-", "dim")
        self.prompt = f"\nsn {mode} {task}> "

    def _log(self, msg: str) -> None:
        m = re.match(r"^\[(OK|PROEFRUN|OVERGESLAGEN|FOUT)\]", msg)
        self.out(color(msg, m.group(1)) if m else msg)

    def emptyline(self) -> bool:
        return False

    def precmd(self, line: str) -> str:
        if self.relaunch and self.bot.page.is_closed():
            self.out("Browser was gesloten, ik start hem opnieuw...")
            self.bot = self.relaunch()
            self.bot.log = self._log
        return line

    def onecmd(self, line: str) -> bool:
        try:
            return super().onecmd(line)
        except KeyboardInterrupt:
            self.out(color("Afgebroken.", "FOUT"))
        except (AutomationError, SmartLocatorError) as e:
            self.out(color("FOUT: " + str(e).split("\nFrame tree:")[0], "FOUT"))
        except PlaywrightError as e:
            self.out(color("Browserfout: " + str(e).splitlines()[0], "FOUT"))
        return False

    def postcmd(self, stop: bool, line: str) -> bool:
        self._update_prompt()
        return stop

    def default(self, line: str) -> None:
        tokens = line.split()
        if len(tokens) == 1 and is_number(tokens[0]):
            return self.do_taak(tokens[0])
        if len(tokens) == 1 and tokens[0].isdigit():
            return self.do_toon(tokens[0])
        if tokens and tokens[0].lower() in self.lists:
            return self._show_list(tokens[0].lower(), " ".join(tokens[1:]))
        try:
            parse_command(line)
        except AutomationError:
            self.out(f"Onbekende opdracht: {line!r}. Typ 'help'.")
            return None
        if self._ok_to_save(f"opdracht: {line}"):
            self._record(run_commands(self.bot, [line]))

    # ------------------------------------------------------------- helpers
    def _split(self, arg: str) -> Tuple[Optional[str], List[str]]:
        """Pull an optional task number out of the arguments; the rest are CI names."""
        tokens = [t.strip(",;") for t in shlex.split(arg.replace(",", " "))]
        number = next((t.upper() for t in tokens if is_number(t)), None)
        cis = [t for t in tokens if t and not is_number(t)]
        if number and number != self.task:
            self.task, self.task_cc = number, None
        return self.task, cis

    def _need(self, task: Optional[str], cis: List[str], what: str) -> bool:
        if not task:
            self.out("Kies eerst een taak:  taak SCTASK0194511   (of zet het nummer in de opdracht)")
            return False
        if not cis:
            self.out(f"Welke CI('s)? Bijvoorbeeld:  {what} bs-26-1244 pc-26-4455")
            return False
        return True

    def _ok_to_save(self, summary: str) -> bool:
        if not self.live or not self.confirm:
            return True
        ans = self.ask(color(f"ECHT opslaan - {summary}. Doorgaan? [j/N] ", "ECHT")).strip().lower()
        if ans in ("j", "ja", "y", "yes"):
            return True
        self.out("Niet uitgevoerd.")
        return False

    def _cost_center(self) -> Optional[Tuple[str, str]]:
        if self.task_cc is None and self.task:
            self.bot.open_record(self.task)
            self.task_cc = self.bot.task_cost_center()
        return self.task_cc if self.task_cc and self.task_cc[0] else None

    def _record(self, results: List[ActionResult]) -> None:
        self.last = results
        if results:
            append_results(results, self.log_dir / f"{_dt.date.today():%Y-%m-%d}.csv")
            counts = {s: sum(r.status == s for r in results) for s in ("OK", "PROEFRUN", "OVERGESLAGEN", "FOUT")}
            self.out(color("  ".join(f"{k}: {v}" for k, v in counts.items() if v), "dim"))

    # ------------------------------------------------------------ commands
    def do_taak(self, arg: str) -> None:
        """taak SCTASK0194511 - kies de taak waar volgende opdrachten over gaan (en toon hem)."""
        number = arg.strip().upper()
        if not is_number(number):
            self.out("Gebruik:  taak SCTASK0194511")
            return
        self.task, self.task_cc = number, None
        self.do_info("")

    def do_info(self, arg: str) -> None:
        """info [nummer] - toon omschrijving, cost center en Affected CIs (met status) van de taak."""
        task, _ = self._split(arg)
        if not task:
            self.out("Kies eerst een taak:  taak SCTASK0194511")
            return
        info = self.bot.task_info(task)
        self.task_cc = info["cost_center"]
        self.out(color(f"{info['number']}  {info['short_description']}", "kop"))
        self.out(f"  Cost center: {info['cost_center'][1] or '-'}")
        if not info["affected"]:
            self.out("  Affected CIs: (geen)")
        else:
            self.out("  Affected CIs:")
            for a in info["affected"]:
                cls = {"cmdb_ci_peripheral": "Randapparaat", "Computer Peripheral": "Randapparaat",
                       "cmdb_ci_computer": "Computer"}.get(a["class"], a["class"] or "")
                cc_ok = "" if not self.task_cc[1] or a["cost_center"] in ("?", self.task_cc[1]) \
                    else color(f"  (cost center {a['cost_center'] or '-'})", "FOUT")
                st = a["status"] or "-"
                self.out(f"    - {a['name']:<16} {cls:<22} {color(st, 'OK') if st == 'Installed' else st}{cc_ok}")

    def do_koppel(self, arg: str) -> None:
        """koppel <ci> [<ci> ...] - zet CI's bij Affected CIs van de taak (via Edit...)."""
        task, cis = self._split(arg)
        if self._need(task, cis, "koppel") and self._ok_to_save(f"{len(cis)} CI('s) koppelen aan {task}: {', '.join(cis)}"):
            self._record([self.bot.add_ci(task, ci, mode="affected") for ci in cis])

    def do_installeer(self, arg: str) -> None:
        """installeer <ci> [...] - Status Installed (+ Type Monitor bij monitors) + cost center van de taak."""
        task, cis = self._split(arg)
        if not (self._need(task, cis, "installeer") and
                self._ok_to_save(f"{len(cis)} CI('s) installeren voor {task}: {', '.join(cis)}")):
            return
        cc = self._cost_center()
        if not cc:
            self.out(color(f"FOUT: {task} heeft geen cost center.", "FOUT"))
            return
        self._record([self.bot.install_ci(ci, task, cost_center=cc) for ci in cis])

    def do_lever(self, arg: str) -> None:
        """lever <ci> [...] - koppelen + installeren in één keer (het hele werk voor een levering)."""
        task, cis = self._split(arg)
        if not (self._need(task, cis, "lever") and
                self._ok_to_save(f"{len(cis)} CI('s) koppelen + installeren voor {task}: {', '.join(cis)}")):
            return
        cc = self._cost_center()
        if not cc:
            self.out(color(f"FOUT: {task} heeft geen cost center.", "FOUT"))
            return
        results = []
        for ci in cis:
            self.out(color(f"-- {ci}", "kop"))
            r = self.bot.add_ci(task, ci, mode="affected")
            results.append(r)
            if r.ok:  # also when it was already linked
                results.append(self.bot.install_ci(ci, task, cost_center=cc))
            else:
                self.out(color(f"   {ci} wordt niet geïnstalleerd omdat koppelen mislukte.", "dim"))
        self._record(results)

    def do_zet(self, arg: str) -> None:
        """zet <veld> op <waarde> - veld van het gekozen ticket wijzigen (of: zet <veld> van <nummer> op <waarde>)."""
        if " van " not in f" {arg} " and " op " in f" {arg} " and self.task:
            field, value = re.split(r"\s+op\s+", arg, maxsplit=1)
            arg = f"{field} van {self.task} op {value}"
        self.default("zet " + arg)

    # ------------------------------------------------------ tickets / menu
    def do_menu(self, arg: str) -> None:
        """menu - het menu."""
        std = "niet toegewezen" if self.default_filter == "vrij" else "alle open"
        rows = [("i", "incidenten", f"{std} incidenten van de groep   (i vrij · i mijn · i nieuw · i <tekst>)"),
                ("in", "incidenten vrij", "niet toegewezen incidenten van de groep"),
                ("v", "verzoeken", f"{std} service requests / catalog tasks   (v vrij · v mijn · ...)"),
                ("vn", "verzoeken vrij", "niet toegewezen service requests / catalog tasks"),
                ("m", "mijn", "mijn open tickets"),
                ("n", "vrij", "niet toegewezen tickets"),
                ("z", "zoek <tekst>", "zoeken in open tickets"),
                ("r", "ververs", "laatste lijst opnieuw ophalen"),
                ("c", "cls", "scherm leegmaken")]
        rows += [("", name, spec[2]) for name, spec in self.lists.items()
                 if name not in ("incidenten", "verzoeken", "mijn", "vrij")]
        self.out("")
        self._heading("Menu")
        for key, name, what in rows:
            self.out(f"  {color(f'{key:>2}', 'kop')}  {name:<16} {what}")
        self.out("  Daarna: typ het regelnummer (bijv. 3) of een nummer (INC.../SCTASK...) om het ticket te bekijken.")
        self.out("  Op het gekozen ticket: " + color("pak · notitie · opmerking · status · oplossen · zet · lever · open", "dim"))

    def _width(self) -> int:
        return max(80, min(shutil.get_terminal_size((120, 30)).columns, 200))

    def _show_list(self, name: str, arg: str = "") -> None:
        table, query, title = self.lists[name]
        extra = []
        words = arg.split()
        if not words and name in ("incidenten", "verzoeken") and self.default_filter == "vrij":
            words, arg = ["vrij"], "vrij"     # your queue: only what nobody has picked up yet
        if [w.lower() for w in words] == ["alles"]:
            words, arg = [], ""
        for w in list(words):
            lw = w.lower()
            if lw == "nieuw":
                extra.append("state=1"); words.remove(w)
            elif lw in ("vrij", "leeg"):
                extra.append("assigned_toISEMPTY"); words.remove(w)
            elif lw == "alles":
                words.remove(w)
            elif lw == "mijn":
                extra.append("assigned_to=javascript:gs.getUserID()"); words.remove(w)
        if words:
            t = " ".join(words)
            extra.append(f"short_descriptionLIKE{t}^ORnumberLIKE{t}^ORdescriptionLIKE{t}")
        if extra:
            head, _, order = query.partition("^ORDERBY")
            query = "^".join([head] + extra) + (f"^ORDERBY{order}" if order else "")
            title += " (" + arg.replace("vrij", "niet toegewezen") + ")"
        rows = self.bot.list_records(table, query, limit=self.max_rows)
        self.listing, self.listing_cmd = rows, f"{name} {arg}".strip()
        self.task, self.task_cc = None, None  # new list = let go of the chosen ticket
        self._print_rows(title, rows)

    def _heading(self, title: str) -> None:
        """Section heading:  ── Title ───────────"""
        rest = max(3, self._width() - len(title) - 8)
        self.out(color(f"  ── {title} ", "kop") + color("─" * rest, "dim"))

    def _print_rows(self, title: str, rows: List[dict]) -> None:
        w = self._width()
        self.out("")
        self.out(color(f"  {title}", "kop") + color(f"  ·  {len(rows)}" +
                                                    (" (max)" if len(rows) >= self.max_rows else ""), "dim"))
        if not rows:
            return
        desc_w = max(20, w - 79)
        self.out(color(f"  {'#':>3}  {'Nummer':<14} {'Geopend':<11}  {'Status':<16} {'P':<2} {'Toegewezen':<20} Omschrijving", "dim"))
        self.out(color("  " + "─" * (w - 4), "dim"))
        for i, r in enumerate(rows, 1):
            opened = re.sub(r"^(\d{2})[/-](\d{2})[/-]\d{4} (\d{2}:\d{2}).*$", r"\1/\2 \3", r.get("opened_at", ""))
            prio = (r.get("priority") or "")[:1]
            state = (r.get("state") or "")[:16]
            number = r.get("number", "")
            who = r.get("assigned_to") or ""
            who = f"{who[:20]:<20}" if who else color(f"{'— niemand':<20}", "PROEFRUN")
            line = (f"  {color(f'{i:>3}', 'dim')}  {color(f'{number:<14}', 'nummer')} "
                    f"{color(f'{opened:<11}', 'dim')}  {color(f'{state:<16}', state_kind(state))} "
                    f"{color(f'{prio:<2}', 'p' + prio if prio in ('1', '2', '3') else 'dim')} "
                    f"{who} {(r.get('short_description') or '')[:desc_w]}")
            self.out(line)

    def do_incidenten(self, arg: str) -> None:
        """incidenten [nieuw|vrij|mijn|<tekst>] - open incidenten van de groep (kort: i)."""
        self._show_list("incidenten", arg)

    def do_verzoeken(self, arg: str) -> None:
        """verzoeken [nieuw|vrij|mijn|<tekst>] - open service requests / catalog tasks van de groep (kort: v)."""
        self._show_list("verzoeken", arg)

    def do_mijn(self, arg: str) -> None:
        """mijn - mijn open tickets (kort: m)."""
        self._show_list("mijn", arg)

    def do_vrij(self, arg: str) -> None:
        """vrij - niet toegewezen tickets van de groep (kort: n)."""
        self._show_list("vrij", arg)

    def do_zoek(self, arg: str) -> None:
        """zoek <tekst> - zoek in open tickets (nummer, omschrijving). 'zoek -a <tekst>' zoekt ook in gesloten."""
        t = arg.strip()
        everything = t.startswith("-a ")
        t = t[3:].strip() if everything else t
        if not t:
            self.out("Gebruik:  zoek <tekst>")
            return
        query = ("" if everything else "active=true^") + \
            f"short_descriptionLIKE{t}^ORnumberLIKE{t}^ORdescriptionLIKE{t}^ORDERBYDESCsys_updated_on"
        rows = self.bot.list_records("task", query, limit=self.max_rows)
        self.listing, self.listing_cmd = rows, f"zoek {arg}"
        self.task, self.task_cc = None, None
        self._print_rows(f"Zoeken naar '{t}'", rows)

    do_i, do_v, do_m, do_n, do_z = do_incidenten, do_verzoeken, do_mijn, do_vrij, do_zoek

    def do_in(self, arg: str) -> None:
        """in [nieuw|mijn|<tekst>] - niet toegewezen incidenten van de groep."""
        self._show_list("incidenten", f"vrij {arg}".strip())

    def do_vn(self, arg: str) -> None:
        """vn [nieuw|<tekst>] - niet toegewezen service requests / catalog tasks van de groep."""
        self._show_list("verzoeken", f"vrij {arg}".strip())

    def do_ververs(self, arg: str) -> None:
        """ververs - laatste lijst opnieuw ophalen (kort: r)."""
        if self.listing_cmd:
            self.onecmd(self.listing_cmd)

    do_r = do_ververs

    def do_cls(self, arg: str) -> None:
        """cls - scherm leegmaken (kort: c)."""
        self.clear()

    do_c = do_cls

    def do_toon(self, arg: str) -> None:
        """toon [regelnummer|nummer] [alles] - alles van een ticket: velden, omschrijving, vragen, SLA's, CI's, activiteit."""
        parts = arg.split()
        show_all = "alles" in [p.lower() for p in parts]
        parts = [p for p in parts if p.lower() != "alles"]
        number = None
        if parts and parts[0].isdigit():
            i = int(parts[0])
            if not 1 <= i <= len(self.listing):
                self.out(f"Geen regel {i} in de laatste lijst.")
                return
            number = self.listing[i - 1]["number"]
        elif parts and is_number(parts[0]):
            number = parts[0].upper()
        number = number or self.task
        if not number:
            self.out("Welk ticket? Bijvoorbeeld:  toon 3   of   toon INC1058593")
            return
        self.task, self.task_cc = number, None
        d = self.bot.record_details(number)
        self._print_details(d, show_all)

    def _print_details(self, d: dict, show_all: bool) -> None:
        w = self._width() - 4
        by = {f["name"]: f for f in d["fields"]}
        val = lambda n: (by.get(n) or {}).get("value", "")
        self.out("")
        self.out(color(f"  {d['number']}  {val('short_description')}", "titel"))
        self.out(color("  " + "━" * (w - 2), "kop"))
        main = ["state", "hold_reason", "priority", "assignment_group", "assigned_to", "caller_id", "location",
                "u_alt_phone", "cmdb_ci", "contact_type", "category", "service_offering", "request_item",
                "request_item.cat_item", "request_item.request.requested_for", "request_item.u_cost_center",
                "opened_at", "request_item.opened_at"]
        skip = {"number", "short_description", "description"}
        shown = []
        for n in main + [f["name"] for f in d["fields"]]:
            f = by.get(n)
            if not f or n in skip or n in shown or not f["visible"]:
                continue
            v = (f["value"] or "").strip()
            if not v or v == "-- None --" or (f["type"] == "boolean" and v == "false"):
                continue
            shown.append(n)
        self._print_fields([(by[n]["label"], by[n]["value"].strip()) for n in shown], w)
        desc = val("description").strip()
        if desc:
            self.out("")
            self._heading("Omschrijving")
            for para in desc.splitlines():
                self.out(textwrap.fill(para, w - 2, initial_indent="    ", subsequent_indent="    ") or "")
        vars_ = [v for v in d["variables"] if (v["value"] or "").strip() not in ("", "-- None --") and v["visible"]]
        if vars_:
            self.out("")
            self._heading("Vragen uit de catalogus")
            for v in vars_:
                self.out(textwrap.fill(f"{v['label']}: {v['value']}", w - 2, initial_indent="    ", subsequent_indent="      "))
        for lst in d["lists"]:
            if not lst["row_count"]:
                continue
            self.out("")
            self._heading(f"{lst['title']} ({lst['row_count']})")
            for row in lst["rows"][: (50 if show_all else 5)]:
                cells = row.items() if isinstance(row, dict) else enumerate(row)
                vals = [str(v) for k, v in cells if v and not re.match(r"^(Select|Show|Hide) ", str(k))
                        and not str(v).startswith("Select record")]
                self.out("    • " + " │ ".join(vals)[:w - 6].replace(" │ ", color(" │ ", "dim")))
        if d["journal"]:
            n = len(d["journal"]) if show_all else 5
            self.out("")
            self._heading(f"Activiteit ({min(n, len(d['journal']))} van {len(d['journal'])}"
                          + ("" if show_all or len(d["journal"]) <= 5 else ", 'toon alles' voor meer") + ")")
            for j in d["journal"][:n]:
                t = re.sub(r"^(\d{2})[/-](\d{2})[/-]\d{4} (\d{2}:\d{2}).*$", r"\1/\2 \3", j["time"])
                kind = "opmerking" if "comment" in j["kind"].lower() else "work note"
                bar = color("│", "PROEFRUN" if kind == "opmerking" else "dim")
                self.out(f"    {color(t, 'dim')}  {color(j['who'], 'nummer')}  "
                         + (color("opmerking (melder ziet dit)", "PROEFRUN") if kind == "opmerking"
                            else color(kind, "dim")))
                for para in j["text"].splitlines():
                    for ln in (textwrap.wrap(para, w - 10) or [""]):
                        self.out(f"    {bar} {ln}")
        for m in d["messages"]:
            self.out(color(f"[{m['type']}] {m['text']}", "FOUT" if m["type"] == "error" else "dim"))

    def _print_fields(self, cells: List[Tuple[str, str]], w: int) -> None:
        """Label/value pairs in two aligned columns; long values get a whole line."""
        if not cells:
            return
        lw = min(22, max(len(label) for label, _ in cells))
        half = w // 2

        def cell(label: str, value: str) -> str:
            kind = state_kind(value) if label.lower() in ("state", "status") else \
                "p" + value[:1] if label.lower() == "priority" and value[:1] in "123" else ""
            return color(f"{label[:lw]:<{lw}}", "label") + "  " + (color(value, kind) if kind else value)

        pending = None
        for label, value in cells:
            if lw + 2 + len(value) <= half - 3:
                if pending is None:
                    pending = (label, value)
                    continue
                pad = " " * (half - lw - 2 - len(pending[1]))
                self.out("    " + cell(*pending) + pad + cell(label, value))
                pending = None
                continue
            if pending:
                self.out("    " + cell(*pending))
                pending = None
            lines = textwrap.wrap(value, max(20, w - lw - 4)) or [""]
            self.out("    " + cell(label, lines[0]))
            for ln in lines[1:]:
                self.out("    " + " " * (lw + 2) + ln)
        if pending:
            self.out("    " + cell(*pending))

    def _current(self, what: str) -> Optional[str]:
        if not self.task:
            self.out(f"Kies eerst een ticket (bijv. 'toon 3' of 'toon INC1058593'), daarna: {what}")
        return self.task

    def do_pak(self, arg: str) -> None:
        """pak - wijs het gekozen ticket aan jezelf toe."""
        t = self._current("pak")
        if t and self._ok_to_save(f"{t} aan jou toewijzen"):
            self._record([self.bot.assign_to_me(t)])

    def do_notitie(self, arg: str) -> None:
        """notitie <tekst> - interne work note toevoegen aan het gekozen ticket."""
        t = self._current("notitie <tekst>")
        if t and arg.strip() and self._ok_to_save(f"work note op {t}: {arg.strip()[:60]}"):
            self._record([self.bot.add_journal(t, arg.strip(), "work_notes")])
        elif t and not arg.strip():
            self.out("Gebruik:  notitie <tekst>")

    def do_opmerking(self, arg: str) -> None:
        """opmerking <tekst> - opmerking toevoegen die de MELDER ziet (Additional comments)."""
        t = self._current("opmerking <tekst>")
        if t and arg.strip() and self._ok_to_save(f"opmerking ZICHTBAAR VOOR MELDER op {t}: {arg.strip()[:60]}"):
            self._record([self.bot.add_journal(t, arg.strip(), "comments")])
        elif t and not arg.strip():
            self.out("Gebruik:  opmerking <tekst>")

    def do_status(self, arg: str) -> None:
        """status <status>[: <reden>] - bijv. 'status In Progress' of 'status On Hold: Awaiting Caller'."""
        t = self._current("status <status>")
        if not t:
            return
        state, _, reason = (x.strip() for x in arg.partition(":"))
        if not state:
            self.out("Gebruik:  status In Progress   of   status On Hold: Awaiting Caller")
            return
        if self._ok_to_save(f"{t} op status '{state}'" + (f" ({reason})" if reason else "")):
            self._record([self.bot.set_state(t, state, reason or None)])

    def do_oplossen(self, arg: str) -> None:
        """oplossen <oplossing> - incident op Resolved (met resolution code + notes) / catalog task op Closed Complete."""
        t = self._current("oplossen <oplossing>")
        if t and not arg.strip():
            self.out("Gebruik:  oplossen <wat je gedaan hebt>")
        elif t and self._ok_to_save(f"{t} oplossen/sluiten: {arg.strip()[:60]}"):
            self._record([self.bot.resolve(t, arg.strip())])

    def do_proef(self, arg: str) -> None:
        """proef - proefrun: alles wordt ingevuld en gecontroleerd, maar NIET opgeslagen (standaard)."""
        self.bot.dry_run = True
        self.out(color("Proefrun: er wordt niets opgeslagen.", "PROEF"))

    def do_echt(self, arg: str) -> None:
        """echt - vanaf nu wordt er ECHT opgeslagen in ServiceNow."""
        self.bot.dry_run = False
        self.out(color("Let op: vanaf nu wordt er ECHT opgeslagen. Typ 'proef' om terug te gaan.", "ECHT"))

    def do_csv(self, arg: str) -> None:
        """csv <bestand> - voer alle regels uit een CSV uit (kolommen melding;pc;modus)."""
        path = Path(arg.strip().strip('"'))
        if not path.exists():
            self.out(f"Bestand niet gevonden: {path}")
            return
        commands = commands_from_csv(path)
        if self._ok_to_save(f"{len(commands)} regels uit {path.name}"):
            self._record(run_commands(self.bot, commands))

    def do_scrape(self, arg: str) -> None:
        """scrape - sla de huidige pagina op als JSON (voor analyse)."""
        from .scraper import save, scrape
        path = save(scrape(self.bot.page), Path(f"scrape_{_dt.datetime.now():%Y%m%d_%H%M%S}.json"))
        self.out(f"Opgeslagen: {path.resolve()}")

    def do_open(self, arg: str) -> None:
        """open [nummer] - open de taak in het browservenster."""
        task, _ = self._split(arg)
        if task:
            self.bot.open_record(task)

    def do_stop(self, arg: str) -> bool:
        """stop - afsluiten (ook: exit, quit, Ctrl+Z/Enter)."""
        return True

    do_exit = do_quit = do_EOF = do_stop

    def do_help(self, arg: str) -> None:
        if arg:
            return super().do_help(arg)
        self.out(color("Opdrachten", "kop") + """
  menu                        het menu
  incidenten | i [filter]     alle open incidenten van de groep  (filter: vrij, nieuw, mijn of zoektekst)
  verzoeken | v [filter]      alle open service requests (catalog tasks) van de groep
  in,  vn                     niet toegewezen incidenten / service requests (= i vrij, v vrij)
  mijn | m,  vrij | n         mijn open tickets / niet toegewezen
  zoek | z <tekst>            zoeken in open tickets (zoek -a <tekst> ook gesloten)
  3   of   toon 3             ticket uit de lijst bekijken (of: toon INC1058593); 'toon alles' = alle activiteit
  ververs | r                 laatste lijst opnieuw ophalen
  cls | c                     scherm leegmaken

  Op het gekozen ticket:
  pak                         aan mij toewijzen
  notitie <tekst>             interne work note
  opmerking <tekst>           opmerking die de melder ziet
  status <status>[: reden]    bijv. status In Progress  /  status On Hold: Awaiting Caller
  oplossen <tekst>            incident Resolved / catalog task Closed Complete
  zet <veld> op <waarde>      willekeurig veld

  Leveren (catalog tasks):
  taak SCTASK0194511          kies de taak (of typ alleen het nummer) en toon hem
  info                        omschrijving, cost center, Affected CIs + status
  koppel <ci> [<ci> ...]      CI's bij Affected CIs zetten (via Edit...)
  installeer <ci> [...]       Status Installed (+ Type Monitor) + cost center van de taak
  lever <ci> [<ci> ...]       koppel + installeer in één keer
  proef / echt                niets opslaan (standaard) / echt opslaan
  csv <bestand>               alle regels uit een CSV uitvoeren
  scrape                      huidige pagina opslaan als JSON
  open                        taak in de browser openen
  stop                        afsluiten

  Taaknummer mag ook in de opdracht:  lever SCTASK0194511 bs-26-1244 pc-26-4455
  Hele zinnen werken ook:             voeg pc-26-4455 toe aan SCTASK0194511 als affected ci
  Logboek van alles:                  """ + str((self.log_dir / f"{_dt.date.today():%Y-%m-%d}.csv").resolve()))


def venv_warning(project: Path = Path(__file__).resolve().parents[1]) -> Optional[str]:
    """Warn when the project has a .venv but we are running another Python."""
    venv = project / ".venv"
    if not venv.exists() or Path(sys.prefix).resolve() == venv.resolve():
        return None
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    msg = (f"Let op: je draait met {sys.executable}, niet met de .venv van dit project.\n"
           f"  Activeer hem eerst (.venv\\Scripts\\Activate.ps1) of start met: {exe} sn.py")
    print(color(msg, "PROEFRUN"))
    return msg


# ------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    from playwright.sync_api import sync_playwright

    if os.name == "nt":
        os.system("")  # enable colours in the Windows console
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # box-drawing characters also when output is redirected (cp1252)
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python sn.py", description="ServiceNow-opdrachtregel")
    ap.add_argument("--echt", action="store_true", help="start in ECHT-modus (opslaan)")
    ap.add_argument("--headless", action="store_true", help="geen zichtbaar browservenster")
    ap.add_argument("--geen-bevestiging", action="store_true", help="niet vragen voor ECHT opslaan")
    ap.add_argument("opdracht", nargs=argparse.REMAINDER, help="één opdracht uitvoeren en stoppen")
    args = ap.parse_args(argv)

    cfg = load_settings()
    profile = str(Path.home() / ".snsl_inspector_profile")
    live = args.echt or cfg.get("start_modus", "proef").lower() == "echt"

    with sync_playwright() as p:
        state = {}

        def start() -> ServiceNowBot:
            ctx, page = launch(p, cfg.get("browser", "chromium"), profile, args.headless)
            state["ctx"] = ctx
            bot = ServiceNowBot(page, cfg["instance"], dry_run=not live, ui=cfg.get("ui", "polaris"),
                                user=os.environ.get("SN_USER"), password=os.environ.get("SN_PASSWORD"))
            page.goto(bot.instance + "/", wait_until="domcontentloaded")
            bot.ensure_logged_in("")
            return bot

        print(color("ServiceNow-opdrachtregel", "kop") + f"  ({cfg['instance']}, {cfg.get('ui', 'polaris')})")
        venv_warning()
        try:
            bot = start()
        except AutomationError as e:
            print(color(str(e), "FOUT"))
            return 1
        extra = dict(cfg.parser.items("lijsten")) if cfg.parser.has_section("lijsten") else {}
        ServiceNowBot.RESOLVE_CODE = cfg.get("oplos_code", ServiceNowBot.RESOLVE_CODE) or ServiceNowBot.RESOLVE_CODE
        shell = SnShell(bot, confirm=cfg.get("bevestigen", "ja").lower() in ("ja", "yes", "true", "1")
                        and not args.geen_bevestiging, relaunch=start,
                        lists=default_lists(cfg.get("groep", "").strip(), extra),
                        max_rows=int(cfg.get("max_regels", "50") or 50),
                        default_filter=(cfg.get("lijst_standaard", "alles") or "alles").strip().lower())
        if args.opdracht:
            shell.onecmd(" ".join(args.opdracht))
            code = 1 if any(not r.ok for r in shell.last) else 0
        else:
            shell.do_menu("")
            shell.out(shell.intro_text)
            if live:
                shell.out(color("Let op: ECHT-modus, er wordt opgeslagen.", "ECHT"))
            while True:
                try:
                    shell.cmdloop()
                    break
                except KeyboardInterrupt:
                    shell.out("\n(Ctrl+C) Typ 'stop' om af te sluiten.")
            code = 0
        try:
            state["ctx"].close()
        except Exception:
            pass
        return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
