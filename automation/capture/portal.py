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

        # If we're already on the Pathways page (category tabs visible), skip
        for selector in self.selectors.get_chain("pathway_tab_prefix"):
            if await page.locator(selector).count() > 0:
                logger.info("Already on Pathways page (category tabs detected), skipping navigation")
                return

        # Portal widgets render dynamically — retry for up to ~10 seconds
        for attempt in range(10):
            # Try each selector in the chain
            for selector in self.selectors.get_chain("pathways_box"):
                locator = page.locator(selector)
                if await locator.count() > 0:
                    await self._click_pathways_link(locator.first)
                    return

            # Fallback: find all "Pathways" text matches, click first visible one
            pathways_all = page.get_by_text("Pathways", exact=True)
            count = await pathways_all.count()
            for i in range(count):
                el = pathways_all.nth(i)
                if await el.is_visible():
                    await self._click_pathways_link(el)
                    return

            logger.debug("Pathways box not yet visible (attempt %d/10)", attempt + 1)
            await asyncio.sleep(1)

        raise NavigationError("Pathways box not found on the portal page")

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

        # Scroll into view — course links in pathway tables may be outside viewport
        await link.scroll_into_view_if_needed()
        await asyncio.sleep(0.5)

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
