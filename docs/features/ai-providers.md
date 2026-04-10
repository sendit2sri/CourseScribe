# AI Providers

## Summary
CourseScribe supports OpenAI and Anthropic as interchangeable AI backends via a provider abstraction layer.

## Context
Different AI providers offer different vision capabilities and pricing. The abstraction allows switching providers without changing pipeline logic.

## Supported Providers

### OpenAI
- Default model: `gpt-4o` (vision-capable)
- Sends images as base64 `image_url` content blocks
- Token counting via `tiktoken`
- Table-heavy content gets increased `max_tokens` (6000 vs 4000)

### Anthropic
- Default model: `claude-3-5-sonnet-20241022`
- Sends images as base64 `image` source blocks
- Token counting via character estimation (len / 4)

## Usage
```bash
# OpenAI (default)
python coursescribe.py ./slides --provider openai

# Anthropic
python coursescribe.py ./slides --provider anthropic

# Specific model
python coursescribe.py ./slides --provider openai --model gpt-4o
```

## How to Add a New Provider
1. Subclass `AIProvider` in `coursescribe.py`
2. Implement `_initialize_client()` and `call_api()`
3. Add pricing to `CostTracker._get_pricing()`
4. Add to the `--provider` choices in `main()`

## Files Touched
- `coursescribe.py` -- `AIProvider`, `OpenAIProvider`, `AnthropicProvider` classes
