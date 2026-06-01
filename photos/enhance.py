from __future__ import annotations
from PIL import Image, ImageEnhance, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def enhance_photo(img: Image.Image, quality: str | None) -> Image.Image:
    """Apply PIL color correction. Dark photos get stronger brightness/contrast params."""
    img = img.convert("RGB")
    if quality == "dark":
        img = ImageOps.autocontrast(img, cutoff=2)
        img = ImageEnhance.Brightness(img).enhance(1.6)
        img = ImageEnhance.Color(img).enhance(1.3)
        img = ImageEnhance.Sharpness(img).enhance(1.1)
    else:
        img = ImageOps.autocontrast(img, cutoff=1)
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Color(img).enhance(1.2)
        img = ImageEnhance.Sharpness(img).enhance(1.1)
    return img
