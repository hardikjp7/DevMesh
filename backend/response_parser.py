"""
response_parser.py

Parses the LLM's raw text output (fixed format enforced by prompt_builder.py)
into structured Finding objects that both the WebSocket broadcaster and the
report generator can consume.

Expected line format:
[c] FILE:LINE — Issue description. Fix: recommended fix.

Section 20 update: hardened defensively for Qwen3-4B-Instruct-2507 (the
newly confirmed primary model, see Section 19.4) ahead of real local
testing via Ollama. Qwen-family instruct models are generally strong at
format-following, but two quirks are common enough across instruct models
in general to guard against pre-emptively:
  1. Wrapping the severity token in markdown bold (**CRITICAL**) even when
     told not to use markdown.
  2. Prefixing findings with a list marker ("1. ", "- ", "* ") despite being
     told not to use bullet points — easy to confuse with the bold-asterisk
     case above, so list-marker stripping happens first, as its own pass.
These are additive/backward-compatible: the original Phi-3 tolerant-parsing
behavior (missing brackets, "Recommended fix" phrasing, stray non-finding
lines) is unchanged.
"""

import re
from dataclasses import dataclass
from typing import List

# Strips a leading list marker ("1. ", "1) ", "- ", "* ", "• ") before the
# main matcher runs. Applied first and separately from FINDING_RE's own
# markdown handling so a line like "1. **CRITICAL** app.py:10 — ..." is
# still parsed correctly (marker strip -> bold strip -> normal match).
LIST_MARKER_RE = re.compile(r"^(?:\d+[.)]\s+|[-*•]\s+)")

# Tolerant header matcher: brackets around severity are optional, an
# optional markdown-bold wrapper (**CRITICAL**) around severity is also
# tolerated, and the separator between location and description can be an
# em-dash, hyphen, or colon. Smaller/quantized models (and even larger
# instruct models under strict formatting instructions) drift from the
# exact requested format fairly often, so this only requires the parts we
# actually need (severity, file, line, description) and treats everything
# else as best-effort.
FINDING_RE = re.compile(
    r"^\**\[?(CRITICAL|MAJOR|MINOR|SUGGESTION)\]?\**\s*[-:]?\s*"  # severity
    r"(.+?):(\d+)"                                               # file:line (file may contain spaces)
    r"\s*[—\-:]\s*"                                              # separator
    r"(.+)$",                                                     # rest of line
    re.IGNORECASE,
)

# Looks for a "Fix:" style marker anywhere in the description tail, with
# common phrasing variants a model might use instead of the exact word "Fix:".
FIX_SPLIT_RE = re.compile(
    r"\s*(?:Fix|Recommended fix|Suggested fix)\s*(?:is)?\s*[:\-]?\s+",
    re.IGNORECASE,
)


@dataclass
class Finding:
    severity: str
    file: str
    line: int
    description: str
    fix: str = ""
    id: str = ""  # assigned by review_session.py after parsing, not by the parser itself


def _strip_code_blocks(text: str) -> str:
    """Removes fenced ``` code blocks so they don't pollute line-by-line parsing."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def parse_findings(raw_output: str) -> List[Finding]:
    """
    Parses raw LLM text output into a list of Finding objects.

    Deliberately tolerant: the requested format is strict, but models
    drift from it (missing brackets, "Recommended fix" instead of "Fix:",
    occasional code blocks, markdown bullets/bold despite being told not
    to use markdown). This only requires enough structure to reliably
    locate severity + file + line, and takes a best-effort pass at
    splitting off the fix suggestion. Only the first line of a multi-line
    finding is captured — if a model wraps a finding across multiple
    lines, everything after the first line is dropped rather than causing
    a parse failure.
    """
    from devlog import get_logger
    _log = get_logger("devmesh.response_parser")

    findings: List[Finding] = []

    cleaned = _strip_code_blocks(raw_output)

    _log.info(f"[parse] Parsing {len(cleaned)} chars, {len(cleaned.splitlines())} lines")

    for line_num, line in enumerate(cleaned.splitlines(), 1):
        line = line.strip()
        if not line:
            continue

        original_line = line
        line = LIST_MARKER_RE.sub("", line)

        match = FINDING_RE.match(line)
        if not match:
            _log.debug(f"[parse] Line {line_num} NO MATCH: {original_line[:200]}")
            continue  # skip malformed/non-finding lines rather than raising

        severity, file_path, line_no, rest = match.groups()
        _log.info(f"[parse] Line {line_num} MATCHED: [{severity}] {file_path}:{line_no}")

        fix = ""
        fix_split = FIX_SPLIT_RE.split(rest, maxsplit=1)
        description = fix_split[0].strip()
        if len(fix_split) > 1:
            fix = fix_split[1].strip()

        findings.append(
            Finding(
                severity=severity.upper(),
                file=file_path,
                line=int(line_no),
                description=description,
                fix=fix,
            )
        )

    # Only honor NO_ISSUES_FOUND if no actual findings were parsed.
    # LLMs sometimes contradict themselves by emitting real findings AND
    # then appending NO_ISSUES_FOUND — in that case the parsed findings
    # take priority over the marker.
    if not findings and "NO_ISSUES_FOUND" in raw_output.upper():
        _log.info("[parse] Found NO_ISSUES_FOUND marker and no parsed findings — confirmed clean")
    elif not findings:
        _log.info("[parse] No findings parsed and no NO_ISSUES_FOUND marker")
    elif "NO_ISSUES_FOUND" in raw_output.upper():
        _log.warning(
            f"[parse] LLM contradicted itself: emitted {len(findings)} finding(s) "
            f"AND a NO_ISSUES_FOUND marker — keeping the {len(findings)} finding(s)"
        )

    _log.info(f"[parse] Total findings parsed: {len(findings)}")
    return findings


if __name__ == "__main__":
    sample_output = """[CRITICAL] auth.py:42 — SQL injection vulnerability. Fix: Use parameterized queries instead of string formatting.
[WARNING] utils.py:17 — Unused import 'os'. Fix: Remove the unused import.
some stray line the model shouldn't have written
[SUGGESTION] helpers.py:55 — Consider extracting to separate function."""

    print("--- baseline (Phi-3-style) output ---")
    for f in parse_findings(sample_output):
        print(f)

    # Synthetic Qwen-style drift cases: bold severity, numbered list markers,
    # lowercase NO_ISSUES_FOUND. Real output from `ollama run qwen3:4b-instruct`
    # should be swapped in here once available to replace this synthetic check.
    qwen_style_output = """1. **CRITICAL** app.py:10 — Unvalidated input used directly in SQL query. Fix: use parameterized queries.
- **WARNING** app.py:22 — Unused import 'os'. Fix: remove the unused import.
* [SUGGESTION] app.py:30 — Consider extracting this block into a helper function."""

    print("\n--- synthetic Qwen-style drift (bold + list markers) ---")
    for f in parse_findings(qwen_style_output):
        print(f)

    print("\n--- lowercase no_issues_found ---")
    print(parse_findings("no_issues_found"))

