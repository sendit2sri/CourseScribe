"""Portal navigation: Pathways, course selection, launch, and exit.

Handles all platform-specific navigation that occurs OUTSIDE of the
actual course page traversal.  Uses Playwright Locators (not raw
ElementHandles) for built-in auto-waiting, retries, and visibility checks.

Navigation flow:
  1. navigate_to_pathways()    -> click Pathways box on landing page
  2. select_pathway(name)      -> click correct pathway tab
  3. expand_course_section()   -> expand toggle within the selected pathway
  4. find_course_link(name)    -> return Locator for a specific course link
  5. open_course_link(name)    -> click link, new tab opens
  6. launch_course()           -> Open Curriculum -> Launch -> Fullscreen -> dismiss resume
  7. detect_content_frame()    -> check for iframe, return Frame or Page
  8. exit_course()             -> click Exit Course button
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Union

from playwright.async_api import Frame, Locator, Page

from automation.capture.browser import BrowserSession
from automation.config import TargetsConfig
from automation.selectors import SelectorProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NavigationError(Exception):
    """Raised when portal navigation fails."""


class CourseLaunchError(NavigationError):
    """Raised when the course launch sequence fails."""


# ---------------------------------------------------------------------------
# Launch result
# ---------------------------------------------------------------------------

@dataclass
class LaunchResult:
    """Structured metadata from the course launch sequence."""

    resume_prompt_appeared: bool = False
    fullscreen_succeeded: bool = False
    iframe_detected: bool = False
    content_frame: Any = None  # Frame or Page
    final_page_title: str = ""


# ---------------------------------------------------------------------------
# PortalNavigator
# ---------------------------------------------------------------------------

class PortalNavigator:
    """Navigates the learning portal: pathways, course selection, launch."""

    def __init__(
        self,
        session: BrowserSession,
        selectors: SelectorProfile,
        targets: TargetsConfig,
    ):
        self.session = session
        self.selectors = selectors
        self.targets = targets
        # Set after select_pathway(); used to scope expand/find operations
        self._pathway_container: Optional[Locator] = None

    # ------------------------------------------------------------------
    # Step 1: Navigate to Pathways
    # ------------------------------------------------------------------

    async def navigate_to_pathways(self) -> None:
        """Click the Pathways box on the portal landing page."""
        page = self.session.page
        logger.info("Looking for Pathways box...")

        # Try each selector in the chain
        for selector in self.selectors.get_chain("pathways_box"):
            locator = page.locator(selector)
            if await locator.count() > 0:
                await locator.first.click()
                await self.session.wait_for_stable_page()
                logger.info("Clicked Pathways box")
                return

        # Fallback: broad text search
        pathways_text = page.get_by_text("Pathways", exact=True)
        if await pathways_text.count() > 0:
            await pathways_text.first.click()
            await self.session.wait_for_stable_page()
            logger.info("Clicked Pathways via text match")
            return

        raise NavigationError("Pathways box not found on the portal page")

    # ------------------------------------------------------------------
    # Step 2: Select Pathway tab
    # ------------------------------------------------------------------

    async def select_pathway(self, pathway_name: str) -> None:
        """Find and click the pathway tab matching the given name.

        Sets self._pathway_container to scope subsequent operations.
        """
        page = self.session.page
        logger.info("Looking for pathway tab: %s", pathway_name)

        # Strategy 1: Search elements matching the tab prefix selectors
        for selector in self.selectors.get_chain("pathway_tab_prefix"):
            elements = page.locator(selector)
            count = await elements.count()
            for i in range(count):
                el = elements.nth(i)
                # Check title attribute
                title = await el.get_attribute("title") or ""
                text = await el.inner_text()
                if (
                    pathway_name.lower() in title.lower()
                    or pathway_name.lower() in text.lower()
                ):
                    await el.click()
                    await self.session.wait_for_stable_page()
                    logger.info("Selected pathway tab: %s", pathway_name)
                    # Try to identify the pathway container for scoped operations
                    self._pathway_container = self._find_pathway_container(page)
                    return

        # Strategy 2: broad text match
        tab = page.get_by_text(pathway_name, exact=False)
        if await tab.count() > 0:
            await tab.first.click()
            await self.session.wait_for_stable_page()
            logger.info("Selected pathway via text match: %s", pathway_name)
            self._pathway_container = self._find_pathway_container(page)
            return

        raise NavigationError(f"Pathway tab not found: {pathway_name}")

    def _find_pathway_container(self, page: Page) -> Optional[Locator]:
        """Try to identify the container element for the selected pathway.

        Returns a Locator scoped to the pathway section, or None if
        we cannot determine it (falls back to whole-page search).
        """
        # The pathway course table is the most reliable anchor
        table_sel = self.selectors.get("pathway_course_table")
        if table_sel:
            locator = page.locator(table_sel)
            # Return the parent scope — we'll search within the page but
            # use the table locator to confirm we are in the right area
            return locator
        return None

    # ------------------------------------------------------------------
    # Step 3: Expand course section (scoped)
    # ------------------------------------------------------------------

    async def expand_course_section(self) -> None:
        """Expand the course listing within the selected pathway section.

        Scoped: identifies the toggle within the pathway container.
        Only clicks if currently collapsed (angle-down visible).
        """
        page = self.session.page
        logger.info("Expanding course section...")

        for selector in self.selectors.get_chain("pathway_dropdown_toggle"):
            toggles = page.locator(selector)
            count = await toggles.count()
            for i in range(count):
                toggle = toggles.nth(i)
                if not await toggle.is_visible():
                    continue

                # Detect state: angle-down = collapsed, angle-up = expanded
                classes = await toggle.get_attribute("class") or ""
                if "fa-angle-down" in classes:
                    # Collapsed — click to expand
                    await toggle.click()
                    await self.session.wait_for_stable_page()
                    logger.info("Expanded collapsed course section")
                    return
                elif "fa-angle-up" in classes:
                    # Already expanded
                    logger.info("Course section already expanded")
                    return

        # If we got here, try clicking whatever toggle is visible
        for selector in self.selectors.get_chain("pathway_dropdown_toggle"):
            toggle = page.locator(selector).first
            if await toggle.count() > 0 and await toggle.is_visible():
                await toggle.click()
                await self.session.wait_for_stable_page()
                logger.info("Clicked course section toggle (state unknown)")
                return

        # No toggle found — the section might already be visible
        table_sel = self.selectors.get("pathway_course_table")
        if table_sel:
            table = page.locator(table_sel).first
            if await table.count() > 0 and await table.is_visible():
                logger.info("Course table already visible, no toggle needed")
                return

        logger.warning("Could not find or expand course section toggle")

    # ------------------------------------------------------------------
    # Step 4 & 5: Find and open course link
    # ------------------------------------------------------------------

    async def find_course_link(self, course_name: str) -> Locator:
        """Return a Locator for the course link matching the given name.

        Searches within pathway course tables by the link's title attribute.
        Does NOT click.

        Raises:
            NavigationError: If the course link is not found.
        """
        page = self.session.page
        table_sel = self.selectors.get("pathway_course_table")

        if table_sel:
            # Search for <a> tags within pathway tables
            tables = page.locator(table_sel)
            count = await tables.count()
            for i in range(count):
                table = tables.nth(i)
                if not await table.is_visible():
                    continue

                # Find link by title attribute
                link = table.locator(f'a[title*="{course_name}"]')
                if await link.count() > 0:
                    logger.info("Found course link: %s", course_name)
                    return link.first

                # Fallback: search by link text
                links = table.locator("a")
                link_count = await links.count()
                for j in range(link_count):
                    a = links.nth(j)
                    text = (await a.inner_text()).strip()
                    if course_name.lower() in text.lower():
                        logger.info("Found course link by text: %s", course_name)
                        return a

        # Broader fallback: search entire page
        link = page.locator(f'a[title*="{course_name}"]')
        if await link.count() > 0:
            logger.info("Found course link (broad search): %s", course_name)
            return link.first

        # Log available courses for debugging
        await self._log_available_courses(page)
        raise NavigationError(f"Course link not found: {course_name}")

    async def open_course_link(self, course_name: str) -> None:
        """Find the course link and click it, opening a new tab.

        The caller should use session.click_and_wait_for_new_tab() to
        capture the new tab.  This method just finds the link and triggers
        the click inside the new-tab expectation.
        """
        link = await self.find_course_link(course_name)

        async def _click():
            await link.click()

        await self.session.click_and_wait_for_new_tab(_click)
        logger.info("Opened course in new tab: %s", course_name)

    async def _log_available_courses(self, page: Page) -> None:
        """Log available course links for debugging when a course is not found."""
        table_sel = self.selectors.get("pathway_course_table")
        if not table_sel:
            return

        tables = page.locator(table_sel)
        count = await tables.count()
        for i in range(count):
            table = tables.nth(i)
            links = table.locator("a")
            link_count = await links.count()
            if link_count > 0:
                logger.debug("Available courses in table %d:", i)
                for j in range(link_count):
                    text = (await links.nth(j).inner_text()).strip()
                    if text:
                        logger.debug("  - %s", text)

    # ------------------------------------------------------------------
    # Step 6: Launch course
    # ------------------------------------------------------------------

    async def launch_course(self) -> LaunchResult:
        """Execute the full course launch sequence in the current tab.

        Steps:
          1. Click "Open Curriculum"
          2. Click "Launch"
          3. Click fullscreen button
          4. Dismiss resume prompt if present (optional)
          5. Detect content iframe

        Returns:
            LaunchResult with metadata about the launch.
        """
        result = LaunchResult()
        page = self.session.page

        # Step 1: Open Curriculum
        await self._wait_and_click(
            self.selectors.get("open_curriculum_button"),
            "Open Curriculum",
            timeout_ms=30000,
        )

        # Step 2: Launch
        await self._wait_and_click(
            self.selectors.get("launch_button"),
            "Launch",
            timeout_ms=30000,
        )

        # Step 3: Fullscreen
        result.fullscreen_succeeded = await self._wait_and_click(
            self.selectors.get("fullscreen_button"),
            "Fullscreen",
            timeout_ms=15000,
            optional=True,
        )

        # Step 4: Dismiss resume prompt (optional, may not appear)
        resume_dismissed = await self._wait_and_click(
            self.selectors.get("dismiss_resume_no"),
            "Dismiss resume prompt (No)",
            timeout_ms=5000,
            optional=True,
        )
        result.resume_prompt_appeared = resume_dismissed

        # Step 5: Detect content iframe
        content = await self.detect_content_frame()
        if isinstance(content, Frame):
            result.iframe_detected = True
            result.content_frame = content
            logger.info("Course content detected in iframe")
        else:
            result.content_frame = content

        # Capture final page title
        try:
            result.final_page_title = await page.title()
        except Exception:
            result.final_page_title = ""

        logger.info(
            "Course launched (fullscreen=%s, resume_prompt=%s, iframe=%s)",
            result.fullscreen_succeeded,
            result.resume_prompt_appeared,
            result.iframe_detected,
        )
        return result

    # ------------------------------------------------------------------
    # Step 7: Detect content frame
    # ------------------------------------------------------------------

    async def detect_content_frame(self) -> Union[Frame, Page]:
        """Check if course content lives in an iframe.

        Returns the Frame if found, otherwise the current Page.
        """
        page = self.session.page

        for selector in self.selectors.get_chain("content_iframe"):
            try:
                iframe_locator = page.locator(selector)
                if await iframe_locator.count() > 0 and await iframe_locator.first.is_visible():
                    frame_el = await iframe_locator.first.element_handle()
                    if frame_el:
                        frame = await frame_el.content_frame()
                        if frame:
                            logger.info("Content iframe detected via selector: %s", selector)
                            return frame
            except Exception as e:
                logger.debug("Iframe check failed for '%s': %s", selector, e)

        # Check all frames for content indicators
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                url = frame.url
                if url and ("course" in url.lower() or "content" in url.lower()):
                    logger.info("Content iframe detected via URL: %s", url)
                    return frame
            except Exception:
                continue

        return page

    # ------------------------------------------------------------------
    # Step 8: Exit course
    # ------------------------------------------------------------------

    async def exit_course(self) -> None:
        """Click the Exit Course button at the end of a course."""
        clicked = await self._wait_and_click(
            self.selectors.get("exit_course_button"),
            "Exit Course",
            timeout_ms=10000,
            optional=True,
        )
        if clicked:
            logger.info("Clicked Exit Course")
        else:
            logger.info("Exit Course button not found (course may have already ended)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_and_click(
        self,
        selector_chain: str,
        description: str,
        timeout_ms: int = 15000,
        optional: bool = False,
    ) -> bool:
        """Wait for an element to appear and click it.

        Tries each selector in the chain. Uses Locator with auto-waiting.

        Args:
            selector_chain: Comma-separated CSS selectors.
            description: Human-readable step name for logging.
            timeout_ms: Max time to wait for the element.
            optional: If True, return False instead of raising on timeout.

        Returns:
            True if clicked, False if not found (only when optional=True).

        Raises:
            CourseLaunchError: If not optional and element is not found.
        """
        page = self.session.page
        selectors = [s.strip() for s in selector_chain.split(",") if s.strip()]

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=timeout_ms)
                await locator.click()
                await self.session.wait_for_stable_page()
                logger.info("Clicked: %s (via %s)", description, selector)
                return True
            except Exception:
                continue

        if optional:
            logger.debug("%s not found (optional, skipping)", description)
            return False

        raise CourseLaunchError(
            f"{description} button not found after {timeout_ms}ms. "
            f"Tried selectors: {selectors}"
        )
