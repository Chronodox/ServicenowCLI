"""
Visual inspector for ServiceNow pages.

Opens a browser window. Press F8, hover over any field or button to see what
it is, and click it to get the Smart Locator code that finds *exactly that*
element. Each suggestion is checked live against the page.

    python -m servicenow_smart_locator.inspector https://dev12345.service-now.com
    python -m servicenow_smart_locator.inspector https://... --browser msedge

Your login is kept in a browser profile (default: ~/.snsl_inspector_profile),
so you only have to log in once. Every pick is also appended to
picked_locators.py so you can copy it into your tests later.
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import sys
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Frame, Page, sync_playwright

from .smart_locator import SmartLocator, SmartLocatorError, SmartMatch, _css_str

JS = Path(__file__).with_name("inspector.js").read_text(encoding="utf-8")
_IS_PICKED = "e => e.getAttribute('data-snsl-pick')"


class Inspector:
    """Attach the visual inspector to a Playwright ``BrowserContext``.

    Works with any context, so you can also use it from your own scripts::

        insp = Inspector(page.context).install()
        insp.run()       # blocks until the browser is closed
    """

    def __init__(self, context: BrowserContext, output_file: Optional[Path] = None, echo: bool = True):
        self.context = context
        self.output_file = Path(output_file) if output_file else None
        self.echo = echo
        self.on = False
        self.results: List[dict] = []
        self._events: collections.deque = collections.deque()

    # ----------------------------------------------------------------- setup
    def install(self) -> "Inspector":
        c = self.context
        # Callbacks only queue work: Playwright calls happen in process_pending().
        c.expose_binding("__snslPick", lambda source, payload: self._events.append(("pick", source, payload)))
        c.expose_binding("__snslToggle", lambda source: self._events.append(("toggle", source, None)))
        c.expose_binding("__snslState", lambda source: self.on)
        c.expose_binding("__snslScrape", lambda source: self._events.append(("scrape", source, None)))
        c.add_init_script(JS)
        for page in c.pages:  # pages that were already open
            for fr in page.frames:
                try:
                    fr.evaluate(JS)
                except PlaywrightError:
                    pass
        return self

    def set_on(self, on: bool) -> None:
        self.on = on
        for page in self.context.pages:
            for fr in page.frames:
                try:
                    fr.evaluate("v => window.__snslSetOn && window.__snslSetOn(v)", on)
                except PlaywrightError:
                    pass
        self._say(f"Inspectie {'AAN - klik op een veld of knop' if on else 'UIT'}")

    # ------------------------------------------------------------------ loop
    def process_pending(self) -> None:
        while self._events:
            kind, source, payload = self._events.popleft()
            if kind == "toggle":
                self.set_on(not self.on)
            elif kind == "scrape":
                self._scrape(source["page"])
            elif kind == "pick":
                page: Page = source["page"]
                try:
                    result = self.analyse(page, source["frame"], payload)
                except PlaywrightError as e:  # page navigated while checking
                    result = {"kind": payload.get("kind"), "title": payload.get("summary", ""),
                              "frame": "?", "lines": [], "raw": "", "error": f"Pagina veranderde tijdens controle: {e}"}
                self.results.append(result)
                self._report(page, result)

    def run(self) -> None:
        """Process picks until every browser window is closed (or Ctrl+C)."""
        try:
            while True:
                pages = self.context.pages
                if not pages:
                    break
                try:
                    pages[-1].wait_for_timeout(100)
                except PlaywrightError:
                    continue
                self.process_pending()
        except KeyboardInterrupt:
            pass

    # -------------------------------------------------------------- analysis
    def analyse(self, page: Page, frame: Frame, p: dict) -> dict:
        """Check which queries resolve to the picked element (marked with data-snsl-pick)."""
        sn = SmartLocator(page, timeout=0)
        pid, kind = p["pickId"], p["kind"]
        lines, best = [], None

        for q in p.get("candidates", []):
            code = self._action_code(p, q)
            try:
                m = (sn.locate_field if kind == "field" else sn.locate_button)(q, timeout=0)
            except SmartLocatorError:
                lines.append({"ok": False, "code": code, "note": "vindt niets met deze zoekterm"})
                continue
            if self._is_picked(m, pid):
                how = "uniek" if m.match_count == 1 else f"eerste zichtbare van {m.match_count}"
                lines.append({"ok": True, "code": code, "note": f"{how} · strategie: {m.strategy}"})
                best = best or m
            else:
                where = " > ".join(m.frame_path) or "hoofdpagina"
                lines.append({"ok": False, "code": code, "note": f"vindt een ánder element ({where})"})

        raw, raw_ok, raw_note = (best.code, True, "") if best else self._raw(sn, p)
        frame_path = best.frame_path if best else self._frame_path(sn, frame)
        return {"kind": kind, "title": p.get("summary", ""), "frame": " > ".join(frame_path) or "hoofdpagina",
                "frame_path": frame_path, "lines": lines, "raw": raw, "raw_ok": raw_ok, "raw_note": raw_note,
                "time": _dt.datetime.now().strftime("%H:%M:%S")}

    @staticmethod
    def _is_picked(m: SmartMatch, pid: str) -> bool:
        try:
            return m.locator.evaluate(_IS_PICKED) == pid
        except PlaywrightError:
            return False

    def _raw(self, sn: SmartLocator, p: dict):
        """Plain selector fallback when no Smart Locator query fits."""
        cands = []
        if p.get("id") and '"' not in p["id"]:
            cands.append(f"[id={_css_str(p['id'])}]")
        if p.get("name"):
            cands.append(f"{p['tag']}[name={_css_str(p['name'])}]")
        if p.get("cssPath"):
            cands.append(p["cssPath"])
        for sel in cands:
            try:
                m = sn.locate_css(sel, timeout=0)
            except SmartLocatorError:
                continue
            if self._is_picked(m, p["pickId"]):
                note = "" if m.match_count == 1 else f"⚠ selector matcht {m.match_count} elementen, eerste wordt gebruikt"
                return m.code, True, note or "geen Smart Locator-term gevonden, dit is de stabielste selector"
        return "", False, "Kon geen selector vinden die dit element uniek aanwijst"

    @staticmethod
    def _frame_path(sn: SmartLocator, frame: Frame) -> List[str]:
        for node in sn._frames(None):
            if node.frame == frame:
                return sn._frame_chain(node)[1]
        return []

    @staticmethod
    def _action_code(p: dict, q: str) -> str:
        if p["kind"] == "button":
            return f"sn.click({q!r})"
        if p.get("tag") == "select":
            return f"sn.select({q!r}, '<optie>')"
        if p.get("type") == "checkbox" or p.get("role") in ("checkbox", "switch"):
            return f"sn.fill({q!r}, True)"
        return f"sn.fill({q!r}, '<waarde>')"

    def _scrape(self, page: Page) -> Optional[Path]:
        from .scraper import save, scrape, summarize
        try:
            data = scrape(page)
        except PlaywrightError as e:
            self._say(f"Scrapen mislukt: {e}")
            return None
        folder = self.output_file.parent if self.output_file else Path(".")
        path = save(data, folder / f"scrape_{_dt.datetime.now():%Y%m%d_%H%M%S}.json").resolve()
        s = data["summary"]
        r = {"kind": "scrape", "frame": " / ".join(" > ".join(f["frame_path"]) or "hoofdpagina" for f in data["frames"]),
             "title": f"{s['form_fields']} velden, {s['buttons']} knoppen, {s['lists']} lijsten, {s['messages']} meldingen",
             "lines": [{"ok": True, "code": str(path), "note": "JSON opgeslagen"}], "raw": ""}
        self.results.append(r)
        try:
            page.main_frame.evaluate("r => window.__snslShow && window.__snslShow(r)", r)
        except PlaywrightError:
            pass
        self._say(summarize(data) + f"\nOpgeslagen: {path}")
        return path

    # --------------------------------------------------------------- output
    def _report(self, page: Page, r: dict) -> None:
        try:
            page.main_frame.evaluate("r => window.__snslShow && window.__snslShow(r)", r)
        except PlaywrightError:
            pass
        kind = {"field": "VELD", "button": "KNOP"}.get(r["kind"], "ELEMENT")
        out = [f"\n[{r.get('time', '')}] {kind}  {r['title']}", f"  frame: {r['frame']}"]
        if r.get("error"):
            out.append(f"  ! {r['error']}")
        for l in r["lines"]:
            out.append(f"  {'✓' if l['ok'] else '✗'} {l['code']}    # {l['note']}")
        if r.get("raw"):
            out.append(f"  ≡ {r['raw']}")
        self._say("\n".join(out))

        if self.output_file and (r["lines"] or r.get("raw")):
            good = [l["code"] for l in r["lines"] if l["ok"]]
            with self.output_file.open("a", encoding="utf-8") as fh:
                fh.write(f"# [{r.get('time', '')}] {kind}: {r['title']}   (frame: {r['frame']})\n")
                fh.write((good[0] if good else f"# {r['raw']}") + "\n")
                if good and r.get("raw"):
                    fh.write(f"# {r['raw']}\n")
                fh.write("\n")

    def _say(self, msg: str) -> None:
        if self.echo:
            try:
                print(msg, flush=True)
            except UnicodeEncodeError:  # old Windows console code page
                print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


# ------------------------------------------------------------------- CLI
def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m servicenow_smart_locator.inspector",
                                 description="Visuele inspector: klik op een ServiceNow-element en zie de juiste locator.")
    ap.add_argument("url", help="ServiceNow URL, bijv. https://dev12345.service-now.com")
    ap.add_argument("--browser", choices=["chromium", "msedge", "chrome"], default="chromium",
                    help="msedge/chrome gebruiken de geïnstalleerde browser (handig als Chromium-download geblokkeerd is)")
    ap.add_argument("--profile", default=str(Path.home() / ".snsl_inspector_profile"),
                    help="map voor het browserprofiel (bewaart je login)")
    ap.add_argument("--out", default="picked_locators.py", help="bestand waar elke klik aan wordt toegevoegd")
    args = ap.parse_args(argv)

    with sync_playwright() as p:
        kw = {"headless": False, "no_viewport": True, "args": ["--start-maximized"]}
        if args.browser != "chromium":
            kw["channel"] = args.browser
        ctx = p.chromium.launch_persistent_context(args.profile, **kw)
        insp = Inspector(ctx, output_file=Path(args.out)).install()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url)
        print("\nServiceNow Smart Locator - inspector\n"
              "  1. Log in en open de pagina die je wilt bekijken\n"
              "  2. Druk op F8 (of klik op UIT in het paneel rechtsonder)\n"
              "  3. Beweeg over velden/knoppen en klik om de locator te krijgen\n"
              "  F9 = hele pagina scrapen naar JSON\n"
              f"  Elke klik wordt ook opgeslagen in {Path(args.out).resolve()}\n"
              "  Sluit het browservenster om te stoppen.\n", flush=True)
        insp.run()
        try:
            ctx.close()
        except PlaywrightError:
            pass


if __name__ == "__main__":
    main(sys.argv[1:])
