"""
reiteration.py

Builds the prompt and parses the response for the "false-positive
reiteration" flow: when a developer flags a finding as false positive on
mobile with a comment explaining why, this sends the original diff context
+ the original finding + the developer's comment back to the LLM, and asks
it to reiterate — confirm the finding still stands, withdraw it, or land
somewhere in between — given what the developer said.

Mirrors prompt_builder.py / response_parser.py's pattern (fixed-format
instruction + tolerant parsing) rather than inventing a new style.
"""

import re
from dataclasses import dataclass

from review_session import ReviewedFinding

REITERATION_PROMPT_TEMPLATE = """You are a senior code reviewer. You previously flagged an issue in a code \
review. The developer has marked it as a false positive and explained why. \
Reconsider the finding given their explanation.

Respond with ONLY ONE line, in EXACTLY this format:
[VERDICT] One or two sentence explanation.

VERDICT must be one of:
- MAINTAINED (the developer's comment does not change your assessment — the issue is still real)
- WITHDRAWN (the developer's comment shows this was not actually an issue — you agree it's a false positive)
- PARTIALLY_VALID (the developer has a point, but there is still a smaller concern worth noting)

Do not use markdown, code blocks, or bullet points. Do not add any text before or after the single line.

Original file: {file_path}

Original diff:
{diff}

Your original finding:
[{severity}] {file_path}:{line} — {description} Fix: {fix}

Developer's comment (why they believe this is a false positive):
{comment}
"""


@dataclass
class Reiteration:
    verdict: str
    text: str


# Tolerant, same spirit as response_parser.FINDING_RE — optional brackets,
# optional markdown bold, case-insensitive verdict word.
REITERATION_RE = re.compile(
    r"^\**\[?(MAINTAINED|WITHDRAWN|PARTIALLY_VALID)\]?\**\s*[-:]?\s*(.+)$",
    re.IGNORECASE,
)


def build_reiteration_prompt(reviewed_finding: ReviewedFinding, dev_comment: str) -> str:
    f = reviewed_finding.finding
    return REITERATION_PROMPT_TEMPLATE.format(
        file_path=f.file,
        diff=reviewed_finding.diff_text,
        severity=f.severity,
        line=f.line,
        description=f.description,
        fix=f.fix,
        comment=dev_comment,
    )


def parse_reiteration(raw_output: str) -> Reiteration:
    """
    Tolerant parse — if the model doesn't follow the format at all, falls
    back to verdict="MAINTAINED" (the safe default: an unparseable response
    should not silently make a real issue disappear from the report) with
    the raw output kept as the text so a human can still read what the
    model said.
    """
    cleaned = raw_output.strip()
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        match = REITERATION_RE.match(line)
        if match:
            verdict, text = match.groups()
            return Reiteration(verdict=verdict.upper(), text=text.strip())

    return Reiteration(verdict="MAINTAINED", text=cleaned or "(no response from model)")


if __name__ == "__main__":
    from response_parser import Finding

    fake_finding = Finding(
        severity="WARNING",
        file="test_utils.py",
        line=12,
        description="Hardcoded API key in test fixture.",
        fix="Move to environment variable or test-only config.",
        id="f1",
    )
    reviewed = ReviewedFinding(
        finding=fake_finding,
        diff_text='+    API_KEY = "sk-test-1234567890"  # test fixture, not real',
    )
    prompt = build_reiteration_prompt(reviewed, "This is a mock key used only in unit tests, never a real credential.")
    print(prompt)
    print("--- sample parse ---")
    print(parse_reiteration("[WITHDRAWN] Confirmed — this is a test fixture key, not a real credential leak."))
