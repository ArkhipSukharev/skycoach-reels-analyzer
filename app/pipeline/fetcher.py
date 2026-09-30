import logging
from datetime import datetime, timezone

import httpx
import yt_dlp

from app.config import settings
from app.errors import PipelineError, not_found, private

log = logging.getLogger(__name__)


def fetch_metadata(canonical_url: str) -> tuple[dict, float]:
    """Возвращает (нормализованные метрики, стоимость в USD)."""
    if settings.apify_token:
        try:
            return _fetch_apify(canonical_url), settings.apify_price_per_result
        except PipelineError as exc:
            if exc.code in ("not_found", "private", "not_a_video"):
                raise
            log.warning("Apify failed (%s), falling back to yt-dlp", exc.message)
            try:
                return _fetch_ytdlp(canonical_url), 0.0
            except PipelineError:
                raise exc
    return _fetch_ytdlp(canonical_url), 0.0


def _fetch_apify(url: str) -> dict:
    endpoint = f"https://api.apify.com/v2/acts/{settings.apify_actor}/run-sync-get-dataset-items"
    payload = {"directUrls": [url], "resultsType": "posts", "resultsLimit": 1, "addParentData": False}
    try:
        # Короткий connect: на части VPS IPv4 до Apify (AWS) закрыт, ждать 3+ мин бессмысленно.
        response = httpx.post(
            endpoint,
            params={"timeout": 60},
            json=payload,
            headers={"Authorization": f"Bearer {settings.apify_token}"},
            timeout=httpx.Timeout(75.0, connect=12.0),
        )
    except httpx.HTTPError as exc:
        raise PipelineError("provider_error", f"Сервис сбора данных (Apify) не ответил: {exc}", retryable=True)

    if response.status_code in (401, 403):
        raise PipelineError("config_error", "Apify отклонил токен. Проверьте APIFY_TOKEN в .env.")
    if response.status_code == 402:
        raise PipelineError("config_error", "На аккаунте Apify закончились кредиты.")
    if response.status_code >= 400:
        raise PipelineError(
            "provider_error",
            f"Apify вернул ошибку HTTP {response.status_code}.",
            retryable=response.status_code in (408, 429) or response.status_code >= 500,
        )

    items = response.json()
    if not items:
        raise PipelineError(
            "unavailable",
            "Instagram не отдал данные о ролике. Обычно это значит, что ролик приватный или удалён.",
            retryable=True,
        )

    item = items[0]
    error = (item.get("error") or "").lower()
    if error:
        description = item.get("errorDescription") or error
        if "not_found" in error or "not exist" in description.lower():
            raise not_found()
        if "private" in error or "restricted" in error or "private" in description.lower():
            raise private()
        raise PipelineError("provider_error", f"Instagram вернул ошибку: {description}", retryable=True)

    return _normalize_apify(item)


def _count(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _normalize_apify(item: dict) -> dict:
    views = _count(item.get("videoPlayCount"))
    if views is None:
        views = _count(item.get("videoViewCount"))
    return {
        "source": "apify",
        "type": item.get("type"),
        "product_type": item.get("productType"),
        "author": item.get("ownerUsername"),
        "author_full_name": item.get("ownerFullName"),
        "published_at": item.get("timestamp"),
        "views": views,
        "likes": _count(item.get("likesCount")),
        "comments": None if item.get("isCommentsDisabled") else _count(item.get("commentsCount")),
        "comments_disabled": bool(item.get("isCommentsDisabled")),
        "duration": item.get("videoDuration"),
        "caption": item.get("caption") or "",
        "hashtags": item.get("hashtags") or [],
        "mentions": item.get("mentions") or [],
        "paid_partnership": item.get("paidPartnership"),
        "video_url": item.get("videoUrl"),
        "audio_url": item.get("audioUrl"),
        "width": item.get("originalWidth") or item.get("dimensionsWidth"),
        "height": item.get("originalHeight") or item.get("dimensionsHeight"),
    }


def _fetch_ytdlp(url: str) -> dict:
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "source_address": "::"}
    if settings.instagram_cookies_file:
        options["cookiefile"] = settings.instagram_cookies_file
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise _ytdlp_error(str(exc))

    published = None
    if info.get("timestamp"):
        published = datetime.fromtimestamp(info["timestamp"], tz=timezone.utc).isoformat()
    return {
        "source": "yt-dlp",
        "type": "Video",
        "product_type": None,
        "author": info.get("channel") or info.get("uploader_id") or info.get("uploader"),
        "author_full_name": info.get("uploader"),
        "published_at": published,
        "views": _count(info.get("view_count")),
        "likes": _count(info.get("like_count")),
        "comments": _count(info.get("comment_count")),
        "comments_disabled": False,
        "duration": info.get("duration"),
        "caption": info.get("description") or "",
        "hashtags": [],
        "mentions": [],
        "paid_partnership": None,
        "video_url": None,
        "audio_url": None,
        "width": info.get("width"),
        "height": info.get("height"),
    }


def _ytdlp_error(message: str) -> PipelineError:
    lowered = message.lower()
    if "private" in lowered:
        return private()
    if "not available" in lowered or "404" in lowered or "does not exist" in lowered:
        return not_found()
    if "login" in lowered or "rate" in lowered or "cookies" in lowered:
        return PipelineError(
            "blocked",
            "Instagram потребовал авторизацию и не отдал ролик. Нужен APIFY_TOKEN или cookies для yt-dlp.",
            retryable=True,
        )
    return PipelineError("provider_error", f"yt-dlp не смог получить ролик: {message[:300]}", retryable=True)
