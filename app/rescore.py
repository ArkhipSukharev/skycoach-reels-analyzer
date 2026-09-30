"""Пересчёт баллов и вердиктов по сохранённым рамкам логотипа — без повторных запросов к Gemini.

Запуск: docker compose exec worker python -m app.rescore
"""
from sqlalchemy import select

from app.db import SessionLocal, Video
from app.pipeline import scoring


def rescore(analysis: dict) -> dict | None:
    visual = analysis.get("visual") or {}
    if "detections" not in visual or not analysis.get("integration_class"):
        return None
    new_visual = scoring.visual_metrics(
        scoring.from_stored(visual["detections"]),
        visual["frames_analyzed"],
        visual["frame_interval_s"],
        visual["analyzed_seconds"],
        visual["ui_check"],
    )
    audio, text = analysis["audio"], analysis["text"]
    text_mention = text["caption_mentions_skycoach"] or bool(text["on_screen_texts"])
    return analysis | {
        "visual": new_visual,
        "prominence": scoring.prominence(new_visual, audio["voice_cta"], text["text_cta"], bool(audio["spoken_mentions"]), text_mention),
        "placement": scoring.placement_review(new_visual),
    }


def main() -> None:
    with SessionLocal.begin() as session:
        for video in session.execute(select(Video).where(Video.status == "done")).scalars():
            updated = rescore(video.analysis or {})
            if updated is None:
                print(f"{video.shortcode}: нет сохранённых рамок, пропущен")
                continue
            video.analysis = updated
            print(f"{video.shortcode}: {updated['prominence']['score']}/5, {updated['placement']['verdict']}")


if __name__ == "__main__":
    main()
