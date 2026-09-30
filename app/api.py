import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import and_, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.types import DateTime

from app.config import settings
from app.db import Job, SessionLocal, Video, init_db
from app.urls import parse_instagram_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("api")

STATIC = Path(__file__).resolve().parent / "static"
public_url: str | None = None


def watch_tunnel() -> None:
    global public_url
    while True:
        try:
            hostname = httpx.get(settings.tunnel_metrics_url, timeout=3).json().get("hostname")
            url = f"https://{hostname}" if hostname else None
            if url and url != public_url:
                public_url = url
                log.info("=" * 60)
                log.info("PUBLIC URL: %s", public_url)
                log.info("=" * 60)
        except Exception:
            pass
        time.sleep(5 if public_url is None else 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    for _attempt in range(30):
        try:
            init_db()
            break
        except Exception as exc:
            log.info("Waiting for database: %s", exc)
            time.sleep(2)
    threading.Thread(target=watch_tunnel, daemon=True).start()
    yield


app = FastAPI(title="Skycoach Reels Analyzer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class SubmitRequest(BaseModel):
    urls: list[str]


def serialize(job: Job) -> dict:
    video = job.video
    base = {
        "id": str(job.id),
        "input_url": job.input_url,
        "cached": job.cached,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
    if video is None:
        return base | {
            "status": "invalid",
            "stage": None,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "shortcode": None,
            "url": None,
            "metrics": None,
            "analysis": None,
            "cost": None,
        }
    return base | {
        "status": video.status,
        "stage": video.stage,
        "error_code": video.error_code,
        "error_message": video.error_message,
        "shortcode": video.shortcode,
        "url": video.canonical_url,
        "attempts": video.attempts,
        "metrics": video.metrics,
        "analysis": video.analysis,
        "cost": video.cost,
        "finished_at": video.finished_at.isoformat() if video.finished_at else None,
    }


def attach_video(session, raw: str) -> Job:
    parsed = parse_instagram_url(raw)
    if not parsed.ok:
        return Job(input_url=raw, error_code=parsed.error_code, error_message=parsed.error_message)

    inserted = session.execute(
        pg_insert(Video)
        .values(
            shortcode=parsed.shortcode,
            canonical_url=parsed.canonical_url,
            status="queued",
            stage="В очереди",
            pipeline_step="fetch",
            pipeline_state={},
        )
        .on_conflict_do_nothing(index_elements=["shortcode"])
        .returning(Video.id)
    ).scalar()
    if inserted:
        return Job(input_url=raw, video_id=inserted, cached=False)

    video = session.execute(select(Video).where(Video.shortcode == parsed.shortcode).with_for_update()).scalar_one()
    if video.status == "failed" and video.retryable:
        video.status = "queued"
        video.stage = "В очереди (повторная попытка)"
        video.pipeline_step = "fetch"
        video.pipeline_state = {}
        video.attempts = 0
        video.error_code = video.error_message = None
        video.next_attempt_at = datetime.now(timezone.utc)
        return Job(input_url=raw, video_id=video.id, cached=False)
    return Job(input_url=raw, video_id=video.id, cached=True)


def load_jobs(ids: list[uuid.UUID]) -> list[dict]:
    with SessionLocal() as session:
        jobs = {job.id: job for job in session.execute(select(Job).where(Job.id.in_(ids))).scalars()}
    return [serialize(jobs[i]) for i in ids if i in jobs]


@app.post("/api/jobs")
def create_jobs(body: SubmitRequest) -> list[dict]:
    urls = [u.strip() for u in body.urls if u and u.strip()]
    if not urls:
        raise HTTPException(400, "Не передано ни одной ссылки.")
    if len(urls) > settings.max_urls_per_request:
        raise HTTPException(400, f"За раз можно отправить не больше {settings.max_urls_per_request} ссылок, получено {len(urls)}.")

    ids = []
    with SessionLocal.begin() as session:
        for raw in urls:
            job = attach_video(session, raw[:2000])
            session.add(job)
            session.flush()
            ids.append(job.id)
    return load_jobs(ids)


@app.get("/api/jobs")
def list_jobs(ids: str | None = Query(None), limit: int = Query(100, le=500)) -> list[dict]:
    if ids:
        try:
            parsed = [uuid.UUID(i) for i in ids.split(",") if i]
        except ValueError:
            raise HTTPException(400, "Некорректный ID задачи.")
        return load_jobs(parsed)
    with SessionLocal() as session:
        jobs = session.execute(select(Job).order_by(Job.created_at.desc()).limit(limit)).scalars().all()
        return [serialize(job) for job in jobs]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(400, "Некорректный ID задачи.")
    result = load_jobs([parsed])
    if not result:
        raise HTTPException(404, "Задача не найдена.")
    return result[0]


def serialize_video(video: Video) -> dict:
    return {
        "id": f"video-{video.id}",
        "video_id": video.id,
        "shortcode": video.shortcode,
        "url": video.canonical_url,
        "input_url": video.canonical_url,
        "status": video.status,
        "stage": video.stage,
        "error_code": video.error_code,
        "error_message": video.error_message,
        "metrics": video.metrics,
        "analysis": video.analysis,
        "cost": video.cost,
        "cached": False,
        "created_at": video.created_at.isoformat() if video.created_at else None,
        "finished_at": video.finished_at.isoformat() if video.finished_at else None,
        "updated_at": video.updated_at.isoformat() if video.updated_at else None,
    }


@app.get("/api/videos")
def list_videos(
    author: str | None = Query(None, description="Автор: подстрока без @"),
    date_from: date | None = Query(None, description="Дата публикации с (включительно)"),
    date_to: date | None = Query(None, description="Дата публикации по (включительно)"),
    checked_from: date | None = Query(None, description="Дата проверки с"),
    checked_to: date | None = Query(None, description="Дата проверки по"),
    views_min: int | None = Query(None, ge=0),
    views_max: int | None = Query(None, ge=0),
    likes_min: int | None = Query(None, ge=0),
    likes_max: int | None = Query(None, ge=0),
    status: str | None = Query(None, description="queued|processing|done|failed"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """Общая база всех проверенных роликов с фильтрами."""
    if views_min is not None and views_max is not None and views_min > views_max:
        raise HTTPException(400, "views_min не может быть больше views_max.")
    if likes_min is not None and likes_max is not None and likes_min > likes_max:
        raise HTTPException(400, "likes_min не может быть больше likes_max.")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "date_from не может быть позже date_to.")
    if checked_from and checked_to and checked_from > checked_to:
        raise HTTPException(400, "checked_from не может быть позже checked_to.")

    views = Video.metrics["views"].as_integer()
    likes = Video.metrics["likes"].as_integer()
    published = cast(Video.metrics["published_at"].as_string(), DateTime(timezone=True))
    author_col = func.lower(Video.metrics["author"].as_string())

    filters = []
    # По умолчанию база — только успешно разобранные ролики.
    # failed/queued/processing видны при явном выборе статуса в фильтре.
    if status:
        filters.append(Video.status == status)
    else:
        filters.append(Video.status == "done")
    if author:
        filters.append(author_col.ilike(f"%{author.lstrip('@').strip().lower()}%"))
    if date_from:
        filters.append(published >= datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc))
    if date_to:
        filters.append(published < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    if checked_from:
        filters.append(func.coalesce(Video.finished_at, Video.created_at) >= datetime.combine(checked_from, datetime.min.time(), tzinfo=timezone.utc))
    if checked_to:
        filters.append(func.coalesce(Video.finished_at, Video.created_at) < datetime.combine(checked_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    if views_min is not None:
        filters.append(and_(Video.metrics["views"].as_string().isnot(None), views >= views_min))
    if views_max is not None:
        filters.append(and_(Video.metrics["views"].as_string().isnot(None), views <= views_max))
    if likes_min is not None:
        filters.append(and_(Video.metrics["likes"].as_string().isnot(None), likes >= likes_min))
    if likes_max is not None:
        filters.append(and_(Video.metrics["likes"].as_string().isnot(None), likes <= likes_max))

    where = and_(*filters) if filters else True
    with SessionLocal() as session:
        total = session.execute(select(func.count()).select_from(Video).where(where)).scalar_one()
        videos = session.execute(
            select(Video).where(where).order_by(func.coalesce(Video.finished_at, Video.created_at).desc()).offset(offset).limit(limit)
        ).scalars().all()
        return {"total": total, "offset": offset, "limit": limit, "items": [serialize_video(v) for v in videos]}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "public_url": public_url, "cascade": settings.gemini_cascade}


@app.get("/api/public-url")
def get_public_url() -> dict:
    return {"public_url": public_url}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
