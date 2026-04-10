"""Content-type classification for captured pages.

Determines whether a page is predominantly text, table, diagram,
T24 screenshot, or mixed content.  This drives prompt routing in
the processor — each content type gets a specialized AI prompt.
"""

import logging
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np
from playwright.async_api import Page

from automation.selectors import SelectorProfile

logger = logging.getLogger(__name__)

# Content type constants
TYPE_TEXT_HEAVY = "text_heavy"
TYPE_TABLE = "table"
TYPE_DIAGRAM = "diagram"
TYPE_T24_SCREENSHOT = "t24_screenshot"
TYPE_MIXED = "mixed"

ALL_TYPES = {TYPE_TEXT_HEAVY, TYPE_TABLE, TYPE_DIAGRAM, TYPE_T24_SCREENSHOT, TYPE_MIXED}

# Content-type-specific prompt extensions
PROMPT_EXTENSIONS: Dict[str, str] = {
    TYPE_TEXT_HEAVY: (
        "This page is primarily text content. "
        "Clean up OCR artifacts, fix formatting, structure into clear sections with headings. "
        "Preserve all information exactly."
    ),
    TYPE_TABLE: (
        "This page contains one or more tables. "
        "Preserve exact columns and row relationships as Markdown tables. "
        "Do not paraphrase cell values. Retain all abbreviations, service codes, "
        "and T24 identifiers exactly as shown."
    ),
    TYPE_DIAGRAM: (
        "This page contains a workflow diagram or flowchart. "
        "Describe all nodes and connectors in sequence. "
        "Mention every decision point, branch, and endpoint. "
        "Preserve system names, service names, and labels exactly. "
        "Output as a step-by-step flow description."
    ),
    TYPE_T24_SCREENSHOT: (
        "This page contains a T24 application screenshot. "
        "Capture every visible field label and its value. "
        "Preserve the service name, version name, and tab names exactly. "
        "Preserve enquiry and report headings. "
        "Distinguish static labels from user-entered values. "
        "Do not normalize T24 codes, field names, or service identifiers."
    ),
    TYPE_MIXED: (
        "This page has mixed content including text, tables, and/or visual elements. "
        "Process each section appropriately: clean text, preserve tables as Markdown, "
        "describe visual elements. Preserve all T24 identifiers and service names exactly."
    ),
}


class ContentClassifier:
    """Classifies page content type using DOM inspection and image heuristics."""

    def __init__(self, selectors: SelectorProfile):
        self.selectors = selectors

    async def classify_from_dom(self, page: Page) -> str:
        """Inspect the DOM for content indicators.

        Returns the most likely content type based on what elements exist.
        """
        scores: Dict[str, float] = {t: 0.0 for t in ALL_TYPES}

        # Check for tables
        table_count = await _count_elements(page, self.selectors.get("tables"))
        if table_count > 0:
            scores[TYPE_TABLE] += 2.0 * table_count

        # Check for diagrams/SVG/canvas
        diagram_count = await _count_elements(page, self.selectors.get("diagrams"))
        if diagram_count > 0:
            scores[TYPE_DIAGRAM] += 2.0 * diagram_count

        # Check for screenshots/images
        screenshot_count = await _count_elements(page, self.selectors.get("screenshots"))
        if screenshot_count > 0:
            scores[TYPE_T24_SCREENSHOT] += 2.0 * screenshot_count

        # Check general image count (non-icon images)
        try:
            large_images = await page.evaluate("""
                () => Array.from(document.querySelectorAll('img'))
                    .filter(img => img.naturalWidth > 200 && img.naturalHeight > 150)
                    .length
            """)
            if large_images > 0:
                scores[TYPE_T24_SCREENSHOT] += 0.5 * large_images
        except Exception:
            pass

        # Check text density
        try:
            text_length = await page.evaluate("""
                () => {
                    const main = document.querySelector('main, .content, article, #content');
                    return (main || document.body).innerText.length;
                }
            """)
            if text_length > 1000:
                scores[TYPE_TEXT_HEAVY] += 1.5
            if text_length > 3000:
                scores[TYPE_TEXT_HEAVY] += 1.0
        except Exception:
            pass

        # Determine winner
        best_type = max(scores, key=scores.get)
        total_score = sum(scores.values())

        # If multiple types have significant scores, it's mixed
        significant = [t for t, s in scores.items() if s > 0.5 and t != best_type]
        if significant and scores[best_type] < total_score * 0.7:
            return TYPE_MIXED

        return best_type if scores[best_type] > 0 else TYPE_TEXT_HEAVY

    def classify_from_image(self, image_path: Path) -> str:
        """Heuristic classification from the screenshot image itself.

        Uses edge density, color variance, and text-to-graphic ratio.
        """
        image = cv2.imread(str(image_path))
        if image is None:
            return TYPE_TEXT_HEAVY

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape

        # Edge detection
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.count_nonzero(edges) / (h * w)

        # Color variance (more color = likely UI/screenshot)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        saturation = hsv[:, :, 1]
        mean_saturation = float(np.mean(saturation))

        # Horizontal/vertical line detection (tables have many straight lines)
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
        h_lines = cv2.morphologyEx(edges, cv2.MORPH_OPEN, h_kernel)
        v_lines = cv2.morphologyEx(edges, cv2.MORPH_OPEN, v_kernel)
        line_density = (np.count_nonzero(h_lines) + np.count_nonzero(v_lines)) / (h * w)

        # Classification logic
        if line_density > 0.02:
            return TYPE_TABLE
        elif mean_saturation > 40 and edge_density > 0.08:
            return TYPE_T24_SCREENSHOT
        elif edge_density > 0.1:
            return TYPE_DIAGRAM
        elif edge_density < 0.03:
            return TYPE_TEXT_HEAVY
        else:
            return TYPE_MIXED

    async def classify(
        self, page: Optional[Page], image_path: Optional[Path]
    ) -> str:
        """Combined classification. DOM takes priority, image as tiebreaker."""
        dom_type = TYPE_TEXT_HEAVY
        image_type = TYPE_TEXT_HEAVY

        if page:
            try:
                dom_type = await self.classify_from_dom(page)
            except Exception as e:
                logger.debug(f"DOM classification failed: {e}")

        if image_path and image_path.exists():
            try:
                image_type = self.classify_from_image(image_path)
            except Exception as e:
                logger.debug(f"Image classification failed: {e}")

        # DOM result is trusted more, unless it's just text_heavy (default)
        if dom_type != TYPE_TEXT_HEAVY:
            return dom_type
        if image_type != TYPE_TEXT_HEAVY:
            return image_type
        return TYPE_TEXT_HEAVY

    @staticmethod
    def get_prompt_extension(content_type: str) -> str:
        """Get the prompt extension for a content type."""
        return PROMPT_EXTENSIONS.get(content_type, PROMPT_EXTENSIONS[TYPE_MIXED])


async def _count_elements(page: Page, selector_chain: str) -> int:
    """Count elements matching any selector in the chain."""
    total = 0
    for sel in [s.strip() for s in selector_chain.split(",") if s.strip()]:
        try:
            elements = await page.query_selector_all(sel)
            total += len(elements)
        except Exception:
            continue
    return total
