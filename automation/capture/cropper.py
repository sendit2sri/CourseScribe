"""OpenCV-based content region detection as fallback for DOM-less cropping.

Used when DOM selectors miss embedded content regions (e.g., T24 terminal
screenshots within a slide image). DOM-based detection via Playwright
element.screenshot() is the primary approach; this is the safety net.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Region detection parameters
MIN_REGION_AREA = 5000  # px^2, skip very small regions
MIN_WIDTH = 80
MIN_HEIGHT = 60
MERGE_DISTANCE = 20  # px, merge regions closer than this


@dataclass
class RegionInfo:
    """A detected content region within an image."""

    x: int
    y: int
    width: int
    height: int
    region_type: str  # "table", "diagram", "screenshot", "text_block"
    confidence: float  # 0.0 to 1.0

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def aspect_ratio(self) -> float:
        return self.width / max(self.height, 1)


class ContentCropper:
    """Detects and crops content regions from page screenshots using OpenCV."""

    def detect_regions(self, image_path: Path) -> List[RegionInfo]:
        """Detect distinct content blocks via contour analysis.

        Steps:
          1. Grayscale + Gaussian blur
          2. Adaptive threshold
          3. Find contours
          4. Filter by minimum area and aspect ratio
          5. Merge overlapping/adjacent regions
          6. Classify by aspect ratio heuristic
        """
        image = cv2.imread(str(image_path))
        if image is None:
            logger.warning(f"Could not read image: {image_path}")
            return []

        h, w = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # Gaussian blur to reduce noise
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # Adaptive threshold to handle varying lighting
        thresh = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 11, 2
        )

        # Dilate to connect nearby features
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 5))
        dilated = cv2.dilate(thresh, kernel, iterations=2)

        # Find contours
        contours, _ = cv2.findContours(
            dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        # Filter and collect regions
        raw_regions: List[RegionInfo] = []
        for contour in contours:
            x, y, rw, rh = cv2.boundingRect(contour)

            # Skip small regions
            if rw * rh < MIN_REGION_AREA or rw < MIN_WIDTH or rh < MIN_HEIGHT:
                continue

            # Skip regions that span nearly the full image (that's the page itself)
            if rw > w * 0.95 and rh > h * 0.90:
                continue

            region_type = self._classify_by_shape(rw, rh)
            confidence = self._estimate_confidence(image, x, y, rw, rh)
            raw_regions.append(RegionInfo(x, y, rw, rh, region_type, confidence))

        # Merge overlapping regions
        merged = self._merge_nearby(raw_regions)

        # Sort by Y position (top to bottom)
        merged.sort(key=lambda r: (r.y, r.x))

        logger.debug(
            f"Detected {len(merged)} regions from {len(contours)} contours "
            f"in {image_path.name}"
        )
        return merged

    def crop_region(
        self, image_path: Path, region: RegionInfo, output_path: Path
    ) -> Path:
        """Extract and save a cropped region from the image."""
        image = cv2.imread(str(image_path))
        if image is None:
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Add small padding
        h, w = image.shape[:2]
        pad = 5
        x1 = max(0, region.x - pad)
        y1 = max(0, region.y - pad)
        x2 = min(w, region.x + region.width + pad)
        y2 = min(h, region.y + region.height + pad)

        crop = image[y1:y2, x1:x2]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), crop)
        logger.debug(f"Cropped region ({region.region_type}) -> {output_path.name}")
        return output_path

    def crop_all_regions(
        self, image_path: Path, output_dir: Path, prefix: str
    ) -> List[tuple]:
        """Detect regions and crop them all.

        Returns list of (output_path, region_type) tuples.
        """
        regions = self.detect_regions(image_path)
        results = []
        for i, region in enumerate(regions, 1):
            out_path = output_dir / f"{prefix}_ocv_crop_{i:02d}_{region.region_type}.png"
            self.crop_region(image_path, region, out_path)
            results.append((out_path, region.region_type))
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_by_shape(width: int, height: int) -> str:
        """Heuristic classification based on aspect ratio."""
        ratio = width / max(height, 1)
        if ratio > 2.5:
            return "table"  # wide and short → likely a table/grid
        elif ratio < 0.5:
            return "text_block"  # tall and narrow → text column
        elif 0.8 < ratio < 1.5 and width > 200:
            return "screenshot"  # roughly square, decent size → app screenshot
        else:
            return "diagram"  # anything else

    @staticmethod
    def _estimate_confidence(
        image: np.ndarray, x: int, y: int, w: int, h: int
    ) -> float:
        """Rough confidence based on edge density in the region."""
        roi = image[y : y + h, x : x + w]
        if roi.size == 0:
            return 0.0
        gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if len(roi.shape) == 3 else roi
        edges = cv2.Canny(gray_roi, 50, 150)
        edge_density = np.count_nonzero(edges) / max(edges.size, 1)
        # Higher edge density → more structured content → higher confidence
        return min(1.0, edge_density * 10)

    @staticmethod
    def _merge_nearby(regions: List[RegionInfo]) -> List[RegionInfo]:
        """Merge regions that overlap or are very close together."""
        if not regions:
            return []

        # Sort by area (largest first) for stable merging
        regions = sorted(regions, key=lambda r: r.area, reverse=True)
        merged: List[RegionInfo] = []

        for region in regions:
            was_merged = False
            for i, existing in enumerate(merged):
                if _regions_overlap(region, existing, MERGE_DISTANCE):
                    # Expand existing to encompass both
                    x1 = min(existing.x, region.x)
                    y1 = min(existing.y, region.y)
                    x2 = max(existing.x + existing.width, region.x + region.width)
                    y2 = max(existing.y + existing.height, region.y + region.height)
                    merged[i] = RegionInfo(
                        x=x1,
                        y=y1,
                        width=x2 - x1,
                        height=y2 - y1,
                        region_type=existing.region_type,  # keep the larger region's type
                        confidence=max(existing.confidence, region.confidence),
                    )
                    was_merged = True
                    break
            if not was_merged:
                merged.append(region)

        return merged


def _regions_overlap(a: RegionInfo, b: RegionInfo, margin: int = 0) -> bool:
    """Check if two regions overlap (with optional margin for near-misses)."""
    return not (
        a.x + a.width + margin < b.x
        or b.x + b.width + margin < a.x
        or a.y + a.height + margin < b.y
        or b.y + b.height + margin < a.y
    )
