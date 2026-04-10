# Image Preprocessing

## Summary
OpenCV-based image enhancement pipeline to improve Tesseract OCR accuracy on slide screenshots.

## Context
Raw screenshots often have uneven lighting, noise, and low contrast that degrade OCR quality. The preprocessing pipeline addresses these before OCR extraction.

## Pipeline Steps

1. **Grayscale conversion** -- `cv2.cvtColor(BGR2GRAY)`
2. **CLAHE** -- Contrast Limited Adaptive Histogram Equalization (clipLimit=2.0, tileGrid=8x8)
3. **Denoising** -- `cv2.fastNlMeansDenoising` for non-local means
4. **Otsu thresholding** -- `cv2.threshold` with `THRESH_BINARY + THRESH_OTSU`

## Quality Check
Both raw and preprocessed images are OCR'd. The result with more extracted characters is kept. The `preprocessing_improved` flag in metadata tracks which won.

## Supported Formats
`.png`, `.jpg`, `.jpeg`, `.tiff`, `.bmp`

## Files Touched
- `coursescribe.py` -- `MultiAIOCR.preprocess_image()`, `MultiAIOCR.extract_text_from_slide()`
