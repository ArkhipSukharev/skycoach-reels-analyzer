import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

INSTAGRAM_HOSTS = {"instagram.com", "m.instagram.com", "instagr.am"}
OTHER_PLATFORMS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "vk.com": "VK",
}
POST_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._]+/)?(?i:reels?|p|tv)/([A-Za-z0-9_-]{5,64})(?:/|$)")
SHARE_PATH_RE = re.compile(r"^/share/(?i:reels?|p)/[A-Za-z0-9_-]+/?$")


@dataclass
class ParsedUrl:
    ok: bool
    shortcode: str | None = None
    canonical_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def _invalid(message: str, code: str = "invalid_url") -> ParsedUrl:
    return ParsedUrl(ok=False, error_code=code, error_message=message)


def _host(netloc: str) -> str:
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def parse_instagram_url(raw: str, resolve_share: bool = True) -> ParsedUrl:
    url = (raw or "").strip()
    if not url:
        return _invalid("Пустая ссылка.")
    if " " in url:
        return _invalid("В ссылке есть пробелы — это не похоже на URL.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except ValueError:
        return _invalid("Не удалось разобрать ссылку.")

    host = _host(parsed.netloc)
    if not host or "." not in host:
        return _invalid("Не удалось разобрать ссылку.")

    for domain, platform in OTHER_PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return _invalid(
                f"Ссылка на {platform}. Сервис поддерживает только Instagram Reels.",
                code="unsupported_platform",
            )

    if host not in INSTAGRAM_HOSTS:
        return _invalid("Это не ссылка на Instagram.")

    if SHARE_PATH_RE.match(parsed.path) and resolve_share:
        resolved = _resolve_share_link(url)
        if resolved is None:
            return _invalid("Не удалось раскрыть короткую ссылку Instagram (/share/...). Откройте её в браузере и пришлите полную ссылку.")
        return parse_instagram_url(resolved, resolve_share=False)

    match = POST_PATH_RE.match(parsed.path)
    if not match:
        return _invalid("Ссылка ведёт на Instagram, но не на ролик. Нужен формат instagram.com/reel/<код>/.")

    shortcode = match.group(1)
    return ParsedUrl(
        ok=True,
        shortcode=shortcode,
        canonical_url=f"https://www.instagram.com/reel/{shortcode}/",
    )


def _resolve_share_link(url: str) -> str | None:
    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"},
        )
    except httpx.HTTPError:
        return None
    final = str(response.url)
    return final if POST_PATH_RE.match(urlparse(final).path) else None
