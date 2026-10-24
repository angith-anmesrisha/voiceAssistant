"""Text out of the user's documents, so JARVIS can read and summarise them.

Plain text formats are read directly; Word (.docx), Excel (.xlsx) and PowerPoint
(.pptx) are zip files of XML, read with the standard library; PDF needs the pypdf
package. The text is outside content: the assistant screens it like anything else read
from outside.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_TYPES = frozenset(
    {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml",
     ".ini", ".cfg", ".toml", ".html", ".htm", ".rtf", ".srt", ".py", ".js", ".ts", ".java",
     ".c", ".cpp", ".cs", ".go", ".rs", ".sql", ".css"}
)  # fmt: skip
# New text files JARVIS may create: never anything that runs.
WRITABLE_TYPES = frozenset({".txt", ".md", ".csv", ".json", ".log", ".xml", ".yaml", ".yml"})
MAX_READ_BYTES = 20_000_000
MAX_CHARS = 8000


class DocumentError(RuntimeError):
    pass


def _xml_text(data: bytes, tag: str) -> list[str]:
    root = ElementTree.fromstring(data)
    return [node.text or "" for node in root.iter() if node.tag.endswith(tag)]


def _docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for para in root.iter():
        if para.tag.endswith("}p"):
            text = "".join(t.text or "" for t in para.iter() if t.tag.endswith("}t"))
            if text.strip():
                paragraphs.append(text)
    return "\n".join(paragraphs)


def _pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.findall(r"\d+", n)[0]),
        )
        parts = []
        for i, name in enumerate(slides, 1):
            words = " ".join(t for t in _xml_text(archive.read(name), "}t") if t.strip())
            parts.append(f"Slide {i}: {words}")
    return "\n".join(parts)


def _xlsx(path: Path, max_rows: int = 200) -> str:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(t.text or "" for t in item.iter() if t.tag.endswith("}t")))
        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        out = []
        for sheet in sheets[:3]:
            out.append(f"[{sheet.rsplit('/', 1)[-1].removesuffix('.xml')}]")
            root = ElementTree.fromstring(archive.read(sheet))
            for count, row in enumerate(r for r in root.iter() if r.tag.endswith("}row")):
                if count >= max_rows:
                    out.append("…")
                    break
                cells = []
                for cell in row:
                    value = next((v.text for v in cell if v.tag.endswith("}v")), None)
                    if value is None:
                        inline = [t.text or "" for t in cell.iter() if t.tag.endswith("}t")]
                        value = "".join(inline)
                    elif cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    cells.append(value or "")
                out.append(" | ".join(cells))
    return "\n".join(out)


def _pdf(path: Path, max_pages: int = 40) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError("reading PDFs needs the pypdf package") from exc
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise DocumentError("that PDF is password-protected")
    pages = []
    for i, page in enumerate(reader.pages[:max_pages], 1):
        pages.append(f"[page {i}] {page.extract_text() or ''}")
    return "\n".join(pages)


def read_text(path: Path, limit: int = MAX_CHARS, *, pdf_pages: int = 40) -> tuple[str, bool]:
    """The document's text (cut to ``limit`` characters) and whether it was cut."""
    if not path.is_file():
        raise DocumentError(f"{path.name} isn't a file")
    if path.stat().st_size > MAX_READ_BYTES:
        raise DocumentError(f"{path.name} is too big to read")
    suffix = path.suffix.lower()
    try:
        if suffix in TEXT_TYPES:
            text = path.read_text(encoding="utf-8", errors="replace")
        elif suffix == ".docx":
            text = _docx(path)
        elif suffix == ".xlsx":
            text = _xlsx(path)
        elif suffix == ".pptx":
            text = _pptx(path)
        elif suffix == ".pdf":
            text = _pdf(path, pdf_pages)
        else:
            raise DocumentError(f"JARVIS can't read {suffix or 'that kind of'} files yet")
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise DocumentError(f"{path.name} looks damaged") from exc
    text = text.strip()
    return (text[:limit], True) if len(text) > limit else (text, False)
