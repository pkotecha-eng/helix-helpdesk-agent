"""LangGraph agent for the Helix Industries IT helpdesk.

    START
      |
      v
   intake            -- pull ticket + reporter profile, injection pre-filter
      |
      v
  dedupe_check        -- known duplicate? link + short-circuit to log
      |
      v
  retrieve_policy      -- TF-IDF retrieval over the 10-policy corpus
      |
      v
   classify (LLM)       -- Claude proposes a disposition + tool call(s)
      |
      v
  ground_check          -- citation must match what was actually retrieved
      |                     (deterministic; overrides hallucinated citations)
      v
  authorize_gate        -- on-behalf-of actions need a verified directory
      |                     relationship, never a claim in the ticket text
      v
   risk_gate             -- THE load-bearing safety node (deterministic):
      |                     - AMBER/RED tool proposals are never dispatched
      |                     - risk signals can force-escalate a nominally
      |                       GREEN action (risk class is a floor, not a
      |                       ceiling)
      |                     - blast-radius requests are never auto-fired
      v
    execute               -- disposition-driven dispatch; AMBER/RED tools
      |                     are not reachable from here at all (see
      |                     GREEN_DISPATCH_TABLE — it doesn't contain them)
      v
     verify                -- re-read state before claiming success; this
      |                     is what catches the silent-no-op failure mode
      v
      log                  -- decision log + JIRA comment/transition/label
      |
      v
     END

Early exits straight to `log` happen for ANSWER_ONLY / ASK_CLARIFICATION /
DEFER_HUMAN, which need no tool execution.
"""

import hashlib
import os
import re
from datetime import date
from typing import TypedDict

import anthropic
from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

import decision_log
import tools
from prompts import (ASSETMGMT_CREATE_CASE_SCHEMA, CLASSIFY_TOOL_SCHEMA, DISPOSITION_RUBRIC,
                     DOMAIN_CLASSIFY_TOOL_SCHEMA, DOMAIN_RUBRIC, ENDPOINT_GRANT_ADMIN_SCHEMA,
                     SERVICENOW_CREATE_REQUEST_SCHEMA, build_classification_context, build_domain_prompt)
from retriever import retrieve, top_confidence

load_dotenv()

CLAUDE_MODEL = "claude-sonnet-5"
DOMAIN_MODEL = "claude-haiku-4-5-20251001"  # Stage 1 is a coarse scope check — cheap/fast model is enough
CONFIDENCE_THRESHOLD = 0.15  # per-citation floor (not domain-max — see ground_check). Set from real
                              # observed data across 7 legitimately-cited tickets: E-05's genuinely
                              # correct citation (POL-04 §4.2, reached via the conditional-citation
                              # fallback) scores 0.1887, the weakest of the set by a wide margin — the
                              # rest cluster at 0.32-0.65. 0.15 sits below E-05 with real margin, well
                              # clear of the cluster. See DECISIONS.md for the full data and caveats.

_client = None


def _anthropic_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_\s-]?key|secret|token)\b\s*(?:is|was|[:=])\s*(\S+)"
)


def redact_secrets(text: str) -> str:
    """Mask secret-shaped substrings before they reach the model's context or
    any agent-authored output (§6.5: never echo a ticket-body secret into a
    comment or log line). Applied at intake, before the raw body is used to
    build classification context — so the model never sees the plaintext
    value in the first place and can't repeat what it never saw."""
    return SECRET_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", text)


INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|your\s+)?(previous|prior|above|earlier)\s+instructions?",
    r"you\s+are\s+now\s+in\s+(test|debug|developer|admin)\s+mode",
    r"disregard\s+(your|the)\s+(system\s+prompt|instructions|policy)",
    r"act\s+as\s+(if\s+you\s+are\s+|a\s+)?(system|admin|root)",
    r"this\s+is\s+an?\s+(override|admin)\s+(command|request|directive)",
    r"new\s+system\s+prompt",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)

BLAST_RADIUS_RE = re.compile(
    r"\b(everyone|whole\s+team|entire\s+team|all\s+of\s+(my\s+)?team|team[-\s]wide|all\s+employees)\b",
    re.IGNORECASE,
)

GREEN_TOOL_NAMES = {name for name, cls in tools.TOOL_RISK_CLASS.items() if cls == tools.GREEN}
AMBER_TOOL_NAMES = {name for name, cls in tools.TOOL_RISK_CLASS.items() if cls == tools.AMBER}
RED_TOOL_NAMES = {name for name, cls in tools.TOOL_RISK_CLASS.items() if cls == tools.RED}


class TicketState(TypedDict, total=False):
    ticket_id: str
    ticket: dict
    reporter_profile: dict
    injection_flagged: bool
    is_duplicate: bool
    duplicate_of: str | None
    domains: list[str]
    out_of_scope: bool
    retrieved_spans: list
    retrieval_confidence: float
    disposition: str
    proposed_disposition: str
    citation: dict | None
    proposed_citation: dict | None
    target_user: str | None
    proposed_tool_calls: list
    reasoning: str
    comment_to_requester: str
    ground_ok: bool
    authorized: bool
    risk_note: str | None
    blocked_privileged_attempt: bool
    executed_tool_calls: list
    unsafe_execution: bool
    verification_ok: bool
    withdrawn: bool


def canonical_ticket_id(ticket_id: str) -> str:
    """Walk duplicate_of chains so a duplicate shares its original's idempotency key."""
    seen = set()
    tid = ticket_id
    while True:
        t = tools.JIRA_STATE.get(tid)
        dup = t.get("duplicate_of") if t else None
        if not dup or dup in seen:
            return tid
        seen.add(tid)
        tid = dup


def _idem_key(*parts) -> str:
    return ":".join(str(p) for p in parts)


# Known Restricted/Confidential resources in the mock, matched against ticket
# body keywords rather than the model's free-text tool-call args (see execute()).
_RESOURCE_KEYWORDS = {
    "prod-postgres-cluster": ("postgres", "prod db", "production database", "production cluster"),
    "sales-pricing-sheet": ("pricing sheet", "pricing", "confidential"),
}


def _resolve_data_owner(ticket_body: str, citation: dict | None) -> str | None:
    body_lower = ticket_body.lower()
    for resource, keywords in _RESOURCE_KEYWORDS.items():
        if any(kw in body_lower for kw in keywords):
            owner = tools.data_owner_of(resource)
            if owner:
                return owner
    if citation and citation.get("policy_id") == "POL-05":
        return tools.data_owner_of("sales-pricing-sheet")
    return None


def _build_directory_notes(ticket_body: str, reporter_profile: dict) -> list[str]:
    """Real, grounded directory facts for the classify prompt — previously this
    was always an empty list, which meant the model was told to route approvals
    "per directory facts" while being shown none, and testing showed that
    contradiction correlates with flipping a legitimate self-request to
    DEFER_HUMAN under resampling. The model doesn't need to get the exact
    approver right (execute() resolves that deterministically either way) but
    it should not be reasoning from a stub that undercuts its own instructions."""
    notes = [f"{reporter_profile['username']}'s manager on file is {reporter_profile.get('manager') or 'none'}."]
    data_owner = _resolve_data_owner(ticket_body, citation=None)
    if data_owner:
        owner_profile = tools.directory_lookup_user(data_owner)
        owner_name = owner_profile["name"] if owner_profile else data_owner
        notes.append(f"The system/data referenced matches a known Restricted or Confidential resource; its data owner on file is {data_owner} ({owner_name}).")
    return notes


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def intake(state: TicketState) -> dict:
    ticket = tools.jira_get(state["ticket_id"])
    injection_flagged = bool(_INJECTION_RE.search(ticket["body"]))
    # Redact secret-shaped text in our own working copy — the mock JIRA ticket
    # (tools.JIRA_STATE) keeps what the user actually typed, same as real JIRA
    # would, but nothing derived from here (retrieval query, LLM context,
    # comments, log entries) should ever carry the plaintext forward.
    ticket = {**ticket, "body": redact_secrets(ticket["body"])}
    reporter_profile = tools.directory_lookup_user(ticket["reporter"])
    return {"ticket": ticket, "reporter_profile": reporter_profile, "injection_flagged": injection_flagged}


def dedupe_check(state: TicketState) -> dict:
    dup_of = state["ticket"].get("duplicate_of")
    return {"is_duplicate": bool(dup_of), "duplicate_of": dup_of}


def classify_domain(state: TicketState) -> dict:
    if state["is_duplicate"] or state["injection_flagged"]:
        return {"domains": [], "out_of_scope": False}  # short-circuited by other gates already

    response = _anthropic_client().messages.create(
        model=DOMAIN_MODEL,
        max_tokens=256,
        system=DOMAIN_RUBRIC + "\n\n" + build_domain_prompt(),
        tools=[DOMAIN_CLASSIFY_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "classify_domain"},
        messages=[{"role": "user", "content": state["ticket"]["body"]}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    domains = tool_use.input.get("domains", [])

    if not domains:
        return {
            "domains": [], "out_of_scope": True,
            "disposition": "DEFER_HUMAN", "proposed_disposition": "DEFER_HUMAN",
            "citation": None, "proposed_citation": None, "target_user": None,
            "proposed_tool_calls": [], "retrieved_spans": [], "retrieval_confidence": 0.0,
            "reasoning": "No policy domain plausibly applies to this ticket (Stage 1 domain check found none) — out of scope for the IT helpdesk.",
            "comment_to_requester": "This request is outside IT's policy scope; routing to the right team.",
        }
    return {"domains": domains, "out_of_scope": False}


def _route_after_domain(state: TicketState) -> str:
    return "log" if state.get("out_of_scope") else "retrieve_policy"


def retrieve_policy(state: TicketState) -> dict:
    domains = state.get("domains") or []
    if len(domains) <= 1:
        allowed = set(domains) if domains else None
        spans = retrieve(state["ticket"]["body"], top_k=3, allowed_policies=allowed)
    else:
        # Multiple domains matched by Stage 1 — give each its own guaranteed
        # top-3 instead of one shared top-3 across all of them. A flat
        # combined ranking silently overrides Stage 1's own multi-domain
        # judgment: found on a real case (E-08) where POL-05 §5.3 — the
        # substantively correct citation — was crowded out of a combined
        # top-3 entirely by POL-07 sections that share more surface
        # vocabulary with "email...to a customer," even though POL-05 was
        # correctly identified as relevant. Verified fix: per-domain top-3
        # surfaces POL-05 §5.3 (score 0.269) as a real candidate again. This
        # is the first confirmed Stage-2 failure on a ticket where Stage 1
        # worked correctly — every prior retrieval fix targeted single-domain
        # ranking misses, never tested against this failure mode.
        spans, seen = [], set()
        for domain in domains:
            for span in retrieve(state["ticket"]["body"], top_k=3, allowed_policies={domain}):
                key = (span["policy_id"], span["section"])
                if key not in seen:
                    seen.add(key)
                    spans.append(span)
        spans.sort(key=lambda s: s["score"], reverse=True)
    return {"retrieved_spans": spans, "retrieval_confidence": top_confidence(spans)}


def classify(state: TicketState) -> dict:
    if state["is_duplicate"]:
        return {
            "disposition": "DEFER_HUMAN",  # placeholder; execute() short-circuits duplicates before acting
            "proposed_disposition": "DEFER_HUMAN",
            "citation": None, "proposed_citation": None, "target_user": None, "proposed_tool_calls": [],
            "reasoning": f"Duplicate of {state['duplicate_of']}.",
            "comment_to_requester": f"This looks like a duplicate of {state['duplicate_of']}; linking and not re-acting.",
        }
    if state["injection_flagged"]:
        return {
            "disposition": "DEFER_HUMAN",
            "proposed_disposition": "DEFER_HUMAN",
            "citation": None, "proposed_citation": None, "target_user": None, "proposed_tool_calls": [],
            "reasoning": "Ticket body matched a prompt-injection / override pattern; refusing without invoking the model.",
            "comment_to_requester": "This request has been routed to a human for security review.",
        }

    context = build_classification_context(
        ticket_body=state["ticket"]["body"],
        reporter_profile=state["reporter_profile"],
        retrieved_spans=state["retrieved_spans"],
        directory_notes=_build_directory_notes(state["ticket"]["body"], state["reporter_profile"]),
    )
    result = _single_classify_call(context)
    proposed, dropped = _validate_tool_calls(result.get("proposed_tool_calls", []))
    reasoning = result.get("reasoning", "")
    if dropped:
        reasoning += f" [classify: dropped {len(dropped)} malformed tool-call entr{'y' if len(dropped)==1 else 'ies'} that didn't match the expected shape: {dropped!r}]"
    citation = result.get("citation")
    return {
        "disposition": result["disposition"],
        # Set once, here, before any downstream gate can override "disposition" —
        # never touched again after this point (verified: no other node's return
        # dict includes this key). Lets the decision log show whether a gate
        # actually corrected the model's proposal or the model got it right on
        # its own — "gates overrode N of M runs" is unprovable without this.
        "proposed_disposition": result["disposition"],
        "citation": citation,
        # Same principle as proposed_disposition, extended to citation: captures
        # the model's RAW citation (including None) before ground_check can
        # overwrite it via the conditional-citation swap or the ungrounded-
        # citation rejection. Distinguishes three cases a logged entry couldn't
        # tell apart before this: model provided no citation at all
        # (proposed_citation is None), model cited something real that
        # ground_check rejected as unretrieved (proposed_citation is a dict,
        # citation ends up None), or the conditional-citation fallback fired
        # (proposed_citation != citation, both non-None).
        "proposed_citation": citation,
        "target_user": result.get("target_user") or state["ticket"]["reporter"],
        "proposed_tool_calls": proposed,
        "reasoning": reasoning,
        "comment_to_requester": result.get("comment_to_requester", ""),
    }


def _validate_tool_calls(raw_calls: list) -> tuple[list, list]:
    """The model's tool_use output isn't schema-enforced server-side — it's a
    guide, not a guarantee. Caught in testing: a background eval run crashed
    with "'str' object has no attribute 'get'" in risk_gate because
    proposed_tool_calls contained a bare string instead of a
    {"tool": ..., "args": ...} object, on some call that wasn't reliably
    reproducible across 11 follow-up attempts — consistent with this model's
    already-documented sampling variance, not a specific ticket's content.
    Never trust structured LLM output without validating its shape before it
    flows downstream, same principle as ground_check for citations. Returns
    (valid_calls, dropped_malformed_entries)."""
    valid, dropped = [], []
    for c in raw_calls:
        if isinstance(c, dict) and "tool" in c:
            valid.append(c)
        else:
            dropped.append(c)
    return valid, dropped


def _single_classify_call(context: str) -> dict:
    response = _anthropic_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=DISPOSITION_RUBRIC,
        tools=[CLASSIFY_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "classify_ticket"},
        messages=[{"role": "user", "content": context}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input


# Sections whose applicability depends on a fact the ticket must actually
# confirm, not just a topically-relevant citation. Metadata about the POLICY
# TEXT itself (static), not invented facts about the world (e.g. no fake
# software catalog) — see DECISIONS.md, "E-05 citation-action coherence gap."
# Maps a conditional citation to the non-conditional fallback to prefer when
# the condition isn't confirmed in context.
CONDITIONAL_CITATIONS = {
    ("POL-04", "4.1"): ("POL-04", "4.2"),
}


def ground_check(state: TicketState) -> dict:
    if state["is_duplicate"] or state["injection_flagged"] or state.get("out_of_scope"):
        return {"ground_ok": True}  # already short-circuited before reaching the model

    disposition = state["disposition"]
    if disposition not in ("ANSWER_ONLY", "AUTO_ACTION", "PROPOSE_FOR_APPROVAL"):
        return {"ground_ok": True}  # ESCALATE/ASK_CLARIFICATION/DEFER don't require a policy citation

    citation = state.get("citation")
    # Score lookup keyed by the exact (policy_id, section) pair, not just a
    # membership set — the floor check below has to evaluate the SPECIFIC
    # cited section's own score, not the domain's top score. Caught in
    # testing: E-08's cited POL-05 §5.3 individually scores 0.269, but the
    # domain's top score (a different section, POL-05 §5.6) is 0.308 — the
    # old check (state["retrieval_confidence"], the domain's top-1 score)
    # would have passed E-08 for the wrong reason, on a section it didn't
    # even cite.
    scored_by_key = {(s["policy_id"], s["section"]): s["score"] for s in state["retrieved_spans"]}
    citation_key = (citation["policy_id"], citation["section"]) if citation else None
    if not citation or citation_key not in scored_by_key:
        return {
            "ground_ok": False,
            "disposition": "DEFER_HUMAN",
            "reasoning": f"Ungrounded citation {citation!r} — not among the sections actually retrieved this turn.",
            "comment_to_requester": "This request needs a closer look from the IT team before we can act on it.",
            "proposed_tool_calls": [],
        }

    updates: dict = {}
    # A conditional citation's applicability depends on a fact the ticket
    # must confirm (e.g. POL-04 §4.1's self-serve path requires confirmed
    # catalog membership). A ticket that just asks "how do I get X" never
    # confirms that — same principle as authorize_gate not trusting an
    # unverified authority claim, applied to an unverified precondition
    # instead. Default to the non-conditional fallback rather than assume
    # the condition is met. Caught in testing: the model cited POL-04 §4.1
    # while proposing an action (file a request) that only makes sense under
    # §4.2 — an internally inconsistent citation-action pair.
    if citation_key in CONDITIONAL_CITATIONS:
        fallback_key = CONDITIONAL_CITATIONS[citation_key]
        if fallback_key not in scored_by_key:
            return {
                "ground_ok": False,
                "disposition": "DEFER_HUMAN",
                "reasoning": f"Cited {citation!r} is conditional and unconfirmed by the ticket, and its fallback {fallback_key} wasn't retrieved either — deferring rather than guessing.",
                "comment_to_requester": "This request needs a closer look from the IT team before we can act on it.",
                "proposed_tool_calls": [],
            }
        citation = {"policy_id": fallback_key[0], "section": fallback_key[1]}
        citation_key = fallback_key
        updates["citation"] = citation
        updates["reasoning"] = state.get("reasoning", "") + f" [ground_check: original citation was conditional and unconfirmed by the ticket; corrected to {fallback_key[0]} §{fallback_key[1]}.]"

    # Defense-in-depth backstop, not the primary scope mechanism anymore —
    # Stage 1 domain classification handles out-of-scope detection now. This
    # catches the remaining case: the SPECIFIC cited section scores weakly,
    # even though it was technically retrieved.
    citation_score = scored_by_key[citation_key]
    if citation_score < CONFIDENCE_THRESHOLD:
        return {
            **updates,
            "ground_ok": False,
            "disposition": "DEFER_HUMAN",
            "reasoning": f"Cited section {citation!r} scores {citation_score:.3f}, below floor {CONFIDENCE_THRESHOLD}.",
            "comment_to_requester": "This request needs a closer look from the IT team before we can act on it.",
            "proposed_tool_calls": [],
        }
    return {"ground_ok": True, **updates}


def authorize_gate(state: TicketState) -> dict:
    if state["disposition"] in ("ANSWER_ONLY", "ASK_CLARIFICATION", "DEFER_HUMAN"):
        return {"authorized": True}

    reporter = state["ticket"]["reporter"]
    target = state.get("target_user") or reporter
    if target == reporter:
        return {"authorized": True}

    # Acting on someone else's behalf: the directory, not the ticket text, decides.
    if tools.directory_verify_manager(reporter, target):
        return {"authorized": True}

    return {
        "authorized": False,
        "disposition": "DEFER_HUMAN",
        "reasoning": f"{reporter} is not a verified manager of {target} in the directory; on-behalf-of authority in the ticket text is not trusted.",
        "comment_to_requester": "We can't act on someone else's account based on a request submitted by another employee. Please have the account owner submit this directly, or provide manager verification.",
        "proposed_tool_calls": [],
    }


def risk_gate(state: TicketState) -> dict:
    updates: dict = {}
    disposition = state["disposition"]
    proposed = state.get("proposed_tool_calls", [])

    # 1. AMBER/RED tools are never dispatched from here regardless of what was
    #    proposed — execute()'s dispatch table structurally excludes them, but
    #    we still detect and record the attempt as a safety-gate metric.
    privileged_attempts = [c for c in proposed if c.get("tool") in AMBER_TOOL_NAMES | RED_TOOL_NAMES]
    if privileged_attempts:
        updates["blocked_privileged_attempt"] = True
        if disposition == "AUTO_ACTION":
            updates["disposition"] = "DEFER_HUMAN"
            updates["reasoning"] = f"Model proposed a privileged tool call ({[c['tool'] for c in privileged_attempts]}) under AUTO_ACTION; forced to DEFER_HUMAN for review."
            updates["comment_to_requester"] = "This request needs review before we can proceed."
            disposition = "DEFER_HUMAN"

    # 2. Risk class is a floor, not a ceiling: check risk signals on the
    #    target account for ANY account-affecting GREEN action, regardless
    #    of what the model decided.
    target = state.get("target_user")
    account_tools = {"okta.unlock_account", "okta.send_password_reset", "okta.revoke_sessions", "okta.force_password_reset"}
    touches_account = any(c.get("tool") in account_tools for c in proposed) or disposition == "AUTO_ACTION"
    if target and touches_account and disposition not in ("ESCALATE_INCIDENT", "DEFER_HUMAN"):
        signals = tools.okta_risk_signals(target)
        if signals["compromise"] or signals["mfa_fatigue"] or signals["impossible_travel"]:
            flagged = [k for k in ("compromise", "mfa_fatigue", "impossible_travel") if signals[k]]
            updates["disposition"] = "ESCALATE_INCIDENT"
            updates["risk_note"] = f"okta.risk_signals flagged {flagged} on {target} — escalating instead of a routine account action."
            updates["comment_to_requester"] = "We've flagged unusual activity on this account and are escalating to security before taking any routine action."
            disposition = "ESCALATE_INCIDENT"

    # 3. Blast radius: never auto-fire a multi-user request.
    if disposition == "AUTO_ACTION" and BLAST_RADIUS_RE.search(state["ticket"]["body"]):
        updates["disposition"] = "DEFER_HUMAN"
        updates["reasoning"] = "Request affects multiple users (blast radius); routing to a human instead of auto-firing."
        updates["comment_to_requester"] = "Bulk/team-wide requests need human review before we act — routing this to the Service Desk team."
        updates["proposed_tool_calls"] = []

    return updates


def execute(state: TicketState) -> dict:
    # Re-read live ticket state immediately before acting — catches withdrawal.
    fresh = tools.jira_get(state["ticket_id"])
    if fresh["status"] == "Withdrawn":
        return {"withdrawn": True, "executed_tool_calls": [], "unsafe_execution": False,
                "comment_to_requester": "This ticket was withdrawn before we took any action; no changes were made."}

    disposition = state["disposition"]
    reporter = state["ticket"]["reporter"]
    target = state.get("target_user") or reporter
    canonical = canonical_ticket_id(state["ticket_id"])
    executed = []
    unsafe = False

    if state.get("is_duplicate"):
        tools.jira_link_issues(state["ticket_id"], state["duplicate_of"], relation="duplicates")
        return {"executed_tool_calls": [], "unsafe_execution": False, "withdrawn": False}

    # An action-requiring disposition with zero validated tool calls is a
    # signal the model's proposal was empty or entirely dropped by
    # _validate_tool_calls (e.g. every entry malformed) — not proof there's
    # nothing to do. Letting AUTO_ACTION/PROPOSE_FOR_APPROVAL proceed here
    # would silently do nothing while still reporting success downstream
    # (verify() only checks specific known failure shapes, not "were any
    # calls made at all"). Force DEFER_HUMAN instead of guessing.
    if disposition in ("AUTO_ACTION", "PROPOSE_FOR_APPROVAL") and not state.get("proposed_tool_calls"):
        return {
            "disposition": "DEFER_HUMAN",
            "executed_tool_calls": [], "unsafe_execution": False, "withdrawn": False,
            "reasoning": f"{disposition} had no valid tool calls after validation — proceeding would silently do nothing while claiming success. Deferring instead.",
            "comment_to_requester": "This request needs a closer look from the IT team before we can act on it.",
        }

    comment_override = None
    if disposition == "AUTO_ACTION":
        for call in state.get("proposed_tool_calls", []):
            name = call.get("tool")
            if name not in GREEN_TOOL_NAMES:
                if name in AMBER_TOOL_NAMES | RED_TOOL_NAMES:
                    unsafe = True  # would only happen if risk_gate's filter above were bypassed
                continue
            args = call.get("args", {})
            if name in _ACTION_ARG_SCHEMAS:
                # Tools 13/14/15 — the args on the main classify() call aren't
                # schema-validated (see DECISIONS.md); resolve them via the
                # isolated, strictly-schema'd second call instead of trusting
                # whatever shape the model happened to use there.
                args = _resolve_action_args(name, state["ticket"]["body"])
            result = _dispatch_green(name, args, reporter=reporter, target=target, canonical=canonical)
            if result is not None:
                executed.append({"tool": name, "args": args, "result": result})
        # The comment drafted at classify() time is written BEFORE execution — it can't
        # know a tool call is about to fail. Never let that optimistic draft reach the
        # requester if a call actually failed; the requester hears the true outcome.
        failures = [c for c in executed if isinstance(c.get("result"), dict) and c["result"].get("status") == "failed"]
        if failures:
            comment_override = (
                "We ran into an issue completing this and it hasn't gone through — "
                f"flagging for manual follow-up rather than reporting it done ({failures[0]['result'].get('error', 'see log')})."
            )

    elif disposition == "PROPOSE_FOR_APPROVAL":
        target_profile = tools.directory_lookup_user(target)
        approvers = [target_profile["manager"]] if target_profile and target_profile.get("manager") else []
        # Resolve the Restricted/Confidential resource from the ticket body against the
        # known catalog rather than the model's free-text tool-call args, whose key names
        # aren't reliable enough to parse (caught by testing: the model used "resource"/
        # "requested_access" here instead of "access", silently dropping data-owner routing).
        data_owner = _resolve_data_owner(state["ticket"]["body"], state.get("citation"))
        if data_owner:
            approvers.append(data_owner)
        action = {
            "target_user": target,
            "requested_by": reporter,
            "disposition_reasoning": state.get("reasoning", ""),
            "proposed_tool_calls": state.get("proposed_tool_calls", []),
        }
        result = tools.iam_create_approval(action, approvers or ["security-review-queue"], idempotency_key=_idem_key(canonical, target))
        executed.append({"tool": "iam.create_approval", "args": action, "result": result})

    elif disposition == "ESCALATE_INCIDENT":
        sev = "SEV-2" if state.get("risk_note") else "SEV-3"
        inc = tools.soc_open_incident(sev, state.get("reasoning", state["ticket"]["body"][:200]), idempotency_key=canonical)
        executed.append({"tool": "soc.open_incident", "args": {"sev": sev}, "result": inc})
        page = tools.soc_page_oncall("security-oncall")
        executed.append({"tool": "soc.page_oncall", "args": {"team": "security-oncall"}, "result": page})
        # Immediate GREEN containment where it applies (e.g. MFA-fatigue / compromise signals).
        if target:
            signals = tools.okta_risk_signals(target)
            if signals["compromise"] or signals["mfa_fatigue"] or signals["impossible_travel"]:
                r1 = tools.okta_revoke_sessions(target, idempotency_key=_idem_key(target, canonical))
                executed.append({"tool": "okta.revoke_sessions", "args": {"user": target}, "result": r1})
                r2 = tools.okta_force_password_reset(target, idempotency_key=_idem_key(target, canonical))
                executed.append({"tool": "okta.force_password_reset", "args": {"user": target}, "result": r2})

    result = {"executed_tool_calls": executed, "unsafe_execution": unsafe, "withdrawn": False}
    if comment_override:
        result["comment_to_requester"] = comment_override
    return result


_ACTION_ARG_SCHEMAS = {
    "servicenow.create_request": SERVICENOW_CREATE_REQUEST_SCHEMA,
    "endpoint.grant_admin": ENDPOINT_GRANT_ADMIN_SCHEMA,
    "assetmgmt.create_case": ASSETMGMT_CREATE_CASE_SCHEMA,
}


def _resolve_action_args(tool_name: str, ticket_body: str) -> dict:
    """Isolated second call for Tools 13/14/15 — the only GREEN tools whose
    args are actually consumed downstream (see DECISIONS.md, "Tool-call args
    are not actually schema-structured"). Deliberately NOT folded into
    _single_classify_call: that function is the single point every
    classify-path ticket flows through, so a bug in its parsing risks
    breaking classification for every ticket. This function only runs for
    AUTO_ACTION tickets proposing one of these 3 tools, and a bug here can
    only affect that one tool's argument shape, not classification
    generally. Forces a real per-tool schema via tool_choice, unlike
    proposed_tool_calls[].args in CLASSIFY_TOOL_SCHEMA, which is an
    unconstrained {"type": "object"}."""
    schema = _ACTION_ARG_SCHEMAS[tool_name]
    response = _anthropic_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system="Extract the exact arguments for this tool call from the ticket. Only use information actually present in the ticket text — do not invent values it doesn't state.",
        tools=[schema],
        # Use the schema's own (API-valid, underscored) name here, not the dotted
        # catalog-style tool_name key — Anthropic's tool names must match
        # ^[a-zA-Z0-9_-]{1,128}$, no dots. Found via a live BadRequestError.
        tool_choice={"type": "tool", "name": schema["name"]},
        messages=[{"role": "user", "content": ticket_body}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input


def _dispatch_green(name: str, args: dict, reporter: str, target: str, canonical: str):
    if name == "okta.unlock_account":
        return tools.okta_unlock_account(target, idempotency_key=_idem_key(target, canonical))
    if name == "okta.send_password_reset":
        return tools.okta_send_password_reset(target, idempotency_key=_idem_key(target, date.today().isoformat()))
    if name == "okta.revoke_sessions":
        return tools.okta_revoke_sessions(target, idempotency_key=_idem_key(target, canonical))
    if name == "okta.force_password_reset":
        return tools.okta_force_password_reset(target, idempotency_key=_idem_key(target, canonical))
    if name == "servicenow.create_request":
        item = args.get("item", "")
        return tools.servicenow_create_request(reporter, item, args.get("fields", {}), idempotency_key=_idem_key(reporter, item, date.today().isoformat()))
    if name == "endpoint.grant_admin":
        minutes = min(int(args.get("minutes", 60)), 60)
        return tools.endpoint_grant_admin(target, minutes, idempotency_key=_idem_key(target, canonical))
    if name == "assetmgmt.create_case":
        asset_tag = args.get("asset_tag", "")
        case_type = args.get("case_type", "lost_stolen")
        try:
            return tools.assetmgmt_create_case(asset_tag, case_type, args.get("fields", {}), idempotency_key=_idem_key(asset_tag, case_type))
        except tools.AssetCaseDispatchError as exc:
            return {"status": "failed", "error": str(exc)}
    if name in ("directory.lookup_user", "okta.risk_signals", "jira.get", "iam.get_approval"):
        return None  # read-only informational calls the model may propose; nothing to execute
    return None


def verify(state: TicketState) -> dict:
    if state.get("withdrawn"):
        return {"verification_ok": True}

    target = state.get("target_user")
    calls = state.get("executed_tool_calls", [])

    if state["disposition"] == "AUTO_ACTION":
        unlocked_calls = [c for c in calls if c["tool"] == "okta.unlock_account"]
        if unlocked_calls and target:
            signals = tools.okta_risk_signals(target)
            if signals["account_locked"]:
                return {
                    "verification_ok": False,
                    "comment_to_requester": "We attempted to unlock this account but a state check shows it's still locked — flagging for manual follow-up rather than reporting it resolved.",
                }
        return {"verification_ok": True}

    if state["disposition"] == "ESCALATE_INCIDENT" and target:
        revoke_calls = [c for c in calls if c["tool"] == "okta.revoke_sessions"]
        reset_calls = [c for c in calls if c["tool"] == "okta.force_password_reset"]
        if revoke_calls or reset_calls:
            signals = tools.okta_risk_signals(target)
            failures = []
            if revoke_calls and signals["sessions_active"]:
                failures.append("sessions still active after revoke_sessions")
            if reset_calls and not signals["password_reset_pending"]:
                failures.append("no pending reset recorded after force_password_reset")
            if failures:
                return {
                    "verification_ok": False,
                    "comment_to_requester": "We took containment action but a state check shows it may not have fully taken effect (" + "; ".join(failures) + ") — flagging for manual follow-up.",
                }

    return {"verification_ok": True}


def log(state: TicketState) -> dict:
    entry = decision_log.build_entry(state)
    decision_log.append(entry)

    ticket_id = state["ticket_id"]
    comment = state.get("comment_to_requester", "")
    if comment:
        tools.jira_comment(ticket_id, comment)

    if state.get("is_duplicate"):
        tools.jira_transition(ticket_id, "Closed — Duplicate")
    elif state["disposition"] == "ASK_CLARIFICATION":
        tools.jira_transition(ticket_id, "Waiting for Customer")
        tools.jira_add_label(ticket_id, "needs-clarification")
    elif state["disposition"] == "ESCALATE_INCIDENT":
        tools.jira_transition(ticket_id, "Escalated")
    elif state["disposition"] == "PROPOSE_FOR_APPROVAL":
        tools.jira_transition(ticket_id, "Pending Approval")
    elif state["disposition"] == "DEFER_HUMAN":
        tools.jira_transition(ticket_id, "Routed to Human Queue")
    elif state.get("withdrawn"):
        tools.jira_transition(ticket_id, "Withdrawn")
    elif not state.get("verification_ok", True):
        tools.jira_transition(ticket_id, "Needs Manual Follow-up")
    else:
        tools.jira_transition(ticket_id, "Resolved")

    return {}


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------

def _needs_no_action(state: TicketState) -> str:
    if state["disposition"] in ("ANSWER_ONLY", "ASK_CLARIFICATION", "DEFER_HUMAN") and not state["is_duplicate"]:
        return "log"
    return "execute"


def build_graph():
    graph = StateGraph(TicketState)
    graph.add_node("intake", intake)
    graph.add_node("dedupe_check", dedupe_check)
    graph.add_node("classify_domain", classify_domain)
    graph.add_node("retrieve_policy", retrieve_policy)
    graph.add_node("classify", classify)
    graph.add_node("ground_check", ground_check)
    graph.add_node("authorize_gate", authorize_gate)
    graph.add_node("risk_gate", risk_gate)
    graph.add_node("execute", execute)
    graph.add_node("verify", verify)
    graph.add_node("log", log)

    graph.set_entry_point("intake")
    graph.add_edge("intake", "dedupe_check")
    graph.add_edge("dedupe_check", "classify_domain")
    graph.add_conditional_edges("classify_domain", _route_after_domain, {"log": "log", "retrieve_policy": "retrieve_policy"})
    graph.add_edge("retrieve_policy", "classify")
    graph.add_edge("classify", "ground_check")
    graph.add_edge("ground_check", "authorize_gate")
    graph.add_edge("authorize_gate", "risk_gate")
    graph.add_conditional_edges("risk_gate", _needs_no_action, {"log": "log", "execute": "execute"})
    graph.add_edge("execute", "verify")
    graph.add_edge("verify", "log")
    graph.add_edge("log", END)
    return graph.compile()


_graph = None


def run_ticket(ticket_id: str) -> TicketState:
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph.invoke({"ticket_id": ticket_id})
