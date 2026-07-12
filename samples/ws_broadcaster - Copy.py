"""
ws_broadcaster.py

REAL VERSION, UPDATED for the false-positive reiteration flow (Section 20).

Now bidirectional: still broadcasts findings to mobile as before, but also
listens for two message types coming back FROM mobile on the same
connection:

  {"type": "finding_decision", "finding_id": "f3", "decision": "approved"}
  {"type": "finding_decision", "finding_id": "f7", "decision": "false_positive", "comment": "..."}
  {"type": "generate_report"}

On "generate_report", delegates to report_trigger.handle_generate_report_request()
(reiteration pass + report_generator call) and sends the result back:
  {"type": "report_ready", "path": "..."}
  {"type": "report_error", "message": "..."}

See project knowledge Section 16 for the original one-directional version's
scope/limits, still true here: server lifetime tied to this process, no
backlog/replay for a client connecting late, local network only.
"""

import asyncio
import json
import socket
import threading
from typing import List
from response_parser import Finding
import review_session

HOST = "0.0.0.0"
PORT = 8765
SERVER_START_TIMEOUT_SECONDS = 5

_loop = None
_server_started = threading.Event()
_connected_clients = set()
_server_thread_lock = threading.Lock()
_server_thread_launched = False


def _port_already_in_use(host: str, port: int) -> bool:
    """
    Checked BEFORE attempting to bind, so a second run_review.py process
    fails immediately and loudly instead of silently losing the port bind
    in a background thread while its main-thread logic keeps running as if
    nothing's wrong (see PortInUseError docstring for why this matters —
    this is the actual root cause of "sometimes commit_id is present,
    sometimes null/stale").
    """
    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((probe_host, port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False


class PortInUseError(RuntimeError):
    pass


def _start_server_thread():
    global _loop, _server_thread_launched

    with _server_thread_lock:
        if _server_thread_launched:
            return

        if _port_already_in_use(HOST, PORT):
            # Fail LOUD, in the main thread, at import/startup time — not
            # silently inside the background daemon thread later. This is
            # the fix for the "two sessions" bug: review_session.session is
            # a true singleton WITHIN one process, but if a previous
            # run_review.py is still alive (e.g. sitting at its final
            # "Press Enter to stop" prompt from the last commit's review)
            # and a second one starts for a new commit, the second process
            # gets its OWN independent review_session — and previously would
            # silently fail to bind port 8765, never actually deliver its
            # findings, while mobile stays connected to the FIRST (stale)
            # process with the OLD commit's info. Refusing to start instead
            # of limping along makes that impossible to hit by accident.
            raise PortInUseError(
                f"Port {PORT} is already in use — another DevMesh review "
                f"process is almost certainly still running (check for a "
                f"run_review.py sitting at its 'Press Enter to stop' prompt "
                f"from a previous commit's review, and close it first). "
                f"Only one run_review.py process should be alive at a time; "
                f"this check exists specifically to prevent two independent "
                f"review_session instances from both being reachable at "
                f"once, which produces intermittent null/stale commit_id "
                f"and finding data depending on which process mobile "
                f"happens to be connected to."
            )

        _server_thread_launched = True

    async def handler(websocket):
        _connected_clients.add(websocket)
        print(f"[ws_broadcaster] Mobile client connected: {websocket.remote_address}")
        try:
            async for raw_message in websocket:
                await _handle_incoming_message(raw_message, websocket)
        finally:
            _connected_clients.discard(websocket)
            print("[ws_broadcaster] Mobile client disconnected.")

    async def serve_forever():
        import websockets
        try:
            async with websockets.serve(handler, HOST, PORT):
                print(f"[ws_broadcaster] WebSocket server listening on ws://{HOST}:{PORT}")
                _server_started.set()
                await asyncio.Future()  # run until process exits
        except OSError as e:
            # Safety net for the race between the pre-flight check above and
            # the actual bind (small window, another process could grab the
            # port in between). Printed loudly since exceptions inside a
            # background daemon thread are otherwise easy to miss entirely.
            print(f"\n{'=' * 70}\n[ws_broadcaster] FATAL: failed to bind ws://{HOST}:{PORT}: {e}\n"
                  f"Another DevMesh process almost certainly grabbed the port "
                  f"in the moment between this process's pre-flight check and "
                  f"its actual bind attempt. Findings will NOT be delivered "
                  f"from this process. Restart it after closing the other one.\n{'=' * 70}\n")

    def run_loop():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop.run_until_complete(serve_forever())

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    if not _server_started.wait(timeout=SERVER_START_TIMEOUT_SECONDS):
        print(
            "[ws_broadcaster] WARNING: server did not confirm startup within "
            f"{SERVER_START_TIMEOUT_SECONDS}s — findings may not be delivered."
        )


async def _handle_incoming_message(raw_message: str, websocket) -> None:
    try:
        data = json.loads(raw_message)
    except (json.JSONDecodeError, TypeError):
        print(f"[ws_broadcaster] Ignoring non-JSON message: {raw_message!r:.200}")
        return

    msg_type = data.get("type")

    if msg_type == "finding_decision":
        error = review_session.session.record_decision(
            finding_id=data.get("finding_id", ""),
            decision=data.get("decision", ""),
            comment=data.get("comment", ""),
        )
        if error:
            print(f"[ws_broadcaster] Rejected finding_decision: {error}")
            await websocket.send(json.dumps({"type": "decision_error", "message": error}))
        else:
            print(f"[ws_broadcaster] Recorded decision for {data.get('finding_id')}: {data.get('decision')}")

    elif msg_type == "generate_report":
        print("[ws_broadcaster] generate_report requested — running reiteration pass...")
        # Imported lazily to avoid import-order issues at module load time
        # (report_trigger imports llm_client/report_generator, which is fine
        # to do lazily here regardless).
        from report_trigger import handle_generate_report_request
        result = await handle_generate_report_request()
        await websocket.send(json.dumps(result))
        if result.get("type") == "report_ready":
            print(f"[ws_broadcaster] Report ready: {result.get('path')}")
        else:
            print(f"[ws_broadcaster] Report generation refused/failed: {result.get('message')}")

    else:
        print(f"[ws_broadcaster] Unknown message type from mobile: {msg_type!r}")


def _commit_payload():
    """
    Builds the "commit" object attached to every broadcast message. Mobile
    should key its finding groups by commit["short_id"] instead of flatly
    appending every finding from every run into one list — a new commit_id
    arriving means "this is a new review, start a new group" rather than
    "add more items to the same group."
    """
    commit_info = review_session.session.get_commit_info()
    if commit_info is None:
        return None
    return {
        "id": commit_info.commit_id,
        "short_id": commit_info.short_id,
        "author": commit_info.author_name,
        "author_email": commit_info.author_email,
        "message": commit_info.message,
        "timestamp": commit_info.timestamp,
    }


def broadcast_findings(findings: List[Finding], file_path: str) -> None:
    payload = {
        "commit": _commit_payload(),
        "file": file_path,
        "findings": [
            {
                "id": f.id,
                "severity": f.severity,
                "line": f.line,
                "description": f.description,
                "fix": f.fix,
            }
            for f in findings
        ],
    }
    message = json.dumps(payload)

    if not _connected_clients:
        print("[ws_broadcaster] No mobile client connected — message NOT delivered:")
        print(message)
        return

    async def _send_to_all():
        await asyncio.gather(
            *[client.send(message) for client in list(_connected_clients)],
            return_exceptions=True,
        )

    asyncio.run_coroutine_threadsafe(_send_to_all(), _loop)
    print(f"[ws_broadcaster] Sent to {len(_connected_clients)} client(s):")
    print(message)


_start_server_thread()
