"""Render thermal divider labels at the exact CUPS paper dimensions."""

from functools import lru_cache
from io import BytesIO

from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

FONT = "LibraryDividerBold"


@lru_cache(maxsize=1)
def _font():
    pdfmetrics.registerFont(TTFont(FONT, "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    return FONT


def _fit(text: str, width: float, height: float, font: str):
    words = text.split()
    breaks = [(text,)] + [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))]

    def fit(lines):
        longest = max(pdfmetrics.stringWidth(line, font, 1) for line in lines)
        size = min(width / max(longest, 0.01), height / (len(lines) * 1.12), 30)
        return size, lines

    return max(map(fit, breaks), key=lambda candidate: candidate[0])


def render(names: list[str], width_mm: float, height_mm: float) -> bytes:
    output = BytesIO()
    page_width, page_height = width_mm * mm, height_mm * mm
    width, height = max(page_width, page_height), min(page_width, page_height)
    canvas = Canvas(output, pagesize=(page_width, page_height), pageCompression=1)
    canvas.setTitle("Library of Tanglesprings Section Labels")
    font = _font()
    for name in names:
        if page_height > page_width:
            canvas.translate(page_width, 0)
            canvas.rotate(90)
        canvas.setLineWidth(0.65)
        for y in (2.5 * mm, height - 2.5 * mm):
            canvas.line(3 * mm, y, width - 3 * mm, y)
        size, lines = _fit(" ".join(name.upper().split()), width - 8 * mm, height - 7 * mm, font)
        canvas.setFont(font, size)
        for index, line in enumerate(reversed(lines)):
            baseline = height / 2 + (index - (len(lines) - 1) / 2) * size * 1.12 - size * 0.35
            canvas.drawCentredString(width / 2, baseline, line)
        canvas.showPage()
    canvas.save()
    return output.getvalue()
