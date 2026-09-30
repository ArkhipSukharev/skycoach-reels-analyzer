import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Video(Base):
    """Один ролик Instagram. Уникален по shortcode — на этом держится дедупликация."""

    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shortcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    canonical_url: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str | None] = mapped_column(Text)
    # fetch → download → prepare → detect → classify
    pipeline_step: Mapped[str] = mapped_column(String(20), default="fetch", index=True)
    pipeline_state: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    metrics: Mapped[dict | None] = mapped_column(JSONB)
    analysis: Mapped[dict | None] = mapped_column(JSONB)
    cost: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    """Одна поданная ссылка. Несколько задач могут ссылаться на один ролик."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    input_url: Mapped[str] = mapped_column(Text)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id"), index=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    video: Mapped[Video | None] = relationship(lazy="joined")


def init_db() -> None:
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(424242)"))
        Base.metadata.create_all(conn)
        conn.execute(text("ALTER TABLE videos ADD COLUMN IF NOT EXISTS pipeline_step VARCHAR(20) DEFAULT 'fetch'"))
        conn.execute(text("ALTER TABLE videos ADD COLUMN IF NOT EXISTS pipeline_state JSONB"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_videos_pipeline_step ON videos (pipeline_step)"))
        conn.execute(
            text(
                """
                UPDATE videos
                SET pipeline_step = 'fetch', pipeline_state = COALESCE(pipeline_state, '{}'::jsonb)
                WHERE status IN ('queued', 'processing')
                  AND (pipeline_step IS NULL OR pipeline_step = '')
                """
            )
        )
