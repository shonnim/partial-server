"""What the vision model is told about a figure, for a chart and for an image."""

from app.services.image_description import build_user_text, figure_texts, image_name_of

CHART = {
    "type": "chart",
    "img_path": "images/ff6570e2.jpg",
    "content": "| Year | Total Gross Debt |\n| --- | --- |\n| 2002 | ~200 |\n| 2015 | ~226 |",
    "chart_caption": ["[그림 1] 세계 총채무 추이", "(단위: GDP 대비 %, 가중평균)"],
    "chart_footnote": ["자료: IMF, Fiscal Monitor October 2016, Figure 1.1"],
    "page_idx": 4,
}

IMAGE = {
    "type": "image",
    "img_path": "images/2bbe9242.jpg",
    "image_caption": ["[그림 5] 거시경제정책과 디레버리징"],
    "image_footnote": [],
    "content": "Central bank policy rate \\- Cyclically adjusted primary balance",
    "page_idx": 13,
}


def test_chart_texts_come_from_the_chart_keys() -> None:
    caption, footnote, reading = figure_texts(CHART)

    assert caption == "[그림 1] 세계 총채무 추이\n(단위: GDP 대비 %, 가중평균)"
    assert footnote == "자료: IMF, Fiscal Monitor October 2016, Figure 1.1"
    assert reading == CHART["content"]


def test_image_texts_come_from_the_image_keys_and_lose_the_escaping() -> None:
    caption, footnote, reading = figure_texts(IMAGE)

    assert caption == "[그림 5] 거시경제정책과 디레버리징"
    assert footnote is None
    assert reading == "Central bank policy rate - Cyclically adjusted primary balance"


def test_blank_reading_is_none() -> None:
    assert figure_texts({**IMAGE, "content": "   "})[2] is None
    assert figure_texts({**IMAGE, "content": None})[2] is None


def test_user_text_always_carries_the_three_labels() -> None:
    text = build_user_text("[그림 1] 세계 총채무 추이", None, "| Year | Debt |")

    assert text == (
        "CAPTION:\n[그림 1] 세계 총채무 추이\n\n"
        "FOOTNOTE:\n(none)\n\n"
        "EXTRACTED CONTENT:\n| Year | Debt |"
    )


def test_user_text_says_none_for_everything_missing() -> None:
    assert build_user_text(None, None) == (
        "CAPTION:\n(none)\n\nFOOTNOTE:\n(none)\n\nEXTRACTED CONTENT:\n(none)"
    )


def test_image_name_is_the_last_path_segment() -> None:
    assert image_name_of(CHART) == "ff6570e2.jpg"
    assert image_name_of({"type": "chart"}) is None
    assert image_name_of({"type": "chart", "img_path": "  "}) is None
