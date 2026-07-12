"""
run_review.py

Orchestrator — the "does the pipeline work at all" script.

UPDATED for the false-positive reiteration flow (Section 20): findings are
now registered into review_session (which assigns each one a stable id and
retains its source diff, server-side only) BEFORE being broadcast, so
mobile-side decisions and the eventual reiteration pass have something to
reference. Report generation is no longer automatic at the end of this
script — it's now triggered by mobile sending {"type": "generate_report"}
once every finding has been marked approved/false_positive (see
ws_broadcaster.py + report_trigger.py). This script's job ends once findings
are broadcast; it just keeps the process (and WS server) alive after that.

UPDATED for structured logging (Section 20 — devlog.py): every phase is now
logged (not just printed) and persisted to logs/devmesh.log, with major
blocking steps (git subprocess calls, per-hunk LLM review) wrapped in
stage() so a hang anywhere shows up as a "STAGE START" with no matching
"STAGE END" instead of the terminal just going silent.
"""

import argparse
import time
from collections import defaultdict

from diff_extractor import get_last_commit_diff, get_diff, split_into_hunks, get_commit_info
from prompt_builder import build_prompt
from llm_client import review_hunk
from response_parser import parse_findings
from ws_broadcaster import broadcast_findings
from ws_broadcaster import _start_server_thread
import review_session
from devlog import get_logger, stage

log = get_logger(__name__)


def main():
    _start_server_thread()
    parser = argparse.ArgumentParser(description="DevMesh skeleton pipeline runner")
    parser.add_argument("--repo", default=".", help="Path to the git repo to review")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Review staged changes instead of the last commit",
    )
    args = parser.parse_args()

    try:
        with stage(log, "fetch_commit_info"):
            commit_info = get_commit_info(args.repo, staged=args.staged)
    except RuntimeError as e:
        log.error(f"{e}")
        return

    review_session.session.start_new_session(commit_info=commit_info)
    log.info(f"Reviewing commit {commit_info.short_id} by {commit_info.author_name}: "
              f"\"{commit_info.message}\"")

    log.info(f"Extracting diff from: {args.repo}")
    try:
        with stage(log, "extract_diff"):
            if args.staged:
                raw_diff = get_diff(args.repo, staged=True)
            else:
                raw_diff = get_last_commit_diff(args.repo)
    except RuntimeError as e:
        log.error(f"{e}")
        return

    hunks = split_into_hunks(raw_diff)
    log.info(f"Found {len(hunks)} hunk(s) to review")

    if not hunks:
        log.warning("No hunks found — nothing to review (empty diff, or no commits yet).")
        return

    all_findings = defaultdict(list)
    total_latency = 0.0

    for i, hunk in enumerate(hunks, start=1):
        log.info(f"Reviewing hunk {i}/{len(hunks)}: {hunk.file_path} (line {hunk.start_line})")
        prompt = build_prompt(hunk)

        start = time.time()
        with stage(log, f"llm_review(hunk {i}/{len(hunks)}, {hunk.file_path})"):
            result = review_hunk(prompt)
        elapsed = time.time() - start
        total_latency += elapsed

        findings = parse_findings(result.raw_output)

        # Register each finding into the session BEFORE broadcasting — this
        # assigns finding.id (mutated in place) and retains the hunk's diff
        # text server-side, so a later false-positive decision can trigger
        # a reiteration call with real code context. Order matters here:
        # broadcast_findings() below needs finding.id already set.
        for f in findings:
            review_session.session.add(f, hunk.diff_text)

        all_findings[hunk.file_path].extend(findings)

        log.info(f"    -> {len(findings)} finding(s), {elapsed:.2f}s")

    log.info(f"Total LLM latency: {total_latency:.2f}s across {len(hunks)} call(s), "
              f"average {total_latency / len(hunks):.2f}s/hunk")

    for file_path, findings in all_findings.items():
        if findings:
            broadcast_findings(findings, file_path)

    total_findings = sum(len(v) for v in all_findings.values())
    if total_findings:
        log.info(f"{total_findings} finding(s) broadcast. Waiting for mobile to mark "
                  f"each as approved/false_positive, then send 'generate_report'.")
    else:
        log.info("No findings — nothing for mobile to review.")

    print("\n[run_review] Keeping websocket alive. Press Enter to stop.")
    input()


if __name__ == "__main__":
    main()
