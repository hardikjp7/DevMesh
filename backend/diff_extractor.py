"""
diff_extractor.py

Extracts diff hunks from a git repo using --function-context so each hunk
carries the full enclosing function, not just +/- 3 lines of default context.

SKELETON VERSION (pre-orientation):
- No chunking yet (Section 7 chunking fallback deferred to Phase C).
- Assumes every hunk fits in the model's context window.
- One call per hunk downstream (this module just does the splitting).
"""

import subprocess
import re
from dataclasses import dataclass
from typing import List

# --- Section 19.4/19.5/Section 20 resolution --------------------------------
# PRIMARY MODEL (confirmed via direct AI Hub benchmark check on real
# Snapdragon X Elite CRD hardware — see Section 19.4 update): Qwen3-4B-
# Instruct-2507, running via GenieX/QAIRT (native NPU runtime), w4a16,
# confirmed 4096-token context tier: 1,301 tok/s prefill, 23.1 tok/s decode.
#
# Phi-4-Mini-Instruct was directly checked on the same hardware and does NOT
# reach a native-NPU 4096-token config: on Snapdragon X Elite CRD it only
# runs via GenieX/llama.cpp (community runtime), and its NPU-mode context
# length caps at 512 tokens — 4096 is only reachable by switching to
# CPU/GPU backend, not NPU. Demoted to documented fallback only.
#
# MAX_HUNK_TOKENS = AI_HUB_CONTEXT_CEILING - PROMPT_TEMPLATE_OVERHEAD, using
# Qwen3-4B-Instruct-2507's confirmed 4096-token native-NPU tier as the ceiling.
AI_HUB_CONTEXT_CEILING = 4096
# Measured via prompt_builder.measure_template_overhead() against an empty
# diff: ~225 tokens (char-based estimate). ~35% margin on top of that
# covers tokenizer variance and the char->token estimator's own error.
# Re-run measure_template_overhead() and update this if PROMPT_TEMPLATE's
# wording changes.
PROMPT_TEMPLATE_OVERHEAD = 300
MAX_HUNK_TOKENS = AI_HUB_CONTEXT_CEILING - PROMPT_TEMPLATE_OVERHEAD  # = 3796


def estimate_token_count(text: str) -> int:
    """
    Cheap, tokenizer-agnostic token estimate used for chunking decisions.

    We deliberately don't pull in a real tokenizer (tiktoken/sentencepiece)
    here: Phi-4-Mini-Instruct and Qwen3-4B use different tokenizers, and the
    AI Hub compiled binaries hide the tokenizer boundary from us anyway. The
    ~4 chars/token heuristic is a widely-used, good-enough approximation for
    English + code text, and MAX_HUNK_TOKENS already carries a safety margin
    to absorb this estimate's error. If chunking decisions turn out to be
    systematically wrong once real hardware is available, replace this with
    the actual tokenizer used by the compiled model.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


@dataclass
class Hunk:
    file_path: str
    start_line: int
    diff_text: str


@dataclass
class CommitInfo:
    """
    Metadata about the commit being reviewed. Fetched once per run_review.py
    run (see Section 20 — commit tracking) and threaded through
    review_session.py to: (1) the WebSocket payload sent to mobile, so it
    can group findings by commit instead of flatly appending everything
    from every run into one undifferentiated list, and (2) the final report,
    which needs to show which commit + which author it covers.
    """
    commit_id: str    # full hash, or "STAGED" for uncommitted staged changes
    short_id: str      # first 7 chars, or "STAGED"
    author_name: str
    author_email: str
    message: str        # first line of the commit message
    timestamp: str        # ISO 8601, or current time for staged changes


def get_commit_info(repo_path: str = ".", staged: bool = False) -> CommitInfo:
    """
    Fetches metadata for the commit being reviewed. Mirrors the staged/
    unstaged distinction already used by get_diff()/get_last_commit_diff():
    staged changes don't have a commit id yet (nothing's been committed),
    so that case returns a synthetic placeholder instead of erroring —
    downstream code (mobile grouping, report header) should treat
    commit_id == "STAGED" as "not yet committed" rather than a real hash.
    """
    if staged:
        from datetime import datetime, timezone
        return CommitInfo(
            commit_id="STAGED",
            short_id="STAGED",
            author_name="(uncommitted changes)",
            author_email="",
            message="(staged changes, not yet committed)",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # \x1f (unit separator) as the field delimiter avoids collisions with
    # anything that could plausibly appear in a commit message/author name.
    cmd = ["git", "-C", repo_path, "log", "-1", "--format=%H\x1f%an\x1f%ae\x1f%s\x1f%aI", "HEAD"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git log failed while fetching commit info: {result.stderr}")

    parts = result.stdout.strip().split("\x1f")
    if len(parts) != 5:
        raise RuntimeError(f"Unexpected git log output while parsing commit info: {result.stdout!r}")

    commit_id, author_name, author_email, message, timestamp = parts
    return CommitInfo(
        commit_id=commit_id,
        short_id=commit_id[:7],
        author_name=author_name,
        author_email=author_email,
        message=message,
        timestamp=timestamp,
    )


def get_diff(repo_path: str = ".", staged: bool = True) -> str:
    """
    Returns the raw git diff with full function context.

    staged=True  -> git diff --cached --function-context   (post-commit style: compares last commit)
    staged=False -> git diff --function-context             (unstaged working tree changes)
    """
    cmd = ["git", "-C", repo_path, "diff"]
    if staged:
        cmd.append("--cached")
    cmd.append("--function-context")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr}")
    return result.stdout


def get_last_commit_diff(repo_path: str = ".") -> str:
    """
    For the post-commit hook use case: diff of the most recent commit
    against its parent, with function context.
    """
    cmd = ["git", "-C", repo_path, "diff", "HEAD~1", "HEAD", "--function-context"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Likely the very first commit (no HEAD~1) — fall back to full diff of that commit
        cmd = ["git", "-C", repo_path, "show", "HEAD", "--function-context"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"git diff/show failed: {result.stderr}")
    return result.stdout


def split_into_hunks(raw_diff: str) -> List[Hunk]:
    """
    Splits a unified diff into per-file, per-hunk chunks.
    Each Hunk.diff_text is self-contained enough to send as one LLM call.

    No chunking of oversized hunks here (skeleton). That's a Phase C addition
    once MAX_HUNK_TOKENS is confirmed post-orientation.
    """
    hunks: List[Hunk] = []
    current_file = None
    current_hunk_lines: List[str] = []
    current_start_line = 0

    file_header_re = re.compile(r"^\+\+\+ b/(.+)$")
    hunk_header_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    def flush():
        if current_file and current_hunk_lines:
            diff_text = "\n".join(current_hunk_lines)
            token_count = estimate_token_count(diff_text)
            if token_count > MAX_HUNK_TOKENS:
                # Chunking itself (Section 7's logical-boundary splitting,
                # ~20-line overlap, 3-chunk cap) is still a Phase C item —
                # this only makes the oversized case visible instead of
                # silently sending a hunk the model will truncate/fail on.
                print(
                    f"[diff_extractor] WARNING: hunk in {current_file} "
                    f"(~{token_count} tokens) exceeds MAX_HUNK_TOKENS "
                    f"({MAX_HUNK_TOKENS}) — chunking not yet implemented, "
                    f"sending as-is. See Section 7/20 of project knowledge."
                )
            hunks.append(
                Hunk(
                    file_path=current_file,
                    start_line=current_start_line,
                    diff_text=diff_text,
                )
            )

    for line in raw_diff.splitlines():
        file_match = file_header_re.match(line)
        if file_match:
            flush()
            current_file = file_match.group(1)
            current_hunk_lines = []
            continue

        hunk_match = hunk_header_re.match(line)
        if hunk_match:
            flush()
            current_start_line = int(hunk_match.group(1))
            current_hunk_lines = [line]
            continue

        if current_hunk_lines:
            current_hunk_lines.append(line)

    flush()
    return hunks


if __name__ == "__main__":
    # Manual smoke test: run from inside a git repo with a commit already made
    diff = get_last_commit_diff(".")
    hunks = split_into_hunks(diff)
    print(f"Found {len(hunks)} hunk(s)")
    for h in hunks:
        print(f"--- {h.file_path} @ line {h.start_line} ---")
        print(h.diff_text[:300])
        print()
