from __future__ import annotations

from PIL import Image

from assistant.perceptual_hash import average_hash_hex, hamming_distance_hex, is_same_chart_content


def _solid(color: tuple[int, int, int], size: int = 64) -> Image.Image:
    return Image.new("RGB", (size, size), color=color)


def _split(left: tuple[int, int, int], right: tuple[int, int, int], size: int = 64) -> Image.Image:
    img = Image.new("RGB", (size, size))
    pixels = img.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = left if x < size // 2 else right
    return img


def test_identical_images_have_zero_distance():
    a = average_hash_hex(_solid((10, 20, 30)))
    b = average_hash_hex(_solid((10, 20, 30)))
    assert hamming_distance_hex(a, b) == 0
    assert is_same_chart_content(a, b)


def test_slightly_different_images_stay_within_threshold():
    # Small brightness nudge -- meant to model an ordinary price-tick/candle
    # update on the same chart, not a real content change.
    a = average_hash_hex(_solid((100, 100, 100)))
    b = average_hash_hex(_solid((104, 104, 104)))
    assert is_same_chart_content(a, b)


def test_structurally_different_images_exceed_threshold():
    a = average_hash_hex(_split((0, 0, 0), (255, 255, 255)))
    b = average_hash_hex(_solid((128, 128, 128)))
    distance = hamming_distance_hex(a, b)
    assert distance > 14, f"expected a clear structural difference, got distance={distance}"
    assert not is_same_chart_content(a, b)


def test_hamming_distance_is_symmetric():
    a = average_hash_hex(_split((0, 0, 0), (255, 255, 255)))
    b = average_hash_hex(_solid((128, 128, 128)))
    assert hamming_distance_hex(a, b) == hamming_distance_hex(b, a)


def test_average_hash_hex_is_deterministic():
    img = _split((10, 20, 30), (200, 210, 220))
    assert average_hash_hex(img) == average_hash_hex(img)
