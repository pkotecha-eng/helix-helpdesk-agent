# Helix Helpdesk Agent

An autonomous IT-helpdesk agent for Helix Industries (fictional, 12,000-person, SOX/HIPAA/GDPR-regulated). It reads Service Desk tickets, grounds every answer or action in one of 10 IT policies, and commits to one of six dispositions: `ANSWER_ONLY`, `AUTO_ACTION`, `PROPOSE_FOR_APPROVAL`, `ESCALATE_INCIDENT`, `ASK_CLARIFICATION`, `DEFER_HUMAN`. The graded axis this build optimizes hardest for is restraint: the agent must never execute a privileged, irreversible, or unauthorized action inline. Across 82 test runs (27 tickets + a 5x-repeated adversarial subset), **unsafe executions: 0**.

Full engineering reasoning — what was tried, what broke, the actual evidence — is in [`DECISIONS.md`](DECISIONS.md), written as decisions were made. This is the short version.

## Architecture

```
intake → dedupe_check → classify_domain → retrieve_policy → classify (LLM)
   → ground_check → authorize_gate → risk_gate → execute → verify → log
```

**Every gate after `classify` is deterministic Python, not another LLM call asked to be careful.** `classify` proposes a disposition, citation, and tool calls; everything downstream can only *override*, never rubber-stamp. `ground_check` rejects unretrieved or weak citations; `authorize_gate` rejects on-behalf-of actions the directory can't verify; `risk_gate` force-escalates GREEN actions when risk signals disagree, and structurally excludes AMBER/RED tools regardless of what the model proposed. The AMBER gate lives in the tool layer itself: `iam.grant_access` and `okta.disable_mfa` refuse to run without a matching `APPROVED` record — verified by calling them with no approval, a `PENDING` one, and an `APPROVED` one.

**Retrieval is two categorical-then-continuous stages, not one similarity score.** `classify_domain` (a cheap Haiku call) answers "is this ticket in scope for any of the 10 domains" *before* anything gets ranked. Sharpest evidence why: ticket E-08 correctly matched two domains at Stage 1 (`POL-05`, `POL-07`), but a combined top-3 ranking let POL-07's lexically-closer sections crowd out `POL-05 §5.3` entirely — Stage 1's own judgment silently overridden one stage later. Fixed by ranking top-3 **per domain**, then merging. Verified on the original case plus two incidental multi-domain tickets found via regression, and confirmed with no regression on a full 27-ticket run (E-08's disposition at precision=1.00, recall=1.00). The same split also stops an out-of-scope ticket (vacation days) from winning on raw similarity (0.445) over a genuinely correct citation elsewhere (0.347) — nearest-neighbor search alone has no "none of these apply" option.

## Prompt strategy

Context engineering, not prompt engineering: the policy-grounding portion of context — retrieved spans, directory facts — is assembled fresh per ticket. The behavioral rubric (disposition definitions, hard rules, worked examples) stays constant across every ticket, since safety rules shouldn't vary by request. `classify` sees only the spans retrieved for *this* ticket, directory facts relevant to *this* question, and the ticket body fenced as data, never instructions — layered under that fixed rubric.

Two rejected approaches, kept visible rather than hidden: self-consistency (3x voting) didn't reliably fix disposition flip-flopping, since the real cause was retrieval context, not model noise; fixing retrieval made the workaround unnecessary. A hardcoded software catalog was rejected in favor of a general reasoning rule ("an unconfirmed precondition defaults to the reviewed path") — a rule generalizes, a catalog only covers what it enumerates. Full reasoning for both in `DECISIONS.md`.

## Grounding

`ground_check` makes "grounded" a checked property, not a prompt instruction trusted on faith: (1) the cited `{policy_id, section}` must be among the sections actually retrieved this turn; (2) the *specific cited section's own score* — not the batch's best score — must clear a floor (0.15, set from the weakest genuinely-correct citation across 7 tickets). That second check is real: one case cited a section scoring 0.269 while a different section in the same batch scored 0.308 — a domain-max check would have passed it for the wrong reason.

**Grounding and correctness are different axes.** The E-08 investigation above (see Architecture) surfaced a second, distinct problem: before the retrieval fix, the model's reasoning stated `POL-05 §5.3`'s substance with real specificity — while that section was **not in its retrieved spans that turn**. The conclusion and reasoning were right; only the citation was wrong, and `ground_check` passed it cleanly because the section it *did* cite was genuinely retrieved and scored above the floor. `ground_check` verifies a citation was retrieved and scores adequately; it doesn't verify the model's reasoning is actually derived from what it cites. Closing the retrieval trigger that exposed this doesn't close the underlying risk — kept as a distinct, open finding.

## Where the act-vs-instruct line is drawn

Risk class is a floor, not a ceiling. GREEN may be called directly once grounded and authorized; AMBER is always drafted and routed for approval, never executed inline, enforced at the tool boundary independent of the model; RED is escalation-only. The subtler version, found mid-build: an *unconfirmed precondition* is the same category of mistake as an *unverified authority claim* — "my manager approved this" and "this software is catalog-listed" both get treated as false until confirmed, not true until disproven. The agent instructs by default; it only acts when the fact required to justify acting is actually present in context.

## What I'd harden before production

- The 0.15 confidence floor comes from 7 observed points, not a validated distribution — needs a larger, adversarial calibration set.
- The model can produce correct-sounding reasoning drawn from content not actually in that turn's context (confirmed on E-08, see Grounding) — `ground_check` can't catch this class of error.
- This model has no sampling-variance control (`temperature`/`top_p` unavailable). Residual, low-rate output instability was observed across several tickets (E-03, X-04, E-05, E-08, E-04's domain classification) — always safety-neutral, never an unsafe execution, but unresolved (full data in `DECISIONS.md`). A bounded test of the newer `effort`/adaptive-thinking parameter is scoped there, not run — it touches the highest-blast-radius call path in the system, not worth the risk this close to submission.
- Only 3 of 21 tools have real per-tool argument schemas; the rest don't need it today (args unused or path already deterministic), but the pattern should extend to any new tool whose args get consumed.
- The empty-tool-call `DEFER_HUMAN` guard is synthetic-tested but never exercised by a real ticket in the current test set — added reactively after a live crash, not yet a validated behavior.
- Verification coverage: 3 of 7 GREEN action tools now have genuine after-the-fact verification, closing the gap on the two containment tools tied to a confirmed incident (E-10). Three tools still have no persistent record of having run at all.
- Retry/backoff exists on the mock layer; add real circuit-breaking and structured tracing before production.

## Deployment judgment: healthcare vs. fintech

**Healthcare (HIPAA):** the secret-redaction pattern already in `agent.py` (mask before the model sees it) would extend to PHI patterns under the minimum-necessary principle. Audit retention becomes a compliance requirement, not a nice-to-have, and `decision_log.jsonl`'s append-only design is already the right shape. A third-party LLM API means a signed BAA before real deployment. `ESCALATE_INCIDENT`'s severity classification becomes legally load-bearing once breach-notification clocks start on discovery.

**Fintech (SOX/PCI):** approval routing needs segregation-of-duties awareness beyond "manager + data owner" — an approver can't share reporting authority with the requester on financial controls. I'd tighten the confidence floor, not loosen it: a wrong citation to a compliance officer is a potential control-failure finding, so more `DEFER_HUMAN` in exchange for fewer confidently-wrong groundings is the right trade. No tool, real or mocked, should ever be positioned to see real card data. The categorical-filter-before-similarity pattern and tool-boundary AMBER enforcement carry over unchanged to either vertical.

## Onboarding an 11th policy or tool

**Policy:** add an entry to `POLICIES` in `policies.py`. Nothing else changes by hand — `retriever.py`'s index and `classify_domain`'s domain list are both built dynamically from `POLICIES`, so a new policy is retrievable and classifiable immediately.

**Tool:** add the implementation + a `TOOL_RISK_CLASS` entry in `tools.py`, a line in `TOOL_CATALOG` (`prompts.py`), and a dispatch case in `_dispatch_green` (or AMBER/RED, if privileged). If its arguments are actually consumed downstream, add a real `input_schema` and register it in `_ACTION_ARG_SCHEMAS` — the same isolated-second-call pattern built for Tools 13/14/15 — rather than trusting the generic `proposed_tool_calls[].args` field.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
python demo.py --list                 # see all ticket ids
python demo.py --ticket E-04 --fresh  # full trace for one ticket
streamlit run app.py                  # visual walkthrough
python eval/run_eval.py               # full suite + confusion matrix + unsafe-action count
```
