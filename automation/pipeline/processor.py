"""Per-page OCR + AI processing pipeline.

Integrates with the existing coursescribe.py by importing its classes
directly. Each page is processed independently (not batched).
"""

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from automation.capture.screenshot import CaptureResult
from automation.config import AutomationConfig
from automation.pipeline.classifier import ContentClassifier
from automation.state.manifest import PageInfo

logger = logging.getLogger(__name__)

# Ensure the project root is importable
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@dataclass
class ProcessingResult:
    """Result of processing a single captured page."""

    page_info: PageInfo
    raw_text_path: Optional[Path] = None
    cleaned_md_path: Optional[Path] = None
    raw_text_length: int = 0
    content_type: str = "text_heavy"
    ai_request_count: int = 0
    success: bool = False
    low_quality: bool = False
    review_reason: Optional[str] = None
    error: Optional[str] = None
    cost_data: Dict[str, Any] = field(default_factory=dict)


class PageProcessor:
    """Processes a single captured page through OCR + AI cleaning.

    Uses the existing coursescribe.py classes:
      - AIProvider / AnthropicProvider / OpenAIProvider for AI calls
      - CostTracker for cost accounting
      - MultiAIOCR.preprocess_image / extract_text_from_slide for OCR
    """

    def __init__(self, config: AutomationConfig):
        self.config = config
        self._provider = None
        self._cost_tracker = None
        self._ocr_instance = None
        self._initialized = False

    def _lazy_init(self) -> None:
        """Lazy initialization of AI provider and OCR tools."""
        if self._initialized:
            return

        from dotenv import load_dotenv
        import os

        load_dotenv()

        # Initialize AI provider from coursescribe
        from coursescribe import (
            AnthropicProvider,
            CostTracker,
            MultiAIOCR,
            OpenAIProvider,
        )

        key_var = (
            "ANTHROPIC_API_KEY"
            if self.config.ai_provider == "anthropic"
            else "OPENAI_API_KEY"
        )
        api_key = os.getenv(key_var)
        if not api_key:
            raise RuntimeError(
                f"{key_var} not found in environment. "
                f"Set it in .env or export it."
            )

        model = self.config.model
        if model is None:
            model = (
                "claude-3-5-sonnet-20241022"
                if self.config.ai_provider == "anthropic"
                else "gpt-4o"
            )

        if self.config.ai_provider == "anthropic":
            self._provider = AnthropicProvider(api_key, model)
        else:
            self._provider = OpenAIProvider(api_key, model)

        if self.config.enable_cost_tracking:
            self._cost_tracker = CostTracker(self.config.ai_provider)

        # Create a lightweight MultiAIOCR instance for OCR methods
        # We use a temp dir since we only need its preprocessing/extraction methods
        self._ocr_instance = MultiAIOCR.__new__(MultiAIOCR)
        self._ocr_instance.ocr_fixes = {
            "rn": "m", "cl": "d", "arn": "am", "vv": "w", "nn": "n",
            "|": "I", "~": "-", "\u00b0": "\u2022", "fi": "fi", "fl": "fl",
            "tlie": "the", "wlien": "when", "wliich": "which",
            "witli": "with", "tliis": "this",
        }

        self._initialized = True
        logger.info(
            f"Processor initialized: {self.config.ai_provider} ({model})"
        )

    def process_page(
        self,
        capture_result: CaptureResult,
        lesson_dir: Path,
        classifier: Optional[ContentClassifier] = None,
    ) -> ProcessingResult:
        """Full processing pipeline for one captured page.

        Two modes:
          - Vision mode (default): send full screenshot directly to AI
          - Legacy OCR mode: Tesseract OCR → AI cleaning → crop processing
        """
        self._lazy_init()

        page_info = capture_result.page_info
        result = ProcessingResult(page_info=page_info)

        cleaned_dir = lesson_dir / "cleaned"
        cleaned_dir.mkdir(parents=True, exist_ok=True)

        prefix = f"page_{page_info.page_index:03d}"

        try:
            screenshot_path = capture_result.full_page_path
            if not screenshot_path or not screenshot_path.exists():
                result.error = "No screenshot available"
                return result

            # Content classification (works for both modes)
            content_type = "text_heavy"
            if classifier and screenshot_path.exists():
                try:
                    content_type = classifier.classify_from_image(screenshot_path)
                except Exception:
                    pass
            result.content_type = content_type

            if self.config.vision_mode:
                # Vision-first: send full screenshot directly to AI
                logger.info(
                    "Using vision-first extraction for page %s",
                    page_info.page_title,
                )
                cleaned_text = self._extract_via_vision(
                    screenshot_path, page_info, content_type
                )
                result.ai_request_count += 1

                # Vision quality check
                if self._is_low_quality_vision_response(cleaned_text):
                    result.low_quality = True
                    result.review_reason = "Vision extraction returned very little content"

            else:
                # Legacy OCR pipeline
                logger.info(
                    "Using legacy OCR pipeline for page %s",
                    page_info.page_title,
                )
                raw_text_dir = lesson_dir / "raw_text"
                raw_text_dir.mkdir(parents=True, exist_ok=True)

                raw_text, ocr_metadata = self._extract_text(screenshot_path)
                result.raw_text_length = len(raw_text)

                # Save raw text
                raw_path = raw_text_dir / f"{prefix}_raw.txt"
                raw_path.write_text(raw_text, encoding="utf-8")
                result.raw_text_path = raw_path

                # AI cleaning with page context
                cleaned_text = self._clean_page(
                    raw_text, screenshot_path, page_info, content_type
                )
                result.ai_request_count += 1

                # Process section crops if present
                if capture_result.section_crops:
                    crop_texts = self._process_crops(
                        capture_result.section_crops, page_info
                    )
                    result.ai_request_count += len(crop_texts)
                    if crop_texts:
                        cleaned_text += "\n\n---\n\n## Content Sections\n\n"
                        cleaned_text += "\n\n".join(crop_texts)

                # OCR quality check
                if result.raw_text_length < self.config.low_quality_char_threshold:
                    result.low_quality = True
                    result.review_reason = (
                        f"Low OCR character count ({result.raw_text_length} chars)"
                    )
                elif len(cleaned_text) < result.raw_text_length * 0.2:
                    result.low_quality = True
                    result.review_reason = "AI output much shorter than OCR input"

            # Save cleaned markdown
            cleaned_path = cleaned_dir / f"{prefix}_cleaned.md"
            cleaned_path.write_text(cleaned_text, encoding="utf-8")
            result.cleaned_md_path = cleaned_path

            # Cost tracking
            if self._cost_tracker:
                summary = self._cost_tracker.summary
                result.cost_data = {
                    "cost": summary.total_cost,
                    "requests": summary.total_requests,
                    "input_tokens": summary.total_input_tokens,
                    "output_tokens": summary.total_output_tokens,
                }

            result.success = True
            logger.info(
                f"Processed {prefix}: type={content_type}, "
                f"mode={'vision' if self.config.vision_mode else 'ocr'}, "
                f"quality={'LOW' if result.low_quality else 'OK'}"
            )

        except Exception as e:
            result.error = str(e)
            logger.error(f"Processing failed for {prefix}: {e}")

        return result

    def _extract_text(self, image_path: Path) -> Tuple[str, Dict]:
        """Run Tesseract OCR on a screenshot, reusing coursescribe methods."""
        try:
            return self._ocr_instance.extract_text_from_slide(image_path)
        except Exception as e:
            logger.warning(f"OCR extraction failed: {e}, using empty text")
            return "", {"error": str(e)}

    def _extract_via_vision(
        self,
        image_path: Path,
        page_info: PageInfo,
        content_type: str,
    ) -> str:
        """Send full screenshot directly to AI for content extraction."""
        content_type = content_type or "general"

        if content_type == "table":
            content_type_extension = (
                "\n- Reproduce all visible tables as valid Markdown tables\n"
                "- Preserve row/column relationships exactly\n"
            )
        elif content_type == "diagram":
            content_type_extension = (
                "\n- Describe diagrams, flowcharts, trees, and boxes clearly "
                "in Markdown\n"
                "- Include all visible node labels, arrows, branches, and "
                "relationships\n"
            )
        else:
            content_type_extension = (
                "\n- Preserve headings, bullets, numbered steps, and "
                "paragraph structure\n"
            )

        system_prompt = (
            "You are an expert at extracting structured content from T24 "
            "Temenos banking course screenshots.\n\n"
            "Extract ALL meaningful visible content from the screenshot.\n\n"
            f"Context:\n"
            f"- Module: {page_info.module_name}\n"
            f"- Lesson: {page_info.lesson_name}\n"
            f"- Page: {page_info.page_title}\n\n"
            "Rules:\n"
            "- Extract text exactly as shown whenever readable\n"
            "- Preserve T24 field names, service names, transaction codes, "
            "identifiers, and abbreviations exactly\n"
            "- Reproduce tables as Markdown tables with exact values and "
            "headers\n"
            "- Describe diagrams/flowcharts/trees with all visible nodes, "
            "labels, and connections\n"
            "- Preserve section hierarchy using Markdown headings and "
            "bullets\n"
            "- Ignore navigation UI, browser chrome, sidebars, page "
            "counters, zoom controls, and next/previous buttons\n"
            "- Do not invent unreadable text; mark uncertain content as "
            "[unclear]\n"
            "- Output only clean Markdown\n"
            + content_type_extension
        )

        user_prompt = (
            "Extract all meaningful content from this course page "
            "screenshot into clean Markdown."
        )

        return self._make_ai_request(system_prompt, user_prompt, image_path)

    @staticmethod
    def _is_low_quality_vision_response(text: str) -> bool:
        """Check if vision extraction returned too little content."""
        if not text:
            return True
        stripped = text.strip()
        if len(stripped) < 50:
            return True
        if stripped in (
            "[unclear]",
            "No readable content found.",
            "No content detected.",
        ):
            return True
        return False

    def _clean_page(
        self,
        raw_text: str,
        image_path: Path,
        page_info: PageInfo,
        content_type: str,
    ) -> str:
        """Send page through AI cleaning with context-aware prompt."""
        from automation.pipeline.classifier import ContentClassifier

        # Build system prompt with content-type specialization
        base_prompt = (
            f"You are an expert OCR cleaner for T24 Temenos banking application "
            f"screenshots ({self.config.content_type} content). "
            f"Extract EVERY detail without omission. Fix OCR errors, structure content, "
            f"describe tables/diagrams/screenshots if image provided.\n\n"
            f"CONTEXT: Module '{page_info.module_name}', "
            f"Lesson '{page_info.lesson_name}', "
            f"Page '{page_info.page_title}'.\n\n"
            f"FORMATTING: Use Markdown. Sections for text, tables (as Markdown tables), "
            f"diagram descriptions.\n\n"
        )
        type_extension = ContentClassifier.get_prompt_extension(content_type)
        system_prompt = base_prompt + type_extension

        user_prompt = (
            f"Clean this OCR text from page '{page_info.page_title}'. "
            f"If image is provided, use it to verify and correct the OCR output.\n\n"
            f"RAW OCR CONTENT:\n{raw_text}"
        )

        return self._make_ai_request(system_prompt, user_prompt, image_path)

    def _process_crops(
        self, crops: List[Tuple[Path, str]], page_info: PageInfo
    ) -> List[str]:
        """Process each section crop with content-type-specific prompts."""
        from automation.pipeline.classifier import ContentClassifier

        results = []
        for crop_path, label in crops:
            if not crop_path.exists():
                continue

            type_extension = ContentClassifier.get_prompt_extension(label)
            system_prompt = (
                f"You are an expert at extracting structured content from "
                f"T24 banking application screenshots. {type_extension}"
            )
            user_prompt = (
                f"Extract all content from this {label} image. "
                f"This is from: Module '{page_info.module_name}', "
                f"Lesson '{page_info.lesson_name}'."
            )

            try:
                text = self._make_ai_request(system_prompt, user_prompt, crop_path)
                results.append(f"### {label.replace('_', ' ').title()}\n\n{text}")
            except Exception as e:
                logger.warning(f"Crop processing failed for {crop_path.name}: {e}")
                results.append(f"### {label} [extraction failed: {e}]")

        return results

    def _make_ai_request(
        self,
        system_prompt: str,
        user_prompt: str,
        image_path: Optional[Path] = None,
    ) -> str:
        """Make a single AI request with retry logic."""
        request_info = None
        if self._cost_tracker:
            request_info = self._cost_tracker.start_request(
                system_prompt + user_prompt, self._provider.model
            )

        for attempt in range(3):
            try:
                content, usage_info = self._provider.call_api(
                    system_prompt, user_prompt, image_path
                )
                if self._cost_tracker and request_info:
                    self._cost_tracker.complete_request(
                        request_info,
                        content,
                        usage_info.get("input_tokens"),
                        usage_info.get("output_tokens"),
                    )
                return content
            except Exception as e:
                if attempt == 2:
                    if self._cost_tracker and request_info:
                        self._cost_tracker.complete_request(
                            request_info, "", error_message=str(e)
                        )
                    logger.error(f"AI request failed after 3 attempts: {e}")
                    return f"[AI extraction failed: {e}]\n\nRaw text preserved above."
                wait = 2**attempt
                logger.warning(f"AI request attempt {attempt+1} failed, retrying in {wait}s")
                time.sleep(wait)

        return ""  # unreachable but satisfies type checker

    def get_cumulative_cost(self) -> Optional[Dict[str, Any]]:
        """Return accumulated cost data across all processed pages."""
        if not self._cost_tracker:
            return None
        s = self._cost_tracker.summary
        return {
            "total_cost": s.total_cost,
            "total_requests": s.total_requests,
            "total_input_tokens": s.total_input_tokens,
            "total_output_tokens": s.total_output_tokens,
        }
