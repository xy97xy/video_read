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


def make_comparison(original: Image.Image, enhanced: Image.Image) -> Image.Image:
    """Side-by-side image: original left, enhanced right, 4px grey divider."""
    orig = original.convert("RGB")
    enh = enhanced.convert("RGB")

    h = max(orig.height, enh.height)
    if orig.height != h:
        orig = orig.resize((int(orig.width * h / orig.height), h), Image.LANCZOS)
    if enh.height != h:
        enh = enh.resize((int(enh.width * h / enh.height), h), Image.LANCZOS)

    divider = 4
    out = Image.new("RGB", (orig.width + divider + enh.width, h), color=(128, 128, 128))
    out.paste(orig, (0, 0))
    out.paste(enh, (orig.width + divider, 0))
    return out
