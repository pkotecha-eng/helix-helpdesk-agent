"""CLI walkthrough — the primary demo surface (see DEMO_SCRIPT.md).

    python demo.py --ticket E-04       # AUTO_ACTION executed end-to-end
    python demo.py --ticket E-07       # AMBER request refused-and-routed
    python demo.py --list              # show every ticket id + body

Prints the ticket, the full graph trace (retrieval -> decision -> gate
checks -> tool calls -> verification -> log line), not just the final
answer — the point of this project is the guardrails, not the chatbot
reply, so the trace is the thing worth looking at.
"""

import argparse
import json

import agent
import decision_log
import tools
from tickets import TICKETS


def _print_header(title: str):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def run_and_print(ticket_id: str):
    if ticket_id not in TICKETS:
        print(f"Unknown ticket id: {ticket_id}")
        return

    ticket = TICKETS[ticket_id]
    _print_header(f"TICKET {ticket_id}  (reporter: {ticket['reporter']})")
    print(f"  {ticket['body']}")

    state = agent.run_ticket(ticket_id)

    _print_header("RETRIEVAL")
    for span in state["retrieved_spans"]:
        print(f"  [{span['policy_id']} §{span['section']}] score={span['score']:.3f}  {span['title']}")
    print(f"  top confidence: {state['retrieval_confidence']:.3f}")

    _print_header("DECISION")
    proposed_disposition = state.get("proposed_disposition")
    print(f"  disposition:  {state['disposition']}")
    if proposed_disposition and proposed_disposition != state["disposition"]:
        print(f"                (model originally proposed: {proposed_disposition} — overridden by a safety gate below)")

    citation = state.get("citation")
    proposed_citation = state.get("proposed_citation")
    citation_required_for = proposed_disposition or state["disposition"]
    if citation:
        print(f"  citation:     {citation['policy_id']} §{citation['section']}")
    elif citation_required_for in ("ANSWER_ONLY", "AUTO_ACTION", "PROPOSE_FOR_APPROVAL"):
        print("  citation:     (none — ground_check rejected it or the model omitted one; see reasoning below)")
    else:
        print("  citation:     (none — not required for this disposition)")
    if proposed_citation and proposed_citation != citation:
        print(f"                (model originally cited: {proposed_citation['policy_id']} §{proposed_citation['section']} — not accepted, see ground_check below)")

    print(f"  reasoning:    {state.get('reasoning', '')}")

    _print_header("SAFETY GATES")
    print(f"  ground_check ok:            {state.get('ground_ok', True)}")
    print(f"  authorized:                 {state.get('authorized', True)}")
    print(f"  blocked privileged attempt: {state.get('blocked_privileged_attempt', False)}")
    if state.get("risk_note"):
        print(f"  risk escalation:            {state['risk_note']}")

    _print_header("TOOL CALLS")
    calls = state.get("executed_tool_calls", [])
    if not calls:
        print("  (none — this disposition doesn't execute a tool)")
    for c in calls:
        print(f"  {c['tool']}({json.dumps(c['args'])}) -> {json.dumps(c['result'])}")
        if isinstance(c.get("result"), dict) and c["result"].get("idempotent_replay"):
            print("    -> served from the idempotency cache (same idempotency_key seen before; tool body did not re-execute)")

    _print_header("VERIFICATION")
    print(f"  verification_ok: {state.get('verification_ok', True)}")

    _print_header("REQUESTER-FACING COMMENT")
    print(f"  {state.get('comment_to_requester', '')}")

    _print_header("SAFETY SUMMARY")
    print(f"  unsafe_execution: {state.get('unsafe_execution', False)}  <-- must always be False")


def list_tickets():
    for tid, t in TICKETS.items():
        print(f"{tid:6s} [{t['reporter']:10s}] {t['body']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket", help="ticket id to run, e.g. E-04")
    parser.add_argument("--list", action="store_true", help="list all ticket ids")
    parser.add_argument("--fresh", action="store_true", help="reset all mock state before running")
    parser.add_argument("--twice", action="store_true", help="run the ticket twice in one process, to demonstrate idempotency (mock state is in-memory only and doesn't persist across separate CLI invocations)")
    args = parser.parse_args()

    if args.list:
        list_tickets()
    elif args.ticket:
        if args.fresh:
            tools.reset_state()
            decision_log.reset()
        run_and_print(args.ticket)
        if args.twice:
            print("\n" + "#" * 70)
            print("# RUNNING AGAIN — same process, same idempotency cache, no reset")
            print("#" * 70)
            run_and_print(args.ticket)
    else:
        parser.print_help()
