"""The interactive command line (sn.py) against the mock ServiceNow."""
import re

import pytest

from servicenow_smart_locator.automation import ServiceNowBot
from servicenow_smart_locator.shell import SnShell, load_settings
from mock_servicenow import MockServiceNow


@pytest.fixture
def mock():
    m = MockServiceNow().start()
    yield m
    m.stop()


def make_shell(page, mock, tmp_path, answers=()):
    bot = ServiceNowBot(page, mock.url, user="admin", password="admin", timeout=8000, ui="polaris",
                        screenshot_dir=str(tmp_path / "shots"))
    out, ans = [], iter(answers)
    sh = SnShell(bot, log_dir=tmp_path / "log", out=out.append, ask=lambda q: next(ans))
    return sh, out


def text(out):
    return "\n".join(out)


def test_taak_shows_overview(page, mock, tmp_path):
    sh, out = make_shell(page, mock, tmp_path)
    sh.onecmd("taak SCTASK0010001")
    t = text(out)
    assert "SCTASK0010001  Standaard werkplek leveren" in t and "Cost center: 1004" in t and "(geen)" in t
    sh.postcmd(False, "")
    assert "SCTASK0010001" in sh.prompt and "PROEF" in sh.prompt


def test_lever_proef_changes_nothing(page, mock, tmp_path):
    sh, out = make_shell(page, mock, tmp_path)
    sh.onecmd("lever SCTASK0010001 bs-26-1244 pc-26-4455")
    assert [r.status for r in sh.last] == ["PROEFRUN"] * 4, text(out)
    assert mock.affected("SCTASK0010001") == [] and mock.db["cmdb_ci"]["ci8"]["install_status"] == "6"
    assert (tmp_path / "log").glob("*.csv")


def test_lever_echt_does_everything(page, mock, tmp_path):
    sh, out = make_shell(page, mock, tmp_path, answers=["j"])
    sh.onecmd("SCTASK0010001")              # bare number = taak
    sh.onecmd("echt")
    sh.onecmd("lever bs-26-1244, pc-26-4455")
    assert [r.status for r in sh.last] == ["OK"] * 4, text(out)
    assert sorted(mock.affected("SCTASK0010001")) == ["BS-26-1244", "pc-26-4455"]
    m, p = mock.db["cmdb_ci"]["ci8"], mock.db["cmdb_ci"]["ci9"]
    assert (m["install_status"], m["type"], m["cost_center"]) == ("1", "monitor", "cc1004")
    assert (p["install_status"], p["cost_center"]) == ("1", "cc1004")
    out.clear()
    sh.onecmd("info")
    t = text(out)
    assert "BS-26-1244" in t and "pc-26-4455" in t and t.count("Installed") == 2 and "cost center 1253" not in t
    log = next((tmp_path / "log").glob("*.csv")).read_text(encoding="utf-8-sig")
    assert log.count(";OK;") == 4


def test_echt_asks_and_no_means_no(page, mock, tmp_path):
    sh, out = make_shell(page, mock, tmp_path, answers=["n"])
    sh.onecmd("echt")
    sh.onecmd("koppel SCTASK0010001 bs-26-1244")
    assert "Niet uitgevoerd." in text(out) and mock.affected("SCTASK0010001") == []


def test_lever_skips_install_when_link_fails(page, mock, tmp_path):
    sh, out = make_shell(page, mock, tmp_path, answers=["j"])
    sh.onecmd("echt")
    sh.onecmd("lever SCTASK0010001 XX-00-0000 pc-26-4455")
    assert [r.status for r in sh.last] == ["FOUT", "OK", "OK"], text(out)
    assert "XX-00-0000 wordt niet geïnstalleerd" in text(out)


def test_full_sentence_and_help(page, mock, tmp_path):
    sh, out = make_shell(page, mock, tmp_path)
    sh.onecmd("voeg PC-0042 toe aan INC0010001")
    assert sh.last[0].status == "PROEFRUN"
    sh.onecmd("help")
    sh.onecmd("koffie zetten")
    t = text(out)
    assert "lever <ci>" in t and "Onbekende opdracht" in t
    sh.onecmd("lever bs-26-1244")
    assert "Kies eerst een taak" in text(out)


def test_settings_created_on_first_run(tmp_path):
    cfg = load_settings(tmp_path, ask=lambda q: "https://amsterdamumc.service-now.com/")
    assert cfg["instance"] == "https://amsterdamumc.service-now.com" and cfg["ui"] == "polaris"
    assert cfg["bevestigen"] == "ja"
    assert load_settings(tmp_path, ask=lambda q: 1 / 0)["instance"].endswith("service-now.com")  # no 2nd question


# ------------------------------------------------------------ ticket menu
from servicenow_smart_locator.shell import default_lists


def make_menu_shell(page, mock, tmp_path, answers=()):
    sh, out = make_shell(page, mock, tmp_path, answers)
    sh.lists = default_lists("grp1", {"opgelost": "incident | state=6^ORDERBYDESCopened_at"})
    return sh, out


def test_lists_and_filters(page, mock, tmp_path):
    sh, out = make_menu_shell(page, mock, tmp_path)
    sh.onecmd("i")                              # default: all open incidents of the group
    assert [r["number"] for r in sh.listing] == ["INC0010002", "INC0010001", "INC0010003"]  # not other group/resolved
    sh.onecmd("i vrij")
    assert [r["number"] for r in sh.listing] == ["INC0010001"]
    assert "niet toegewezen" in text(out)
    sh.onecmd("incidenten nieuw")
    assert [r["number"] for r in sh.listing] == ["INC0010001", "INC0010003"]
    sh.onecmd("v")
    assert [r["number"] for r in sh.listing] == ["SCTASK0010001", "SCTASK0010002"]
    sh.onecmd("v mijn")
    assert [r["number"] for r in sh.listing] == ["SCTASK0010002"]
    sh.onecmd("in")                             # shortcut for 'i vrij'
    assert [r["number"] for r in sh.listing] == ["INC0010001"]
    sh.onecmd("in printer")
    assert [r["number"] for r in sh.listing] == ["INC0010001"]
    sh.onecmd("vn")                             # shortcut for 'v vrij'
    assert [r["number"] for r in sh.listing] == ["SCTASK0010001"]
    sh.default_filter = "vrij"                  # lijst_standaard = vrij in instellingen.ini
    sh.onecmd("v")
    assert [r["number"] for r in sh.listing] == ["SCTASK0010001"]
    sh.default_filter = "alles"
    sh.onecmd("m")
    assert {r["number"] for r in sh.listing} == {"SCTASK0010002", "INC0010003"}
    sh.onecmd("n")
    assert {r["number"] for r in sh.listing} == {"SCTASK0010001", "INC0010001"}
    sh.onecmd("zoek printer")
    assert [r["number"] for r in sh.listing] == ["INC0010001"]
    sh.onecmd("opgelost")                       # own list from instellingen.ini [lijsten]
    assert [r["number"] for r in sh.listing] == ["INC0010005"]
    assert "Printer on fire" in text(out) and "Kim de Vries" in text(out)


def test_toon_by_row_number(page, mock, tmp_path):
    sh, out = make_menu_shell(page, mock, tmp_path)
    sh.onecmd("i")
    out.clear()
    sh.onecmd("2")
    t = text(out)
    assert sh.task == "INC0010001"
    sh.onecmd("vn")                                 # a new list lets go of the chosen ticket
    sh.postcmd(False, "")
    assert sh.task is None and "INC0010001" not in sh.prompt
    assert "INC0010001  Printer on fire" in t and re.search(r"Caller +Sam Jansen", t) and "staat in brand" in t
    assert "Hij rookt nog steeds!" in t and "opmerking" in t


def test_cls_clears_screen(page, mock, tmp_path):
    sh, out = make_shell(page, mock, tmp_path)
    calls = []
    sh.clear = lambda: calls.append(1)
    sh.onecmd("cls")
    sh.onecmd("c")
    assert calls == [1, 1]


def test_ticket_actions_echt(page, mock, tmp_path):
    sh, out = make_menu_shell(page, mock, tmp_path, answers=["j"] * 6)
    sh.onecmd("toon INC0010001")
    sh.onecmd("echt")
    for c in ["pak", "notitie Ben onderweg", "opmerking We komen om 14:00", "status On Hold: Awaiting Caller"]:
        sh.onecmd(c)
        assert sh.last[0].status == "OK", (c, text(out))
    inc = mock.incident("INC0010001")
    assert (inc["assigned_to"], inc["state"], inc["hold_reason"]) == ("u_admin", "3", "1")
    notes = [(j["element"], j["value"]) for j in mock.db["journal"] if j["element_id"] == "inc1"]
    assert ("work_notes", "Ben onderweg") in notes and ("comments", "We komen om 14:00") in notes
    sh.onecmd("oplossen Printer vervangen")
    inc = mock.incident("INC0010001")
    assert sh.last[0].status == "OK" and inc["state"] == "6" and inc["close_code"] == "Solved (Permanently)"
    assert inc["close_notes"] == "Printer vervangen"
    sh.onecmd("zet priority op 2 - High")
    assert mock.incident("INC0010001")["priority"] == "2"


def test_ticket_actions_proef_and_guards(page, mock, tmp_path):
    sh, out = make_menu_shell(page, mock, tmp_path)
    sh.onecmd("pak")
    assert "Kies eerst een ticket" in text(out)
    sh.onecmd("toon SCTASK0010001")
    sh.onecmd("status Work in Progress")
    sh.onecmd("oplossen Geleverd")
    assert [r.status for r in sh.last] == ["PROEFRUN"]
    assert mock.record("SCTASK0010001")["state"] == "1"
    sh.onecmd("echt")
    sh.ask = lambda q: "n"
    sh.onecmd("status On Hold")
    assert "Niet uitgevoerd." in text(out)


# ------------------------------------------------------------ browser start problems
from playwright.sync_api import Error as PlaywrightError
from servicenow_smart_locator.automation import AutomationError, launch
from servicenow_smart_locator.shell import venv_warning


class _FakeChromium:
    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def launch_persistent_context(self, profile, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise PlaywrightError("BrowserType.launch_persistent_context: Target page, context or browser has been closed")
        return type("Ctx", (), {"pages": ["page"]})()


def test_launch_removes_stale_lock_and_retries(tmp_path):
    (tmp_path / "lockfile").write_text("")
    p = type("P", (), {"chromium": _FakeChromium(fail_times=1)})()
    ctx, page = launch(p, profile=str(tmp_path), headless=True)
    assert page == "page" and p.chromium.calls == 2 and not (tmp_path / "lockfile").exists()


def test_launch_gives_dutch_help(tmp_path):
    p = type("P", (), {"chromium": _FakeChromium(fail_times=9)})()
    with pytest.raises(AutomationError) as e:
        launch(p, profile=str(tmp_path), headless=True)
    msg = str(e.value)
    assert "De browser start niet" in msg and ".venv" in msg and "Rename-Item" in msg and "Stop-Process" in msg


def test_venv_warning(tmp_path):
    assert venv_warning(tmp_path) is None              # no .venv in project: nothing to say
    (tmp_path / ".venv").mkdir()
    assert "niet met de .venv" in venv_warning(tmp_path)
