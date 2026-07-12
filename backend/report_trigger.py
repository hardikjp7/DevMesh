"""
report_trigger.py

Orchestrates what happens when mobile sends {"type": "generate_report"}:
  1. Refuse if any finding is still undecided (mobile's UI is supposed to
     gate the button until everything's marked, but the backend shouldn't
     trust that blindly).
  2. For every false_positive decision, call the LLM again with the
     original diff + finding + the developer's comment (reiteration.py),
     and attach the verdict/text to that decision.
  3. Hand the full decision list to report_generator.generate_report() and
     return the resulting file path.

Called from ws_broadcaster.py's message handler, so this runs on the
asyncio event loop thread. The actual LLM calls (llm_client.review_hunk)
are synchronous/blocking (requests.post under the hood) — each one is run
via run_in_executor so a slow reiteration call doesn't freeze the WebSocket
server for other connected clients while it's in flight.
"""

import asyncio
import json
from typing import Dict, List

import review_session
from devlog import get_logger
from llm_client import review_hunk
from reiteration import build_reiteration_prompt, parse_reiteration
from report_generator import generate_report

log = get_logger(__name__)

# Maps backend's three-way reiteration verdict to mobile's coarser two-way
# vocabulary (see App.js's getDisplayStatus / STATUS_BADGE_STYLES).
# PARTIALLY_VALID intentionally maps to "needs_review", not "accepted" — a
# partial concession from the model still means a human should look at it,
# matching this codebase's existing "don't silently drop it" pattern (same
# reasoning as reiteration.parse_reiteration's unparseable -> MAINTAINED
# fallback).
VERDICT_MAP = {
    "WITHDRAWN": "accepted",
    "MAINTAINED": "needs_review",
    "PARTIALLY_VALID": "needs_review",
}


async def handle_request_verdicts(finding_ids: List[str], websocket) -> None:
    """
    Handles {"type": "request_verdicts", "finding_ids": [...]} — mobile's
    explicit "verifying false positives" step (App.js). Unlike
    handle_generate_report_request() below (which does reiteration
    silently as an internal step, only responding once at the very end),
    this runs reiteration for each requested finding and sends a
    {"type": "verdict"} message back to the requesting client AS EACH ONE
    COMPLETES — so mobile's per-card status updates progressively instead
    of the whole batch appearing to hang until the slowest one finishes.

    This is a UX nicety, not the only path that guarantees reiteration
    happens: handle_generate_report_request() independently re-checks for
    any false_positive decision still missing a reiteration and runs it if
    needed. If mobile ever skips straight to generate_report without
    calling this first, the report still comes out correct — just without
    the incremental "verifying" experience.
    """
    loop = asyncio.get_event_loop()

    for finding_id in finding_ids:
        d = review_session.session.get_decision(finding_id)
        if d is None:
            log.warning(f"request_verdicts: unknown or undecided finding_id "
                        f"{finding_id!r}, skipping")
            continue
        if d.decision != "false_positive":
            log.warning(f"request_verdicts: {finding_id} is not false_positive "
                        f"(it's {d.decision!r}), skipping")
            continue

        if not d.llm_reiteration:
            prompt = build_reiteration_prompt(d.reviewed_finding, d.dev_comment)
            log.info(f"Reiterating finding {finding_id} "
                     f"({d.reviewed_finding.finding.file}:{d.reviewed_finding.finding.line})...")
            result = await loop.run_in_executor(None, review_hunk, prompt)
            reiteration = parse_reiteration(result.raw_output)
            d.llm_verdict = reiteration.verdict
            d.llm_reiteration = reiteration.text
            log.info(f"    -> {reiteration.verdict}: {reiteration.text}")

        mobile_verdict = VERDICT_MAP.get(d.llm_verdict, "needs_review")
        await websocket.send(json.dumps({
            "type": "verdict",
            "finding_id": finding_id,
            "verdict": mobile_verdict,
            "note": d.llm_reiteration,
        }))


async def handle_generate_report_request() -> Dict:
    if not review_session.session.has_findings():
        return {"type": "report_error", "message": "No findings to report — has a review run yet?"}

    pending = review_session.session.pending_ids()
    if pending:
        return {
            "type": "report_error",
            "message": f"{len(pending)} finding(s) still undecided: {', '.join(pending)}",
        }

    decisions = review_session.session.all_decisions()
    log.info(f"report trigger decisions{decisions}")
    loop = asyncio.get_event_loop()

    for d in decisions:
        if d.decision != "false_positive" or d.llm_reiteration:
            continue  # approved findings need no reiteration; already-reiterated ones are skipped

        prompt = build_reiteration_prompt(d.reviewed_finding, d.dev_comment)
        log.info(f"Reiterating finding {d.reviewed_finding.finding.id} "
                 f"({d.reviewed_finding.finding.file}:{d.reviewed_finding.finding.line})...")

        # review_hunk() is a blocking call (requests.post under the hood) —
        # offload it so the event loop can keep serving other WS traffic.
        result = await loop.run_in_executor(None, review_hunk, prompt)
        reiteration = parse_reiteration(result.raw_output)

        d.llm_verdict = reiteration.verdict
        d.llm_reiteration = reiteration.text
        log.info(f"    -> {reiteration.verdict}: {reiteration.text}")

    try:
        commit_info = review_session.session.get_commit_info()
        output_path = (
            f"devmesh_report_{commit_info.short_id}.txt" if commit_info else "devmesh_report.txt"
        )
        report_path = generate_report(decisions, commit_info=commit_info, output_path=output_path)
    except Exception as e:  # noqa: BLE001 — surface any report-generation failure back to mobile
        return {"type": "report_error", "message": f"Report generation failed: {e}"}

    return {"type": "report_ready", "path": report_path}
