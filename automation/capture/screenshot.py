"""Screenshot capture with three modes: full-page, viewport-scroll, section.

Mode A (full-page):  page.screenshot(full_page=True) — always runs.
Mode B (viewport):   Scroll viewport-by-viewport with overlap — for long pages.
Mode C (section):    element.screenshot() on DOM-detected regions — when --enable-crops.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union

from playwright.async_api import Frame, Page

from automation.capture.browser import BrowserSession
from automation.capture.cropper import ContentCropper
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
        self._content_frame: Optional[Union[Frame, Page]] = None

    def set_content_frame(self, frame: Optional[Union[Frame, Page]]) -> None:
        """Set the content frame (iframe) where course content lives."""
        self._content_frame = frame

    @property
    def content_page(self) -> Union[Frame, Page]:
        """Return the content frame if set, otherwise the session page."""
        if self._content_frame is not None:
            if isinstance(self._content_frame, Frame) and self._content_frame.is_detached():
                logger.warning("Content frame is detached, falling back to page")
                self._content_frame = None
                return self.session.page
            return self._content_frame
        return self.session.page

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
        """Mode A: Capture the content area as a single screenshot.

        When a content iframe is set, screenshots just the iframe element
        (excludes sidebar, header). Falls back to full-page if no iframe.
        """
        page = self.session.page

        # If content is in an iframe, screenshot just the iframe element
        if self._content_frame is not None and self._content_frame != page:
            iframe_sel = (
                'iframe#training-iframe, '
                'iframe[data-testid="curriculumPlayer@coursePlayer"]'
            )
            try:
                iframe_el = await page.query_selector(iframe_sel)
                if iframe_el:
                    # Scroll iframe content to top before capture
                    try:
                        await self._content_frame.evaluate(
                            "window.scrollTo(0, 0)"
                        )
                    except Exception:
                        pass
                    await iframe_el.screenshot(path=str(output_path))
                    logger.info(f"Content-frame screenshot: {output_path.name}")
                    return output_path
            except Exception as e:
                logger.debug(
                    f"Iframe screenshot failed, falling back to full page: {e}"
                )

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
        Uses a collect-then-filter approach to eliminate redundant crops:
        containment, near-duplicate (IoU), clipping, and minimum-height filters.
        Returns list of (path, content_type_label) tuples.
        """
        page = self.content_page
        crops: List[Tuple[Path, str]] = []

        # --- Phase 1: Collect candidates ---
        candidates = []  # (element_handle, box_dict, label)

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
                        box = await el.bounding_box()
                        if not box or box["width"] < 50:
                            continue
                        candidates.append((el, box, label))
                except Exception as e:
                    logger.debug(f"Selector '{selector}' failed: {e}")
                    continue

        if not candidates:
            # OpenCV fallback when DOM selectors find nothing
            full_page_path = output_dir / f"{prefix}_full.png"
            if full_page_path.exists():
                try:
                    cropper = ContentCropper()
                    ocv_crops = cropper.crop_all_regions(
                        full_page_path, output_dir, prefix
                    )
                    for path, region_type in ocv_crops:
                        crops.append((path, region_type))
                    if ocv_crops:
                        logger.info(
                            f"OpenCV fallback: {len(ocv_crops)} regions detected"
                        )
                except Exception as e:
                    logger.warning(f"OpenCV crop detection failed: {e}")
            return crops

        # --- Phase 2: Filter ---

        def _rect(b):
            return (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])

        def _area(b):
            return b["width"] * b["height"]

        def _intersection_area(r1, r2):
            x1 = max(r1[0], r2[0])
            y1 = max(r1[1], r2[1])
            x2 = min(r1[2], r2[2])
            y2 = min(r1[3], r2[3])
            if x2 <= x1 or y2 <= y1:
                return 0
            return (x2 - x1) * (y2 - y1)

        def _iou(box_a, box_b):
            r1, r2 = _rect(box_a), _rect(box_b)
            inter = _intersection_area(r1, r2)
            union = _area(box_a) + _area(box_b) - inter
            return inter / union if union > 0 else 0

        def _contains(outer, inner, margin=10):
            ro, ri = _rect(outer), _rect(inner)
            return (
                ri[0] >= ro[0] - margin
                and ri[1] >= ro[1] - margin
                and ri[2] <= ro[2] + margin
                and ri[3] <= ro[3] + margin
            )

        # 2a. Viewport clipping — reject elements with negative coords
        #     or extending beyond content width
        try:
            vp_bounds = await page.evaluate(
                "() => ({ width: document.documentElement.clientWidth })"
            )
            vp_width = vp_bounds["width"]
        except Exception:
            vp_width = 1920

        CLIP_MARGIN = 5
        candidates = [
            (el, box, label) for el, box, label in candidates
            if _rect(box)[0] >= -CLIP_MARGIN
            and _rect(box)[2] <= vp_width + CLIP_MARGIN
        ]

        # 2b. Minimum height 80px — eliminates thin header-bar-only crops
        MIN_CROP_HEIGHT = 80
        before = len(candidates)
        candidates = [
            (el, box, label) for el, box, label in candidates
            if box["height"] >= MIN_CROP_HEIGHT
        ]
        if len(candidates) < before:
            logger.debug(
                "Height filter removed %d short elements",
                before - len(candidates),
            )

        # 2c. Containment filter — sort by area desc, skip elements
        #     fully inside a larger kept element
        candidates.sort(key=lambda c: _area(c[1]), reverse=True)
        kept = []
        for el, box, label in candidates:
            if any(_contains(kb, box) for _, kb, _ in kept):
                logger.debug(
                    f"Skipping contained element: {label} "
                    f"({box['width']:.0f}x{box['height']:.0f})"
                )
                continue
            kept.append((el, box, label))
        candidates = kept

        # 2d. IoU near-duplicate filter — drop smaller of two boxes
        #     overlapping > 70%
        IOU_THRESHOLD = 0.7
        to_remove = set()
        for i in range(len(candidates)):
            if i in to_remove:
                continue
            for j in range(i + 1, len(candidates)):
                if j in to_remove:
                    continue
                if _iou(candidates[i][1], candidates[j][1]) > IOU_THRESHOLD:
                    to_remove.add(j)
                    logger.debug(
                        f"Removing near-duplicate: {candidates[j][2]} "
                        f"({candidates[j][1]['width']:.0f}x"
                        f"{candidates[j][1]['height']:.0f})"
                    )
        candidates = [
            c for idx, c in enumerate(candidates) if idx not in to_remove
        ]

        # Re-sort by vertical position (top to bottom) before screenshotting
        candidates.sort(key=lambda c: (c[1]["y"], c[1]["x"]))

        # --- Phase 3: Screenshot survivors ---
        crop_index = 1
        for el, box, label in candidates:
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

        if crops:
            logger.info(f"Section crops: {len(crops)} regions captured")
        return crops
