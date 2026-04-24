"""Top-level state tracking across multiple courses in a pathway.

Manages courses_state.json in the base output directory.
Each course entry tracks status, attempt count, errors, and output location.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from automation.config import CourseTarget, TargetsConfig, TargetsFile

logger = logging.getLogger(__name__)

# Course status values
COURSE_PENDING = "pending"
COURSE_IN_PROGRESS = "in_progress"
COURSE_COMPLETED = "completed"
COURSE_FAILED = "failed"


def _now() -> str:
    """ISO format timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def classify_failure(error: str) -> str:
    """Classify a failure error string into a failure type."""
    lower = error.lower()
    if "course link not found" in lower:
        return "course_link_not_found"
    if "open curriculum" in lower and ("not found" in lower or "button" in lower):
        return "open_curriculum_missing"
    if "scroll_into_view" in lower or "element is not visible" in lower or (
        "timeout" in lower and ("scroll" in lower or "view" in lower)
    ):
        return "visibility_or_scroll_timeout"
    if "old version" in lower or "redirected" in lower:
        return "redirected_version"
    return "unknown"


def _build_course_dir_name(course_name: str) -> str:
    """Build a descriptive, filesystem-safe directory name from a course name.

    Converts the full course name to underscored form so that both the
    descriptive title and the course code are visible in the folder name.
    e.g. "Transact Derivatives Administration TR2PRDXA"
      -> "Transact_Derivatives_Administration_TR2PRDXA"
    """
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in course_name)
    safe = "_".join(safe.split())
    return safe[:80]


@dataclass
class CourseEntry:
    """State of a single course in the multi-course pipeline."""

    name: str
    course_code: str = ""
    pathway_name: str = ""
    status: str = COURSE_PENDING
    output_dir: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    attempt_count: int = 0
    last_error: Optional[str] = None
    failure_type: Optional[str] = None
    total_pages: int = 0
    old_version_redirect: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "status": self.status,
            "output_dir": self.output_dir,
            "attempt_count": self.attempt_count,
            "total_pages": self.total_pages,
        }
        if self.course_code:
            d["course_code"] = self.course_code
        if self.pathway_name:
            d["pathway_name"] = self.pathway_name
        if self.started_at:
            d["started_at"] = self.started_at
        if self.completed_at:
            d["completed_at"] = self.completed_at
        if self.last_attempt_at:
            d["last_attempt_at"] = self.last_attempt_at
        if self.last_error:
            d["last_error"] = self.last_error
        if self.failure_type:
            d["failure_type"] = self.failure_type
        if self.old_version_redirect:
            d["old_version_redirect"] = self.old_version_redirect
        return d

    @classmethod
    def from_dict(cls, name: str, d: Dict[str, Any]) -> "CourseEntry":
        return cls(
            name=name,
            course_code=d.get("course_code", ""),
            pathway_name=d.get("pathway_name", ""),
            status=d.get("status", COURSE_PENDING),
            output_dir=d.get("output_dir", ""),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            last_attempt_at=d.get("last_attempt_at"),
            attempt_count=d.get("attempt_count", 0),
            last_error=d.get("last_error"),
            failure_type=d.get("failure_type"),
            total_pages=d.get("total_pages", 0),
            old_version_redirect=d.get("old_version_redirect"),
        )


class CoursesStateManager:
    """Tracks completion state across multiple courses in a pathway."""

    def __init__(self, base_output_dir: Path):
        self.base_dir = base_output_dir
        self.state_path = base_output_dir / "courses_state.json"
        self._state: Dict[str, Any] = self._load_or_create()
        self._courses: Dict[str, CourseEntry] = self._rebuild_index()

    def _load_or_create(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                logger.info(
                    "Loaded courses state: %d courses",
                    len(data.get("courses", {})),
                )
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Corrupt courses state, starting fresh: %s", e)

        return {
            "version": "1.0",
            "pathway_name": "",
            "created_at": _now(),
            "updated_at": _now(),
            "courses": {},
        }

    def _rebuild_index(self) -> Dict[str, CourseEntry]:
        index: Dict[str, CourseEntry] = {}
        for name, data in self._state.get("courses", {}).items():
            index[name] = CourseEntry.from_dict(name, data)
        return index

    def init_from_targets_file(self, targets_file: TargetsFile) -> None:
        """Populate course entries from all pathways in targets.json.

        Idempotent: only adds courses not already present (for resume).
        Backfills course_code and pathway_name on existing entries.
        """
        # Clear the legacy single-pathway key; per-entry pathway_name is authoritative.
        self._state["pathway_name"] = ""
        for pathway in targets_file.pathways:
            self._init_from_pathway(pathway)

    def init_from_targets(self, targets: TargetsConfig) -> None:
        """Single-pathway initializer (kept for callers still operating on one pathway)."""
        self._init_from_pathway(targets)

    def _init_from_pathway(self, targets: TargetsConfig) -> None:
        for course_target in targets.pending_courses:
            if course_target.name not in self._courses:
                dir_name = _build_course_dir_name(course_target.name)
                entry = CourseEntry(
                    name=course_target.name,
                    course_code=course_target.code,
                    pathway_name=targets.pathway_name,
                    status=COURSE_PENDING,
                    output_dir=dir_name,
                )
                self._courses[course_target.name] = entry
                self._state["courses"][course_target.name] = entry.to_dict()
                logger.info(
                    "Added course: %s (%s) -> %s/",
                    course_target.name,
                    targets.pathway_name,
                    dir_name,
                )
            else:
                # Backfill code and pathway on existing entries
                entry = self._courses[course_target.name]
                if not entry.course_code and course_target.code:
                    entry.course_code = course_target.code
                if not entry.pathway_name:
                    entry.pathway_name = targets.pathway_name
                self._sync(course_target.name)

    def get_pending_courses(self) -> List[str]:
        """Return course names not yet completed (pending, in_progress, or failed)."""
        return [
            name
            for name, entry in self._courses.items()
            if entry.status in (COURSE_PENDING, COURSE_IN_PROGRESS, COURSE_FAILED)
        ]

    def get_pending_course_targets(self) -> List[CourseTarget]:
        """Return CourseTarget objects for courses not yet completed."""
        return [
            CourseTarget(name=name, code=entry.course_code)
            for name, entry in self._courses.items()
            if entry.status in (COURSE_PENDING, COURSE_IN_PROGRESS, COURSE_FAILED)
        ]

    def is_course_complete(self, course_name: str) -> bool:
        entry = self._courses.get(course_name)
        return entry is not None and entry.status == COURSE_COMPLETED

    def mark_in_progress(self, course_name: str, output_dir: str) -> None:
        entry = self._courses.get(course_name)
        if not entry:
            return
        entry.status = COURSE_IN_PROGRESS
        entry.output_dir = output_dir
        entry.attempt_count += 1
        entry.last_attempt_at = _now()
        if not entry.started_at:
            entry.started_at = _now()
        entry.last_error = None
        self._sync(course_name)

    def mark_completed(self, course_name: str, total_pages: int) -> None:
        entry = self._courses.get(course_name)
        if not entry:
            return
        entry.status = COURSE_COMPLETED
        entry.completed_at = _now()
        entry.total_pages = total_pages
        entry.last_error = None
        self._sync(course_name)

    def mark_failed(self, course_name: str, error: str, failure_type: Optional[str] = None) -> None:
        entry = self._courses.get(course_name)
        if not entry:
            return
        entry.status = COURSE_FAILED
        entry.last_error = error
        entry.failure_type = failure_type or classify_failure(error)
        entry.last_attempt_at = _now()
        self._sync(course_name)

    def course_output_dir(self, course_name: str) -> str:
        """Get the output directory name for a course."""
        entry = self._courses.get(course_name)
        if entry and entry.output_dir:
            return entry.output_dir
        return _build_course_dir_name(course_name)

    def _sync(self, course_name: str) -> None:
        entry = self._courses.get(course_name)
        if entry:
            self._state["courses"][course_name] = entry.to_dict()

    def save(self) -> None:
        """Persist courses_state.json to disk."""
        self._state["updated_at"] = _now()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug("Courses state saved")

    def summary_text(self) -> str:
        """Human-readable summary of all courses, grouped by pathway."""
        lines = [
            "CourseScribe Multi-Course Status",
            "=" * 50,
        ]

        # Group by pathway, preserving insertion order
        groups: Dict[str, List[CourseEntry]] = {}
        for entry in self._courses.values():
            key = entry.pathway_name or "(unknown pathway)"
            groups.setdefault(key, []).append(entry)

        total_completed = 0
        total_failed = 0
        total_pending = 0

        for pathway_name, entries in groups.items():
            lines.append("")
            lines.append(f"Pathway: {pathway_name}")
            lines.append("-" * 50)

            p_completed = 0
            p_failed = 0
            p_pending = 0

            for entry in entries:
                status_icon = {
                    COURSE_COMPLETED: "[done]",
                    COURSE_IN_PROGRESS: "[...]",
                    COURSE_FAILED: "[FAIL]",
                    COURSE_PENDING: "[    ]",
                }.get(entry.status, "[?]")

                line = f"  {status_icon} {entry.name}"
                if entry.total_pages:
                    line += f" ({entry.total_pages} pages)"
                if entry.old_version_redirect:
                    line += " [redirected -> new version]"
                if entry.last_error:
                    line += f" -- {entry.last_error}"
                lines.append(line)

                if entry.status == COURSE_COMPLETED:
                    p_completed += 1
                elif entry.status == COURSE_FAILED:
                    p_failed += 1
                else:
                    p_pending += 1

            lines.append(
                f"  Completed: {p_completed} | Failed: {p_failed} | Remaining: {p_pending}"
            )
            total_completed += p_completed
            total_failed += p_failed
            total_pending += p_pending

        lines.append("")
        lines.append("=" * 50)
        lines.append(
            f"TOTAL — Completed: {total_completed} | Failed: {total_failed}"
            f" | Remaining: {total_pending}"
        )
        return "\n".join(lines)
