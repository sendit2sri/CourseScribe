# CourseScribe

Multi-AI OCR pipeline for extracting, cleaning, and structuring content from T24 Temenos course screenshots. Supports OpenAI (GPT-4o vision) and Anthropic Claude with cost tracking.

## What It Does

1. **Captures** slide/screenshot images from a directory
2. **Extracts** text via Tesseract OCR with image preprocessing (CLAHE, denoising, Otsu thresholding)
3. **Cleans & structures** extracted text using AI (OpenAI or Anthropic) with vision support for tables/diagrams
4. **Outputs** structured Markdown with cost reports

## Quick Start

```bash
# Clone
git clone https://github.com/sendit2sri/CourseScribe.git
cd CourseScribe

# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API key

# Ensure Tesseract is installed
brew install tesseract  # macOS

# Run
python coursescribe.py /path/to/slides --provider openai --cost-tracking
```

## Usage

```bash
python coursescribe.py <input_dir> [options]

Options:
  --provider {openai,anthropic}   AI provider (default: openai)
  --model MODEL                   Model name (default: gpt-4o / claude-3-5-sonnet)
  --type {course,presentation,technical}  Content type (default: course)
  --cost-tracking                 Enable API cost tracking
```

## Output

| File | Description |
|------|-------------|
| `cleaned_content.md` | AI-cleaned structured Markdown |
| `raw_extracted_text.txt` | Raw OCR output |
| `ocr_output/slide_NNN.txt` | Per-slide extracted text |
| `ocr_output/extraction_metadata.json` | Extraction stats |
| `ocr_output/processing_summary.md` | Run summary |
| `cost_report.txt` | API cost breakdown (if enabled) |
| `cost_data.json` | Machine-readable cost data (if enabled) |

## Prerequisites

- Python 3.10+
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) installed and on PATH
- OpenAI or Anthropic API key

## Project Structure

```
CourseScribe/
  coursescribe.py      # Main pipeline script
  requirements.txt     # Python dependencies
  .env.example         # API key template
  .gitignore
  docs/                # Documentation
```

## License

MIT
