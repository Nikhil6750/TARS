"""Manual, network/CLI-dependent reliability validation for skill_registry.
executor.execute_skill_prompt (Phase requirements 9 & 10 of the SKILL_USE
reliability pass) -- not part of the automated pytest suite, which must not
depend on the live Claude CLI. Run by hand:

    cd apps/backend
    python scripts/test_skill_execution_reliability.py

Prints every real number it reports -- exit codes, event counts, content
lengths, timing -- nothing here is fabricated or assumed to pass.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skill_registry.executor import execute_skill_prompt  # noqa: E402

SHORT_SKILL = "Be extremely concise. Answer in one sentence."

MEDIUM_SKILL = """---
name: rest-graphql-debug
description: Debug REST/GraphQL APIs.
---

# API Testing & Debugging

When debugging a REST or GraphQL API issue, work through this checklist:
1. Confirm the exact HTTP method, URL, and status code observed.
2. Check authentication: is the token/header actually being sent?
3. Compare the request/response schema against the API's documented contract.
4. For GraphQL, check whether the error is in the query shape or a resolver.
5. Reproduce with the minimal possible request (strip down to the failing field).
6. Note whether the failure is deterministic or intermittent.

Keep your answer focused on the specific symptom described, not a generic
tutorial on REST/GraphQL.
""" * 3  # ~3x repetition to get real markdown structure at a representative size

LARGE_SKILL = (MEDIUM_SKILL * 8) + (
    "\n\n# Additional reference notes\n"
    + ("Consider rate limiting, pagination cursors, and idempotency keys. " * 400)
)


async def run_batch(label: str, skill_content: str, task: str, count: int) -> dict:
    print(f"\n=== {label} (len={len(skill_content)} chars, {count} run(s)) ===")
    successes = 0
    failures = 0
    retried_count = 0
    total_duration = 0.0
    for i in range(1, count + 1):
        t0 = time.monotonic()
        result = await execute_skill_prompt(skill_content, task)
        elapsed = time.monotonic() - t0
        total_duration += elapsed
        d = result.diagnostics
        status = "OK" if result.success else "FAIL"
        print(
            f"  run {i:>2}: {status}  exit={d.returncode} attempts={d.attempts} "
            f"retried={d.retried} events={d.event_count} content_len={d.final_content_length} "
            f"elapsed={elapsed:.1f}s"
        )
        if not result.success:
            print(f"           error: {result.error}")
            print(f"           stderr: {d.stderr_text[:300]}")
            failures += 1
        else:
            successes += 1
        if d.retried:
            retried_count += 1
    print(f"  -> {successes}/{count} succeeded, {retried_count} needed a retry, "
          f"avg {total_duration/count:.1f}s/run")
    return {"label": label, "successes": successes, "failures": failures, "count": count}


async def main() -> None:
    task = "Review a pull request that changes an API endpoint's error handling."
    results = []

    results.append(await run_batch("SHORT skill", SHORT_SKILL, task, count=3))
    results.append(await run_batch("MEDIUM skill", MEDIUM_SKILL, task, count=3))
    results.append(await run_batch("LARGE skill", LARGE_SKILL, task, count=3))

    print("\n=== 20x repeated real executions of the SAME skill/request (MEDIUM skill) ===")
    repeated = await run_batch("MEDIUM skill x20", MEDIUM_SKILL, task, count=20)
    results.append(repeated)

    print("\n=== SUMMARY ===")
    total_ok = sum(r["successes"] for r in results)
    total_runs = sum(r["count"] for r in results)
    for r in results:
        print(f"  {r['label']}: {r['successes']}/{r['count']}")
    print(f"  TOTAL: {total_ok}/{total_runs}")
    print(f"  20x batch alone: {repeated['successes']}/{repeated['count']}")


if __name__ == "__main__":
    asyncio.run(main())
