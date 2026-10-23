"""
ServiceNow Smart Locator for Playwright (sync API).

Finds the right iframe / nested iframe / shadow root and the right field or
button on any ServiceNow UI:

* Classic UI16          -> content lives in ``iframe#gsft_main``
* Next Experience       -> ``iframe#gsft_main`` sits inside shadow DOM
  (Polaris)                (``macroponent-*`` components)
* Workspaces / UIB      -> Web components with open shadow roots, no iframe
* Embedded UI pages     -> iframes nested inside gsft_main

Usage::

    from servicenow_smart_locator import SmartLocator

    sn = SmartLocator(page)
    sn.fill("Short description", "Printer on fire")
    sn.fill("caller_id", "Abel Tuter")          # reference field
    sn.select("State", "In Progress")
    sn.click("Submit")

    m = sn.locate_field("Short description")
    print(m.code)   # page.frame_locator('iframe[id="gsft_main"]').locator(...)

Every lookup returns a normal Playwright ``Locator`` built on a
``frame_locator`` chain, so it keeps working when ServiceNow reloads the
iframe after a save.
"""
from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, Union

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, FrameLocator, Locator, Page

log = logging.getLogger("servicenow_smart_locator")

Scope = Union[Page, Frame, FrameLocator]

#: iframe names/ids that hold the main ServiceNow content. Searched first.
CONTENT_FRAME_NAMES: Tuple[str, ...] = ("gsft_main",)

#: Button text -> ServiceNow UI action names (sys_ui_action.action_name).
#: Action names don't change with the user's language, so they are tried
#: before visible text.
BUTTON_ACTIONS = {
    "submit": ["sysverb_insert"],
    "insert": ["sysverb_insert"],
    "update": ["sysverb_update"],
    "save": ["sysverb_update_and_stay", "sysverb_insert_and_stay"],
    "insert and stay": ["sysverb_insert_and_stay"],
    "delete": ["sysverb_delete"],
    "cancel": ["sysverb_cancel"],
    "new": ["sysverb_new"],
    "back": ["sysverb_back"],
    "resolve": ["resolve_incident"],
}

_INPUTISH = (
    'input:not([type="hidden"]), select, textarea, '
    '[contenteditable="true"], [role="textbox"], [role="combobox"], '
    '[role="checkbox"], [role="switch"], [role="spinbutton"]'
)
_CLICKABLE = (
    'button, input[type="submit"], input[type="button"], input[type="reset"], '
    'a, [role="button"], [role="menuitem"], [role="tab"], [role="link"]'
)
_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
_TABLE_FIELD = re.compile(r"^([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")


class SmartLocatorError(LookupError):
    """Raised when nothing matching the query is found before the timeout."""


# --------------------------------------------------------------------------- #
# Small value objects
# --------------------------------------------------------------------------- #
def _css_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class Spec:
    """One way of locating an element: a Playwright method + arguments.

    Kept as data (not a lambda) so it can be re-applied to any scope
    (Frame or FrameLocator chain) and printed as copy-pasteable code.
    """

    method: str
    args: tuple = ()
    kwargs: tuple = ()  # tuple of (key, value) pairs -> hashable

    def build(self, scope: Scope) -> Locator:
        return getattr(scope, self.method)(*self.args, **dict(self.kwargs))

    def code(self) -> str:
        parts = [repr(a) for a in self.args]
        parts += [f"{k}={v!r}" for k, v in self.kwargs]
        return f".{self.method}({', '.join(parts)})"


def _css(selector: str) -> Spec:
    return Spec("locator", (selector,))


@dataclass
class SmartMatch:
    """Result of a lookup."""

    locator: Locator          # stable locator (frame_locator chain)
    frame: Frame              # the frame the element lives in right now
    frame_path: List[str]     # iframe selectors from the top page down
    strategy: str             # which strategy matched
    code: str                 # copy-pasteable Playwright code
    match_count: int          # how many elements the selector matched

    def __str__(self) -> str:
        where = " > ".join(self.frame_path) or "(top page)"
        return (f"[{self.strategy}] in {where}  ({self.match_count} match"
                f"{'es' if self.match_count != 1 else ''})\n  {self.code}")


@dataclass
class _FrameNode:
    frame: Frame
    path: List[Frame]  # frames from (excluding) main frame down to this one
    rank: Tuple[int, int, int]


StrategyFn = Callable[[Frame], List[Spec]]


# --------------------------------------------------------------------------- #
# Browser-side helpers
# --------------------------------------------------------------------------- #
# Finds UI16-style <label for="..."> whose text matches, and returns the id of
# the element the user actually types into (handles reference fields
# "sys_display.*", checkboxes "ni.*" and read-only "sys_readonly.*").
_JS_LABEL_TARGETS = r"""
([wanted, exact]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').replace(/^[\s*]+|[\s*:]+$/g, '').trim().toLowerCase();
  const usable = el => el && !(el.tagName === 'INPUT' && el.type === 'hidden');
  const strip = id => id.replace(/^(sys_display|ni|sys_readonly)\./, '');
  const out = [];
  const walk = root => {
    for (const lab of root.querySelectorAll('label[for]')) {
      const span = lab.querySelector('.label-text');
      const txt = norm(span ? span.textContent : lab.textContent);
      if (!(exact ? txt === wanted : txt.includes(wanted))) continue;
      const id = lab.getAttribute('for');
      const base = strip(id);
      for (const cand of [id, 'sys_display.' + base, base, 'ni.' + base, 'sys_readonly.' + base]) {
        const el = root.getElementById ? root.getElementById(cand) : root.querySelector('[id="' + CSS.escape(cand) + '"]');
        if (usable(el)) { out.push(cand); break; }
      }
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  return [...new Set(out)];
}
"""

# Collects visible label / button texts in a frame (incl. open shadow roots)
# for "did you mean ...?" suggestions.
_JS_INVENTORY = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/^[\s*]+|[\s*:]+$/g, '').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const fields = new Set(), buttons = new Set();
  const idre = /^(?:sys_display\.|ni\.|sys_readonly\.)?([a-z0-9_]+)\.([a-z0-9_]+)$/;
  const walk = root => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      const tag = el.tagName.toLowerCase();
      if (tag === 'label') {
        const sp = el.querySelector('.label-text'); const t = clean(sp ? sp.textContent : el.textContent);
        if (t && t.length < 80) fields.add(t);
      } else if ((tag === 'input' && el.type !== 'hidden' && !['submit','button','reset'].includes(el.type)) || tag === 'select' || tag === 'textarea') {
        if (!vis(el)) continue;
        for (const a of ['aria-label', 'placeholder']) { const v = clean(el.getAttribute(a)); if (v) fields.add(v); }
        const m = idre.exec(el.id || ''); if (m) { fields.add(m[2]); fields.add(m[1] + '.' + m[2]); }
        if (el.name && !el.name.includes('.')) fields.add(el.name);
      } else if (tag === 'button' || el.getAttribute('role') === 'button' ||
                 (tag === 'input' && ['submit','button'].includes(el.type))) {
        if (!vis(el)) continue;
        const t = clean(el.getAttribute('aria-label') || el.textContent || el.value);
        if (t && t.length < 60) buttons.add(t);
        const an = el.getAttribute('data-action-name') || (/^sysverb_/.test(el.id) ? el.id : '');
        if (an) buttons.add(an);
      }
    }
  };
  walk(document);
  return {fields: [...fields], buttons: [...buttons]};
}
"""

_JS_FRAME_ATTRS = """e => ({tag: e.tagName.toLowerCase(), id: e.id || '',
  name: e.getAttribute('name') || '', title: e.getAttribute('title') || '',
  src: e.getAttribute('src') || ''})"""


# --------------------------------------------------------------------------- #
# The locator
# --------------------------------------------------------------------------- #
class SmartLocator:
    """Finds ServiceNow fields, buttons and frames across iframes and shadow DOM.

    :param page: Playwright ``Page``.
    :param timeout: default wait (ms) for an element to appear.
    :param poll_interval: ms between re-scans of the frame tree.
    :param content_frames: iframe names/ids searched first.
    """

    def __init__(self, page: Page, timeout: float = 15_000, poll_interval: float = 250,
                 content_frames: Sequence[str] = CONTENT_FRAME_NAMES):
        self.page = page
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.content_frames = tuple(content_frames)
        self._cache: dict = {}

    # ------------------------------------------------------------------ API
    def field(self, query: str, **kw) -> Locator:
        """Locator for a form field by label, field name or ``table.field``."""
        return self.locate_field(query, **kw).locator

    def button(self, query: str, **kw) -> Locator:
        """Locator for a button / UI action by text, aria-label or action name."""
        return self.locate_button(query, **kw).locator

    def css(self, selector: str, **kw) -> Locator:
        """Search every frame for a raw Playwright selector (CSS, text=, role=...)."""
        return self.locate_css(selector, **kw).locator

    def locate_field(self, query: str, table: Optional[str] = None, *, in_frame: Optional[str] = None,
                     timeout: Optional[float] = None, visible: bool = True) -> SmartMatch:
        strategies = self._field_strategies(query, table)
        return self._search("field", query, strategies, in_frame, timeout, visible, key=(table,))

    def locate_button(self, query: str, *, in_frame: Optional[str] = None,
                      timeout: Optional[float] = None, visible: bool = True) -> SmartMatch:
        return self._search("button", query, self._button_strategies(query), in_frame, timeout, visible)

    def locate_css(self, selector: str, *, in_frame: Optional[str] = None,
                   timeout: Optional[float] = None, visible: bool = True) -> SmartMatch:
        strategies = [("selector", lambda f, s=selector: [_css(s)])]
        return self._search("css", selector, strategies, in_frame, timeout, visible)

    def find(self, query: str, **kw) -> Locator:
        """Field first, then button, then any element with that exact text."""
        t = kw.pop("timeout", None) or self.timeout
        strategies = (self._field_strategies(query, None) + self._button_strategies(query) +
                      [("text exact", lambda f, q=query: [Spec("get_by_text", (q,), (("exact", True),))])])
        return self._search("any", query, strategies, kw.get("in_frame"), t,
                            kw.get("visible", True)).locator

    # ---- frames
    def frame(self, name_or_url: Optional[str] = None, *, containing: Optional[str] = None,
              timeout: Optional[float] = None) -> FrameLocator:
        """Stable ``FrameLocator`` for an iframe (default: the content frame).

        :param name_or_url: iframe name/id, or a substring of its URL.
        :param containing: alternatively, a selector that must exist inside it.
        """
        return self.locate_frame(name_or_url, containing=containing, timeout=timeout)[0]

    def locate_frame(self, name_or_url: Optional[str] = None, *, containing: Optional[str] = None,
                     timeout: Optional[float] = None) -> Tuple[FrameLocator, Frame, List[str]]:
        deadline = time.monotonic() + (timeout or self.timeout) / 1000
        want = name_or_url or (None if containing else self.content_frames[0])
        while True:
            for node in self._frames(None):
                if not node.path:
                    continue
                try:
                    if want and not self._frame_matches(node, want):
                        continue
                    if containing and node.frame.locator(containing).count() == 0:
                        continue
                    scope, sels = self._frame_chain(node)
                    return scope, node.frame, sels  # type: ignore[return-value]
                except PlaywrightError:
                    continue
            if time.monotonic() >= deadline:
                raise SmartLocatorError(
                    f"No iframe matching name/url={want!r} containing={containing!r}.\n"
                    f"Frame tree:\n{self.dump_frames()}")
            self.page.wait_for_timeout(self.poll_interval)

    # ---- actions
    def fill(self, query: str, value: Union[str, bool], *, table: Optional[str] = None,
             commit_reference: bool = True, **kw) -> SmartMatch:
        """Fill a field. Handles text, textarea, select, checkbox and reference fields."""
        m = self.locate_field(query, table, **kw)
        loc = m.locator
        info = loc.evaluate("e => ({tag: e.tagName.toLowerCase(), type: (e.type||'').toLowerCase(),"
                            " id: e.id || '', role: e.getAttribute('role') || ''})")
        if info["tag"] == "select":
            self._select(loc, str(value))
        elif info["type"] == "checkbox" or info["role"] in ("checkbox", "switch"):
            loc.set_checked(value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on"))
        else:
            loc.fill(str(value))
            if commit_reference and info["id"].startswith("sys_display."):
                # Reference field: let the autocomplete AJAX run, then tab out so
                # ServiceNow resolves the display value to a sys_id.
                self.page.wait_for_timeout(600)
                loc.press("Tab")
        return m

    def select(self, query: str, option: str, **kw) -> SmartMatch:
        m = self.locate_field(query, **kw)
        self._select(m.locator, option)
        return m

    def click(self, query: str, **kw) -> SmartMatch:
        m = self.locate_button(query, **kw)
        m.locator.click()
        return m

    # ---- g_form helpers (classic forms)
    def gform_frame(self, timeout: Optional[float] = None) -> Frame:
        """The frame that exposes ServiceNow's client-side ``g_form`` API."""
        deadline = time.monotonic() + (timeout or self.timeout) / 1000
        while True:
            for node in self._frames(None):
                try:
                    if node.frame.evaluate("() => !!(window.g_form && typeof g_form.getValue === 'function')"):
                        return node.frame
                except PlaywrightError:
                    continue
            if time.monotonic() >= deadline:
                raise SmartLocatorError("No frame exposes g_form (is a classic form open?)")
            self.page.wait_for_timeout(self.poll_interval)

    def get_value(self, field: str) -> str:
        return self.gform_frame().evaluate("f => g_form.getValue(f)", field)

    def set_value(self, field: str, value: str, display_value: Optional[str] = None) -> None:
        args = [field, value] + ([display_value] if display_value is not None else [])
        self.gform_frame().evaluate("a => g_form.setValue(...a)", args)

    def wait_until_ready(self, timeout: Optional[float] = None) -> None:
        """Wait until every frame has finished loading (incl. gsft_main reloads)."""
        deadline = time.monotonic() + (timeout or self.timeout) / 1000
        self.page.wait_for_load_state("domcontentloaded")
        while time.monotonic() < deadline:
            try:
                states = [n.frame.evaluate("() => document.readyState") for n in self._frames(None)]
                if all(s == "complete" for s in states):
                    return
            except PlaywrightError:
                pass
            self.page.wait_for_timeout(self.poll_interval)
        raise SmartLocatorError("Frames did not finish loading in time")

    # ---- diagnostics
    def dump_frames(self) -> str:
        """Human-readable frame tree, useful when a lookup fails."""
        lines: List[str] = []

        def rec(frame: Frame, depth: int):
            try:
                if frame.is_detached():
                    return
                url = frame.url
            except PlaywrightError:
                return
            label = "(top page)" if frame.parent_frame is None else f"name={frame.name!r}"
            lines.append(f"{'  ' * depth}- {label} {url}")
            for c in frame.child_frames:
                rec(c, depth + 1)
        rec(self.page.main_frame, 0)
        return "\n".join(lines)

    def inventory(self) -> dict:
        """Visible field labels / names and button texts, per frame URL."""
        out = {}
        for node in self._frames(None):
            try:
                out[node.frame.url] = node.frame.evaluate(_JS_INVENTORY)
            except PlaywrightError:
                pass
        return out

    # --------------------------------------------------------- strategies
    def _field_strategies(self, query: str, table: Optional[str]) -> List[Tuple[str, StrategyFn]]:
        q = query.strip()
        m = _TABLE_FIELD.match(q)
        if m:
            table, fname = m.group(1), m.group(2)
        elif _IDENT.match(q) and ("_" in q or q.islower()):
            fname = q
        else:
            fname = None

        label = q if fname is None else _humanize(fname)
        label_l = label.lower()

        def by_id(f: Frame, fname=fname, table=table) -> List[Spec]:
            if table:
                ids = [f"sys_display.{table}.{fname}", f"{table}.{fname}",
                       f"ni.{table}.{fname}", f"sys_readonly.{table}.{fname}"]
                return [_css(f'[id={_css_str(i)}]:not([type="hidden"])') for i in ids]
            sfx = _css_str("." + fname)
            return [
                _css(f'input[id^="sys_display."][id$={sfx}]'),
                _css(f':is(input, select, textarea)[id$={sfx}]:not([type="hidden"])'
                     f':not([id^="sys_original."])'),
            ]

        def by_name(f: Frame, fname=fname) -> List[Spec]:
            v = _css_str(fname)
            return [_css(f':is({_INPUTISH})[name={v}]'),
                    _css(f'[data-field-name={v}] :is({_INPUTISH})'),
                    _css(f'[data-field={v}] :is({_INPUTISH})')]

        def label_for(f: Frame, exact: bool) -> List[Spec]:
            ids = f.evaluate(_JS_LABEL_TARGETS, [label_l, exact])
            return [_css(f"[id={_css_str(i)}]") for i in ids]

        strategies: List[Tuple[str, StrategyFn]] = []
        id_strats: List[Tuple[str, StrategyFn]] = []
        if fname:
            id_strats = [("servicenow element id", by_id), ("name attribute", by_name)]

        label_strats: List[Tuple[str, StrategyFn]] = [
            ("label[for] exact", lambda f: label_for(f, True)),
            ("aria label exact", lambda f: [Spec("get_by_label", (label,), (("exact", True),))]),
        ]
        loose_strats: List[Tuple[str, StrategyFn]] = [
            ("aria-label / title", lambda f: [
                _css(f':is({_INPUTISH})[aria-label={_css_str(label)} i]'),
                _css(f':is({_INPUTISH})[title={_css_str(label)} i]')]),
            ("placeholder", lambda f: [Spec("get_by_placeholder", (label,), (("exact", True),))]),
            ("label[for] contains", lambda f: label_for(f, False)),
            ("aria label contains", lambda f: [Spec("get_by_label", (label,))]),
        ]

        if fname:
            strategies = id_strats + label_strats
        else:
            # A human label: trust the label first, then guess the field name.
            guess = re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")
            guessed = [(f"guessed field name '{guess}'", lambda f, g=guess: by_id(f, g, table))] \
                if guess and _IDENT.match(guess) else []
            strategies = label_strats + guessed
        return strategies + loose_strats

    def _button_strategies(self, query: str) -> List[Tuple[str, StrategyFn]]:
        q = query.strip()
        actions = list(BUTTON_ACTIONS.get(q.lower(), []))
        if _IDENT.match(q) and "_" in q:
            actions.insert(0, q)  # user passed an action name like "sysverb_update"
        qs = _css_str(q)
        strategies: List[Tuple[str, StrategyFn]] = []
        if actions:
            def by_action(f: Frame) -> List[Spec]:
                out = []
                for a in actions:
                    v = _css_str(a)
                    out += [_css(f':is(button, input, a, [role="button"])[data-action-name={v}]'),
                            _css(f':is(button, input, a, [role="button"])[id={v}]'),
                            _css(f':is(button, input, a)[gsft_id={v}]')]
                return out
            strategies.append(("ui action name", by_action))
        strategies += [
            ("role=button exact", lambda f: [Spec("get_by_role", ("button",), (("name", q), ("exact", True)))]),
            ("button text", lambda f: [_css(f'button:text-is({qs})'),
                                       _css(f'input:is([type="submit"], [type="button"])[value={qs} i]')]),
            ("aria-label / title", lambda f: [_css(f':is({_CLICKABLE})[aria-label={qs} i]'),
                                              _css(f':is({_CLICKABLE})[title={qs} i]')]),
            ("role=link/menuitem/tab exact", lambda f: [
                Spec("get_by_role", (r,), (("name", q), ("exact", True))) for r in ("link", "menuitem", "tab")]),
            ("role=button contains", lambda f: [Spec("get_by_role", ("button",), (("name", q),))]),
        ]
        return strategies

    # ------------------------------------------------------------ engine
    def _search(self, kind: str, query: str, strategies: List[Tuple[str, StrategyFn]],
                in_frame: Optional[str], timeout: Optional[float], visible: bool,
                key: tuple = ()) -> SmartMatch:
        cache_key = (kind, query, in_frame, visible) + key
        cached = self._cache.get(cache_key)
        if cached:
            hit = self._try_cached(cached, visible)
            if hit:
                return hit

        wait_ms = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            nodes = self._frames(in_frame)
            for strat_name, strat in strategies:
                for node in nodes:
                    hit = self._try(node, strat_name, strat, visible)
                    if hit:
                        log.debug("found %s %r: %s", kind, query, hit)
                        self._cache[cache_key] = hit
                        return hit
            if time.monotonic() >= deadline:
                raise SmartLocatorError(self._not_found_message(kind, query, in_frame, wait_ms))
            self.page.wait_for_timeout(self.poll_interval)

    def _try(self, node: _FrameNode, strat_name: str, strat: StrategyFn,
             visible: bool) -> Optional[SmartMatch]:
        try:
            specs = strat(node.frame)
        except PlaywrightError:
            return None
        for spec in specs:
            try:
                loc = spec.build(node.frame)
                n = loc.count()
                if n == 0:
                    continue
                idx = 0
                if visible:
                    idx = next((i for i in range(min(n, 10)) if loc.nth(i).is_visible()), -1)
                    if idx < 0:
                        continue
                return self._make_match(node, spec, strat_name, idx, n)
            except PlaywrightError as e:  # frame navigated / detached mid-check
                log.debug("strategy %s failed in %s: %s", strat_name, node.frame.url, e)
                continue
        return None

    def _try_cached(self, m: SmartMatch, visible: bool) -> Optional[SmartMatch]:
        try:
            if m.locator.count() and (not visible or m.locator.is_visible()):
                return m
        except PlaywrightError:
            pass
        return None

    def _make_match(self, node: _FrameNode, spec: Spec, strat_name: str,
                    idx: int, n: int) -> SmartMatch:
        scope, sels = self._frame_chain(node)
        loc = spec.build(scope)
        code = "page" + "".join(f".frame_locator({s!r})" for s in sels) + spec.code()
        if n > 1:
            loc = loc.first if idx == 0 else loc.nth(idx)
            code += ".first" if idx == 0 else f".nth({idx})"
        return SmartMatch(loc, node.frame, sels, strat_name, code, n)

    # ------------------------------------------------------------ frames
    def _frames(self, in_frame: Optional[str]) -> List[_FrameNode]:
        nodes: List[_FrameNode] = []

        def rec(frame: Frame, path: List[Frame], in_content: bool):
            try:
                if frame.is_detached():
                    return
                name = frame.name or ""
            except PlaywrightError:
                return
            is_content = in_content or name in self.content_frames
            depth = len(path)
            # rank: content frames first (shallow first), then other iframes, top page last
            group = 0 if is_content else (2 if depth == 0 else 1)
            nodes.append(_FrameNode(frame, path, (group, depth, len(nodes))))
            for child in frame.child_frames:
                rec(child, path + [child], is_content)

        rec(self.page.main_frame, [], False)
        if in_frame:
            roots = [n for n in nodes if self._frame_matches(n, in_frame)]
            nodes = [n for n in nodes if any(r.frame in n.path or r is n for r in roots)]
        return sorted(nodes, key=lambda n: n.rank)

    @staticmethod
    def _frame_matches(node: _FrameNode, want: str) -> bool:
        """True if the iframe's name/id/title equals ``want`` or its URL contains it."""
        try:
            if node.frame.name == want:
                return True
            if node.path:
                attrs = node.frame.frame_element().evaluate(_JS_FRAME_ATTRS)
                if want in (attrs["id"], attrs["name"], attrs["title"]):
                    return True
            return want in node.frame.url
        except PlaywrightError:
            return False

    def _frame_chain(self, node: _FrameNode) -> Tuple[Scope, List[str]]:
        scope: Scope = self.page
        sels: List[str] = []
        for fr in node.path:
            sel = self._frame_selector(fr)
            sels.append(sel)
            scope = scope.frame_locator(sel)
        return scope, sels

    @staticmethod
    def _frame_selector(frame: Frame) -> str:
        """Most stable selector that uniquely identifies ``frame`` in its parent."""
        parent = frame.parent_frame
        el = frame.frame_element()
        a = el.evaluate(_JS_FRAME_ATTRS)
        tag = a["tag"]
        cands = []
        for attr in ("id", "name", "title"):
            if a[attr]:
                cands.append(f"{tag}[{attr}={_css_str(a[attr])}]")
        if a["src"]:
            seg = a["src"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            if seg and not seg.startswith(("javascript:", "about:")):
                cands.append(f"{tag}[src*={_css_str(seg)}]")
        for c in cands:
            if parent.locator(c).count() == 1:
                return c
        for i, h in enumerate(parent.locator(tag).element_handles()):
            if h.evaluate("(a, b) => a === b", el):
                return f"{tag} >> nth={i}"
        return cands[0] if cands else tag

    # ------------------------------------------------------------ misc
    @staticmethod
    def _select(loc: Locator, option: str) -> None:
        try:
            loc.select_option(label=option, timeout=2000)
        except PlaywrightError:
            loc.select_option(value=option)

    def _not_found_message(self, kind: str, query: str, in_frame: Optional[str],
                           wait_ms: float) -> str:
        pool = set()
        for inv in self.inventory().values():
            if kind in ("field", "any"):
                pool.update(inv.get("fields", []))
            if kind in ("button", "any"):
                pool.update(inv.get("buttons", []))
        lower = {p.lower(): p for p in pool}
        close = [lower[c] for c in difflib.get_close_matches(query.lower(), list(lower), n=6, cutoff=0.5)]
        msg = [f"Could not find {kind} {query!r}" + (f" in frame {in_frame!r}" if in_frame else "")
               + f" within {wait_ms / 1000:g}s."]
        if close:
            msg.append("Did you mean: " + ", ".join(repr(c) for c in close) + "?")
        msg.append("Frame tree:\n" + self.dump_frames())
        return "\n".join(msg)


def _humanize(field_name: str) -> str:
    """'short_description' -> 'Short description', 'u_cost_center' -> 'Cost center'."""
    name = re.sub(r"^u_", "", field_name).replace("_", " ").strip()
    return name[:1].upper() + name[1:]
