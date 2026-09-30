import pytest

from app.urls import parse_instagram_url


@pytest.mark.parametrize("raw", [
    "https://www.instagram.com/reel/DceO7gsR0w-/",
    "https://www.instagram.com/valorant_funzone/reel/DceO7gsR0w-/",
    "instagram.com/reel/DceO7gsR0w-",
    "https://instagram.com/reels/DceO7gsR0w-/?igsh=abc123",
    "https://m.instagram.com/p/DceO7gsR0w-/",
    "  https://www.instagram.com/reel/DceO7gsR0w-/#comments  ",
])
def test_valid_variants_share_shortcode(raw):
    parsed = parse_instagram_url(raw)
    assert parsed.ok
    assert parsed.shortcode == "DceO7gsR0w-"
    assert parsed.canonical_url == "https://www.instagram.com/reel/DceO7gsR0w-/"


def test_shortcode_is_case_sensitive():
    assert parse_instagram_url("https://www.instagram.com/REEL/AbCdEf123/").shortcode == "AbCdEf123"


@pytest.mark.parametrize("raw,code", [
    ("", "invalid_url"),
    ("not a url", "invalid_url"),
    ("https://example.com/reel/abc123", "invalid_url"),
    ("https://www.instagram.com/valorant_funzone/", "invalid_url"),
    ("https://www.instagram.com/stories/user/123/", "invalid_url"),
    ("https://youtube.com/shorts/L3wbACRc_v0", "unsupported_platform"),
    ("https://vm.tiktok.com/ZGdxWt2fc/", "unsupported_platform"),
    ("https://www.facebook.com/share/r/19ZwChKjBV/", "unsupported_platform"),
])
def test_invalid(raw, code):
    parsed = parse_instagram_url(raw)
    assert not parsed.ok
    assert parsed.error_code == code
    assert parsed.error_message
