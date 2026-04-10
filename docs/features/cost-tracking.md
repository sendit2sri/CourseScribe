# Cost Tracking

## Summary
Optional per-request API cost tracking with token counting and report generation.

## Context
AI API calls have real dollar costs. Cost tracking helps monitor spend during batch processing of large slide decks.

## How It Works
- Enabled via `--cost-tracking` flag
- `CostTracker` records input/output tokens, cost, duration, and success/failure per request
- Token counting: `tiktoken` for OpenAI, character estimate for Anthropic
- Cost calculated from hardcoded pricing table (per 1K tokens)

## Output Files
- `cost_report.txt` -- human-readable Markdown report
- `cost_data.json` -- machine-readable JSON with per-request details

## Pricing Table (hardcoded)

| Provider | Model | Input/1K | Output/1K |
|----------|-------|----------|-----------|
| OpenAI | gpt-4o | $0.005 | $0.015 |
| OpenAI | gpt-4 | $0.030 | $0.060 |
| Anthropic | claude-3-5-sonnet | $0.003 | $0.015 |
| Anthropic | claude-3-opus | $0.015 | $0.075 |

## Files Touched
- `coursescribe.py` -- `CostTracker`, `CostSummary`, `RequestDetails` classes
