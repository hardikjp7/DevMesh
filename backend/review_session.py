"""
review_session.py

In-memory state for a single review run, bridging run_review.py (which
produces findings) and ws_broadcaster.py (which receives mobile decisions
asynchronously on a different thread's event loop).

WHY THIS EXISTS
---------------
The false-positive -> LLM reiteration flow needs two things findings alone
don't carry:
  1. A stable ID so a mobile decision message can reference "which finding"
     without re-sending the full finding text back over the wire.
  2. The original diff hunk text, so a reiteration prompt can give the LLM
     real code context again — but the diff must NEVER be sent to mobile
     (that would break the "zero code leaves the machine" story). It's kept
     here, server-side (AI PC) only, and only read back out when a
     false-positive reiteration call is made.

SCOPE (matches the rest of the pre-venue skeleton's honesty about limits):
- Single global session, single review run at a time — same "one process,
  one run" scope already accepted for ws_broadcaster's server lifetime
  (Section 16 known-gaps). Not designed for concurrent/overlapping reviews.
- In-memory only — a process restart loses all decisions. Fine for a single
  demo run; would need persistence for anything longer-lived.
- Thread-safety: run_review.py populates this on the main thread before any
  broadcast happens; decisions arrive later on the asyncio event loop thread
  (ws_broadcaster.py). A simple lock guards the shared dicts since these two
  threads can both touch them.
"""

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from response_parser import Finding
from diff_extractor import CommitInfo
from devlog import get_logger

log = get_logger(__name__)

VALID_DECISIONS = {"approved", "false_positive"}


@dataclass
class ReviewedFinding:
    finding: Finding
    diff_text: str  # server-side only — never broadcast to mobile


@dataclass
class FindingDecision:
    reviewed_finding: ReviewedFinding
    decision: str  # "approved" | "false_positive"
    dev_comment: str = ""
    llm_reiteration: str = ""
    llm_verdict: str = ""  # "MAINTAINED" | "WITHDRAWN" | "PARTIALLY_VALID" | ""


class ReviewSession:
    def __init__(self):
        self._lock = threading.RLock()
        self._findings: Dict[str, ReviewedFinding] = {}
        self._decisions: Dict[str, FindingDecision] = {}
        self._counter = 0
        self._commit_info: Optional[CommitInfo] = None

    def start_new_session(self, commit_info: Optional[CommitInfo] = None) -> None:
        """Call once at the start of each run_review.py run — clears any
        previous run's state so decisions don't leak across commits, and
        records which commit this run covers (see CommitInfo)."""
        with self._lock:
            self._findings.clear()
            self._decisions.clear()
            self._counter = 0
            self._commit_info = commit_info
            log.info(f"New review session started (commit={commit_info.short_id if commit_info else 'N/A'}).")

    def get_commit_info(self) -> Optional[CommitInfo]:
        with self._lock:
            return self._commit_info

    def add(self, finding: Finding, diff_text: str) -> str:
        """
        Registers a finding, assigns it a stable id (mutates finding.id in
        place so the same object can be broadcast with its id included),
        and retains the diff_text server-side for later reiteration use.
        Returns the assigned id.
        """
        with self._lock:
            self._counter += 1
            finding_id = f"f{self._counter}"
            finding.id = finding_id
            self._findings[finding_id] = ReviewedFinding(finding=finding, diff_text=diff_text)
            log.info(f"Registered finding {finding_id}: [{finding.severity}] {finding.file}:{finding.line}")
            return finding_id

    def record_decision(self, finding_id: str, decision: str, comment: str = "") -> Optional[str]:
        """
        Records a decision from mobile. Returns an error message string if
        the request is invalid (unknown id / invalid decision value), or
        None on success. Deliberately returns rather than raises — this is
        called from the WS message handler, which should report bad
        requests back to the client rather than crash the connection.
        """
        with self._lock:
            if finding_id not in self._findings:
                return f"Unknown finding_id: {finding_id}"
            if decision not in VALID_DECISIONS:
                return f"Invalid decision '{decision}', must be one of {VALID_DECISIONS}"
            if decision == "false_positive" and not comment.strip():
                return "false_positive decisions require a non-empty comment"

            self._decisions[finding_id] = FindingDecision(
                reviewed_finding=self._findings[finding_id],
                decision=decision,
                dev_comment=comment.strip(),
            )
            log.info(f"Decision recorded: {finding_id} -> {decision}")
            return None

    def pending_ids(self) -> List[str]:
        with self._lock:
            return [fid for fid in self._findings if fid not in self._decisions]

    def all_decisions(self) -> List[FindingDecision]:
        with self._lock:
            return list(self._decisions.values())

    def get_decision(self, finding_id: str) -> Optional[FindingDecision]:
        with self._lock:
            return self._decisions.get(finding_id)

    def has_findings(self) -> bool:
        with self._lock:
            return bool(self._findings)


# Single global session — see SCOPE note above.
session = ReviewSession()
