"""Course navigation: structure discovery, page traversal, content expansion.

Navigation fallback strategy (ordered):
  1. Explicit page URL from manifest (fastest for resume)
  2. Sidebar click by lesson/page label (if DOM is parseable)
  3. "Next" button following (universal fallback)
"""

import logging
import re
from typing import List, Optional, Set, Tuple

from playwright.async_api import Page

from automation.capture.browser import BrowserSession
from automation.selectors import SelectorProfile
from automation.state.manifest import PageInfo

logger = logging.getLogger(__name__)


class CourseNavigator:
    """Discovers and traverses course structure from the DOM."""

    def __init__(self, session: BrowserSession, selectors: SelectorProfile):
        self.session = session
        self.selectors = selectors
        self._visited_urls: Set[str] = set()
        self._page_counter: int = 0  # global page counter for sequential discovery
        self._current_module_index: int = 1
        self._current_lesson_index: int = 1
        self._current_page_index: int = 0
        # Loop protection: track (url, title) fingerprint
        self._prev_fingerprint: Optional[tuple] = None
        self._consecutive_same_fingerprint: int = 0
        self._LOOP_THRESHOLD: int = 3

    async def discover_structure_sequential(self) -> List[PageInfo]:
        """Discover pages by following Next buttons from current position.

        This is the most robust strategy — works regardless of course
        platform DOM structure.  Returns all discovered PageInfo objects.
        """
        pages: List[PageInfo] = []
        page = self.session.page

        while True:
            await self.session.wait_for_stable_page()
            current_url = await self.session.get_current_url()

            # Loop detection
            if current_url in self._visited_urls:
                logger.warning(f"Loop detected: already visited {current_url}")
                break

            self._visited_urls.add(current_url)
            self._current_page_index += 1
            self._page_counter += 1

            info = await self.get_current_page_info()
            pages.append(info)
            logger.info(
                f"Discovered page {self._page_counter}: "
                f"[{info.page_id}] {info.page_title}"
            )

            if not await self.has_next():
                logger.info("No more pages — reached end of course/lesson")
                break

            # Don't actually navigate during discovery-only mode
            # The caller decides whether to advance
            break  # return one page at a time for streaming discovery

        return pages

    async def get_current_page_info(self) -> PageInfo:
        """Extract page metadata from the currently loaded DOM."""
        page = self.session.page
        url = page.url

        page_title = await self._extract_text(page, self.selectors.get("page_title"))
        if not page_title:
            page_title = await page.title() or f"Page {self._page_counter}"

        # Try to extract module/lesson names from DOM
        module_name = await self._extract_module_name(page)
        lesson_name = await self._extract_lesson_name(page)

        return PageInfo(
            url=url,
            module_name=module_name,
            module_index=self._current_module_index,
            lesson_name=lesson_name,
            lesson_index=self._current_lesson_index,
            page_title=page_title.strip(),
            page_index=self._current_page_index,
        )

    async def navigate_to_page(self, page_info: PageInfo) -> None:
        """Navigate to a specific page using fallback strategy.

        Order:
          1. Direct URL navigation (if URL is stored)
          2. Sidebar click (if lesson label is findable)
          3. Sequential Next-clicking (last resort)
        """
        # Strategy 1: Direct URL
        if page_info.url:
            logger.debug(f"Navigating to {page_info.url} (direct URL)")
            await self.session.navigate(page_info.url)
            await self.session.wait_for_stable_page()
            self._visited_urls.add(page_info.url)
            self._current_module_index = page_info.module_index
            self._current_lesson_index = page_info.lesson_index
            self._current_page_index = page_info.page_index
            return

        # Strategy 2: Try sidebar click
        if await self._try_sidebar_navigation(page_info):
            return

        # Strategy 3: Sequential Next clicking to reach target
        logger.warning(
            f"No direct URL for {page_info.page_id}, "
            "falling back to sequential navigation"
        )

    async def go_next(self) -> Optional[PageInfo]:
        """Click the Next button, wait for load, return new PageInfo.

        Returns None if no Next button or at end of course.
        """
        page = self.session.page
        next_sel = self.selectors.get("next_button")

        for selector in self.selectors.get_chain("next_button"):
            try:
                button = await page.query_selector(selector)
                if button:
                    is_disabled = await button.get_attribute("disabled")
                    aria_disabled = await button.get_attribute("aria-disabled")
                    if is_disabled is not None or aria_disabled == "true":
                        continue

                    old_url = page.url
                    await button.click()
                    # Wait for navigation or content change
                    try:
                        await page.wait_for_url(
                            lambda url: url != old_url, timeout=10000
                        )
                    except Exception:
                        # URL might not change (SPA) — wait for DOM change instead
                        await self.session.wait_for_stable_page()

                    new_url = await self.session.get_current_url()

                    # Loop detection
                    if new_url in self._visited_urls and new_url == old_url:
                        logger.warning("Next click did not change the page")
                        return None

                    self._visited_urls.add(new_url)
                    self._current_page_index += 1
                    self._page_counter += 1

                    info = await self.get_current_page_info()

                    # Loop protection: check fingerprint
                    fingerprint = (new_url, info.page_title)
                    if fingerprint == self._prev_fingerprint:
                        self._consecutive_same_fingerprint += 1
                        if self._consecutive_same_fingerprint >= self._LOOP_THRESHOLD:
                            logger.warning(
                                "Loop detected: same page repeated %d times after Next "
                                "(url=%s, title=%s)",
                                self._consecutive_same_fingerprint,
                                new_url,
                                info.page_title,
                            )
                            return None
                    else:
                        self._consecutive_same_fingerprint = 0
                    self._prev_fingerprint = fingerprint

                    logger.info(f"Navigated to: [{info.page_id}] {info.page_title}")
                    return info
            except Exception as e:
                logger.debug(f"Next button selector '{selector}' failed: {e}")
                continue

        logger.info("No active Next button found")
        return None

    async def has_next(self) -> bool:
        """Check if a Next button exists and is enabled."""
        page = self.session.page
        for selector in self.selectors.get_chain("next_button"):
            try:
                button = await page.query_selector(selector)
                if button:
                    is_disabled = await button.get_attribute("disabled")
                    aria_disabled = await button.get_attribute("aria-disabled")
                    if is_disabled is None and aria_disabled != "true":
                        return True
            except Exception:
                continue
        return False

    async def expand_all_content(self) -> int:
        """Click all accordions, tabs, expandable sections to reveal hidden content.

        Returns the number of elements expanded.
        """
        page = self.session.page
        expanded = 0

        # Expand closed accordions
        for selector in self.selectors.get_chain("accordion_closed"):
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    try:
                        await el.click()
                        expanded += 1
                    except Exception:
                        pass
            except Exception:
                continue

        # Click inactive tabs to load their content
        for selector in self.selectors.get_chain("tab_inactive"):
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    try:
                        await el.click()
                        await page.wait_for_timeout(300)
                        expanded += 1
                    except Exception:
                        pass
            except Exception:
                continue

        if expanded:
            logger.debug(f"Expanded {expanded} hidden content sections")
            await self.session.wait_for_stable_page()

        return expanded

    async def detect_module_change(self, prev_info: Optional[PageInfo]) -> bool:
        """Detect if we've moved to a new module (for index tracking)."""
        if prev_info is None:
            return False
        current_module = await self._extract_module_name(self.session.page)
        if current_module and current_module != prev_info.module_name:
            self._current_module_index += 1
            self._current_lesson_index = 1
            self._current_page_index = 0
            return True
        return False

    async def detect_lesson_change(self, prev_info: Optional[PageInfo]) -> bool:
        """Detect if we've moved to a new lesson."""
        if prev_info is None:
            return False
        current_lesson = await self._extract_lesson_name(self.session.page)
        if current_lesson and current_lesson != prev_info.lesson_name:
            self._current_lesson_index += 1
            self._current_page_index = 0
            return True
        return False

    def set_position(self, module_index: int, lesson_index: int, page_index: int) -> None:
        """Set the navigator's internal position (for resume)."""
        self._current_module_index = module_index
        self._current_lesson_index = lesson_index
        self._current_page_index = page_index

    def mark_url_visited(self, url: str) -> None:
        """Register a URL as already visited (for loop detection on resume)."""
        self._visited_urls.add(url)

    def reset(self) -> None:
        """Reset navigator state for reuse across courses."""
        self._visited_urls.clear()
        self._page_counter = 0
        self._current_module_index = 1
        self._current_lesson_index = 1
        self._current_page_index = 0
        self._prev_fingerprint = None
        self._consecutive_same_fingerprint = 0

    async def is_skip_page(self, skip_titles: List[str]) -> bool:
        """Multi-source skip detection for the current page.

        Checks multiple DOM sources for titles matching skip_titles:
          1. Page title selector (existing)
          2. Chapter/item title selector
          3. Chapter root title selector
          4. Visible <h1> text

        Returns True if ANY source contains a skip_titles entry
        (case-insensitive substring match).
        """
        if not skip_titles:
            return False

        page = self.session.page
        sources: List[str] = []

        # Source 1: page_title selector
        text = await self._extract_text(page, self.selectors.get("page_title"))
        if text:
            sources.append(text)

        # Source 2: chapter_item_title selector
        text = await self._extract_text(page, self.selectors.get("chapter_item_title"))
        if text:
            sources.append(text)

        # Source 3: chapter_root_title selector
        text = await self._extract_text(page, self.selectors.get("chapter_root_title"))
        if text:
            sources.append(text)

        # Source 4: visible <h1> as fallback
        try:
            h1 = await page.query_selector("h1")
            if h1:
                h1_text = await h1.inner_text()
                if h1_text and h1_text.strip():
                    sources.append(h1_text.strip())
        except Exception:
            pass

        # Check all sources against all skip titles
        for source in sources:
            source_lower = source.lower()
            for skip in skip_titles:
                if skip.lower() in source_lower:
                    logger.info(
                        "Skip page detected: title '%s' matches skip rule '%s'",
                        source,
                        skip,
                    )
                    return True

        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _extract_text(self, page: Page, selector_chain: str) -> str:
        """Try each selector in a chain, return first non-empty text."""
        if not selector_chain:
            return ""
        for sel in [s.strip() for s in selector_chain.split(",")]:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    text = text.strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    async def _extract_module_name(self, page: Page) -> str:
        """Try to extract current module name from DOM."""
        name = await self._extract_text(page, self.selectors.get("module_name"))
        if name:
            return name

        # Fallback: look for breadcrumbs or active sidebar item
        try:
            breadcrumb = await page.query_selector(
                "nav[aria-label='breadcrumb'] li:nth-child(2), "
                ".breadcrumb li:nth-child(2)"
            )
            if breadcrumb:
                return (await breadcrumb.inner_text()).strip()
        except Exception:
            pass

        return f"Module {self._current_module_index}"

    async def _extract_lesson_name(self, page: Page) -> str:
        """Try to extract current lesson name from DOM."""
        name = await self._extract_text(page, self.selectors.get("lesson_name"))
        if name:
            return name

        # Fallback: look for breadcrumbs
        try:
            breadcrumb = await page.query_selector(
                "nav[aria-label='breadcrumb'] li:nth-child(3), "
                ".breadcrumb li:nth-child(3)"
            )
            if breadcrumb:
                return (await breadcrumb.inner_text()).strip()
        except Exception:
            pass

        return f"Lesson {self._current_lesson_index}"

    async def _try_sidebar_navigation(self, page_info: PageInfo) -> bool:
        """Try to navigate by clicking a matching lesson in the sidebar."""
        page = self.session.page
        lesson_chain = self.selectors.get("lesson_item")
        if not lesson_chain:
            return False

        for selector in [s.strip() for s in lesson_chain.split(",")]:
            try:
                items = await page.query_selector_all(selector)
                for item in items:
                    text = await item.inner_text()
                    if (
                        page_info.lesson_name.lower() in text.lower()
                        or page_info.page_title.lower() in text.lower()
                    ):
                        await item.click()
                        await self.session.wait_for_stable_page()
                        self._visited_urls.add(page.url)
                        self._current_module_index = page_info.module_index
                        self._current_lesson_index = page_info.lesson_index
                        self._current_page_index = page_info.page_index
                        logger.info(f"Sidebar navigation to: {page_info.page_id}")
                        return True
            except Exception:
                continue
        return False

    async def extract_dom_text(self) -> str:
        """Extract visible text from the main content area for fingerprinting."""
        page = self.session.page
        for selector in self.selectors.get_chain("main_content"):
            try:
                el = await page.query_selector(selector)
                if el:
                    text = await el.inner_text()
                    if text and len(text.strip()) > 10:
                        return text.strip()
            except Exception:
                continue

        # Fallback: body text
        try:
            return (await page.inner_text("body")).strip()[:5000]
        except Exception:
            return ""
