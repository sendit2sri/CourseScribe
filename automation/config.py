"""Configuration dataclass and validation for the automation tool."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_DATA_DIR = Path.home() / ".coursescribe" / "browser_profile"
DEFAULT_OUTPUT_DIR = Path("course_capture")
DEFAULT_PAGE_DELAY = 1.0
DEFAULT_VIEWPORT_WIDTH = 1920
DEFAULT_VIEWPORT_HEIGHT = 1080
DEFAULT_STABLE_WAIT_MS = 30000
DEFAULT_MUTATION_QUIET_MS = 500

# Minimum OCR char count below which a page is flagged for review
LOW_QUALITY_CHAR_THRESHOLD = 50


@dataclass
class AutomationConfig:
    """All settings for a CourseScribe automation run."""

    # Browser
    browser_data_dir: Path = field(default_factory=lambda: DEFAULT_BROWSER_DATA_DIR)
    headless: bool = False
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH
    viewport_height: int = DEFAULT_VIEWPORT_HEIGHT

    # Course
    start_url: str = ""
    start_module: int = 1
    start_lesson: int = 1

    # Capture
    capture_mode: str = "full"  # "full" | "viewport" | "section"
    enable_crops: bool = False

    # OCR / AI
    ai_provider: str = "openai"
    model: Optional[str] = None
    content_type: str = "course"  # "course" | "presentation" | "technical"
    enable_cost_tracking: bool = False

    # Operation mode
    capture_only: bool = False
    ocr_only: bool = False
    login_mode: bool = False
    dry_run: bool = False

    # Output
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)

    # Selectors
    selectors_file: Optional[Path] = None

    # Timing
    page_delay: float = DEFAULT_PAGE_DELAY
    stable_wait_ms: int = DEFAULT_STABLE_WAIT_MS
    mutation_quiet_ms: int = DEFAULT_MUTATION_QUIET_MS

    # Quality
    low_quality_char_threshold: int = LOW_QUALITY_CHAR_THRESHOLD

    # Logging
    log_level: str = "INFO"
    log_file: Optional[Path] = None

    def validate(self) -> List[str]:
        """Return a list of validation error messages. Empty list means valid."""
        errors: List[str] = []

        if not self.login_mode and not self.ocr_only and not self.start_url:
            errors.append("--start-url is required (unless using --login or --ocr-only)")

        if self.capture_mode not in ("full", "viewport", "section"):
            errors.append(f"Invalid capture mode: {self.capture_mode}. Must be full, viewport, or section")

        if self.ai_provider not in ("openai", "anthropic"):
            errors.append(f"Invalid AI provider: {self.ai_provider}. Must be openai or anthropic")

        if self.content_type not in ("course", "presentation", "technical"):
            errors.append(f"Invalid content type: {self.content_type}")

        if self.capture_only and self.ocr_only:
            errors.append("Cannot use both --capture-only and --ocr-only")

        if self.page_delay < 0:
            errors.append("--page-delay must be non-negative")

        if self.selectors_file and not self.selectors_file.exists():
            errors.append(f"Selectors file not found: {self.selectors_file}")

        return errors

    def setup_logging(self) -> None:
        """Configure logging based on config settings."""
        level = getattr(logging, self.log_level.upper(), logging.INFO)
        handlers: list = [logging.StreamHandler()]

        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(str(self.log_file)))

        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=handlers,
            force=True,
        )

    def resolve_paths(self) -> None:
        """Resolve relative paths to absolute."""
        self.output_dir = self.output_dir.resolve()
        self.browser_data_dir = self.browser_data_dir.resolve()
        if self.selectors_file:
            self.selectors_file = self.selectors_file.resolve()
