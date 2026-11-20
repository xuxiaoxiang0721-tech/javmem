"""打码模式表（packet-spec 第 6、7 节）：认密码、验证码、密钥和令牌。

打码是打包方的事：转换器和导入工具用 redact 替换内容。收件时再用 scan_types 扫一遍，
命中只提醒，不拒收，也不改文件。提醒只写类型，不抄值。
默认的表写在这里，可以用 JAVMEM_REDACTION_FILE 换一份：

    patterns:
      - {type: 密码, regex: '...'}

正则里有命名组 value 时只替换这一段，关键词留着，写成“密码是 [已隐去:密码]”。没有就替换整段。

值只认连续的英文、数字和符号：“密码是多少”不算，已经打码的 [已隐去:密码] 也不算。
连接词明确（是、为、冒号、等号）时，值有四个字符就算。只隔空白或者英文 is 时，值里要有数字，
不然 “password is required”、“token usage” 这种普通的话也会提醒。

替换按表的顺序来，所以有固定长相的（sk-、Bearer）排在前面。不然 “token: Bearer xxx”
会先按关键词把 Bearer 这个词替换掉，真正的令牌反而留下了。

验证码认 4 到 8 位数字；关键词明说是验证码（verification code、验证码、校验码）、后面跟着冒号（是、为、is）的，
4 到 8 位字母、字母加数字的码也算，码可以在下一行、隔一个空行（D91）。全小写、只有开头大写的是普通单词，不算。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from javmem.config import ConfigError
from javmem.yamlio import load_yaml

REDACTION_TYPES = ("密码", "验证码", "密钥", "令牌")

_QUOTE = "[「『“\"']?"
# 明确的连接词：是、就是、为、冒号、等号。“\s*[:：=]?\s*”写成“\s*(?:[:：=]\s*)?”：认的一样，
# “密码是”后面一大串空格时不会平方级地回溯（D93：以前 2 万个空格要 65 秒）
_SAID = rf"\s*(?:(?:就?是|为)\s*(?:[:：=]\s*)?|[:：=]\s*){_QUOTE}"
# 带副词的“是”（一直都是、也是、还是、现在是……）值里要有数字才算：
# “用密码还是 Face ID 登录”说的是“或者”，“网盘密码始终是 999999”说的才是值
_ADVERB = "(?:一直都|一直|始终|依然|仍然|仍|还|也|都|总|现在|目前)"
_ADVERB_SAID = rf"\s*{_ADVERB}是\s*(?:[:：=]\s*)?{_QUOTE}(?=[!-~]*\d)"
_LOOSELY = rf"(?:\s+is)?\s+{_QUOTE}(?=[!-~]*\d)"  # 只隔空白或者英文 is，后面的值里要有数字
# 不是值的几种：技术名词只认固定写法（“token 现在是 base64 编码的”，base2024 还是值）、
# 带 v 的版本号（“密码还是 v1.0.3”）、年份（“密码都是 2024 年改的”）。
# 技术名词和版本号要是整个值：后面紧跟的还是值的一部分（https://…、sha256.xxx、v1.2.3secret）就照样算值
_NOT_SECRET = (
    r"(?:(?:base(?:32|58|64|85)|sha(?:1|224|256|384|512|3)?|md5|bcrypt|scrypt|argon2(?:i|d|id)?|pbkdf2"
    r"|hmac(?:-sha(?:1|256|512))?|aes(?:128|192|256)?|rsa(?:1024|2048|3072|4096)?|utf-?8|ascii|cookies?"
    r"|oauth2?|jwt|json|https?|v\d+(?:\.\d+)+)(?![!-~])"
    r"|(?:19|20)\d\d\s*年)"
)
# 一段连续的英文、数字和符号。“token: Bearer xxx” 里 Bearer、Basic 是认证方式，值在它后面
_VALUE = rf"(?:(?:bearer|basic)\s+)?(?!(?:bearer|basic)\b)(?!{_NOT_SECRET})(?P<value>[!-~]{{4,}})"
_WORD_START = r"(?<![A-Za-z0-9])"
_MAX_ROUNDS = 5  # 一遍换完可能露出新的：“验证码 123456sk-xxx” 换掉验证码后 sk- 才像个密钥


# JSON、字典的写法："password":"…"、'token': '…'：关键词后面紧跟着引号，引号后面紧跟着冒号、等号才算
# （D93 审阅第 13 条；HTML 的 type="password" name=… 只隔空白，不算）
_KEY_SAID = r"[\"'”」』]\s*[:：=]\s*" + _QUOTE


# passphrase 后面跟着冒号、等号的，值一直算到行尾，最多 6 个词（D99 第 43 条：口令常是几个词连着写，以前只换掉第一个词，
# 后面几个词原样进包）。引号开头的到同一种收尾的引号为止（"rock'n'roll" 里的 ' 不算收尾）；不带引号开头的，值里可以有
# 引号（第四轮审阅第 4 条：“rock'n'roll forever”以前只换掉 rock）
_PASSPHRASE = (
    r"passphrase[\"'”」』]?[ \t]*[:：=][ \t]*"
    # 换完会再扫一遍：占位本身（[已隐去:密码]，带不带引号）不算值
    r"(?![「『“\"']?\[已隐去:)"
    + _QUOTE
    + r"(?P<value>(?<=\")[^\"\r\n]+|(?<=')[^'\r\n]+|(?<=“)[^”\r\n]+|(?<=「)[^」\r\n]+|(?<=『)[^』\r\n]+"
    r"|(?<![「『“\"'])\S+(?:[ \t]+\S+){0,5})"
)


def _keyword_then_value(keywords: str) -> str:
    return rf"(?:{keywords})(?:{_SAID}|{_ADVERB_SAID}|{_LOOSELY}|{_KEY_SAID}){_VALUE}"


# 字母的、字母加数字的一次性验证码（D91）。关键词要明说是验证码（verification code、验证码、校验码，还有动态码、OTP），
# 码在同一行，或者在下一行、隔一个空行（“Here is your verification code:”空一行，下一行是码）。码是 4 到 8 位：
# - 字母加数字的（B7Q2XR、4821kqzp）：连接词是冒号、等号、是、为、is 都行；
# - 只有字母的（KXQZPM）：码后面接着话也照样换掉（“… is KXQZPM and it expires …”）。全小写、只有开头一个大写的
#   是普通单词，常见的大写词（THANKS、NONE、NULL、ONLY、NEVER、HTTPS、FAILED……）也不算：“verification code is
#   required”“…is ONLY valid”“验证码是 HTTPS 页面”“Verification code: Please …”“…the verification code:”下面一段是“Thanks”。
# 要排在只认数字的那一条前面：“验证码：1234abcd”整个换掉，不能只换掉前面的数字。
# 空白只吃一遍（is 后面的冒号和下一行的空白分开写），一大串空格也是线性的
_CODE_WORDS = r"(?:验证码|校验码|动态码|verification[ _-]?code|otp)"
# JSON、字典、带引号的写法："otp":"…"、“验证码”：…（D93 第二遍审阅：以前不打码）。引号后面紧跟着冒号、等号才算
_CODE_KEY_SAID = r"[\"'”」』][ \t]*[:：=]"
_CODE_SAID = rf"\s*(?:(?:就|{_ADVERB}){{0,3}}是|为|is\b)"
_CODE_GAP = r"[ \t]*(?:\r?\n[ \t]*){0,2}"
# 技术名词不是码（D93）：“verification code is SHA256 hashed”“验证码是 UTF8 编码的”“… is base64 encoded”
_CODE_TECH = (
    r"(?:base(?:32|58|64|85)|sha(?:1|224|256|384|512|3)?|md5|utf-?(?:8|16|32)|ucs-?[24]|aes(?:128|192|256)?"
    r"|rsa(?:1024|2048|3072|4096)?|crc32|x509|ipv[46]|h26[45]|mp[34]|pbkdf2|argon2(?:i|d|id)?|ascii|json|bcrypt|scrypt)"
    r"(?![A-Za-z0-9])"
)
_MIXED_CODE = rf"(?!{_CODE_TECH})(?=[A-Za-z0-9]{{0,7}}[A-Za-z])(?=[A-Za-z0-9]{{0,7}}[0-9])[A-Za-z0-9]{{4,8}}(?![A-Za-z0-9])"
_CODE_STOP_WORDS = (
    "THANKS|THANK|THX|REGARDS|PLEASE|NONE|NULL|NIL|ONLY|NEVER|ALWAYS|REQUIRED|NEEDED|OPTIONAL|MANDATORY|INVALID|VALID"
    "|EXPIRED|WRONG|INCORRECT|CORRECT|ACTIVE|INACTIVE|DISABLED|ENABLED|BLOCKED|LOCKED|MISSING|BLANK|EMPTY|UNKNOWN"
    "|PENDING|SENT|RESENT|RESEND|RESET|CHANGED|UPDATED|FAILED|FAIL|SUCCESS|PASSED|SECRET|PRIVATE|PUBLIC|URGENT"
    "|IMPORTANT|WARNING|ERROR|TRUE|FALSE|SAME|BELOW|ABOVE|HERE|THERE|ATTACHED|HTTPS|HTTP|TEST|TESTING|CODE|NOTE|DONE"
    "|OKAY|YES"
    # D93：常见的状态词、形容词（“Verification code: APPROVED”“验证码：RECEIVED”“Your OTP is VERIFIED.”）
    "|APPROVED|ACCEPTED|REJECTED|RECEIVED|VERIFIED|DENIED|REVOKED|DELETED|CLOSED|OPENED|ISSUED|SHOWN|GIVEN|NOTED"
    "|LISTED|STATED|SAVED|STORED|HIDDEN|MASKED|USED|UNUSED|REUSED|READY|OVERDUE|LOST|STOLEN|FIXED|MATCH|MATCHED"
    "|ENCLOSED|INCLUDED|PROVIDED|REQUEST|RENEWED|REPLACED|TEMP|VOID|DRAFT|FINAL|NOTICE|ALERT|APPLIED|ASSIGNED|BLOCK"
    "|CHECK|CHECKED|CLEAR|CLEARED|COPIED|ENTERED|EXPIRES|EXPIRY|FORGOT|INSIDE|ISSUE|LATER|MISMATCH|PAUSED|PRINTED"
    "|RETRY|SECURE|SHARED|SKIPPED|STALE|TYPED|UNIQUE|UNSENT|UPDATE|VIEW|VISIBLE"
)
_LETTER_CODE_VALUE = (
    rf"(?-i:(?![A-Z]?[a-z]+(?![A-Za-z0-9])))(?!(?:{_CODE_STOP_WORDS})(?![A-Za-z0-9]))(?!{_CODE_TECH})"
    rf"[A-Za-z]{{4,8}}(?![A-Za-z0-9])"
)
_MIXED_CODE_PATTERN = (
    rf"{_CODE_WORDS}(?:{_CODE_SAID}(?:[ \t]*[:：=])?|[ \t]*[:：=]|{_CODE_KEY_SAID}){_CODE_GAP}{_QUOTE}(?P<value>{_MIXED_CODE})"
)
_LETTER_CODE_PATTERN = (
    rf"{_CODE_WORDS}(?:{_CODE_SAID}(?:[ \t]*[:：=])?|[ \t]*[:：=]|{_CODE_KEY_SAID}){_CODE_GAP}{_QUOTE}"
    rf"(?P<value>{_LETTER_CODE_VALUE})"
)


_DEFAULT_PATTERNS = (
    ("密钥", rf"{_WORD_START}sk-[A-Za-z0-9_-]{{16,}}"),
    # AWS 的密钥编号只有大写：AKIA 加 16 位大写字母、数字。整张表编译时不分大小写，这一条要分，
    # 不然邮件编号（Message-ID）里碰巧有一段 AKia… 也算密钥（D91）
    ("密钥", rf"{_WORD_START}(?-i:AKIA[0-9A-Z]{{16}})"),
    # 只有开头这一行也要算：替换时要连密钥本身一起去掉，缺了结尾就到文字末尾
    ("密钥", r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?s:.*?-----END [A-Z ]*PRIVATE KEY-----|.*)"),
    ("令牌", rf"{_WORD_START}bearer\s+(?P<value>[A-Za-z0-9._~+/-]{{16,}})"),
    ("令牌", rf"{_WORD_START}gh[pousr]_[A-Za-z0-9]{{20,}}"),
    ("令牌", rf"{_WORD_START}xox[abprs]-[A-Za-z0-9-]{{10,}}"),
    ("令牌", rf"{_WORD_START}eyJ[A-Za-z0-9_-]{{8,}}\.[A-Za-z0-9_-]{{8,}}\.[A-Za-z0-9_-]+"),
    # passphrase、client_secret 是 D96 第 42 条补的，app_secret、consumer_secret 是 D99 第 43 条补的；
    # 单独的 secret 太常见（“the secret is …”），不算
    ("密码", _PASSPHRASE),
    ("密码", _keyword_then_value("密码|口令|password|passwd|pwd|passphrase")),
    ("验证码", _MIXED_CODE_PATTERN),
    ("验证码", _LETTER_CODE_PATTERN),
    (
        "验证码",
        # “\s*[:：]?\s*”写成“\s*(?:[:：]\s*)?”：认的一样，一大串空格时不会平方级地回溯
        rf"{_CODE_WORDS}(?:{_CODE_KEY_SAID}[ \t]*|(?:\s*(?:(?:就|{_ADVERB}){{0,3}}是|为|is))?\s*(?:[:：=]\s*)?){_QUOTE}"
        r"(?P<value>\d{4,8})(?!\d)",
    ),
    ("密钥", _keyword_then_value(
        "密钥|私钥|api[ _-]?key|secret[ _-]?key|access[ _-]?key|client[ _-]?secret|app[ _-]?secret|consumer[ _-]?secret"
    )),
    ("令牌", _keyword_then_value("令牌|token")),
)


@dataclass(frozen=True)
class RedactionPattern:
    type: str
    regex: re.Pattern[str]


def _compile(kind: object, regex: object) -> RedactionPattern:
    if kind not in REDACTION_TYPES:
        raise ConfigError(f"打码模式表的 type 只能是 {'、'.join(REDACTION_TYPES)}")
    if not isinstance(regex, str):
        raise ConfigError("打码模式表的 regex 要写成文本")
    try:
        return RedactionPattern(kind, re.compile(regex, re.IGNORECASE))
    except re.error:
        raise ConfigError("打码模式表里有写错的正则") from None


def default_patterns() -> list[RedactionPattern]:
    return [_compile(kind, regex) for kind, regex in _DEFAULT_PATTERNS]


def load_patterns(path: Path | None) -> list[RedactionPattern]:
    if path is None:
        return default_patterns()
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise ConfigError("打码模式表读不了") from None
    except (yaml.YAMLError, RecursionError):
        raise ConfigError("打码模式表不是合法的 YAML") from None
    entries = data.get("patterns") if isinstance(data, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise ConfigError("打码模式表要写 patterns 列表")
    patterns = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ConfigError("打码模式表的每一项要写 type 和 regex")
        patterns.append(_compile(entry.get("type"), entry.get("regex")))
    return patterns


def scan_types(text: str, patterns: Sequence[RedactionPattern]) -> list[str]:
    """这段文字命中了哪些类型。不返回命中的内容。"""
    return [kind for kind in REDACTION_TYPES if any(p.type == kind and p.regex.search(text) for p in patterns)]


def _replace_all(text: str, pattern: RedactionPattern, counts: dict[str, int]) -> str:
    def replace(match: re.Match[str]) -> str:
        counts[pattern.type] = counts.get(pattern.type, 0) + 1
        placeholder = f"[已隐去:{pattern.type}]"
        if "value" not in pattern.regex.groupindex or match.group("value") is None:
            return placeholder
        start, end = match.span("value")
        whole = match.group(0)
        return whole[: start - match.start()] + placeholder + whole[end - match.start() :]

    return pattern.regex.sub(replace, text)


def redact(text: str, patterns: Sequence[RedactionPattern]) -> tuple[str, dict[str, int]]:
    """把命中的内容换成 [已隐去:类型]，返回新文字和每种类型换了几处。不返回换掉的内容。

    换完再扫，还有命中就再换一遍，所以换完的文字 scan_types 扫不出东西。
    """
    counts: dict[str, int] = {}
    for _ in range(_MAX_ROUNDS):
        before = text
        for pattern in patterns:
            text = _replace_all(text, pattern, counts)
        if text == before:
            break
    return text, counts
