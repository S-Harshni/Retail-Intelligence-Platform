"""Measure the SQL assistant: does the query a model writes return the same table as the reference query?

    ollama pull qwen2.5-coder:3b qwen2.5:3b llama3.2:3b gemma2:2b
    python -m retail.assistant_eval          # writes docs/data/assistant.json

Model replies are cached in data/assistant_cache.jsonl, so the run can be stopped and resumed.
"""
import hashlib
import json
import os
import time
from collections import defaultdict

import httpx

from retail import assistant
from retail.assistant_questions import MUST_DECLINE, QUESTIONS
from retail.config import DATA, EXPORT, WAREHOUSE

MODELS = ["qwen2.5-coder:3b", "qwen2.5:3b", "llama3.2:3b", "gemma2:2b"]
ZERO_SHOT_TOO = {"qwen2.5-coder:3b"}       # the effect of the worked examples, on the code model
CACHE = DATA / "assistant_cache.jsonl"
SHOWN_ROWS = 8


class CachedModel:
    def __init__(self, model: str, store: dict):
        self.model, self.store, self.llm = model, store, assistant.ChatModel(model)

    def chat(self, messages: list[dict]) -> str:
        key = hashlib.sha256(json.dumps([self.model, messages]).encode()).hexdigest()
        if key not in self.store:
            for _ in range(3):
                try:
                    self.store[key] = self.llm.chat(messages)
                    break
                except httpx.HTTPError:
                    time.sleep(5)
            else:
                self.store[key] = "CANNOT ANSWER"
            with CACHE.open("a") as f:
                f.write(json.dumps({"key": key, "reply": self.store[key]}) + "\n")
        return self.store[key]


def _cell(v):
    return round(v, 4) if isinstance(v, float) else v if isinstance(v, int | bool) or v is None else str(v)


def main() -> None:
    conn = assistant.read_only_connection(WAREHOUSE)
    store = {}
    if CACHE.exists():
        store = {r["key"]: r["reply"] for r in map(json.loads, CACHE.read_text().splitlines())}
    reference = [assistant.run_query(conn, sql) for _, _, sql in QUESTIONS]

    runs, examples, finals = [], [], {}
    for model in os.environ.get("ASSISTANT_MODELS", ",".join(MODELS)).split(","):
        llm = CachedModel(model, store)
        for few_shot in ((False, True) if model in ZERO_SHOT_TOO else (True,)):
            first = right = ran = repaired = 0
            by_kind = defaultdict(lambda: [0, 0])
            for (kind, question, _), (_, expected) in zip(QUESTIONS, reference, strict=True):
                once = assistant.ask(question, llm, conn, few_shot, repairs=0)
                final = once if once.error is None else assistant.ask(question, llm, conn, few_shot, repairs=1)
                ok_once = once.error is None and not once.declined and assistant.same_result(expected, once.rows)
                ok = final.error is None and not final.declined and assistant.same_result(expected, final.rows)
                first += ok_once
                right += ok
                ran += final.error is None and not final.declined
                repaired += ok and not ok_once
                by_kind[kind][0] += ok
                by_kind[kind][1] += 1
                if few_shot:
                    finals.setdefault(model, []).append(final)
                    examples.append({"model": model, "question": question, "kind": kind, "sql": final.sql, "correct": bool(ok),
                                     "error": final.error, "attempts": final.attempts, "columns": final.columns,
                                     "rows": [[_cell(v) for v in row] for row in final.rows[:SHOWN_ROWS]], "row_count": len(final.rows)})
            refusals = [assistant.ask(q, llm, conn, few_shot, repairs=0) for q in MUST_DECLINE]
            declined = sum(r.declined for r in refusals)
            answered = sum(r.error is None and not r.declined for r in refusals)      # a query ran: a made-up answer
            n = len(QUESTIONS)
            runs.append({"model": model, "prompt": "few_shot" if few_shot else "zero_shot",
                         "accuracy": round(right / n, 4), "accuracy_first_try": round(first / n, 4),
                         "query_ran": round(ran / n, 4), "fixed_by_repair": repaired,
                         "declined_when_it_should": round(declined / len(MUST_DECLINE), 4),
                         "stopped_by_guard_or_database": len(MUST_DECLINE) - declined - answered,
                         "answered_when_it_should_not": answered,
                         "by_kind": {k: round(a / b, 4) for k, (a, b) in sorted(by_kind.items())}})
            print(runs[-1], flush=True)

    best = max((r for r in runs if r["prompt"] == "few_shot"), key=lambda r: r["accuracy"])
    # Voting: each question goes to the three strongest models; the result most of them agree on wins.
    panel = [best["model"]] + [r["model"] for r in sorted(runs, key=lambda r: -r["accuracy"]) if r["prompt"] == "few_shot" and r["model"] != best["model"]][:2]
    voted = [assistant.vote([finals[m][i] for m in panel]) for i in range(len(QUESTIONS))]
    hits = [v.error is None and not v.declined and assistant.same_result(expected, v.rows) for v, (_, expected) in zip(voted, reference, strict=True)]
    voting = {"models": panel, "accuracy": round(sum(hits) / len(hits), 4),
              "by_kind": {k: round(sum(h for h, q in zip(hits, QUESTIONS, strict=True) if q[0] == k) / sum(q[0] == k for q in QUESTIONS), 4)
                          for k in sorted({q[0] for q in QUESTIONS})}}
    print(voting, flush=True)
    out = {
        "questions": len(QUESTIONS), "must_decline": len(MUST_DECLINE), "tables": sorted(assistant.TABLES),
        "kinds": {k: sum(q[0] == k for q in QUESTIONS) for k in sorted({q[0] for q in QUESTIONS})},
        "runs": runs, "best_model": best["model"], "voting": voting,
        "reference": [{"question": q, "kind": k, "sql": sql} for k, q, sql in QUESTIONS],
        "examples": [e for e in examples if e["model"] == best["model"]],
        "runtime": "Ollama, 4-bit quantised models, on a laptop",
    }
    EXPORT.mkdir(parents=True, exist_ok=True)
    (EXPORT / "assistant.json").write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
