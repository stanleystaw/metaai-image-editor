"""Playwright browser backend — pure Python, no Node.js needed.

This is an alternative to BrowserBackend that uses Playwright instead of
agent-browser. It's ideal for Google Colab and other environments where
Node.js isn't available or is too old.

Install:
    pip install metaai-sdk[playwright]
    playwright install chromium
    playwright install-deps  # Linux/Colab only
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .exceptions import BrowserNotInstalledError, ConnectionError
from .utils import DEFAULT_UA, is_media_url, logger

try:
    from playwright.sync_api import sync_playwright, Browser, Page
except ImportError:
    sync_playwright = None
    Browser = None
    Page = None


class PlaywrightBackend:
    """Browser automation backend using Playwright (pure Python).

    This is the recommended backend for Google Colab and environments
    where Node.js/agent-browser isn't available.
    """

    def __init__(self, cookies: dict, headed: bool = False,
                 session_name: str = "metaai", user_agent: str = DEFAULT_UA):
        self.cookies = cookies
        self.headed = headed
        self.session_name = session_name
        self.user_agent = user_agent
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._ready = False

    def _check_installed(self):
        if sync_playwright is None:
            raise BrowserNotInstalledError(
                "Playwright is not installed. Install it with:\n"
                "  pip install metaai-sdk[playwright]\n"
                "  playwright install chromium\n"
                "  playwright install-deps  # Linux/Colab only"
            )

    def setup(self) -> None:
        """Start browser and inject cookies."""
        self._check_installed()

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=not self.headed,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1280, "height": 720},
        )

        # Set cookies
        cookie_list = []
        for name, value in self.cookies.items():
            cookie_list.append({
                "name": name,
                "value": value,
                "domain": ".meta.ai",
                "path": "/",
            })
        context.add_cookies(cookie_list)

        self._page = context.new_page()
        self._page.goto("https://www.meta.ai/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        # Dismiss any cookie consent / overlay dialogs
        self._dismiss_overlays()

        # Reload to apply cookies
        self._page.reload(wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        self._dismiss_overlays()

        self._ready = True
        logger.info("Playwright backend ready")

    def _dismiss_overlays(self):
        """Dismiss cookie consent, dialogs, and overlays that might block the input."""
        try:
            # Try clicking "Connect" / "Dismiss" / "Accept" buttons
            for text in ["Dismiss", "Connect", "Accept all", "Accept", "Got it", "Close", "OK"]:
                try:
                    btn = self._page.query_selector(f'button:has-text("{text}")')
                    if btn and btn.is_visible():
                        btn.click()
                        time.sleep(1)
                        logger.info(f"Dismissed overlay: {text}")
                except Exception:
                    pass
        except Exception:
            pass

    def _collect_media_urls(self) -> Set[str]:
        """Collect media URLs already visible so old gallery images are ignored."""
        urls: Set[str] = set()
        try:
            elements = self._page.query_selector_all(
                'img[src], video[src], source[src], a[href]'
            )
            for el in elements:
                src = el.get_attribute("src") or el.get_attribute("href") or ""
                if src and ("fbcdn" in src or is_media_url(src)):
                    urls.add(src)
        except Exception:
            pass
        return urls

    def _submit_prompt(self, prompt: str) -> None:
        """Fill Meta AI's composer and submit it, tolerating UI revisions."""
        selectors = [
            'textarea[data-testid="composer-input"]',
            'div[data-testid="composer-input"] [contenteditable]',
            'textarea[placeholder*="Ask Meta"]',
            '[role="textbox"][contenteditable="true"]',
            '[role="textbox"]',
            'textarea',
        ]
        for selector in selectors:
            try:
                el = self._page.query_selector(selector)
                if not el or not el.is_visible():
                    continue
                el.scroll_into_view_if_needed(timeout=5000)
                el.click(timeout=5000)
                # fill() correctly triggers React input events and replaces stale text.
                try:
                    el.fill(prompt)
                except Exception:
                    self._page.keyboard.press("ControlOrMeta+A")
                    self._page.keyboard.type(prompt, delay=20)
                time.sleep(0.5)
                self._page.keyboard.press("Enter")
                logger.info("Submitted prompt using selector: %s", selector)
                return
            except Exception as exc:
                logger.debug("Composer selector %s failed: %s", selector, exc)

        raise ConnectionError("Could not find or operate the Meta AI chat input")

    def send_message(self, prompt: str, timeout: int = 120,
                     thinking_mode: bool = False) -> Dict[str, Any]:
        """Send a prompt and return media URLs + text response."""
        if not self._ready:
            self.setup()

        self._switch_mode("Thinking" if thinking_mode else "Instant")
        self._dismiss_overlays()
        # Capture before submitting. Capturing afterwards can classify a fast result
        # as an old image and cause an unnecessary timeout.
        existing_urls = self._collect_media_urls()
        self._submit_prompt(prompt)

        urls, text = self._wait_for_response(timeout, existing_urls)
        return {
            "urls": urls,
            "text": text,
            "conversation_id": self._get_conversation_id(),
        }

    def edit_image(self, image_path: str, prompt: str,
                   timeout: int = 180) -> Dict[str, Any]:
        """Upload a local image to meta.ai and edit it with a text instruction.

        This deliberately uses the visible product workflow instead of replaying
        private GraphQL frames. Selectors are broad because Meta frequently changes
        labels and test IDs.
        """
        if not self._ready:
            self.setup()

        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            raise ConnectionError(f"Input image does not exist: {path}")
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ConnectionError("Only PNG, JPEG and WebP images are supported")

        self.new_chat()
        self._dismiss_overlays()
        existing_urls = self._collect_media_urls()

        uploaded = False
        # Meta normally keeps a hidden file input in the composer.
        for selector in [
            'input[type="file"][accept*="image"]',
            'input[type="file"]',
        ]:
            try:
                inputs = self._page.query_selector_all(selector)
                for file_input in inputs:
                    try:
                        file_input.set_input_files(str(path), timeout=10000)
                        uploaded = True
                        logger.info("Uploaded edit source through %s", selector)
                        break
                    except Exception:
                        continue
                if uploaded:
                    break
            except Exception:
                continue

        # Some builds only create the input after the attachment button is clicked.
        if not uploaded:
            button_selectors = [
                'button[aria-label*="Upload" i]',
                'button[aria-label*="Attach" i]',
                'button[aria-label*="photo" i]',
                'button:has-text("Upload")',
                'button:has-text("Add photo")',
            ]
            for selector in button_selectors:
                try:
                    button = self._page.query_selector(selector)
                    if not button or not button.is_visible():
                        continue
                    with self._page.expect_file_chooser(timeout=5000) as chooser:
                        button.click()
                    chooser.value.set_files(str(path))
                    uploaded = True
                    logger.info("Uploaded edit source through button %s", selector)
                    break
                except Exception as exc:
                    logger.debug("Upload button %s failed: %s", selector, exc)

        if not uploaded:
            raise ConnectionError(
                "Could not find Meta AI's image upload control. "
                "The site UI may have changed or image editing is unavailable "
                "for this account/region."
            )

        # Wait briefly for the attachment preview and upload processing.
        time.sleep(3)
        self._submit_prompt(prompt)
        urls, text = self._wait_for_response(timeout, existing_urls)
        return {
            "urls": urls,
            "text": text,
            "conversation_id": self._get_conversation_id(),
            "source_file": path.name,
        }

    def _switch_mode(self, mode_name: str) -> None:
        """Switch between Instant and Thinking modes."""
        try:
            mode_button = self._page.query_selector('button:has-text("Instant"), button:has-text("Thinking")')
            if mode_button and mode_button.is_visible():
                mode_button.click()
                time.sleep(1)
                mode_item = self._page.query_selector(f'[role="menuitemcheckbox"]:has-text("{mode_name}")')
                if mode_item:
                    mode_item.click()
                    time.sleep(1)
        except Exception:
            pass

    def _wait_for_response(self, timeout: int, existing_urls: Optional[Set[str]] = None) -> Tuple[List[str], str]:
        """Wait for either media URLs or text response. Only collect NEW URLs."""
        urls: Set[str] = set()
        last_text = ""
        text_stable_count = 0
        start = time.time()
        existing = existing_urls or set()

        while time.time() - start < timeout:
            time.sleep(2)

            # Check for NEW media URLs only
            try:
                elements = self._page.query_selector_all('img[src], video[src], source[src], a[href]')
                for el in elements:
                    src = el.get_attribute("src") or el.get_attribute("href") or ""
                    if src and "fbcdn" in src and is_media_url(src) and src not in existing:
                        urls.add(src)
            except Exception:
                pass

            if urls:
                time.sleep(3)
                return sorted(urls), self._extract_text()

            # Check for text response
            try:
                text = self._page.evaluate("""
                    () => {
                        const msgs = document.querySelectorAll('[class*="assistant-message"]');
                        if (msgs.length === 0) return '';
                        return msgs[msgs.length - 1].textContent?.trim() || '';
                    }
                """)
                if text:
                    if text == last_text:
                        text_stable_count += 1
                        if text_stable_count >= 2:
                            return [], text
                    else:
                        text_stable_count = 0
                        last_text = text
            except Exception:
                pass

        return sorted(urls), last_text

    def _extract_text(self) -> str:
        try:
            return self._page.evaluate("""
                () => {
                    const msgs = document.querySelectorAll('[class*="assistant-message"]');
                    if (msgs.length === 0) return '';
                    return msgs[msgs.length - 1].textContent?.trim() || '';
                }
            """)
        except Exception:
            return ""

    def _get_conversation_id(self) -> str:
        try:
            url = self._page.url
            m = re.search(r'/prompt/([0-9a-f-]+)', url)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

    def list_conversations(self) -> List[dict]:
        """Get all conversations from the sidebar."""
        try:
            convs = self._page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a[href*="/prompt/"]'));
                    return links.map(a => ({
                        title: a.textContent?.trim(),
                        url: a.href
                    })).filter(c => c.title);
                }
            """)
            result = []
            for c in convs:
                if isinstance(c, dict):
                    result.append({
                        "id": c.get("url", "").split("/prompt/")[-1] if "/prompt/" in c.get("url", "") else "",
                        "title": c.get("title", ""),
                        "url": c.get("url", ""),
                    })
            return result
        except Exception:
            return []

    def use_as_reference(self, image_url: str, prompt: str, timeout: int = 120) -> Dict[str, Any]:
        """Use an existing image as a reference for a new generation."""
        try:
            ref_button = self._page.query_selector('button[aria-label="Use as reference"]')
            if ref_button:
                ref_button.click()
                time.sleep(2)

            self._dismiss_overlays()
            for selector in ['textarea[data-testid="composer-input"]', '[role="textbox"]', 'textarea']:
                try:
                    el = self._page.query_selector(selector)
                    if el:
                        el.click(timeout=5000)
                        self._page.keyboard.type(prompt, delay=30)
                        time.sleep(0.5)
                        self._page.keyboard.press("Enter")
                        break
                except Exception:
                    continue

            urls, text = self._wait_for_response(timeout)
            return {"urls": urls, "text": text, "conversation_id": self._get_conversation_id()}
        except Exception as e:
            return {"urls": [], "text": "", "conversation_id": "", "error": str(e)}

    def new_chat(self) -> None:
        """Start a new chat conversation."""
        try:
            link = self._page.query_selector('a:has-text("New chat")')
            if link:
                link.click()
                time.sleep(2)
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._page:
                self._page.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._ready = False
