# CourseScribe — AI Agent Handover

## Project Identity

**CourseScribe** is a Mac-compatible Python tool that automates extracting structured Markdown notes from secured online T24 Temenos banking courses. It combines Playwright browser automation with an OCR + multimodal AI cleaning pipeline.

**Repository**: `sendit2sri/CourseScribe`
**Main branch**: `main`
**Python**: 3.10+
**Primary target**: macOS

## What Exists (as of 2026-04-10)

### Two independent layers

| Layer | Entry point | Purpose |
|-------|-------------|---------|
| **OCR pipeline** | `coursescribe.py` (592 lines) | Processes existing screenshot images → Tesseract OCR → AI cleaning → Markdown |
| **Automation** | `python -m automation <command>` (15 files, ~3200 lines) | Playwright browser capture + navigation + per-page OCR pipeline + resume state |

### File map

```
CourseScribe/
  coursescribe.py              # Standalone OCR pipeline (DO NOT REFACTOR — automation imports from it)
  requirements.txt             # All deps: opencv, playwright, openai, anthropic, etc.
  .env.example                 # Template for credentials and API keys
  .gitignore

  automation/                  # Playwright automation package
    __init__.py                # v0.1.0
    __main__.py                # python -m automation entry point
    cli.py                     # CLI: login, capture, process, run, status, review
    config.py                  # AutomationConfig dataclass, .env loading, validation
    selectors.py               # SelectorProfile — configurable CSS selectors with defaults

    capture/
      browser.py               # BrowserSession: persistent context, session validation, 3-tier login
      navigator.py             # CourseNavigator: Next-button + sidebar + URL-based navigation
      screenshot.py            # ScreenshotCapture: full-page, viewport-scroll, section modes
      cropper.py               # ContentCropper: OpenCV contour-based region detection

    pipeline/
      processor.py             # PageProcessor: per-page OCR + AI cleaning (imports from coursescribe.py)
      classifier.py            # ContentClassifier: DOM + image heuristics → content type routing

    state/
      manifest.py              # ManifestManager: manifest.json + run_state.json, resume logic

  docs/                        # Architecture and feature docs for the OCR pipeline
```

### Integration pattern

`automation/pipeline/processor.py` imports directly from `coursescribe.py`:
- `AIProvider`, `AnthropicProvider`, `OpenAIProvider` — AI API clients
- `CostTracker` — token counting and cost calculation
- OCR preprocessing and extraction logic is replicated (not imported) because `MultiAIOCR` methods are instance-bound

**Rule**: Do not refactor `coursescribe.py` into a package without updating `processor.py` imports.

## Configuration

### .env file (credentials — never committed)

```env
OPENAI_API_KEY=sk-proj-...
COURSESCRIBE_LOGIN_URL=https://example.com/login
COURSESCRIBE_START_URL=https://example.com/course
COURSESCRIBE_USERNAME=your_username
COURSESCRIBE_PASSWORD=your_password
# COURSESCRIBE_BROWSER_PROFILE=~/.coursescribe/browser_profile
# ANTHROPIC_API_KEY=sk-ant-...
```

### Priority order

1. CLI arguments (highest)
2. `.env` file
3. Dataclass defaults (lowest)

Credentials (`COURSESCRIBE_USERNAME`, `COURSESCRIBE_PASSWORD`) are loaded **only** from `.env` — never from CLI args, never logged.

## CLI Commands

```bash
python -m automation login    [--start-url URL]                    # Save browser session
python -m automation capture  [--start-url URL] [--output-dir DIR] # Screenshots only
python -m automation process  [--output-dir DIR] [--provider X]    # OCR only on existing captures
python -m automation run      [--start-url URL] [--output-dir DIR] # Full pipeline
python -m automation status   [--output-dir DIR]                   # Progress summary
python -m automation review   [--output-dir DIR]                   # Low-confidence pages
```

Key flags: `--provider {openai,anthropic}`, `--model`, `--enable-crops`, `--capture-mode {full,viewport,section}`, `--cost-tracking`, `--headless`, `--dry-run`, `--page-delay`, `--selectors-file`, `--login-url`, `--log-level`.

## Architecture Decisions

1. **Persistent browser session** via `playwright.chromium.launch_persistent_context()` — stores cookies/localStorage in `~/.coursescribe/browser_profile`. No manual cookie serialization.

2. **3-tier login strategy**: saved session → auto-login from `.env` → manual pause for MFA/CAPTCHA. Implemented in `browser.py:BrowserSession.ensure_authenticated()`.

3. **Per-page processing** (not batch) — each page gets independent OCR extraction + AI cleaning. Enables resume, page-level error isolation, and content-type-specific prompts.

4. **Crash-safe state** — `manifest.json` + `run_state.json` written after every page. Atomic writes. SHA-256 fingerprints for screenshots and DOM text.

5. **Page status lifecycle**: `discovered` → `captured` → `crop_generated` → `raw_extracted` → `cleaned`. Error states: `failed_capture`, `failed_processing`. Quality flag: `needs_review`.

6. **Navigation fallback order**: stored URL → sidebar click → Next button. Loop detection via visited URL tracking.

7. **Content-type routing**: classifier detects `text_heavy`, `table`, `diagram`, `t24_screenshot`, `mixed` — each gets a specialized AI prompt.

8. **Async capture, sync processing**: Playwright uses `async_api`. OCR/AI processing is synchronous (CPU/API-bound). Connected via `asyncio.run()` in CLI.

## Output Structure

```
course_capture/
  module_01_Name/
    lesson_01_Name/
      screenshots/page_001_full.png, page_001_crop_01_table.png
      raw_text/page_001_raw.txt
      cleaned/page_001_cleaned.md
      metadata.json
    lesson_combined.md
  needs_review/                # Symlinks to low-confidence pages
  manifest.json                # Course structure (modules/lessons/pages)
  run_state.json               # Per-page progress, fingerprints, costs
  cost_report.txt              # If --cost-tracking enabled
```

## What Is NOT Built Yet

These are designed but not yet implemented or tested end-to-end:

- **No tests exist** — `tests/` directory is not created. Unit tests needed for: config validation, manifest CRUD/resume logic, selector fallback, classifier heuristics, cropper region detection.
- **No local test harness** — static HTML fixtures simulating a course structure for Playwright integration tests.
- **Viewport-scroll capture (Mode B)** — method exists in `screenshot.py` but untested against real long pages.
- **OpenCV cropper** — `cropper.py` has full contour detection logic but needs validation with real T24 screenshots.
- **Lesson-combined markdown** — per-lesson merge of all cleaned pages is referenced but not wired.
- **`pyproject.toml` / `setup.py`** — no installable package yet; runs via `python -m automation`.

## Setup for New Sessions

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Configure credentials
cp .env.example .env
# Edit .env with real API keys and course credentials

# First run — establish browser session
python -m automation login --start-url https://your-course-url

# Full pipeline
python -m automation run --start-url https://your-course-url --provider openai
```

## Common Pitfalls

- **Never refactor `coursescribe.py` into a subpackage** without updating `automation/pipeline/processor.py` imports — they import `AIProvider`, `AnthropicProvider`, `OpenAIProvider`, `CostTracker` directly.
- **Tesseract must be installed separately** — `brew install tesseract` on Mac. Not a pip dependency.
- **Playwright needs `playwright install chromium`** after pip install — the browser binary is separate.
- **`.env` must exist** for any AI-powered command — the tool exits if `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) is missing.
- **Credentials are never in CLI args** — always `.env`. `config.masked_username()` for safe logging.
- **Signal handling** — SIGINT/SIGTERM triggers graceful shutdown: saves state, finishes current page, then exits.

## Key Code Paths

| Scenario | Entry | Core path |
|----------|-------|-----------|
| Login | `cli.py:cmd_login` → `browser.py:login_flow` → `_try_auto_login` or `_manual_login_prompt` |
| Capture | `cli.py:_run_capture_loop` → `browser.ensure_authenticated` → `navigator.go_next` loop → `screenshot.capture_page` → `manifest.mark_captured` |
| Full run | Same as capture + `processor.process_page` → `classifier.classify` → content-type prompt → AI call → `manifest.mark_processed` |
| Resume | `manifest.get_resume_position` → navigate to stored URL → skip already-captured pages → continue loop |
| OCR-only | `cli.py:cmd_process` → `manifest.get_unprocessed_pages` → `processor.process_page` for each |
