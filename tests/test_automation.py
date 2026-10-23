"""Scraper + automation against the mock ServiceNow server."""
import json

import pytest

from servicenow_smart_locator.automation import (
    AutomationError, Command, ServiceNowBot, commands_from_csv, parse_command, run_commands, write_results)
from servicenow_smart_locator.scraper import scrape, summarize
from mock_servicenow import MockServiceNow


@pytest.fixture
def mock():
    m = MockServiceNow().start()
    yield m
    m.stop()


def make_bot(page, mock, tmp_path, **kw):
    kw.setdefault("dry_run", False)
    return ServiceNowBot(page, mock.url, user="admin", password="admin", timeout=8000,
                         screenshot_dir=str(tmp_path / "shots"), log=lambda m: None, **kw)


# ----------------------------------------------------------------- scraper
@pytest.mark.parametrize("ui", ["classic", "polaris"])
def test_scrape_incident_form(page, mock, tmp_path, ui):
    bot = make_bot(page, mock, tmp_path, ui=ui)
    bot.open_record("INC0010002")
    data = scrape(page)
    fr = next(f for f in data["frames"] if f["form"])
    assert fr["frame_path"] == ([] if ui == "classic" else ['iframe[id="gsft_main"]'])
    form = fr["form"]
    assert form["table"] == "incident" and form["sys_id"] == "inc2" and form["is_new_record"] is False
    f = {x["name"]: x for x in form["fields"]}
    assert f["cmdb_ci"] == {"name": "cmdb_ci", "table": "incident", "label": "Configuration item", "type": "reference",
                            "value": "ci2", "display_value": "PC-0043", "mandatory": False, "readonly": False,
                            "visible": True, "reference": "cmdb_ci"}
    assert f["short_description"]["mandatory"] is True
    assert f["number"]["readonly"] is True and f["number"]["value"] == "INC0010002"
    assert f["state"]["display_value"] == "In Progress"
    assert {"value": "6", "label": "Resolved"} in f["state"]["choices"]
    assert form["buttons"] == [{"text": "Update", "action_name": "sysverb_update", "visible": True},
                               {"text": "Edit...", "action_name": "7ca0a8d60a0a0b340080d6f48c880640", "visible": True}]
    lst = fr["lists"][0]
    assert lst["title"] == "Affected CIs" and lst["rows"] == [{"Configuration Item": "PC-0043"}]
    assert "cmdb_ci" in summarize(data)
    json.dumps(data)  # serialisable


def test_scrape_workspace_generic(page, base_url):
    page.goto(f"{base_url}/workspace.html")
    data = scrape(page)
    fr = data["frames"][0]
    assert fr["form"] is None
    assert {"Short description", "Assignment group", "Work notes"} <= {f["label"] for f in fr["fields"]}
    assert {"Save", "More actions"} <= {b["text"] for b in fr["buttons"]}


# ------------------------------------------------------------ add CI: field
def test_login_and_add_ci_field(page, mock, tmp_path):
    bot = make_bot(page, mock, tmp_path)
    r = bot.add_ci("INC0010001", "PC-0042")
    assert r.ok and r.status == "OK", r.message
    assert mock.incident("INC0010001")["cmdb_ci"] == "ci1"
    assert r.before == {"cmdb_ci": ""} and r.after == {"cmdb_ci": "PC-0042"}
    assert r.screenshot and (tmp_path / "shots").exists()


def test_dry_run_saves_nothing(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path, dry_run=True).add_ci("INC0010001", "PC-0042")
    assert r.ok and r.status == "PROEFRUN"
    assert mock.incident("INC0010001")["cmdb_ci"] == ""


def test_add_ci_via_polaris(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path, ui="polaris").add_ci("INC0010001", "LAPTOP-7")
    assert r.ok, r.message
    assert mock.incident("INC0010001")["cmdb_ci"] == "ci3"


def test_existing_ci_not_overwritten_unless_asked(page, mock, tmp_path):
    bot = make_bot(page, mock, tmp_path)
    r = bot.add_ci("INC0010002", "PC-0042")
    assert not r.ok and "al gevuld met 'PC-0043'" in r.message
    assert mock.incident("INC0010002")["cmdb_ci"] == "ci2"
    r = bot.add_ci("INC0010002", "PC-0042", overwrite=True)
    assert r.ok and mock.incident("INC0010002")["cmdb_ci"] == "ci1"
    r = bot.add_ci("INC0010002", "pc-0042")
    assert r.ok and r.skipped


@pytest.mark.parametrize("ci, expected", [("PC-9999", "bestaat niet"), ("PC-DUP", "niet uniek")])
def test_bad_ci_reports_clearly(page, mock, tmp_path, ci, expected):
    r = make_bot(page, mock, tmp_path).add_ci("INC0010001", ci)
    assert not r.ok and expected in r.message
    assert mock.incident("INC0010001")["cmdb_ci"] == ""


def test_unknown_incident(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path).add_ci("INC0099999", "PC-0042")
    assert not r.ok and "INC0099999 niet gevonden" in r.message


def test_mandatory_field_blocks_save(page, mock, tmp_path):
    # INC0010003 has an empty mandatory Short description: ServiceNow refuses to save
    r = make_bot(page, mock, tmp_path).add_ci("INC0010003", "PC-0042")
    assert not r.ok and "mandatory" in r.message and "Short description" in r.message
    assert mock.incident("INC0010003")["cmdb_ci"] == ""


# --------------------------------------------------------- add CI: affected
@pytest.mark.parametrize("ui", ["classic", "polaris"])
def test_add_affected_ci(page, mock, tmp_path, ui):
    bot = make_bot(page, mock, tmp_path, ui=ui)
    r = bot.add_ci("INC0010002", "PC-0042", mode="affected")
    assert r.ok, r.message
    assert mock.affected("INC0010002") == ["PC-0043", "PC-0042"]
    r = bot.add_ci("INC0010002", "PC-0042", mode="affected")
    assert r.skipped and mock.affected("INC0010002") == ["PC-0043", "PC-0042"]


def test_affected_dry_run(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path, dry_run=True).add_ci("INC0010001", "PC-0042", mode="affected")
    assert r.status == "PROEFRUN" and mock.affected("INC0010001") == []


# -------------------------------------------------------------- update
def test_update_record_by_label(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path).update_record("INC0010001", {"State": "In Progress",
                                                                    "Short description": "Toner leeg"})
    assert r.ok, r.message
    inc = mock.incident("INC0010001")
    assert inc["state"] == "2" and inc["short_description"] == "Toner leeg"


# ------------------------------------------------------------- commands
@pytest.mark.parametrize("text, expected", [
    ("voeg PC-0042 toe aan INC0010001", ("add_ci", "INC0010001", "PC-0042", "field")),
    ("Voeg de pc PC-0042 toe aan melding inc0010001", ("add_ci", "INC0010001", "PC-0042", "field")),
    ("koppel laptop LAPTOP-7 aan INC0010002 als affected ci", ("add_ci", "INC0010002", "LAPTOP-7", "affected")),
    ("add PC-0042 to RITM0010001 as affected CI", ("add_ci", "RITM0010001", "PC-0042", "affected")),
])
def test_parse_add(text, expected):
    c = parse_command(text)
    assert (c.action, c.number, c.ci, c.mode) == expected


def test_parse_set_and_garbage():
    c = parse_command("zet State van INC0010001 op In Progress")
    assert (c.action, c.number, c.field, c.value) == ("update", "INC0010001", "State", "In Progress")
    with pytest.raises(AutomationError):
        parse_command("maak koffie")


def test_batch_from_csv_one_failure_does_not_stop(page, mock, tmp_path):
    csv_file = tmp_path / "koppelingen.csv"
    csv_file.write_text("melding;pc;modus\nINC0010001;PC-0042;\nINC0099999;PC-0042;\nINC0010002;LAPTOP-7;affected\n",
                        encoding="utf-8")
    cmds = commands_from_csv(csv_file) + ["zet state van INC0010001 op In Progress", "onzin",
                                          "zet state van INC0010002 op Resolved"]
    results = run_commands(make_bot(page, mock, tmp_path), cmds)
    assert [r.status for r in results] == ["OK", "FOUT", "OK", "OK", "FOUT", "FOUT"]
    assert mock.incident("INC0010001")["cmdb_ci"] == "ci1" and mock.incident("INC0010001")["state"] == "2"
    # Resolved without resolution code/notes is refused by ServiceNow (UI policy) and reported
    assert "mandatory" in results[-1].message and "Resolution code" in results[-1].message
    assert "LAPTOP-7" in mock.affected("INC0010002")
    out = write_results(results, tmp_path / "res.csv")
    assert out.read_text(encoding="utf-8-sig").count("\n") == 7


def test_works_without_rest_api_rights(page, mock, tmp_path):
    """Users without Table API access: the bot must rely on the UI only."""
    mock.api_enabled = False
    bot = make_bot(page, mock, tmp_path)
    assert bot.add_ci("INC0010001", "PC-0042").ok
    assert mock.incident("INC0010001")["cmdb_ci"] == "ci1"
    r = bot.add_ci("INC0010002", "LAPTOP-7", mode="affected")       # existence check via related list
    assert r.ok, r.message
    assert bot.add_ci("INC0010002", "LAPTOP-7", mode="affected").skipped
    r = bot.add_ci("INC0010001", "PC-9999", overwrite=True)
    assert not r.ok and "niet herkend" in r.message


def test_catalog_task_without_ci_field(page, mock, tmp_path):
    """Real-world case: an SCTASK form has no Configuration item field, only Affected CIs."""
    bot = make_bot(page, mock, tmp_path)
    r = bot.add_ci("SCTASK0010001", "PC-0042")
    assert not r.ok and "geen veld Configuration item" in r.message and r.seconds < 8
    r = bot.add_ci("SCTASK0010001", "PC-0042", mode="affected")
    assert r.ok, r.message
    assert mock.affected("SCTASK0010001") == ["PC-0042"]


# ------------------------------------------- Affected CIs via "Edit..." (slushbucket)
@pytest.mark.parametrize("search_mode", ["keyup", "enter"])
def test_edit_slushbucket_on_sctask(page, mock, tmp_path, search_mode):
    """Like by hand: Edit... -> search -> select -> arrow -> Save. Lower case input, exact match wins
    over BS-26-12410."""
    mock.search_mode = search_mode
    r = make_bot(page, mock, tmp_path, ui="polaris").add_ci("SCTASK0010001", "bs-26-1241", mode="affected")
    assert r.ok, r.message
    assert "via Edit..." in r.message
    assert mock.affected("SCTASK0010001") == ["BS-26-1241"]


def test_edit_slushbucket_dry_run_cancels(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path, dry_run=True).add_ci("SCTASK0010001", "BS-26-1241", mode="affected")
    assert r.status == "PROEFRUN" and "Cancel" in r.message
    assert mock.affected("SCTASK0010001") == []
    assert "SCTASK0010001" in page.title() or "sc_task" in page.url  # back on the task


@pytest.mark.parametrize("ci, expected", [("BS-99-0000", "niet gevonden in de linkerlijst"),
                                          ("PC-DUP", "2 keer in de lijst")])
def test_edit_slushbucket_problems(page, mock, tmp_path, ci, expected):
    r = make_bot(page, mock, tmp_path).add_ci("SCTASK0010001", ci, mode="affected")
    assert not r.ok and expected in r.message, r.message
    assert mock.affected("SCTASK0010001") == []
    assert r.screenshot and (tmp_path / "shots" / (r.screenshot.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:-4] + ".json")).exists()


def test_edit_slushbucket_keeps_existing(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path).add_ci("INC0010002", "BS-26-1241", mode="affected")
    assert r.ok, r.message
    assert sorted(mock.affected("INC0010002")) == ["BS-26-1241", "PC-0043"]


def test_without_edit_button_falls_back_to_task_ci_form(page, mock, tmp_path):
    mock.edit_button = False
    r = make_bot(page, mock, tmp_path).add_ci("SCTASK0010001", "BS-26-1241", mode="affected")
    assert r.ok and "via Edit" not in r.message
    assert mock.affected("SCTASK0010001") == ["BS-26-1241"]


def test_edit_found_when_related_list_loads_late(page, mock, tmp_path):
    """At Amsterdam UMC the Affected CIs list appears seconds after the form: wait for it."""
    mock.related_delay_ms = 2500
    mock.api_enabled = False   # also no API: the 'already there?' check must wait for the list too
    r = make_bot(page, mock, tmp_path, ui="polaris").add_ci("SCTASK0010001", "bs-26-1241", mode="affected")
    assert r.ok and "via Edit..." in r.message, r.message
    assert mock.affected("SCTASK0010001") == ["BS-26-1241"]


# ------------------------------------------------------- CI installeren
def test_install_monitor_from_task(page, mock, tmp_path):
    """Monitor: Status Installed, Type Monitor (auto: model is a monitor), cost center of the task."""
    r = make_bot(page, mock, tmp_path, ui="polaris").install_ci("bs-26-1244", "SCTASK0010001")
    assert r.ok and r.status == "OK", r.message
    ci = mock.db["cmdb_ci"]["ci8"]
    assert (ci["install_status"], ci["type"], ci["cost_center"]) == ("1", "monitor", "cc1004")
    assert r.before == {"install_status": "In Stock", "type": "-- None --", "cost_center": "1253"}
    assert r.after == {"install_status": "Installed", "type": "Monitor", "cost_center": "1004"}


def test_install_pc_by_command(page, mock, tmp_path):
    """PC: Installed (not 'Installed - Exception') + cost center; no Type field on a computer."""
    [r] = run_commands(make_bot(page, mock, tmp_path), ["installeer pc-26-4455 voor SCTASK0010001"])
    assert r.ok, r.message
    ci = mock.db["cmdb_ci"]["ci9"]
    assert (ci["install_status"], ci["cost_center"]) == ("1", "cc1004") and "type" not in ci
    # second time: nothing to do
    [r] = run_commands(make_bot(page, mock, tmp_path), ["installeer pc-26-4455 voor SCTASK0010001"])
    assert r.skipped, r.message


def test_install_find_by_stickernumber_and_dry_run(page, mock, tmp_path):
    r = make_bot(page, mock, tmp_path, dry_run=True).install_ci("PC-26-4455", "SCTASK0010001")
    assert r.status == "PROEFRUN" and "Installed" in r.message and "1004" in r.message, r.message
    assert mock.db["cmdb_ci"]["ci9"]["install_status"] == "6"  # nothing saved


@pytest.mark.parametrize("ci, task, expected", [
    ("XX-00-0000", "SCTASK0010001", "niet gevonden in de CMDB"),
    ("pc-26-4455", "SCTASK0010002", "heeft geen cost center"),
])
def test_install_problems(page, mock, tmp_path, ci, task, expected):
    r = make_bot(page, mock, tmp_path).install_ci(ci, task)
    assert not r.ok and expected in r.message, r.message
    assert mock.db["cmdb_ci"]["ci9"]["install_status"] == "6"


def test_csv_install_rows(tmp_path):
    f = tmp_path / "lijst.csv"
    f.write_text("melding;pc;modus\nSCTASK0194511;bs-26-1244;installeer monitor\nSCTASK0194511;pc-26-4455;installeer\n",
                 encoding="utf-8")
    cmds = commands_from_csv(f)
    assert [(c.action, c.ci, c.value) for c in cmds] == [("install", "bs-26-1244", "Monitor"), ("install", "pc-26-4455", "")]


@pytest.mark.parametrize("ui", ["classic", "polaris"])
def test_dry_run_batch_leaves_unsaved_forms(page, mock, tmp_path, ui):
    """A dry run leaves a filled-in, unsaved form; ServiceNow then asks 'leave page?' on the next command."""
    results = run_commands(make_bot(page, mock, tmp_path, dry_run=True, ui=ui),
                           ["installeer bs-26-1244 voor SCTASK0010001", "installeer pc-26-4455 voor SCTASK0010001",
                            "voeg PC-0042 toe aan INC0010001", "voeg PC-0043 toe aan INC0010003"])
    assert [r.status for r in results] == ["PROEFRUN"] * 4, [r.message for r in results]
    assert mock.db["cmdb_ci"]["ci8"]["install_status"] == "6"
