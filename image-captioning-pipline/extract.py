"""
extract.py

Runs Azure AI Document Intelligence (layout model) against a local PDF/DOCX,
pulls out each detected figure (cropped image bytes), and builds a generic
"context window" for it: nearby heading path, caption if detected, and the
paragraphs immediately before/after it in reading order.

This does NOT assume a "step 1, step 2..." structure. It just captures
whatever heading/paragraph structure the document actually has, so the
same code works for tutorials, reports, manuals, etc.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient

load_dotenv()

CONTEXT_PARAGRAPHS_BEFORE = 2
CONTEXT_PARAGRAPHS_AFTER = 2

# Roles that count as "structure", not body text
HEADING_ROLES = {"title", "sectionHeading"}
SKIP_ROLES = {"pageHeader", "pageFooter", "pageNumber", "footnote"}


def get_client() -> DocumentIntelligenceClient:
    return DocumentIntelligenceClient(
        endpoint=os.environ["DOCINTEL_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["DOCINTEL_KEY"]),
    )


@dataclass
class FigureContext:
    figure_id: str
    page_number: Optional[int]
    caption: Optional[str]
    section_path: list = field(default_factory=list)   # e.g. ["Manufacturing Process", "Curing"]
    context_before: str = ""
    context_after: str = ""
    image_bytes: Optional[bytes] = None
    source_type: str = "figure"
    table_cells: Optional[list[dict]] = None
    bounding_box: Optional[tuple[float, float, float, float]] = None

def _canonical_heading(text: str) -> str:
    """Normalize continuation labels so continued headings match their originals."""
    text = text.strip()
    marker = r"(?:continued|cont\.?|cont['’]d)"

    text = re.sub(
        rf"\s*[\(\[\{{]\s*{marker}\s*[\)\]\}}]\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"(?:\s*[-–—:]\s*|\s+){marker}\s*\d*\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()

def get_table_image_bytes(file_path: str, result, ctx: FigureContext) -> bytes:
    """Crop a table from the original page so embedded images are preserved."""
    import subprocess
    import tempfile
    from pathlib import Path

    import fitz  # PyMuPDF

    if ctx.page_number is None or ctx.bounding_box is None:
        raise ValueError(f"Table {ctx.figure_id} has no page bounds to crop")

    source_path = Path(file_path)

    # PyMuPDF reads PDFs. Convert DOCX temporarily; LibreOffice must be installed.
    with tempfile.TemporaryDirectory() as temp_dir:
        if source_path.suffix.lower() == ".docx":
            subprocess.run(
                [
                    "soffice", "--headless", "--convert-to", "pdf",
                    "--outdir", temp_dir, str(source_path),
                ],
                check=True,
                capture_output=True,
            )
            pdf_path = Path(temp_dir) / f"{source_path.stem}.pdf"
        else:
            pdf_path = source_path

        document = fitz.open(pdf_path)
        try:
            page = document[ctx.page_number - 1]
            azure_page = result.pages[ctx.page_number - 1]
            x1, y1, x2, y2 = ctx.bounding_box

            # Scale Azure coordinates to the PDF page's coordinate system.
            rect = fitz.Rect(
                x1 * page.rect.width / azure_page.width,
                y1 * page.rect.height / azure_page.height,
                x2 * page.rect.width / azure_page.width,
                y2 * page.rect.height / azure_page.height,
            )
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(2, 2),
                clip=rect & page.rect,
                alpha=False,
            )
            return pixmap.tobytes("png")
        finally:
            document.close()

def analyze_document(file_path: str):
    """
    Sends the document to Document Intelligence and returns
    (result, operation_id) -- operation_id is needed later to fetch
    the cropped figure images.
    """
    client = get_client()
    with open(file_path, "rb") as f:
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            body=f,
            content_type="application/octet-stream",
            output=["figures"],
        )
    result = poller.result()
    operation_id = poller.details["operation_id"]
    return result, operation_id


def get_figure_image_bytes(result, operation_id: str, figure_id: str) -> bytes:
    """Fetches the cropped image for one figure as raw bytes."""
    client = get_client()
    stream = client.get_analyze_result_figure(
        model_id=result.model_id,
        result_id=operation_id,
        figure_id=figure_id,
    )
    return b"".join(stream)


def _offset_of(item) -> int:
    """Every paragraph/figure has spans; use the first span's offset to order things."""
    if item.spans:
        return item.spans[0].offset
    return 0


def _region_bounds(item):
    """Return (page, x1, y1, x2, y2), using cell bounds if needed."""
    regions = list(getattr(item, "bounding_regions", None) or [])
    if not regions:
        for cell in getattr(item, "cells", None) or []:
            regions.extend(getattr(cell, "bounding_regions", None) or [])

    parsed = []
    for region in regions:
        polygon = getattr(region, "polygon", None) or []

        if polygon and all(isinstance(v, (int, float)) for v in polygon):
            points = list(zip(polygon[::2], polygon[1::2]))
        else:
            points = []
            for point in polygon:
                if isinstance(point, dict):
                    x, y = point.get("x"), point.get("y")
                else:
                    x, y = getattr(point, "x", None), getattr(point, "y", None)
                if x is not None and y is not None:
                    points.append((x, y))

        if points:
            page = getattr(region, "page_number", None)
            xs, ys = zip(*points)
            parsed.append((page, min(xs), min(ys), max(xs), max(ys)))

    if not parsed:
        return None

    page = parsed[0][0]
    same_page = [r for r in parsed if r[0] == page]
    return (
        page,
        min(r[1] for r in same_page),
        min(r[2] for r in same_page),
        max(r[3] for r in same_page),
        max(r[4] for r in same_page),
    )

def _figure_is_inside_table(figure, table) -> bool:
    figure_bounds = _region_bounds(figure)
    table_bounds = _region_bounds(table)
    if not figure_bounds or not table_bounds:
        return False

    fp, fx1, fy1, fx2, fy2 = figure_bounds
    tp, tx1, ty1, tx2, ty2 = table_bounds
    center_x, center_y = (fx1 + fx2) / 2, (fy1 + fy2) / 2
    return fp == tp and tx1 <= center_x <= tx2 and ty1 <= center_y <= ty2


def _table_cells(table) -> list[dict]:
    return [
        {
            "row": getattr(cell, "row_index", 0),
            "column": getattr(cell, "column_index", 0),
            "row_span": getattr(cell, "row_span", 1),
            "column_span": getattr(cell, "column_span", 1),
            "content": (getattr(cell, "content", None) or "").strip(),
        }
        for cell in (getattr(table, "cells", None) or [])
    ]


def _render_table_image(table, cells: list[dict]) -> bytes:
    """Render extracted table cells as a PNG figure."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    row_count = max(1, getattr(table, "row_count", 0) or 0)
    column_count = max(1, getattr(table, "column_count", 0) or 0)
    grid = [["" for _ in range(column_count)] for _ in range(row_count)]

    for cell in cells:
        row, column = cell["row"], cell["column"]
        if 0 <= row < row_count and 0 <= column < column_count:
            grid[row][column] = cell["content"]

    cell_width = max(120, min(280, 1200 // column_count))
    padding = 8
    font = ImageFont.load_default()
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)

    def wrap(text):
        words, lines, line = text.split(), [], ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if line and draw.textlength(candidate, font=font) > cell_width - 2 * padding:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        return lines or [""]

    wrapped = [[wrap(text) for text in row] for row in grid]
    row_heights = [
        max(32, max(len(text_lines) for text_lines in row) * 16 + 2 * padding)
        for row in wrapped
    ]
    image = Image.new(
        "RGB",
        (cell_width * column_count + 1, sum(row_heights) + 1),
        "white",
    )
    draw = ImageDraw.Draw(image)

    y = 0
    for row_index, row in enumerate(wrapped):
        x = 0
        for text_lines in row:
            draw.rectangle(
                (x, y, x + cell_width, y + row_heights[row_index]),
                outline="#555555",
            )
            draw.multiline_text(
                (x + padding, y + padding),
                "\n".join(text_lines),
                fill="black",
                font=font,
                spacing=2,
            )
            x += cell_width
        y += row_heights[row_index]

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()

def build_figure_contexts(result) -> list[FigureContext]:
    """Build contexts for figures and tables in document reading order."""
    paragraphs = list(result.paragraphs or [])
    tables = list(result.tables or [])
    figures = list(result.figures or [])

    # Skip figures embedded in a table; the table will be represented as one image.
    figures = [
        figure for figure in figures
        if not any(_figure_is_inside_table(figure, table) for table in tables)
    ]

    timeline = (
        [("paragraph", p) for p in paragraphs]
        + [("figure", f) for f in figures]
        + [("table", t) for t in tables]
    )
    timeline.sort(key=lambda pair: _offset_of(pair[1]))

    contexts = []
    current_title = None
    current_heading = None
    recent_body_paragraphs = []
    table_number = 0

    for idx, (kind, item) in enumerate(timeline):
        if kind == "paragraph":
            role = getattr(item, "role", None)
            content = (item.content or "").strip()
            if not content:
                continue
            if role == "title":
                current_title = content
            elif role == "sectionHeading":
                if (
                    current_heading is None
                    or _canonical_heading(content) != _canonical_heading(current_heading)
                ):
                    current_heading = content
            elif role not in SKIP_ROLES:
                recent_body_paragraphs.append(content)
                recent_body_paragraphs = recent_body_paragraphs[-CONTEXT_PARAGRAPHS_BEFORE:]
            continue

        after_paragraphs = []
        for kind2, item2 in timeline[idx + 1:]:
            if kind2 != "paragraph":
                continue
            role2 = getattr(item2, "role", None)
            if role2 in HEADING_ROLES or role2 in SKIP_ROLES:
                break
            text = (item2.content or "").strip()
            if text:
                after_paragraphs.append(text)
            if len(after_paragraphs) >= CONTEXT_PARAGRAPHS_AFTER:
                break

        if kind == "figure":
            figure_bounds = _region_bounds(item)
            page_number = figure_bounds[0] if figure_bounds else None
            caption = getattr(item, "caption", None)
            caption_text = caption.content.strip() if caption and caption.content else None

            contexts.append(FigureContext(
                figure_id=item.id,
                page_number=page_number,
                caption=caption_text,
                section_path=[s for s in (current_title, current_heading) if s],
                context_before="\n".join(recent_body_paragraphs),
                context_after="\n".join(after_paragraphs),
                source_type="figure",
            ))
        else:  # table
            table_number += 1
            cells = _table_cells(item)
            bounds = _region_bounds(item)

            contexts.append(FigureContext(
                figure_id=f"table-{table_number}",
                page_number=bounds[0] if bounds else None,
                caption=None,
                section_path=[s for s in (current_title, current_heading) if s],
                context_before="\n".join(recent_body_paragraphs),
                context_after="\n".join(after_paragraphs),
                source_type="table",
                table_cells=cells,
                bounding_box=bounds[1:] if bounds else None,
            ))

    return contexts

if __name__ == "__main__":
    result, op_id = analyze_document("input_docs/sample.pdf")
    contexts = build_figure_contexts(result)
    print(f"Found {len(contexts)} figures")
    for ctx in contexts:
        print("---")
        print("section:", " > ".join(ctx.section_path))
        print("caption:", ctx.caption)
        print("before:", ctx.context_before[:120])
        print("after:", ctx.context_after[:120])