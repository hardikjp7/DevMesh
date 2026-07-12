"""
pdf_report.py

fpdf2-based PDF renderer for the DevMesh review report — the real
replacement for the "Placeholder for the real fpdf2-based PDF report"
noted in report_generator.py's docstring.

WHY fpdf2 AND NOT THE report.html.j2 / WeasyPrint TEMPLATE:
report.html.j2 (built earlier, still useful as a design reference — see
the color/layout constants mirrored below) relies on full CSS: flexbox,
CSS custom properties, gradients, border-radius. fpdf2 does not render
HTML/CSS — it's a positioned-drawing API (rects, cells, text). WeasyPrint
would understand that template directly, but pulls in Cairo/Pango/GDK-
PixBuf, native libraries that are a real risk to get working on Windows
ARM64. fpdf2 is pure Python — no native deps — which is why it's the
right call for this project's "ARM64 everywhere" constraint, even though
it means re-implementing the layout by hand instead of reusing the
template.

CONTRACT:
    render_pdf(decisions, commit_info=None, output_path=..., model_name=..., device_name=...) -> str

    decisions: List[FindingDecision] — see review_session.py. Each one has:
        .decision            "approved" | "false_positive"
        .dev_comment          developer's FP reasoning (only when false_positive)
        .llm_verdict           "MAINTAINED" | "WITHDRAWN" | "PARTIALLY_VALID" | None
        .llm_reiteration       model's reiteration text | None
        .reviewed_finding.finding.file / .line / .severity / .description / .fix / .id

    commit_info: Optional[CommitInfo] — .short_id / .message / .author_name /
                 .author_email / .timestamp

Returns the absolute path to the written PDF.
"""

import os
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

from fpdf import FPDF

# ---------------------------------------------------------------------------
# Design tokens — mirrors report.html.j2's :root CSS variables, converted to
# RGB tuples for fpdf2's set_fill_color / set_text_color / set_draw_color.
# Keep these two files' palettes in sync if either changes.
# ---------------------------------------------------------------------------

BG = (11, 15, 20)
PANEL = (19, 26, 34)
PANEL_BORDER = (35, 46, 58)
TEXT = (230, 237, 243)
TEXT_DIM = (124, 139, 155)
TEXT_FAINT = (75, 88, 103)
ACCENT = (91, 141, 239)
# Mirrors App.js SEVERITY_STYLES hex values, converted to RGB tuples.
CRITICAL = (239, 68, 68)    # #EF4444
MAJOR = (234, 88, 12)       # #EA580C
MINOR = (234, 179, 8)       # #EAB308
SUGGESTION = (22, 163, 74)  # #16A34A
NEEDS_REVIEW = (199, 125, 255)

SEVERITY_COLOR = {"CRITICAL": CRITICAL, "MAJOR": MAJOR, "MINOR": MINOR, "SUGGESTION": SUGGESTION}
SEVERITY_ORDER = ["CRITICAL", "MAJOR", "MINOR", "SUGGESTION"]
SEVERITY_LABEL = {"CRITICAL": "Critical", "MAJOR": "Major", "MINOR": "Minor", "SUGGESTION": "Suggestion"}

VERDICT_TO_STATUS = {
    "WITHDRAWN": "false_positive",
    "MAINTAINED": "needs_review",
    "PARTIALLY_VALID": "needs_review",
}

VERDICT_LABEL = {
    "MAINTAINED": "AI maintains this finding",
    "WITHDRAWN": "AI agrees this is a false positive",
    "PARTIALLY_VALID": "AI partially agrees",
}

STATUS_BADGE = {
    "approved": ("Approved", SUGGESTION),
    "false_positive": ("False Positive", TEXT_DIM),
    "needs_review": ("Needs Review", NEEDS_REVIEW),
    "pending_review": ("Pending Model Review", MAJOR),
}

MARGIN = 15
PAGE_W = 210
CONTENT_W = PAGE_W - 2 * MARGIN

_CHAR_MAP = {
    "\u2014": "-",
    "\u2013": "-",
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...",
    "\u2022": "-",
    "\u00a0": " ",
}


def _sanitize(text) -> str:
    if text is None:
        return ""
    text = str(text)
    for src, dst in _CHAR_MAP.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _rrect(pdf, x, y, w, h, style="D"):
    """Rounded rectangle — radius is 5% of the shorter side (clamped so
    tiny elements like badges don't over-round and large panels don't
    under-round). Falls back to a square rect on older fpdf2 that doesn't
    support round_corners."""
    radius = max(0.8, min(4.0, min(w, h) * 0.05))
    try:
        pdf.rect(x, y, w, h, style=style, round_corners=True, corner_radius=radius)
    except TypeError:
        pdf.rect(x, y, w, h, style=style)


def _decision_status(d) -> str:
    if d.decision == "approved":
        return "approved"
    if d.decision == "false_positive":
        if not d.llm_verdict:
            return "pending_review"
        return VERDICT_TO_STATUS.get(d.llm_verdict, "needs_review")
    return "pending_review"


class DevMeshReportPDF(FPDF):
    def header(self):
        self.set_fill_color(*BG)
        self.rect(0, 0, self.w, self.h, style="F")

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*TEXT_FAINT)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _status_strip(pdf: DevMeshReportPDF, commit_info, counts: dict, model_name: str, device_name: str):
    x, y = MARGIN, MARGIN
    
    # Process commit message and handle truncation
    commit_short = _sanitize(commit_info.short_id) if commit_info else "-"
    commit_msg = _sanitize(commit_info.message) if commit_info else "-"
    has_commit_msg = commit_info and commit_msg and commit_msg != "-"
    
    if has_commit_msg and len(commit_msg) > 100:
        commit_msg = commit_msg[:100] + "..."

    # Dynamically size the panel based on whether we need the 3rd row for the commit message
    strip_h = 58 if has_commit_msg else 46
    
    pdf.set_draw_color(*PANEL_BORDER)
    pdf.set_fill_color(*PANEL)
    _rrect(pdf, x, y, CONTENT_W, strip_h, style="DF")

    # Top severity color strip
    band_w = CONTENT_W / 4
    for i, color in enumerate([CRITICAL, MAJOR, MINOR, SUGGESTION]):
        pdf.set_fill_color(*color)
        pdf.rect(x + i * band_w, y, band_w, 1.2, style="F")

    # Title
    pdf.set_xy(x + 8, y + 6)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*TEXT)
    pdf.cell(0, 7, "DevMesh - On-Device Review Report")

    generated_at = datetime.now().strftime("%b %d, %Y - %H:%M")

    # --- Grid Layout Parameters ---
    col1_x = x + 8
    col2_x = x + 8 + (CONTENT_W / 2)

    def _draw_meta(label_str, val_str, cx, cy_label, cy_val, val_font="Courier", val_style="", val_size=9.5, val_color=TEXT):
        pdf.set_xy(cx, cy_label)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*ACCENT)
        pdf.cell(60, 3, label_str)
        
        pdf.set_xy(cx, cy_val)
        pdf.set_font(val_font, val_style, val_size)
        pdf.set_text_color(*val_color)
        pdf.cell(60, 5, val_str)

    # --- Row 1 ---
    _draw_meta("COMMIT_ID", commit_short[:28], col1_x, y + 17, y + 21.5)
    _draw_meta("GENERATED_ON", generated_at, col2_x, y + 17, y + 21.5)

    # --- Row 2 ---
    _draw_meta("MODEL_USED", str(_sanitize(model_name))[:35], col1_x, y + 29, y + 33.5)
    _draw_meta("DEVICE_USED", str(_sanitize(device_name))[:35], col2_x, y + 29, y + 33.5)

    # --- Row 3 (Commit Message) ---
    if has_commit_msg:
        _draw_meta(
            "COMMIT_MESSAGE", 
            f'"{commit_msg}"', 
            col1_x, y + 41, y + 45.5, 
            val_font="Helvetica", val_style="I", val_size=8.5, val_color=TEXT_DIM
        )

    # --- Pill Badges Section ---
    pill_y = y + strip_h + 6
    pill_gap = 4
    pill_w = (CONTENT_W - 3 * pill_gap) / 4
    pill_h = 16
    pill_defs = [
        ("Critical", counts.get("CRITICAL", 0), CRITICAL),
        ("Major", counts.get("MAJOR", 0), MAJOR),
        ("Minor", counts.get("MINOR", 0), MINOR),
        ("Suggestion", counts.get("SUGGESTION", 0), SUGGESTION),
    ]
    
    for i, (label, n, color) in enumerate(pill_defs):
        px = x + i * (pill_w + pill_gap)
        pdf.set_draw_color(*PANEL_BORDER)
        pdf.set_fill_color(*PANEL)
        _rrect(pdf, px, pill_y, pill_w, pill_h, style="DF")
        pdf.set_fill_color(*color)
        pdf.rect(px, pill_y, 1.2, pill_h, style="F")

        pdf.set_xy(px + 6, pill_y + 2)
        pdf.set_font("Courier", "B", 15)
        pdf.set_text_color(*color)
        pdf.cell(pill_w - 8, 8, str(n))
        pdf.set_xy(px + 6, pill_y + 10)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*TEXT_DIM)
        pdf.cell(pill_w - 8, 4, label.upper())

    return pill_y + pill_h + 10

def _section_heading(pdf: DevMeshReportPDF, y: float, severity: str, count: int) -> float:
    x = MARGIN
    color = SEVERITY_COLOR[severity]
    pdf.set_fill_color(*color)
    pdf.ellipse(x, y + 1.2, 3, 3, style="F")

    pdf.set_font("Helvetica", "B", 10)
    label = SEVERITY_LABEL[severity].upper()
    label_w = pdf.get_string_width(label)

    pdf.set_xy(x + 6, y)
    pdf.set_text_color(*TEXT)
    pdf.cell(label_w, 5, label)

    pdf.set_xy(x + 6 + label_w + 2.5, y)
    pdf.set_font("Courier", "", 9)
    pdf.set_text_color(*TEXT_FAINT)
    pdf.cell(0, 5, f"({count})")

    line_y = y + 7
    pdf.set_draw_color(*PANEL_BORDER)
    pdf.line(x, line_y, x + CONTENT_W, line_y)
    return line_y + 5


def _measure_finding_height(pdf: DevMeshReportPDF, d) -> float:
    f = d.reviewed_finding.finding
    pdf.set_font("Helvetica", "", 9.5)
    desc_lines = pdf.multi_cell(CONTENT_W - 16, 5, _sanitize(f.description), dry_run=True, output="LINES")
    h = 6 + 4.5 + len(desc_lines) * 5 + 2

    if f.fix:
        pdf.set_font("Helvetica", "", 8.5)
        fix_lines = pdf.multi_cell(CONTENT_W - 16, 4.5, _sanitize(f.fix), dry_run=True, output="LINES")
        h += 4 + len(fix_lines) * 4.5 + 3

    status = _decision_status(d)
    if status in ("false_positive", "needs_review", "pending_review") and d.decision == "false_positive":
        pdf.set_font("Helvetica", "", 8.5)
        dev_lines = pdf.multi_cell(CONTENT_W - 16, 4.5, _sanitize(d.dev_comment or ""), dry_run=True, output="LINES")
        h += 4 + len(dev_lines) * 4.5 + 6
        if d.llm_reiteration:
            model_lines = pdf.multi_cell(CONTENT_W - 16, 4.5, _sanitize(d.llm_reiteration), dry_run=True, output="LINES")
            h += 4 + len(model_lines) * 4.5 + 4

    return h + 12


def _render_finding(pdf: DevMeshReportPDF, y: float, d) -> float:
    f = d.reviewed_finding.finding
    status = _decision_status(d)
    badge_label, badge_color = STATUS_BADGE[status]

    card_h = _measure_finding_height(pdf, d)
    if y + card_h > pdf.h - 22:
        pdf.add_page()
        y = MARGIN

    x = MARGIN
    pdf.set_draw_color(*PANEL_BORDER)
    pdf.set_fill_color(*PANEL)
    _rrect(pdf, x, y, CONTENT_W, card_h, style="DF")

    cy = y + 6
    pdf.set_xy(x + 8, cy)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(*TEXT_FAINT)
    file_label = "File: "
    pdf.cell(pdf.get_string_width(file_label), 5, file_label)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_text_color(*TEXT_DIM)
    pdf.cell(0, 5, _sanitize(f"{f.file} | Line: {f.line}"))

    pdf.set_font("Helvetica", "B", 7.5)
    badge_w = pdf.get_string_width(badge_label) + 8
    pdf.set_xy(x + CONTENT_W - badge_w - 8, cy - 1)
    pdf.set_fill_color(*badge_color)
    pdf.set_text_color(*BG)
    _rrect(pdf, x + CONTENT_W - badge_w - 8, cy - 1, badge_w, 5, style="F")
    pdf.set_xy(x + CONTENT_W - badge_w - 8, cy - 1)
    pdf.cell(badge_w, 5, badge_label, align="C")

    cy += 7
    pdf.set_xy(x + 8, cy)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(*TEXT_FAINT)
    pdf.cell(0, 4.5, "Issue")
    cy += 4.5
    pdf.set_xy(x + 8, cy)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(CONTENT_W - 16, 5, _sanitize(f.description))
    cy = pdf.get_y() + 2

    if f.fix:
        pdf.set_xy(x + 8, cy)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*TEXT_FAINT)
        pdf.cell(0, 4, "Recommended Fix")
        cy += 4
        pdf.set_xy(x + 8, cy)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*TEXT)
        pdf.multi_cell(CONTENT_W - 16, 4.5, _sanitize(f.fix))
        cy = pdf.get_y() + 3

    if d.decision == "false_positive":
        pdf.set_draw_color(*PANEL_BORDER)
        pdf.line(x + 8, cy, x + CONTENT_W - 8, cy)
        cy += 4

        pdf.set_xy(x + 8, cy)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*TEXT_FAINT)
        pdf.cell(0, 4, "Developer Comment")
        cy += 4
        pdf.set_xy(x + 8, cy)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*TEXT)
        pdf.multi_cell(CONTENT_W - 16, 4.5, _sanitize(d.dev_comment or "(no reason recorded)"))
        cy = pdf.get_y() + 2

        if d.llm_reiteration:
            pdf.set_xy(x + 8, cy)
            pdf.set_font("Helvetica", "B", 7.5)
            pdf.set_text_color(*TEXT_FAINT)
            pdf.cell(0, 4, "Model Response")
            cy += 4
            pdf.set_xy(x + 8, cy)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(*TEXT)
            pdf.multi_cell(CONTENT_W - 16, 4.5, _sanitize(d.llm_reiteration))
            cy = pdf.get_y()

    return y + card_h + 4


def _empty_state(pdf: DevMeshReportPDF, y: float):
    pdf.set_draw_color(*PANEL_BORDER)
    _rrect(pdf, MARGIN, y, CONTENT_W, 30, style="D")
    pdf.set_xy(MARGIN, y + 10)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*SUGGESTION)
    pdf.cell(CONTENT_W, 6, "No issues found", align="C")
    pdf.set_xy(MARGIN, y + 17)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*TEXT_DIM)
    pdf.cell(CONTENT_W, 5, "This diff is clean.", align="C")


def _footer_privacy_line(pdf: DevMeshReportPDF):
    y = pdf.h - 26
    pdf.set_draw_color(*PANEL_BORDER)
    pdf.line(MARGIN, y, MARGIN + CONTENT_W, y)
    pdf.set_xy(MARGIN, y + 4)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*TEXT_FAINT)
    pdf.cell(CONTENT_W / 2, 5, "DevMesh - Qualcomm Multiverse Hackathon")
    pdf.set_xy(MARGIN + CONTENT_W / 2, y + 4)
    pdf.set_text_color(*TEXT_DIM)
    pdf.cell(CONTENT_W / 2, 5, "Generated entirely on-device. No code left this machine.", align="R")


def render_pdf(
    decisions: List,
    commit_info=None,
    output_path: str = "devmesh_report.pdf",
    model_name: str = "Qwen-4B-Instruct-2507"
    "",
    device_name: str = "OnePlus 15",
) -> str:
    pdf = DevMeshReportPDF(format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    counts = defaultdict(int)
    by_severity = defaultdict(list)
    for d in decisions:
        sev = d.reviewed_finding.finding.severity
        counts[sev] += 1
        by_severity[sev].append(d)

    y = _status_strip(pdf, commit_info, counts, model_name, device_name)

    if not decisions:
        _empty_state(pdf, y)
    else:
        for severity in SEVERITY_ORDER:
            findings_in_section = by_severity.get(severity, [])
            if not findings_in_section:
                continue
            if y > pdf.h - 40:
                pdf.add_page()
                y = MARGIN
            y = _section_heading(pdf, y, severity, len(findings_in_section))
            for d in findings_in_section:
                y = _render_finding(pdf, y, d)
            y += 6

    _footer_privacy_line(pdf)

    out = os.path.abspath(output_path)
    pdf.output(out)
    return out

