"""Детерминированный расчёт метрик заметности, баллов и проблем размещения.

Модель только находит логотип на кадрах; все секунды, площади и баллы считаются здесь,
чтобы оценку можно было проверить и объяснить.
"""
from dataclasses import dataclass

# Зоны интерфейса Instagram Reels на вертикальном видео (доли кадра, y — сверху вниз).
# Пороги откалиброваны по примерам выплат: баннеры ~7% кадра на высоте до 92% оплачены полностью,
# 2.5% — «слишком мелкий», 5.7% — «мелкий» (вычет 20%).
UI_ZONES = [
    (0.00, 0.00, 0.07, 1.00),  # верхняя панель
    (0.93, 0.00, 1.00, 1.00),  # нижняя кромка: автор и музыка
    (0.55, 0.88, 0.93, 1.00),  # колонка кнопок справа
]
EDGE_MARGIN = 0.005
TOO_SMALL_AREA = 0.035
SLIGHTLY_SMALL_AREA = 0.06
UI_OVERLAP_FRAME = 0.15
MAJORITY = 0.5

CLASS_LABELS = {
    0: "0 — Skycoach не упоминается",
    1: "1 — упоминание без рекламы",
    2: "2 — реклама продукта Skycoach",
}


@dataclass
class Detection:
    t: float
    box: tuple[float, float, float, float]  # ymin, xmin, ymax, xmax в долях кадра
    cut_off: bool
    hidden: bool


def to_detection(t: float, box_2d: list[int], cut_off: bool, hidden: bool) -> Detection | None:
    if len(box_2d) != 4:
        return None
    ymin, xmin, ymax, xmax = (max(0.0, min(1000.0, float(v))) / 1000 for v in box_2d)
    if ymax <= ymin or xmax <= xmin:
        return None
    return Detection(t=t, box=(ymin, xmin, ymax, xmax), cut_off=cut_off, hidden=hidden)


def from_stored(rows: list[list]) -> list[Detection]:
    return [Detection(t=r[0], box=tuple(r[1:5]), cut_off=bool(r[5]), hidden=bool(r[6])) for r in rows]


def _area(box) -> float:
    ymin, xmin, ymax, xmax = box
    return (ymax - ymin) * (xmax - xmin)


def ui_overlap(box, grid: int = 20) -> float:
    """Доля площади рамки, попадающая в зоны интерфейса Reels."""
    ymin, xmin, ymax, xmax = box
    inside = 0
    for i in range(grid):
        y = ymin + (ymax - ymin) * (i + 0.5) / grid
        for j in range(grid):
            x = xmin + (xmax - xmin) * (j + 0.5) / grid
            if any(z[0] <= y <= z[2] and z[1] <= x <= z[3] for z in UI_ZONES):
                inside += 1
    return inside / (grid * grid)


def touches_edge(box) -> bool:
    ymin, xmin, ymax, xmax = box
    return xmin <= EDGE_MARGIN or ymin <= EDGE_MARGIN or xmax >= 1 - EDGE_MARGIN or ymax >= 1 - EDGE_MARGIN


def segments(times: list[float], interval: float) -> list[tuple[float, float]]:
    result: list[list[float]] = []
    for t in sorted(times):
        if result and t - result[-1][1] <= interval * 1.5:
            result[-1][1] = t
        else:
            result.append([t, t])
    return [(round(s, 1), round(e + interval, 1)) for s, e in result]


def _position(boxes) -> str:
    cy = sum((b[0] + b[2]) / 2 for b in boxes) / len(boxes)
    cx = sum((b[1] + b[3]) / 2 for b in boxes) / len(boxes)
    vertical = "верх" if cy < 0.33 else "центр" if cy < 0.66 else "низ"
    horizontal = "слева" if cx < 0.33 else "по центру" if cx < 0.66 else "справа"
    return f"{vertical}, {horizontal}"


def visual_metrics(detections: list[Detection], frames_total: int, interval: float,
                   analyzed_seconds: float, vertical: bool) -> dict:
    seconds = min(len(detections) * interval, analyzed_seconds)
    result = {
        "frames_analyzed": frames_total,
        "frames_with_logo": len(detections),
        "frame_interval_s": round(interval, 2),
        "analyzed_seconds": round(analyzed_seconds, 1),
        "logo_seconds": round(seconds, 1),
        "logo_share": round(seconds / analyzed_seconds, 3) if analyzed_seconds else 0.0,
        "segments": [],
        "avg_area_pct": None,
        "max_area_pct": None,
        "position": None,
        "cut_off_share": None,
        "ui_overlap_share": None,
        "ui_check": vertical,
        "detections": [
            [d.t, *(round(v, 3) for v in d.box), int(d.cut_off), int(d.hidden)] for d in detections
        ],
    }
    if not detections:
        return result
    boxes = [d.box for d in detections]
    areas = [_area(b) for b in boxes]
    cut = [d.cut_off or touches_edge(d.box) for d in detections]
    result.update(
        segments=segments([d.t for d in detections], interval),
        avg_area_pct=round(100 * sum(areas) / len(areas), 2),
        max_area_pct=round(100 * max(areas), 2),
        position=_position(boxes),
        cut_off_share=round(sum(cut) / len(cut), 2),
    )
    if vertical:
        overlaps = [ui_overlap(b) >= UI_OVERLAP_FRAME or d.hidden for b, d in zip(boxes, detections)]
        result["ui_overlap_share"] = round(sum(overlaps) / len(overlaps), 2)
    return result


def prominence(visual: dict, voice_cta: bool, text_cta: bool, spoken: bool, text_mention: bool) -> dict:
    """Шкала 1-5: 1 базовый балл + до 2 за время в кадре + 1 за размер + 1 за призыв."""
    seconds, share = visual["logo_seconds"], visual["logo_share"]
    avg_area = (visual["avg_area_pct"] or 0) / 100
    max_area = (visual["max_area_pct"] or 0) / 100

    if seconds == 0:
        time_pts, time_why = 0, "логотип в кадре не обнаружен"
    elif share >= 0.5 or seconds >= 15:
        time_pts, time_why = 2, f"логотип в кадре {seconds:.1f} с ({share:.0%} ролика) — долго"
    elif share >= 0.2 or seconds >= 5:
        time_pts, time_why = 1, f"логотип в кадре {seconds:.1f} с ({share:.0%} ролика) — заметно"
    else:
        time_pts, time_why = 0, f"логотип в кадре всего {seconds:.1f} с ({share:.0%} ролика) — мелькнул"

    if seconds == 0:
        size_pts, size_why = 0, "размер не оценивается"
    elif avg_area >= SLIGHTLY_SMALL_AREA or max_area >= 0.25:
        size_pts, size_why = 1, f"крупный: в среднем {avg_area:.1%} кадра, максимум {max_area:.1%}"
    else:
        size_pts, size_why = 0, f"небольшой: в среднем {avg_area:.1%} кадра, максимум {max_area:.1%}"

    cta = voice_cta or text_cta
    cta_parts = []
    if voice_cta:
        cta_parts.append("голосовой призыв")
    if text_cta:
        cta_parts.append("текстовый призыв/промокод")
    cta_why = ("есть " + " и ".join(cta_parts)) if cta else "призыва к действию нет"

    score = min(5, 1 + time_pts + size_pts + (1 if cta else 0))
    justification = [
        f"Время в кадре: {time_why} (+{time_pts}).",
        f"Размер: {size_why} (+{size_pts}).",
        f"Призыв: {cta_why} (+{1 if cta else 0}).",
    ]
    if visual["segments"]:
        spans = ", ".join(f"{s:.1f}–{e:.1f} с" for s, e in visual["segments"][:8])
        justification.append(f"Появления логотипа: {spans}; расположение: {visual['position']}.")
    if seconds == 0 and (spoken or text_mention):
        justification.append("Бренд упоминается только голосом или текстом, визуально не показан.")
    justification.append(f"Итог: 1 базовый + {time_pts} + {size_pts} + {1 if cta else 0} = {score}/5.")
    return {
        "score": score,
        "breakdown": {"base": 1, "time": time_pts, "size": size_pts, "cta": 1 if cta else 0},
        "justification": justification,
    }


def escalation_reasons(visual: dict) -> list[str]:
    """Когда результат дешёвой модели пограничный и кадры стоит перепроверить сильной моделью."""
    reasons = []
    frames = visual["frames_with_logo"]
    area = (visual["avg_area_pct"] or 0) / 100
    if frames == 0:
        reasons.append("логотип не найден — перед исключением из выплаты нужна проверка")
    elif frames <= 3:
        reasons.append(f"логотип найден всего в {frames} кадрах")
    if frames and area < SLIGHTLY_SMALL_AREA + 0.01:
        reasons.append(f"баннер {area:.1%} кадра — рядом с порогами размера, мелкий текст читается хуже")
    for key, label in (("cut_off_share", "обрезан краем"), ("ui_overlap_share", "под интерфейсом")):
        share = visual.get(key)
        if share is not None and 0.3 <= share <= 0.7:
            reasons.append(f"баннер {label} в {share:.0%} появлений — рядом с порогом 50%")
    return reasons


def placement_review(visual: dict) -> dict:
    """Правила из прошлых выплат: обрезан или перекрыт — 20%, мелкий — 30% (20% если чуть мелкий), не виден — исключение."""
    issues = []
    if visual["frames_with_logo"] == 0:
        issues.append({"code": "not_visible", "label": "Баннер не виден в кадре", "deduction": 100})
    else:
        avg_area = (visual["avg_area_pct"] or 0) / 100
        if avg_area < TOO_SMALL_AREA:
            issues.append({"code": "too_small", "label": f"Баннер слишком мелкий ({avg_area:.1%} кадра)", "deduction": 30})
        elif avg_area < SLIGHTLY_SMALL_AREA:
            issues.append({"code": "slightly_small", "label": f"Баннер мелковат ({avg_area:.1%} кадра)", "deduction": 20})
        if (visual["cut_off_share"] or 0) >= MAJORITY:
            issues.append({"code": "cut_off", "label": f"Баннер обрезан краем кадра ({visual['cut_off_share']:.0%} появлений)", "deduction": 20})
        if (visual["ui_overlap_share"] or 0) >= MAJORITY:
            issues.append({"code": "ui_overlap", "label": f"Баннер попадает под интерфейс Reels ({visual['ui_overlap_share']:.0%} появлений)", "deduction": 20})

    deduction = max((i["deduction"] for i in issues), default=0)
    if deduction >= 100:
        verdict = "Не засчитывать: баннер не виден"
    elif deduction:
        verdict = f"Вычет {deduction}%"
    else:
        verdict = "Полная выплата"
    return {"issues": issues, "deduction_pct": deduction, "verdict": verdict}
