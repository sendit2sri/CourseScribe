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
        """Populate course entries from every pathway in targets.json.

        Idempotent:
          - Course missing → add.
          - Course present with empty course_code / pathway_name → backfill from file.
          - Course present with non-empty course_code / pathway_name that differs
            from file → warn, do NOT overwrite (protects resume state).
        """
        for pathway in targets_file.pathways:
            for course_target in pathway.pending_courses:
                existing = self._courses.get(course_target.name)
                if existing is None:
                    dir_name = _build_course_dir_name(course_target.name)
                    entry = CourseEntry(
                        name=course_target.name,
                        course_code=course_target.code,
                        pathway_name=pathway.pathway_name,
                        status=COURSE_PENDING,
                        output_dir=dir_name,
                    )
                    self._courses[course_target.name] = entry
                    self._state["courses"][course_target.name] = entry.to_dict()
                    logger.info(
                        "Added course: %s -> %s/ (pathway=%s)",
                        course_target.name, dir_name, pathway.pathway_name,
                    )
                    continue

                if course_target.code:
                    if not existing.course_code:
                        existing.course_code = course_target.code
                    elif existing.course_code != course_target.code:
                        logger.warning(
                            "Course '%s' already has code '%s' in state; "
                            "targets.json claims '%s' — keeping existing.",
                            course_target.name, existing.course_code, course_target.code,
                        )

                if pathway.pathway_name:
                    if not existing.pathway_name:
                        existing.pathway_name = pathway.pathway_name
                    elif existing.pathway_name != pathway.pathway_name:
                        logger.warning(
                            "Course '%s' already belongs to pathway '%s' in state; "
                            "targets.json claims '%s' — keeping existing.",
                            course_target.name, existing.pathway_name, pathway.pathway_name,
                        )
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
        status_icon = {
            COURSE_COMPLETED: "[done]",
            COURSE_IN_PROGRESS: "[...]",
            COURSE_FAILED: "[FAIL]",
            COURSE_PENDING: "[    ]",
        }

        # Group by pathway, preserving first-appearance order.
        grouped: Dict[str, List[CourseEntry]] = {}
        for entry in self._courses.values():
            bucket = entry.pathway_name or "(legacy)"
            grouped.setdefault(bucket, []).append(entry)

        lines = ["CourseScribe Multi-Course Status", "=" * 50, ""]
        completed = failed = pending = 0

        for pathway_name, entries in grouped.items():
            p_done = sum(1 for e in entries if e.status == COURSE_COMPLETED)
            p_fail = sum(1 for e in entries if e.status == COURSE_FAILED)
            p_pend = len(entries) - p_done - p_fail
            lines.append(f"Pathway: {pathway_name}")
            lines.append(
                f"  Completed: {p_done} | Failed: {p_fail} | Remaining: {p_pend}"
            )
            for entry in entries:
                line = f"  {status_icon.get(entry.status, '[?]')} {entry.name}"
                if entry.total_pages:
                    line += f" ({entry.total_pages} pages)"
                if entry.old_version_redirect:
                    line += " [redirected -> new version]"
                if entry.last_error:
                    line += f" -- {entry.last_error}"
                lines.append(line)
            lines.append("")

            completed += p_done
            failed += p_fail
            pending += p_pend

        lines.append(
            f"Totals — Completed: {completed} | Failed: {failed} | Remaining: {pending}"
        )
        return "\n".join(lines)
