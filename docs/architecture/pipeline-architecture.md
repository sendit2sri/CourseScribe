# Pipeline Architecture

## Summary
CourseScribe runs a 4-stage pipeline that converts screenshot images of T24 Temenos course slides into structured Markdown documentation.

## Context
T24 course content is delivered as slide decks. Manual transcription is slow and error-prone. This tool automates extraction using OCR + AI vision to capture text, tables, and diagram descriptions.

## Pipeline Stages

```
[Input Images] --> [1. Preprocessing] --> [2. OCR Extraction] --> [3. AI Cleaning] --> [4. Output]
```

### Stage 1: Image Preprocessing
- Grayscale conversion
- CLAHE contrast enhancement (clipLimit=2.0, tileGrid=8x8)
- Non-local means denoising
- Otsu binary thresholding
- Compares basic vs enhanced OCR output, keeps the longer result

### Stage 2: OCR Extraction
- Tesseract with `--oem 3 --psm 6` (LSTM engine, uniform block)
- Per-slide text files saved to `ocr_output/`
- Common OCR ligature fixes applied (rn->m, cl->d, etc.)
- Metadata captured per slide (text length, preprocessing improvement flag)

### Stage 3: AI Cleaning
- Text sent to AI provider with T24-specific system prompt
- Vision-capable models (gpt-4o, Claude) receive the source image alongside OCR text
- Large content auto-chunked (5 slides per chunk, ~8000 char threshold)
- 3 retries with exponential backoff; fallback to basic formatting on failure
- Cost tracked per request when enabled

### Stage 4: Output
- `cleaned_content.md` -- final structured Markdown
- `raw_extracted_text.txt` -- raw OCR for comparison
- `cost_report.txt` / `cost_data.json` -- API usage (optional)
- `processing_summary.md` -- run stats

## Key Classes

| Class | Role |
|-------|------|
| `MultiAIOCR` | Pipeline orchestrator |
| `AIProvider` | Abstract base for AI calls |
| `OpenAIProvider` | GPT-4o vision integration |
| `AnthropicProvider` | Claude vision integration |
| `CostTracker` | Token counting and cost calculation |

## Files Touched
- `coursescribe.py` -- single-file implementation
