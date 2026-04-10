"""Persistent Playwright browser session management.

Uses chromium.launch_persistent_context() so the user logs in once
and cookies/session are reused across runs.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from automation.config import AutomationConfig

logger = logging.getLogger(__name__)


class BrowserSession:
    """Manages a persistent Playwright Chromium browser context."""

    def __init__(self, config: AutomationConfig):
        self.config = config
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def start(self) -> "BrowserSession":
        """Launch a persistent browser context. Reuses existing profile."""
        self.config.browser_data_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.config.browser_data_dir),
            headless=self.config.headless,
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        # Use the first page or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        logger.info(
            f"Browser started (headless={self.config.headless}, "
            f"profile={self.config.browser_data_dir})"
        )
        return self

    async def login_flow(self) -> None:
        """Open browser for manual login.

        Navigates to start_url, then waits for the user to complete login.
        The user presses Enter in the terminal when done.
        Session cookies are automatically persisted in the browser profile.
        """
        if not self._page:
            await self.start()

        url = self.config.start_url
        if url:
            logger.info(f"Navigating to {url} for login...")
            await self._page.goto(url, wait_until="domcontentloaded")
        else:
            logger.info("Browser opened. Navigate to the course login page.")

        print("\n" + "=" * 60)
        print("MANUAL LOGIN REQUIRED")
        print("=" * 60)
        print("1. Log in to the course platform in the browser window")
        print("2. Navigate to the course you want to capture")
        print("3. Press ENTER here when you are logged in and ready")
        print("=" * 60)

        # Wait for user confirmation (run in executor to not block async loop)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "Press ENTER when login is complete... ")

        current_url = self._page.url
        logger.info(f"Login flow complete. Current URL: {current_url}")
        print(f"\nSession saved to: {self.config.browser_data_dir}")
        print(f"Current URL: {current_url}")
        print("You can now run capture/run commands without logging in again.")

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """Navigate to a URL and wait for initial load."""
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")
        logger.debug(f"Navigating to {url}")
        await self._page.goto(url, wait_until=wait_until, timeout=60000)

    async def wait_for_stable_page(self, timeout_ms: Optional[int] = None) -> None:
        """Wait for page to be fully loaded and stable.

        Strategy:
        1. Wait for network idle (no pending requests for 500ms)
        2. Wait for DOM mutation quiescence (no changes for configured period)
        """
        if not self._page:
            return

        timeout = timeout_ms or self.config.stable_wait_ms
        quiet_ms = self.config.mutation_quiet_ms

        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            logger.debug("Network idle timeout — continuing with DOM check")

        # Inject MutationObserver to wait for DOM stability
        try:
            await self._page.evaluate(
                f"""() => new Promise(resolve => {{
                    let timer;
                    const observer = new MutationObserver(() => {{
                        clearTimeout(timer);
                        timer = setTimeout(() => {{
                            observer.disconnect();
                            resolve();
                        }}, {quiet_ms});
                    }});
                    observer.observe(document.body, {{
                        childList: true,
                        subtree: true,
                        attributes: true
                    }});
                    // Fallback: resolve after 2x quiet period even if mutations continue
                    timer = setTimeout(() => {{
                        observer.disconnect();
                        resolve();
                    }}, {quiet_ms * 2});
                }})""",
                timeout=timeout,
            )
        except Exception:
            logger.debug("DOM stability check timed out — proceeding anyway")

    async def get_page_height(self) -> int:
        """Get the full scrollable height of the page."""
        if not self._page:
            return 0
        return await self._page.evaluate(
            "() => document.documentElement.scrollHeight"
        )

    async def get_viewport_height(self) -> int:
        """Get the viewport height."""
        if not self._page:
            return 0
        return await self._page.evaluate("() => window.innerHeight")

    async def scroll_to(self, y: int) -> None:
        """Scroll to a specific Y position."""
        if self._page:
            await self._page.evaluate(f"window.scrollTo(0, {y})")
            await asyncio.sleep(0.3)  # brief pause for render

    async def get_current_url(self) -> str:
        """Get the current page URL."""
        return self._page.url if self._page else ""

    @property
    def page(self) -> Page:
        """Current active Playwright page."""
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    async def close(self) -> None:
        """Clean shutdown of browser and Playwright."""
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser session closed")
