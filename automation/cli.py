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
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Union

from automation.config import AutomationConfig

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown_requested = False


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

    # Capture arguments
    capture_args = argparse.ArgumentParser(add_help=False)
    capture_args.add_argument("--enable-crops", action="store_true",
                              help="Auto-crop tables/diagrams/screenshots")
    capture_args.add_argument("--capture-mode", choices=["full", "viewport", "section"],
                              default="full")
    capture_args.add_argument("--page-delay", type=float, default=1.0,
                              help="Delay between pages in seconds")
    capture_args.add_argument("--selectors-file", type=Path, default=None)
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
            time.sleep(config.page_delay)

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
    """Handle the 'run-all' command — multi-course pipeline."""
    await _run_multi_course_loop(config, process_pages=True)


async def _run_multi_course_loop(
    config: AutomationConfig, process_pages: bool
) -> None:
    """Main orchestration loop for processing multiple courses from targets.json."""
    from automation.capture.browser import BrowserSession
    from automation.capture.navigator import CourseNavigator
    from automation.capture.portal import CourseLaunchError, NavigationError, PortalNavigator
    from automation.capture.screenshot import ScreenshotCapture
    from automation.pipeline.classifier import ContentClassifier
    from automation.pipeline.processor import PageProcessor
    from automation.selectors import SelectorProfile
    from automation.state.courses_state import CoursesStateManager
    from automation.state.manifest import ManifestManager

    # Load targets
    targets = config.load_targets(config.targets_file)
    selectors = _load_selectors(config)

    # Initialize multi-course state
    courses_state = CoursesStateManager(config.output_dir)
    courses_state.init_from_targets(targets)
    courses_state.save()

    session = BrowserSession(config)

    try:
        await session.start()
        await session.ensure_authenticated()

        # Navigate to pathway
        portal = PortalNavigator(session, selectors, targets)
        await portal.navigate_to_pathways()
        await portal.select_pathway(targets.pathway_name)
        await portal.expand_course_section()

        # Save portal page reference for tab management
        session.save_as_portal_page()

        pending = courses_state.get_pending_courses()
        print(f"\n{len(pending)} course(s) to process\n")

        for course_name in pending:
            if _shutdown_requested:
                logger.info("Shutdown requested, stopping after current course")
                break

            print(f"\n{'=' * 60}")
            print(f"Starting: {course_name}")
            print(f"{'=' * 60}\n")

            course_dir_name = courses_state.course_output_dir(course_name)
            full_course_dir = config.output_dir / course_dir_name
            courses_state.mark_in_progress(course_name, course_dir_name)
            courses_state.save()

            try:
                # Find and open course link (opens new tab)
                await portal.open_course_link(course_name)

                # Launch course in the new tab
                launch_result = await portal.launch_course()

                # Extract curriculum sidebar before navigating into content
                try:
                    curriculum_data = await portal.extract_curriculum_from_page()
                    if curriculum_data["summary"]["total_items"] > 0:
                        curriculum_file = full_course_dir / "curriculum.json"
                        full_course_dir.mkdir(parents=True, exist_ok=True)
                        import json
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

                # Run the per-course capture loop
                await _run_single_course(
                    session=session,
                    config=config,
                    selectors=selectors,
                    course_output_dir=full_course_dir,
                    skip_titles=targets.skip_titles,
                    process_pages=process_pages,
                    content_frame=launch_result.content_frame,
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

        # Final summary
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
) -> None:
    """Run the capture+process loop for a single course (already on the course page).

    This is the inner loop extracted from _run_capture_loop(), adapted for
    multi-course orchestration. The caller handles browser start/close and
    tab management.
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
    capturer = ScreenshotCapture(session, config, selectors)
    processor = PageProcessor(config) if process_pages else None
    classifier = ContentClassifier(selectors) if process_pages else None

    # Check for resume
    resume_pos = manifest.get_resume_position()
    if resume_pos:
        logger.info(
            "Resuming from %s: %s", resume_pos.page_id, resume_pos.page_title
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

    prev_info = None
    pages_processed = 0

    while not _shutdown_requested:
        # Detect module/lesson changes
        if prev_info:
            await navigator.detect_module_change(prev_info)
            await navigator.detect_lesson_change(prev_info)

        # Get current page info
        page_info = await navigator.get_current_page_info()

        # Skip if already captured (resume scenario)
        if manifest.is_page_captured(page_info.page_id):
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

                # Build lesson directory
                lesson_dir = (
                    course_output_dir
                    / page_info.module_dir_name
                    / page_info.lesson_dir_name
                )

                # Capture
                logger.info(
                    "Capturing [%s] %s", page_info.page_id, page_info.page_title
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
                crop_paths = [
                    str(p.relative_to(course_output_dir))
                    for p, _ in capture_result.section_crops
                ]
                manifest.mark_captured(
                    page_info.page_id,
                    screenshot_path=str(
                        capture_result.full_page_path.relative_to(course_output_dir)
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
                                result.raw_text_path.relative_to(course_output_dir)
                            )
                            if result.raw_text_path
                            else "",
                            cleaned_path=str(
                                result.cleaned_md_path.relative_to(course_output_dir)
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

        # Save state after every page
        manifest.save()

        # Delay between pages
        if config.page_delay > 0:
            await asyncio.sleep(config.page_delay)

        # Try to navigate to next page
        prev_info = page_info
        next_info = await navigator.go_next()
        if next_info is None:
            logger.info("Reached end of course")
            break

    # Final save
    manifest.save()

    # Print per-course summary
    print("\n" + manifest.summary_text())

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

                # Build lesson directory
                lesson_dir = (
                    config.output_dir
                    / page_info.module_dir_name
                    / page_info.lesson_dir_name
                )

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

            # Delay between pages
            if config.page_delay > 0:
                await asyncio.sleep(config.page_delay)

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
