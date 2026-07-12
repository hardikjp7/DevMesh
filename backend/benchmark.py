"""
benchmark.py

Benchmark harness for the DevMesh review pipeline — Section 9 / Section 20
open item: "Build benchmark harness with structured latency logging
(separate single-call vs. chunked latency)".

WHAT THIS MEASURES
------------------
For each hunk extracted from a real git commit, this runs the full
prompt -> LLM -> parse cycle and records:
  - hunk identity (file, start line)
  - estimated hunk token count (diff_extractor.estimate_token_count)
  - whether the hunk was "chunked" or "single-call"
    (today chunking isn't implemented yet — Phase C item — so this field
    is a forward-looking placeholder that will start being populated once
    diff_extractor gains real chunking; until then every hunk logs as
    "single-call" and any hunk over MAX_HUNK_TOKENS logs a flag instead)
  - latency in seconds
  - number of findings parsed out
  - which LLM backend produced the result (Ollama model name, or "mock")

Results are written as both a CSV (for quick spreadsheet comparisons across
runs / models / hardware) and a JSON (for anything that wants structured
data, e.g. a future dashboard).

USAGE
-----
  python benchmark.py                       # benchmarks the last commit
  python benchmark.py --staged              # benchmarks staged changes
  python benchmark.py --repo /path/to/repo
  python benchmark.py --runs 3              # repeat each hunk N times and average
  python benchmark.py --out results/        # output directory (default: ./benchmark_results)

Respects the same env vars as llm_client.py (DEVMESH_MODEL, DEVMESH_MOCK_LLM,
DEVMESH_TIMEOUT, DEVMESH_BACKEND) so this can be run once against Ollama/Phi-3
or Phi-4-Mini now, and again against the real QNN backend once wired up at
the venue — same harness, just a different backend env var, so numbers are
directly comparable across CPU-only dev testing and real NPU hardware.
"""

import argparse
import csv
import json
import os
import statistics
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List

from diff_extractor import (
    get_diff,
    get_last_commit_diff,
    split_into_hunks,
    estimate_token_count,
    MAX_HUNK_TOKENS,
)
from prompt_builder import build_prompt
from llm_client import review_hunk, MODEL_NAME, MOCK_MODE
from response_parser import parse_findings


@dataclass
class HunkBenchmark:
    file_path: str
    start_line: int
    estimated_tokens: int
    call_type: str  # "single-call" | "chunked" | "single-call (over threshold, unchunked)"
    run_latencies: List[float] = field(default_factory=list)
    finding_count: int = 0
    backend: str = ""

    @property
    def mean_latency(self) -> float:
        return statistics.mean(self.run_latencies) if self.run_latencies else 0.0

    @property
    def min_latency(self) -> float:
        return min(self.run_latencies) if self.run_latencies else 0.0

    @property
    def max_latency(self) -> float:
        return max(self.run_latencies) if self.run_latencies else 0.0


def _backend_label() -> str:
    if MOCK_MODE:
        return "mock"
    backend = os.environ.get("DEVMESH_BACKEND", "ollama")
    return f"{backend}:{MODEL_NAME}"


def run_benchmark(repo_path: str, staged: bool, runs_per_hunk: int) -> List[HunkBenchmark]:
    print(f"[benchmark] Extracting diff from: {repo_path}")
    raw_diff = get_diff(repo_path, staged=True) if staged else get_last_commit_diff(repo_path)
    hunks = split_into_hunks(raw_diff)

    if not hunks:
        print("[benchmark] No hunks found — nothing to benchmark.")
        return []

    print(f"[benchmark] Found {len(hunks)} hunk(s). Backend: {_backend_label()}. "
          f"Runs per hunk: {runs_per_hunk}\n")

    results: List[HunkBenchmark] = []

    for i, hunk in enumerate(hunks, start=1):
        token_count = estimate_token_count(hunk.diff_text)
        # Chunking isn't implemented yet (Phase C item) — this call_type
        # field is deliberately explicit about that so benchmark output
        # never silently implies chunked timing exists when it doesn't.
        call_type = (
            "single-call"
            if token_count <= MAX_HUNK_TOKENS
            else "single-call (over threshold, unchunked)"
        )

        bench = HunkBenchmark(
            file_path=hunk.file_path,
            start_line=hunk.start_line,
            estimated_tokens=token_count,
            call_type=call_type,
            backend=_backend_label(),
        )

        print(f"[benchmark] Hunk {i}/{len(hunks)}: {hunk.file_path} "
              f"(line {hunk.start_line}, ~{token_count} tokens, {call_type})")

        prompt = build_prompt(hunk)

        for run_idx in range(1, runs_per_hunk + 1):
            start = time.time()
            result = review_hunk(prompt)
            elapsed = time.time() - start
            bench.run_latencies.append(elapsed)
            print(f"    run {run_idx}/{runs_per_hunk}: {elapsed:.2f}s")

        findings = parse_findings(result.raw_output)
        bench.finding_count = len(findings)
        print(f"    -> mean {bench.mean_latency:.2f}s, {bench.finding_count} finding(s)\n")

        results.append(bench)

    return results


def write_results(results: List[HunkBenchmark], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    csv_path = os.path.join(out_dir, f"benchmark_{timestamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "file_path", "start_line", "estimated_tokens", "call_type",
            "backend", "mean_latency_s", "min_latency_s", "max_latency_s",
            "runs", "finding_count",
        ])
        for r in results:
            writer.writerow([
                r.file_path, r.start_line, r.estimated_tokens, r.call_type,
                r.backend, f"{r.mean_latency:.4f}", f"{r.min_latency:.4f}",
                f"{r.max_latency:.4f}", len(r.run_latencies), r.finding_count,
            ])

    json_path = os.path.join(out_dir, f"benchmark_{timestamp}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in results], fh, indent=2)

    print(f"[benchmark] Wrote {csv_path}")
    print(f"[benchmark] Wrote {json_path}")

    if results:
        overall_mean = statistics.mean(r.mean_latency for r in results)
        total_findings = sum(r.finding_count for r in results)
        print(f"\n[benchmark] Summary: {len(results)} hunk(s), "
              f"overall mean latency {overall_mean:.2f}s, "
              f"{total_findings} total finding(s), backend={results[0].backend}")


def main():
    parser = argparse.ArgumentParser(description="DevMesh review pipeline benchmark harness")
    parser.add_argument("--repo", default=".", help="Path to the git repo to benchmark")
    parser.add_argument("--staged", action="store_true", help="Benchmark staged changes instead of the last commit")
    parser.add_argument("--runs", type=int, default=1, help="Number of repeated runs per hunk to average (default: 1)")
    parser.add_argument("--out", default="benchmark_results", help="Output directory for CSV/JSON results")
    args = parser.parse_args()

    results = run_benchmark(args.repo, args.staged, args.runs)
    if results:
        write_results(results, args.out)


if __name__ == "__main__":
    main()
