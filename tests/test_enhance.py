import os, sys
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJ)

from PIL import Image
from photos.enhance import enhance_photo, make_comparison


def _make_image(width=100, height=80, color=(100, 100, 100)):
    return Image.new("RGB", (width, height), color=color)


def test_enhance_photo_returns_same_size():
    img = _make_image(200, 150)
    result = enhance_photo(img, quality="good")
    assert result.size == (200, 150)


def test_enhance_photo_standard_changes_pixels():
    img = _make_image(100, 100, color=(80, 80, 80))
    result = enhance_photo(img, quality="good")
    orig_px = img.getpixel((50, 50))
    new_px = result.getpixel((50, 50))
    assert new_px != orig_px


def test_enhance_photo_dark_brighter_than_standard():
    img = _make_image(100, 100, color=(50, 50, 50))
    standard = enhance_photo(img.copy(), quality="good")
    dark = enhance_photo(img.copy(), quality="dark")
    std_brightness = sum(standard.getpixel((50, 50)))
    dark_brightness = sum(dark.getpixel((50, 50)))
    assert dark_brightness > std_brightness


def test_make_comparison_correct_width():
    orig = _make_image(200, 150)
    enh = _make_image(200, 150)
    result = make_comparison(orig, enh)
    # width = orig.width + 4px divider + enh.width
    assert result.width == 200 + 4 + 200


def test_make_comparison_correct_height():
    orig = _make_image(200, 150)
    enh = _make_image(200, 150)
    result = make_comparison(orig, enh)
    assert result.height == 150
