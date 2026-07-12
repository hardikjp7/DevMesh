"""
prompt_builder.py

Builds the LLM prompt for a single diff hunk, using the fixed-format
instruction template from Section 8 of the project knowledge file.

Keeping the template in one place means we can iterate on wording without
touching llm_client.py or response_parser.py.
"""

from diff_extractor import Hunk, estimate_token_count

PROMPT_TEMPLATE = """You are a senior code reviewer. Analyze the following git diff and identify issues.

Respond with ONLY a list of findings, one per line, in EXACTLY this format:
[SEVERITY] {file_path}:LINE — Issue description. Fix: one-sentence fix.

Rules:
- SEVERITY must be one of: CRITICAL, MAJOR, MINOR, SUGGESTION
- CRITICAL: security vulnerabilities or bugs with high probability of breaking production (e.g. SQL injection, unhandled crash, data corruption).
- MAJOR: bugs or quality issues with real but lower production risk (e.g. missing error handling, N+1 query, resource leak).
- MINOR: small correctness or maintainability defects (e.g. unused import, unclear naming, missing edge case).
- SUGGESTION: optional style or design advice that is not a defect (e.g. extract to a function, add a comment).
- Each finding must be ONE single line. Do not wrap or split a finding across multiple lines.
- Do not use markdown, code blocks, backticks, or bullet points.
- Do not explain your reasoning. Do not add any text before or after the findings.
- Keep the "Fix:" part to one short sentence — no code samples.
- If there are no issues, respond with exactly: NO_ISSUES_FOUND

Example of a correctly formatted response:
[CRITICAL] app.py:10 — Unvalidated user input used in SQL query. Fix: use parameterized queries.
[MAJOR] app.py:15 — External API call has no error handling. Fix: wrap the call in a try/except and handle failures.
[MINOR] app.py:22 — Unused import 'os'. Fix: remove the unused import.
[SUGGESTION] app.py:40 — This block is repeated twice. Fix: extract into a shared helper function.

File: {file_path}

Git diff:
{diff}
"""


def build_prompt(hunk: Hunk) -> str:
    """
    Fills in the fixed prompt template with a single hunk's diff text and
    file path. The file path must be passed explicitly — the diff text
    itself (post-split) does not retain the "+++ b/file.py" header, so
    without this the model will hallucinate a file name.
    One hunk -> one prompt -> one LLM call (Section 7 design decision).
    """
    return PROMPT_TEMPLATE.format(diff=hunk.diff_text, file_path=hunk.file_path)


def measure_template_overhead() -> int:
    """
    Measures the actual token cost of everything in PROMPT_TEMPLATE other
    than {diff} itself, using an empty-diff hunk. This is what
    diff_extractor.PROMPT_TEMPLATE_OVERHEAD is based on — run this whenever
    PROMPT_TEMPLATE's wording changes to re-check that constant is still
    accurate (see Section 19.5 of the project knowledge file).
    """
    empty_hunk = Hunk(file_path="app.py", start_line=1, diff_text="")
    return estimate_token_count(build_prompt(empty_hunk))


if __name__ == "__main__":
    # Manual smoke test with a fake hunk
    fake_hunk = Hunk(
        file_path="auth.py",
        start_line=42,
        diff_text='+    query = "SELECT * FROM users WHERE id = " + user_id',
    )
    print(build_prompt(fake_hunk))
    print(f"\n[measured template overhead: ~{measure_template_overhead()} tokens]")
