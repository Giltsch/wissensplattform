#!/usr/bin/env python3
"""Retrieval evaluation runner.

Runs each question from a gold-set JSON against the live /api/admin/eval/retrieve
endpoint and reports Recall@5 and MRR. The endpoint is retrieval-only —
no LLM call, no conversation persistence, no budget consumption — so this
is safe to run against production.

Usage:
    EVAL_BASE=http://localhost:8080 \\
    EVAL_EMAIL=admin@example.com \\
    EVAL_PASSWORD='...'  \\
        python eval/run_eval.py eval/gold_questions.json

Why a CLI instead of a smoke shell script: precision metrics (rank,
reciprocal rank, recall) are easier to express as Python.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def login(opener: urllib.request.OpenerDirector, base: str, email: str, password: str) -> None:
    req = urllib.request.Request(
        f"{base}/api/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    r = opener.open(req)
    if r.status != 200:
        raise SystemExit(f"login failed: {r.status}")


def retrieve(opener: urllib.request.OpenerDirector, base: str, question: str) -> list[dict]:
    req = urllib.request.Request(
        f"{base}/api/admin/eval/retrieve",
        data=json.dumps({"message": question}).encode(),
        headers={"Content-Type": "application/json"},
    )
    r = opener.open(req)
    return json.loads(r.read())


def evaluate_one(hits: list[dict], expected: list[str]) -> tuple[bool, float, int]:
    """Return (hit_in_top5, reciprocal_rank, rank_or_-1)."""
    for idx, h in enumerate(hits[:5], start=1):
        if h["source"] in expected:
            return True, 1.0 / idx, idx
    return False, 0.0, -1


def fmt_row(cols: list[str], widths: list[int]) -> str:
    return "  ".join(c[:w].ljust(w) for c, w in zip(cols, widths))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", type=Path, help="Path to gold-set JSON")
    parser.add_argument("--base", default=os.environ.get("EVAL_BASE", "http://localhost:8080"))
    parser.add_argument("--email", default=os.environ.get("EVAL_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("EVAL_PASSWORD"))
    parser.add_argument("--json-out", type=Path, help="Optional path to write a JSON report")
    args = parser.parse_args()

    if not args.email or not args.password:
        print("FAIL: EVAL_EMAIL and EVAL_PASSWORD env vars (or --email/--password) required", file=sys.stderr)
        return 2
    if not args.gold.is_file():
        print(f"FAIL: gold-set file not found: {args.gold}", file=sys.stderr)
        return 2

    gold = json.loads(args.gold.read_text("utf-8"))
    questions = gold.get("questions", [])
    if not questions:
        print("FAIL: gold-set has no 'questions' array", file=sys.stderr)
        return 2

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    login(opener, args.base, args.email, args.password)

    widths = [3, 60, 8, 6, 30]
    print(fmt_row(["#", "Frage", "Recall", "Rank", "Erwartet (Top-5 enthält?)"], widths))
    print("-" * sum(widths) + "----")

    results: list[dict] = []
    rrs: list[float] = []
    recalls: list[bool] = []
    latencies: list[float] = []

    for i, q in enumerate(questions, start=1):
        question = q["question"]
        expected = q.get("expected_sources") or [q["expected_source"]]
        t0 = time.monotonic()
        try:
            hits = retrieve(opener, args.base, question)
        except urllib.error.HTTPError as e:
            print(f"  [{i:>3}] HTTP {e.code} on {question[:60]}", file=sys.stderr)
            continue
        latencies.append(time.monotonic() - t0)

        in_top5, rr, rank = evaluate_one(hits, expected)
        rrs.append(rr)
        recalls.append(in_top5)
        results.append({
            "question": question,
            "expected": expected,
            "rank": rank,
            "in_top5": in_top5,
            "top_sources": [h["source"] for h in hits[:5]],
        })

        marker = "✓" if in_top5 else "✗"
        print(fmt_row([
            str(i), question, marker,
            str(rank) if rank > 0 else "—",
            ", ".join(expected),
        ], widths))

    # ---- Summary ----
    n = len(recalls)
    if n == 0:
        print("\nFAIL: no questions evaluated", file=sys.stderr)
        return 1
    recall_at_5 = sum(recalls) / n
    mrr = sum(rrs) / n
    p50 = statistics.median(latencies) if latencies else 0.0
    p95 = sorted(latencies)[int(0.95 * len(latencies))] if len(latencies) > 1 else (latencies[0] if latencies else 0.0)

    print("\n--- Zusammenfassung ---")
    print(f"  Fragen:        {n}")
    print(f"  Recall@5:      {recall_at_5:.1%}  ({sum(recalls)}/{n})")
    print(f"  MRR:           {mrr:.3f}")
    print(f"  Latenz p50/p95: {p50*1000:.0f} ms / {p95*1000:.0f} ms")

    if args.json_out:
        report = {
            "n": n,
            "recall_at_5": recall_at_5,
            "mrr": mrr,
            "latency_p50_ms": int(p50 * 1000),
            "latency_p95_ms": int(p95 * 1000),
            "results": results,
        }
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        print(f"\nJSON-Report: {args.json_out}")

    return 0 if recall_at_5 >= 0.7 else 1  # heuristic threshold for CI


if __name__ == "__main__":
    sys.exit(main())
