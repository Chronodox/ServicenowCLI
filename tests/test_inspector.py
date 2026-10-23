"""Inspector: real mouse hover/click on the mock ServiceNow pages."""
import pytest
from playwright.sync_api import expect

from servicenow_smart_locator.inspector import Inspector

GSFT = 'iframe[id="gsft_main"]'


@pytest.fixture
def insp(page):
    i = Inspector(page.context, echo=False).install()
    return i


def _pick(page, insp, locator):
    """Hover + click an element like a user would, then let the inspector work."""
    n = len(insp.results)
    box = locator.bounding_box()
    x, y = box["x"] + min(box["width"] / 2, 20), box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.click(x, y)
    for _ in range(50):
        insp.process_pending()
        if len(insp.results) > n:
            return insp.results[-1]
        page.wait_for_timeout(50)
    raise AssertionError("no pick result")


def _on(page, insp):
    page.wait_for_load_state("load")
    insp.set_on(True)


def test_classic_field_by_label(page, base_url, insp):
    page.goto(f"{base_url}/classic.html")
    _on(page, insp)
    r = _pick(page, insp, page.frame_locator("#gsft_main").locator('[id="incident.short_description"]'))
    assert r["kind"] == "field"
    assert r["frame_path"] == [GSFT]
    ok = [l["code"] for l in r["lines"] if l["ok"]]
    assert ok[0] == "sn.fill('Short description', '<waarde>')"
    assert "sn.fill('incident.short_description', '<waarde>')" in ok
    assert r["raw"].startswith("page.frame_locator('iframe[id=\"gsft_main\"]')")


def test_clicking_a_label_picks_its_field(page, base_url, insp):
    page.goto(f"{base_url}/classic.html")
    _on(page, insp)
    r = _pick(page, insp, page.frame_locator("#gsft_main").get_by_text("Caller", exact=True))
    assert r["kind"] == "field"
    assert r["lines"][0] == {"ok": True, "code": "sn.fill('Caller', '<waarde>')",
                             "note": "uniek · strategie: label[for] exact"}


def test_select_field_code(page, base_url, insp):
    page.goto(f"{base_url}/classic.html")
    _on(page, insp)
    r = _pick(page, insp, page.frame_locator("#gsft_main").locator('[id="incident.state"]'))
    assert r["lines"][0]["code"] == "sn.select('State', '<optie>')"


def test_button_pick_does_not_click(page, base_url, insp):
    page.goto(f"{base_url}/classic.html")
    _on(page, insp)
    gs = page.frame_locator("#gsft_main")
    r = _pick(page, insp, gs.locator("#sysverb_insert").first)
    assert r["kind"] == "button"
    assert [l["code"] for l in r["lines"] if l["ok"]] == ["sn.click('Submit')", "sn.click('sysverb_insert')"]
    expect(gs.locator("#result")).to_have_text("")       # click was swallowed
    insp.set_on(False)
    gs.locator("#sysverb_insert").first.click()          # inspector off: normal click works
    expect(gs.locator("#result")).to_have_text("submitted:")


def test_bottom_duplicate_button_is_flagged(page, base_url, insp):
    page.goto(f"{base_url}/classic.html")
    _on(page, insp)
    r = _pick(page, insp, page.frame_locator("#gsft_main").locator("#sysverb_insert").last)
    # sn.click('Submit') resolves to the TOP button, so for the bottom one it must say so
    assert not any(l["ok"] for l in r["lines"])
    assert "nth-of-type" in r["raw"] or ".nth(" in r["raw"]


def test_nested_iframe_pick(page, base_url, insp):
    page.goto(f"{base_url}/classic.html")
    _on(page, insp)
    r = _pick(page, insp, page.frame_locator("#gsft_main").frame_locator("#approval_frame")
              .get_by_role("button", name="Approve"))
    assert r["frame_path"] == [GSFT, 'iframe[id="approval_frame"]']
    assert r["lines"][0]["code"] == "sn.click('Approve')" and r["lines"][0]["ok"]


def test_polaris_shadow_iframe(page, base_url, insp):
    page.goto(f"{base_url}/polaris.html")
    _on(page, insp)
    r = _pick(page, insp, page.frame_locator("#gsft_main").locator('[id="sys_display.incident.caller_id"]'))
    assert r["frame_path"] == [GSFT]
    assert [l["code"] for l in r["lines"] if l["ok"]][:2] == ["sn.fill('Caller', '<waarde>')",
                                                              "sn.fill('caller_id', '<waarde>')"]


def test_workspace_shadow_components(page, base_url, insp):
    page.goto(f"{base_url}/workspace.html")
    _on(page, insp)
    r = _pick(page, insp, page.get_by_label("Work notes"))
    assert r["lines"][0] == {"ok": True, "code": "sn.fill('Work notes', '<waarde>')",
                             "note": "uniek · strategie: label[for] exact"}
    r = _pick(page, insp, page.get_by_role("button", name="More actions"))
    assert r["lines"][0]["code"] == "sn.click('More actions')" and r["lines"][0]["ok"]
    expect(page.locator("#out")).to_have_text("")


def test_f8_toggles(page, base_url, insp):
    page.goto(f"{base_url}/classic.html")
    page.wait_for_load_state("load")
    page.keyboard.press("F8")
    for _ in range(20):
        insp.process_pending()
        if insp.on:
            break
        page.wait_for_timeout(50)
    assert insp.on


def test_inspector_does_not_confuse_smart_locator(page, base_url, insp):
    """The overlay panel uses a closed shadow root, so it never shows up in lookups."""
    from servicenow_smart_locator import SmartLocator, SmartLocatorError
    page.goto(f"{base_url}/classic.html")
    _on(page, insp)
    with pytest.raises(SmartLocatorError):
        SmartLocator(page).locate_button("Kopieer", timeout=300)


def test_f9_scrapes_page(page, base_url, tmp_path):
    insp = Inspector(page.context, output_file=tmp_path / "picked.py", echo=False).install()
    page.goto(f"{base_url}/classic.html")
    page.wait_for_load_state("load")
    page.keyboard.press("F9")
    for _ in range(40):
        insp.process_pending()
        if insp.results:
            break
        page.wait_for_timeout(50)
    r = insp.results[-1]
    assert r["kind"] == "scrape" and "velden" in r["title"]
    files = list(tmp_path.glob("scrape_*.json"))
    assert len(files) == 1 and "short_description" in files[0].read_text(encoding="utf-8")
