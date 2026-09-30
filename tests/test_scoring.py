from app.pipeline import scoring


def detections(n, box, interval=0.5):
    return [scoring.Detection(t=i * interval, box=box, cut_off=False, hidden=False) for i in range(n)]


def test_no_logo():
    visual = scoring.visual_metrics([], 20, 0.5, 10.0, vertical=True)
    assert visual["logo_seconds"] == 0
    prominence = scoring.prominence(visual, voice_cta=False, text_cta=False, spoken=True, text_mention=False)
    assert prominence["score"] == 1
    assert scoring.placement_review(visual)["deduction_pct"] == 100


def test_big_long_logo_with_cta_is_max():
    visual = scoring.visual_metrics(detections(20, (0.3, 0.1, 0.5, 0.9)), 20, 0.5, 10.0, vertical=True)
    assert visual["logo_seconds"] == 10.0
    assert visual["segments"] == [(0.0, 10.0)]
    result = scoring.prominence(visual, voice_cta=True, text_cta=False, spoken=True, text_mention=True)
    assert result["score"] == 5
    assert scoring.placement_review(visual)["verdict"] == "Полная выплата"


def test_flash_is_low_score():
    visual = scoring.visual_metrics(detections(1, (0.4, 0.4, 0.45, 0.5)), 60, 0.5, 30.0, vertical=True)
    assert scoring.prominence(visual, False, False, False, False)["score"] == 1


def test_small_banner_deduction():
    visual = scoring.visual_metrics(detections(10, (0.4, 0.4, 0.43, 0.6)), 10, 1.0, 10.0, vertical=True)
    review = scoring.placement_review(visual)
    assert review["deduction_pct"] == 30
    assert review["issues"][0]["code"] == "too_small"


def test_cut_off_and_ui_overlap():
    cut = scoring.visual_metrics(detections(10, (0.3, 0.0, 0.4, 0.4)), 10, 1.0, 10.0, vertical=True)
    assert "cut_off" in [i["code"] for i in scoring.placement_review(cut)["issues"]]
    low = scoring.visual_metrics(detections(10, (0.9, 0.2, 0.99, 0.7)), 10, 1.0, 10.0, vertical=True)
    assert "ui_overlap" in [i["code"] for i in scoring.placement_review(low)["issues"]]


def test_calibration_examples_from_payouts():
    """Рамки, реально полученные на роликах из примеров выплат, и вердикты менеджера по ним."""
    cases = [
        ((0.083, 0.16, 0.191, 0.839), 0),   # DceO7gsR0w- — хороший
        ((0.817, 0.16, 0.925, 0.839), 0),   # Dblv_p8RP4T — хороший, баннер внизу
        ((0.103, 0.103, 0.176, 0.877), 20),  # DbpiVrpMa1j — мелкий, вычет 20%
        ((0.12, 0.22, 0.165, 0.78), 30),     # DbQGs9HMcOQ — слишком мелкий, вычет 30%
    ]
    for box, expected in cases:
        visual = scoring.visual_metrics(detections(10, box), 10, 1.0, 10.0, vertical=True)
        assert scoring.placement_review(visual)["deduction_pct"] == expected, box


def test_escalation_only_for_borderline_results():
    clear = scoring.visual_metrics(detections(20, (0.083, 0.16, 0.191, 0.839)), 20, 0.5, 10.0, vertical=True)
    assert scoring.escalation_reasons(clear) == []
    near_threshold = scoring.visual_metrics(detections(20, (0.817, 0.16, 0.9, 0.839)), 20, 0.5, 10.0, vertical=True)
    assert scoring.escalation_reasons(near_threshold)
    assert scoring.escalation_reasons(scoring.visual_metrics([], 20, 0.5, 10.0, vertical=True))
    flicker = scoring.visual_metrics(detections(2, (0.083, 0.16, 0.191, 0.839)), 20, 0.5, 10.0, vertical=True)
    assert any("кадрах" in r for r in scoring.escalation_reasons(flicker))


def test_segments_split_on_gap():
    assert scoring.segments([0, 0.5, 1.0, 5.0, 5.5], 0.5) == [(0.0, 1.5), (5.0, 6.0)]


def test_to_detection_rejects_bad_boxes():
    assert scoring.to_detection(0, [], False, False) is None
    assert scoring.to_detection(0, [500, 500, 400, 600], False, False) is None
    assert scoring.to_detection(0, [100, 200, 300, 400], False, False).box == (0.1, 0.2, 0.3, 0.4)
