"""
devlog.py

Centralized logging for the DevMesh backend + hooks-invoked components
(Section 20 — "good logs to debug where the pipeline is breaking or
stuck"). Frontend (mobile) error handling is Vatsal's own concern and out
of scope here.

WHY A SHARED MODULE INSTEAD OF AD HOC print():
- Every component so far used bare print() with a "[module_name]" prefix.
  Fine in a foreground terminal, but disappears the moment something runs
  backgrounded (setup.sh's webhook listener, nohup'd Expo, the
  post-commit hook's curl call) — exactly the class of bug already hit
  twice this project (the port-collision bug, the post-commit
  silent-curl-failure bug). A persistent log FILE, written regardless of
  whether anyone's watching the terminal, is the fix.
- A configurable level (DEBUG/INFO/WARNING/ERROR) lets you turn up
  verbosity while actively debugging without editing code, and turn it
  back down for a clean demo.
- Every log line is automatically tagged with which commit it's about,
  read live from review_session.session (not manually threaded through
  every function call) — so `grep c5eb95a logs/devmesh.log` shows the
  complete story of one specific review run even with several commits'
  logs interleaved in the same file.
- stage() gives a direct answer to "where did it get stuck": a hang shows
  up as a "STAGE START: ..." line with no matching "STAGE END" ever
  following it — you don't have to guess which of several blocking calls
  (git subprocess, LLM request, WebSocket send) is the one still running.

USAGE (from any backend module):
    from devlog import get_logger, stage
    log = get_logger(__name__)

    log.info("Something happened")
    log.warning("Something unexpected but recoverable")
    log.error("Something failed: %s", e)
    log.debug("Fine-grained detail, only shown at DEBUG level")

    with stage(log, "llm_call"):
        result = review_hunk(prompt)   # logs START/END/duration, or
                                        # FAILED + traceback + re-raises

CONFIGURATION (env vars):
    DEVMESH_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR   (default: INFO)
    DEVMESH_LOG_DIR=<path>                        (default: ./logs, relative to cwd)

Every log record goes to BOTH the console (same stdout everything already
printed to before) and a persistent file at <DEVMESH_LOG_DIR>/devmesh.log
(append mode — never truncated, so an earlier run's log isn't lost when a
new one starts).
"""

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager

_configured = False
_configure_lock = threading.RLock()  # RLock: see the deadlock note in _configure_once below

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] [commit=%(commit)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _CommitContextFilter(logging.Filter):
    """
    Injects the currently-active commit's short_id into every log record,
    read live from review_session.session at the moment each line is
    logged. Correct even across the WebSocket server's separate
    background thread — review_session.session is a true singleton
    shared by the whole process, threads included, so there's no need to
    manually propagate context across threads/async tasks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        commit_tag = "-"
        try:
            import review_session
            info = review_session.session.get_commit_info()
            if info is not None:
                commit_tag = info.short_id
        except Exception:
            # devlog must never be the reason a log line fails to print —
            # any problem here (review_session not importable yet during
            # early module init, circular import timing, etc.) just falls
            # back to "-" silently.
            pass
        record.commit = commit_tag
        return True


def _configure_once() -> None:
    global _configured
    with _configure_lock:
        if _configured:
            return

        level_name = os.environ.get("DEVMESH_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

        log_dir = os.environ.get("DEVMESH_LOG_DIR", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "devmesh.log")

        formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
        commit_filter = _CommitContextFilter()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(commit_filter)

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(commit_filter)

        root = logging.getLogger("devmesh")
        root.setLevel(level)
        root.addHandler(console_handler)
        root.addHandler(file_handler)
        root.propagate = False

        _configured = True

    # Logging happens AFTER releasing the lock, deliberately. The commit
    # filter attached above calls `import review_session` on every log
    # emission — and review_session.py calls devlog.get_logger() at module
    # level, which re-enters _configure_once() and this SAME lock. Logging
    # from inside the `with _configure_lock:` block self-deadlocks the
    # thread the first time anything logs (a plain Lock isn't reentrant;
    # RLock above is defense-in-depth, but keeping the actual log call
    # outside the critical section is the real fix — same pattern already
    # hit once in review_session.py's record_decision()).
    root = logging.getLogger("devmesh")
    root.info(f"Logging initialized — level={level_name}, file={os.path.abspath(log_path)}")


def get_logger(name: str) -> logging.Logger:
    """
    Returns a logger for `name` (pass __name__). Configured on first call
    to write to both console and the shared logs/devmesh.log file, with
    the current commit automatically tagged on every line.
    """
    _configure_once()
    # Component loggers nest under "devmesh." so they share the handlers
    # configured above (via propagation to the "devmesh" logger) while
    # still reporting their own module name in each line.
    return logging.getLogger(f"devmesh.{name}")


@contextmanager
def stage(log: logging.Logger, name: str):
    """
    Logs the start/end/duration of a named pipeline stage. If an
    exception occurs inside, logs it (with the stage name and full
    traceback) as an ERROR and re-raises — nothing is swallowed.

    A hang inside the `with` block is visible in the log as a
    "STAGE START" line with no "STAGE END" ever following it, which is
    exactly the signal you want when the pipeline seems stuck: it tells
    you precisely which blocking call (git subprocess, LLM request,
    WebSocket send, ...) is the one still running.
    """
    log.info(f"STAGE START: {name}")
    start = time.time()
    try:
        yield
    except Exception:
        elapsed = time.time() - start
        log.error(f"STAGE FAILED: {name} (after {elapsed:.2f}s)", exc_info=True)
        raise
    else:
        elapsed = time.time() - start
        log.info(f"STAGE END: {name} ({elapsed:.2f}s)")
