"""
Demo against a real ServiceNow instance (e.g. a free Personal Developer Instance).

    set SN_INSTANCE=https://dev12345.service-now.com
    set SN_USER=admin
    set SN_PASSWORD=********
    python examples/demo_incident.py

The record is only saved when SN_SUBMIT=1 is set, so by default this is a dry run
that fills the form and prints the locators it found.
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from servicenow_smart_locator import SmartLocator  # noqa: E402

INSTANCE = os.environ["SN_INSTANCE"].rstrip("/")
USER = os.environ["SN_USER"]
PASSWORD = os.environ["SN_PASSWORD"]
SUBMIT = os.environ.get("SN_SUBMIT") == "1"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=150)
        page = browser.new_page()
        sn = SmartLocator(page, timeout=30_000)

        # Login page (no iframe) - same API works there too
        page.goto(f"{INSTANCE}/login.do")
        sn.fill("user_name", USER)
        sn.fill("user_password", PASSWORD)
        sn.click("sysverb_login")
        page.wait_for_load_state("networkidle")

        # New incident. Next Experience wraps it in a shadow-DOM iframe,
        # UI16 in a plain iframe - the locator doesn't care which.
        page.goto(f"{INSTANCE}/now/nav/ui/classic/params/target/incident.do%3Fsys_id%3D-1")
        sn.wait_until_ready()

        for query, value in [("Caller", "Abel Tuter"),
                             ("Short description", "Created by ServiceNow Smart Locator"),
                             ("Description", "Playwright found every field across iframes + shadow DOM."),
                             ("Urgency", "2 - Medium")]:
            m = sn.fill(query, value)
            print(f"{query:<18} -> {m}")

        print("caller_id sys_id via g_form:", sn.get_value("caller_id"))
        print("Submit button          ->", sn.locate_button("Submit"))

        if SUBMIT:
            sn.click("Submit")
            page.wait_for_timeout(3000)
        else:
            print("\nDry run: set SN_SUBMIT=1 to actually save the incident.")
            page.wait_for_timeout(5000)
        browser.close()


if __name__ == "__main__":
    main()
