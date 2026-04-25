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
    old_version_redirected: bool = False
    old_version_url: str = ""
    course_already_completed: bool = False


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

        # If we're already on the Pathways page (category tabs visible), skip
        for selector in self.selectors.get_chain("pathway_tab_prefix"):
            if await page.locator(selector).count() > 0:
                logger.info("Already on Pathways page (category tabs detected), skipping navigation")
                self.session.pathways_landing_url = page.url
                return

        # Portal widgets render dynamically — retry for up to ~10 seconds
        for attempt in range(10):
            # Try each selector in the chain
            for selector in self.selectors.get_chain("pathways_box"):
                locator = page.locator(selector)
                if await locator.count() > 0:
                    await self._click_pathways_link(locator.first)
                    self.session.pathways_landing_url = self.session.page.url
                    return

            # Fallback: find all "Pathways" text matches, click first visible one
            pathways_all = page.get_by_text("Pathways", exact=True)
            count = await pathways_all.count()
            for i in range(count):
                el = pathways_all.nth(i)
                if await el.is_visible():
                    await self._click_pathways_link(el)
                    self.session.pathways_landing_url = self.session.page.url
                    return

            logger.debug("Pathways box not yet visible (attempt %d/10)", attempt + 1)
            await asyncio.sleep(1)

        raise NavigationError("Pathways box not found on the portal page")

    async def return_to_pathways_landing(self) -> None:
        """Navigate the portal page back to the cached Pathways landing URL.

        Used between pathways in a multi-pathway run-all. Falls back to
        full navigate_to_pathways() if no URL was cached.
        """
        url = getattr(self.session, "pathways_landing_url", None)
        if url:
            logger.info("Returning to pathways landing: %s", url)
            await self.session.page.goto(url)
            await self.session.wait_for_stable_page()
            return
        await self.navigate_to_pathways()

    async def _click_pathways_link(self, locator: Locator) -> None:
        """Click the Pathways element, handling new-tab or same-page navigation."""
        page_count_before = len(self.session._context.pages)
        try:
            new_page = await self.session.click_and_wait_for_new_tab(
                locator.click, timeout_ms=5000
            )
            logger.info("Pathways opened in new tab: %s", new_page.url)
        except Exception:
            # Didn't open a new tab — stayed on same page
            await self.session.wait_for_stable_page()
            logger.info("Clicked Pathways box (same page)")

    # ------------------------------------------------------------------
    # Step 2a: Select category tab (e.g. "Transact - Core Banking")
    # ------------------------------------------------------------------

    async def select_category_tab(self, category: str) -> None:
        """Click the category tab in the scslider (e.g. 'Transact - Core Banking')."""
        page = self.session.page
        logger.info("Looking for category tab: %s", category)

        for attempt in range(10):
            for selector in self.selectors.get_chain("pathway_tab_prefix"):
                elements = page.locator(selector)
                count = await elements.count()
                for i in range(count):
                    el = elements.nth(i)
                    title = await el.get_attribute("title") or ""
                    text = await el.inner_text()
                    if (
                        category.lower() in title.lower()
                        or category.lower() in text.lower()
                    ):
                        await el.click()
                        await self.session.wait_for_stable_page()
                        logger.info("Selected category tab: %s", category)
                        return

            logger.debug("Category tab not yet visible (attempt %d/10)", attempt + 1)
            await asyncio.sleep(1)

        raise NavigationError(f"Category tab not found: {category}")

    # ------------------------------------------------------------------
    # Step 2b: Select Pathway dropdown (e.g. "...Wealth Management Practitioner")
    # ------------------------------------------------------------------

    async def select_pathway(self, pathway_name: str) -> None:
        """Find and click the pathway dropdown matching the given name.

        On CSOD, pathways are listed as expandable rows under the category tab.
        Clicking the pathway row expands the course table beneath it.
        Sets self._pathway_container to scope subsequent operations.
        """
        page = self.session.page
        logger.info("Looking for pathway: %s", pathway_name)

        for attempt in range(10):
            # Strategy 1: look for pathway labels/links with matching text
            # CSOD uses [id^='pathway-'] elements or text within the pathway list
            pathway_labels = page.locator(
                "[id^='pathway-'], .pathway-name, .pathway-title, "
                "[class*='pathway'] a, [class*='pathway'] span"
            )
            count = await pathway_labels.count()
            for i in range(count):
                el = pathway_labels.nth(i)
                text = await el.inner_text()
                title = await el.get_attribute("title") or ""
                if (
                    pathway_name.lower() in text.lower()
                    or pathway_name.lower() in title.lower()
                ):
                    if await el.is_visible():
                        await el.click()
                        await self.session.wait_for_stable_page()
                        logger.info("Selected pathway: %s", pathway_name)
                        self._pathway_container = self._find_pathway_container(page)
                        return

            # Strategy 2: broad visible text match
            matches = page.get_by_text(pathway_name, exact=False)
            match_count = await matches.count()
            for i in range(match_count):
                el = matches.nth(i)
                if await el.is_visible():
                    await el.click()
                    await self.session.wait_for_stable_page()
                    logger.info("Selected pathway via text match: %s", pathway_name)
                    self._pathway_container = self._find_pathway_container(page)
                    return

            logger.debug("Pathway not yet visible (attempt %d/10)", attempt + 1)
            await asyncio.sleep(1)

        raise NavigationError(f"Pathway not found: {pathway_name}")

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

    async def find_course_link(self, course_name: str, course_code: str = "") -> Locator:
        """Return a Locator for the course link matching the given name or code.

        Search order per table:
          1. Name match via title attribute
          2. Name match via link text
          3. Code match via title attribute
          4. Code match via link text
          5. Code match via row/card text -> nearest anchor

        Raises:
            NavigationError: If the course link is not found.
        """
        page = self.session.page
        table_sel = self.selectors.get("pathway_course_table")

        if table_sel:
            tables = page.locator(table_sel)
            count = await tables.count()
            for i in range(count):
                table = tables.nth(i)
                if not await table.is_visible():
                    continue

                # 1. Name match via title attribute
                link = table.locator(f'a[title*="{course_name}"]')
                if await link.count() > 0:
                    logger.info("Found course link: %s", course_name)
                    return link.first

                # 2. Name match via link text
                links = table.locator("a")
                link_count = await links.count()
                for j in range(link_count):
                    a = links.nth(j)
                    text = (await a.inner_text()).strip()
                    if course_name.lower() in text.lower():
                        logger.info("Found course link by text: %s", course_name)
                        return a

                # 3-5. Code-based fallbacks
                if course_code:
                    # 3. Code in title attribute
                    link = table.locator(f'a[title*="{course_code}"]')
                    if await link.count() > 0:
                        logger.info("Found course link by code in title: %s", course_code)
                        return link.first

                    # 4. Code in link text
                    link = table.locator(f'a:has-text("{course_code}")')
                    if await link.count() > 0:
                        logger.info("Found course link by code in text: %s", course_code)
                        return link.first

                    # 5. Code anywhere in row/card -> nearest anchor
                    row = table.locator(f'tr:has-text("{course_code}"), [class*="card"]:has-text("{course_code}")')
                    if await row.count() > 0:
                        row_link = row.first.locator("a").first
                        if await row_link.count() > 0:
                            logger.info("Found course link by code in row: %s", course_code)
                            return row_link

        # Broader fallback: search entire page
        link = page.locator(f'a[title*="{course_name}"]')
        if await link.count() > 0:
            logger.info("Found course link (broad search): %s", course_name)
            return link.first

        if course_code:
            link = page.locator(f'a[title*="{course_code}"]')
            if await link.count() > 0:
                logger.info("Found course link by code (broad): %s", course_code)
                return link.first

            link = page.locator(f'a:has-text("{course_code}")')
            if await link.count() > 0:
                logger.info("Found course link by code text (broad): %s", course_code)
                return link.first

            # Pathway tables sometimes lazy-render entries below the fold —
            # scroll and rescan once before giving up.
            try:
                await page.mouse.wheel(0, 3000)
                await asyncio.sleep(1)
                code_link = page.locator(
                    f'a[title*="{course_code}"], a:has-text("{course_code}")'
                ).first
                if (
                    await code_link.count() > 0
                    and await code_link.is_visible()
                ):
                    logger.info(
                        "Found course link after scroll by course code: %s",
                        course_code,
                    )
                    return code_link
            except Exception:
                pass

        # Log available courses for debugging
        await self._log_available_courses(page)
        code_info = f" (code: {course_code})" if course_code else ""
        raise NavigationError(
            f"Course link not found: {course_name}{code_info}. "
            f"The course may have been renamed or removed from this pathway. "
            f"Check targets.json and update the course name to match the current listing."
        )

    async def open_course_link(self, course_name: str, course_code: str = "") -> None:
        """Find the course link and click it, opening a new tab.

        The caller should use session.click_and_wait_for_new_tab() to
        capture the new tab.  This method just finds the link and triggers
        the click inside the new-tab expectation.

        If the course isn't listed on the current pathway page, falls back
        to the portal's global search (when a course_code is provided).
        """
        try:
            link = await self.find_course_link(course_name, course_code=course_code)
        except NavigationError as pathway_error:
            if not course_code:
                raise
            logger.warning(
                "Course not found on pathway page; attempting global search fallback for %s",
                course_code,
            )
            try:
                await self.open_course_via_global_search(course_code)
                return
            except Exception as search_error:
                raise NavigationError(
                    f"Failed to open course '{course_name}' ({course_code}) via "
                    f"pathway lookup and global search. Pathway error: "
                    f"{pathway_error}. Global search error: {search_error}"
                ) from search_error

        # Scroll into view — course links in pathway tables may be outside viewport
        try:
            await link.scroll_into_view_if_needed()
        except Exception:
            logger.warning("Scroll failed for %s, re-finding link", course_name)
            await asyncio.sleep(1)
            link = await self.find_course_link(course_name, course_code=course_code)
            await link.scroll_into_view_if_needed()
        await asyncio.sleep(0.5)

        async def _click():
            await link.click()

        await self.session.click_and_wait_for_new_tab(_click)
        logger.info("Opened course in new tab: %s", course_name)

    # ------------------------------------------------------------------
    # Global search fallback
    # ------------------------------------------------------------------

    async def find_via_global_search(self, course_code: str) -> Locator:
        """Locate a course via the portal's global search by course code.

        Returns:
            A Locator pointing at the search-result link for the course.

        Raises:
            NavigationError: If no search UI is present or no result matches.
        """
        page = self.session.page

        trigger_sel = self.selectors.get("global_search_trigger")
        input_sel = self.selectors.get("global_search_input")
        result_sel = self.selectors.get("global_search_result_link")

        if not trigger_sel or not result_sel:
            raise NavigationError("Global search selectors not configured")

        trigger = page.locator(trigger_sel).first
        if await trigger.count() == 0:
            raise NavigationError("Global search trigger not found on portal")

        tag_name = (
            await trigger.evaluate("(el) => el.tagName")
        ).upper()

        if tag_name == "INPUT":
            search_input = trigger
            try:
                await search_input.focus()
            except Exception:
                await search_input.click()
        else:
            await trigger.click()
            if not input_sel:
                raise NavigationError("Global search input selector missing")
            search_input = page.locator(input_sel).first
            await search_input.wait_for(state="visible", timeout=10000)

        await search_input.fill(course_code)
        await search_input.press("Enter")

        result = page.locator(result_sel).first
        await result.wait_for(state="visible", timeout=15000)

        # Scope each branch of the result-link chain with a title= or
        # :has-text() filter so we don't accidentally click the wrong result.
        result_branches = self.selectors.get_chain("global_search_result_link")
        by_title = ",".join(
            f'{branch}[title*="{course_code}"]' for branch in result_branches
        )
        if by_title:
            scoped = page.locator(by_title).first
            if await scoped.count() > 0:
                return scoped

        by_text = ",".join(
            f'{branch}:has-text("{course_code}")' for branch in result_branches
        )
        if by_text:
            scoped = page.locator(by_text).first
            if await scoped.count() > 0:
                return scoped

        raise NavigationError(
            f"Global search returned results but none matched code '{course_code}'"
        )

    async def open_course_via_global_search(self, course_code: str) -> None:
        """Find a course via global search and click it, opening a new tab."""
        result = await self.find_via_global_search(course_code)
        try:
            await result.scroll_into_view_if_needed()
        except Exception:
            pass

        async def _click():
            await result.click()

        await self.session.click_and_wait_for_new_tab(_click)
        logger.info("Opened course via global search: %s", course_code)

    async def open_course_url(self, url: str) -> None:
        """Open a course directly via URL in a new tab, bypassing pathway lookup.

        Used when targets.json provides an explicit `url` for a course (e.g. when
        the pathway page hides or lazy-loads the course link).
        """
        page = self.session.page

        async def _open():
            await page.evaluate("(u) => window.open(u, '_blank')", url)

        await self.session.click_and_wait_for_new_tab(_open)
        logger.info("Opened course in new tab via direct URL: %s", url)

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

        # Track whether any old-version CTA was followed during the run.
        self._old_version_followed_during_launch = False
        self._old_version_followed_url = ""
        pre_launch_url = page.url

        # Step 0: Check for old-version CTA before Open Curriculum.
        if await self._follow_old_version_if_present():
            self._old_version_followed_during_launch = True
            self._old_version_followed_url = page.url

        # Step 1: Open Curriculum — primary button for in-progress courses,
        # dropdown menu item for completed ones (primary shows "View Certificate").
        # CTA section renders after domcontentloaded — let the network settle
        # so the duplex button is attached before we probe it.
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await self._click_open_curriculum_or_dropdown(timeout_ms=60000)

        # Step 1b: Some courses expose the old-version CTA only after
        # Open Curriculum — check again before Launch.
        if await self._follow_old_version_if_present():
            self._old_version_followed_during_launch = True
            self._old_version_followed_url = page.url

        # Step 1c: Detect fully-completed course state. The curriculum page
        # for a completed course shows "Evaluate" (the optional post-test) as
        # the primary CTA instead of "Launch". Skip Steps 2–4; per-lesson
        # Launch is handled inside click_curriculum_item.
        if await self._evaluate_primary_visible():
            logger.info(
                "Course already completed — Evaluate (not Launch) primary CTA "
                "detected; skipping Launch/Fullscreen/Resume steps"
            )
            result.course_already_completed = True
            content = await self.detect_content_frame()
            if isinstance(content, Frame):
                result.iframe_detected = True
            result.content_frame = content
            try:
                result.final_page_title = await page.title()
            except Exception:
                result.final_page_title = ""
            return result

        # Step 2: Launch (with old-version fallback on timeout).
        await self._click_with_old_version_fallback(
            self.selectors.get("launch_button"),
            "Launch",
            timeout_ms=30000,
        )

        if self._old_version_followed_during_launch:
            result.old_version_redirected = True
            final_url = self._old_version_followed_url or page.url
            if final_url and final_url != pre_launch_url:
                result.old_version_url = final_url

        # Step 3: Fullscreen
        result.fullscreen_succeeded = await self._wait_and_click(
            self.selectors.get("fullscreen_button"),
            "Fullscreen",
            timeout_ms=15000,
            optional=True,
        )

        # Step 4: Dismiss resume prompt on outer page (optional)
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

            # Step 5b: Dismiss resume prompt inside iframe (may appear here instead)
            if not resume_dismissed:
                try:
                    dismiss_sel = self.selectors.get("dismiss_resume_no")
                    if dismiss_sel:
                        for sel in [s.strip() for s in dismiss_sel.split(",")]:
                            no_btn = content.locator(sel)
                            if await no_btn.count() > 0 and await no_btn.first.is_visible():
                                await no_btn.first.click()
                                await asyncio.sleep(1)
                                logger.info("Dismissed resume prompt inside iframe")
                                result.resume_prompt_appeared = True
                                break
                except Exception as e:
                    logger.debug("iframe resume dismiss check: %s", e)
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
    # Step 6b: Extract curriculum from sidebar
    # ------------------------------------------------------------------

    async def extract_curriculum_from_page(self) -> dict:
        """Extract the curriculum sidebar data from the current page.

        Should be called after launch_course(), when the curriculum
        sidebar is visible. Uses Playwright Locators to read the tree
        items directly from the live DOM.

        Returns:
            Dict with course_name, course_code, progress, items, etc.
            Same structure as extract_courses.extract_curriculum().
        """
        page = self.session.page

        # Course title
        course_title = ""
        title_sel = self.selectors.get("curriculum_title")
        if title_sel:
            title_loc = page.locator(title_sel).first
            if await title_loc.count() > 0:
                course_title = (await title_loc.inner_text()).strip()

        # Extract course code from title
        course_name = course_title
        course_code = ""
        if course_title:
            import re
            code_match = re.search(r'\s+([A-Z]{2,4}\d[A-Z]{2,5}\d*)\s*$', course_title)
            if code_match:
                course_code = code_match.group(1)
                course_name = course_title[:code_match.start()].strip()

        # Progress percentage
        progress_pct = ""
        pct_sel = self.selectors.get("curriculum_progress_pct")
        if pct_sel:
            pct_loc = page.locator(pct_sel).first
            if await pct_loc.count() > 0:
                progress_pct = (await pct_loc.inner_text()).strip()

        # Progress count (e.g., "20/20")
        progress_count = ""
        count_sel = self.selectors.get("curriculum_progress_count")
        if count_sel:
            count_loc = page.locator(count_sel).first
            if await count_loc.count() > 0:
                progress_count = (await count_loc.inner_text()).strip()

        # Overall status
        overall_status = ""
        status_sel = self.selectors.get("curriculum_status")
        if status_sel:
            status_loc = page.locator(status_sel).first
            if await status_loc.count() > 0:
                overall_status = (await status_loc.inner_text()).strip()

        # Total duration
        total_duration = ""
        dur_sel = self.selectors.get("curriculum_duration")
        if dur_sel:
            dur_loc = page.locator(dur_sel).first
            if await dur_loc.count() > 0:
                total_duration = (await dur_loc.inner_text()).strip()

        # Training items from tree
        items = []
        item_sel = self.selectors.get("curriculum_tree_item")
        if item_sel:
            tree_items = page.locator(item_sel)
            item_count = await tree_items.count()

            for i in range(item_count):
                item = tree_items.nth(i)

                node_id = await item.get_attribute("data-node-id") or ""
                position = await item.get_attribute("aria-posinset") or ""
                total = await item.get_attribute("aria-setsize") or ""

                # Title from .titles element
                title = ""
                title_item_sel = self.selectors.get("curriculum_item_title")
                if title_item_sel:
                    titles_el = item.locator(title_item_sel).first
                    if await titles_el.count() > 0:
                        title = await titles_el.get_attribute("content") or ""
                        if not title:
                            title = await titles_el.get_attribute("aria-label") or ""
                        if not title:
                            title = (await titles_el.inner_text()).strip()
                title = title.strip()

                # Completion status
                status = "not_started"
                completed_sel = self.selectors.get("curriculum_item_completed")
                in_progress_sel = self.selectors.get("curriculum_item_in_progress")

                if completed_sel and await item.locator(completed_sel).count() > 0:
                    status = "completed"
                elif in_progress_sel and await item.locator(in_progress_sel).count() > 0:
                    status = "in_progress"

                # Duration
                duration = ""
                due_el = item.locator(".dueDate").first
                if await due_el.count() > 0:
                    duration = (await due_el.inner_text()).strip()

                if not title:
                    continue

                items.append({
                    "position": int(position) if position else 0,
                    "total": int(total) if total else 0,
                    "title": title,
                    "status": status,
                    "duration": duration,
                    "node_id": node_id,
                })

        completed = sum(1 for it in items if it["status"] == "completed")
        in_progress = sum(1 for it in items if it["status"] == "in_progress")
        not_started = sum(1 for it in items if it["status"] == "not_started")

        result = {
            "course_name": course_name,
            "course_code": course_code,
            "overall_status": overall_status,
            "progress": progress_pct,
            "progress_count": progress_count,
            "total_duration": total_duration,
            "summary": {
                "total_items": len(items),
                "completed": completed,
                "in_progress": in_progress,
                "not_started": not_started,
            },
            "items": items,
        }

        logger.info(
            "Extracted curriculum: %s (%s) — %d items (%d completed, %d in progress)",
            course_name, course_code, len(items), completed, in_progress,
        )
        return result

    # ------------------------------------------------------------------
    # Step 6c: Click a specific curriculum item in the sidebar
    # ------------------------------------------------------------------

    async def click_curriculum_item(self, position: int, node_id: str = "") -> "LaunchResult":
        """Click a curriculum item in the sidebar to open its content.

        Finds the tree item by position (1-based) or data-node-id,
        clicks it, waits for content to load, dismisses any resume
        popup, and re-detects the content iframe.

        Args:
            position: 1-based position of the item in the curriculum.
            node_id: Optional data-node-id attribute for precise targeting.

        Returns:
            LaunchResult with updated content_frame.
        """
        result = LaunchResult()
        page = self.session.page

        # Try to find the tree item by node_id first, then by position
        item_locator = None
        if node_id:
            sel = f'[role="treeitem"][aria-level="2"][data-node-id="{node_id}"]'
            loc = page.locator(sel)
            if await loc.count() > 0:
                item_locator = loc.first
                logger.debug("Found curriculum item by node_id: %s", node_id)

        if item_locator is None:
            sel = f'[role="treeitem"][aria-level="2"][aria-posinset="{position}"]'
            loc = page.locator(sel)
            if await loc.count() > 0:
                item_locator = loc.first
                logger.debug("Found curriculum item by position: %d", position)

        if item_locator is None:
            # Fallback: get all tree items and pick by index
            all_items = page.locator('[role="treeitem"][aria-level="2"]')
            count = await all_items.count()
            if position <= count:
                item_locator = all_items.nth(position - 1)
                logger.debug("Found curriculum item by index fallback: %d/%d", position, count)

        if item_locator is None:
            raise CourseLaunchError(
                f"Curriculum item not found: position={position}, node_id={node_id}"
            )

        # Capture old iframe src before clicking (to detect content change)
        old_iframe_src = ""
        iframe_sel = (
            'iframe#training-iframe, '
            'iframe[data-testid="curriculumPlayer@coursePlayer"]'
        )
        try:
            iframe_loc = page.locator(iframe_sel).first
            if await iframe_loc.count() > 0:
                old_iframe_src = await iframe_loc.get_attribute("src") or ""
        except Exception:
            pass

        # Click the item using force=True to bypass iframe overlay
        try:
            await item_locator.evaluate("el => el.scrollIntoView({block: 'center'})")
        except Exception:
            pass
        await asyncio.sleep(0.5)
        await item_locator.dispatch_event("click")
        logger.info("Clicked curriculum item %d", position)

        # Wait for iframe src to change (confirms platform loaded new item)
        src_changed = False
        for _ in range(30):  # 30 x 0.5s = 15s max
            try:
                iframe_loc = page.locator(iframe_sel).first
                if await iframe_loc.count() > 0:
                    new_src = await iframe_loc.get_attribute("src") or ""
                    if new_src and new_src != old_iframe_src:
                        logger.info("Iframe src changed — new content loading")
                        src_changed = True
                        break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        if not src_changed:
            logger.warning(
                "Iframe src did not change after clicking item %d — "
                "content may be stale",
                position,
            )

        # Wait for new content to fully load
        await self.session.wait_for_stable_page()
        await asyncio.sleep(2)

        # Handle "Your training is completed" state — click Launch to re-enter
        launch_clicked = await self._wait_and_click(
            self.selectors.get("launch_button"),
            "Launch (re-enter completed item)",
            timeout_ms=20000,
            optional=True,
        )

        if launch_clicked:
            # Launch opens the item in full-page mode — wait for it to load
            await self.session.wait_for_stable_page()
            await asyncio.sleep(2)
            result.fullscreen_succeeded = True
        else:
            # No Launch button (item not yet completed) — click fullscreen
            result.fullscreen_succeeded = await self._wait_and_click(
                self.selectors.get("fullscreen_button"),
                "Fullscreen",
                timeout_ms=10000,
                optional=True,
            )

        # Dismiss resume prompt on outer page (wait longer — popup can be slow)
        resume_dismissed = await self._wait_and_click(
            self.selectors.get("dismiss_resume_no"),
            "Dismiss resume prompt (No)",
            timeout_ms=15000,
            optional=True,
        )
        result.resume_prompt_appeared = resume_dismissed

        # Re-detect content iframe (it may have reloaded)
        content = await self.detect_content_frame()
        if isinstance(content, Frame):
            result.iframe_detected = True
            result.content_frame = content

            # Dismiss resume prompt inside iframe if not already dismissed
            if not resume_dismissed:
                try:
                    dismiss_sel = self.selectors.get("dismiss_resume_no")
                    if dismiss_sel:
                        for sel in [s.strip() for s in dismiss_sel.split(",")]:
                            no_btn = content.locator(sel)
                            if await no_btn.count() > 0 and await no_btn.first.is_visible():
                                await no_btn.first.click()
                                await asyncio.sleep(1)
                                logger.info("Dismissed resume prompt inside iframe")
                                result.resume_prompt_appeared = True
                                break
                except Exception as e:
                    logger.debug("iframe resume dismiss check: %s", e)
        else:
            result.content_frame = content

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

    async def _evaluate_primary_visible(self) -> bool:
        """Return True if the duplex primary CTA is the post-test "Evaluate"
        button (signals a fully-completed course on the curriculum page).
        """
        page = self.session.page
        for selector in self.selectors.get_chain("evaluate_button"):
            try:
                button = await page.query_selector(selector)
                if button and await button.is_visible():
                    logger.debug("Evaluate primary detected (selector=%s)", selector)
                    return True
            except Exception as e:
                logger.debug("Evaluate probe selector '%s' failed: %s", selector, e)
                continue
        return False

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

    async def _follow_old_version_if_present(self) -> bool:
        """Probe for an old-version "click here to access the latest version"
        CTA and click it if visible. Idempotent and cheap — safe to call
        multiple times during a launch sequence.

        Returns:
            True if the CTA was found and clicked (regardless of whether the
            URL visibly changed — some portals swap the DOM in place).
            False if no CTA was visible.
        """
        page = self.session.page
        link_sel = self.selectors.get("old_version_link")
        if not link_sel:
            return False

        try:
            link = page.locator(link_sel).first
            if not await link.is_visible():
                return False
        except Exception:
            return False

        logger.warning("Old version link detected; redirecting to latest version")

        try:
            old_url = page.url
            await link.click()

            try:
                await page.wait_for_url(
                    lambda u: u != old_url and "/Curriculum/" in u,
                    timeout=15000,
                )
            except Exception:
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=10000
                    )
                except Exception:
                    pass

            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            new_url = page.url
            if new_url != old_url:
                logger.info("Redirected to new version: %s", new_url)
            else:
                logger.info(
                    "Old-version CTA clicked; DOM swapped in place (URL unchanged)"
                )
            return True
        except Exception as e:
            logger.error("Failed to follow old version redirect: %s", e)
            return False

    async def _click_open_curriculum_or_dropdown(self, timeout_ms: int) -> None:
        """Click Open Curriculum, handling both the primary-button form
        (in-progress courses) and the duplex-button dropdown form
        (completed courses show "View Certificate" as the primary and hide
        Open Curriculum inside the chevron menu).

        Falls back to the old-version CTA retry if neither path works.
        """
        page = self.session.page
        primary_sel = self.selectors.get("open_curriculum_button")
        trigger_sel = self.selectors.get("open_curriculum_menu_trigger")
        menu_item_sel = self.selectors.get("open_curriculum_menu_item")

        logger.info(
            "Attempting Open Curriculum (primary button → dropdown → old-version fallback)"
        )

        try:
            primary = page.locator(primary_sel).first
            await primary.wait_for(state="visible", timeout=3000)
            await primary.click()
            await self.session.wait_for_stable_page()
            logger.info("Clicked: Open Curriculum (primary button)")
            return
        except Exception as e:
            logger.debug(
                "Primary Open Curriculum button not visible within 3s: %s", e
            )

        if trigger_sel and menu_item_sel:
            trigger_clicked = False
            matched_trigger = ""
            for tsel in [s.strip() for s in trigger_sel.split(",") if s.strip()]:
                try:
                    trigger = page.locator(tsel).first
                    if await trigger.count() == 0:
                        continue
                    if not await trigger.is_visible():
                        continue
                    await trigger.click()
                    logger.info("Opened duplex dropdown (via %s)", tsel)
                    trigger_clicked = True
                    matched_trigger = tsel
                    break
                except Exception as e:
                    logger.debug("Dropdown trigger %s failed: %s", tsel, e)
                    continue

            if trigger_clicked:
                for msel in [s.strip() for s in menu_item_sel.split(",") if s.strip()]:
                    try:
                        item = page.locator(msel).first
                        await item.wait_for(state="visible", timeout=5000)
                        await item.click()
                        await self.session.wait_for_stable_page()
                        logger.info(
                            "Clicked: Open Curriculum (dropdown menu item via %s)",
                            msel,
                        )
                        return
                    except Exception as e:
                        logger.debug("Menu item %s not clickable: %s", msel, e)
                        continue
                raise CourseLaunchError(
                    f"Dropdown opened via {matched_trigger} but Open Curriculum "
                    f"menu item not found; tried {menu_item_sel}"
                )

        await self._click_with_old_version_fallback(
            primary_sel,
            "Open Curriculum",
            timeout_ms=timeout_ms,
        )

    async def _click_with_old_version_fallback(
        self,
        selector_chain: str,
        description: str,
        timeout_ms: int,
    ) -> None:
        """Click a button; if the click times out, probe for an old-version
        CTA that may have appeared between steps, follow it if present, and
        retry the click once.
        """
        try:
            await self._wait_and_click(selector_chain, description, timeout_ms=timeout_ms)
            return
        except CourseLaunchError as original_error:
            logger.warning(
                "%s not found within %dms; probing for old-version CTA",
                description,
                timeout_ms,
            )
            followed = await self._follow_old_version_if_present()
            if not followed:
                raise original_error

            # Capture that a redirect happened so launch_course() can record it.
            self._old_version_followed_during_launch = True
            self._old_version_followed_url = self.session.page.url

            page = self.session.page
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            await self._wait_and_click(selector_chain, description, timeout_ms=timeout_ms)
