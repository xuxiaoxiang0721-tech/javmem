"""读 PDF 的子进程（D79 第 11、12、15 条）。样例都是编的。

- 子进程拿父进程的 sys.path：依赖靠 PYTHONPATH、pip install --user 装的，子进程也找得到。起不来（找不到 pdfminer、
  javmem）单独报出来，不悄悄记成 unreadable。
- 一封信里有一个 PDF 读到点还没读完，这封信后面的 PDF 不再读，也记 timeout。
- 子进程自己设时限：父进程被杀了，它也会自己停。
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from pdfgen import make_fanout_pdf, make_pdf
from test_attachments import MAIL_FIXTURES, PATTERNS, read

from javmem import attachments as reader
from javmem.cli import main
from javmem.mailimport import NOTE_PDF_SKIPPED, import_mail, load_mail_config
from javmem.packet import parse_packet
from javmem.sources import load_registry

PLAIN = make_pdf(["Unit price 9.8 USD."])
ZONE = timezone(timedelta(hours=8))

_STUB = {
    "converter": "class TextConverter:\n    pass\n",
    "layout": "class LAParams:\n    pass\n",
    "pdfdocument": (
        "class PDFPasswordIncorrect(Exception):\n    pass\n\n\n"
        "class PDFEncryptionError(Exception):\n    pass\n\n\n"
        "class PDFDocument:\n    def __init__(self, *args, **kwargs):\n        raise PDFPasswordIncorrect()\n"
    ),
    "pdfinterp": "class PDFPageInterpreter:\n    pass\n\n\nclass PDFResourceManager:\n    pass\n",
    "pdfpage": "class PDFPage:\n    pass\n",
    "pdfparser": "class PDFParser:\n    def __init__(self, *args, **kwargs):\n        pass\n",
}


def fake_pdfminer(root: Path, *, broken: bool = False) -> str:
    """一个假的 pdfminer，放在父进程 sys.path 的最前面：子进程要是用父进程的路径，用的就是它。
    不 broken 的什么 PDF 都说要密码（encrypted）；broken 的一导入就出错，就像没装。"""
    package = root / "pdfminer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise ImportError('没装 pdfminer')\n" if broken else "", encoding="utf-8")
    if not broken:
        for name, body in _STUB.items():
            (package / f"{name}.py").write_text(body, encoding="utf-8")
    return str(root)


# ---------- D79 第 11 条：子进程拿父进程的 sys.path，起不来单独报 ----------


def test_the_reader_finds_what_the_parent_finds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(fake_pdfminer(tmp_path / "site"))
    result = read(PLAIN, "application/pdf")
    assert (result.state, result.reason) == ("none", "encrypted")  # 读到的是父进程路径上的那个


def test_a_reader_that_cannot_start_is_reported_not_recorded_as_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(fake_pdfminer(tmp_path / "site", broken=True))
    with pytest.raises(reader.PdfUnavailable) as caught:
        read(PLAIN, "application/pdf")
    assert "pdfminer" in str(caught.value)
    assert read(b"x", "text/plain").state == "full"  # 别的类型照读


def mail(path: Path, message_id: str, attachments: list[tuple[str, bytes]], *, minute: int = 12) -> Path:
    message = EmailMessage()
    message["From"] = "Tran Mai <tran.mai@example.org>"
    message["To"] = "李明 <li.ming@example.com>"
    message["Subject"] = f"Documents {message_id}"
    message["Date"] = f"Tue, 03 Nov 2026 09:{minute:02d}:00 +0700"
    message["Message-ID"] = f"<{message_id}@example.org>"
    message.set_content("Hi Ming,\n\nDocuments attached.\n")
    for name, data in attachments:
        if name.endswith(".pdf"):
            message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
        else:
            message.add_attachment(data, maintype="text", subtype="plain", filename=name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(message))
    return path


def imported(tmp_path: Path, *paths: Path):
    registry = load_registry(MAIL_FIXTURES / "sources.yaml")
    return import_mail(
        list(paths),
        source="mail-sales-a",
        registry=registry,
        config=load_mail_config(None, registry, "mail-sales-a"),
        patterns=PATTERNS,
        out_dir=tmp_path / "out",
        work_dir=tmp_path / "work",
        zone=ZONE,
        now=datetime(2026, 11, 10, 9, 0, tzinfo=ZONE),
    )


def test_mail_import_stops_and_says_so_when_the_reader_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = mail(tmp_path / "in" / "docs.eml", "docs-0001", [("quote.pdf", PLAIN)])
    monkeypatch.syspath_prepend(fake_pdfminer(tmp_path / "site", broken=True))
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(MAIL_FIXTURES / "sources.yaml"))
    args = ["mail-import", str(source), "--source", "mail-sales-a", "--work-dir", str(tmp_path / "work"), "--out", str(tmp_path / "out")]
    assert main(args) == 2
    output = capsys.readouterr()
    assert "读 PDF" in output.out + output.err
    assert not list((tmp_path / "out").rglob("*.md"))  # 一个包都没投
    assert not (tmp_path / "work" / "state" / "mail-sales-a.test.json").exists()  # 也没记成导过，装好再导一次就行
    monkeypatch.undo()
    monkeypatch.setenv("JAVMEM_SOURCES_FILE", str(MAIL_FIXTURES / "sources.yaml"))
    assert main(args) == 0
    [packet] = (tmp_path / "out" / "mail-sales-a").glob("*.md")
    [body] = parse_packet(packet.read_bytes(), filename=packet.name).attachments
    assert body.text == "full"


def test_a_broken_dependency_of_pdfminer_also_counts_as_cannot_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """审阅第 6 条：起不来的检查只导了 pdfminer.pdfparser；pdfminer.pdfdocument 一导入就要 cryptography，
    它坏了（Windows 上常见的“DLL load failed”）照样过了检查，每个 PDF 又悄悄成了 unreadable。"""
    root = tmp_path / "site"
    (root / "cryptography").mkdir(parents=True)
    (root / "cryptography" / "__init__.py").write_text("raise ImportError('DLL load failed')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    with pytest.raises(reader.PdfUnavailable):
        read(PLAIN, "application/pdf")


# ---------- D79 第 12 条：一封信里有一个 PDF 超时，后面的不再读 ----------


def test_after_one_pdf_of_a_mail_times_out_its_other_pdfs_are_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "PDF_SECONDS", 1)
    launched: list[int] = []
    real = reader.run_child  # 起读附件的子进程都走它（U3：Windows 上起来以后放进作业对象限内存）

    def counting(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        launched.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reader, "run_child", counting)
    slow = make_fanout_pdf(30)
    first = mail(tmp_path / "in" / "a.eml", "slow-0001", [("a.pdf", slow), ("b.pdf", slow), ("c.pdf", slow), ("note.txt", "备注".encode())])
    second = mail(tmp_path / "in" / "b.eml", "fine-0001", [("quote.pdf", PLAIN)], minute=20)
    started = time.monotonic()
    report = imported(tmp_path, first, second)
    assert len(launched) == 2  # 第 1 封只读了第一个 PDF，第 2 封照读
    assert time.monotonic() - started < 20
    assert report.attachments == {"none:timeout": 3, "full": 2}
    assert report.notes.get(NOTE_PDF_SKIPPED) == 1
    [packet] = (tmp_path / "out" / "mail-sales-a").glob("*.md")
    found = {body.name: (body.text, body.reason) for body in parse_packet(packet.read_bytes(), filename=packet.name).attachments}
    assert found == {
        "a.pdf": ("none", "timeout"),
        "b.pdf": ("none", "timeout"),
        "c.pdf": ("none", "timeout"),
        "note.txt": ("full", None),
        "quote.pdf": ("full", None),
    }


# ---------- D79 第 15 条：子进程自己设时限 ----------


def test_the_reader_stops_by_itself_when_nobody_kills_it() -> None:
    """父进程被杀了（计划任务到点、崩了），读 PDF 的子进程也要自己停。这里不靠 subprocess 的时限，看它自己退。"""
    started = time.monotonic()
    done = subprocess.run(
        reader.child_command(reader.MAX_TEXT_CHARS, 1.0), input=make_fanout_pdf(30), capture_output=True, timeout=60
    )
    assert time.monotonic() - started < 30
    assert reader.stopped_by_itself(done.returncode)


def test_a_reader_that_stopped_by_itself_counts_as_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def stopped(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, reader.STOPPED_BY_ITSELF, b"", b"")

    monkeypatch.setattr(reader, "run_child", stopped)
    result = read(PLAIN, "application/pdf")
    assert (result.state, result.reason) == ("none", "timeout")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有 RLIMIT_CPU，靠子进程里看时间的那个线程")
def test_the_reader_sets_a_cpu_limit_on_itself() -> None:
    code = "import resource\nfrom javmem.attachments import limit_child\nlimit_child(3)\nprint(resource.getrlimit(resource.RLIMIT_CPU))\n"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "(3, 4)"
