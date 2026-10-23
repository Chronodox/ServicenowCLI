# ServiceNow Smart Locator for Playwright

Tell it what you want ("Short description", `caller_id`, "Submit") and it finds
the right element, however ServiceNow wraps it. That covers:

| UI | Where the element lives | Handled by |
|---|---|---|
| Classic UI16 | `iframe#gsft_main` | frame-tree walk, gsft_main searched first |
| Next Experience (Polaris) | `iframe#gsft_main` **inside shadow DOM** (`macroponent-*`) | Playwright CSS pierces open shadow roots |
| Workspaces / UI Builder | web components (`now-input`, `now-button`), no iframe | label/role strategies that pierce shadow DOM |
| UI pages, embedded widgets | iframes nested inside gsft_main | recursive walk, any depth |
| Slow instances | iframe appears late or reloads after save | polling until timeout; returned locators use a `frame_locator` chain that re-resolves |

## Command line (sn.py): the easiest way to work

```
python sn.py
```
The first time, it asks for your ServiceNow address and writes `instellingen.ini` (instance, `ui = polaris`,
browser, confirmation). The browser opens once, you log in once, and then you type short commands:

```
sn PROEF -> taak SCTASK0194511
SCTASK0194511  Standaard werkplek met extra beeldscherm leveren
  Cost center: 1004
  Affected CIs: (geen)
sn PROEF SCTASK0194511> lever bs-26-1244 pc-26-4455      <- dry run: shows what would happen
sn PROEF SCTASK0194511> echt
sn ECHT  SCTASK0194511> lever bs-26-1244 pc-26-4455      <- asks "Doorgaan? [j/N]", then saves
sn ECHT  SCTASK0194511> info
  Affected CIs:
    - BS-26-1244   Randapparaat   Installed
    - pc-26-4455   Computer       Installed
```

| Command | What it does |
|---|---|
| `taak SCTASK…` (or just the number) | select the task and show it |
| `info` | description, cost center, Affected CIs with their status (red if the cost center differs) |
| `koppel <ci> [...]` | add CIs to Affected CIs (through Edit...) |
| `installeer <ci> [...]` | Status Installed (+ Type Monitor) + cost center of the task |
| `lever <ci> [...]` | koppel + installeer for each CI (a CI that fails to link is not installed) |
| `zet state van <nr> op <waarde>` | change any field |
| `proef` / `echt` | dry run (default) / really save, with confirmation |
| `csv <file>`, `scrape`, `open`, `help`, `stop` | |

You can put the task number inside the command (`lever SCTASK0194511 bs-26-1244`). Full sentences such as
"voeg … toe aan …" still work. Everything is logged in `logboek/<date>.csv`. If you close the browser window, it
restarts on the next command. For a single command without the prompt:
`python sn.py lever SCTASK0194511 bs-26-1244 pc-26-4455` (add `--echt` to save).

### Ticket menu: lists, details and actions

```
sn PROEF -> i                                   <- open incidents of your group (also: v, n = unassigned, m, zoek <text>)
Open incidenten van de groep: 29
    #  Nummer         Geopend      Status           P  Toegewezen           Omschrijving
    1  INC1058694     22/10 11:22  In Progress      3  …                    PC … vraagt om een bitlocker recovery
    3  INC1058593     22/10 09:57  New              3  -                    USB-c aansluiting docking station ontbreekt
sn PROEF -> 3                                   <- everything about row 3: fields, description, catalog questions,
                                                   SLAs, Affected CIs and the latest work notes / comments
sn PROEF INC1058593> pak                        <- assign to me
sn PROEF INC1058593> notitie Docking station besteld
sn PROEF INC1058593> opmerking We komen morgen langs        (visible to the caller)
sn PROEF INC1058593> status On Hold: Awaiting Caller
sn PROEF INC1058593> oplossen Nieuw docking station geplaatst
```
* Lists use the Table API with your own session (fast, and only what you are allowed to see). `incidenten`, `verzoeken`
  and `vrij` filter on `groep` in `instellingen.ini`. Leave it empty for all your groups. `i` / `v` show all open tickets,
  `n` only the unassigned ones (`lijst_standaard = vrij` makes `i`/`v` show only unassigned). Filters: `i vrij`,
  `i nieuw`, `i mijn`, `i <text>`. Your own lists go under `[lijsten]` as `name = table | encoded query`.
* `toon` opens the ticket in the browser as well, so you can always look along.
* `oplossen`: an incident goes to Resolved with `oplos_code` + your text as resolution notes. A catalog task goes to
  Closed Complete.
* All actions follow PROEF/ECHT + confirmation, re-read the ticket after saving, and are logged in `logboek/`.

## Visual inspector: point at an element, get its locator

```bash
python -m servicenow_smart_locator.inspector https://dev12345.service-now.com
# or in PyCharm: set INSTANCE in run_inspector.py and Run it
# company laptop without Chromium? add:  --browser msedge
```

1. Log in and open the page you want to inspect. Your login is kept in `~/.snsl_inspector_profile`.
2. Press **F8**, or click the `UIT · F8` pill in the panel. Dashed lines now show every iframe.
3. **Hover** over any element. A label shows what it is (VELD, KNOP or ELEMENT), its name / `table.field`
   and the iframe it's in.
4. **Click** it. The click is *not* passed on to ServiceNow, so nothing gets submitted. The panel then lists:
   * ✓ every Smart Locator call that finds **exactly this element**. Each one is checked live, not guessed.
   * the same element as plain Playwright code, with the full `frame_locator` chain.
   * ✗ calls that look right but would hit a different element, such as the duplicate Submit button at the
     bottom of a UI16 form.
5. **Kopieer** puts the line on your clipboard. Every pick is also appended to `picked_locators.py`.
6. Press F8 or Esc again to use the page normally, for example to open the next form.

The overlay runs in every frame, including nested iframes and shadow DOM. Its panel sits in a *closed*
shadow root, so your tests' locators can never find it by accident.

You can also attach it to a browser you already started in your own script:

```python
from servicenow_smart_locator.inspector import Inspector
insp = Inspector(page.context).install()
insp.run()   # until you close the window
```

## Scraper: read everything on a page

```bash
python run_scraper.py INC0010001          # or: incident_list.do, or a full URL
```
or press **F9** in the inspector. You get a JSON file plus a readable overview:

```
== frame: iframe[id="gsft_main"]
  Formulier: incident  sys_id=inc2
    number             Number               string     ro   = INC0010002
    short_description  Short description    string     *    = Mail not working
    state              State                choice          = In Progress  [2]
    cmdb_ci            Configuration item   reference       = PC-0043  [ci2]
  Knoppen: Update (sysverb_update)
  Lijst 'Affected CIs': 1 rij(en), kolommen: Configuration Item
```
For each field you get the technical name, label, type, value + display value, mandatory, read-only, visible,
choices and reference table. It also collects the form's UI actions, related lists and list views (with rows) and
info/error messages. Pages without a classic form (Workspace, Portal) still give a generic list of fields and buttons.
In Python: `from servicenow_smart_locator.scraper import scrape; data = scrape(page)`.

## Automation: "voeg deze pc toe aan deze melding"

```bash
python run_automation.py "voeg PC-0042 toe aan INC0010001"                       # PROEFRUN
python run_automation.py "voeg PC-0042 toe aan INC0010001" --uitvoeren           # echt opslaan
python run_automation.py "voeg PC-0042 toe aan INC0010001 als affected ci" --uitvoeren
python run_automation.py "zet state van INC0010001 op In Progress" --uitvoeren
python run_automation.py --csv koppelingen.csv --uitvoeren                       # batch (melding;pc;modus)
```
Or edit the settings at the top of `run_automation.py` and run it from PyCharm.

What the bot does for "voeg PC-0042 toe aan INC0010001":
1. Opens the record by number. The number prefix picks the table: INC, RITM, CHG, PRB, SCTASK and others.
   It logs in with `SN_USER`/`SN_PASSWORD`, or waits while you log in yourself (SSO works).
2. Checks that `PC-0042` exists exactly once in `cmdb_ci`, through the Table API with your own session.
   If the name doesn't exist or isn't unique, it stops with a clear message.
3. Types it into **Configuration item** like a user would and waits until ServiceNow has resolved the reference.
4. **Proefrun (dry run):** stops here. Nothing is saved, and you get a screenshot.
   **With `--uitvoeren`:** it saves. It uses the Save button, or `gsftSubmit` if Save is only in the context menu.
   It then re-reads the record and checks that the CI is really there.
5. Writes `resultaten_<tijd>.csv` (OK / PROEFRUN / OVERGESLAGEN / FOUT, before/after values, screenshot) and saves
   screenshots in `automation_screenshots/`.

Safety rules:
* It never overwrites a filled Configuration item unless you pass `--overschrijven`.
* It skips a PC that's already linked.
* One failing row never stops a batch.
* A mandatory field or a business rule that blocks the save is reported as FOUT with ServiceNow's own message.

`--affected` (or "als affected ci" in the command) adds the PC to the **Affected CIs** related list instead
(a new `task_ci` record). `--polaris` runs everything through Next Experience. It only uses field names and
UI action names, so the user's language setting doesn't matter.

If a user has no REST API rights, everything still works through the UI alone. You just get a less specific error
for a CI that doesn't exist.

In Python:
```python
from servicenow_smart_locator.automation import ServiceNowBot
bot = ServiceNowBot(page, "https://dev123.service-now.com", dry_run=False)
bot.add_ci("INC0010001", "PC-0042")
bot.add_ci("INC0010001", "PC-0042", mode="affected")
bot.update_record("INC0010001", {"State": "In Progress", "assigned_to": "Beth Anglin"})
```

### Install a CI for a task ("installeer")

```bash
python run_automation.py "installeer bs-26-1244 voor SCTASK0194511"        # PROEFRUN
python run_automation.py "installeer pc-26-4455 voor SCTASK0194511" --uitvoeren
```
1. Reads the task's cost center (`request_item.u_cost_center`, change via `ServiceNowBot.TASK_COST_CENTER_FIELD`).
2. Finds the CI by name, stickernumber, asset tag or serial number, and opens it on its own class form
   (`cmdb_ci_peripheral`, `cmdb_ci_computer`, ...).
3. Sets **Status = Installed**. "Installed - Exception" is a different choice and is never picked.
4. Monitors (a peripheral whose model contains "Monitor") also get **Type = Monitor**. Write
   "installeer monitor ..." to force it.
5. Sets **Cost center** to the task's cost center, even when that field is on another form tab.
6. Saves and re-reads every value. A CI that's already correct is reported as OVERGESLAGEN.

In a CSV, put `installeer` (or `installeer monitor`) in the `modus` column.

### Practise without risk
```bash
python tests/mock_servicenow.py     # fake ServiceNow on http://127.0.0.1:8765, login admin / admin
```
Set `INSTANCE = "http://127.0.0.1:8765"` in the run files to try everything out. The mock has CIs PC-0042,
PC-0043, LAPTOP-7 and a duplicate PC-DUP, plus incidents INC0010001 to INC0010003.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Use

```python
from playwright.sync_api import sync_playwright
from servicenow_smart_locator import SmartLocator

with sync_playwright() as p:
    page = p.chromium.launch(headless=False).new_page()
    page.goto("https://<instance>.service-now.com/incident.do?sys_id=-1")
    sn = SmartLocator(page)              # default timeout 15 s

    sn.fill("Caller", "Abel Tuter")      # reference field: fills sys_display.*, tabs out
    sn.fill("short_description", "Printer on fire")
    sn.fill("State", "In Progress")      # <select>: by option label or value
    sn.fill("Active", False)             # checkbox
    sn.click("Submit")                   # matches data-action-name="sysverb_insert"
```

Every lookup gives you a normal Playwright `Locator`, so `expect(...)` and every other Locator method work on it:

```python
from playwright.sync_api import expect
expect(sn.field("Number")).to_have_value("INC0010001")
sn.button("Resolve").click()
sn.css("#related_lists_wrapper").scroll_into_view_if_needed()  # raw selector, any frame
sn.frame()                       # FrameLocator for gsft_main (wherever it is)
sn.frame(containing="#approval_comments")  # the iframe that contains this selector
```

### How do I find out where it found something?

```python
m = sn.locate_field("Caller")
print(m)
# [label[for] exact] in iframe[id="gsft_main"]  (1 match)
#   page.frame_locator('iframe[id="gsft_main"]').locator('[id="sys_display.incident.caller_id"]')
```

`m.code` is a plain Playwright expression you can paste into a test if you'd
rather hard-code it. `m.frame_path`, `m.strategy`, `m.frame` and `m.match_count`
are also available.

## What it matches

**Fields** (`sn.field(...)` / `sn.fill(...)`). You can pass a label, a field name or `table.field`:

1. ServiceNow element ids: `sys_display.<table>.<field>` (reference), `<table>.<field>`,
   `ni.<table>.<field>` (checkbox), `sys_readonly.<table>.<field>`. Hidden
   `sys_original.*` inputs are ignored.
2. `name` / `data-field-name` attributes (Service Portal, Workspace).
3. UI16 `<label for>` with `.label-text`, and the mandatory `*` is ignored.
4. Accessible label (`get_by_label`), which works across shadow DOM.
5. `aria-label`, `title`, placeholder, then partial label matches.

Field names try the id strategies first. Labels try the label strategies first, then a guessed field name
(`"Short description"` → `short_description`). `u_` custom fields work both ways.

**Buttons** (`sn.button(...)` / `sn.click(...)`):

1. UI action name (`data-action-name`, `id`, `gsft_id`). "Submit" → `sysverb_insert`,
   "Update" → `sysverb_update`, "Save" → `sysverb_update_and_stay`, and so on (see `BUTTON_ACTIONS`).
   Action names stay the same whatever language the user's UI is in, so they're tried first.
   You can also pass the action name directly: `sn.click("sysverb_update")`.
2. Exact accessible name (role=button), button text, `input[type=submit]` value.
3. `aria-label` / `title` (icon buttons), then links, menu items and tabs, then partial matches.

**Frames**: searched in this order: gsft_main and its children, then other iframes, then the top page.
For each strategy, every frame is checked before moving on to the next strategy. A stronger match in a
nested frame therefore beats a weaker match in gsft_main. Only **visible** elements count. Hidden helper
iframes and UI16's duplicated bottom-of-form buttons are skipped, and `.first` is chosen. Pass
`visible=False` to turn that off.

## Options

```python
sn = SmartLocator(page, timeout=30_000, poll_interval=250, content_frames=("gsft_main",))
sn.field("Comments", in_frame="approval_frame")    # restrict to an iframe (name/id/title/URL part)
sn.field("short_description", table="incident")   # exact table
sn.button("Submit", timeout=5_000)
```

### g_form helpers (classic forms)

```python
sn.get_value("caller_id")                 # sys_id via g_form.getValue, from the right frame
sn.set_value("caller_id", "<sys_id>", "Abel Tuter")
sn.wait_until_ready()                     # all frames document.readyState == complete
```

### When nothing is found

`SmartLocatorError` tells you what's on the page and suggests the closest match:

```
Could not find field 'Short descriptoin' within 15s.
Did you mean: 'Short description'?
Frame tree:
- (top page) https://dev123.service-now.com/now/nav/ui/classic/...
  - name='gsft_main' https://dev123.service-now.com/incident.do?sys_id=-1
```

`sn.dump_frames()` and `sn.inventory()` (visible labels and buttons in each frame) help with debugging.

## Using with pytest-playwright

```python
import pytest
from servicenow_smart_locator import SmartLocator

@pytest.fixture
def sn(page):
    return SmartLocator(page)

def test_create_incident(sn, page):
    page.goto(".../incident.do?sys_id=-1")
    sn.fill("Short description", "test")
    sn.click("Submit")
```

## Project layout

```
servicenow_smart_locator/   the library (no dependencies besides playwright)
  smart_locator.py          the locator engine
  inspector.py / .js        the visual point-and-click inspector (F8 pick, F9 scrape)
  scraper.py                scrape a page to JSON
  automation.py             ServiceNowBot, commands, CSV batch
  shell.py                  the sn.py command line
run_inspector.py            start the inspector from PyCharm
run_scraper.py              scrape a record / list / URL
sn.py                       interactive command line (start here)
run_automation.py           run automations (dry run by default)
koppelingen_voorbeeld.csv   example batch file
tests/mock_servicenow.py    fake ServiceNow instance for safe testing
tests/                      pytest suite + mock pages copying UI16, Next Experience, Workspace,
                            nested and late-loading iframes
examples/demo_incident.py   run against a real instance (dry run unless SN_SUBMIT=1)
```

Run the tests with `pytest`. Set `HEADED=1` to watch them in a browser window.

## Limits

* Sync API only. If you need async, the same design ports straight to `playwright.async_api`.
* **Closed** shadow roots can't be reached by Playwright or anything else. ServiceNow uses open ones.
* Reference fields are committed by typing the value and pressing Tab, so the value must match one record.
  Use `set_value(field, sys_id, display)` when you need an exact sys_id.
