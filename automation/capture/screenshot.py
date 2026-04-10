"""Screenshot capture with three modes: full-page, viewport-scroll, section.

Mode A (full-page):  page.screenshot(full_page=True) — always runs.
Mode B (viewport):   Scroll viewport-by-viewport with overlap — for long pages.
Mode C (section):    element.screenshot() on DOM-detected regions — when --enable-crops.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from playwright.async_api import Page

from automation.capture.browser import BrowserSession
from automation.config import AutomationConfig
from automation.selectors import SelectorProfile
from automation.state.manifest import PageInfo

logger = logging.getLogger(__name__)

# Overlap between viewport screenshots (px) to avoid missing content at edges
VIEWPORT_OVERLAP = 100
# Minimum height ratio to trigger viewport scrolling (page must be > Nx viewport)
VIEWPORT_SCROLL_THRESHOLD = 3


@dataclass
class CaptureResult:
    """Results from capturing a single page."""

    page_info: PageInfo
    full_page_path: Optional[Path] = None
    viewport_paths: List[Path] = field(default_factory=list)
    section_crops: List[Tuple[Path, str]] = field(default_factory=list)  # (path, label)
    timestamp: str = ""
    page_height: int = 0
    viewport_height: int = 0


class ScreenshotCapture:
    """Captures screenshots with multiple strategies."""

    def __init__(
        self,
        session: BrowserSession,
        config: AutomationConfig,
        selectors: SelectorProfile,
    ):
        self.session = session
        self.config = config
        self.selectors = selectors

    async def capture_page(
        self, page_info: PageInfo, lesson_dir: Path
    ) -> CaptureResult:
        """Main entry: runs configured capture mode(s).

        Always does Mode A (full-page). Optionally adds B and/or C.
        """
        import time

        screenshots_dir = lesson_dir / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        prefix = f"page_{page_info.page_index:03d}"
        page_height = await self.session.get_page_height()
        viewport_height = await self.session.get_viewport_height()

        result = CaptureResult(
            page_info=page_info,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            page_height=page_height,
            viewport_height=viewport_height,
        )

        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would capture {prefix} (height={page_height})")
            return result

        # Mode A: Full-page screenshot (always)
        full_path = await self.capture_full_page(
            screenshots_dir / f"{prefix}_full.png"
        )
        result.full_page_path = full_path

        # Mode B: Viewport scroll (if page is very long)
        if (
            self.config.capture_mode in ("viewport", "section")
            and page_height > viewport_height * VIEWPORT_SCROLL_THRESHOLD
        ):
            vp_paths = await self.capture_viewport_scroll(
                screenshots_dir, prefix
            )
            result.viewport_paths = vp_paths

        # Mode C: Section capture (if crops enabled)
        if self.config.enable_crops:
            crops = await self.capture_content_sections(
                screenshots_dir, prefix
            )
            result.section_crops = crops

        return result

    async def capture_full_page(self, output_path: Path) -> Path:
        """Mode A: Capture the entire page as a single screenshot."""
        page = self.session.page
        await page.screenshot(path=str(output_path), full_page=True)
        logger.info(f"Full-page screenshot: {output_path.name}")
        return output_path

    async def capture_viewport_scroll(
        self, output_dir: Path, prefix: str
    ) -> List[Path]:
        """Mode B: Scroll viewport-by-viewport, capture each.

        Returns list of screenshot paths in scroll order.
        Uses overlapping captures to avoid missing content at boundaries.
        """
        page = self.session.page
        page_height = await self.session.get_page_height()
        viewport_height = await self.session.get_viewport_height()

        paths: List[Path] = []
        step = viewport_height - VIEWPORT_OVERLAP
        y = 0
        index = 1

        while y < page_height:
            await self.session.scroll_to(y)
            path = output_dir / f"{prefix}_vp{index:02d}.png"
            await page.screenshot(path=str(path))
            paths.append(path)
            logger.debug(f"Viewport capture {index} at y={y}")
            y += step
            index += 1

        # Scroll back to top
        await self.session.scroll_to(0)
        logger.info(f"Viewport scroll: {len(paths)} captures")
        return paths

    async def capture_content_sections(
        self, output_dir: Path, prefix: str
    ) -> List[Tuple[Path, str]]:
        """Mode C: Find and screenshot specific DOM elements.

        Targets tables, diagrams, T24 screenshots via selector profiles.
        Returns list of (path, content_type_label) tuples.
        """
        page = self.session.page
        crops: List[Tuple[Path, str]] = []
        crop_index = 1

        # Define what to look for
        region_types = [
            ("tables", "table"),
            ("diagrams", "diagram"),
            ("screenshots", "t24_screenshot"),
        ]

        for role, label in region_types:
            for selector in self.selectors.get_chain(role):
                try:
                    elements = await page.query_selector_all(selector)
                    for el in elements:
                        # Skip tiny or invisible elements
                        box = await el.bounding_box()
                        if not box or box["width"] < 50 or box["height"] < 30:
                            continue

                        path = output_dir / f"{prefix}_crop_{crop_index:02d}_{label}.png"
                        try:
                            await el.screenshot(path=str(path))
                            crops.append((path, label))
                            logger.debug(
                                f"Section crop {crop_index}: {label} "
                                f"({box['width']:.0f}x{box['height']:.0f})"
                            )
                            crop_index += 1
                        except Exception as e:
                            logger.debug(f"Could not screenshot element: {e}")
                except Exception as e:
                    logger.debug(f"Selector '{selector}' failed: {e}")
                    continue

        if crops:
            logger.info(f"Section crops: {len(crops)} regions captured")
        return crops
