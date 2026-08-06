"""Append-only decision log — one JSON line per ticket processed.

A genuine end-to-end trace, not just the final outcome: which domains Stage
1 matched, every retrieved policy span with its score, whether ground_check
passed or corrected a citation, whether authorize_gate verified the
requester, any risk-escalation note, every tool call made (with args and
result), and the post-action verification result. This is the audit trail
the assignment asks for (§1.2) and what eval/run_eval.py reads back to build
the report and confusion matrix.

proposed_disposition (set once in classify(), never touched again) vs.
disposition (the final, possibly gate-corrected value) is what makes gate
overrides provable rather than asserted: disposition != proposed_disposition
means a downstream gate actually changed the outcome, not just that the
model happened to propose something safe on its own.
"""

import json
import os
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "decision_log.jsonl")


def build_entry(state: dict) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticket_id": state["ticket_id"],
        "reporter": state["ticket"]["reporter"],
        "disposition": state["disposition"],
        "proposed_disposition": state.get("proposed_disposition"),
        "citation": state.get("citation"),
        "proposed_citation": state.get("proposed_citation"),
        "target_user": state.get("target_user"),
        "is_duplicate": state.get("is_duplicate", False),
        "duplicate_of": state.get("duplicate_of"),
        "domains": state.get("domains", []),
        "out_of_scope": state.get("out_of_scope", False),
        "retrieved_spans": state.get("retrieved_spans", []),
        "retrieval_confidence": state.get("retrieval_confidence", 0.0),
        "ground_ok": state.get("ground_ok", True),
        "authorized": state.get("authorized", True),
        "risk_note": state.get("risk_note"),
        "withdrawn": state.get("withdrawn", False),
        "blocked_privileged_attempt": state.get("blocked_privileged_attempt", False),
        "unsafe_execution": state.get("unsafe_execution", False),
        "verification_ok": state.get("verification_ok", True),
        "executed_tool_calls": state.get("executed_tool_calls", []),
        "reasoning": state.get("reasoning", ""),
        "comment_to_requester": state.get("comment_to_requester", ""),
    }


def append(entry: dict, path: str = LOG_PATH) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_all(path: str = LOG_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def reset(path: str = LOG_PATH) -> None:
    if os.path.exists(path):
        os.remove(path)
