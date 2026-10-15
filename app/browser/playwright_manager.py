"""The assistant's own browser, driven with Playwright.

* Separate profile (data/browser-profile): never your personal browser profile,
  so the assistant sees none of your logins unless you sign in there yourself.
* Drives the installed Microsoft Edge by default (no extra download).
* Only http(s) pages. Downloads are disabled.
* Closed automatically after BROWSER_IDLE_MINUTES without use.
* Page elements are numbered by ``snapshot()``; clicks and typing refer to those
  numbers, so every action targets something the assistant has actually seen.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.tools.base import ToolError
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("browser")
MAX_TEXT = 3000
MAX_ELEMENTS = 60
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000

_SNAPSHOT_JS = r"""
(max) => {
  const out = [];
  document.querySelectorAll('[data-jarvis-id]').forEach(e => e.removeAttribute('data-jarvis-id'));
  const nodes = document.querySelectorAll(
    'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=searchbox], [contenteditable=true]');
  let n = 0;
  for (const el of nodes) {
    if (out.length >= max) break;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || style.visibility === 'hidden' || style.display === 'none') continue;
    n += 1;
    el.setAttribute('data-jarvis-id', String(n));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let name = el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder')
               || el.getAttribute('title') || el.getAttribute('name') || el.value || '';
    if (tag === 'input' && ['text','search','email','url','tel','number','password',''].includes(type)) {
      name = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || 'text field';
    }
    out.push({
      n, tag, type, role: el.getAttribute('role') || '',
      name: name.replace(/\s+/g, ' ').trim().slice(0, 80),
      href: tag === 'a' ? el.href : '',
      download: el.hasAttribute('download'),
      in_form: !!el.closest('form'),
      field: el.getAttribute('name') || el.id || '',
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
    });
  }
  const text = (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  return {elements: out, text};
}
"""


@dataclass(frozen=True)
class Element:
    n: int
    tag: str
    type: str
    role: str
    name: str
    href: str = ""
    download: bool = False
    in_form: bool = False
    field: str = ""
    autocomplete: str = ""

    @property
    def kind(self) -> str:
        if self.tag == "a" or self.role == "link":
            return "link"
        if self.tag in ("input", "textarea") and self.type not in ("submit", "button", "checkbox", "radio", "image"):
            return "field"
        if self.tag == "select":
            return "dropdown"
        if self.type in ("checkbox", "radio"):
            return self.type
        return "button"

    @property
    def is_submit(self) -> bool:
        return self.type in ("submit", "image") or (self.tag == "button" and self.in_form and self.type in ("", "submit"))

    def describe(self) -> str:
        return f"[{self.n}] {self.kind}: {self.name or '(no label)'}"


@dataclass
class Snapshot:
    title: str
    url: str
    text: str
    elements: list[Element] = field(default_factory=list)


def check_url(url: str) -> str:
    """Only http(s). 'github.com' -> https://github.com, 'localhost:3000' -> http://..., and
    'javascript:', 'file:', 'data:' ... are refused (never mistaken for a host name)."""
    text = url.strip()
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):(?!\d)", text, re.I)
    if scheme is None:
        local = re.match(r"^(localhost|127\.0\.0\.1)(:\d+)?(/|$)", text, re.I)
        text = ("http://" if local else "https://") + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("Only http and https web addresses can be opened.", "invalid_url")
    return text


class BrowserSession:
    def __init__(
        self,
        profile_dir: Path,
        *,
        channel: str = "msedge",
        headless: bool = False,
        idle_minutes: float = 10.0,
        on_active_change: Callable[[bool], None] | None = None,
        executable_path: str | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self.headless = headless
        self.idle_seconds = idle_minutes * 60
        self.on_active_change = on_active_change
        self.executable_path = executable_path
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._last_used = 0.0
        self._idle_task: asyncio.Task | None = None
        self.elements: dict[int, Element] = {}  # from the last snapshot
        self.elements_url: str | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._context is not None

    def _notify(self, active: bool) -> None:
        if self.on_active_change is not None:
            try:
                self.on_active_change(active)
            except Exception:
                _log.warning("browser indicator callback failed", exc_info=True)

    def _guard(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # Playwright objects belong to the loop that created them
            self._pw = self._context = self._page = None
            self._loop, self._lock = loop, asyncio.Lock()
        return self._lock  # type: ignore[return-value]

    async def _start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ToolError("Browser automation is not installed. Run 'pip install -r requirements.txt'.",
                            "missing_dependency") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        options: dict[str, Any] = dict(headless=self.headless, accept_downloads=False, viewport=None,
                                       args=["--no-first-run", "--no-default-browser-check"])
        attempts: list[dict[str, Any]] = []
        if self.executable_path:
            attempts.append({"executable_path": self.executable_path})
        if self.channel in ("msedge", "chrome"):
            attempts.append({"channel": self.channel})
        attempts.append({})  # Playwright's own Chromium, if installed
        errors = []
        for extra in attempts:
            try:
                self._context = await self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), **options, **extra)
                break
            except Exception as exc:
                errors.append(str(exc).splitlines()[0][:200])
        if self._context is None:
            await self._pw.stop()
            self._pw = None
            _log.warning("browser launch failed: %s", errors)
            raise ToolError(
                "No browser could be started. Install Microsoft Edge or Google Chrome (and set BROWSER_CHANNEL), "
                "or run 'python -m playwright install chromium'.", "no_browser")
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self._context.on("close", lambda *_: self._closed())
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        log_event(_log, "browser_started", channel=self.channel, headless=self.headless)
        self._notify(True)
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_watch())

    def _closed(self) -> None:
        was_running = self._context is not None
        self._context = self._page = None
        self.elements.clear()
        if was_running:
            self._notify(False)

    async def _idle_watch(self) -> None:
        while self._context is not None:
            await asyncio.sleep(min(30.0, self.idle_seconds))
            if self._context is not None and time.monotonic() - self._last_used > self.idle_seconds:
                log_event(_log, "browser_idle_closed")
                await self.close()

    async def page(self) -> Any:
        async with self._guard():
            self._last_used = time.monotonic()
            if self._context is None:
                await self._start()
            if self._page is None or self._page.is_closed():
                pages = [p for p in self._context.pages if not p.is_closed()]
                self._page = pages[-1] if pages else await self._context.new_page()
            return self._page

    async def close(self) -> None:
        context, pw = self._context, self._pw
        self._context = self._page = self._pw = None
        self.elements.clear()
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._idle_task = None
        try:
            if context is not None:
                await context.close()
            if pw is not None:
                await pw.stop()
        except Exception:
            _log.warning("closing the browser failed", exc_info=True)
        if context is not None:
            self._notify(False)
            log_event(_log, "browser_closed")

    # ----------------------------------------------------------------- pages

    async def goto(self, url: str) -> Snapshot:
        target = check_url(url)  # validate before starting a browser
        page = await self.page()
        try:
            await page.goto(target, wait_until="domcontentloaded")
        except Exception as exc:
            raise ToolError(f"The page could not be opened: {str(exc).splitlines()[0][:160]}", "navigation_failed") from exc
        return await self.snapshot()

    async def snapshot(self) -> Snapshot:
        page = await self.page()
        try:
            await page.wait_for_load_state("domcontentloaded")
            data = await page.evaluate(_SNAPSHOT_JS, MAX_ELEMENTS)
            title = await page.title()
        except Exception as exc:
            raise ToolError(f"The page could not be read: {str(exc).splitlines()[0][:160]}", "read_failed") from exc
        elements = [Element(**{k: item.get(k, "") if k not in ("download", "in_form") else bool(item.get(k))
                               for k in Element.__dataclass_fields__}) for item in data["elements"]]
        self.elements = {e.n: e for e in elements}
        self.elements_url = page.url
        text = data["text"]
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + " ...(more on the page)"
        return Snapshot(title=title, url=page.url, text=text, elements=elements)

    def element(self, number: int) -> Element:
        element = self.elements.get(number)
        if element is None:
            raise ToolError(f"There is no element [{number}] on the page as last read. Read the page again.",
                            "unknown_element")
        return element

    async def click(self, number: int) -> Snapshot:
        page = await self.page()
        self.element(number)
        locator = page.locator(f'[data-jarvis-id="{number}"]')
        if await locator.count() == 0:
            raise ToolError("The page changed since it was read. Read it again.", "stale")
        try:
            await locator.first.click()
            await page.wait_for_load_state("domcontentloaded")
        except Exception as exc:
            raise ToolError(f"Clicking failed: {str(exc).splitlines()[0][:160]}", "click_failed") from exc
        await asyncio.sleep(0.5)  # let client-side navigation settle
        return await self.snapshot()

    async def type_text(self, number: int, text: str, submit: bool) -> Snapshot:
        page = await self.page()
        self.element(number)
        locator = page.locator(f'[data-jarvis-id="{number}"]')
        if await locator.count() == 0:
            raise ToolError("The page changed since it was read. Read it again.", "stale")
        try:
            await locator.first.fill(text)
            if submit:
                await locator.first.press("Enter")
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(0.5)
        except Exception as exc:
            raise ToolError(f"Typing failed: {str(exc).splitlines()[0][:160]}", "type_failed") from exc
        return await self.snapshot()

    async def back(self) -> Snapshot:
        page = await self.page()
        await page.go_back(wait_until="domcontentloaded")
        return await self.snapshot()

    async def tabs(self) -> list[dict[str, Any]]:
        page = await self.page()
        result = []
        for index, tab in enumerate(p for p in self._context.pages if not p.is_closed()):
            result.append({"index": index + 1, "title": (await tab.title())[:80], "url": tab.url, "active": tab is page})
        return result

    async def switch_tab(self, index: int) -> Snapshot:
        await self.page()
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not 1 <= index <= len(pages):
            raise ToolError(f"There is no tab {index}; there are {len(pages)}.", "no_tab")
        self._page = pages[index - 1]
        await self._page.bring_to_front()
        return await self.snapshot()

    async def new_tab(self, url: str | None) -> Snapshot:
        await self.page()
        self._page = await self._context.new_page()
        if url:
            return await self.goto(url)
        return await self.snapshot()

    async def close_tab(self, index: int | None) -> int:
        page = await self.page()
        pages = [p for p in self._context.pages if not p.is_closed()]
        target = page if index is None else (pages[index - 1] if 1 <= index <= len(pages) else None)
        if target is None:
            raise ToolError(f"There is no tab {index}.", "no_tab")
        await target.close()
        remaining = [p for p in self._context.pages if not p.is_closed()]
        self._page = remaining[-1] if remaining else None
        self.elements.clear()
        return len(remaining)
