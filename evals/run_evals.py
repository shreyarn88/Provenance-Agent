"""
Evaluate the provenance agent on test questions with known answers.

Run from the project root:
    python -m evals.run_evals
Results are saved to evals/results.json.
"""

import json
import re
import time
from pathlib import Path

from agent.agent import MODEL, PROVIDER, run_agent

HERE = Path(__file__).parent
PAUSE_SECONDS = 5  # stay under free-tier rate limits


def normalise(text: str) -> str:
    """Lowercase, unify dashes, and drop thousands separators (1,000 -> 1000)."""
    text = (text or "").lower()
    text = re.sub(r"[\u2010\u2011\u2012\u2013\u2014]", "-", text)
    text = re.sub(r"[\u2018\u2019]", "'", text)
    return re.sub(r"(?<=\d),(?=\d{3})", "", text)


def check(test: dict, result: dict) -> dict:
    answer = normalise(result["answer"])
    used = [c["tool"] for c in result["tool_calls"]]
    missing_tools = [t for t in test["expected_tools"] if t not in used]
    missing_facts = [
        group for group in test["must_include"]
        if not any(normalise(option) in answer for option in group.split("|"))
    ]
    return {
        "id": test["id"],
        "question": test["question"],
        "passed": not missing_tools and not missing_facts,
        "missing_tools": missing_tools,
        "missing_facts": missing_facts,
        "tools_used": used,
        "answer": result["answer"],
    }


def ask(question: str) -> dict:
    """Ask the agent, waiting and retrying if the free tier rate-limits us."""
    for attempt in range(3):
        result = run_agent(question, verbose=False)
        if "rate limit" not in (result["answer"] or "").lower():
            return result
        print("    rate limited, waiting 30 s ...")
        time.sleep(30)
    return result


def main():
    tests = json.loads((HERE / "questions.json").read_text())
    print(f"Evaluating {PROVIDER}: {MODEL} on {len(tests)} questions\n")

    results = []
    for test in tests:
        start = time.time()
        outcome = check(test, ask(test["question"]))
        outcome["seconds"] = round(time.time() - start, 1)
        results.append(outcome)

        mark = "PASS" if outcome["passed"] else "FAIL"
        print(f"[{mark}] {test['id']} ({outcome['seconds']} s)")
        if outcome["missing_tools"]:
            print(f"    expected tools not used: {outcome['missing_tools']}")
        if outcome["missing_facts"]:
            print(f"    missing in answer: {outcome['missing_facts']}")
        time.sleep(PAUSE_SECONDS)

    passed = sum(r["passed"] for r in results)
    print(f"\nScore: {passed}/{len(results)} passed ({passed / len(results):.0%})")

    (HERE / "results.json").write_text(json.dumps(
        {"provider": PROVIDER, "model": MODEL, "passed": passed,
         "total": len(results), "results": results},
        indent=2, ensure_ascii=False))
    print("Details saved to evals/results.json")


if __name__ == "__main__":
    main()
