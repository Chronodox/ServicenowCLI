"""Tests against mock pages that copy ServiceNow's DOM structure."""
import time

import pytest
from playwright.sync_api import expect

from servicenow_smart_locator import SmartLocator, SmartLocatorError

GSFT = 'iframe[id="gsft_main"]'


@pytest.fixture
def classic(page, base_url):
    page.goto(f"{base_url}/classic.html")
    return SmartLocator(page, timeout=5000)


@pytest.fixture
def polaris(page, base_url):
    page.goto(f"{base_url}/polaris.html")
    return SmartLocator(page, timeout=5000)


# --------------------------------------------------------------- classic UI16
def test_field_by_label_lands_in_gsft_main(classic):
    m = classic.locate_field("Short description")
    assert m.frame_path == [GSFT]
    assert m.strategy == "label[for] exact"
    m.locator.fill("Printer on fire")
    assert classic.get_value("short_description") == "Printer on fire"


@pytest.mark.parametrize("query", ["short_description", "incident.short_description"])
def test_field_by_name(classic, query):
    m = classic.locate_field(query)
    assert m.strategy == "servicenow element id"
    assert m.locator.get_attribute("id") == "incident.short_description"


def test_reference_field_targets_display_input(classic):
    for q in ("Caller", "caller_id"):
        assert classic.field(q).get_attribute("id") == "sys_display.incident.caller_id"
    classic.fill("Caller", "Abel Tuter")
    assert classic.get_value("caller_id") == "6816f79cc0a8016401c5a33be04be441"


def test_select_checkbox_textarea_readonly(classic):
    classic.fill("State", "In Progress")
    assert classic.get_value("state") == "2"
    classic.fill("active", False)
    expect(classic.field("Active")).not_to_be_checked()
    classic.fill("Description", "multi\nline")
    assert classic.get_value("description") == "multi\nline"
    expect(classic.field("Number")).to_have_value("INC0010001")


def test_custom_field_u_prefix(classic):
    assert classic.field("u_cost_center").get_attribute("id") == "incident.u_cost_center"
    assert classic.field("Cost center").get_attribute("id") == "incident.u_cost_center"


def test_submit_uses_action_name_ignores_hidden_frame_and_duplicate(classic):
    classic.fill("Short description", "via smart locator")
    m = classic.click("Submit")
    assert m.strategy == "ui action name"
    assert m.frame_path == [GSFT]          # not the display:none helper frame
    assert m.match_count == 2 and m.code.endswith(".first")
    expect(classic.css("#result")).to_have_text("submitted:via smart locator")


def test_invisible_frame_is_skipped_unless_asked(classic):
    with pytest.raises(SmartLocatorError):
        classic.locate_button("Submit", in_frame="hidden_frame", timeout=500)
    m = classic.locate_button("Submit", in_frame="hidden_frame", visible=False)
    assert m.frame_path == ['iframe[name="hidden_frame"]']


def test_custom_ui_action_by_text(classic):
    m = classic.click("Resolve")
    expect(classic.css("#result")).to_have_text("resolved")
    assert m.frame_path == [GSFT]


def test_top_page_elements_still_found(classic):
    m = classic.locate_field("Search")
    assert m.frame_path == []


# -------------------------------------------------------------- nested iframe
def test_nested_iframe(classic):
    m = classic.locate_button("Approve")
    assert m.frame_path == [GSFT, 'iframe[id="approval_frame"]']
    classic.fill("Approval comments", "LGTM")
    m.locator.click()
    expect(classic.css("#out")).to_have_text("approved")


def test_in_frame_restricts_search(classic):
    with pytest.raises(SmartLocatorError):
        classic.locate_field("Short description", in_frame="approval_frame", timeout=500)
    assert classic.locate_field("Approval comments", in_frame="approval.html").frame_path[-1] \
        == 'iframe[id="approval_frame"]'


# ---------------------------------------------------- Next Experience (shadow)
def test_polaris_iframe_inside_shadow_dom(polaris):
    m = polaris.locate_field("Short description")
    assert m.frame_path == [GSFT]
    polaris.fill("Short description", "from polaris")
    polaris.click("Submit")
    expect(polaris.css("#result")).to_have_text("submitted:from polaris")


def test_polaris_frame_helper(polaris):
    content = polaris.frame()                       # defaults to gsft_main
    expect(content.locator('[id="incident.number"]')).to_have_value("INC0010001")
    by_child = polaris.frame(containing="#approval_comments")
    expect(by_child.get_by_role("button", name="Approve")).to_be_visible()


# ----------------------------------------------------------------- Workspace
def test_workspace_shadow_components(page, base_url):
    page.goto(f"{base_url}/workspace.html")
    sn = SmartLocator(page, timeout=5000)
    sn.fill("Short description", "workspace!")
    sn.fill("Work notes", "note")
    assert sn.locate_field("Assignment group").frame_path == []
    m = sn.click("Save")
    assert m.strategy == "role=button exact"
    expect(page.locator("#out")).to_have_text("saved:workspace!")
    expect(sn.button("More actions")).to_be_visible()


# ------------------------------------------------------------- timing & errors
def test_waits_for_late_iframe(page, base_url):
    page.goto(f"{base_url}/late.html")
    sn = SmartLocator(page, timeout=6000)
    t0 = time.monotonic()
    sn.fill("Short description", "late")
    assert time.monotonic() - t0 >= 1.0


def test_survives_iframe_reload(classic, page):
    loc = classic.field("Short description")
    page.frame(name="gsft_main").evaluate("location.reload()")
    page.wait_for_timeout(300)
    loc.fill("after reload")                        # frame_locator chain re-resolves
    assert classic.get_value("short_description") == "after reload"


def test_not_found_suggests(classic):
    with pytest.raises(SmartLocatorError) as e:
        classic.locate_field("Short descriptoin", timeout=500)
    msg = str(e.value)
    assert "Did you mean" in msg and "'Short description'" in msg
    assert "gsft_main" in msg  # frame tree included


def test_generated_code_is_runnable(classic, page):
    m = classic.locate_field("Caller")
    loc = eval(m.code, {"page": page})
    assert loc.get_attribute("id") == "sys_display.incident.caller_id"
