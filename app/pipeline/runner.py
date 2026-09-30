"""Конвейер обработки ролика по этапам.

fetch → download → prepare → detect → classify

Пока один ролик в Gemini (detect/classify), другой может уже качать метрики/видео.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from app.config import settings
from app.errors import PipelineError
from app.pipeline import media, scoring
from app.pipeline.fetcher import fetch_metadata
from app.pipeline.gemini import FrameObservation, GeminiAnalyzer, Usage, default_models

log = logging.getLogger(__name__)

PREP_STEPS = ("fetch", "download", "prepare")
AI_STEPS = ("detect", "classify")
NEXT_STEP = {
    "fetch": "download",
    "download": "prepare",
    "prepare": "detect",
    "detect": "classify",
    "classify": None,
}
STEP_LABELS = {
    "fetch": "Получение метрик из Instagram",
    "download": "Скачивание видео",
    "prepare": "Подготовка кадров и звука",
    "detect": "Поиск логотипа на кадрах",
    "classify": "Анализ речи и классификация",
}


def work_dir_for(video_id: int) -> Path:
    path = Path(settings.work_dir) / str(video_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_work(video_id: int) -> None:
    shutil.rmtree(Path(settings.work_dir) / str(video_id), ignore_errors=True)


def _usage_to_dict(usage: Usage) -> dict:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "calls": usage.calls,
        "cost": usage.cost,
        "models": sorted(usage.models),
    }


def _usage_from_dict(data: dict | None) -> Usage:
    usage = Usage()
    if not data:
        return usage
    usage.input_tokens = int(data.get("input_tokens") or 0)
    usage.output_tokens = int(data.get("output_tokens") or 0)
    usage.calls = int(data.get("calls") or 0)
    usage.cost = float(data.get("cost") or 0)
    usage.models = set(data.get("models") or [])
    return usage


def _load_frames(work_dir: Path, state: dict) -> list[media.Frame]:
    fps = float(state["fps"])
    files = sorted((work_dir / "frames").glob("f_*.jpg"))
    if len(files) != int(state.get("frame_count") or 0):
        # допускаем рассинхрон по числу, берём фактические файлы
        pass
    return [media.Frame(index=i, t=round(i / fps, 2), path=file) for i, file in enumerate(files)]


def _collect(frames: list[media.Frame], observations: dict[int, FrameObservation]) -> tuple[list[scoring.Detection], list[str]]:
    detections: list[scoring.Detection] = []
    screen_texts: list[str] = []
    for frame in frames:
        obs = observations.get(frame.index)
        if obs is None:
            continue
        for text in (obs.skycoach_text, obs.cta_text):
            if text and text not in screen_texts:
                screen_texts.append(text)
        if obs.logo_visible:
            detection = scoring.to_detection(frame.t, obs.box_2d, obs.logo_cut_off, obs.logo_partially_hidden)
            if detection:
                detections.append(detection)
    return detections, screen_texts


def _store_detections(detections: list[scoring.Detection]) -> list[list]:
    return [[d.t, *d.box, d.cut_off, d.hidden] for d in detections]


def run_step(video_id: int, canonical_url: str, step: str, metrics: dict | None, state: dict | None) -> dict:
    """Выполняет один этап. Возвращает patch для Video."""
    state = dict(state or {})
    metrics = dict(metrics or {})
    work_dir = work_dir_for(video_id)
    label = STEP_LABELS.get(step, step)

    if step == "fetch":
        cleanup_work(video_id)
        work_dir = work_dir_for(video_id)
        raw_metrics, apify_cost = fetch_metadata(canonical_url)
        video_url = raw_metrics.pop("video_url", None)
        audio_url = raw_metrics.pop("audio_url", None)
        if raw_metrics.get("type") and raw_metrics["type"] != "Video":
            raise PipelineError("not_a_video", "По ссылке не видео, а фото или карусель — анализировать нечего.")
        duration_hint = raw_metrics.get("duration") or 0
        if duration_hint and duration_hint > settings.max_video_seconds * 4:
            raise PipelineError("too_long", f"Ролик длится {duration_hint / 60:.0f} мин — это слишком долго для анализа.")
        state.update({
            "video_url": video_url,
            "audio_url": audio_url,
            "apify_cost": apify_cost,
            "notes": [],
            "gemini_usage": _usage_to_dict(Usage()),
        })
        return {
            "metrics": raw_metrics,
            "pipeline_state": state,
            "stage": label,
            "next_step": NEXT_STEP[step],
        }

    if step == "download":
        path = media.download_video(state.get("video_url"), canonical_url, work_dir)
        info = media.probe(path)
        notes = list(state.get("notes") or [])
        fps, analyzed = media.frame_plan(info.duration)
        if analyzed < info.duration:
            notes.append(f"Ролик длинный ({info.duration:.0f} с): проанализированы первые {analyzed:.0f} с.")
        if fps < 1:
            notes.append(f"Кадры взяты каждые {1 / fps:.1f} с, поэтому время в кадре посчитано с точностью ±{1 / fps:.1f} с.")
        state.update({
            "video_path": str(path),
            "duration": info.duration,
            "width": info.width,
            "height": info.height,
            "has_audio_track": info.has_audio,
            "fps": fps,
            "analyzed": analyzed,
            "interval": 1 / fps,
            "notes": notes,
        })
        return {"pipeline_state": state, "stage": label, "next_step": NEXT_STEP[step]}

    if step == "prepare":
        path = Path(state["video_path"])
        fps = float(state["fps"])
        analyzed = float(state["analyzed"])
        notes = list(state.get("notes") or [])
        frames = media.extract_frames(path, work_dir / "frames", fps, analyzed)
        if not frames:
            raise PipelineError("broken_video", "Не удалось извлечь ни одного кадра из видео.")

        audio_source = path if state.get("has_audio_track") else None
        if audio_source is None and state.get("audio_url"):
            audio_source = media.download_audio(state["audio_url"], work_dir)
        has_audio = audio_source is not None

        audio = None
        silent = False
        if has_audio:
            audio = media.extract_audio(audio_source, work_dir / "audio.mp3", analyzed)
            silent = audio is not None and media.is_silent(audio)
        if not has_audio:
            audio_note = "Звуковой дорожки в ролике нет — анализ речи не выполнялся."
            notes.append("В ролике нет звуковой дорожки: анализ только по кадрам и описанию.")
        elif silent or audio is None:
            audio_note = "Звуковая дорожка беззвучная — анализ речи не выполнялся."
            notes.append("Звук в ролике беззвучный: анализ только по кадрам и описанию.")
            audio = None
        else:
            audio_note = "Ниже приложена звуковая дорожка ролика."

        state.update({
            "frame_count": len(frames),
            "has_audio": has_audio,
            "silent": silent,
            "audio_note": audio_note,
            "audio_path": str(audio) if audio else None,
            "notes": notes,
        })
        return {
            "pipeline_state": state,
            "stage": f"Кадры готовы ({len(frames)}), ожидание Gemini",
            "next_step": NEXT_STEP[step],
        }

    if step == "detect":
        frames = _load_frames(work_dir, state)
        if not frames:
            raise PipelineError("broken_video", "Не найдены подготовленные кадры для анализа.")
        analyzer = GeminiAnalyzer()
        analyzer.usage = _usage_from_dict(state.get("gemini_usage"))
        notes = list(state.get("notes") or [])
        cascade = {"enabled": settings.gemini_cascade, "escalated": False, "reasons": []}
        first_models = [settings.gemini_cheap_model] + default_models() if settings.gemini_cascade else None
        observations = analyzer.detect_frames(frames, first_models)
        detections, screen_texts = _collect(frames, observations)
        visual = scoring.visual_metrics(
            detections, len(frames), float(state["interval"]), float(state["analyzed"]),
            int(state["height"]) > int(state["width"]),
        )
        if settings.gemini_cascade:
            cascade["first_model"] = settings.gemini_cheap_model
            cascade["reasons"] = scoring.escalation_reasons(visual)
            if cascade["reasons"]:
                observations = analyzer.detect_frames(frames)
                detections, screen_texts = _collect(frames, observations)
                visual = scoring.visual_metrics(
                    detections, len(frames), float(state["interval"]), float(state["analyzed"]),
                    int(state["height"]) > int(state["width"]),
                )
                cascade["escalated"] = True
                notes.append(
                    f"Кадры перепроверены моделью {settings.gemini_model}: " + "; ".join(cascade["reasons"]) + "."
                )
        if len(observations) < len(frames):
            notes.append(f"Модель не вернула ответ для {len(frames) - len(observations)} из {len(frames)} кадров.")
        if not visual["ui_check"]:
            notes.append("Видео не вертикальное — проверка перекрытия интерфейсом Reels не выполнялась.")

        state.update({
            "detections": _store_detections(detections),
            "screen_texts": screen_texts,
            "visual": visual,
            "cascade": cascade,
            "notes": notes,
            "gemini_usage": _usage_to_dict(analyzer.usage),
            "models": sorted(analyzer.usage.models),
        })
        return {"pipeline_state": state, "stage": "Ожидание классификации", "next_step": NEXT_STEP[step]}

    if step == "classify":
        frames = _load_frames(work_dir, state)
        analyzer = GeminiAnalyzer()
        analyzer.usage = _usage_from_dict(state.get("gemini_usage"))
        notes = list(state.get("notes") or [])
        visual = state["visual"]
        screen_texts = list(state.get("screen_texts") or [])
        detections = scoring.from_stored(state.get("detections") or [])
        cascade = state.get("cascade") or {"enabled": False, "escalated": False, "reasons": []}
        audio_path = Path(state["audio_path"]) if state.get("audio_path") else None
        context = json.dumps({
            "author": metrics.get("author"),
            "caption": (metrics.get("caption") or "")[:3000],
            "hashtags": metrics.get("hashtags"),
            "paid_partnership_label": metrics.get("paid_partnership"),
            "duration_seconds": round(float(state["duration"]), 1),
            "on_screen_skycoach_texts": screen_texts[:30],
            "logo_visual_metrics": {
                k: visual[k] for k in ("logo_seconds", "logo_share", "segments", "avg_area_pct", "max_area_pct", "position")
            },
        }, ensure_ascii=False, indent=1)
        final = analyzer.final_analysis(context, audio_path, state.get("audio_note") or "")

        spoken = bool(final.spoken_mentions)
        text_mention = final.caption_mentions_skycoach or bool(screen_texts)
        any_evidence = bool(detections) or spoken or text_mention or final.voice_cta or final.text_cta
        klass = final.integration_class if final.integration_class in (0, 1, 2) else 1
        if klass == 0 and any_evidence:
            klass = 1
            notes.append("Модель поставила класс 0, но найдены упоминания бренда — класс повышен до 1.")
        if not any_evidence:
            klass = 0

        prominence = scoring.prominence(visual, final.voice_cta, final.text_cta, spoken, text_mention) if klass else None
        placement = scoring.placement_review(visual) if klass else None
        models = sorted(set(state.get("models") or []) | analyzer.usage.models)

        analysis = {
            "integration_class": klass,
            "class_label": scoring.CLASS_LABELS[klass],
            "class_reason": final.class_reason,
            "summary": final.summary,
            "prominence": prominence,
            "placement": placement,
            "visual": visual,
            "audio": {
                "has_audio_track": bool(state.get("has_audio")),
                "silent": bool(state.get("silent")),
                "speech_present": final.speech_present if audio_path is not None else False,
                "transcript": final.transcript if audio_path is not None else "",
                "spoken_mentions": [m.model_dump() for m in final.spoken_mentions],
                "voice_cta": final.voice_cta,
                "voice_cta_quote": final.voice_cta_quote,
            },
            "text": {
                "caption_mentions_skycoach": final.caption_mentions_skycoach,
                "text_cta": final.text_cta,
                "promo_codes": final.promo_codes,
                "on_screen_texts": screen_texts[:30],
            },
            "video": {
                "duration": round(float(state["duration"]), 1),
                "width": int(state["width"]),
                "height": int(state["height"]),
            },
            "notes": notes,
            "cascade": cascade,
            "model": ", ".join(models) or settings.gemini_model,
        }
        cost = {
            "apify_usd": float(state.get("apify_cost") or 0),
            "gemini_cost": analyzer.usage.cost,
            "gemini_currency": settings.gemini_price_currency,
            "gemini_input_tokens": analyzer.usage.input_tokens,
            "gemini_output_tokens": analyzer.usage.output_tokens,
            "gemini_calls": analyzer.usage.calls,
        }
        cleanup_work(video_id)
        return {
            "analysis": analysis,
            "cost": cost,
            "pipeline_state": {},
            "stage": None,
            "next_step": None,
            "done": True,
        }

    raise PipelineError("internal_error", f"Неизвестный этап пайплайна: {step}")


def run(video_id: int, canonical_url: str, set_stage, save_metrics) -> tuple[dict, dict]:
    """Совместимость: прогон всех этапов подряд (тесты/ручной вызов)."""
    metrics: dict = {}
    state: dict = {}
    step = "fetch"
    while step:
        set_stage(STEP_LABELS.get(step, step))
        patch = run_step(video_id, canonical_url, step, metrics, state)
        if patch.get("metrics") is not None:
            metrics = patch["metrics"]
            save_metrics(metrics)
        state = patch.get("pipeline_state") or state
        if patch.get("done"):
            return patch["analysis"], patch["cost"]
        step = patch["next_step"]
    raise PipelineError("internal_error", "Пайплайн завершился без результата.")
