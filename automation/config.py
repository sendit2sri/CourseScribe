"""Configuration dataclass and validation for the automation tool.

Settings are resolved in priority order:
  1. Explicit CLI arguments (highest priority)
  2. Environment variables / .env file
  3. Dataclass defaults (lowest priority)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

@dataclass
class CourseTarget:
    """A single course entry from targets.json."""

    name: str
    code: str = ""
    url: str = ""
    needs_manual_enrollment: bool = False


@dataclass
class TargetsConfig:
    """A single pathway block from targets.json."""

    pathway_name: str
    pending_courses: List[CourseTarget]
    category: str = ""
    skip_titles: List[str] = field(default_factory=lambda: ["Course Document"])


@dataclass
class TargetsFile:
    """Parsed contents of targets.json — one or more pathway blocks."""

    pathways: List[TargetsConfig]


def _parse_targets_config(entry: object, source: Path, index: int) -> TargetsConfig:
    """Parse a single pathway block into a TargetsConfig. Raises ValueError on bad shape."""
    if not isinstance(entry, dict):
        raise ValueError(
            f"Pathway entry #{index} in {source} must be an object, got {type(entry).__name__}"
        )

    pathway_name = entry.get("pathway_name", "")
    if not pathway_name:
        raise ValueError(
            f"Pathway entry #{index} in {source} is missing 'pathway_name'"
        )

    raw_courses = entry.get("pending_courses")
    if raw_courses is None:
        raise ValueError(
            f"Pathway '{pathway_name}' in {source} is missing 'pending_courses'"
        )
    if not isinstance(raw_courses, list) or not raw_courses:
        raise ValueError(
            f"Pathway '{pathway_name}' in {source} has empty or invalid 'pending_courses'"
        )

    pending_courses = [
        CourseTarget(
            name=c["name"],
            code=c.get("code", ""),
            url=c.get("url", ""),
            needs_manual_enrollment=c.get("needs_manual_enrollment", False),
        )
        if isinstance(c, dict)
        else CourseTarget(name=c)
        for c in raw_courses
    ]

    return TargetsConfig(
        pathway_name=pathway_name,
        pending_courses=pending_courses,
        category=entry.get("category", ""),
        skip_titles=entry.get("skip_titles", ["Course Document"]),
    )


def _validate_pathways(pathways: List[TargetsConfig], source: Path) -> None:
    """Emit warnings for duplicate codes and names; no hard rejection here."""
    # 1) Within a pathway: duplicate course_code
    for p in pathways:
        seen_codes: set = set()
        for c in p.pending_courses:
            if c.code and c.code in seen_codes:
                logger.warning(
                    "Duplicate course code '%s' within pathway '%s' in %s",
                    c.code, p.pathway_name, source,
                )
            if c.code:
                seen_codes.add(c.code)

    # 2) Across whole file: duplicate course_name (state key + output dir collide)
    name_counts: dict = {}
    for p in pathways:
        for c in p.pending_courses:
            name_counts[c.name] = name_counts.get(c.name, 0) + 1
    for name, count in name_counts.items():
        if count > 1:
            logger.warning(
                "Duplicate course name '%s' appears %d times across %s; "
                "state entries and output directories will collide",
                name, count, source,
            )

    # 3) Across pathways: duplicate (pathway_name, code) pair
    pair_counts: dict = {}
    for p in pathways:
        for c in p.pending_courses:
            if c.code:
                key = (p.pathway_name, c.code)
                pair_counts[key] = pair_counts.get(key, 0) + 1
    for (pname, code), count in pair_counts.items():
        if count > 1:
            logger.warning(
                "Pathway '%s' contains code '%s' %d times in %s "
                "(likely a duplicated pathway block)",
                pname, code, count, source,
            )


DEFAULT_BROWSER_DATA_DIR = Path.home() / ".coursescribe" / "browser_profile"
DEFAULT_OUTPUT_DIR = Path("course_capture")
DEFAULT_PAGE_DELAY = 3.0
DEFAULT_VIEWPORT_WIDTH = 1920
DEFAULT_VIEWPORT_HEIGHT = 1080
DEFAULT_STABLE_WAIT_MS = 30000
DEFAULT_MUTATION_QUIET_MS = 500
DEFAULT_IDLE_PAUSE_INTERVAL_MIN = 15
DEFAULT_IDLE_PAUSE_INTERVAL_MAX = 30
DEFAULT_IDLE_PAUSE_DURATION_MIN = 120.0
DEFAULT_IDLE_PAUSE_DURATION_MAX = 300.0

# Minimum OCR char count below which a page is flagged for review
LOW_QUALITY_CHAR_THRESHOLD = 50


def load_env_config() -> None:
    """Load .env file. Searches cwd then project root."""
    # Try .env in current dir first, then parent dirs
    load_dotenv(override=False)


@dataclass
class AutomationConfig:
    """All settings for a CourseScribe automation run."""

    # Browser
    browser_data_dir: Path = field(default_factory=lambda: DEFAULT_BROWSER_DATA_DIR)
    headless: bool = False
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH
    viewport_height: int = DEFAULT_VIEWPORT_HEIGHT

    # Course URLs
    start_url: str = ""
    login_url: str = ""
    start_module: int = 1
    start_lesson: int = 1

    # Credentials (loaded from .env, never from CLI args)
    login_username: str = ""
    login_password: str = ""

    # Capture
    capture_mode: str = "full"  # "full" | "viewport" | "section"
    enable_crops: bool = False

    # OCR / AI
    ai_provider: str = "openai"
    model: Optional[str] = None
    content_type: str = "course"  # "course" | "presentation" | "technical"
    enable_cost_tracking: bool = False
    vision_mode: bool = True  # True = send full screenshot directly to AI

    # Operation mode
    capture_only: bool = False
    ocr_only: bool = False
    login_mode: bool = False
    dry_run: bool = False

    # Output
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)

    # Selectors
    selectors_file: Optional[Path] = None

    # Multi-course
    targets_file: Optional[Path] = None
    multi_course_mode: bool = False

    # Timing
    page_delay: float = DEFAULT_PAGE_DELAY
    stable_wait_ms: int = DEFAULT_STABLE_WAIT_MS
    mutation_quiet_ms: int = DEFAULT_MUTATION_QUIET_MS

    # Pacing — idle reading breaks
    idle_pause_interval_min: int = DEFAULT_IDLE_PAUSE_INTERVAL_MIN
    idle_pause_interval_max: int = DEFAULT_IDLE_PAUSE_INTERVAL_MAX
    idle_pause_duration_min: float = DEFAULT_IDLE_PAUSE_DURATION_MIN
    idle_pause_duration_max: float = DEFAULT_IDLE_PAUSE_DURATION_MAX

    # Pacing — session batching
    batch_size: int = 0  # 0 = disabled; auto-stop after N pages

    # Quality
    low_quality_char_threshold: int = LOW_QUALITY_CHAR_THRESHOLD

    # Logging
    log_level: str = "INFO"
    log_file: Optional[Path] = None

    def load_from_env(self) -> None:
        """Load settings from environment variables.

        Called after construction but before CLI overrides, so CLI wins.
        Credentials are ONLY loaded from env (never from CLI args).
        """
        load_env_config()

        # Credentials — only from .env, never CLI
        self.login_username = os.getenv("COURSESCRIBE_USERNAME", self.login_username)
        self.login_password = os.getenv("COURSESCRIBE_PASSWORD", self.login_password)

        # URLs — .env provides defaults, CLI can override
        if not self.login_url:
            self.login_url = os.getenv("COURSESCRIBE_LOGIN_URL", "")
        if not self.start_url:
            self.start_url = os.getenv("COURSESCRIBE_START_URL", "")

        # Browser profile — .env default, CLI can override
        env_profile = os.getenv("COURSESCRIBE_BROWSER_PROFILE", "")
        if env_profile and self.browser_data_dir == DEFAULT_BROWSER_DATA_DIR:
            self.browser_data_dir = Path(env_profile).expanduser()

    @property
    def has_credentials(self) -> bool:
        """True if login credentials are available from .env."""
        return bool(self.login_username and self.login_password)

    @property
    def effective_login_url(self) -> str:
        """The URL to use for login: login_url if set, else start_url."""
        return self.login_url or self.start_url

    def masked_username(self) -> str:
        """Return partially masked username for safe logging."""
        u = self.login_username
        if not u:
            return "(none)"
        if len(u) <= 4:
            return u[0] + "***"
        return u[:2] + "***" + u[-2:]

    def validate(self) -> List[str]:
        """Return a list of validation error messages. Empty list means valid."""
        errors: List[str] = []

        # start_url is not required in multi-course mode (portal URL comes from login)
        if not self.login_mode and not self.ocr_only and not self.multi_course_mode and not self.start_url:
            errors.append("--start-url is required (unless using --login, --ocr-only, or run-all)")

        if self.login_mode and not self.effective_login_url:
            errors.append(
                "--start-url or COURSESCRIBE_LOGIN_URL required for login command"
            )

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

        if self.idle_pause_interval_min > self.idle_pause_interval_max:
            errors.append("--idle-pause-interval min must be <= max")
        if self.idle_pause_duration_min > self.idle_pause_duration_max:
            errors.append("--idle-pause-duration min must be <= max")
        if self.batch_size < 0:
            errors.append("--batch-size must be non-negative")

        if self.selectors_file and not self.selectors_file.exists():
            errors.append(f"Selectors file not found: {self.selectors_file}")

        if self.multi_course_mode:
            if not self.targets_file:
                errors.append("--targets-file is required for run-all command")
            elif not self.targets_file.exists():
                errors.append(f"Targets file not found: {self.targets_file}")
            if not self.effective_login_url:
                errors.append(
                    "COURSESCRIBE_LOGIN_URL or --login-url required for run-all command"
                )

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

    def load_targets(self, path: Optional[Path] = None) -> "TargetsFile":
        """Load and validate targets.json.

        Args:
            path: Explicit path, or falls back to self.targets_file, then ./targets.json.

        Returns:
            Parsed TargetsFile (one or more pathways).

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file is malformed or missing required fields.
        """
        targets_path = path or self.targets_file or Path("targets.json")
        if not targets_path.exists():
            raise FileNotFoundError(f"Targets file not found: {targets_path}")

        try:
            data = json.loads(targets_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {targets_path}: {e}") from e

        if not isinstance(data, dict):
            raise ValueError(f"Root of {targets_path} must be an object")

        if "targets" in data:
            raw_pathways = data["targets"]
            if not isinstance(raw_pathways, list):
                raise ValueError(f"'targets' in {targets_path} must be a list")
            if not raw_pathways:
                raise ValueError(f"'targets' in {targets_path} is empty")
        else:
            raw_pathways = [data]

        pathways = [
            _parse_targets_config(entry, targets_path, idx)
            for idx, entry in enumerate(raw_pathways)
        ]
        _validate_pathways(pathways, targets_path)

        logger.info(
            "Loaded targets: pathways=%d, total_courses=%d",
            len(pathways),
            sum(len(p.pending_courses) for p in pathways),
        )
        return TargetsFile(pathways=pathways)

    def resolve_paths(self) -> None:
        """Resolve relative paths to absolute."""
        self.output_dir = self.output_dir.resolve()
        self.browser_data_dir = self.browser_data_dir.resolve()
        if self.selectors_file:
            self.selectors_file = self.selectors_file.resolve()
        if self.targets_file:
            self.targets_file = self.targets_file.resolve()
