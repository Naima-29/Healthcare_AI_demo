
"""OCR utilities for patient-provided laboratory report images."""

from __future__ import annotations

from pathlib import Path
from io import BytesIO
import pytesseract
from PIL import Image, ImageOps


# Windows Tesseract installation path.
# This can be overridden with the TESSERACT_CMD environment variable.
pytesseract.pytesseract.tesseract_cmd = str(
    Path(
        __import__("os").environ.get(
            "TESSERACT_CMD",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        )
    )
)


def extract_text(image_path: str | Path) -> str:
    """Extract text from a laboratory report image."""
    path = Path(image_path)

    if not path.is_file():
        raise FileNotFoundError(f"OCR image not found: {path}")

    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        text = pytesseract.image_to_string(image, lang="eng")

    text = text.strip()

    if not text:
        raise ValueError("No readable text found in the laboratory image")

    return text


def extract_text_from_bytes(image_bytes: bytes) -> str:
    """Extract text directly from downloaded image bytes."""
    if not image_bytes:
        raise ValueError("The downloaded image is empty")

    with Image.open(BytesIO(image_bytes)) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        text = pytesseract.image_to_string(image, lang="eng")

    text = text.strip()

    if not text:
        raise ValueError("No readable text found in the laboratory image")

    return text
