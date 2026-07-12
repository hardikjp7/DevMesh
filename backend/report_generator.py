"""
report_generator.py

Contract UPDATED for the false-positive reiteration flow (Section 20 work)
AND for the real fpdf2 PDF renderer (pdf_report.py) — no longer a stub.

generate_report() now writes TWO files from the same output_path (whatever
extension is passed in is stripped and treated as a base name):
  - "{base}.pdf" — the real, designed report (pdf_report.py). This is the
    file path returned and the one report_trigger.py sends back to mobile
    as report_ready.path.
  - "{base}.txt" — the original plain-text listing, kept as a lightweight
    debug/grep-able artifact alongside the PDF. Same content/grouping as
    before this change; nothing about it was altered.

FIX (this pass): pdf_report.render_pdf()'s model_name/device_name
parameters default to a hardcoded "Phi-4-Mini-Instruct" / "Snapdragon X
Elite (NPU)" — stale placeholders from before the model decision was
finalized (see project knowledge Section 19.4: Qwen3-4B-Instruct-2507 is
the actual primary model). generate_report() now pulls the ACTUALLY
active model/backend live from llm_client.py and passes it through
explicitly, so the report always reflects reality regardless of which
backend (ollama/qnn) or model is configured at run time — instead of
silently mislabeling every report with a model that isn't even running.

CONTRACT (unchanged — this is what mattered to keep stable):
  generate_report(decisions: List[FindingDecision], commit_info=None, output_path=...) -> str

  Each FindingDecision (see review_session.py) is either:
    - decision == "approved"       -> render the finding as-is
    - decision == "false_positive" -> render the finding, the developer's
                                       comment, and the LLM's reiteration
                                       (verdict + text)

Same pattern already used for llm_client.py (Ollama -> QNN) and
ws_broadcaster.py (print stub -> real WebSocket) — signature stays put,
report_trigger.py needed zero changes to call this.
"""

import os
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

from review_session import FindingDecision
from diff_extractor import CommitInfo
from pdf_report import render_pdf
from devlog import get_logger, stage
import llm_client

log = get_logger(__name__)

SEVERITY_ICONS = {
    "CRITICAL": "\U0001F534",   # 🔴
    "MAJOR": "\U0001F7E0",      # 🟠
    "MINOR": "\U0001F7E1",      # 🟡 (was WARNING's icon — reused)
    "SUGGESTION": "\U0001F7E2", # 🟢
}

VERDICT_LABELS = {
    "MAINTAINED": "AI maintains this finding",
    "WITHDRAWN": "AI agrees this is a false positive",
    "PARTIALLY_VALID": "AI partially agrees",
}


def _current_model_device_labels() -> tuple:
    """
    Reads llm_client's ACTUAL active configuration at call time (not a
    hardcoded guess) so the PDF's MODEL/DEVICE fields always match what
    really produced the findings in this report — including correctly
    labeling a mock-mode run as a mock run rather than implying a real
    model/device that never actually ran.
    """
    if llm_client.MOCK_MODE:
        return "Mock LLM (DEVMESH_MOCK_LLM=1)", "N/A (mock mode)"
    if llm_client.BACKEND == "qnn":
        return llm_client.QNN_MODEL_NAME, "Qwen-4B-Instruct-2507"
    return llm_client.MODEL_NAME, "OnePlus 15"


def _write_text_report(
    decisions: List[FindingDecision],
    commit_info: Optional[CommitInfo],
    output_path: str,
) -> str:
    """
    The original plain-text writer, unchanged in behavior — kept as a
    quick debug/grep-able artifact written alongside the PDF. Grouped by
    file (not by severity like the PDF) since that's what's most useful
    for a quick terminal scan while developing.
    """
    by_file = defaultdict(list)
    for d in decisions:
        by_file[d.reviewed_finding.finding.file].append(d)

    lines = []
    lines.append(f"DevMesh Review Report — {datetime.now().strftime('%B %d, %Y %H:%M')}")
    if commit_info:
        lines.append(f"Commit: {commit_info.short_id} — \"{commit_info.message}\"")
        lines.append(f"Author: {commit_info.author_name} <{commit_info.author_email}>")
        lines.append(f"Committed: {commit_info.timestamp}")
    lines.append("")

    if not decisions:
        lines.append("No issues found. Clean diff!")
    else:
        approved_count = sum(1 for d in decisions if d.decision == "approved")
        fp_count = sum(1 for d in decisions if d.decision == "false_positive")
        lines.append(f"{approved_count} approved finding(s), {fp_count} developer-flagged false positive(s)")
        lines.append("")

        for file_path, file_decisions in by_file.items():
            
            lines.append(f"--- {file_path} ---")
            for d in file_decisions:
                f = d.reviewed_finding.finding
                icon = SEVERITY_ICONS.get(f.severity, "-")
                lines.append(f"  {icon} [{f.severity}] line {f.line}: {f.description}")
                if f.fix:
                    lines.append(f"      Fix: {f.fix}")

                if d.decision == "false_positive":
                    lines.append(f"      Developer comment (flagged as false positive): {d.dev_comment}")
                    verdict_label = VERDICT_LABELS.get(d.llm_verdict, d.llm_verdict or "no reiteration recorded")
                    lines.append(f"      LLM reiteration [{verdict_label}]: {d.llm_reiteration}")
            lines.append("")

    report_text = "\n".join(lines)

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)

    return os.path.abspath(output_path)


def generate_report(
    decisions: List[FindingDecision],
    commit_info: Optional[CommitInfo] = None,
    output_path: str = "devmesh_report.txt",
) -> str:
    """
    Writes both the PDF (primary — this is the path returned) and a plain-
    text sibling (secondary/debug) for the given decisions.

    decisions: full list of FindingDecision from review_session.session —
    every finding that was part of the review run, each carrying its final
    mobile-side decision (and, for false positives, the LLM reiteration).
    commit_info: the commit this report covers — review_session.session.get_commit_info().
    output_path: any extension is stripped and treated as a base name, so
    existing call sites (report_trigger.py passes "devmesh_report_{short_id}.txt")
    keep working unmodified — both a .pdf and .txt get written next to it.
    """
    base, _ext = os.path.splitext(output_path)
    model_name, device_name = _current_model_device_labels()
    log.info(f"Generating report for {len(decisions)} finding(s) — model={model_name}, device={device_name}")

    with stage(log, "write_text_report"):
        txt_path = _write_text_report(decisions, commit_info, f"{base}.txt")

    with stage(log, "render_pdf_report"):
        pdf_path = render_pdf(
            decisions,
            commit_info=commit_info,
            output_path=f"{base}.pdf",
            model_name=model_name,
            device_name=device_name,
        )

    log.info(f"Wrote {pdf_path}")
    log.info(f"Wrote {txt_path} (debug copy)")

    return pdf_path
