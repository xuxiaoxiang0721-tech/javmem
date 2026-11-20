"""打码模式表：扫得到，不抄值，已经打码的不算。测试里的值都是编的，用拼接写，免得被当成真密钥。"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import CHAT_PACKET, MAIL_PACKET

from javmem.config import ConfigError
from javmem.redaction import default_patterns, load_patterns, redact, scan_types

PATTERNS = default_patterns()


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("后台密码是 abcd1234", "密码"),
        ("password: hunter22", "密码"),
        ("PWD=hunter22", "密码"),
        ("密码是：abcd1234", "密码"),
        ("密码为：abcd1234", "密码"),
        ("密码就是 abcd1234", "密码"),
        ("密码 abcd1234", "密码"),
        ("password is hunter22", "密码"),
        ("验证码 482913", "验证码"),
        ("你的验证码是482913，五分钟内有效", "验证码"),
        ("验证码是：123456", "验证码"),
        ("verification code is 123456", "验证码"),
        ("api_key = abcdefgh", "密钥"),
        ("密钥：abcd-efgh", "密钥"),
        ("api key is abcd1234", "密钥"),
        ("密钥：「abcd1234」", "密钥"),
        ("令牌是：abcd1234", "令牌"),
        ("the token is abcd1234efgh", "令牌"),
        ("用这个 " + "sk-" + "a" * 24, "密钥"),
        ("AKIA" + "A" * 16, "密钥"),
        ("-----BEGIN RSA " + "PRIVATE KEY-----", "密钥"),
        ("物流后台的令牌是 abcdefgh", "令牌"),
        ("token: abcdefgh", "令牌"),
        ("Authorization: Bearer " + "a" * 24, "令牌"),
        ("gh" + "p_" + "a" * 36, "令牌"),
        ("xox" + "b-" + "1" * 24, "令牌"),
        ("eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, "令牌"),
    ],
)
def test_patterns_find_each_kind(text: str, kind: str) -> None:
    assert kind in scan_types(text, PATTERNS)


@pytest.mark.parametrize(
    "text",
    [
        "物流后台的令牌是 [已隐去:令牌]，你先别用。",
        "订舱系统的令牌 [已隐去:令牌] 下周过期，记得换。",
        "后台密码是 [已隐去:密码]",
        "验证码 [已隐去:验证码]",
        "密钥：[已隐去:密钥]",
        "密码是多少？",
        "这个令牌是什么意思",
        "我忘了密码。",
        "password is required",
        "the token is expired",
        "token usage is high",
        "api key rotation",
        "the password is 8 characters",
        "手机号 13800000000，身份证 110101199001011234",  # 这些不打码
        "尾号 6688 那笔付款先别动。",
        "单价 12.5 美元",
    ],
)
def test_patterns_leave_ordinary_text_alone(text: str) -> None:
    assert scan_types(text, PATTERNS) == []


@pytest.mark.parametrize("packet", [CHAT_PACKET, MAIL_PACKET])
def test_the_spec_examples_have_no_hits(packet: str) -> None:
    assert scan_types(packet, PATTERNS) == []


def test_result_has_types_only() -> None:
    assert scan_types("密码是 abcd1234，令牌是 efgh5678", PATTERNS) == ["密码", "令牌"]


def test_table_can_be_replaced_by_a_file(tmp_path: Path) -> None:
    path = tmp_path / "patterns.yaml"
    path.write_text("patterns:\n  - {type: 令牌, regex: 'demo-[0-9]+'}\n", encoding="utf-8")
    patterns = load_patterns(path)
    assert scan_types("DEMO-123", patterns) == ["令牌"]
    assert scan_types("密码是 abcd1234", patterns) == []


def test_no_file_means_the_default_table() -> None:
    assert scan_types("密码是 abcd1234", load_patterns(None)) == ["密码"]


@pytest.mark.parametrize(
    "text",
    [
        "[不合法",
        "patterns: []\n",
        "patterns: 不是列表\n",
        "patterns:\n  - 不是字典\n",
        "patterns:\n  - {type: 手机号, regex: 'x'}\n",
        "patterns:\n  - {type: 令牌}\n",
        "patterns:\n  - {type: 令牌, regex: '(没关'}\n",
    ],
)
def test_bad_table_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "patterns.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_patterns(path)


def test_missing_table_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_patterns(tmp_path / "missing.yaml")


# ---------- 替换（转换器和导入工具用） ----------


@pytest.mark.parametrize(
    ("text", "expected", "counts"),
    [
        ("后台密码是 abcd1234", "后台密码是 [已隐去:密码]", {"密码": 1}),
        ("password: hunter22, ok", "password: [已隐去:密码] ok", {"密码": 1}),
        ("网盘密码始终是" + "9" * 6, "网盘密码始终是[已隐去:密码]", {"密码": 1}),
        ("相册密码  " + "1" * 6 + "；别外传", "相册密码  [已隐去:密码]；别外传", {"密码": 1}),
        ("你的验证码是482913，五分钟内有效", "你的验证码是[已隐去:验证码]，五分钟内有效", {"验证码": 1}),
        ("用这个 " + "sk-" + "a" * 24 + " 调接口", "用这个 [已隐去:密钥] 调接口", {"密钥": 1}),
        ("token: Bearer " + "a" * 24, "token: Bearer [已隐去:令牌]", {"令牌": 1}),
        ("Authorization: Bearer " + "a" * 24, "Authorization: Bearer [已隐去:令牌]", {"令牌": 1}),
        ("令牌是 abcd1234，密码是 efgh5678", "令牌是 [已隐去:令牌]，密码是 [已隐去:密码]", {"令牌": 1, "密码": 1}),
        (
            "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIabc\n-----END RSA " + "PRIVATE KEY-----\n后面",
            "[已隐去:密钥]\n后面",
            {"密钥": 1},
        ),
        ("-----BEGIN " + "PRIVATE KEY-----\nMIIabc 没有结尾", "[已隐去:密钥]", {"密钥": 1}),
        ("物流后台的令牌是 [已隐去:令牌]，你先别用。", "物流后台的令牌是 [已隐去:令牌]，你先别用。", {}),
        ("password is required", "password is required", {}),
        ("尾号 6688 那笔付款先别动。", "尾号 6688 那笔付款先别动。", {}),
    ],
)
def test_redact_replaces_values_and_keeps_the_words(text: str, expected: str, counts: dict[str, int]) -> None:
    assert redact(text, PATTERNS) == (expected, counts)


def test_redacted_text_has_no_hits_left() -> None:
    text = "密码是 abcd1234，验证码 482913，api_key = abcdefgh，token: Bearer " + "a" * 24
    redacted, _ = redact(text, PATTERNS)
    assert scan_types(redacted, PATTERNS) == []
    assert redact(redacted, PATTERNS) == (redacted, {})


def test_redact_with_a_pattern_without_value_group(tmp_path: Path) -> None:
    path = tmp_path / "patterns.yaml"
    path.write_text("patterns:\n  - {type: 令牌, regex: 'demo-[0-9]+'}\n", encoding="utf-8")
    assert redact("用 DEMO-123 登录", load_patterns(path)) == ("用 [已隐去:令牌] 登录", {"令牌": 1})


# ---------- 独立审阅找到的问题 ----------


@pytest.mark.parametrize(
    "text",
    [
        "用 token 还是 cookie 做鉴权？",
        "用密码还是 Face ID 登录？",
        "token 都是 base64 编码的",
        "密码都是 SHA256 存的",
        "密码也是 abcd 开头吗",
    ],
)
def test_or_and_all_are_not_connectors(text: str) -> None:
    assert scan_types(text, PATTERNS) == []
    assert redact(text, PATTERNS) == (text, {})


@pytest.mark.parametrize(
    "text",
    [
        "token 现在是 base64 编码的",
        "token 目前是 base64 编码的",
        "密码一直是 SHA256 存的",
        "密码是 bcrypt 算的",
    ],
)
def test_technical_words_are_not_secrets(text: str) -> None:
    assert scan_types(text, PATTERNS) == []


@pytest.mark.parametrize(
    "text",
    ["密码也是 abcd1234", "密码还是 abcd1234", "密码仍是 abcd1234", "密码现在是 abcd1234", "密码都是 abcd1234"],
)
def test_adverb_connectors_count_when_the_value_has_a_digit(text: str) -> None:
    assert redact(text, PATTERNS) == (text.replace("abcd1234", "[已隐去:密码]"), {"密码": 1})


@pytest.mark.parametrize(
    "text",
    ["密码是 base2024", "密码是 rsa1234!", "密码：sha12345678", "密码 = hmac_9f8e7d6c5b4a3210", "密码是 argon2_secret99"],
)
def test_values_that_only_start_like_a_technical_word_are_still_secrets(text: str) -> None:
    assert scan_types(text, PATTERNS) == ["密码"]
    assert scan_types(redact(text, PATTERNS)[0], PATTERNS) == []


@pytest.mark.parametrize("text", ["密码都是 2024 年改的", "密码还是 v1.0.3", "密码 2026 年改过"])
def test_years_and_versions_are_not_values(text: str) -> None:
    assert scan_types(text, PATTERNS) == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("验证码就是 123456", "验证码"),
        ("token=https://x.example/abc123def456", "令牌"),
        ("密码：http://admin:Passw0rd@10.0.0.1", "密码"),
        ("密码是 sha256.abcd1234", "密码"),
        ("密码是 v1.2.3secret", "密码"),
    ],
)
def test_a_technical_word_only_counts_when_it_is_the_whole_value(text: str, kind: str) -> None:
    assert scan_types(text, PATTERNS) == [kind]
    assert scan_types(redact(text, PATTERNS)[0], PATTERNS) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("auth token: Basic YWRtaW46aHVudGVyMg==", "auth token: Basic [已隐去:令牌]"),
        ("令牌：Bearer abcdef123456", "令牌：Bearer [已隐去:令牌]"),
    ],
)
def test_auth_scheme_words_are_kept_and_the_value_is_hidden(text: str, expected: str) -> None:
    assert scan_types(text, PATTERNS) == ["令牌"]
    assert redact(text, PATTERNS) == (expected, {"令牌": 1})


@pytest.mark.parametrize(
    "text",
    [
        "验证码 123456" + "sk-" + "a" * 24,
        "验证码 123456" + "gh" + "p_" + "a" * 36,
        "验证码 123456" + "AKIA" + "A" * 16,
    ],
)
def test_nothing_is_left_for_the_scan_after_redact(text: str) -> None:
    redacted, _ = redact(text, PATTERNS)
    assert scan_types(redacted, PATTERNS) == []


# ---------- 字母的、字母加数字的一次性验证码（D91） ----------
# 关键词明说是验证码（verification code、验证码、校验码），后面跟着冒号、等号、是、为、is 的，
# 4 到 8 位纯字母、字母加数字也算；码可以在同一行、下一行，或者隔一个空行。值都是编的，用拼接写

LETTER_CODE = "KX" + "QZPM"
MIXED_CODE = "B7" + "Q2XR"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Here is your verification code:\n\n" + LETTER_CODE + "\n\nIt expires in 30 minutes.", LETTER_CODE),
        ("Verification code: " + MIXED_CODE, MIXED_CODE),
        ("Your verification code is " + MIXED_CODE + ", valid for 10 minutes.", MIXED_CODE),
        ("Your verification code is: " + LETTER_CODE, LETTER_CODE),
        ("verification code:\n" + "aB" + "cD9e", "aB" + "cD9e"),
        ("验证码：" + "k3" + "m9x2", "k3" + "m9x2"),
        ("您的验证码是 " + LETTER_CODE + "，五分钟内有效", LETTER_CODE),
        ("校验码为" + MIXED_CODE, MIXED_CODE),
        ("验证码：“" + LETTER_CODE + "”", LETTER_CODE),
    ],
)
def test_letter_codes_after_an_explicit_keyword(text: str, code: str) -> None:
    redacted, counts = redact(text, PATTERNS)
    assert counts == {"验证码": 1}
    assert code not in redacted and "[已隐去:验证码]" in redacted
    assert scan_types(redacted, PATTERNS) == []


@pytest.mark.parametrize(
    "text",
    [
        "The verification code is required to log in.",  # 小写的普通单词
        "Verification code: Please enter it within 10 minutes.",  # 开头大写的普通单词
        "Please send me the verification code:\n\nThanks",  # 下一段是问候
        "Enter the verification code below and press OK.",  # 没有冒号、是、为
        "verification code SENT",  # 没有连接词
        "Promo code: " + "SAVE" + "20NOW",  # 关键词不是验证码
        "Your code: " + LETTER_CODE,
        "verification code:\n\n\n" + LETTER_CODE,  # 隔了两个空行
        "verification code: " + "ABC",  # 太短
        "verification code: " + "ABCDEFGHJ",  # 太长
        "验证码已经发到你手机上了",
        "验证码：OK了",
    ],
)
def test_letter_codes_leave_ordinary_words_alone(text: str) -> None:
    assert scan_types(text, PATTERNS) == []
    assert redact(text, PATTERNS) == (text, {})


def test_a_mixed_code_is_masked_whole_not_just_its_digits() -> None:
    """以前“验证码：1234abcd”只换掉前面的数字，后面的字母留着。"""
    redacted, counts = redact("验证码：" + "1234" + "abcd", PATTERNS)
    assert (redacted, counts) == ("验证码：[已隐去:验证码]", {"验证码": 1})


def test_aws_key_ids_are_matched_case_sensitively() -> None:
    """AWS 的密钥编号只有大写：AKIA 加 16 位大写字母、数字。整张表编译时不分大小写，这一条要分，
    不然邮件编号里碰巧有一段 AKia… 也算密钥（J3.2 收件时巡检多报了 23 条，D91）。"""
    assert scan_types("AKIA" + "ABCDEFGH" + "12345678", PATTERNS) == ["密钥"]
    for text in ("akia" + "abcdefgh12345678", "AKia" + "Bc3dEf5gHi7jKl9m", "<2026." + "AKia" + "Zz9Qw1Er2Ty3Ui4O@mail.example.com>"):
        assert scan_types(text, PATTERNS) == []
        assert redact(text, PATTERNS) == (text, {})


# ---------- D91 审阅找到的：每条先红再修 ----------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("OTP: " + "4821" + "kqzp", "4821" + "kqzp"),
        ("Your OTP is " + "B7" + "Q2XR", "B7" + "Q2XR"),
        ("动态码：" + "B7" + "Q2XR", "B7" + "Q2XR"),
        ("动态码：" + "4821" + "kqzp", "4821" + "kqzp"),
    ],
)
def test_review_otp_and_dynamic_codes_are_masked_whole(text: str, code: str) -> None:
    redacted, counts = redact(text, PATTERNS)
    assert counts == {"验证码": 1} and code not in redacted and "kqzp" not in redacted and "Q2XR" not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "The verification code is ONLY valid for 10 minutes.",
        "Your verification code is NEVER shared by our staff.",
        "Please send me the verification code:\n\nTHANKS",
        "Verification code is\nREQUIRED for every login.",
        "这个验证码是 HTTPS 页面上的吗？",
        "系统里验证码为 NULL，要重发。",
        "verification code ISLAND",
    ],
)
def test_review_capital_words_are_not_codes(text: str) -> None:
    assert redact(text, PATTERNS) == (text, {})


@pytest.mark.parametrize("text", ["verification code is" + " " * 20000 + "!", "验证码是" + " " * 20000 + "!", "OTP" + " " * 20000 + "!"])
def test_review_code_patterns_take_linear_time(text: str) -> None:
    import time

    started = time.perf_counter()
    redact(text, PATTERNS)
    scan_types(text, PATTERNS)
    assert time.perf_counter() - started < 5


# ---------- D91 第二遍审阅找到的（严重的：验证码会漏到模型那边） ----------


@pytest.mark.parametrize(
    "text",
    [
        "Your verification code is " + "KX" + "QZPM" + " and it expires in 10 minutes.",
        "验证码是 " + "KX" + "QZPM" + " 五分钟内有效",
        "验证码：" + "KX" + "QZPM" + " 请勿泄露给他人",
        "验证码：" + "KX" + "QZPM" + " 5分钟内有效",
    ],
)
def test_review2_letter_codes_followed_by_words_are_masked(text: str) -> None:
    redacted, counts = redact(text, PATTERNS)
    assert counts == {"验证码": 1} and ("KX" + "QZPM") not in redacted


@pytest.mark.parametrize(
    "text",
    ["Verification code: FAILED", "Your verification code is SECRET.", "The verification code is URGENT!", "验证码：RESENT"],
)
def test_review2_status_words_are_not_codes(text: str) -> None:
    assert redact(text, PATTERNS) == (text, {})
