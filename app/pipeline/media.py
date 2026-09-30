import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
import yt_dlp

from app.config import settings
from app.errors import PipelineError
from app.pipeline.fetcher import _ytdlp_error

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass
class Frame:
    index: int
    t: float
    path: Path


def download_video(video_url: str | None, canonical_url: str, work_dir: Path) -> Path:
    if video_url:
        try:
            return _download_direct(video_url, work_dir / "video.mp4")
        except PipelineError as exc:
            if exc.code == "too_large":
                raise
            log.warning("Direct download failed (%s), trying yt-dlp", exc.message)
    return _download_ytdlp(canonical_url, work_dir)


def download_audio(audio_url: str, work_dir: Path) -> Path | None:
    """Instagram отдаёт звук отдельным потоком; без него ролик выглядел бы беззвучным."""
    try:
        path = _download_direct(audio_url, work_dir / "audio_src.mp4")
    except PipelineError as exc:
        log.warning("Audio download failed: %s", exc.message)
        return None
    result = _run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], timeout=60)
    return path if result.returncode == 0 and result.stdout.strip() else None


def _download_direct(url: str, dest: Path) -> Path:
    limit = settings.max_download_mb * 1024 * 1024
    try:
        with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as response:
            if response.status_code >= 400:
                raise PipelineError("download_failed", f"CDN Instagram вернул HTTP {response.status_code}.", retryable=True)
            size = 0
            with dest.open("wb") as file:
                for chunk in response.iter_bytes(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise PipelineError("too_large", f"Видео больше {settings.max_download_mb} МБ, анализ не выполнялся.")
                    file.write(chunk)
    except httpx.HTTPError as exc:
        raise PipelineError("download_failed", f"Не удалось скачать видео: {exc}", retryable=True)
    return dest


def _download_ytdlp(url: str, work_dir: Path) -> Path:
    options = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(work_dir / "ytdlp.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "max_filesize": settings.max_download_mb * 1024 * 1024,
        # На части VPS Instagram/CDN доступны только по IPv6.
        "source_address": "::",
    }
    if settings.instagram_cookies_file:
        options["cookiefile"] = settings.instagram_cookies_file
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise _ytdlp_error(str(exc))
    files = sorted(work_dir.glob("ytdlp.*"))
    if not files:
        raise PipelineError("download_failed", "Видео не скачалось.", retryable=True)
    return files[0]


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise PipelineError("processing_error", "Обработка видео заняла слишком много времени.", retryable=True)


def probe(path: Path) -> VideoInfo:
    result = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], timeout=60)
    if result.returncode != 0:
        raise PipelineError("broken_video", "Скачанный файл не является корректным видео.")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise PipelineError("broken_video", "В файле нет видеодорожки.")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width
    return VideoInfo(
        duration=duration,
        width=width,
        height=height,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return abs(int(side["rotation"])) % 360
    tag = (stream.get("tags") or {}).get("rotate")
    return abs(int(tag)) % 360 if tag else 0


def frame_plan(duration: float) -> tuple[float, float]:
    """Возвращает (fps выборки, длительность анализируемого участка)."""
    analyzed = min(duration, float(settings.max_video_seconds))
    fps = min(settings.max_fps, settings.max_frames / max(analyzed, 1.0))
    return fps, analyzed


def extract_frames(path: Path, out_dir: Path, fps: float, analyzed: float) -> list[Frame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    h = settings.frame_height
    scale = f"scale='if(gt(iw,ih),min({h},iw),-2)':'if(gt(iw,ih),-2,min({h},ih))'"
    result = _run([
        "ffmpeg", "-v", "error", "-y", "-t", f"{analyzed:.2f}", "-i", str(path),
        "-vf", f"fps={fps:.4f},{scale}", "-q:v", "4", str(out_dir / "f_%05d.jpg"),
    ])
    if result.returncode != 0:
        raise PipelineError("processing_error", f"ffmpeg не смог извлечь кадры: {result.stderr[-300:]}")
    files = sorted(out_dir.glob("f_*.jpg"))
    return [Frame(index=i, t=round(i / fps, 2), path=file) for i, file in enumerate(files)]


def extract_audio(path: Path, out: Path, analyzed: float) -> Path | None:
    result = _run([
        "ffmpeg", "-v", "error", "-y", "-t", f"{analyzed:.2f}", "-i", str(path),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(out),
    ])
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return None
    return out


def is_silent(audio: Path) -> bool:
    result = _run(["ffmpeg", "-v", "info", "-i", str(audio), "-af", "volumedetect", "-f", "null", "-"], timeout=120)
    match = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", result.stderr)
    if not match:
        return False
    value = match.group(1)
    return value == "-inf" or float(value) < -50
