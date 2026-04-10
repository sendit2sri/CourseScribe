#!/usr/bin/env python3
"""
Multi-AI OCR Tool with Cost Tracking
Supports OpenAI, Anthropic Claude, and other AI providers
Enhanced for tables, diagrams, and screenshots.
"""

import os
import sys
import argparse
import pytesseract
from dotenv import load_dotenv
import json
from PIL import Image
import cv2
import numpy as np
from typing import Dict, List, Tuple, Optional, Any
import time
import logging
from pathlib import Path
import re
from dataclasses import dataclass, field
import base64  # Added for vision API support

# Configure logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Check dependencies
def check_dependencies():
    try:
        import anthropic  # Optional
    except ImportError:
        pass
    try:
        import openai  # Optional
    except ImportError:
        pass
    try:
        import tiktoken  # Optional for OpenAI
    except ImportError:
        pass
    required = ['cv2', 'numpy', 'PIL.Image', 'pytesseract']
    for mod in required:
        try:
            __import__(mod.split('.')[0])
        except ImportError:
            logger.error(f"❌ Missing dependency: {mod}. Install with pip install opencv-python numpy pillow pytesseract")
            sys.exit(1)

check_dependencies()

# =============================================================================
# Cost Tracking Classes (unchanged, but cleaned up)
# =============================================================================

@dataclass
class RequestDetails:
    request_number: int
    chunk_number: int
    timestamp: str
    input_tokens: int
    output_tokens: int
    cost: float
    model: str
    provider: str
    duration: float
    success: bool = True
    error_message: str = ""

@dataclass
class CostSummary:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    total_duration: float = 0.0
    provider: str = ""
    request_details: List[RequestDetails] = field(default_factory=list)

class CostTracker:
    def __init__(self, provider: str = "anthropic"):
        self.provider = provider.lower()
        self.pricing = self._get_pricing()
        self.summary = CostSummary(provider=self.provider)
        self.encoder = None
        self._initialize_tokenizer()
    
    def _get_pricing(self) -> Dict[str, Dict[str, float]]:
        return {
            'anthropic': {
                'claude-3-5-sonnet-20241022': {'input': 0.003, 'output': 0.015},
                'claude-3-5-haiku-20241022': {'input': 0.001, 'output': 0.005},
                'claude-3-opus-20240229': {'input': 0.015, 'output': 0.075},
                'claude-3-sonnet-20240229': {'input': 0.003, 'output': 0.015},
                'claude-3-haiku-20240307': {'input': 0.00025, 'output': 0.00125}
            },
            'openai': {
                'gpt-4o': {'input': 0.005, 'output': 0.015},  # Added for vision
                'gpt-4': {'input': 0.03, 'output': 0.06},
                'gpt-4-turbo': {'input': 0.01, 'output': 0.03},
                'gpt-3.5-turbo': {'input': 0.001, 'output': 0.002}
            }
        }
    
    def _initialize_tokenizer(self):
        try:
            if self.provider == "anthropic":
                self.encoder = None  # Estimate for Claude
            elif self.provider == "openai":
                import tiktoken
                self.encoder = tiktoken.encoding_for_model("gpt-4")
        except Exception:
            logger.warning(f"⚠️ Tokenizer not available, using estimates")
    
    def count_tokens(self, text: str) -> int:
        if self.encoder:
            return len(self.encoder.encode(text))
        return len(text) // 4  # Rough estimate
    
    def calculate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        provider_pricing = self.pricing.get(self.provider, {})
        model = model if model in provider_pricing else ("claude-3-5-sonnet-20241022" if self.provider == "anthropic" else "gpt-4")
        input_cost = (input_tokens / 1000) * provider_pricing[model]['input']
        output_cost = (output_tokens / 1000) * provider_pricing[model]['output']
        return input_cost + output_cost
    
    def start_request(self, input_text: str, model: str, chunk_number: int = 0) -> Dict[str, Any]:
        input_tokens = self.count_tokens(input_text)
        estimated_output_tokens = input_tokens // 3
        estimated_cost = self.calculate_cost(input_tokens, estimated_output_tokens, model)
        return {
            'start_time': time.time(),
            'input_tokens': input_tokens,
            'estimated_cost': estimated_cost,
            'model': model,
            'chunk_number': chunk_number,
            'provider': self.provider
        }
    
    def complete_request(
        self, 
        request_info: Dict[str, Any], 
        output_text: str, 
        actual_input_tokens: Optional[int] = None,
        actual_output_tokens: Optional[int] = None,
        error_message: str = ""
    ) -> RequestDetails:
        end_time = time.time()
        duration = end_time - request_info['start_time']
        input_tokens = actual_input_tokens or request_info['input_tokens']
        output_tokens = actual_output_tokens or self.count_tokens(output_text) if output_text else 0
        actual_cost = self.calculate_cost(input_tokens, output_tokens, request_info['model'])
        success = not error_message
        details = RequestDetails(
            request_number=self.summary.total_requests + 1,
            chunk_number=request_info['chunk_number'],
            timestamp=time.strftime('%Y-%m-%d %H:%M:%S'),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=actual_cost,
            model=request_info['model'],
            provider=self.provider,
            duration=duration,
            success=success,
            error_message=error_message
        )
        self.summary.total_requests += 1
        if success:
            self.summary.successful_requests += 1
            self.summary.total_input_tokens += input_tokens
            self.summary.total_output_tokens += output_tokens
            self.summary.total_cost += actual_cost
        else:
            self.summary.failed_requests += 1
        self.summary.total_duration += duration
        self.summary.request_details.append(details)
        return details
    
    def generate_report(self) -> str:
        if not self.summary.total_requests:
            return "No requests made."
        cost_breakdown = self._get_cost_breakdown()
        avg_cost = self.summary.total_cost / max(1, self.summary.successful_requests)
        report = f"""# Cost and Usage Report - {self.provider.title()}

## Summary
- **AI Provider**: {self.provider.title()}
- **Total API Requests**: {self.summary.total_requests}
- **Successful Requests**: {self.summary.successful_requests}
- **Failed Requests**: {self.summary.failed_requests}
- **Success Rate**: {(self.summary.successful_requests / max(1, self.summary.total_requests) * 100):.1f}%
- **Total Input Tokens**: {self.summary.total_input_tokens:,}
- **Total Output Tokens**: {self.summary.total_output_tokens:,}
- **Total Tokens**: {(self.summary.total_input_tokens + self.summary.total_output_tokens):,}
- **Total Cost**: ${self.summary.total_cost:.4f}
- **Average Cost per Request**: ${avg_cost:.4f}
- **Total Duration**: {self.summary.total_duration:.1f} seconds

## Cost Breakdown
- **Input Cost**: ${cost_breakdown['input_cost']:.4f}
- **Output Cost**: ${cost_breakdown['output_cost']:.4f}

## Request Details
"""
        for detail in self.summary.request_details:
            status = "✅ Success" if detail.success else f"❌ Failed: {detail.error_message}"
            report += f"""
### Request {detail.request_number} (Chunk {detail.chunk_number})
- **Status**: {status}
- **Time**: {detail.timestamp}
- **Duration**: {detail.duration:.1f}s
- **Model**: {detail.model}
- **Provider**: {detail.provider}
- **Tokens**: {detail.input_tokens:,} in + {detail.output_tokens:,} out
- **Cost**: ${detail.cost:.4f}
"""
        return report
    
    def _get_cost_breakdown(self) -> Dict[str, float]:
        default_model = "claude-3-5-sonnet-20241022" if self.provider == "anthropic" else "gpt-4"
        model_pricing = self.pricing[self.provider].get(default_model, {'input': 0.003, 'output': 0.015})
        input_cost = (self.summary.total_input_tokens / 1000) * model_pricing['input']
        output_cost = (self.summary.total_output_tokens / 1000) * model_pricing['output']
        return {"input_cost": input_cost, "output_cost": output_cost}

# =============================================================================
# AI Provider Interface (enhanced for vision)
# =============================================================================

class AIProvider:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.client = None
        self._initialize_client()
    
    def _initialize_client(self):
        raise NotImplementedError
    
    def call_api(self, system_prompt: str, user_prompt: str, image_path: Optional[Path] = None) -> Tuple[str, Dict[str, Any]]:
        raise NotImplementedError

class AnthropicProvider(AIProvider):
    def _initialize_client(self):
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.api_key)
            logger.info(f"✅ Anthropic client initialized with model: {self.model}")
        except ImportError:
            logger.error("❌ Install anthropic: pip install anthropic")
            sys.exit(1)
        except Exception as e:
            logger.error(f"❌ Anthropic init failed: {e}")
            sys.exit(1)
    
    def call_api(self, system_prompt: str, user_prompt: str, image_path: Optional[Path] = None) -> Tuple[str, Dict[str, Any]]:
        try:
            content = [{"type": "text", "text": user_prompt}]
            if image_path:  # Vision support (Claude has it)
                with open(image_path, "rb") as img_file:
                    img_data = base64.b64encode(img_file.read()).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_data}
                })
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                temperature=0.1,
                system=system_prompt,
                messages=[{"role": "user", "content": content}]
            )
            return response.content[0].text, {'input_tokens': response.usage.input_tokens, 'output_tokens': response.usage.output_tokens}
        except Exception as e:
            logger.error(f"Anthropic API failed: {e}")
            raise

class OpenAIProvider(AIProvider):
    def _initialize_client(self):
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key)
            self.version = "new"
        except ImportError:
            logger.error("❌ Install openai: pip install openai")
            sys.exit(1)
        except Exception as e:
            logger.error(f"❌ OpenAI init failed: {e}")
            sys.exit(1)
    
    def call_api(self, system_prompt: str, user_prompt: str, image_path: Optional[Path] = None) -> Tuple[str, Dict[str, Any]]:
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [{"type": "text", "text": user_prompt}]}
            ]
            if image_path:  # Vision support (e.g., gpt-4o)
                with open(image_path, "rb") as img_file:
                    img_data = base64.b64encode(img_file.read()).decode("utf-8")
                messages[1]["content"].append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_data}"}
                })
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
                max_tokens=6000 if 'table' in user_prompt.lower() else 4000
            )
            content = response.choices[0].message.content
            usage = response.usage
            return content, {'input_tokens': usage.prompt_tokens, 'output_tokens': usage.completion_tokens}
        except Exception as e:
            logger.error(f"OpenAI API failed: {e}")
            raise

# =============================================================================
# Main OCR Tool Class (enhanced)
# =============================================================================

class MultiAIOCR:
    def __init__(
        self, 
        input_dir: str, 
        content_type: str = "course", 
        ai_provider: str = "anthropic",
        model: str = None,
        enable_cost_tracking: bool = False
    ):
        self.input_dir = Path(input_dir).resolve()
        self.content_type = content_type
        self.ai_provider = ai_provider.lower()
        self.enable_cost_tracking = enable_cost_tracking
        if model is None:
            model = "claude-3-5-sonnet-20241022" if self.ai_provider == "anthropic" else "gpt-4o"  # Default to vision-capable
        self.model = model
        self.output_dir = self.input_dir / "ocr_output"
        self._load_environment()
        self._initialize_ai_provider()
        self._initialize_ocr()
        self.cost_tracker = CostTracker(self.ai_provider) if enable_cost_tracking else None
        self.stats = {'total_slides': 0, 'processed_slides': 0, 'failed_slides': 0, 'total_characters': 0, 'processing_time': 0}
        self.ocr_fixes = {
            'rn': 'm', 'cl': 'd', 'arn': 'am', 'vv': 'w', 'nn': 'n',
            '|': 'I', '~': '-', '°': '•', 'fi': 'fi', 'fl': 'fl',
            'tlie': 'the', 'wlien': 'when', 'wliich': 'which', 
            'witli': 'with', 'tliis': 'this'
        }
    
    def _load_environment(self):
        load_dotenv()
        key_var = "ANTHROPIC_API_KEY" if self.ai_provider == "anthropic" else "OPENAI_API_KEY"
        self.api_key = os.getenv(key_var)
        if not self.api_key:
            logger.error(f"❌ {key_var} not found in .env")
            sys.exit(1)
        logger.info("✅ Environment loaded")
    
    def _initialize_ai_provider(self):
        if self.ai_provider == "anthropic":
            self.ai_client = AnthropicProvider(self.api_key, self.model)
        elif self.ai_provider == "openai":
            self.ai_client = OpenAIProvider(self.api_key, self.model)
        else:
            logger.error(f"❌ Unsupported provider: {self.ai_provider}")
            sys.exit(1)
    
    def _initialize_ocr(self):
        try:
            pytesseract.get_tesseract_version()
            logger.info("✅ OCR initialized")
        except Exception as e:
            logger.error(f"❌ OCR init failed: {e}. Install tesseract.")
            sys.exit(1)
    
    def validate_input(self) -> bool:
        if not self.input_dir.exists() or not any(self.input_dir.glob('*.[pj][np]g')):  # Simplified check
            logger.error("❌ Invalid input dir or no images")
            return False
        logger.info("✅ Input validated")
        return True
    
    def organize_files(self) -> List[Path]:
        image_extensions = {'.png', '.jpg', '.jpeg', '.tiff', '.bmp'}
        candidates = sorted([f for f in self.input_dir.iterdir() if f.suffix.lower() in image_extensions], key=lambda x: x.stat().st_mtime)
        organized = []
        for i, f in enumerate(candidates, 1):
            new_name = f"slide_{i:03d}{f.suffix}"
            new_path = self.input_dir / new_name
            if f.name != new_name and not new_path.exists():
                f.rename(new_path)
            organized.append(new_path if new_path.exists() else f)
        logger.info(f"✅ Organized {len(organized)} files")
        return organized
    
    def preprocess_image(self, image_path: Path) -> Optional[np.ndarray]:
        try:
            image = cv2.imread(str(image_path))
            if image is None:
                return None
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            enhanced = clahe.apply(gray)
            denoised = cv2.fastNlMeansDenoising(enhanced)
            _, thresh = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            return thresh
        except Exception as e:
            logger.warning(f"⚠️ Preprocess failed for {image_path}: {e}")
            return None
    
    def extract_text_from_slide(self, slide_path: Path) -> Tuple[str, Dict]:
        try:
            basic_text = pytesseract.image_to_string(Image.open(slide_path), config='--oem 3 --psm 6')
            preprocessed = self.preprocess_image(slide_path)
            enhanced_text = pytesseract.image_to_string(Image.fromarray(preprocessed), config='--oem 3 --psm 6') if preprocessed is not None else ""
            final_text = enhanced_text if len(enhanced_text) > len(basic_text) else basic_text
            final_text = self._clean_ocr_text(final_text)
            metadata = {
                'slide_name': slide_path.name,
                'text_length': len(final_text),
                'preprocessing_improved': len(enhanced_text) > len(basic_text),
                'extraction_successful': bool(final_text.strip())
            }
            return final_text.strip(), metadata
        except Exception as e:
            logger.error(f"❌ OCR failed for {slide_path}: {e}")
            return "", {'error': str(e)}
    
    def _clean_ocr_text(self, text: str) -> str:
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n[ \t]+', '\n', text)
        text = re.sub(r'[ \t]+\n', '\n', text)
        for wrong, correct in self.ocr_fixes.items():
            text = text.replace(wrong, correct)
        return text.strip()
    
    def process_all_slides(self, slide_files: List[Path]) -> Tuple[str, List[Dict]]:
        self.output_dir.mkdir(exist_ok=True)
        self.stats['total_slides'] = len(slide_files)
        full_text = ""
        metadata_list = []
        for i, slide_path in enumerate(slide_files, 1):
            logger.info(f"Processing slide {i}/{len(slide_files)}: {slide_path.name}")
            text, metadata = self.extract_text_from_slide(slide_path)
            slide_header = f"\n# Slide {i:03d}: {slide_path.name}\n\n"
            if text:
                full_text += slide_header + text + "\n\n"
                (self.output_dir / f"{slide_path.stem}.txt").write_text(text, encoding='utf-8')
                self.stats['processed_slides'] += 1
                self.stats['total_characters'] += len(text)
            else:
                full_text += slide_header + "[No text extracted]\n\n"
                self.stats['failed_slides'] += 1
            metadata['slide_number'] = i
            metadata_list.append(metadata)
        (self.input_dir / "raw_extracted_text.txt").write_text(full_text, encoding='utf-8')
        (self.output_dir / "extraction_metadata.json").write_text(json.dumps(metadata_list, indent=2), encoding='utf-8')
        logger.info(f"✅ Extraction complete: {self.stats['processed_slides']}/{self.stats['total_slides']}")
        return full_text, metadata_list
    
    def get_cleaning_prompts(self) -> Tuple[str, str]:
        system_prompt = f"""You are an expert OCR cleaner for T24 Temenos banking application screenshots ({self.content_type} slides). Extract EVERY detail without omission:. Fix errors, structure content, describe tables/diagrams/screenshots if image provided.
FORMATTING: Use Markdown. Sections for text, tables (as Markdown tables), diagram descriptions (e.g., 'Workflow: Step 1 -> Step 2').
Preserve all info, add descriptions like 'The diagram shows a workflow with arrows from A to B'."""
        user_prompt_template = """Clean this OCR text. If image, describe tables/diagrams/screenshots.
RAW CONTENT: {content}"""
        return system_prompt, user_prompt_template
    
    def clean_with_ai(self, extracted_text: str, slide_files: List[Path]) -> str:
        logger.info(f"🤖 Cleaning with {self.ai_provider} ({self.model})")
        system_prompt, user_prompt_template = self.get_cleaning_prompts()
        if len(extracted_text) > 8000:
            return self._process_large_content(extracted_text, system_prompt, user_prompt_template, slide_files)
        user_prompt = user_prompt_template.format(content=extracted_text)
        return self._make_ai_request(system_prompt, user_prompt, slide_files[0] if slide_files else None)  # Use first image for demo
    
    def _process_large_content(self, content: str, system_prompt: str, user_prompt_template: str, slide_files: List[Path]) -> str:
        slides = re.split(r'(# Slide \d+:.*?)\n\n', content)
        processed_chunks = []
        chunk_size = 5
        slide_index = 0
        for chunk_num in range(1, (len(slides) // 2 // chunk_size) + 2):
            current_chunk = ""
            for _ in range(chunk_size):
                if slide_index * 2 + 1 < len(slides):
                    header = slides[slide_index * 2 + 1]
                    text = slides[slide_index * 2 + 2] if slide_index * 2 + 2 < len(slides) else ""
                    current_chunk += header + "\n\n" + text + "\n\n"
                    slide_index += 1
            if current_chunk:
                user_prompt = user_prompt_template.format(content=current_chunk)
                image_path = slide_files[chunk_num - 1] if chunk_num - 1 < len(slide_files) else None  # Associate image
                processed = self._make_ai_request(system_prompt, user_prompt, image_path, chunk_num)
                processed_chunks.append(processed)
        return "\n\n---\n\n".join(processed_chunks)
    
    def _make_ai_request(self, system_prompt: str, user_prompt: str, image_path: Optional[Path] = None, chunk_number: int = 0) -> str:
        request_info = self.cost_tracker.start_request(system_prompt + user_prompt, self.model, chunk_number) if self.cost_tracker else None
        for attempt in range(3):
            try:
                content, usage_info = self.ai_client.call_api(system_prompt, user_prompt, image_path)
                if self.cost_tracker:
                    self.cost_tracker.complete_request(request_info, content, usage_info.get('input_tokens'), usage_info.get('output_tokens'))
                return content
            except Exception as e:
                if attempt == 2:
                    if self.cost_tracker:
                        self.cost_tracker.complete_request(request_info, "", error_message=str(e))
                    return self._apply_fallback_formatting(user_prompt)
                time.sleep(2 ** attempt)
    
    def _apply_fallback_formatting(self, text: str) -> str:
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        formatted = []
        for line in lines:
            if line.startswith('# Slide'):
                formatted.append(f"\n{line}\n")
            elif line.startswith('*') or line.startswith('•'):
                formatted.append(f"• {line[1:].strip()}")
            else:
                formatted.append(line)
        return '\n'.join(formatted) + "\n[Visual descriptions skipped due to error]"
    
    def run(self) -> bool:
        start_time = time.time()
        if not self.validate_input():
            return False
        slide_files = self.organize_files()
        extracted_text, metadata = self.process_all_slides(slide_files)
        if not extracted_text.strip():
            return False
        cleaned_content = self.clean_with_ai(extracted_text, slide_files)
        (self.input_dir / "cleaned_content.md").write_text(cleaned_content, encoding='utf-8')  # Changed to .md for Markdown
        processing_time = time.time() - start_time
        self.stats['processing_time'] = processing_time
        summary = self._generate_summary(cleaned_content, processing_time)
        (self.output_dir / "processing_summary.md").write_text(summary, encoding='utf-8')
        if self.cost_tracker:
            cost_report = self.cost_tracker.generate_report()
            (self.input_dir / "cost_report.txt").write_text(cost_report, encoding='utf-8')
            cost_data = {  # Simplified
                'summary': vars(self.cost_tracker.summary),
                'requests': [vars(d) for d in self.cost_tracker.summary.request_details]
            }
            (self.input_dir / "cost_data.json").write_text(json.dumps(cost_data, indent=2), encoding='utf-8')
        logger.info(f"🎉 Complete! Cleaned file: {self.input_dir / 'cleaned_content.md'}")
        return True
    
    def _generate_summary(self, cleaned_content: str, processing_time: float) -> str:
        success_rate = (self.stats['processed_slides'] / self.stats['total_slides']) * 100 if self.stats['total_slides'] else 0
        summary = f"""# OCR Summary

## Results
- Total Slides: {self.stats['total_slides']}
- Processed: {self.stats['processed_slides']}
- Success Rate: {success_rate:.1f}%
- Time: {processing_time:.1f}s

## AI Config
- Provider: {self.ai_provider}
- Model: {self.model}

## Cost
"""
        if self.cost_tracker:
            summary += f"- Total Cost: ${self.cost_tracker.summary.total_cost:.4f}"
        else:
            summary += "- Tracking: Disabled"
        summary += f"\n\n## Preview\n{cleaned_content[:400]}..."
        return summary

def main():
    parser = argparse.ArgumentParser(description="Enhanced Multi-AI OCR Tool")
    parser.add_argument("input_dir", nargs="?", default=".", help="Slides directory")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default="openai")
    parser.add_argument("--model", help="Model (e.g., gpt-4o for vision)")
    parser.add_argument("--type", choices=["course", "presentation", "technical"], default="course")
    parser.add_argument("--cost-tracking", action="store_true")
    args = parser.parse_args()
    processor = MultiAIOCR(args.input_dir, args.type, args.provider, args.model, args.cost_tracking)
    processor.run()

if __name__ == "__main__":
    main()