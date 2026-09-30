import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, Video, init_db
from app.errors import PipelineError
from app.pipeline import runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
for noisy in ("httpx", "google_genai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("worker")

stop_event = threading.Event()

CLAIM_PREP_SQL = text("""
    UPDATE videos SET status = 'processing', locked_at = now(),
                      attempts = CASE WHEN pipeline_step = 'fetch' THEN attempts + 1 ELSE attempts END,
                      updated_at = now(),
                      stage = CASE
                        WHEN pipeline_step = 'fetch' THEN 'Получение метрик из Instagram'
                        WHEN pipeline_step = 'download' THEN 'Скачивание видео'
                        WHEN pipeline_step = 'prepare' THEN 'Подготовка кадров и звука'
                        ELSE stage
                      END
    WHERE id = (
        SELECT id FROM videos
        WHERE status = 'queued'
          AND next_attempt_at <= now()
          AND pipeline_step IN ('fetch', 'download', 'prepare')
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, canonical_url, pipeline_step, attempts, metrics, pipeline_state
""")

CLAIM_AI_SQL = text("""
    UPDATE videos SET status = 'processing', locked_at = now(), updated_at = now(),
                      stage = CASE
                        WHEN pipeline_step = 'detect' THEN 'Поиск логотипа на кадрах'
                        WHEN pipeline_step = 'classify' THEN 'Анализ речи и классификация'
                        ELSE stage
                      END
    WHERE id = (
        SELECT id FROM videos
        WHERE status = 'queued'
          AND next_attempt_at <= now()
          AND pipeline_step IN ('detect', 'classify')
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, canonical_url, pipeline_step, attempts, metrics, pipeline_state
""")

REQUEUE_STALE_SQL = text("""
    UPDATE videos SET
        status = CASE
            WHEN pipeline_step = 'fetch' AND attempts >= :max_attempts THEN 'failed'
            ELSE 'queued'
        END,
        stage = CASE
            WHEN pipeline_step = 'fetch' AND attempts >= :max_attempts THEN NULL
            ELSE 'Повтор после сбоя воркера'
        END,
        error_code = CASE
            WHEN pipeline_step = 'fetch' AND attempts >= :max_attempts THEN 'worker_crash'
            ELSE error_code
        END,
        error_message = CASE
            WHEN pipeline_step = 'fetch' AND attempts >= :max_attempts
            THEN 'Обработка несколько раз прервалась (воркер перезапускался). Попробуйте отправить ссылку ещё раз.'
            ELSE error_message
        END,
        retryable = true, locked_at = NULL, updated_at = now()
    WHERE status = 'processing' AND locked_at < now() - make_interval(mins => :minutes)
""")


def update_video(video_id: int, **fields) -> None:
    with SessionLocal.begin() as session:
        video = session.get(Video, video_id)
        for key, value in fields.items():
            setattr(video, key, value)


def advance_or_finish(video_id: int, patch: dict) -> None:
    if patch.get("done"):
        update_video(
            video_id,
            status="done",
            stage=None,
            pipeline_step="fetch",
            pipeline_state={},
            analysis=patch["analysis"],
            cost=patch["cost"],
            error_code=None,
            error_message=None,
            retryable=False,
            locked_at=None,
            finished_at=datetime.now(timezone.utc),
        )
        return

    fields = {
        "status": "queued",
        "stage": patch.get("stage"),
        "pipeline_step": patch["next_step"],
        "pipeline_state": patch.get("pipeline_state") or {},
        "locked_at": None,
        "next_attempt_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    if patch.get("metrics") is not None:
        fields["metrics"] = patch["metrics"]
    update_video(video_id, **fields)


def fail(video_id: int, step: str, exc: PipelineError, attempts: int) -> None:
    if exc.retryable and attempts < settings.max_attempts:
        delay = 20 * 2 ** max(attempts - 1, 0)
        log.warning("Video %s step %s: %s — retry in %ss", video_id, step, exc.message, delay)
        update_video(
            video_id,
            status="queued",
            stage=f"Повтор через {delay} с: {exc.message[:200]}",
            locked_at=None,
            next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
        )
        return

    log.warning("Video %s failed at %s: %s %s", video_id, step, exc.code, exc.message)
    runner.cleanup_work(video_id)
    update_video(
        video_id,
        status="failed",
        stage=None,
        pipeline_step="fetch",
        pipeline_state={},
        error_code=exc.code,
        error_message=exc.message,
        retryable=exc.retryable,
        locked_at=None,
        finished_at=datetime.now(timezone.utc),
    )


def process_claimed(row) -> None:
    video_id = row.id
    step = row.pipeline_step
    attempts = row.attempts
    log.info("Step %s for video %s (%s), attempt %s", step, video_id, row.canonical_url, attempts)
    try:
        patch = runner.run_step(
            video_id,
            row.canonical_url,
            step,
            row.metrics,
            row.pipeline_state,
        )
        # heartbeat while step ran
        update_video(video_id, locked_at=datetime.now(timezone.utc))
        advance_or_finish(video_id, patch)
        if patch.get("done"):
            log.info("Video %s done: class=%s", video_id, patch["analysis"]["integration_class"])
        else:
            log.info("Video %s advanced to %s", video_id, patch["next_step"])
    except PipelineError as exc:
        fail(video_id, step, exc, attempts)
    except Exception as exc:
        log.exception("Unexpected error for video %s step %s", video_id, step)
        fail(video_id, step, PipelineError("internal_error", f"Внутренняя ошибка обработки: {exc}"[:400], retryable=True), attempts)


def claim(kind: str):
    sql = CLAIM_PREP_SQL if kind == "prep" else CLAIM_AI_SQL
    with SessionLocal.begin() as session:
        return session.execute(sql).first()


def worker_loop(kind: str) -> None:
    while not stop_event.is_set():
        try:
            row = claim(kind)
        except Exception:
            log.exception("Queue claim failed (%s)", kind)
            stop_event.wait(5)
            continue
        if row is None:
            stop_event.wait(0.5)
            continue
        try:
            process_claimed(row)
        except Exception:
            log.exception("Failed to record result for video %s; janitor will requeue it", row.id)


def janitor_loop() -> None:
    while not stop_event.is_set():
        try:
            with SessionLocal.begin() as session:
                count = session.execute(
                    REQUEUE_STALE_SQL,
                    {"minutes": settings.stale_job_minutes, "max_attempts": settings.max_attempts},
                ).rowcount
            if count:
                log.warning("Requeued %s stale jobs", count)
        except Exception:
            log.exception("Janitor failed")
        stop_event.wait(60)


def main() -> None:
    for _attempt in range(30):
        try:
            init_db()
            break
        except Exception as exc:
            log.info("Waiting for database: %s", exc)
            time.sleep(2)
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    prep_n = settings.prep_concurrency or settings.max_urls_per_request
    ai_n = settings.ai_concurrency or settings.max_urls_per_request
    prep_n = max(1, prep_n)
    ai_n = max(1, ai_n)
    threads = [threading.Thread(target=janitor_loop, name="janitor", daemon=True)]
    threads += [threading.Thread(target=worker_loop, args=("prep",), name=f"prep-{i}", daemon=True) for i in range(prep_n)]
    threads += [threading.Thread(target=worker_loop, args=("ai",), name=f"ai-{i}", daemon=True) for i in range(ai_n)]
    for thread in threads:
        thread.start()
    log.info("Pipeline worker started: prep=%s ai=%s", prep_n, ai_n)
    while not stop_event.is_set():
        stop_event.wait(1)
    log.info("Worker stopping")


if __name__ == "__main__":
    main()
