Here's a step-by-step verification guide, in order from "does it even boot" to "does the real GitHub path work."

## Step 0 — Place the files

```bash
cp webhook_server.py backend/webhook_server.py
cp post-commit hooks/post-commit
chmod +x hooks/post-commit
```

If your `.git/hooks/post-commit` is already installed from before, reinstall it so the new version is actually the one that fires:
```bash
cp hooks/post-commit .git/hooks/post-commit
chmod +x .git/hooks/post-commit
```

## Step 1 — Confirm the server still boots

```bash
cd backend
DEVMESH_MOCK_LLM=1 uvicorn webhook_server:app --host 0.0.0.0 --port 8000
```
Use `DEVMESH_MOCK_LLM=1` so you're not waiting on Ollama/a real model for these checks. You should see the FastAPI startup logs and `ws_broadcaster` announcing it's listening on `ws://0.0.0.0:8765`. If it crashes on import, that's a syntax/import problem — paste me the traceback.

In a second terminal:
```bash
curl http://localhost:8000/health
```
Expect: `{"status":"ok","service":"devmesh-webhook-listener"}`

## Step 2 — Test #4 (the loud failure warning) first, since it's easiest

Stop the server (Ctrl+C in terminal 1), then make a commit:
```bash
git commit --allow-empty -m "test: listener down"
```
Expect to see the boxed `WARNING: DevMesh webhook listener is NOT reachable` printed directly in your terminal — not buried in a log file. This confirms #4 works.

## Step 3 — Test the mock/local-commit path still works unchanged

Start the server again (Step 1), then:
```bash
git commit --allow-empty -m "test: local commit review"
```
Watch terminal 1's server logs. You should see:
```
[webhook_server] Received pull_request event (PR #0). Resolving diff source...
[webhook_server] Diff source: local_last_commit. Running review...
```
The `diff_source: local_last_commit` line is the important one — it confirms your own mock payload correctly skipped the GitHub API and fell back exactly like before. If you instead see `diff_source: github_api` here, something's wrong (it shouldn't be able to reach GitHub with a fake repo).

## Step 4 — Test #1 (real GitHub PR diff fetch)

With the server still running, from any terminal:
```bash
curl -X POST http://localhost:8000/webhook \
  -H "X-GitHub-Event: pull_request" \
  -H "Content-Type: application/json" \
  -d '{"action": "opened", "repository": {"full_name": "octocat/Hello-World"}, "pull_request": {"number": 1}}'
```
Watch terminal 1. Expect:
```
[webhook_server] Received pull_request event (PR #1 on octocat/Hello-World). Resolving diff source...
[webhook_server] Diff source: github_api. Running review...
```
`diff_source: github_api` confirms it actually hit GitHub's API and pulled a real diff. If you get `local_last_commit` instead, check terminal 1 for the `Could not fetch PR diff from GitHub (...)` line right above it — that'll tell you why (rate limit is the most likely culprit unauthenticated).

If you hit a 403 rate limit, set a token first and restart the server:
```bash
export GITHUB_TOKEN=ghp_yourtokenhere
```
(a token with no special scopes is enough for public repos)

## Step 5 — Test #action filtering

```bash
curl -X POST http://localhost:8000/webhook \
  -H "X-GitHub-Event: pull_request" \
  -H "Content-Type: application/json" \
  -d '{"action": "closed", "pull_request": {"number": 1}}'
```
Expect an immediate `{"status":"ignored","reason":"action 'closed' does not need a review"}` response with no review triggered.

## Step 6 — Confirm the phone still gets findings

Have your mobile app connected (or the mock WS client) before running Step 3 or 4 again — confirm findings still stream through exactly as they did before this change. This is the regression check: nothing about the WebSocket broadcast path was touched, but it's worth eyeballing once since it's the part that actually matters for the demo.

---

Run through these in order and tell me where (if anywhere) it diverges from what's expected — that'll tell us fast whether it's a code issue or an environment/network one.