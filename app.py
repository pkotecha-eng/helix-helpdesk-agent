"""Streamlit demo — visual walkthrough of the same trace demo.py prints to
the terminal. Not a substitute for eval/run_eval.py or DECISIONS.md; a
legible surface for the 5-minute walkthrough.

    streamlit run app.py
"""

import streamlit as st

import agent
import decision_log
import tools
from tickets import TICKETS

st.set_page_config(page_title="Helix Helpdesk Agent", layout="wide")

st.title("Helix Helpdesk Agent")
st.caption(
    "Autonomous IT-helpdesk agent for Helix Industries — grounds every decision "
    "in policy, restrained by deterministic safety gates. This UI shows the same "
    "trace as `demo.py`; it isn't the source of truth (that's eval/run_eval.py "
    "and decision_log.jsonl)."
)

with st.sidebar:
    st.header("Run a ticket")
    ticket_id = st.selectbox("Ticket", list(TICKETS.keys()))
    st.text_area("Ticket body", TICKETS[ticket_id]["body"], disabled=True, height=100)
    fresh = st.checkbox("Reset mock state first (--fresh)", value=True)
    st.caption(
        "Uncheck this and click Run again on the same ticket to see the "
        "idempotency cache in action (\U0001F501 badge under Tool calls)."
    )
    run = st.button("Run", type="primary")

    st.divider()
    show_log = st.checkbox("Show decision log")

if run:
    if fresh:
        tools.reset_state()
        decision_log.reset()
    with st.spinner("Running through the graph..."):
        state = agent.run_ticket(ticket_id)
    st.session_state["last_state"] = state
    st.session_state["last_ticket_id"] = ticket_id

state = st.session_state.get("last_state")

if state is None:
    st.info("Select a ticket in the sidebar and click Run.")
else:
    active_ticket_id = st.session_state["last_ticket_id"]
    ticket = TICKETS[active_ticket_id]

    st.subheader(f"Ticket {active_ticket_id} — reporter: {ticket['reporter']}")
    st.write(ticket["body"])

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Decision")
        proposed_disposition = state.get("proposed_disposition")
        disposition = state["disposition"]
        if proposed_disposition and proposed_disposition != disposition:
            st.warning(
                f"Model originally proposed **{proposed_disposition}** — "
                f"overridden to **{disposition}** by a safety gate (see right)."
            )
        st.metric("Disposition", disposition)

        citation = state.get("citation")
        proposed_citation = state.get("proposed_citation")
        citation_required_for = proposed_disposition or disposition
        if citation:
            st.write(f"**Citation:** {citation['policy_id']} §{citation['section']}")
        elif citation_required_for in ("ANSWER_ONLY", "AUTO_ACTION", "PROPOSE_FOR_APPROVAL"):
            st.error("**Citation:** none — ground_check rejected it or the model omitted one.")
        else:
            st.write("**Citation:** not required for this disposition.")
        if proposed_citation and proposed_citation != citation:
            st.caption(
                f"Model originally cited: {proposed_citation['policy_id']} "
                f"§{proposed_citation['section']} — not accepted."
            )

        st.markdown("**Reasoning**")
        st.write(state.get("reasoning", ""))

        st.markdown("**Requester-facing comment**")
        st.info(state.get("comment_to_requester", ""))

    with col2:
        st.markdown("### Safety gates")
        g1, g2, g3 = st.columns(3)
        g1.metric("ground_check", "OK" if state.get("ground_ok", True) else "FAILED")
        g2.metric("authorized", "OK" if state.get("authorized", True) else "FAILED")
        g3.metric("blocked privileged", "YES" if state.get("blocked_privileged_attempt") else "no")
        if state.get("risk_note"):
            st.warning(f"Risk escalation: {state['risk_note']}")

        st.markdown("### Verification")
        st.write("Passed" if state.get("verification_ok", True) else "Failed — see comment above")

        st.markdown("### Safety summary")
        if state.get("unsafe_execution", False):
            st.error("unsafe_execution: True — THIS SHOULD NEVER HAPPEN")
        else:
            st.success("unsafe_execution: False")

    st.markdown("### Retrieval")
    spans = state.get("retrieved_spans", [])
    if spans:
        st.caption(
            "Retrieval/classification re-runs fresh every time (only tool "
            "execution is idempotent) — the exact domain/section mix may "
            "vary slightly between runs, but disposition and safety outcome "
            "remain stable."
        )
        st.dataframe(
            [
                {"Policy": s["policy_id"], "Section": s["section"], "Title": s["title"], "Score": round(s["score"], 3)}
                for s in spans
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"Top confidence: {state.get('retrieval_confidence', 0):.3f}")
    else:
        st.caption("No retrieval performed for this ticket (out of scope / short-circuited before retrieval).")

    st.markdown("### Tool calls")
    calls = state.get("executed_tool_calls", [])
    if not calls:
        st.caption("(none — this disposition doesn't execute a tool)")
    for c in calls:
        replay = isinstance(c.get("result"), dict) and c["result"].get("idempotent_replay")
        label = c["tool"] + ("  \U0001F501 served from idempotency cache" if replay else "")
        with st.expander(label, expanded=True):
            st.json({"args": c["args"], "result": c["result"]})

if show_log:
    st.divider()
    st.markdown("### Decision log")
    entries = decision_log.read_all()
    if entries:
        st.dataframe(entries, use_container_width=True)
    else:
        st.caption("No decision log entries on disk yet — run a ticket first.")
