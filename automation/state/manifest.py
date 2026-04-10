"""Manifest and run-state management for resumable course capture.

Two JSON files are maintained:
  manifest.json  -- Course structure (discovered pages, append-only)
  run_state.json -- Processing progress (updated after every page)
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Page status lifecycle
STATUS_DISCOVERED = "discovered"
STATUS_CAPTURED = "captured"
STATUS_CROP_GENERATED = "crop_generated"
STATUS_RAW_EXTRACTED = "raw_extracted"
STATUS_CLEANED = "cleaned"
STATUS_FAILED_CAPTURE = "failed_capture"
STATUS_FAILED_PROCESSING = "failed_processing"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_SKIPPED = "skipped"

TERMINAL_SUCCESS_STATES = {STATUS_CLEANED, STATUS_SKIPPED}
TERMINAL_FAILURE_STATES = {STATUS_FAILED_CAPTURE, STATUS_FAILED_PROCESSING}
CAPTURABLE_STATES = {STATUS_DISCOVERED, STATUS_FAILED_CAPTURE}
PROCESSABLE_STATES = {STATUS_CAPTURED, STATUS_CROP_GENERATED, STATUS_FAILED_PROCESSING}


@dataclass
class PageInfo:
    """Metadata for a single course page."""

    url: str
    module_name: str
    module_index: int
    lesson_name: str
    lesson_index: int
    page_title: str
    page_index: int  # within the lesson

    @property
    def page_id(self) -> str:
        return f"mod{self.module_index:02d}_les{self.lesson_index:02d}_pg{self.page_index:03d}"

    @property
    def module_dir_name(self) -> str:
        safe = _sanitize_name(self.module_name)
        return f"module_{self.module_index:02d}_{safe}"

    @property
    def lesson_dir_name(self) -> str:
        safe = _sanitize_name(self.lesson_name)
        return f"lesson_{self.lesson_index:02d}_{safe}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_id": self.page_id,
            "url": self.url,
            "module_name": self.module_name,
            "module_index": self.module_index,
            "lesson_name": self.lesson_name,
            "lesson_index": self.lesson_index,
            "page_title": self.page_title,
            "page_index": self.page_index,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PageInfo":
        return cls(
            url=d["url"],
            module_name=d["module_name"],
            module_index=d["module_index"],
            lesson_name=d["lesson_name"],
            lesson_index=d["lesson_index"],
            page_title=d["page_title"],
            page_index=d["page_index"],
        )


@dataclass
class PageState:
    """Tracks the processing state of a single page."""

    status: str = STATUS_DISCOVERED
    url: str = ""
    page_title: str = ""
    screenshot_hash: str = ""
    dom_text_hash: str = ""
    screenshot_path: str = ""
    raw_text_path: str = ""
    cleaned_path: str = ""
    content_type: str = ""
    crops: List[str] = field(default_factory=list)
    ocr_char_count: int = 0
    suspected_low_quality: bool = False
    review_reason: Optional[str] = None
    captured_at: Optional[str] = None
    processed_at: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "status": self.status,
            "url": self.url,
            "page_title": self.page_title,
        }
        if self.screenshot_hash:
            d["screenshot_hash"] = self.screenshot_hash
        if self.dom_text_hash:
            d["dom_text_hash"] = self.dom_text_hash
        if self.screenshot_path:
            d["screenshot_path"] = self.screenshot_path
        if self.raw_text_path:
            d["raw_text_path"] = self.raw_text_path
        if self.cleaned_path:
            d["cleaned_path"] = self.cleaned_path
        if self.content_type:
            d["content_type"] = self.content_type
        if self.crops:
            d["crops"] = self.crops
        if self.ocr_char_count:
            d["ocr_char_count"] = self.ocr_char_count
        if self.suspected_low_quality:
            d["suspected_low_quality"] = True
            d["review_reason"] = self.review_reason
        if self.captured_at:
            d["captured_at"] = self.captured_at
        if self.processed_at:
            d["processed_at"] = self.processed_at
        if self.error:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PageState":
        return cls(
            status=d.get("status", STATUS_DISCOVERED),
            url=d.get("url", ""),
            page_title=d.get("page_title", ""),
            screenshot_hash=d.get("screenshot_hash", ""),
            dom_text_hash=d.get("dom_text_hash", ""),
            screenshot_path=d.get("screenshot_path", ""),
            raw_text_path=d.get("raw_text_path", ""),
            cleaned_path=d.get("cleaned_path", ""),
            content_type=d.get("content_type", ""),
            crops=d.get("crops", []),
            ocr_char_count=d.get("ocr_char_count", 0),
            suspected_low_quality=d.get("suspected_low_quality", False),
            review_reason=d.get("review_reason"),
            captured_at=d.get("captured_at"),
            processed_at=d.get("processed_at"),
            error=d.get("error"),
        )


class ManifestManager:
    """Manages manifest.json (course structure) and run_state.json (progress).

    Both files are persisted after every page operation for crash safety.
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.manifest_path = output_dir / "manifest.json"
        self.state_path = output_dir / "run_state.json"

        self._manifest: Dict[str, Any] = self._load_or_create_manifest()
        self._state: Dict[str, Any] = self._load_or_create_state()
        self._pages: Dict[str, PageInfo] = self._rebuild_page_index()
        self._page_states: Dict[str, PageState] = self._rebuild_state_index()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _load_or_create_manifest(self) -> Dict[str, Any]:
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                logger.info(f"Loaded manifest with {data.get('total_pages', 0)} pages")
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Corrupt manifest, starting fresh: {e}")
        return {
            "version": "1.0",
            "course_url": "",
            "created_at": _now(),
            "updated_at": _now(),
            "modules": [],
            "total_pages": 0,
        }

    def _load_or_create_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                logger.info(
                    f"Loaded run state: {data.get('progress', {}).get('captured', 0)} captured, "
                    f"{data.get('progress', {}).get('processed', 0)} processed"
                )
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Corrupt run state, starting fresh: {e}")
        return {
            "version": "1.0",
            "run_id": time.strftime("%Y%m%d_%H%M%S"),
            "started_at": _now(),
            "updated_at": _now(),
            "config": {},
            "progress": {
                "total_pages": 0,
                "captured": 0,
                "processed": 0,
                "failed": 0,
                "needs_review": 0,
                "remaining": 0,
            },
            "current_position": {
                "module_index": 0,
                "lesson_index": 0,
                "page_index": 0,
            },
            "pages": {},
            "cost": {
                "total_cost": 0.0,
                "total_requests": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
            },
        }

    def _rebuild_page_index(self) -> Dict[str, PageInfo]:
        """Build a flat page_id -> PageInfo index from the manifest."""
        index: Dict[str, PageInfo] = {}
        for module in self._manifest.get("modules", []):
            for lesson in module.get("lessons", []):
                for page in lesson.get("pages", []):
                    info = PageInfo(
                        url=page.get("url", ""),
                        module_name=module.get("name", ""),
                        module_index=module.get("index", 0),
                        lesson_name=lesson.get("name", ""),
                        lesson_index=lesson.get("index", 0),
                        page_title=page.get("title", ""),
                        page_index=page.get("index", 0),
                    )
                    index[info.page_id] = info
        return index

    def _rebuild_state_index(self) -> Dict[str, PageState]:
        """Build a flat page_id -> PageState index from run_state."""
        index: Dict[str, PageState] = {}
        for page_id, state_dict in self._state.get("pages", {}).items():
            index[page_id] = PageState.from_dict(state_dict)
        return index

    # ------------------------------------------------------------------
    # Page registration
    # ------------------------------------------------------------------

    def set_course_url(self, url: str) -> None:
        self._manifest["course_url"] = url

    def set_config(self, config_dict: Dict[str, Any]) -> None:
        self._state["config"] = config_dict

    def add_page(self, page_info: PageInfo) -> None:
        """Register a discovered page in the manifest."""
        pid = page_info.page_id
        if pid in self._pages:
            return  # already registered

        self._pages[pid] = page_info

        # Ensure module exists in manifest
        module = self._find_or_create_module(
            page_info.module_index, page_info.module_name
        )
        # Ensure lesson exists
        lesson = self._find_or_create_lesson(
            module, page_info.lesson_index, page_info.lesson_name
        )
        # Add page
        lesson["pages"].append(
            {
                "page_id": pid,
                "index": page_info.page_index,
                "url": page_info.url,
                "title": page_info.page_title,
            }
        )
        self._manifest["total_pages"] = len(self._pages)
        self._manifest["updated_at"] = _now()

        # Initialize page state
        if pid not in self._page_states:
            self._page_states[pid] = PageState(
                status=STATUS_DISCOVERED,
                url=page_info.url,
                page_title=page_info.page_title,
            )
            self._state["pages"][pid] = self._page_states[pid].to_dict()

        self._update_progress()

    def _find_or_create_module(self, index: int, name: str) -> Dict[str, Any]:
        for mod in self._manifest["modules"]:
            if mod["index"] == index:
                return mod
        mod = {"index": index, "name": name, "lessons": []}
        self._manifest["modules"].append(mod)
        self._manifest["modules"].sort(key=lambda m: m["index"])
        return mod

    def _find_or_create_lesson(
        self, module: Dict[str, Any], index: int, name: str
    ) -> Dict[str, Any]:
        for les in module["lessons"]:
            if les["index"] == index:
                return les
        les = {"index": index, "name": name, "pages": []}
        module["lessons"].append(les)
        module["lessons"].sort(key=lambda l: l["index"])
        return les

    # ------------------------------------------------------------------
    # Status updates
    # ------------------------------------------------------------------

    def mark_captured(
        self,
        page_id: str,
        screenshot_path: str,
        screenshot_hash: str,
        dom_text_hash: str = "",
        crops: Optional[List[str]] = None,
    ) -> None:
        """Mark a page as captured with screenshot details."""
        state = self._get_or_create_state(page_id)
        state.status = STATUS_CROP_GENERATED if crops else STATUS_CAPTURED
        state.screenshot_path = screenshot_path
        state.screenshot_hash = screenshot_hash
        state.dom_text_hash = dom_text_hash
        state.captured_at = _now()
        state.crops = crops or []
        state.error = None
        self._sync_state(page_id)

    def mark_processed(
        self,
        page_id: str,
        raw_text_path: str,
        cleaned_path: str,
        content_type: str,
        ocr_char_count: int,
        low_quality: bool = False,
        review_reason: Optional[str] = None,
    ) -> None:
        """Mark a page as fully processed (OCR + AI cleaned)."""
        state = self._get_or_create_state(page_id)
        state.status = STATUS_NEEDS_REVIEW if low_quality else STATUS_CLEANED
        state.raw_text_path = raw_text_path
        state.cleaned_path = cleaned_path
        state.content_type = content_type
        state.ocr_char_count = ocr_char_count
        state.suspected_low_quality = low_quality
        state.review_reason = review_reason
        state.processed_at = _now()
        state.error = None
        self._sync_state(page_id)

    def mark_skipped(self, page_id: str, reason: str) -> None:
        """Mark a page as intentionally skipped (e.g., Course Document page).

        The page's url and page_title are already stored from add_page().
        """
        state = self._get_or_create_state(page_id)
        state.status = STATUS_SKIPPED
        state.review_reason = reason
        state.captured_at = _now()
        state.error = None
        self._sync_state(page_id)

    def mark_failed(self, page_id: str, phase: str, error: str) -> None:
        """Record a failure for a page.

        Args:
            phase: "capture" or "processing"
            error: error message
        """
        state = self._get_or_create_state(page_id)
        state.status = (
            STATUS_FAILED_CAPTURE if phase == "capture" else STATUS_FAILED_PROCESSING
        )
        state.error = error
        self._sync_state(page_id)

    def update_position(self, page_info: PageInfo) -> None:
        """Update current position in the course."""
        self._state["current_position"] = {
            "module_index": page_info.module_index,
            "lesson_index": page_info.lesson_index,
            "page_index": page_info.page_index,
        }

    def update_cost(
        self, cost: float, requests: int, input_tokens: int, output_tokens: int
    ) -> None:
        """Accumulate cost data."""
        c = self._state["cost"]
        c["total_cost"] += cost
        c["total_requests"] += requests
        c["total_input_tokens"] += input_tokens
        c["total_output_tokens"] += output_tokens

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_page_info(self, page_id: str) -> Optional[PageInfo]:
        return self._pages.get(page_id)

    def get_page_state(self, page_id: str) -> Optional[PageState]:
        return self._page_states.get(page_id)

    def get_all_pages(self) -> List[PageInfo]:
        """All discovered pages, sorted by module/lesson/page index."""
        return sorted(
            self._pages.values(),
            key=lambda p: (p.module_index, p.lesson_index, p.page_index),
        )

    def get_uncaptured_pages(self) -> List[PageInfo]:
        """Pages that need (re-)capturing."""
        result = []
        for pid, info in self._pages.items():
            state = self._page_states.get(pid)
            if state is None or state.status in CAPTURABLE_STATES:
                result.append(info)
        return sorted(result, key=lambda p: (p.module_index, p.lesson_index, p.page_index))

    def get_unprocessed_pages(self) -> List[PageInfo]:
        """Pages that are captured but not yet processed."""
        result = []
        for pid, info in self._pages.items():
            state = self._page_states.get(pid)
            if state and state.status in PROCESSABLE_STATES:
                result.append(info)
        return sorted(result, key=lambda p: (p.module_index, p.lesson_index, p.page_index))

    def get_review_pages(self) -> List[PageInfo]:
        """Pages flagged as needing review."""
        result = []
        for pid, info in self._pages.items():
            state = self._page_states.get(pid)
            if state and (state.status == STATUS_NEEDS_REVIEW or state.suspected_low_quality):
                result.append(info)
        return sorted(result, key=lambda p: (p.module_index, p.lesson_index, p.page_index))

    def get_failed_pages(self) -> List[PageInfo]:
        """Pages that failed capture or processing."""
        result = []
        for pid, info in self._pages.items():
            state = self._page_states.get(pid)
            if state and state.status in TERMINAL_FAILURE_STATES:
                result.append(info)
        return sorted(result, key=lambda p: (p.module_index, p.lesson_index, p.page_index))

    def get_resume_position(self) -> Optional[PageInfo]:
        """Find the first uncaptured or unprocessed page to resume from."""
        uncaptured = self.get_uncaptured_pages()
        if uncaptured:
            return uncaptured[0]
        unprocessed = self.get_unprocessed_pages()
        if unprocessed:
            return unprocessed[0]
        return None

    def is_page_captured(self, page_id: str) -> bool:
        state = self._page_states.get(page_id)
        return state is not None and state.status not in CAPTURABLE_STATES

    def is_url_visited(self, url: str) -> bool:
        """Check if a URL has already been captured (loop detection)."""
        for state in self._page_states.values():
            if state.url == url and state.status not in CAPTURABLE_STATES:
                return True
        return False

    @property
    def total_pages(self) -> int:
        return len(self._pages)

    @property
    def progress(self) -> Dict[str, int]:
        return dict(self._state.get("progress", {}))

    @property
    def cost(self) -> Dict[str, Any]:
        return dict(self._state.get("cost", {}))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist both manifest and run_state to disk. Call after every page."""
        self._update_progress()
        self._state["updated_at"] = _now()
        self._manifest["updated_at"] = _now()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self._manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.state_path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.debug("State saved to disk")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def compute_image_hash(image_path: Path) -> str:
        """SHA-256 hash of an image file."""
        h = hashlib.sha256()
        with open(image_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"

    @staticmethod
    def compute_text_hash(text: str) -> str:
        """SHA-256 hash of a text string."""
        return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"

    def _get_or_create_state(self, page_id: str) -> PageState:
        if page_id not in self._page_states:
            self._page_states[page_id] = PageState()
        return self._page_states[page_id]

    def _sync_state(self, page_id: str) -> None:
        """Sync a single page's state back to the state dict."""
        state = self._page_states.get(page_id)
        if state:
            self._state["pages"][page_id] = state.to_dict()

    def _update_progress(self) -> None:
        """Recompute progress counters from page states."""
        total = len(self._pages)
        captured = 0
        processed = 0
        failed = 0
        needs_review = 0
        skipped = 0

        for state in self._page_states.values():
            if state.status in (
                STATUS_CAPTURED, STATUS_CROP_GENERATED,
                STATUS_RAW_EXTRACTED, STATUS_CLEANED, STATUS_NEEDS_REVIEW,
            ):
                captured += 1
            if state.status in (STATUS_CLEANED, STATUS_NEEDS_REVIEW):
                processed += 1
            if state.status == STATUS_SKIPPED:
                skipped += 1
            if state.status in TERMINAL_FAILURE_STATES:
                failed += 1
            if state.status == STATUS_NEEDS_REVIEW or state.suspected_low_quality:
                needs_review += 1

        # Skipped pages count toward "done" for remaining calculation
        done = processed + skipped + failed
        self._state["progress"] = {
            "total_pages": total,
            "captured": captured,
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "needs_review": needs_review,
            "remaining": total - done,
        }

    def summary_text(self) -> str:
        """Human-readable progress summary."""
        p = self._state.get("progress", {})
        c = self._state.get("cost", {})
        lines = [
            "CourseScribe Automation Status",
            "=" * 40,
            f"Total pages:   {p.get('total_pages', 0)}",
            f"Captured:      {p.get('captured', 0)}",
            f"Processed:     {p.get('processed', 0)}",
            f"Skipped:       {p.get('skipped', 0)}",
            f"Failed:        {p.get('failed', 0)}",
            f"Needs review:  {p.get('needs_review', 0)}",
            f"Remaining:     {p.get('remaining', 0)}",
            "",
            f"Total cost:    ${c.get('total_cost', 0):.4f}",
            f"API requests:  {c.get('total_requests', 0)}",
        ]
        pos = self._state.get("current_position", {})
        if pos.get("module_index"):
            lines.append(
                f"Last position: Module {pos['module_index']}, "
                f"Lesson {pos['lesson_index']}, "
                f"Page {pos['page_index']}"
            )
        return "\n".join(lines)


def _now() -> str:
    """ISO format timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sanitize_name(name: str) -> str:
    """Make a name safe for use as a directory name."""
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)
    safe = "_".join(safe.split())  # collapse whitespace
    return safe[:60]  # limit length
