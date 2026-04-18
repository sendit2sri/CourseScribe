"""CSS selector profiles for course platform navigation.

Selectors are organized by role and use comma-separated fallback chains.
The navigator tries each selector in order; first match wins.
Override any selector via --selectors-file pointing to a JSON file.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default selector profile -- comma-separated fallback chains per role.
DEFAULT_SELECTORS: Dict[str, str] = {
    # Navigation structure
    "module_list": ".module-list, nav[role='navigation'] ul, .course-outline, .curriculum",
    "module_item": ".module-item, .course-module, [data-module], .section-item",
    "module_name": ".module-title, .module-name, h2, .section-title",
    "lesson_list": ".lesson-list, .module-content ul, .section-content ul",
    "lesson_item": ".lesson-item, .lesson-link, [data-lesson], .topic-item",
    "lesson_name": ".lesson-title, .lesson-name, h3, .topic-title",

    # Page content
    "page_title": "h1, .page-title, .lesson-title, .content-title",
    "main_content": "main, .content, .lesson-content, #content, article, .page-content",
    "next_button": (
        ".uikit-primary-button_next, "  # Platform-specific (highest priority)
        "button:has-text('Next'), a:has-text('Next'), "
        ".next-btn, [data-action='next'], "
        "button:has-text('Continue'), a:has-text('Continue'), "
        ".btn-next, .next-page"
    ),
    "prev_button": (
        "button:has-text('Previous'), a:has-text('Previous'), "
        ".prev-btn, [data-action='prev'], "
        "button:has-text('Back'), a:has-text('Back')"
    ),

    # Page position indicator (e.g., "Page 3 of 12")
    "page_indicator": ".page-indicator, .page-count, .pagination-info, .progress-text",

    # Loading spinners / busy indicators
    "loading_spinner": ".spinner, .loading, [aria-busy='true'], .loader",

    # Expandable content
    "accordion_closed": "[aria-expanded='false'], details:not([open]), .collapsed",
    "tab_inactive": "[role='tab']:not([aria-selected='true']), .tab:not(.active)",

    # Content regions for cropping
    "tables": "table, .data-table, .report-table, .grid-table",
    "diagrams": ".diagram, .workflow, svg:not([width='0']), canvas, .mermaid, .flowchart",
    "screenshots": ".screenshot, .t24-screen, img[alt*='screen'], img[alt*='Screen'], .app-screenshot",

    # Login detection
    "logged_in_indicator": ".user-menu, .profile-icon, .logged-in, .user-avatar, .account-menu",

    # Course completion / end detection
    "course_complete": ".course-complete, .completion-message, .certificate-link",

    # ---- Portal / Pathway navigation ----
    "pathways_box": "[id*='boxTitle']:has-text('Pathways'), #boxTitle4, [class*='box-title']:has-text('Pathways'), .widget-title:has-text('Pathways')",
    "pathway_tab_prefix": "[id^='tabToolTip'], [id^='tabTitle']",
    "pathway_dropdown_toggle": ".fa.fa-angle-down, .fa.fa-angle-up, [class*='fa-angle']",
    "pathway_course_table": "[id^='pathwayTable-']",

    # ---- Course launch sequence ----
    "old_version_banner": 'span:has-text("Old Version")',
    "old_version_link": (
        'p[data-testid="LD_Call_To_Action_Instructions"] a[href*="lms-learning-details"],'
        'a[href*="lms-learning-details"]'
    ),
    "open_curriculum_button": (
        'button[data-testid="rcl$duplexedButton__primaryButton"]:has-text("Open Curriculum"),'
        'button:has-text("Open Curriculum")'
    ),
    "launch_button": (
        'button[data-testid="rcl$duplexedButton__primaryButton"]:has-text("Launch")'
    ),
    "fullscreen_button": '[data-testid="MinimizeIcon"], [aria-label="View Full Screen Mode"]',
    "dismiss_resume_no": '.uikit-primary-button:has-text("No"), button:has-text("No"), a:has-text("No")',
    "exit_course_button": (
        'button[data-testid="rcl$duplexedButton__primaryButton"]:has-text("Exit Course")'
    ),

    # ---- In-course platform-specific ----
    "chapter_root_title": ".titlesNew h1, .titlesNew",
    "chapter_item_title": ".titles div, .titles",

    # ---- Curriculum sidebar extraction ----
    "curriculum_sidebar": ".sidebarGenericPlayerMFE, .toc-container, [id='rcl$sidePanel__main']",
    "curriculum_title": "h1.titleName",
    "curriculum_progress_pct": ".curriculumProgressPercentage",
    "curriculum_progress_count": '[data-testid="$rcl-baseElement"]',
    "curriculum_status": ".curriculumSummaryStatus",
    "curriculum_duration": '[data-testid="curriculumPlayer$totalDuration_Value"]',
    "curriculum_tree_item": '[role="treeitem"][aria-level="2"]',
    "curriculum_item_title": ".titles",
    "curriculum_item_completed": 'lego-icon[data-icon-name="circle-check"]',
    "curriculum_item_in_progress": 'lego-icon[data-icon-name="circle-50"]',

    # ---- Content frame detection ----
    "content_iframe": "iframe[src*='course'], iframe[name*='content'], iframe.course-frame",
}


class SelectorProfile:
    """Manages CSS selectors with fallback chains and override support."""

    def __init__(self, overrides: Optional[Dict[str, str]] = None):
        self._selectors: Dict[str, str] = dict(DEFAULT_SELECTORS)
        if overrides:
            self._selectors.update(overrides)

    @classmethod
    def from_file(cls, path: Path) -> "SelectorProfile":
        """Load selector overrides from a JSON file."""
        try:
            with open(path) as f:
                overrides = json.load(f)
            logger.info(f"Loaded {len(overrides)} selector overrides from {path}")
            return cls(overrides=overrides)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load selectors file {path}: {e}")
            raise

    def get(self, role: str) -> str:
        """Get the full selector string for a role."""
        return self._selectors.get(role, "")

    def get_chain(self, role: str) -> List[str]:
        """Get individual selectors as a list (split on commas)."""
        raw = self._selectors.get(role, "")
        return [s.strip() for s in raw.split(",") if s.strip()]

    def set(self, role: str, selector: str) -> None:
        """Override a selector for a role."""
        self._selectors[role] = selector

    @property
    def all_roles(self) -> List[str]:
        """List all registered selector roles."""
        return list(self._selectors.keys())

    def to_dict(self) -> Dict[str, str]:
        """Export all selectors as a dictionary."""
        return dict(self._selectors)
