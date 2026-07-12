"""

llm_client.py



Thin wrapper around the local LLM backend.



review_hunk()'s signature stays identical regardless

of backend — only the internals swap. Backend is selected via DEVMESH_BACKEND:

  - "ollama" (default): CPU-only dev/testing + live-demo fallback (unchanged

    from the pre-orientation skeleton). Always available, always works.

  - "qnn": Qualcomm AI Hub-compiled QNN context binary via the GenieX/QAIRT

    runtime, running on the Snapdragon X Elite NPU. This is the production

    path decided in Section 19.3, but the actual runtime call (_run_qnn

    below) can only be wired up and tested on the physical venue hardware —

    see the NotImplementedError and inline TODOs.



model finalized, confirmed via AI Hub benchmark data on real Snapdragon X Elite CRD hardware:

  PRIMARY:  Qwen3-4B-Instruct-2507 — runs on GenieX/QAIRT (native NPU

            runtime), 4096-token context, 1,301 tok/s prefill, 23.1 tok/s

            decode. Non-thinking variant, no <think> block overhead.

  BACKUP:   Phi-4-Mini-Instruct — on this hardware only reaches GenieX/

            llama.cpp (community runtime, not native QNN), and NPU-mode

            context length is capped at 512 tokens (4096 is only reachable

            via CPU/GPU backend, not NPU, per direct AI Hub checks — see

            Section 19.4 update). Kept as a documented fallback only.



Keeping both paths behind the same review_hunk() signature means the rest

of the pipeline (prompt_builder, response_parser, run_review, benchmark)

never needs to know which backend produced a result.



(QNN prompt-file handling):

  Switched the QNN path from a per-call tempfile.NamedTemporaryFile to a

  single fixed-name prompt file (GENIE_PROMPT_FILE) that gets overwritten

  on every call instead of created/deleted each time. Suspected cause of

  the "stuck on loading model..." failures seen at the venue: geniex may

  resolve/open the prompt file lazily (after its own model-load retry

  loop), and by that point a uniquely-named NamedTemporaryFile could

  already be gone (we deleted it in a `finally` block right after

  subprocess.run() returned, regardless of when/whether geniex actually

  read it). A fixed path that we overwrite in place — and do NOT delete

  after the call — removes that race entirely: the file exists before we

  invoke geniex and continues to exist afterward.

"""



import os

import re

import subprocess

import threading

import time

import requests

from dataclasses import dataclass



from devlog import get_logger



log = get_logger(__name__)



OLLAMA_URL = "http://localhost:11434/api/generate"

# Override with: set DEVMESH_MODEL=<ollama tag>

# Default targets the closest Ollama-available tag for local CPU testing of

# the primary model. CONFIRM the exact tag via `ollama list` / `ollama pull

# qwen3:4b-instruct` before relying on this — Ollama's library naming for

# the 2507 non-thinking release may differ slightly from the AI Hub name.

# If unavailable, `ollama pull qwen3:4b` works but requires passing

# enable_thinking=False equivalent behavior isn't guaranteed identical to

# the dedicated -Instruct-2507 checkpoint used on AI Hub.

MODEL_NAME = os.environ.get("DEVMESH_MODEL", "qwen3:4b-instruct")



# "ollama" (default, always works) or "qnn" (production path, venue-hardware only)

BACKEND = os.environ.get("DEVMESH_BACKEND", "qnn").lower()



# Set via env var DEVMESH_MOCK_LLM=1 to bypass the backend entirely and return

# a canned response. Useful for testing the rest of the pipeline (diff

# extraction, parsing, fan-out) while Ollama/QNN issues are debugged separately.

# Works identically regardless of DEVMESH_BACKEND, so mock mode is always

# available as a safety net per Section 19.7.

MOCK_MODE = os.environ.get("DEVMESH_MOCK_LLM", "0") == "1"



MOCK_RESPONSE = """[CRITICAL] file.py:1 — Mocked finding: potential SQL injection. Fix: use parameterized queries.

[WARNING] file.py:2 — Mocked finding: unused variable. Fix: remove it.

[SUGGESTION] file.py:3 — Mocked finding: consider extracting this to a helper function."""



# --- QNN/GenieX runtime config (Section 19.3/19.4/19.7/19.8) ----------------

# Switched from `genie-t2t-run.exe` (bundle-dir based) to `geniex infer`

# (Qualcomm AI Hub's CLI, resolves models by name from its own local cache —

# no bundle dir needed). One-shot subprocess call per hunk, no persistent

# session — simpler and avoids the REPL/readline issues a persistent

# session hit on Windows (needs a real console handle, not a plain pipe).

GENIE_MODEL = os.environ.get("DEVMESH_GENIE_MODEL", "ai-hub-models/Qwen3-4B-Instruct-2507")

GENIE_COMPUTE = os.environ.get("DEVMESH_GENIE_COMPUTE", "npu")

GENIE_THINK = os.environ.get("DEVMESH_GENIE_THINK", "false")

QNN_MODEL_NAME = GENIE_MODEL



# Section 19.8: fixed prompt-file path, overwritten every call instead of a

# fresh NamedTemporaryFile per call. Defaults next to this file so it's

# always writable regardless of TEMP/TMPDIR quirks on the venue box.

# Override with DEVMESH_GENIE_PROMPT_FILE if needed.

GENIE_PROMPT_FILE = os.environ.get(

    "DEVMESH_GENIE_PROMPT_FILE",

    os.path.join(os.path.dirname(os.path.abspath(__file__)), "devmesh_geniex_prompt.txt"),

)



# Matches geniex infer's stats footer line, e.g.:

# "— 21.0 tok/s • 7 tok • 0.1 s first token —"

GENIE_FOOTER_RE_PATTERN = r"^[—\-]+\s*[\d.]+\s*tok/s"



# Strips ANSI/terminal control sequences, e.g. \x1b[?25l (cursor hide),

# \x1b[?2004h (bracketed paste mode), \x1b[32m (color), etc. geniex emits

# these even in non-interactive -i mode, and they can sit in front of

# otherwise-recognizable content (e.g. a leading color code before "> "),

# which is one way the old marker-search could silently miss real output.

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")





@dataclass

class LLMResult:

    raw_output: str

    latency_seconds: float





# Default timeout is generous because CPU-only inference on larger diffs

# (or the first call after Ollama starts, which includes model load time)

# can take well over a minute. Override with: set DEVMESH_TIMEOUT=600

DEFAULT_TIMEOUT = int(os.environ.get("DEVMESH_TIMEOUT", "420"))





def review_hunk(prompt: str, model: str = MODEL_NAME, timeout: int = DEFAULT_TIMEOUT) -> LLMResult:

    """

    Sends a single prompt to the selected backend and returns the raw text

    output plus latency (for the benchmark harness — Section 9/20).



    Signature is stable across backends per Section 19.3 — nothing else in

    the pipeline (prompt_builder, response_parser, run_review, benchmark)

    needs to change when DEVMESH_BACKEND switches from "ollama" to "qnn".



    If MOCK_MODE is on (DEVMESH_MOCK_LLM=1), skips the backend entirely and

    returns a canned response instantly, regardless of which backend is

    selected — the safety-net fallback stays intact per Section 19.7.

    """

    if MOCK_MODE:

        log.debug("MOCK_MODE active — returning canned response instantly")

        time.sleep(0.1)  # simulate a bit of latency for realistic timing logs

        return LLMResult(raw_output=MOCK_RESPONSE, latency_seconds=0.1)



    log.info(f"Dispatching to backend={BACKEND!r} (prompt length: {len(prompt)} chars)")

    if BACKEND == "qnn":

        return _review_hunk_qnn(prompt, timeout)

    return _review_hunk_ollama(prompt, model, timeout)





WATCHDOG_WARN_INTERVAL_SECONDS = 15  # how often to log "still waiting" while blocked on the LLM





def _review_hunk_ollama(prompt: str, model: str, timeout: int) -> LLMResult:

    """

    Original skeleton path (Section 15), unchanged in behavior. CPU-only

    dev/testing and live-demo fallback — always available regardless of

    QNN status.



    A watchdog thread logs a periodic WARNING while requests.post() is

    still blocking, every WATCHDOG_WARN_INTERVAL_SECONDS. Without this, a

    genuinely hung call (Ollama not responding, model still loading,

    network stall) is silent until DEVMESH_TIMEOUT finally fires — which

    defaults to 420 seconds, long enough that "is it stuck or just slow"

    is a real question during a demo. This turns that silence into

    periodic, unmistakable log lines instead.

    """

    payload = {

        "model": model,

        "prompt": prompt,

        "stream": False,

    }



    log.info(f"POST {OLLAMA_URL} (model={model}, timeout={timeout}s)")



    done = threading.Event()



    def _watchdog():

        elapsed = 0

        while not done.wait(WATCHDOG_WARN_INTERVAL_SECONDS):

            elapsed += WATCHDOG_WARN_INTERVAL_SECONDS

            log.warning(

                f"Still waiting on Ollama response after {elapsed}s "

                f"(model={model}, timeout={timeout}s) — is `ollama serve` "

                f"running and responsive? Is the model still loading?"

            )



    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)

    watchdog_thread.start()



    start = time.time()

    try:

        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)

        response.raise_for_status()

    except requests.exceptions.ConnectionError as e:

        log.error(f"Could not reach Ollama at {OLLAMA_URL}: {e}")

        raise RuntimeError(

            "Could not reach Ollama at http://localhost:11434. "

            "Is Ollama running? Try `ollama serve` in another terminal."

        ) from e

    except requests.exceptions.HTTPError as e:

        # Surface Ollama's actual error body instead of just the status code.

        # Common cause: model name doesn't match what's pulled (check `ollama list`).

        try:

            error_detail = response.json().get("error", response.text)

        except ValueError:

            error_detail = response.text

        log.error(f"Ollama returned {response.status_code} for model '{model}': {error_detail}")

        raise RuntimeError(

            f"Ollama returned {response.status_code} for model '{model}': {error_detail}\n"

            f"Run `ollama list` to confirm the exact model name you have pulled."

        ) from e

    finally:

        done.set()

    elapsed = time.time() - start



    data = response.json()

    log.info(f"Ollama responded in {elapsed:.2f}s ({len(data.get('response', ''))} chars)")

    return LLMResult(raw_output=data.get("response", ""), latency_seconds=elapsed)





def _is_spinner_line(line: str) -> bool:

    """A loading-spinner animation frame, e.g. '🌎loading model...' or '🌎encoding...'."""

    low = line.lower()

    return "loading model" in low or "encoding..." in low





def _looks_like_stuck_loading(stdout: str) -> bool:

    """

    Section 19.8: detects the specific failure mode where geniex prints

    nothing but repeating "loading model..." spinner lines (globe emoji

    cycling) and then exits 0 with no response at all — a genuine stuck

    load, not just noisy output surrounding a real response.

    """

    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]

    if not lines:

        return False

    return all(_is_spinner_line(ln) for ln in lines)





def _is_echoed_prompt_line(line: str) -> bool:

    """

    Detects lines that are part of geniex's echoed-prompt output.

    geniex infer -i <file> echoes the entire input prompt back to stdout:

      - The first line is prefixed with "> " (greater-than + space)

      - All continuation lines are prefixed with ". " (dot + space)

    These are INPUT echo, not LLM output, and must be stripped.

    """

    stripped = line.strip()



    # "> " starts the first echoed line; ". " continues subsequent lines.



    # A bare "." (no trailing space) is how geniex echoes blank lines from

    # the original prompt — these also need to be stripped.



    return stripped.startswith("> ") or stripped.startswith(". ") or stripped == "."





def _is_dsp_info_line(line: str) -> bool:

    """Detects Qualcomm DSP runtime info/noise lines, e.g. 'DSP_INFO UNSUPPORTED_KEY: 49'."""

    return line.strip().startswith("DSP_INFO")





def _extract_geniex_response(stdout: str) -> str:

    """

    Section 19.10 REWRITE: correct extraction based on actual geniex CLI output format.



    The geniex infer -i <file> CLI outputs in this structure:



        [optional DSP_INFO lines]              <- Qualcomm runtime noise

        > [first line of echoed prompt]        <- echoed INPUT, not output

        . [continuation of echoed prompt]      <- echoed INPUT, not output

        . [continuation of echoed prompt]      <- echoed INPUT, not output

        ...                                    <- (entire prompt is echoed)

        [optional spinner lines]               <- loading/encoding animation

        [ACTUAL LLM RESPONSE LINE 1]           <- this is what we want

        [ACTUAL LLM RESPONSE LINE 2]           <- this is what we want

        — N tok/s • M tok • X s first token —  <- stats footer



    Previous bug: old code only skipped the single "> " line but left all

    ". " continuation lines (40+ lines of echoed prompt) in the result,

    causing parse_findings() to choke on prompt text mixed with response.



    New approach — filter by line type (order doesn't matter, each line is

    classified independently):

      1. Strip ANSI/terminal control sequences.

      2. Drop DSP_INFO noise lines.

      3. Drop ALL echoed-prompt lines ("> " AND ". " prefixed).

      4. Drop spinner/encoding animation lines.

      5. Drop the stats-footer line.

      6. Whatever remains is the actual LLM response.

    """

    log.debug(f"Full geniex stdout ({len(stdout)} chars): {stdout!r}")



    cleaned = ANSI_ESCAPE_RE.sub("", stdout)

    footer_re = re.compile(GENIE_FOOTER_RE_PATTERN)



    response_lines = []

    for line in cleaned.splitlines():

        stripped = line.strip()



        # Skip DSP runtime noise

        if _is_dsp_info_line(stripped):

            continue



        # Skip echoed-prompt lines (both "> " start and ". " continuations)

        if _is_echoed_prompt_line(stripped):

            continue



        # Skip spinner/encoding lines

        if _is_spinner_line(stripped):

            continue



        # Skip stats footer

        if footer_re.match(stripped):

            continue



        response_lines.append(line)



    result = "\n".join(response_lines).strip()



    if not result:

        # Check against the ANSI-cleaned stdout, not the raw stdout — the

        # raw version's leading control-code line (\x1b[?25l\x1b[?2004h)

        # isn't itself a spinner line, which would break the all-spinner

        # check below even on a genuine stuck-load case.

        if _looks_like_stuck_loading(cleaned):

            log.error(

                "geniex infer produced only loading-spinner output and never "

                "reached the response stage. Full stdout logged above at "

                "DEBUG level."

            )

            raise RuntimeError(

                "geniex infer appears to have never finished loading the model "

                "(stdout contains only repeating 'loading model...' lines, no "

                "response). This is not a timeout and not a parsing bug — check "

                "that the model name/cache is correct and that NPU compute is "

                "actually available on this device. Run `geniex infer <model> "

                "-c npu -i <file>` manually to confirm."

            )

        log.error(

            "geniex infer stdout was non-empty but nothing remained after "

            "stripping ANSI codes, echoed-prompt lines, spinner lines, and "

            "the stats footer — the response format may have changed. Full "

            "stdout logged above at DEBUG level."

        )

        raise RuntimeError(

            "geniex infer returned output that didn't match any known format "

            "after stripping known noise. Check the DEBUG log for the full "

            "raw stdout and update _extract_geniex_response() accordingly."

        )



    return result





def _review_hunk_qnn(prompt: str, timeout: int) -> LLMResult:

    """

    Real implementation — one-shot `geniex infer` subprocess call per hunk.

    No persistent session (a REPL-based approach was tried and abandoned —

    `geniex infer <model>`'s interactive REPL needs a real console handle,

    fails with "init readline: The handle is invalid" over a plain pipe).

    Non-interactive mode via `-i <prompt-file>` avoids that entirely and

    exits cleanly after one response.



    Section 19.8: prompt goes to a FIXED path (GENIE_PROMPT_FILE), opened

    and overwritten every call — not a fresh tempfile.NamedTemporaryFile

    that gets created then deleted per call. Reasoning: on the two venue

    runs so far, geniex sat printing "loading model..." for ~20s and then

    exited cleanly with no echoed prompt and no response — i.e. it never

    got as far as reading -i's contents. If geniex resolves/reads the

    prompt file lazily, *after* its own internal load/retry loop, a

    uniquely-named NamedTemporaryFile is a moving target: nothing

    guaranteed the file (or its parent handle) was still in the state

    geniex expected by the time geniex actually opened it, especially

    combined with the old `finally: os.remove(prompt_file)` cleanup, which

    deleted it as soon as our subprocess.run() call returned — regardless

    of whether geniex itself had actually finished reading it yet. A

    fixed, overwritten-in-place file removes that race: it exists before

    geniex is invoked and is intentionally left in place afterward.



    If this fixed-file version still shows the same "loading model..."

    behavior, that rules out the prompt-file race as the cause and points

    back to the model/runtime itself (see _looks_like_stuck_loading()).

    """

    try:

        with open(GENIE_PROMPT_FILE, "w", encoding="utf-8") as f:

            f.write(prompt)

    except OSError as e:

        log.error(f"Could not write geniex prompt file at {GENIE_PROMPT_FILE}: {e}")

        raise RuntimeError(

            f"Could not write geniex prompt file at {GENIE_PROMPT_FILE}: {e}"

        ) from e



    cmd = [

        "geniex", "infer", GENIE_MODEL,

        "-c", GENIE_COMPUTE,

        "-i", GENIE_PROMPT_FILE,

        f"--think={GENIE_THINK}",

    ]

    log.info(f"Running: geniex infer {GENIE_MODEL} -c {GENIE_COMPUTE} "

             f"-i {GENIE_PROMPT_FILE} ({len(prompt)} char prompt) --think={GENIE_THINK}")



    done = threading.Event()



    def _watchdog():

        elapsed = 0

        while not done.wait(WATCHDOG_WARN_INTERVAL_SECONDS):

            elapsed += WATCHDOG_WARN_INTERVAL_SECONDS

            log.warning(f"Still waiting on geniex NPU inference after {elapsed}s...")



    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)

    watchdog_thread.start()



    start = time.time()

    try:

        result = subprocess.run(

            cmd,

            capture_output=True,

            text=True,

            encoding="utf-8",

            errors="replace",

            timeout=timeout,

        )

    except subprocess.TimeoutExpired as e:

        log.error(f"geniex infer timed out after {timeout}s")

        raise RuntimeError(f"geniex infer timed out after {timeout}s") from e

    except FileNotFoundError as e:

        log.error("geniex not found on PATH")

        raise RuntimeError(

            "`geniex` command not found on PATH. Confirm the AI Hub GenieX "

            "CLI is installed and accessible from this terminal/process."

        ) from e

    finally:

        done.set()

    elapsed = time.time() - start



    # Section 19.8: log stderr unconditionally at debug level (not just on

    # failure) — earlier venue runs may have had useful info here that was

    # only ever surfaced on non-zero exit.

    if result.stderr:

        log.debug(f"geniex stderr ({elapsed:.2f}s): {result.stderr[:1000]}")



    if result.returncode != 0:

        log.error(f"geniex infer exited {result.returncode}. stderr: {result.stderr[:500]}")

        raise RuntimeError(

            f"geniex infer failed (exit {result.returncode}): {result.stderr[:500]}\n"

            f"stdout: {result.stdout[:500]}"

        )



    # NOTE: intentionally NOT deleting GENIE_PROMPT_FILE here (Section 19.8)

    # — it's a fixed path that just gets overwritten on the next call.

    # Deleting it after every call is what made the old tempfile approach

    # racy if geniex reads it lazily.

    raw_output = _extract_geniex_response(result.stdout)

    log.info(f"geniex responded in {elapsed:.2f}s ({len(raw_output)} chars)")

    return LLMResult(raw_output=raw_output, latency_seconds=elapsed)





if __name__ == "__main__":

    # Manual smoke test — requires `ollama serve` running and `ollama pull phi3` done

    test_prompt = (

        "You are a senior code reviewer. Analyze the following git diff and "

        "identify issues.\n\nFor each issue respond ONLY in this exact format:\n"

        "[SEVERITY] FILE:LINE — Issue description. Fix: recommended fix.\n\n"

        "SEVERITY must be one of: CRITICAL, WARNING, SUGGESTION\n"

        "Do not explain. Do not add preamble.\n\n"

        'Git diff:\n+    query = "SELECT * FROM users WHERE id = " + user_id\n'

    )

    result = review_hunk(test_prompt)

    print(f"Latency: {result.latency_seconds:.2f}s")

    print("Output:")

    print(result.raw_output)