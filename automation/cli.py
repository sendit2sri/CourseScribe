"""CLI entry point for CourseScribe automation.

Usage:
  python -m automation login   --start-url URL
  python -m automation capture --start-url URL [--output-dir DIR]
  python -m automation process --output-dir DIR [--provider openai]
  python -m automation run     --start-url URL [--output-dir DIR]
  python -m automation status  --output-dir DIR
  python -m automation review  --output-dir DIR
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Union

from automation.config import AutomationConfig

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown_requested = False

# Exit code when batch limit is reached (more pages remain)
EXIT_BATCH_LIMIT = 2


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    print("\nShutdown requested — saving state and exiting after current page...")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coursescribe-auto",
        description="CourseScribe Automation — Playwright-based course capture + OCR pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Common arguments for all commands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--output-dir", type=Path, default=Path("course_capture"))
    common.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING"], default="INFO")
    common.add_argument("--log-file", type=Path, default=None)

    # Browser arguments (--start-url optional; falls back to COURSESCRIBE_START_URL in .env)
    browser_args = argparse.ArgumentParser(add_help=False)
    browser_args.add_argument("--start-url", default=None, help="Course starting URL (or set COURSESCRIBE_START_URL in .env)")
    browser_args.add_argument("--login-url", default=None, help="Login page URL (or set COURSESCRIBE_LOGIN_URL in .env)")
    browser_args.add_argument("--browser-data", type=Path, default=None,
                              help="Browser profile directory")
    browser_args.add_argument("--headless", action="store_true", help="Run browser headless")

    # AI arguments
    ai_args = argparse.ArgumentParser(add_help=False)
    ai_args.add_argument("--provider", choices=["openai", "anthropic"], default="openai")
    ai_args.add_argument("--model", default=None, help="AI model name")
    ai_args.add_argument("--content-type", choices=["course", "presentation", "technical"],
                         default="course")
    ai_args.add_argument("--cost-tracking", action="store_true")
    ai_args.add_argument("--vision-mode", dest="vision_mode", action="store_true",
                         help="Use vision-first extraction: send full screenshot directly to AI")
    ai_args.add_argument("--no-vision-mode", dest="vision_mode", action="store_false",
                         help="Use legacy OCR + AI cleaning pipeline")
    ai_args.set_defaults(vision_mode=True)

    # Capture arguments
    capture_args = argparse.ArgumentParser(add_help=False)
    capture_args.add_argument("--enable-crops", action="store_true",
                              help="Auto-crop tables/diagrams/screenshots")
    capture_args.add_argument("--capture-mode", choices=["full", "viewport", "section"],
                              default="full")
    capture_args.add_argument("--page-delay", type=float, default=1.0,
                              help="Delay between pages in seconds (default: 1.0)")
    capture_args.add_argument("--selectors-file", type=Path, default=None)
    capture_args.add_argument("--idle-pause-interval", type=str, default="15-30",
                              help="Pages between reading breaks as 'min-max' (default: 15-30). Set '0' to disable.")
    capture_args.add_argument("--idle-pause-duration", type=str, default="120-300",
                              help="Reading break duration range in seconds as 'min-max' (default: 120-300)")
    capture_args.add_argument("--batch-size", type=int, default=0,
                              help="Auto-stop after N pages per batch; 0=disabled (default: 0). Exit code 2 when reached.")
    capture_args.add_argument("--item-launch-timeout", type=float, default=180.0,
                              help="Per-item watchdog: skip a curriculum item if it does not become capture-ready within this many seconds (default: 180).")
    capture_args.add_argument("--dry-run", action="store_true")

    # --- Commands ---

    # login
    login_parser = subparsers.add_parser(
        "login", parents=[common, browser_args],
        help="Open browser for manual login and save session",
    )

    # capture
    capture_parser = subparsers.add_parser(
        "capture", parents=[common, browser_args, capture_args],
        help="Capture screenshots only (no OCR processing)",
    )

    # process
    process_parser = subparsers.add_parser(
        "process", parents=[common, ai_args],
        help="Run OCR + AI cleaning on existing captures",
    )

    # run (full pipeline)
    run_parser = subparsers.add_parser(
        "run", parents=[common, browser_args, ai_args, capture_args],
        help="Full pipeline: capture + OCR + AI cleaning",
    )

    # status
    status_parser = subparsers.add_parser(
        "status", parents=[common],
        help="Show run progress summary",
    )

    # review
    review_parser = subparsers.add_parser(
        "review", parents=[common],
        help="List pages flagged as needing review",
    )

    # run-all (multi-course pipeline)
    run_all_parser = subparsers.add_parser(
        "run-all", parents=[common, browser_args, ai_args, capture_args],
        help="Multi-course pipeline: iterate through courses in targets.json",
    )
    run_all_parser.add_argument(
        "--targets-file", type=Path, default=Path("targets.json"),
        help="Path to targets.json file (default: ./targets.json)",
    )

    return parser


def args_to_config(args: argparse.Namespace) -> AutomationConfig:
    """Convert parsed CLI arguments to AutomationConfig.

    Priority order:
      1. Explicit CLI args (highest)
      2. .env file (loaded by config.load_from_env)
      3. Dataclass defaults (lowest)
    """
    config = AutomationConfig()

    # Load .env defaults FIRST (CLI args override below)
    config.load_from_env()

    # Common args
    config.output_dir = args.output_dir
    config.log_level = args.log_level
    config.log_file = args.log_file

    # Browser args — only override if explicitly provided on CLI
    if hasattr(args, "start_url") and args.start_url is not None:
        config.start_url = args.start_url
    if hasattr(args, "login_url") and args.login_url is not None:
        config.login_url = args.login_url
    if hasattr(args, "browser_data") and args.browser_data is not None:
        config.browser_data_dir = args.browser_data
    if hasattr(args, "headless") and args.headless:
        config.headless = True

    # AI args
    if hasattr(args, "provider"):
        config.ai_provider = args.provider
    if hasattr(args, "model"):
        config.model = args.model
    if hasattr(args, "content_type"):
        config.content_type = args.content_type
    if hasattr(args, "cost_tracking"):
        config.enable_cost_tracking = args.cost_tracking
    if hasattr(args, "vision_mode"):
        config.vision_mode = args.vision_mode

    # Capture args
    if hasattr(args, "enable_crops"):
        config.enable_crops = args.enable_crops
    if hasattr(args, "capture_mode"):
        config.capture_mode = args.capture_mode
    if hasattr(args, "page_delay"):
        config.page_delay = args.page_delay
    if hasattr(args, "selectors_file"):
        config.selectors_file = args.selectors_file
    if hasattr(args, "dry_run"):
        config.dry_run = args.dry_run
    if hasattr(args, "idle_pause_interval"):
        val = args.idle_pause_interval
        if val == "0":
            config.idle_pause_interval_min = 0
            config.idle_pause_interval_max = 0
        else:
            lo, hi = val.split("-")
            config.idle_pause_interval_min = int(lo)
            config.idle_pause_interval_max = int(hi)
    if hasattr(args, "idle_pause_duration"):
        lo, hi = args.idle_pause_duration.split("-")
        config.idle_pause_duration_min = float(lo)
        config.idle_pause_duration_max = float(hi)
    if hasattr(args, "batch_size"):
        config.batch_size = args.batch_size
    if hasattr(args, "item_launch_timeout"):
        config.item_launch_timeout = args.item_launch_timeout

    # Set operation mode based on command
    config.login_mode = args.command == "login"
    config.capture_only = args.command == "capture"
    config.ocr_only = args.command == "process"
    config.multi_course_mode = args.command == "run-all"

    # Multi-course args
    if hasattr(args, "targets_file") and args.targets_file is not None:
        config.targets_file = args.targets_file

    return config


# =====================================================================
# Command handlers
# =====================================================================


async def cmd_login(config: AutomationConfig) -> None:
    """Handle the 'login' command."""
    from automation.capture.browser import BrowserSession

    session = BrowserSession(config)
    try:
        await session.start()
        await session.login_flow()
    finally:
        await session.close()


async def cmd_capture(config: AutomationConfig) -> None:
    """Handle the 'capture' command — screenshots only, no OCR."""
    await _run_capture_loop(config, process_pages=False)


async def cmd_run(config: AutomationConfig) -> None:
    """Handle the 'run' command — full pipeline."""
    await _run_capture_loop(config, process_pages=True)


async def cmd_process(config: AutomationConfig) -> None:
    """Handle the 'process' command — OCR only on existing captures."""
    from automation.pipeline.classifier import ContentClassifier
    from automation.pipeline.processor import PageProcessor
    from automation.selectors import SelectorProfile
    from automation.state.manifest import ManifestManager

    manifest = ManifestManager(config.output_dir)
    unprocessed = manifest.get_unprocessed_pages()

    if not unprocessed:
        print("No unprocessed pages found. Run 'capture' first.")
        return

    print(f"Processing {len(unprocessed)} unprocessed pages...")

    selectors = _load_selectors(config)
    processor = PageProcessor(config)
    classifier = ContentClassifier(selectors)

    for page_info in unprocessed:
        if _shutdown_requested:
            logger.info("Shutdown requested, saving state")
            break

        page_state = manifest.get_page_state(page_info.page_id)
        if not page_state or not page_state.screenshot_path:
            logger.warning(f"No screenshot for {page_info.page_id}, skipping")
            continue

        screenshot_path = config.output_dir / page_state.screenshot_path
        if not screenshot_path.exists():
            logger.warning(f"Screenshot missing: {screenshot_path}")
            continue

        # Build a minimal CaptureResult
        from automation.capture.screenshot import CaptureResult

        capture_result = CaptureResult(
            page_info=page_info,
            full_page_path=screenshot_path,
        )

        # Reconstruct lesson dir from page info
        lesson_dir = (
            config.output_dir
            / page_info.module_dir_name
            / page_info.lesson_dir_name
        )

        result = processor.process_page(capture_result, lesson_dir, classifier)

        if result.success:
            manifest.mark_processed(
                page_info.page_id,
                raw_text_path=str(result.raw_text_path.relative_to(config.output_dir))
                if result.raw_text_path
                else "",
                cleaned_path=str(result.cleaned_md_path.relative_to(config.output_dir))
                if result.cleaned_md_path
                else "",
                content_type=result.content_type,
                ocr_char_count=result.raw_text_length,
                low_quality=result.low_quality,
                review_reason=result.review_reason,
            )
        else:
            manifest.mark_failed(page_info.page_id, "processing", result.error or "Unknown")

        manifest.save()

        if config.page_delay > 0:
            time.sleep(config.page_delay * random.uniform(0.7, 1.5))

    # Print summary
    print("\n" + manifest.summary_text())


def cmd_status(config: AutomationConfig) -> None:
    """Handle the 'status' command."""
    from automation.state.manifest import ManifestManager

    if not (config.output_dir / "run_state.json").exists():
        print(f"No run state found at {config.output_dir}")
        return

    manifest = ManifestManager(config.output_dir)
    print(manifest.summary_text())

    failed = manifest.get_failed_pages()
    if failed:
        print(f"\nFailed pages ({len(failed)}):")
        for p in failed:
            state = manifest.get_page_state(p.page_id)
            err = state.error if state else "unknown"
            print(f"  {p.page_id}: {p.page_title} — {err}")


def cmd_review(config: AutomationConfig) -> None:
    """Handle the 'review' command."""
    from automation.state.manifest import ManifestManager

    if not (config.output_dir / "run_state.json").exists():
        print(f"No run state found at {config.output_dir}")
        return

    manifest = ManifestManager(config.output_dir)
    review_pages = manifest.get_review_pages()

    if not review_pages:
        print("No pages flagged for review.")
        return

    print(f"Pages needing review ({len(review_pages)}):\n")
    for p in review_pages:
        state = manifest.get_page_state(p.page_id)
        reason = state.review_reason if state else "unknown"
        chars = state.ocr_char_count if state else 0
        print(f"  {p.page_id}: {p.page_title}")
        print(f"    Reason: {reason}")
        print(f"    OCR chars: {chars}")
        if state and state.screenshot_path:
            print(f"    Screenshot: {state.screenshot_path}")
        print()


# =====================================================================
# Multi-course commands
# =====================================================================


async def cmd_run_all(config: AutomationConfig) -> None:
    """Handle the 'run-all' command — capture screenshots only, no API processing."""
    await _run_multi_course_loop(config, process_pages=False)


async def _run_multi_course_loop(
    config: AutomationConfig, process_pages: bool
) -> None:
    """Main orchestration loop for processing multiple courses from targets.json."""
    from automation.capture.browser import BrowserSession, looks_like_login_url
    from automation.capture.navigator import CourseNavigator
    from automation.capture.portal import (
        CourseLaunchError,
        NavigationError,
        PortalNavigator,
        SessionExpiredError,
    )
    from automation.capture.screenshot import ScreenshotCapture
    from automation.pipeline.classifier import ContentClassifier
    from automation.pipeline.processor import PageProcessor
    from automation.selectors import SelectorProfile
    from automation.state.courses_state import CoursesStateManager
    from automation.state.manifest import ManifestManager

    # Load targets (one or more pathways)
    targets_file = config.load_targets(config.targets_file)
    selectors = _load_selectors(config)

    # Seed multi-course state for ALL pathways BEFORE opening the browser
    # so a crash mid-pathway-1 still leaves pathways 2..N visible in state.
    courses_state = CoursesStateManager(config.output_dir)
    courses_state.init_from_targets_file(targets_file)
    courses_state.save()

    session = BrowserSession(config)

    async def _navigate_to_pathway_courses(
        portal: "PortalNavigator", pathway_targets, *, fresh: bool
    ) -> None:
        # First pathway: navigate from wherever we landed post-auth.
        # Subsequent / recovery: jump back to the cached landing URL,
        # then call navigate_to_pathways() as a safety net.
        if fresh:
            await portal.navigate_to_pathways()
        else:
            await portal.return_to_pathways_landing()
            await portal.navigate_to_pathways()
        if pathway_targets.category:
            await portal.select_category_tab(pathway_targets.category)
        await portal.select_pathway(pathway_targets.pathway_name)
        await portal.expand_course_section()
        session.save_as_portal_page()

    try:
        await session.start()
        await session.ensure_authenticated()

        first_pathway = True
        for pathway_targets in targets_file.pathways:
            if _shutdown_requested:
                logger.info("Shutdown requested, stopping before next pathway")
                break

            print(f"\n{'#' * 60}")
            print(f"Pathway: {pathway_targets.pathway_name}")
            print(f"{'#' * 60}")

            portal = PortalNavigator(session, selectors, pathway_targets)

            await _navigate_to_pathway_courses(
                portal, pathway_targets, fresh=first_pathway,
            )
            first_pathway = False

            # Pending list is built from THIS pathway only — never the whole file.
            pending = [
                t for t in pathway_targets.pending_courses
                if not courses_state.is_course_complete(t.name)
            ]
            completed_skipped = [
                t.name for t in pathway_targets.pending_courses
                if courses_state.is_course_complete(t.name)
            ]
            for name in completed_skipped:
                print(f"  [SKIP] {name} -- already completed")
            print(f"\n{len(pending)} course(s) to process in this pathway\n")

            for course_target in pending:
                course_name = course_target.name
                course_code = course_target.code
                course_url = course_target.url
                needs_manual_enrollment = course_target.needs_manual_enrollment

                if _shutdown_requested:
                    logger.info("Shutdown requested, stopping after current course")
                    break

                # Re-auth checkpoint: long unattended runs see the SSO session
                # expire between courses. Cheap URL probe avoids the cost of a
                # full is_session_valid() per course; if the portal page has
                # been bounced to login, recover transparently. ensure_authenticated
                # raises on terminal failure -> aborts the run (no operator).
                portal_url = session.page.url if session.page else ""
                if looks_like_login_url(portal_url):
                    logger.warning(
                        "Portal page is on a login URL between courses (%s) — "
                        "re-authenticating",
                        portal_url,
                    )
                    await session.ensure_authenticated()
                    await _navigate_to_pathway_courses(
                        portal, pathway_targets, fresh=False,
                    )

                print(f"\n{'=' * 60}")
                print(f"Starting: {course_name}")
                print(f"{'=' * 60}\n")

                if needs_manual_enrollment:
                    msg = (
                        "Marked needs_manual_enrollment in targets.json — "
                        "user is not enrolled in the new version. Skipping."
                    )
                    print(f"  [SKIP] {course_name} -- {msg}")
                    logger.warning("Skipping %s: %s", course_name, msg)
                    courses_state.mark_failed(course_name, msg, "needs_manual_enrollment")
                    courses_state.save()
                    continue

                course_dir_name = courses_state.course_output_dir(course_name)
                full_course_dir = config.output_dir / course_dir_name
                courses_state.mark_in_progress(course_name, course_dir_name)
                courses_state.save()

                try:
                    # Find and open course link (opens new tab)
                    if course_url:
                        await portal.open_course_url(course_url)
                    else:
                        await portal.open_course_link(course_name, course_code=course_code)

                    # Launch course in the new tab
                    launch_result = await portal.launch_course()

                    # Track old version redirect in state
                    if launch_result.old_version_redirected:
                        entry = courses_state._courses.get(course_name)
                        if entry:
                            entry.old_version_redirect = launch_result.old_version_url
                            courses_state._sync(course_name)
                            courses_state.save()
                        logger.info(
                            "Course '%s' was an old version, redirected to: %s",
                            course_name, launch_result.old_version_url,
                        )

                    # Extract curriculum sidebar before navigating into content
                    curriculum_data = None
                    try:
                        curriculum_data = await portal.extract_curriculum_from_page()

                        # Filter out "Course Document" items (PDF duplicates)
                        curriculum_data["items"] = [
                            item for item in curriculum_data["items"]
                            if "course document" not in item.get("title", "").lower()
                        ]
                        curriculum_data["summary"]["total_items"] = len(
                            curriculum_data["items"]
                        )

                        # Replace platform status with our capture status
                        for item in curriculum_data["items"]:
                            item["platform_status"] = item.pop("status", "unknown")
                            if "capture_status" not in item:
                                item["capture_status"] = "not_captured"

                        if curriculum_data["summary"]["total_items"] > 0:
                            curriculum_file = full_course_dir / "curriculum.json"
                            full_course_dir.mkdir(parents=True, exist_ok=True)
                            import json

                            # Merge capture_status from previous run if exists
                            if curriculum_file.exists():
                                try:
                                    prev = json.loads(
                                        curriculum_file.read_text(encoding="utf-8")
                                    )
                                    prev_by_pos = {
                                        it["position"]: it
                                        for it in prev.get("items", [])
                                        if it.get("capture_status")
                                    }
                                    if prev_by_pos:
                                        for item in curriculum_data["items"]:
                                            prev_item = prev_by_pos.get(
                                                item.get("position")
                                            )
                                            if prev_item:
                                                item["capture_status"] = prev_item[
                                                    "capture_status"
                                                ]
                                                item["pages_captured"] = prev_item.get(
                                                    "pages_captured", 0
                                                )
                                        logger.info(
                                            "Merged capture status from previous run"
                                        )
                                except Exception:
                                    pass

                            with open(curriculum_file, 'w', encoding='utf-8') as f:
                                json.dump(curriculum_data, f, indent=2, ensure_ascii=False)
                                f.write('\n')
                            logger.info(
                                "Saved curriculum: %d items → %s",
                                curriculum_data["summary"]["total_items"],
                                curriculum_file,
                            )
                    except Exception as e:
                        logger.warning("Could not extract curriculum sidebar: %s", e)

                    # Run the per-course capture loop with curriculum iteration
                    await _run_single_course(
                        session=session,
                        config=config,
                        selectors=selectors,
                        course_output_dir=full_course_dir,
                        skip_titles=pathway_targets.skip_titles,
                        process_pages=process_pages,
                        content_frame=launch_result.content_frame,
                        portal=portal,
                        curriculum_items=curriculum_data.get("items", []) if curriculum_data else [],
                    )

                    # Exit course
                    await portal.exit_course()

                    # Get page count from manifest
                    manifest = ManifestManager(full_course_dir)
                    total_pages = manifest.total_pages

                    # Close course tab, return to portal
                    await session.close_current_page()
                    await session.switch_to_portal_page()
                    await session.wait_for_stable_page()

                    courses_state.mark_completed(course_name, total_pages)
                    courses_state.save()

                    print(f"\nCompleted: {course_name} ({total_pages} pages)")

                    # Re-expand course section (portal may have reloaded)
                    try:
                        await portal.expand_course_section()
                    except Exception:
                        logger.debug("Could not re-expand course section (may still be visible)")

                except SessionExpiredError as e:
                    # Session expired mid-course-open. Mark this course failed,
                    # re-authenticate, re-establish the pathway view, then
                    # continue. ensure_authenticated raises RuntimeError on
                    # terminal failure, which propagates and aborts the run.
                    logger.warning(
                        "Session expired during '%s': %s — re-authenticating",
                        course_name, e,
                    )
                    courses_state.mark_failed(course_name, f"session_expired: {e}")
                    courses_state.save()

                    # Best-effort tab cleanup before re-auth
                    try:
                        await session.close_current_page()
                        await session.switch_to_portal_page()
                    except Exception:
                        pass

                    await session.ensure_authenticated()
                    await _navigate_to_pathway_courses(
                        portal, pathway_targets, fresh=False,
                    )
                    continue

                except (NavigationError, CourseLaunchError) as e:
                    logger.error("Course failed: %s: %s", course_name, e)
                    courses_state.mark_failed(course_name, str(e))
                    courses_state.save()

                    # Try to recover: close extra tabs, return to portal
                    try:
                        await session.close_current_page()
                        await session.switch_to_portal_page()
                        await session.wait_for_stable_page()
                        await portal.expand_course_section()
                    except Exception as recover_err:
                        logger.error("Failed to recover to portal page: %s", recover_err)
                        break  # Can't continue if we lost the portal

                    continue

                except Exception as e:
                    logger.error("Unexpected error for %s: %s", course_name, e)
                    courses_state.mark_failed(course_name, str(e))
                    courses_state.save()

                    try:
                        await session.close_current_page()
                        await session.switch_to_portal_page()
                        await session.wait_for_stable_page()
                    except Exception:
                        logger.error("Failed to recover to portal page")
                        break

                    continue

        # Final summary (across all pathways)
        print("\n" + courses_state.summary_text())

    finally:
        await session.close()


async def _run_single_course(
    session: "BrowserSession",
    config: AutomationConfig,
    selectors: "SelectorProfile",
    course_output_dir: Path,
    skip_titles: List[str],
    process_pages: bool,
    content_frame: Optional[Union[Frame, Page]] = None,
    portal: Optional[Any] = None,
    curriculum_items: Optional[List[dict]] = None,
) -> None:
    """Run the capture+process loop for a single course (already on the course page).

    This is the inner loop extracted from _run_capture_loop(), adapted for
    multi-course orchestration. The caller handles browser start/close and
    tab management.

    When curriculum_items is provided, iterates through each item in the
    sidebar, capturing all pages per item before moving to the next.
    """
    from automation.capture.navigator import CourseNavigator
    from automation.capture.screenshot import ScreenshotCapture
    from automation.pipeline.classifier import ContentClassifier
    from automation.pipeline.processor import PageProcessor
    from automation.state.manifest import ManifestManager

    manifest = ManifestManager(course_output_dir)
    manifest.set_course_url(await session.get_current_url())
    manifest.set_config({
        "capture_mode": config.capture_mode,
        "ai_provider": config.ai_provider,
        "model": config.model or "default",
        "content_type": config.content_type,
        "enable_crops": config.enable_crops,
    })

    navigator = CourseNavigator(session, selectors)
    navigator.reset()
    if content_frame is not None:
        navigator.set_content_frame(content_frame)
    capturer = ScreenshotCapture(session, config, selectors)
    if content_frame is not None:
        capturer.set_content_frame(content_frame)
    processor = PageProcessor(config) if process_pages else None
    classifier = ContentClassifier(selectors) if process_pages else None

    pages_processed = 0
    curriculum_results: List[dict] = []  # per-item capture tracking

    # Schedule first reading break at a random page count
    _next_idle_pause_at_mc = (
        random.randint(config.idle_pause_interval_min, config.idle_pause_interval_max)
        if config.idle_pause_interval_min > 0
        else 0
    )

    # Build the list of curriculum items to iterate.
    # If no curriculum items provided, use a single dummy entry so the
    # inner capture loop runs once (legacy behavior).
    # In curriculum mode, always start fresh (no page-level resume).
    # Curriculum-level retry is handled via capture_status in curriculum.json.
    if curriculum_items and portal:
        items_to_process = curriculum_items
        fresh_start = True
    else:
        items_to_process = [None]  # single pass, no sidebar clicking
        fresh_start = False

    for item_idx, cur_item in enumerate(items_to_process):
        if _shutdown_requested:
            break

        if cur_item is not None:
            position = cur_item.get("position", item_idx + 1)
            node_id = cur_item.get("node_id", "")
            title = cur_item.get("title", f"Item {position}")

            # Skip already-captured items (retry-only mode)
            if cur_item.get("capture_status") == "captured" and cur_item.get("pages_captured", 0) > 0:
                logger.info(
                    "Skipping already-captured item %d/%d: %s (%d pages)",
                    position, len(items_to_process), title,
                    cur_item["pages_captured"],
                )
                curriculum_results.append({
                    "position": position,
                    "title": title,
                    "status": "captured",
                    "pages_captured": cur_item["pages_captured"],
                })
                continue

            logger.info(
                "Opening curriculum item %d/%d: %s",
                position,
                len(items_to_process),
                title,
            )

            # Click the curriculum item in the sidebar.
            # Watchdog: a hung Cornerstone player has historically blocked
            # this call for 50+ minutes. Cap each attempt at
            # config.item_launch_timeout. A timeout is treated as terminal
            # (no further retries) — re-clicking a stuck iframe does not
            # recover. Other exceptions still get up to 3 attempts.
            item_result = None
            launch_timed_out = False
            for attempt in range(3):
                try:
                    item_result = await asyncio.wait_for(
                        portal.click_curriculum_item(position, node_id),
                        timeout=config.item_launch_timeout,
                    )
                    # Update content frame (iframe may have reloaded)
                    if item_result.content_frame is not None:
                        navigator.set_content_frame(item_result.content_frame)
                        capturer.set_content_frame(item_result.content_frame)
                    break  # success
                except asyncio.TimeoutError:
                    logger.warning(
                        "Curriculum item %d (%s) did not become ready within "
                        "%.0fs — marking failed_launch and advancing",
                        position, title, config.item_launch_timeout,
                    )
                    launch_timed_out = True
                    break
                except Exception as e:
                    logger.warning(
                        "Attempt %d/3 failed for curriculum item %d (%s): %s",
                        attempt + 1, position, title, e,
                    )
                    if attempt < 2:
                        await asyncio.sleep(3)

            if item_result is None:
                if launch_timed_out:
                    manifest.mark_item_failed(position, title, "launch_timeout")
                    manifest.save()
                    status = "failed_launch"
                else:
                    logger.error(
                        "Skipping curriculum item %d (%s) after 3 failed attempts",
                        position, title,
                    )
                    status = "failed"
                curriculum_results.append({
                    "position": position,
                    "title": title,
                    "status": status,
                    "pages_captured": 0,
                })
                continue

            # Reset navigator for the new curriculum item
            navigator.reset_for_new_item(item_idx + 1, module_name=title)
        else:
            # Legacy single-pass mode — check for resume
            resume_pos = manifest.get_resume_position()
            if resume_pos:
                logger.info(
                    "Resuming from %s: %s",
                    resume_pos.page_id,
                    resume_pos.page_title,
                )
                pos = manifest._state.get("current_position", {})
                navigator.set_position(
                    pos.get("module_index", 1),
                    pos.get("lesson_index", 1),
                    pos.get("page_index", 0),
                )
                for pid, state in manifest._page_states.items():
                    if state.url:
                        navigator.mark_url_visited(state.url)

        # ---- Inner loop: capture all pages for this curriculum item ----
        item_pages = 0
        prev_info = None

        _frame_retried = False
        while not _shutdown_requested:
            try:
                # Detect module/lesson changes
                if prev_info:
                    await navigator.detect_module_change(prev_info)
                    await navigator.detect_lesson_change(prev_info)

                # Get current page info
                page_info = await navigator.get_current_page_info()

                # Skip if already captured (resume scenario — legacy mode only)
                if not fresh_start and manifest.is_page_captured(page_info.page_id):
                    logger.info("Skipping already-captured %s", page_info.page_id)
                else:
                    # Register page
                    manifest.add_page(page_info)
                    manifest.update_position(page_info)

                    # Check skip titles
                    if await navigator.is_skip_page(skip_titles):
                        logger.info(
                            "Skipping page (matched skip title): [%s] %s",
                            page_info.page_id,
                            page_info.page_title,
                        )
                        manifest.mark_skipped(
                            page_info.page_id,
                            f"matched skip_titles: {page_info.page_title}",
                        )
                    else:
                        # Expand hidden content
                        await navigator.expand_all_content()
                        await session.wait_for_stable_page()
                        await session.wait_for_content_ready(selectors)

                        # Build lesson directory
                        lesson_dir = (
                            course_output_dir
                            / page_info.module_dir_name
                            / page_info.lesson_dir_name
                        )

                        # Simulate human browsing before capture
                        await session.random_scroll()

                        # Capture
                        logger.info(
                            "Capturing [%s] %s", page_info.page_id, page_info.page_title
                        )
                        capture_result = await capturer.capture_page(
                            page_info, lesson_dir
                        )

                        # Fingerprint
                        dom_text = await navigator.extract_dom_text()
                        screenshot_hash = ""
                        dom_text_hash = ""
                        if capture_result.full_page_path:
                            screenshot_hash = manifest.compute_image_hash(
                                capture_result.full_page_path
                            )
                        if dom_text:
                            dom_text_hash = manifest.compute_text_hash(dom_text)

                        # Record capture
                        crop_paths = [
                            str(p.relative_to(course_output_dir))
                            for p, _ in capture_result.section_crops
                        ]
                        manifest.mark_captured(
                            page_info.page_id,
                            screenshot_path=str(
                                capture_result.full_page_path.relative_to(
                                    course_output_dir
                                )
                            )
                            if capture_result.full_page_path
                            else "",
                            screenshot_hash=screenshot_hash,
                            dom_text_hash=dom_text_hash,
                            crops=crop_paths,
                        )

                        # Process (if not capture-only)
                        if process_pages and processor and classifier:
                            result = processor.process_page(
                                capture_result, lesson_dir, classifier
                            )
                            if result.success:
                                manifest.mark_processed(
                                    page_info.page_id,
                                    raw_text_path=str(
                                        result.raw_text_path.relative_to(
                                            course_output_dir
                                        )
                                    )
                                    if result.raw_text_path
                                    else "",
                                    cleaned_path=str(
                                        result.cleaned_md_path.relative_to(
                                            course_output_dir
                                        )
                                    )
                                    if result.cleaned_md_path
                                    else "",
                                    content_type=result.content_type,
                                    ocr_char_count=result.raw_text_length,
                                    low_quality=result.low_quality,
                                    review_reason=result.review_reason,
                                )
                                if result.cost_data:
                                    manifest.update_cost(
                                        result.cost_data.get("cost", 0),
                                        result.cost_data.get("requests", 0),
                                        result.cost_data.get("input_tokens", 0),
                                        result.cost_data.get("output_tokens", 0),
                                    )
                            else:
                                manifest.mark_failed(
                                    page_info.page_id,
                                    "processing",
                                    result.error or "Unknown error",
                                )

                        pages_processed += 1
                        item_pages += 1

                # Save state after every page
                manifest.save()

                # --- Pacing: batch auto-stop ---
                if config.batch_size > 0 and pages_processed >= config.batch_size:
                    logger.info(
                        "Batch limit reached (%d pages). Exiting — resume to continue.",
                        pages_processed,
                    )
                    print(f"\nBatch complete: {pages_processed} pages captured. "
                          f"Run again to resume from checkpoint.")
                    print("\n" + manifest.summary_text())
                    sys.exit(EXIT_BATCH_LIMIT)

                # --- Pacing: idle reading break ---
                if _next_idle_pause_at_mc > 0 and pages_processed >= _next_idle_pause_at_mc:
                    pause_secs = random.uniform(
                        config.idle_pause_duration_min,
                        config.idle_pause_duration_max,
                    )
                    logger.info(
                        "Reading break: pausing %.0fs after %d pages",
                        pause_secs, pages_processed,
                    )
                    await asyncio.sleep(pause_secs)
                    _next_idle_pause_at_mc = pages_processed + random.randint(
                        config.idle_pause_interval_min,
                        config.idle_pause_interval_max,
                    )

                # Delay between pages
                if config.page_delay > 0:
                    await asyncio.sleep(config.page_delay * random.uniform(0.7, 1.5))

                # Try to navigate to next page
                prev_info = page_info
                next_info = await navigator.go_next()
                if next_info is None:
                    if cur_item is not None:
                        logger.info(
                            "Reached end of curriculum item %d: %s",
                            cur_item.get("position", item_idx + 1),
                            cur_item.get("title", ""),
                        )
                    else:
                        logger.info("Reached end of course")
                    break

                _frame_retried = False

            except Exception as e:
                if "detached" not in str(e).lower() or _frame_retried:
                    raise
                logger.warning("Frame detached, re-detecting content frame: %s", e)
                _frame_retried = True
                if portal:
                    fresh_frame = await portal.detect_content_frame()
                    navigator.set_content_frame(fresh_frame)
                    capturer.set_content_frame(fresh_frame)
                continue

        # Record per-item result
        if cur_item is not None:
            curriculum_results.append({
                "position": cur_item.get("position", item_idx + 1),
                "title": cur_item.get("title", ""),
                "status": "captured" if item_pages > 0 else "no_pages",
                "pages_captured": item_pages,
            })

    # Final save
    manifest.save()

    # Print per-course summary
    print("\n" + manifest.summary_text())

    # Print curriculum capture summary if we iterated items
    if curriculum_results:
        total_items = len(curriculum_results)
        ok_count = sum(1 for r in curriculum_results if r["status"] == "captured")
        fail_count = sum(
            1 for r in curriculum_results
            if r["status"] in ("failed", "failed_launch")
        )
        timeout_count = sum(
            1 for r in curriculum_results if r["status"] == "failed_launch"
        )
        no_pages_count = sum(1 for r in curriculum_results if r["status"] == "no_pages")
        total_pages = sum(r["pages_captured"] for r in curriculum_results)

        print(f"\nCurriculum Capture Summary ({total_items} items)")
        print("=" * 50)
        for r in curriculum_results:
            icon = {
                "captured": "[OK]  ",
                "failed": "[FAIL]",
                "failed_launch": "[TIME]",
                "no_pages": "[SKIP]",
            }
            status = icon.get(r["status"], "[?]   ")
            pages_str = f"({r['pages_captured']} pages)" if r["pages_captured"] else ""
            print(f"  {status} {r['position']:2d}. {r['title']} {pages_str}")
        timeout_str = f" (of which {timeout_count} timed out)" if timeout_count else ""
        print(f"\nCaptured: {ok_count}/{total_items} | "
              f"Failed: {fail_count}{timeout_str} | Skipped: {no_pages_count} | "
              f"Total pages: {total_pages}")

        # Update curriculum.json with capture results
        import json
        curriculum_file = course_output_dir / "curriculum.json"
        if curriculum_file.exists():
            try:
                data = json.loads(curriculum_file.read_text(encoding="utf-8"))
                results_by_pos = {r["position"]: r for r in curriculum_results}
                for item in data.get("items", []):
                    pos = item.get("position", 0)
                    if pos in results_by_pos:
                        item["capture_status"] = results_by_pos[pos]["status"]
                        item["pages_captured"] = results_by_pos[pos]["pages_captured"]
                data["capture_summary"] = {
                    "total_items": total_items,
                    "captured": ok_count,
                    "failed": fail_count,
                    "no_pages": no_pages_count,
                    "total_pages": total_pages,
                }
                with open(curriculum_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            except Exception as e:
                logger.warning("Could not update curriculum.json: %s", e)

    if process_pages and processor:
        cost = processor.get_cumulative_cost()
        if cost:
            print(
                f"\nAPI cost: ${cost['total_cost']:.4f} "
                f"({cost['total_requests']} requests)"
            )


# =====================================================================
# Core capture loop (single-course, original)
# =====================================================================


async def _run_capture_loop(config: AutomationConfig, process_pages: bool) -> None:
    """Main capture loop shared by 'capture' and 'run' commands."""
    from automation.capture.browser import BrowserSession
    from automation.capture.navigator import CourseNavigator
    from automation.capture.screenshot import ScreenshotCapture
    from automation.pipeline.classifier import ContentClassifier
    from automation.pipeline.processor import PageProcessor
    from automation.selectors import SelectorProfile
    from automation.state.manifest import ManifestManager

    selectors = _load_selectors(config)
    manifest = ManifestManager(config.output_dir)
    manifest.set_course_url(config.start_url)
    manifest.set_config({
        "capture_mode": config.capture_mode,
        "ai_provider": config.ai_provider,
        "model": config.model or "default",
        "content_type": config.content_type,
        "enable_crops": config.enable_crops,
    })

    session = BrowserSession(config)
    processor = PageProcessor(config) if process_pages else None
    classifier = ContentClassifier(selectors) if process_pages else None

    try:
        await session.start()
        await session.check_stealth()

        # Verify session is authenticated; re-login if expired
        await session.ensure_authenticated()

        navigator = CourseNavigator(session, selectors)
        capturer = ScreenshotCapture(session, config, selectors)

        # Check for resume
        resume_pos = manifest.get_resume_position()
        if resume_pos:
            logger.info(
                f"Resuming from {resume_pos.page_id}: {resume_pos.page_title}"
            )
            # Restore navigator position from manifest
            pos = manifest._state.get("current_position", {})
            navigator.set_position(
                pos.get("module_index", 1),
                pos.get("lesson_index", 1),
                pos.get("page_index", 0),
            )
            # Re-register visited URLs
            for pid, state in manifest._page_states.items():
                if state.url:
                    navigator.mark_url_visited(state.url)

            # Navigate to resume position
            if resume_pos.url:
                await session.navigate(resume_pos.url)
                await session.wait_for_stable_page()
        else:
            # Fresh start
            await session.navigate(config.start_url)
            await session.wait_for_stable_page()

        prev_info = None
        pages_processed = 0

        # Schedule first reading break at a random page count
        _next_idle_pause_at = (
            random.randint(config.idle_pause_interval_min, config.idle_pause_interval_max)
            if config.idle_pause_interval_min > 0
            else 0
        )

        while not _shutdown_requested:
            # Detect module/lesson changes for index tracking
            if prev_info:
                await navigator.detect_module_change(prev_info)
                await navigator.detect_lesson_change(prev_info)

            # Get current page info
            page_info = await navigator.get_current_page_info()

            # Skip if already captured (resume scenario)
            if manifest.is_page_captured(page_info.page_id):
                logger.info(f"Skipping already-captured {page_info.page_id}")
            else:
                # Register page
                manifest.add_page(page_info)
                manifest.update_position(page_info)

                # Expand hidden content
                await navigator.expand_all_content()
                await session.wait_for_stable_page()
                await session.wait_for_content_ready(selectors)

                # Build lesson directory
                lesson_dir = (
                    config.output_dir
                    / page_info.module_dir_name
                    / page_info.lesson_dir_name
                )

                # Simulate human browsing before capture
                await session.random_scroll()

                # Capture
                logger.info(
                    f"Capturing [{page_info.page_id}] {page_info.page_title}"
                )
                capture_result = await capturer.capture_page(page_info, lesson_dir)

                # Fingerprint
                dom_text = await navigator.extract_dom_text()
                screenshot_hash = ""
                dom_text_hash = ""
                if capture_result.full_page_path:
                    screenshot_hash = manifest.compute_image_hash(
                        capture_result.full_page_path
                    )
                if dom_text:
                    dom_text_hash = manifest.compute_text_hash(dom_text)

                # Record capture
                crop_paths = [str(p.relative_to(config.output_dir))
                              for p, _ in capture_result.section_crops]
                manifest.mark_captured(
                    page_info.page_id,
                    screenshot_path=str(
                        capture_result.full_page_path.relative_to(config.output_dir)
                    ) if capture_result.full_page_path else "",
                    screenshot_hash=screenshot_hash,
                    dom_text_hash=dom_text_hash,
                    crops=crop_paths,
                )

                # Process (if not capture-only)
                if process_pages and processor and classifier:
                    result = processor.process_page(
                        capture_result, lesson_dir, classifier
                    )
                    if result.success:
                        manifest.mark_processed(
                            page_info.page_id,
                            raw_text_path=str(
                                result.raw_text_path.relative_to(config.output_dir)
                            ) if result.raw_text_path else "",
                            cleaned_path=str(
                                result.cleaned_md_path.relative_to(config.output_dir)
                            ) if result.cleaned_md_path else "",
                            content_type=result.content_type,
                            ocr_char_count=result.raw_text_length,
                            low_quality=result.low_quality,
                            review_reason=result.review_reason,
                        )
                        # Update cost tracking
                        if result.cost_data:
                            manifest.update_cost(
                                result.cost_data.get("cost", 0),
                                result.cost_data.get("requests", 0),
                                result.cost_data.get("input_tokens", 0),
                                result.cost_data.get("output_tokens", 0),
                            )
                    else:
                        manifest.mark_failed(
                            page_info.page_id, "processing",
                            result.error or "Unknown error",
                        )

                pages_processed += 1

            # Save state after every page
            manifest.save()

            # --- Pacing: batch auto-stop ---
            if config.batch_size > 0 and pages_processed >= config.batch_size:
                logger.info(
                    "Batch limit reached (%d pages). Exiting — resume to continue.",
                    pages_processed,
                )
                print(f"\nBatch complete: {pages_processed} pages captured. "
                      f"Run again to resume from checkpoint.")
                print("\n" + manifest.summary_text())
                sys.exit(EXIT_BATCH_LIMIT)

            # --- Pacing: idle reading break ---
            if _next_idle_pause_at > 0 and pages_processed >= _next_idle_pause_at:
                pause_secs = random.uniform(
                    config.idle_pause_duration_min,
                    config.idle_pause_duration_max,
                )
                logger.info(
                    "Reading break: pausing %.0fs after %d pages",
                    pause_secs, pages_processed,
                )
                await asyncio.sleep(pause_secs)
                # Schedule next break
                _next_idle_pause_at = pages_processed + random.randint(
                    config.idle_pause_interval_min,
                    config.idle_pause_interval_max,
                )

            # Delay between pages (jittered to avoid detection)
            if config.page_delay > 0:
                await asyncio.sleep(config.page_delay * random.uniform(0.7, 1.5))

            # Try to navigate to next page
            prev_info = page_info
            next_info = await navigator.go_next()
            if next_info is None:
                logger.info("Reached end of course")
                break

        # Final save
        manifest.save()

        # Print summary
        print("\n" + manifest.summary_text())

        if process_pages and processor:
            cost = processor.get_cumulative_cost()
            if cost:
                print(f"\nAPI cost: ${cost['total_cost']:.4f} "
                      f"({cost['total_requests']} requests)")

    finally:
        await session.close()


def _load_selectors(config: AutomationConfig):
    """Load selector profile from file or use defaults."""
    from automation.selectors import SelectorProfile

    if config.selectors_file:
        return SelectorProfile.from_file(config.selectors_file)
    return SelectorProfile()


# =====================================================================
# Main
# =====================================================================


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = args_to_config(args)
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    config.resolve_paths()
    config.setup_logging()

    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Dispatch command
    if args.command == "login":
        asyncio.run(cmd_login(config))
    elif args.command == "capture":
        asyncio.run(cmd_capture(config))
    elif args.command == "run":
        asyncio.run(cmd_run(config))
    elif args.command == "run-all":
        asyncio.run(cmd_run_all(config))
    elif args.command == "process":
        asyncio.run(cmd_process(config))
    elif args.command == "status":
        cmd_status(config)
    elif args.command == "review":
        cmd_review(config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
