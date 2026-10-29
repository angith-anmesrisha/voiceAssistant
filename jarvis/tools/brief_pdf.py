"""Markdown to a PDF file with Qt's own text engine (no extra dependency).

``QTextDocument`` reads GitHub-flavoured Markdown (headings, bold, lists, check boxes,
tables, links), and ``QPdfWriter`` lays it out on A4 pages. It needs the running
QGuiApplication, so it is called on the GUI thread (the app's event loop).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMarginsF, QSizeF
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument

_CSS = """
body { color: #1d2430; }
h1 { color: #0b6f8a; }
h2 { color: #0b6f8a; margin-top: 14px; }
h3 { color: #22303f; margin-top: 10px; }
a { color: #0b6f8a; }
table { border-collapse: collapse; }
th { background-color: #e7f2f5; padding: 4px 8px; border: 1px solid #c9d6dc; }
td { padding: 4px 8px; border: 1px solid #d5dee2; }
"""


def markdown_pdf(markdown: str, path: Path, title: str = "") -> Path:
    """Write ``markdown`` as a PDF at ``path`` (which must not exist yet)."""
    source = QTextDocument()
    source.setMarkdown(markdown)
    document = QTextDocument()
    document.setDefaultStyleSheet(_CSS)
    font = QFont("Segoe UI")
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSizeF(10.0)
    document.setDefaultFont(font)
    document.setHtml(source.toHtml())
    if title:
        document.setMetaInformation(QTextDocument.MetaInformation.DocumentTitle, title)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):  # never overwrite; Qt then writes the file
        pass
    writer = QPdfWriter(str(path))
    writer.setTitle(title or path.stem)
    writer.setCreator("JARVIS")
    writer.setResolution(96)
    writer.setPageLayout(
        QPageLayout(
            QPageSize(QPageSize.PageSizeId.A4),
            QPageLayout.Orientation.Portrait,
            QMarginsF(15, 15, 15, 15),
            QPageLayout.Unit.Millimeter,
        )
    )
    area = writer.pageLayout().paintRectPixels(96)
    document.setPageSize(QSizeF(area.width(), area.height()))
    document.print_(writer)
    return path


__all__ = ["markdown_pdf"]
