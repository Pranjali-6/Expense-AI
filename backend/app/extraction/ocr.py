"""OCR fallback for scanned statements.

Only ever a fallback. OCR misreads digits — 8 for 3, 1 for 7, a lost decimal
point — and those misreads land directly in someone's ledger. So OCR pages are
marked as such all the way through to ``confidence_extraction``, and a
statement read entirely by OCR cannot silently reach the same trust level as one
read from a text layer: the reconciliation check has to pass on the numbers OCR
produced, which is a genuinely hard test for a bad read.

Tesseract's own per-word confidence is captured and averaged, so the extraction
confidence is measured rather than assumed.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.models.enums import ExtractionMethod

from parsers.document import ExtractedDocument, ExtractedPage

logger = get_logger(__name__)

# Statements are dense, small-point tables. `--psm 6` (a uniform block of text)
# beats the default page-segmentation mode, which tries to find columns and
# shreds a wide table into interleaved fragments.
_TESSERACT_CONFIG = "--oem 1 --psm 6 -c preserve_interword_spaces=1"

_DPI = 300


def ocr_page(data: bytes, page_number: int) -> tuple[str, float | None]:
    """OCR one page. Returns its text and mean word confidence (0–1)."""
    import fitz
    import pytesseract
    from PIL import Image

    with fitz.open(stream=data, filetype="pdf") as document:
        page = document[page_number - 1]
        pixmap = page.get_pixmap(dpi=_DPI, colorspace=fitz.csGRAY)
        image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)

    image = _prepare(image)

    text = pytesseract.image_to_string(image, config=_TESSERACT_CONFIG)
    confidence = _mean_confidence(image)
    return text, confidence


def _prepare(image):
    """Deskew and binarise. Both materially change digit accuracy."""
    try:
        import cv2
        import numpy as np

        array = np.array(image)
        # Otsu picks the threshold from the histogram rather than a constant,
        # which matters because scan brightness varies wildly.
        _, binary = cv2.threshold(array, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        coords = cv2.findNonZero(255 - binary)
        if coords is not None:
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle += 90
            # Only correct genuine skew. Rotating by a fraction of a degree
            # resamples the image and costs more accuracy than it recovers.
            if abs(angle) > 0.75:
                height, width = binary.shape
                matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
                binary = cv2.warpAffine(
                    binary, matrix, (width, height),
                    flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
                )

        from PIL import Image as PILImage

        return PILImage.fromarray(binary)
    except Exception:
        logger.warning("ocr_preprocess_skipped", stage="ocr")
        return image


def _mean_confidence(image) -> float | None:
    import pytesseract

    try:
        data = pytesseract.image_to_data(
            image, config=_TESSERACT_CONFIG, output_type=pytesseract.Output.DICT
        )
    except Exception:
        return None

    scores = [
        float(value) for value in data.get("conf", [])
        if str(value).lstrip("-").replace(".", "", 1).isdigit() and float(value) >= 0
    ]
    if not scores:
        return None
    return round(sum(scores) / len(scores) / 100, 4)


def apply_ocr(data: bytes, document: ExtractedDocument) -> ExtractedDocument:
    """Replace the text of every page that has no usable text layer."""
    for page in document.pages:
        if page.method != ExtractionMethod.OCR:
            continue
        try:
            text, confidence = ocr_page(data, page.page_number)
        except Exception as exc:
            document.warnings.append("ocr_failed")
            logger.error("ocr_failed", stage="ocr", error_code=type(exc).__name__)
            continue

        page.text = text
        page.ocr_confidence = confidence

    return document


def needs_ocr(document: ExtractedDocument) -> bool:
    return any(page.method == ExtractionMethod.OCR for page in document.pages)


def blank_page(page: ExtractedPage) -> bool:
    return page.char_count == 0
