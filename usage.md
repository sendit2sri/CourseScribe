# CourseScribe Usage

## 1. Install

```bash
pip install -r requirements.txt && playwright install chromium
```

## 2. Configure

```bash
cp .env.example .env                     # fill in credentials and login URL
cp targets.json.example targets.json     # fill in course names
```

## 3. Extract targets from catalog (optional)

```bash
# Export full catalog to JSON
python extract_courses.py catalog.html --pretty --output courses.json

# Generate targets.json for a specific pathway
python extract_courses.py catalog.html \
  --category "Core Banking" \
  --pathway "Transact Business Accredited" \
  --make-targets --pretty -o targets.json
```

## 4. Login (saves persistent browser session)

```bash
python -m automation login
```

## 5. Run all courses (Accredited pathway)

```bash
# Default pacing (3s page delay, breaks every 15-30 pages)
python -m automation run-all --targets-file targets.json

# Faster but still human-looking (~5-6s/page effective)
python -m automation run-all --targets-file targets.json \
  --page-delay 2 --idle-pause-interval 25-40 --idle-pause-duration 30-90

# Custom pacing: breaks every 10-20 pages, 1-3 min each
python -m automation run-all --targets-file targets.json \
  --idle-pause-interval 10-20 --idle-pause-duration 60-180

# With OpenCV content cropping enabled
python -m automation run-all --targets-file targets.json --enable-crops

# Fastest (no reading breaks, higher bot-detection risk)
python -m automation run-all --targets-file targets.json \
  --page-delay 1 --idle-pause-interval 0
```

## 6. Run Wealth Management pathway (separate targets)

```bash
python -m automation run-all --targets-file targets_wealth_mgmt.json
```

## 7. Resume after interruption

```bash
# Just re-run the same command -- completed pages are skipped automatically
python -m automation run-all --targets-file targets.json
```

## 8. Retry failed courses

```bash
# Re-run the same command -- failed courses listed in the targets file are
# retried, completed ones are skipped (with a [SKIP] line printed).
python -m automation run-all --targets-file targets.json

# Check which courses failed and why before retrying
python -m automation status --output-dir course_capture
```

Only courses listed in `--targets-file` are processed in a run. Failed/in-progress
entries from unrelated past runs stay in `courses_state.json` but are not picked
up automatically — add them back to a targets file to retry.

## 9. Check status

```bash
# Overall pathway status
python -m automation status --output-dir course_capture

# Single course status
python -m automation status --output-dir course_capture/Transact_Derivatives_Administration_TR2PRDXA
```

## 10. OCR-only processing (on existing screenshots)

```bash
python -m automation process --output-dir <path-to-course-output>
```

## 11. Single course run (by URL)

```bash
# Default pacing
python -m automation run --start-url URL

# Batch of 40 pages, then exit (resume picks up from checkpoint)
python -m automation run --start-url URL --batch-size 40

# No reading breaks
python -m automation run --start-url URL --idle-pause-interval 0
```

## 12. Debugging

```bash
# Verbose logging
python -m automation run-all --targets-file targets.json --log-level DEBUG
```
