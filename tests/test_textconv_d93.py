"""转文字第二轮（D93）：报告第 4 节第 8 条，HTML 里前面的属性值带着 > 的标签。编的 HTML。"""

from __future__ import annotations

import time

import pytest

from javmem.textconv import html_to_text


def test_a_data_uri_after_an_attribute_with_a_gt_inside_quotes_is_dropped() -> None:
    picture = "QUJD" * 3000
    markup = f'<p>See label.</p><img alt="a>b" src="data:image/png;base64,{picture}"><p>Thanks</p>'
    text = html_to_text(markup)
    assert "QUJD" not in text and text == "See label.\n\nThanks"


@pytest.mark.parametrize("markup", ['<a b="' * 20000 + "data:", '<a "' * 20000 + "data:", "<a '" * 20000 + "data:"])
def test_tags_with_unclosed_quotes_stay_linear(markup: str) -> None:
    started = time.perf_counter()
    html_to_text(markup)
    assert time.perf_counter() - started < 2
