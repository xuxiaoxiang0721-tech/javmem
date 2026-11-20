"""打码第二轮（D93）：报告第 4 节第 8 条的打码误伤，第 10 条的平方级。
值都是编的，用拼接写，不出现一整串像码的字。"""

from __future__ import annotations

import time

import pytest

from javmem.redaction import default_patterns, redact

PATTERNS = default_patterns()
LETTERS = "KX" + "QZPM"
MIXED = "B7" + "Q2XR"


# ---------- 第 8 条：打码误伤 ----------


@pytest.mark.parametrize(
    "text",
    [
        "The verification code is SHA256 hashed before storage.",
        "verification code is base64 encoded",
        "验证码是 UTF8 编码的",
        "Verification code: APPROVED",
        "验证码：RECEIVED",
        "Your OTP is VERIFIED.",
        "The verification code is REVOKED!",
    ],
)
def test_technical_and_status_words_after_the_keyword_are_not_codes(text: str) -> None:
    assert redact(text, PATTERNS) == (text, {})


@pytest.mark.parametrize(
    "text",
    ["verification code: " + LETTERS, "验证码：" + MIXED, "Your OTP is " + MIXED + " and it expires soon."],
)
def test_real_codes_are_still_redacted(text: str) -> None:
    redacted, counts = redact(text, PATTERNS)
    assert counts == {"验证码": 1} and LETTERS not in redacted and MIXED not in redacted


# ---------- 第 10 条：密码、密钥、令牌那几条，“是”“为”后面一大串空白也是线性的 ----------


@pytest.mark.parametrize(
    "keyword", ["密码是", "密码还是", "密钥为", "令牌就是", "password is", '"password":', '"token"', '"otp":', "“验证码”：", '"otp"']
)
def test_long_runs_of_spaces_after_the_keyword_are_linear(keyword: str) -> None:
    text = keyword + " " * 10000 + "!"
    started = time.perf_counter()
    assert redact(text, PATTERNS) == (text, {})
    assert time.perf_counter() - started < 2


def test_the_connective_still_works_with_spaces_and_a_colon() -> None:
    value = "abcd" + "1234"
    for text in ("密码是  :  " + value, "密码为： " + value, "密钥是 " + value, "token = " + value):
        redacted, counts = redact(text, PATTERNS)
        assert value not in redacted and sum(counts.values()) == 1, text


# ---------- 第一遍审阅第 13 条：JSON、字典的写法 ----------


@pytest.mark.parametrize(
    "template",
    [
        '{{"password":"{}"}}',
        '{{"password": "{}"}}',
        '{{"password" : "{}"}}',
        "{{'password': '{}'}}",
        '{{"token":"{}"}}',
        '{{"api_key":"{}"}}',
        '{{"密码"：“{}”}}',
    ],
)
def test_json_style_keys_are_redacted(template: str) -> None:
    """D93 审阅第 13 条：JSON 写法（"password":"…"）以前不打码：关键词后面紧跟着引号，引号后面才是冒号。"""
    value = "Zx" + "9q" * 6 + "88"
    redacted, counts = redact(template.format(value), PATTERNS)
    assert value not in redacted and sum(counts.values()) == 1


@pytest.mark.parametrize(
    "text", ['<input type="password" name="pwd2">', 'Leave "password" empty for 2 days.', "{'token' 2}"]
)
def test_a_quoted_keyword_without_a_colon_is_not_a_value(text: str) -> None:
    """引号后面要紧跟着冒号、等号才算 JSON 写法：只隔空白的（HTML 的 type="password" name=…）不算。"""
    assert redact(text, PATTERNS) == (text, {})


# ---------- 第二遍审阅（以前就有的）：验证码的 JSON、带引号的写法 ----------

DIGITS = "40" + "1739"


@pytest.mark.parametrize("value", [DIGITS, MIXED, LETTERS])
@pytest.mark.parametrize(
    "template",
    [
        '{{"otp":"{}"}}',
        '{{"otp": "{}"}}',
        "{{'otp': '{}'}}",
        '{{"验证码":"{}"}}',
        "“验证码”：{}",
        '{{"verification_code":"{}"}}',
        '{{"verificationCode" : "{}"}}',
    ],
)
def test_json_and_quoted_code_keys_are_redacted(template: str, value: str) -> None:
    """D93 第二遍审阅：关键词后面紧跟着引号、引号后面才是冒号的验证码，以前不打码，码原样进包。"""
    redacted, counts = redact(template.format(value), PATTERNS)
    assert value not in redacted and counts == {"验证码": 1}


@pytest.mark.parametrize(
    "text",
    ['{"otp_sent": true}', '{"otp": null}', '{"has_otp": "yes"}', '"otp": "VERIFIED"', "“验证码”：已发送", '"verification_code": str'],
)
def test_quoted_code_keys_without_a_code_are_left_alone(text: str) -> None:
    assert redact(text, PATTERNS) == (text, {})


# ---------- D96 第 42 条：补 client_secret、passphrase，单独的 secret 不补 ----------


@pytest.mark.parametrize(
    ("template", "kind"),
    [
        ("passphrase: {}", "密码"),
        ("The passphrase is {}", "密码"),
        ("client_secret = {}", "密钥"),
        ("client-secret: {}", "密钥"),
        ('{{"client_secret":"{}"}}', "密钥"),
    ],
)
def test_client_secret_and_passphrase_are_redacted(template: str, kind: str) -> None:
    """D96 第 42 条：client_secret、passphrase 以前不在关键词里，值原样进包。"""
    value = "Zx" + "9q" * 6 + "88"
    redacted, counts = redact(template.format(value), PATTERNS)
    assert value not in redacted and counts == {kind: 1}


@pytest.mark.parametrize("text", ["The secret is to ship early.", "Use a passphrase you can remember.", "secret: 20 pallets"])
def test_a_lone_secret_is_not_a_keyword(text: str) -> None:
    """单独的 secret 太常见（“the secret is …”），不补。"""
    assert redact(text, PATTERNS) == (text, {})


# ---------- D99（第四轮）：第 43 条 ----------

WORDS = ["blue", "tiger", "moon", "river", "lamp", "stone", "cloud", "field"]


@pytest.mark.parametrize(
    ("template", "count"),
    [
        ("Wi-Fi passphrase: {}", 4),
        ("passphrase = {}", 6),
        ('{{"passphrase": "{}"}}', 3),
        ("PASSPHRASE：{}", 2),
    ],
)
def test_a_passphrase_after_a_colon_is_redacted_to_the_end_of_the_line(template: str, count: int) -> None:
    """D99 第 43 条：passphrase 后面跟冒号、等号的，值一直算到行尾，最多 6 个词（以前只换掉第一个词，后面几个词原样进包）。"""
    phrase = " ".join(WORDS[:count])
    redacted, counts = redact(template.format(phrase) + "\nShip on Friday.", PATTERNS)
    assert counts == {"密码": 1} and redacted.endswith("\nShip on Friday.")
    assert not any(word in redacted for word in WORDS[:count])


def test_a_passphrase_stops_after_six_words() -> None:
    """最多 6 个词：后面的照录（一行里接着写的别的话不一起换掉）。"""
    redacted, counts = redact("passphrase: " + " ".join(WORDS), PATTERNS)
    assert counts == {"密码": 1} and redacted.endswith("cloud field")
    assert not any(word in redacted for word in WORDS[:6])


@pytest.mark.parametrize("template", ["app_secret = {}", "consumer_secret: {}", '{{"app_secret":"{}"}}', "consumer-secret={}"])
def test_app_and_consumer_secrets_are_redacted(template: str) -> None:
    """D99 第 43 条顺带：app_secret、consumer_secret 以前不认。"""
    value = "Zx" + "9q" * 6 + "88"
    redacted, counts = redact(template.format(value), PATTERNS)
    assert value not in redacted and counts == {"密钥": 1}


@pytest.mark.parametrize("text", ["The passphrase is long.", "Use a passphrase you can remember.", "The app secret is stored in the vault."])
def test_passphrase_and_app_secret_in_plain_words_are_left_alone(text: str) -> None:
    assert redact(text, PATTERNS) == (text, {})



# ---------- 第四轮审阅：passphrase 的值里有引号 ----------


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("passphrase: rock'n'roll forever 2031", ""),
        ("Wi-Fi passphrase = it's \"blue\" moon", ""),
        ('{"passphrase": "rock\'n\'roll forever", "next": 1}', '", "next": 1}'),
        ("passphrase: 'blue tiger moon' then ship", "' then ship"),
    ],
)
def test_quotes_inside_a_bare_passphrase_do_not_cut_it_short(text: str, kept: str) -> None:
    """第四轮审阅第 4 条：值里碰到引号就停，后面几个词原样进包（“rock'n'roll forever 2031”只换掉 rock，比 298c049 以前
    还漏得多）。不带引号开头的值里可以有引号；带引号开头的照旧到收尾的引号为止。"""
    redacted, counts = redact(text, PATTERNS)
    assert counts == {"密码": 1}
    assert redacted.endswith(kept)
    for word in ("rock", "roll", "forever", "2031", "blue", "moon", "tiger"):
        assert word not in redacted
