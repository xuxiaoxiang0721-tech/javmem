"""测试用的小 PDF，现拼，不在仓库里放二进制文件。

只用标准 14 种字体（不用嵌字体），所以只写英文。乱码页的字体把小写字母对到 a1、a2……这种字形名，读出来是
一串装饰符号，就像真实数据里字体没有对照表的那种。加密用 PDF 1.4 标准安全处理器的 RC4 40 位（V1、R2），算法照
PDF 规范 7.6.3：密钥、O、U 都是 MD5 加 RC4，Python 自带的 hashlib 就够。
"""

from __future__ import annotations

import hashlib
import struct

_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_FILE_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")
ALL_PERMISSIONS = -4  # 打印、修改、复制提取文字、批注都行
NO_EXTRACT = -4 & ~16  # 第 5 位（复制、提取文字）是 0


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def _padded(password: str) -> bytes:
    return (password.encode("latin-1") + _PAD)[:32]


def _security(user: str, owner: str, permissions: int) -> tuple[bytes, bytes, bytes]:
    """(文件密钥, O, U)。R2，密钥 5 个字节。"""
    owner_key = hashlib.md5(_padded(owner or user)).digest()[:5]
    o_value = _rc4(owner_key, _padded(user))
    key = hashlib.md5(_padded(user) + o_value + struct.pack("<i", permissions) + _FILE_ID).digest()[:5]
    u_value = _rc4(key, _PAD)
    return key, o_value, u_value


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_fanout_pdf(depth: int) -> bytes:
    """读起来极慢的 PDF：每个表单对象画两次下一个，一共画 2**depth 次，文件才 1 KB 多（审阅复现的那种）。"""

    def stream(data: bytes, extra: bytes = b"") -> bytes:
        return b"<< /Length " + str(len(data)).encode() + extra + b" >>\nstream\n" + data + b"\nendstream"

    forms = list(range(5, 5 + depth))  # 5、6、7……是表单，最后一个是字体
    font = 5 + depth
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        f" /Resources << /XObject << /X 5 0 R >> /Font << /F1 {font} 0 R >> >> >>".encode(),
        stream(b"BT /F1 12 Tf 72 720 Td (Hello) Tj ET /X Do"),
    ]
    for index, number in enumerate(forms):
        form = b" /Type /XObject /Subtype /Form /BBox [0 0 1 1]"
        if index + 1 < len(forms):
            objects.append(stream(b"/N Do /N Do", form + f" /Resources << /XObject << /N {number + 1} 0 R >> >>".encode()))
        else:
            objects.append(stream(b"0 0 m 1 1 l S", form))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_pdf(
    lines: list[str] | None = None,
    *,
    pages: list[list[str]] | None = None,
    dingbat_pages: tuple[int, ...] = (),
    user_password: str | None = None,
    owner_password: str = "owner",
    permissions: int = ALL_PERMISSIONS,
) -> bytes:
    """一页一行一句：lines 是一页，pages 是几页。一页什么字都没有就像扫描件。dingbat_pages 里的页（从 0 数）
    用乱码的字体写，小写字母读出来是装饰符号。user_password 不是 None 就加密，写空字符串是不用密码也能打开、
    只有权限限制的那种。"""
    pages = pages if pages is not None else [lines or []]
    encrypted = user_password is not None
    key = o_value = u_value = b""
    if encrypted:
        key, o_value, u_value = _security(user_password, owner_password, permissions)

    def stream(number: int, data: bytes) -> bytes:
        if encrypted:
            object_key = hashlib.md5(key + struct.pack("<i", number)[:3] + struct.pack("<i", 0)[:2]).digest()[:10]
            data = _rc4(object_key, data)
        return b"<< /Length " + str(len(data)).encode() + b" >>\nstream\n" + data + b"\nendstream"

    # 1 目录，2 页树，3、4 两种字体，5 乱码字体的对照表，之后每页一个页对象、一个内容；最后是加密字典
    first_page = 6
    objects: list[bytes] = [b"", b"", b"", b"", b""]
    kids = []
    for index, page_lines in enumerate(pages):
        page_number, content_number = first_page + 2 * index, first_page + 2 * index + 1
        kids.append(f"{page_number} 0 R")
        font = "F2" if index in dingbat_pages else "F1"
        text_ops = " T* ".join(f"({_escape(line)}) Tj" for line in page_lines)
        content = f"BT /{font} 12 Tf 16 TL 72 720 Td {text_ops} ET".encode("latin-1") if page_lines else b""
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
            + f"{content_number} 0 R".encode()
            + b" /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>"
        )
        objects.append(stream(content_number, content))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()
    objects[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    # 真实的乱码页就是这样：字体的对照表（ToUnicode）把字码对到装饰符号那一块，读出来是一串符号
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 5 0 R >>"
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CMapName /Garbled def\n/CMapType 2 def\n"
        b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n1 beginbfrange\n<61> <7A> <2701>\nendbfrange\n"
        b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    )
    objects[4] = stream(5, cmap)
    if encrypted:
        objects.append(
            b"<< /Filter /Standard /V 1 /R 2 /O <"
            + o_value.hex().encode()
            + b"> /U <"
            + u_value.hex().encode()
            + b"> /P "
            + str(permissions).encode()
            + b" >>"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"<< /Size {len(objects) + 1} /Root 1 0 R /ID [<{_FILE_ID.hex()}> <{_FILE_ID.hex()}>]"
    if encrypted:
        trailer += f" /Encrypt {len(objects)} 0 R"
    out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
