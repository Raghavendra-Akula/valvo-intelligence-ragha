"""Shared dashboard-style equity-research PDF template.

Used by stltech_report.py and mtar_report.py.

Registers DejaVuSans so the rupee glyph (₹) and other Unicode symbols
render correctly. Uses NextPageTemplate to switch from the cover to a
slim header on subsequent pages.
"""
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

# ─────────────────────────────────────────────────────────────────
# Font registration — DejaVuSans supports ₹ and curly quotes
# ─────────────────────────────────────────────────────────────────
import os as _os
_FONT_REG = "DejaVu"
_FONT_BLD = "DejaVu-Bold"


def _find_font(filename):
    """Resolve a TTF across Linux distros, macOS, and a repo-bundled dir."""
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/{filename}",
        f"/usr/share/fonts/dejavu/{filename}",
        f"/usr/share/fonts/TTF/{filename}",
        f"/Library/Fonts/{filename}",
        _os.path.join(_os.path.dirname(__file__), "fonts", filename),
    ]
    for path in candidates:
        if _os.path.isfile(path):
            return path
    raise FileNotFoundError(
        f"Could not locate {filename}. Install fonts-dejavu-core in the image, "
        "or drop the .ttf into Backend/services/deep_research/fonts/."
    )


pdfmetrics.registerFont(TTFont(_FONT_REG, _find_font("DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont(_FONT_BLD, _find_font("DejaVuSans-Bold.ttf")))

from reportlab.pdfbase.pdfmetrics import registerFontFamily
registerFontFamily(
    _FONT_REG,
    normal=_FONT_REG, bold=_FONT_BLD,
    italic=_FONT_REG, boldItalic=_FONT_BLD,  # no italic file shipped — fall back
)

REG = _FONT_REG
BLD = _FONT_BLD

# ─────────────────────────────────────────────────────────────────
# Brand palette
# ─────────────────────────────────────────────────────────────────
NAVY       = colors.HexColor("#0a2540")
INK        = colors.HexColor("#0f172a")
BODY       = colors.HexColor("#1f2937")
MUTE       = colors.HexColor("#64748b")
LINE_C     = colors.HexColor("#e2e8f0")
ACCENT     = colors.HexColor("#FF9F0A")
ACCENT_BG  = colors.HexColor("#fff7ed")
ACCENT_DK  = colors.HexColor("#ea580c")
POS        = colors.HexColor("#16a34a")
POS_BG     = colors.HexColor("#dcfce7")
NEG        = colors.HexColor("#dc2626")
NEG_BG     = colors.HexColor("#fef2f2")
INFO       = colors.HexColor("#2563eb")
INFO_BG    = colors.HexColor("#eff6ff")
NEUTRAL_BG = colors.HexColor("#f1f5f9")

# ─────────────────────────────────────────────────────────────────
# Paragraph styles  (all rebased to DejaVu)
# ─────────────────────────────────────────────────────────────────
_S = getSampleStyleSheet()


def _style(name, **kw):
    kw.setdefault("fontName", REG)
    return ParagraphStyle(name, parent=_S["Normal"], **kw)


COVER_TITLE = _style("CT", fontName=BLD, fontSize=32, leading=36, textColor=colors.white, alignment=TA_LEFT)
COVER_SUB   = _style("CS", fontName=REG, fontSize=11, leading=14, textColor=colors.HexColor("#cbd5e1"), alignment=TA_LEFT)
COVER_HERO  = _style("CH", fontName=BLD, fontSize=84, leading=92, textColor=ACCENT, alignment=TA_CENTER)
COVER_HSUB  = _style("CHS", fontName=REG, fontSize=10, leading=12, textColor=MUTE, alignment=TA_CENTER)

H1 = _style("H1", fontName=BLD, fontSize=17, leading=21, textColor=NAVY, spaceBefore=12, spaceAfter=8)
H2 = _style("H2", fontName=BLD, fontSize=12.5, leading=16, textColor=NAVY, spaceBefore=10, spaceAfter=4)
EYEBROW = _style("EY", fontName=BLD, fontSize=8.5, leading=10, textColor=ACCENT_DK, alignment=TA_LEFT, spaceAfter=2)
LEAD = _style("LE", fontName=REG, fontSize=10.5, leading=14.5, textColor=BODY, alignment=TA_JUSTIFY, spaceAfter=6)
P    = _style("P",  fontName=REG, fontSize=9.5, leading=13, textColor=BODY, alignment=TA_JUSTIFY, spaceAfter=3)
BUL  = _style("BU", fontName=REG, fontSize=9.5, leading=13, textColor=BODY, leftIndent=12, bulletIndent=2, spaceAfter=3)

TILE_NUM_BIG = _style("TNB", fontName=BLD, fontSize=24, leading=28, textColor=NAVY, alignment=TA_CENTER)
TILE_LBL = _style("TL", fontName=REG, fontSize=8, leading=10, textColor=MUTE, alignment=TA_CENTER, spaceBefore=3)
TILE_SUB = _style("TS", fontName=REG, fontSize=7.5, leading=9, textColor=MUTE, alignment=TA_CENTER)

PILLAR_BADGE = _style("PB",  fontName=BLD, fontSize=28, leading=32, textColor=colors.white, alignment=TA_CENTER)
PILLAR_TITLE = _style("PT",  fontName=BLD, fontSize=12.5, leading=16, textColor=NAVY, alignment=TA_LEFT)
PILLAR_KICK  = _style("PK",  fontName=BLD, fontSize=8, leading=10, textColor=ACCENT_DK, alignment=TA_LEFT, spaceAfter=2)
PILLAR_LEAD  = _style("PL",  fontName=BLD, fontSize=10, leading=13, textColor=INK, alignment=TA_LEFT, spaceAfter=4)
PILLAR_BUL   = _style("PBU", fontName=REG, fontSize=9.5, leading=13, textColor=BODY, leftIndent=10, bulletIndent=0, spaceAfter=3, alignment=TA_JUSTIFY)

TL_DATE = _style("TLD", fontName=BLD, fontSize=8.5, leading=11, textColor=NAVY, alignment=TA_RIGHT)
TL_DATE_DIM = _style("TLDD", fontName=REG, fontSize=7.5, leading=10, textColor=MUTE, alignment=TA_RIGHT)
TL_EVT  = _style("TLE", fontName=BLD, fontSize=10, leading=13, textColor=INK, alignment=TA_LEFT)
TL_DESC = _style("TLDe", fontName=REG, fontSize=8.5, leading=11, textColor=MUTE, alignment=TA_LEFT, spaceAfter=2)

SRC = _style("SRC", fontName=REG, fontSize=8.2, leading=11, textColor=INFO, leftIndent=10, spaceAfter=1)


# ─────────────────────────────────────────────────────────────────
# Page decoration
# ─────────────────────────────────────────────────────────────────
def _draw_footer(canvas, doc):
    canvas.saveState()
    w, _ = A4
    canvas.setStrokeColor(LINE_C); canvas.setLineWidth(0.4)
    canvas.line(15 * mm, 13 * mm, w - 15 * mm, 13 * mm)
    canvas.setFillColor(MUTE); canvas.setFont(REG, 7.5)
    canvas.drawString(15 * mm, 9 * mm,
                      "VALVO INTELLIGENCE  ·  Past-Winners Equity Research  ·  Generated from internal data + public news. Not investment advice.")
    canvas.drawRightString(w - 15 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def make_body_header(company_name, ticker, window_str):
    def _draw(canvas, doc):
        canvas.saveState(); w, h = A4
        canvas.setFillColor(NAVY); canvas.rect(0, h - 12 * mm, w, 12 * mm, stroke=0, fill=1)
        canvas.setFillColor(ACCENT); canvas.rect(0, h - 12.6 * mm, w, 0.6 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white); canvas.setFont(BLD, 8.5)
        canvas.drawString(15 * mm, h - 7.7 * mm, f"{company_name}  ·  {ticker}")
        canvas.setFillColor(colors.HexColor("#cbd5e1")); canvas.setFont(REG, 8.5)
        canvas.drawRightString(w - 15 * mm, h - 7.7 * mm, f"Equity Research  ·  {window_str}")
        canvas.restoreState(); _draw_footer(canvas, doc)
    return _draw


def make_cover_decoration(rank_label):
    def _draw(canvas, doc):
        canvas.saveState(); w, h = A4
        band = 110 * mm
        canvas.setFillColor(NAVY); canvas.rect(0, h - band, w, band, stroke=0, fill=1)
        canvas.setFillColor(ACCENT); canvas.rect(0, h - band - 4 * mm, w, 4 * mm, stroke=0, fill=1)
        canvas.setFillColor(ACCENT); canvas.setFont(BLD, 8.5)
        canvas.drawString(15 * mm, h - 14 * mm, rank_label)
        _draw_footer(canvas, doc); canvas.restoreState()
    return _draw


# ─────────────────────────────────────────────────────────────────
# Visual building blocks
# ─────────────────────────────────────────────────────────────────
def sp(h=6):
    return Spacer(1, h)


def kpi_tile(value, label, sub=None, accent=NAVY, bg=NEUTRAL_BG, value_fontsize=22):
    vs = ParagraphStyle("v", parent=TILE_NUM_BIG, textColor=accent, fontSize=value_fontsize)
    cells = [[Paragraph(value, vs)], [Paragraph(label.upper(), TILE_LBL)]]
    rh = [1.15 * cm, 0.55 * cm]
    if sub:
        cells.append([Paragraph(sub, TILE_SUB)]); rh.append(0.4 * cm)
    t = Table(cells, rowHeights=rh)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEABOVE", (0, 0), (-1, 0), 2.5, accent),
    ]))
    return t


def kpi_row(tiles):
    n = len(tiles)
    cw = 18 * cm / n
    row = Table([tiles], colWidths=[cw] * n, rowHeights=[2.6 * cm])
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return row


def tldr_card(text):
    inner = Table([[Paragraph("THE BOTTOM LINE", EYEBROW)],
                   [Paragraph(text, LEAD)]], colWidths=[16.7 * cm])
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_BG),
        ("LINEBEFORE", (0, 0), (-1, -1), 4, ACCENT),
    ]))
    return inner


def pillar_card(num, title, kicker, lead, bullets, color):
    badge = Table([[Paragraph(str(num), PILLAR_BADGE)]],
                  colWidths=[1.6 * cm], rowHeights=[1.6 * cm])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    head = Table([[badge, [Paragraph(kicker, PILLAR_KICK), Paragraph(title, PILLAR_TITLE)]]],
                 colWidths=[1.6 * cm, 14.7 * cm])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    rows = [[head], [Paragraph(lead, PILLAR_LEAD)]]
    for b in bullets:
        rows.append([Paragraph(f"<font color='{color.hexval()}'>•</font>  {b}", PILLAR_BUL)])
    card = Table(rows, colWidths=[16.7 * cm])
    card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("LINEBEFORE", (0, 0), (-1, -1), 3, color),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, LINE_C),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, LINE_C),
        ("LINEAFTER", (0, 0), (-1, -1), 0.5, LINE_C),
    ]))
    return card


def timeline_card(events):
    rows = []
    for d_top, d_bot, evt, desc, color in events:
        date_stack = [Paragraph(d_top, TL_DATE), Paragraph(d_bot, TL_DATE_DIM)]
        right = [Paragraph(evt, TL_EVT)]
        if desc:
            right.append(Paragraph(desc, TL_DESC))
        rows.append([date_stack, "", right])
    t = Table(rows, colWidths=[2.6 * cm, 0.9 * cm, 13.2 * cm])
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 8),
        ("LEFTPADDING", (2, 0), (2, -1), 8),
        ("RIGHTPADDING", (2, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBEFORE", (2, 0), (2, -1), 2, NAVY),
    ]
    for i, (_, _, _, _, color) in enumerate(events):
        cmds.append(("BACKGROUND", (1, i), (1, i), color))
    t.setStyle(TableStyle(cmds))
    return t


def comparison_panel(left_label, left_value, right_label, right_value,
                     change_label, change_value, change_color):
    def cell(label, value, val_color, bg, accent, val_fontsize=30):
        c = Table([
            [Paragraph(label.upper(), TILE_LBL)],
            [Paragraph(value, ParagraphStyle("v", fontName=BLD,
                       fontSize=val_fontsize, leading=val_fontsize + 4, textColor=val_color,
                       alignment=TA_CENTER))],
        ], colWidths=[5.3 * cm], rowHeights=[0.6 * cm, 1.6 * cm])
        c.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LINEABOVE", (0, 0), (-1, 0), 2.5, accent),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
        ]))
        return c
    left = cell(left_label, left_value, MUTE, NEUTRAL_BG, MUTE)
    right = cell(right_label, right_value, NAVY, NEUTRAL_BG, NAVY)
    change = cell(change_label, change_value, change_color, ACCENT_BG, ACCENT)
    arrow = Paragraph("<font size='28' color='" + ACCENT.hexval() + "'><b>→</b></font>",
                      ParagraphStyle("a", alignment=TA_CENTER, fontName=REG))
    panel = Table([[left, arrow, right, arrow, change]],
                  colWidths=[5.3 * cm, 0.7 * cm, 5.3 * cm, 0.7 * cm, 5.3 * cm],
                  rowHeights=[2.4 * cm])
    panel.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return panel


def quarterly_chart(quarters, values, title):
    drawing = Drawing(440, 160)
    bc = VerticalBarChart()
    bc.x = 40; bc.y = 30; bc.height = 100; bc.width = 380
    bc.data = [values]
    bc.categoryAxis.categoryNames = quarters
    bc.categoryAxis.labels.fontName = REG
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = MUTE
    bc.valueAxis.labels.fontName = REG
    bc.valueAxis.labels.fontSize = 7.5
    bc.valueAxis.labels.fillColor = MUTE
    bc.bars[0].fillColor = NAVY
    bc.bars[0].strokeColor = NAVY
    bc.barWidth = 18
    bc.valueAxis.gridStrokeColor = LINE_C
    bc.valueAxis.gridStrokeWidth = 0.4
    bc.valueAxis.visibleGrid = True
    drawing.add(bc)
    drawing.add(String(40, 145, title, fontName=BLD, fontSize=10, fillColor=NAVY))
    return drawing


def section_table(headers, rows, col_widths, header_bg=NAVY):
    head = [Paragraph(f"<font color='white'><b>{h}</b></font>",
                     _style("h", fontName=BLD, fontSize=8.5, alignment=TA_LEFT))
            for h in headers]
    data = [head]
    for r in rows:
        data.append([Paragraph(str(c), _style(f"c{i}", fontName=REG, fontSize=9,
                                              leading=12, textColor=BODY))
                     for i, c in enumerate(r)])
    t = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE_C),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, NEUTRAL_BG]),
    ]))
    return t


def risk_card(label, severity, detail):
    sev_color = {"HIGH": NEG, "MEDIUM": ACCENT_DK, "LOW": POS}[severity]
    sev_bg = {"HIGH": NEG_BG, "MEDIUM": ACCENT_BG, "LOW": POS_BG}[severity]
    pill = Table([[Paragraph(f"<font color='{sev_color.hexval()}'><b>{severity}</b></font>",
                            _style("p", fontName=BLD, fontSize=8.5, alignment=TA_CENTER))]],
                 colWidths=[2.0 * cm], rowHeights=[0.6 * cm])
    pill.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), sev_bg),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    label_p = Paragraph(f"<b>{label}</b>", _style("rl", fontName=BLD, fontSize=10, leading=13, textColor=INK))
    detail_p = Paragraph(detail, _style("rd", fontName=REG, fontSize=9, leading=12, textColor=BODY))
    inner = Table([[pill, [label_p, detail_p]]], colWidths=[2.0 * cm, 14.4 * cm])
    inner.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    wrap = Table([[inner]], colWidths=[16.7 * cm])
    wrap.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("LINEBEFORE", (0, 0), (-1, -1), 3, sev_color),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, LINE_C),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, LINE_C),
        ("LINEAFTER", (0, 0), (-1, -1), 0.5, LINE_C),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return wrap


def make_doc(out_path, title, company_name, ticker, window_str, rank_label):
    """Set up a BaseDocTemplate with cover + body templates."""
    doc = BaseDocTemplate(
        out_path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=title, author="Valvo Intelligence",
    )
    cover_frame = Frame(15 * mm, 18 * mm, A4[0] - 30 * mm, A4[1] - 36 * mm, id="cover")
    body_frame  = Frame(15 * mm, 18 * mm, A4[0] - 30 * mm, A4[1] - 38 * mm, id="body")
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame],
                     onPage=make_cover_decoration(rank_label)),
        PageTemplate(id="body",  frames=[body_frame],
                     onPage=make_body_header(company_name, ticker, window_str)),
    ])
    return doc


_COLOR_MAP = {
    "navy": NAVY, "info": INFO, "pos": POS, "neg": NEG,
    "accent": ACCENT, "accent_dk": ACCENT_DK, "mute": MUTE,
}
_BG_FOR_COLOR = {
    "navy": NEUTRAL_BG, "info": INFO_BG, "pos": POS_BG, "neg": NEG_BG,
    "accent": ACCENT_BG, "accent_dk": ACCENT_BG, "mute": NEUTRAL_BG,
}


def _resolve_color(name, default=NAVY):
    return _COLOR_MAP.get((name or "").lower(), default)


def _resolve_bg(name, default=NEUTRAL_BG):
    return _BG_FOR_COLOR.get((name or "").lower(), default)


def cover_block(company_name_html, identity_line, tags, hero_value, hero_caption, kpi_tiles, tldr_text):
    """Returns the list of flowables that make up the cover page (before NextPageTemplate)."""
    out = [sp(8)]
    cover_title = Table([
        [Paragraph(company_name_html, COVER_TITLE)],
        [Paragraph(identity_line, COVER_SUB)],
    ], colWidths=[18 * cm])
    cover_title.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    out.append(cover_title)
    out.append(sp(8))

    tag_cells = [Paragraph(t, _style("tg", fontName=BLD, fontSize=8,
                                     textColor=colors.white, alignment=TA_CENTER))
                 for t in tags]
    tag_table = Table([tag_cells], colWidths=[5 * cm] * len(tags), rowHeights=[0.7 * cm])
    bg_styles = []
    for i in range(len(tags)):
        bg_styles.append(("BACKGROUND", (i, 0), (i, 0), colors.HexColor("#1e3a5f")))
    tag_table.setStyle(TableStyle(bg_styles + [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    out.append(tag_table)
    out.append(sp(28))

    out.append(Paragraph(hero_value, COVER_HERO))
    out.append(Paragraph(hero_caption, COVER_HSUB))
    out.append(sp(36))

    cw = 18 * cm / len(kpi_tiles)
    tile_row = Table([kpi_tiles], colWidths=[cw] * len(kpi_tiles), rowHeights=[2.6 * cm])
    tile_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    out.append(tile_row)
    out.append(sp(20))
    out.append(tldr_card(tldr_text))
    return out


# ─────────────────────────────────────────────────────────────────
# Data-driven renderer (used by /api/deep-research/report/<id>/pdf)
# ─────────────────────────────────────────────────────────────────

def _auto_value_fontsize(value):
    """Tile width is fixed (~4.5 cm at 4-up); pick a font size that won't wrap.

    Calibrated against the manually-tuned values used in the MTAR / STLTECH
    standalone scripts. Anything 5 chars or shorter gets the headline size;
    long strings like '₹9,330 Cr' (9 chars) drop to 18 so they fit in one line.
    """
    n = len(str(value or ""))
    if n <= 5:
        return 24
    if n <= 6:
        return 22
    if n <= 7:
        return 20
    if n <= 8:
        return 18
    if n <= 10:
        return 16
    return 14


def _kpi_from_dict(t):
    accent = _resolve_color(t.get("color"), NAVY)
    bg = _resolve_bg(t.get("color"), NEUTRAL_BG)
    value = str(t.get("value", ""))
    fs = t.get("font_size")
    return kpi_tile(
        value=value,
        label=str(t.get("label", "")),
        sub=t.get("sub"),
        accent=accent,
        bg=bg,
        value_fontsize=int(fs) if fs else _auto_value_fontsize(value),
    )


def _safe(s, default=""):
    return str(s) if s not in (None, "") else default


def render_report(out_path, data):
    """Render a polished equity-research PDF from a structured dict.

    The dict shape is documented in services/deep_research/prompts.py
    (see PDF_JSON_SCHEMA). Sections gracefully degrade to a "Data gap"
    note when a field is missing — rather than crash — so an imperfect
    model output still produces a usable PDF.
    """
    header = data.get("header") or {}
    company_name = _safe(header.get("company_name"), "Equity Research")
    ticker = _safe(header.get("ticker"), "")
    rank_label = _safe(header.get("rank_label"), "EQUITY RESEARCH  ·  DEEP RESEARCH")
    window_str = _safe(data.get("window"), "")
    title = f"{company_name} — Equity Research (Valvo Intelligence)"

    doc = make_doc(
        out_path=out_path,
        title=title,
        company_name=company_name,
        ticker=ticker,
        window_str=window_str or "—",
        rank_label=rank_label,
    )
    story = []

    # ─── COVER ───────────────────────────────────────────────────
    hero = data.get("hero") or {}
    cover_kpis = data.get("cover_kpis") or []
    tags = [t for t in (header.get("tags") or []) if t]
    if not tags:
        tags = [_safe(header.get("sector"), "EQUITY"), "DEEP RESEARCH"]
    tags = tags[:4]

    identity_line = _safe(header.get("identity_line"))
    if not identity_line:
        bits = [b for b in [
            ticker,
            _safe(header.get("exchange_codes")),
            (f"ISIN {header.get('isin')}" if header.get("isin") else ""),
        ] if b]
        identity_line = "  ·  ".join(bits) or "—"

    story.extend(cover_block(
        company_name_html=company_name,
        identity_line=identity_line,
        tags=tags,
        hero_value=_safe(hero.get("value"), "—"),
        hero_caption=_safe(hero.get("caption")),
        kpi_tiles=[_kpi_from_dict(t) for t in (cover_kpis[:4] or [
            {"value": "—", "label": "Start"},
            {"value": "—", "label": "End"},
            {"value": "—", "label": "Alpha"},
            {"value": "—", "label": "Mcap"},
        ])],
        tldr_text=_safe(data.get("tldr_html"), "Data gap: no bottom-line summary produced."),
    ))
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    # ─── PAGE 2 — DASHBOARD ─────────────────────────────────────
    dash = data.get("dashboard") or {}
    if dash:
        story.append(Paragraph("AT-A-GLANCE", EYEBROW))
        story.append(Paragraph(_safe(dash.get("headline"), "Move Decomposition"), H1))
        if dash.get("lead"):
            story.append(Paragraph(dash["lead"], LEAD))
            story.append(sp(6))
        for row in (dash.get("kpi_rows") or [])[:3]:
            tiles = [_kpi_from_dict(t) for t in row[:4]]
            if tiles:
                story.append(kpi_row(tiles))
                story.append(sp(6))
        snap_rows = dash.get("snapshot_rows") or []
        if snap_rows:
            story.append(sp(8))
            story.append(Paragraph("Snapshot", H2))
            story.append(section_table(
                headers=["Metric", "Value"],
                rows=snap_rows,
                col_widths=[7 * 28.35, 10 * 28.35],
            ))
        if dash.get("snapshot_footer_html"):
            story.append(sp(8))
            story.append(Paragraph(dash["snapshot_footer_html"], P))
        story.append(PageBreak())

    # ─── PAGE 3 — THREE PILLARS ─────────────────────────────────
    pillars = data.get("pillars") or []
    if pillars:
        story.append(Paragraph("WHY IT MOVED", EYEBROW))
        story.append(Paragraph("The Catalyst Trio", H1))
        if data.get("pillars_lead"):
            story.append(Paragraph(data["pillars_lead"], LEAD))
        story.append(sp(6))
        for i, p in enumerate(pillars[:3], start=1):
            story.append(pillar_card(
                num=p.get("num", i),
                kicker=_safe(p.get("kicker"), ""),
                title=_safe(p.get("title"), "Pillar"),
                color=_resolve_color(p.get("color"), INFO),
                lead=_safe(p.get("lead"), ""),
                bullets=[str(b) for b in (p.get("bullets") or [])][:6],
            ))
            story.append(sp(8))
        story.append(PageBreak())

    # ─── PAGE 4 — TIMELINE ──────────────────────────────────────
    timeline = data.get("timeline") or []
    if timeline:
        story.append(Paragraph("CHRONOLOGY", EYEBROW))
        story.append(Paragraph(_safe(data.get("timeline_headline"), "In-Window Catalyst Timeline"), H1))
        if data.get("timeline_lead"):
            story.append(Paragraph(data["timeline_lead"], LEAD))
        story.append(sp(6))

        legend = data.get("timeline_legend") or []
        if legend:
            cells = []
            for entry in legend[:4]:
                col = _resolve_color(entry.get("color"), NAVY)
                cells.append(Paragraph(
                    f"<font color='{col.hexval()}'>■</font> <b>{_safe(entry.get('label'))}</b>",
                    _style(f"lg{len(cells)}", fontName=REG, fontSize=8.5, textColor=BODY),
                ))
            cw_each = 16.5 / max(len(cells), 1)
            leg = Table([cells], colWidths=[cw_each * cm] * len(cells))
            leg.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(leg)
            story.append(sp(6))

        events = []
        for e in timeline[:14]:
            events.append((
                _safe(e.get("date_top"), "—"),
                _safe(e.get("date_bot"), ""),
                _safe(e.get("event"), ""),
                _safe(e.get("desc"), ""),
                _resolve_color(e.get("color"), NAVY),
            ))
        story.append(timeline_card(events))
        story.append(PageBreak())

    # ─── PAGE 5 — QUARTERLY ─────────────────────────────────────
    qtr = data.get("quarterly") or {}
    if qtr:
        story.append(Paragraph("THE EARNINGS PICTURE", EYEBROW))
        story.append(Paragraph(_safe(qtr.get("headline"), "Quarterly Trajectory"), H1))
        if qtr.get("lead"):
            story.append(Paragraph(qtr["lead"], LEAD))
            story.append(sp(6))

        chart = qtr.get("chart") or {}
        labels = chart.get("labels") or []
        values = chart.get("values") or []
        if labels and values and len(labels) == len(values):
            try:
                vfloat = [float(v) for v in values]
                story.append(quarterly_chart(labels, vfloat,
                                             _safe(chart.get("title"), "Quarterly")))
                story.append(sp(6))
            except (TypeError, ValueError):
                pass

        headers = qtr.get("table_headers") or []
        rows = qtr.get("table_rows") or []
        if headers and rows:
            n = len(headers)
            tot = 17.0
            col_widths = [tot / n * 28.35] * n
            story.append(section_table(headers=headers, rows=rows, col_widths=col_widths))
            story.append(sp(6))

        if qtr.get("footer_html"):
            story.append(Paragraph(qtr["footer_html"], P))
        story.append(PageBreak())

    # ─── PAGE 6 — RE-RATING + RISKS ─────────────────────────────
    rerate = data.get("rerating") or {}
    has_rerate = rerate.get("left_value") and rerate.get("right_value")
    risks = data.get("risks") or []
    if has_rerate or risks:
        if has_rerate:
            story.append(Paragraph("VALUATION", EYEBROW))
            story.append(Paragraph(_safe(rerate.get("headline"), "The Re-rating Math"), H1))
            if rerate.get("lead"):
                story.append(Paragraph(rerate["lead"], LEAD))
                story.append(sp(8))
            story.append(comparison_panel(
                left_label=_safe(rerate.get("left_label"), "Start"),
                left_value=_safe(rerate.get("left_value"), "—"),
                right_label=_safe(rerate.get("right_label"), "End"),
                right_value=_safe(rerate.get("right_value"), "—"),
                change_label=_safe(rerate.get("change_label"), "Change"),
                change_value=_safe(rerate.get("change_value"), "—"),
                change_color=_resolve_color(rerate.get("change_color"), POS),
            ))
            story.append(sp(10))
            if rerate.get("footer_html"):
                story.append(Paragraph(rerate["footer_html"], P))
                story.append(sp(12))

        if risks:
            story.append(Paragraph("RISKS", EYEBROW))
            story.append(Paragraph(_safe(data.get("risks_headline"), "What's Priced In"), H1))
            story.append(sp(4))
            for r in risks[:6]:
                sev = (r.get("severity") or "MEDIUM").upper()
                if sev not in ("HIGH", "MEDIUM", "LOW"):
                    sev = "MEDIUM"
                story.append(risk_card(
                    label=_safe(r.get("label"), "Risk"),
                    severity=sev,
                    detail=_safe(r.get("detail"), ""),
                ))
                story.append(sp(4))
        story.append(PageBreak())

    # ─── PAGE 7 — APPENDIX ──────────────────────────────────────
    gaps = data.get("data_gaps") or []
    sources = data.get("sources") or []
    if gaps or sources:
        story.append(Paragraph("APPENDIX", EYEBROW))
        if gaps:
            story.append(Paragraph("Data Gaps", H1))
            story.append(Paragraph("Things the analyst flagged as missing or unverifiable:", LEAD))
            for g in gaps[:8]:
                story.append(Paragraph(f"•  {g}", BUL))
            story.append(sp(12))

        if sources:
            story.append(Paragraph("Sources", H1))
            for s in sources[:24]:
                title_s = _safe(s.get("title")) or _safe(s.get("url"))
                url = _safe(s.get("url"))
                if not url:
                    continue
                story.append(Paragraph(
                    f'<link href="{url}" color="#1e40af">•  {title_s}</link>', SRC,
                ))

    doc.build(story)
    return out_path
