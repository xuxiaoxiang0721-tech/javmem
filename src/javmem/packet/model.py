"""解析出来的包长什么样。后面的处理线（L1 起）直接用这些结构，不用再解析一遍文本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 不拒收、进巡检的警告（packet-spec 第 3、5 节）
UNKNOWN_HEADING = "unknown_heading"
BAD_LINE = "bad_line"
UNRECOGNIZED_LINE = "unrecognized_line"
TIME_BACKWARDS = "time_backwards"
BAD_ATTACHMENT = "bad_attachment"  # 附件块写错了（第 3 节“附件小节”）：这一块不进原文索引，归档里还在
# 下面四种规范没列，所以只提醒，不改解析结果，也不拒收
UNKNOWN_FIELD = "unknown_field"  # 认不出的包头字段，可能是 sensitive 打错了字
ID_TIME_MISMATCH = "id_time_mismatch"  # packet_id 的日期和时分和 covers_to 对不上
SUSPECT_CONTINUATION = "suspect_continuation"  # 缩进的续行，长得像另一个人在说话
ODD_SPEAKER = "odd_speaker"  # 说话人不像人名，可能漏写了说话人

# query-api 4.5 的 origin 和 speaker_role
ORIGIN_SAID = "said"
ORIGIN_RELAYED = "relayed"
ORIGIN_EXTERNAL = "external"
ORIGIN_ATTACHMENT_NOTE = "attachment_note"
ROLE_OWNER = "owner"
ROLE_BOT = "bot"
ROLE_OTHER = "other"

OWNER_SPEAKER = "我"


@dataclass(frozen=True)
class ParseWarning:
    kind: str
    line: int  # 文件的第几行，从 1 数
    detail: str = ""  # 不抄原文


@dataclass(frozen=True)
class PacketHeader:
    packet_id: str
    revision: int
    created_at: datetime
    source: str
    framework: str
    channel: str
    env: str
    kind: str
    covers_from: datetime
    covers_to: datetime
    domain: tuple[str, ...]
    privacy: str
    sensitive: tuple[str, ...]
    entities: tuple[str, ...]
    continues: str | None
    mailbox: str | None

    @property
    def tags(self) -> tuple[str, ...]:
        """包的敏感标记：包头 sensitive，privacy 是 private 时再加上保留标记 private。"""
        return (*self.sensitive, "private") if self.privacy == "private" else self.sensitive


@dataclass
class TextLine:
    text: str  # 去掉缩进和首尾空白，逐字
    quoted: bool  # 去掉缩进后以 "> " 开头的行
    file_line: int


@dataclass
class Utterance:
    """一句话：行首的时间和说话人，加上它的续行和引用行。"""

    number: int  # 小节里第几句，就是 position.line
    file_line: int
    said_at: datetime
    speaker: str
    speaker_role: str
    origin: str = ORIGIN_SAID
    lines: list[TextLine] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return any(line.text for line in self.lines)


@dataclass
class ChatSection:
    number: int  # 第几个原话小节，就是 position.section
    file_line: int
    utterances: list[Utterance]


@dataclass(frozen=True)
class Address:
    name: str | None
    addr: str

    def __str__(self) -> str:
        return f"{self.name} <{self.addr}>" if self.name else self.addr


@dataclass(frozen=True)
class MailHeaders:
    message_id: str
    thread_id: str
    in_reply_to: str | None
    references: tuple[str, ...]
    sender: Address
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    bcc: tuple[Address, ...]
    date: datetime
    subject: str
    attachments: tuple[str, ...]  # 附件名
    inline_images: int = 0  # 内嵌图几张，不在 attachments 里（规范第 6 节，D91）


@dataclass
class MailSection:
    number: int  # 第几封，就是 position.section
    file_line: int
    headers: MailHeaders
    body: list[str]  # 正文逐行，去掉两格缩进，空行是空字符串。下标加一就是 position.line


@dataclass
class AttachmentNote:
    number: int  # 附件小节里第几行，不算空行，就是 position.line
    file_line: int
    text: str


# 附件正文收了多少（packet-spec 第 3 节“附件小节”）
TEXT_FULL = "full"
TEXT_TRUNCATED = "truncated"
TEXT_NONE = "none"
TEXT_STATES = (TEXT_FULL, TEXT_TRUNCATED, TEXT_NONE)

# 只收了一部分（truncated）、没收（none）的原因，规范第 3 节的表。别的取值算写错的块
REASON_TOO_LONG = "too_long"  # 转出来超过 2 万字，截断了
REASON_GARBLED = "garbled"  # PDF 里有几页读出来是乱码，没收那几页
REASON_TOO_LARGE = "too_large"  # 文件超过 20 MB，没打开
REASON_IMAGE = "image"
REASON_ENCRYPTED = "encrypted"  # 要密码才打得开，或者写着不许提取文字
REASON_NO_TEXT = "no_text"  # 扫描件，或者每一页都是乱码
REASON_TIMEOUT = "timeout"  # 读了太久还没读完，多半是故意做成这样的
REASON_UNSUPPORTED = "unsupported"
REASON_UNREADABLE = "unreadable"
REASON_MISSING = "missing"  # 打包方拿不到这个文件
TRUNCATED_REASONS = (REASON_TOO_LONG, REASON_GARBLED)
NONE_REASONS = (
    REASON_TOO_LARGE,
    REASON_IMAGE,
    REASON_ENCRYPTED,
    REASON_NO_TEXT,
    REASON_TIMEOUT,
    REASON_UNSUPPORTED,
    REASON_UNREADABLE,
    REASON_MISSING,
)


@dataclass(frozen=True)
class AttachmentLine:
    number: int  # 附件小节里第几行，和说明连着数，不算空行、### 那一行和 yaml，就是 position.line
    file_line: int
    text: str  # 去掉两格缩进，逐字


@dataclass
class AttachmentBody:
    """附件小节里的一块：一个附件的 yaml 和正文。of 指着它属于的那句话（对话）或者那封信（邮件）。"""

    number: int  # 第几块，从 1 数。原文索引的 section_no 存的就是它
    file_line: int  # ### 那一行
    name: str
    type: str
    size: int | None
    of_section: int  # 对话是第几个原话小节，邮件是第几封
    of_line: int | None  # 对话是那个小节里第几句话；邮件是空的
    text: str  # full / truncated / none
    reason: str | None
    lines: list[AttachmentLine] = field(default_factory=list)


@dataclass
class Packet:
    header: PacketHeader
    text: str  # 统一换行符、去掉 BOM 之后的全文
    content_hash: str
    sections: list[ChatSection | MailSection]
    notes: list[AttachmentNote]
    warnings: list[ParseWarning]
    attachments: list[AttachmentBody] = field(default_factory=list)  # 写对了的附件块
