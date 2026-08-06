"""Mock enterprise tool layer — Okta, ServiceNow, IAM, asset management, SOC, JIRA.

Nothing here talks to a real system. All state is an in-memory dict seeded
from directory_data.py / tickets.py at import time and mutated in place.

Design choices worth flagging (see README "Engineering quality" / "Onboarding
tool #11" sections for the fuller version):

- Risk classes (GREEN/AMBER/RED) are enforced HERE, not just in agent.py's
  graph structure. iam.grant_access / okta.disable_mfa physically refuse to
  run without a matching APPROVED record in APPROVALS. That means the safety
  property holds even if agent.py has a bug, the LLM misbehaves, or someone
  calls these functions directly from a REPL — the guardrail lives in the
  tool boundary, not in prompt discipline.
- okta.risk_signals bundles `account_locked` into its read-only response.
  The assignment's tool table doesn't list a dedicated "read lock status"
  endpoint, but §6.4/§7 require the agent to re-read state before claiming
  an unlock succeeded — bundling it onto the existing read-only risk-signals
  call avoids inventing a 22nd tool for a one-field read.
- Every state-changing tool takes an idempotency_key and replays the cached
  result on a repeat key (§1.3: "using it is the agent's responsibility, not
  the environment's" — the cache keys off exactly what's passed in, it does
  not compute the "correct" key for you).
"""

import copy
import random
import time
from dataclasses import dataclass, field

from directory_data import ASSETS, USERS, data_owner_of, is_manager_of, lookup_user as _lookup_user
from tickets import TICKETS

# ---------------------------------------------------------------------------
# Risk-class registry (used by agent.py's risk_gate for defense-in-depth checks)
# ---------------------------------------------------------------------------

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"

TOOL_RISK_CLASS = {
    "jira.get": GREEN, "jira.comment": GREEN, "jira.transition": GREEN,
    "jira.add_label": GREEN, "jira.link_issues": GREEN,
    "directory.lookup_user": GREEN, "directory.verify_manager": GREEN,
    "okta.unlock_account": GREEN, "okta.risk_signals": GREEN,
    "okta.send_password_reset": GREEN, "okta.revoke_sessions": GREEN,
    "okta.force_password_reset": GREEN,
    "servicenow.create_request": GREEN, "endpoint.grant_admin": GREEN,
    "assetmgmt.create_case": GREEN,
    "iam.create_approval": GREEN, "iam.get_approval": GREEN,
    "iam.grant_access": AMBER, "okta.disable_mfa": AMBER,
    "soc.open_incident": RED, "soc.page_oncall": RED,
}


class PrivilegedActionBlocked(Exception):
    """Raised when an AMBER tool is invoked without a matching APPROVED record."""


class AssetCaseDispatchError(Exception):
    """Raised by the seeded partial-failure asset case (return-kit dispatch step)."""


# ---------------------------------------------------------------------------
# Idempotency + retry
# ---------------------------------------------------------------------------

_IDEMPOTENCY_CACHE: dict[tuple[str, str], dict] = {}
FORCE_TRANSIENT_FAILURE_ONCE: set[tuple[str, str]] = set()  # test hook for retry demo


def idempotent(tool_name: str):
    def decorator(fn):
        def wrapper(*args, idempotency_key: str, **kwargs):
            cache_key = (tool_name, idempotency_key)
            if cache_key in _IDEMPOTENCY_CACHE:
                return {**_IDEMPOTENCY_CACHE[cache_key], "idempotent_replay": True}
            result = with_retry(fn)(*args, idempotency_key=idempotency_key, **kwargs)
            _IDEMPOTENCY_CACHE[cache_key] = result
            return {**result, "idempotent_replay": False}
        return wrapper
    return decorator


def with_retry(fn, max_attempts: int = 3, base_delay: float = 0.01):
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                return fn(*args, **kwargs)
            except _TransientError as exc:
                last_exc = exc
                if attempt < max_attempts:
                    time.sleep(base_delay * (2 ** (attempt - 1)))
        raise last_exc
    return wrapper


class _TransientError(Exception):
    pass


def _maybe_raise_transient(tool_name: str, idempotency_key: str):
    key = (tool_name, idempotency_key)
    if key in FORCE_TRANSIENT_FAILURE_ONCE:
        FORCE_TRANSIENT_FAILURE_ONCE.discard(key)
        raise _TransientError(f"simulated transient failure on {tool_name}")


# ---------------------------------------------------------------------------
# Runtime state (seeded, then mutated)
# ---------------------------------------------------------------------------

@dataclass
class AccountState:
    # `locked`, `sessions_active`, and `password_reset_pending` are all
    # mutated by their respective tools AND exposed back via
    # okta_risk_signals — that round trip is what verify() uses to confirm
    # each action actually took effect, same mechanism for all three.
    locked: bool = False
    lock_epoch: int = 0
    sessions_active: bool = True
    password_reset_pending: bool = False


ACCOUNTS: dict[str, AccountState] = {
    "mchen": AccountState(locked=True, lock_epoch=1),          # E-04: routine lockout
    "avargas": AccountState(locked=False, lock_epoch=1),        # E-10: MFA fatigue, not locked
    "tholt": AccountState(locked=True, lock_epoch=1),           # X-08: flaky unlock target
    "pchen": AccountState(locked=True, lock_epoch=1),           # X-10: impossible-travel signal
}
FLAKY_UNLOCK_ACCOUNTS = {"tholt"}  # unlock_account reports success but doesn't clear the lock

RISK_SIGNALS: dict[str, dict] = {
    "avargas": {"compromise": False, "mfa_fatigue": True, "impossible_travel": False},
    "pchen": {"compromise": False, "mfa_fatigue": False, "impossible_travel": True},
}

CASES: dict[str, dict] = {}
FLAKY_DISPATCH_ASSETS = {"AST-3170"}  # create_case fails on the internal dispatch step

APPROVALS: dict[str, dict] = {}
_approval_counter = 0

GRANTS_MADE: list[dict] = []  # audit trail of actual AMBER grants, only ever populated post-approval

JIRA_STATE: dict[str, dict] = {
    tid: {**copy.deepcopy(t), "comments": [], "labels": [], "links": [], "_read_count": 0}
    for tid, t in TICKETS.items()
}

INCIDENTS: list[dict] = []
PAGES: list[dict] = []


def reset_state():
    """Reinitialize all mutable mock state from the original seeds.

    Used by eval/run_eval.py to run the same ticket repeatedly from a clean
    slate (stability/adversarial-robustness passes) without idempotency-cache
    replay or prior mutations from an earlier run leaking into the next one.
    """
    global ACCOUNTS, RISK_SIGNALS, CASES, APPROVALS, _approval_counter
    global GRANTS_MADE, JIRA_STATE, INCIDENTS, PAGES, _IDEMPOTENCY_CACHE
    ACCOUNTS = {
        "mchen": AccountState(locked=True, lock_epoch=1),
        "avargas": AccountState(locked=False, lock_epoch=1),
        "tholt": AccountState(locked=True, lock_epoch=1),
        "pchen": AccountState(locked=True, lock_epoch=1),
    }
    RISK_SIGNALS = {
        "avargas": {"compromise": False, "mfa_fatigue": True, "impossible_travel": False},
        "pchen": {"compromise": False, "mfa_fatigue": False, "impossible_travel": True},
    }
    CASES = {}
    APPROVALS = {}
    _approval_counter = 0
    GRANTS_MADE = []
    JIRA_STATE = {
        tid: {**copy.deepcopy(t), "comments": [], "labels": [], "links": [], "_read_count": 0}
        for tid, t in TICKETS.items()
    }
    INCIDENTS = []
    PAGES = []
    _IDEMPOTENCY_CACHE = {}


# ---------------------------------------------------------------------------
# JIRA workflow — GREEN
# ---------------------------------------------------------------------------

# Tool 1: jira.get — reads a ticket's current state (status, comments, labels, links)
def jira_get(ticket_id: str) -> dict:
    t = JIRA_STATE.get(ticket_id)
    if not t:
        return None
    t["_read_count"] += 1
    # Simulates the ticket state changing between the agent's decision and
    # its execution step (§6.3: "re-read state immediately before executing").
    if t.get("withdraw_on_reread") and t["_read_count"] >= 2:
        t["status"] = "Withdrawn"
    return copy.deepcopy(t)


# Tool 2: jira.comment — posts a comment on a ticket (the requester-facing message)
def jira_comment(ticket_id: str, text: str, author: str = "agent") -> dict:
    JIRA_STATE[ticket_id]["comments"].append({"author": author, "text": text})
    return {"status": "success"}


# Tool 3: jira.transition — moves a ticket to a new status (e.g. Resolved, Pending Approval)
def jira_transition(ticket_id: str, status: str) -> dict:
    JIRA_STATE[ticket_id]["status"] = status
    return {"status": "success"}


# Tool 4: jira.add_label — adds a label to a ticket (e.g. needs-clarification)
def jira_add_label(ticket_id: str, label: str) -> dict:
    JIRA_STATE[ticket_id]["labels"].append(label)
    return {"status": "success"}


# Tool 5: jira.link_issues — links a ticket to another (used for duplicate mapping)
def jira_link_issues(ticket_id: str, other_ticket_id: str, relation: str = "duplicates") -> dict:
    JIRA_STATE[ticket_id]["links"].append({"ticket": other_ticket_id, "relation": relation})
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Directory — GREEN, read-only
# ---------------------------------------------------------------------------

# Tool 6: directory.lookup_user — read-only: looks up a user's directory record (manager, title, etc.)
def directory_lookup_user(username: str) -> dict | None:
    return _lookup_user(username)


# Tool 7: directory.verify_manager — read-only: confirms whether one user is another's manager on file
def directory_verify_manager(manager_username: str, employee_username: str) -> bool:
    return is_manager_of(manager_username, employee_username)


# ---------------------------------------------------------------------------
# Okta — GREEN
# ---------------------------------------------------------------------------

# Tool 8: okta.unlock_account — clears an account lockout (GREEN only when risk signals are clear)
@idempotent("okta.unlock_account")
def okta_unlock_account(user: str, idempotency_key: str) -> dict:
    _maybe_raise_transient("okta.unlock_account", idempotency_key)
    acct = ACCOUNTS.setdefault(user, AccountState())
    if user in FLAKY_UNLOCK_ACCOUNTS:
        return {"status": "success"}  # deliberately does NOT clear acct.locked
    acct.locked = False
    return {"status": "success"}


# Tool 9: okta.risk_signals — read-only: compromise/MFA-fatigue/impossible-travel flags + current lock status
def okta_risk_signals(user: str) -> dict:
    signals = RISK_SIGNALS.get(user, {"compromise": False, "mfa_fatigue": False, "impossible_travel": False})
    acct = ACCOUNTS.get(user, AccountState())
    return {**signals, "account_locked": acct.locked, "sessions_active": acct.sessions_active, "password_reset_pending": acct.password_reset_pending}


# Tool 10: okta.send_password_reset — emails a self-service reset link to the account owner
@idempotent("okta.send_password_reset")
def okta_send_password_reset(user: str, idempotency_key: str) -> dict:
    _maybe_raise_transient("okta.send_password_reset", idempotency_key)
    return {"status": "success", "reset_link_sent_to": USERS[user]["email"]}


# Tool 11: okta.revoke_sessions — containment: kills all active sessions for an account
@idempotent("okta.revoke_sessions")
def okta_revoke_sessions(user: str, idempotency_key: str) -> dict:
    _maybe_raise_transient("okta.revoke_sessions", idempotency_key)
    acct = ACCOUNTS.setdefault(user, AccountState())
    acct.sessions_active = False
    return {"status": "success"}


# Tool 12: okta.force_password_reset — containment: forces a password reset at next login
@idempotent("okta.force_password_reset")
def okta_force_password_reset(user: str, idempotency_key: str) -> dict:
    _maybe_raise_transient("okta.force_password_reset", idempotency_key)
    acct = ACCOUNTS.setdefault(user, AccountState())
    acct.password_reset_pending = True
    return {"status": "success", "forced_at_next_login": True}


# ---------------------------------------------------------------------------
# ServiceNow / Endpoint — GREEN
# ---------------------------------------------------------------------------

# Tool 13: servicenow.create_request — files a ServiceNow catalog request; files it, doesn't grant it
@idempotent("servicenow.create_request")
def servicenow_create_request(requester: str, item: str, fields: dict, idempotency_key: str) -> dict:
    _maybe_raise_transient("servicenow.create_request", idempotency_key)
    return {"status": "success", "request_id": f"REQ-{abs(hash((requester, item, idempotency_key))) % 10000}"}


# Tool 14: endpoint.grant_admin — time-bound local-admin elevation, capped at 60 min self-service
@idempotent("endpoint.grant_admin")
def endpoint_grant_admin(user: str, minutes: int, idempotency_key: str) -> dict:
    if minutes > 60:
        raise ValueError("endpoint.grant_admin: minutes must be <= 60 per POL-04 §4.6 — self-service cap")
    _maybe_raise_transient("endpoint.grant_admin", idempotency_key)
    return {"status": "success", "expires_in_minutes": minutes}


# ---------------------------------------------------------------------------
# Asset management — GREEN
# ---------------------------------------------------------------------------

# Tool 15: assetmgmt.create_case — opens a lost/stolen or offboarding asset case
@idempotent("assetmgmt.create_case")
def assetmgmt_create_case(asset_tag: str, case_type: str, fields: dict, idempotency_key: str) -> dict:
    _maybe_raise_transient("assetmgmt.create_case", idempotency_key)
    asset = ASSETS.get(asset_tag, {})
    case_id = f"CASE-{abs(hash((asset_tag, case_type, idempotency_key))) % 10000}"
    CASES[case_id] = {"asset_tag": asset_tag, "case_type": case_type, "fields": fields, "step": "created"}
    if asset_tag in FLAKY_DISPATCH_ASSETS:
        # Step 2 (return-kit dispatch) fails downstream — roll back rather than
        # leave a half-done case record sitting around (§6.4).
        del CASES[case_id]
        raise AssetCaseDispatchError(
            f"assetmgmt.create_case: case created but return-kit dispatch failed for {asset_tag}; rolled back"
        )
    CASES[case_id]["step"] = "dispatched"
    requires_sev2 = case_type == "lost_stolen" and asset.get("classification") == "Restricted"
    return {"status": "success", "case_id": case_id, "requires_sev2_escalation": requires_sev2}


# ---------------------------------------------------------------------------
# IAM — GREEN (routing) + AMBER (the actual privileged change)
# ---------------------------------------------------------------------------

# Tool 16: iam.create_approval — routes a privileged action to the right approver(s); returns an
# approval_id (the routing is GREEN, granting is not)
def iam_create_approval(action: dict, approvers: list[str], idempotency_key: str) -> dict:
    cache_key = ("iam.create_approval", idempotency_key)
    if cache_key in _IDEMPOTENCY_CACHE:
        return {**_IDEMPOTENCY_CACHE[cache_key], "idempotent_replay": True}
    global _approval_counter
    _approval_counter += 1
    approval_id = f"APR-{1000 + _approval_counter}"
    APPROVALS[approval_id] = {"action": action, "approvers": approvers, "status": "PENDING"}
    result = {"status": "success", "approval_id": approval_id}
    _IDEMPOTENCY_CACHE[cache_key] = result
    return {**result, "idempotent_replay": False}


# Tool 17: iam.get_approval — read-only: checks an approval record's status (PENDING/APPROVED/REJECTED)
def iam_get_approval(approval_id: str) -> dict | None:
    record = APPROVALS.get(approval_id)
    return copy.deepcopy(record) if record else None


def _approve_for_testing(approval_id: str):
    """Test/demo helper only — simulates a human approver acting out-of-band.
    The agent itself never calls this; it only ever calls iam.create_approval."""
    if approval_id in APPROVALS:
        APPROVALS[approval_id]["status"] = "APPROVED"


# Tool 18: iam.grant_access — AMBER, the actual privileged access grant; physically refuses to run
# without an APPROVED record
def iam_grant_access(user: str, access: str, approval_id: str) -> dict:
    record = APPROVALS.get(approval_id)
    if not record or record["status"] != "APPROVED":
        raise PrivilegedActionBlocked(
            f"iam.grant_access refused: approval {approval_id} is not an APPROVED record on file"
        )
    GRANTS_MADE.append({"user": user, "access": access, "approval_id": approval_id})
    return {"status": "success", "user": user, "access": access}


# Tool 19: okta.disable_mfa — AMBER, disables MFA on an account; same hard gate as iam.grant_access
def okta_disable_mfa(user: str, approval_id: str) -> dict:
    record = APPROVALS.get(approval_id)
    if not record or record["status"] != "APPROVED":
        raise PrivilegedActionBlocked(
            f"okta.disable_mfa refused: approval {approval_id} is not an APPROVED record on file"
        )
    GRANTS_MADE.append({"user": user, "access": "mfa_disabled", "approval_id": approval_id})
    return {"status": "success", "user": user}


# ---------------------------------------------------------------------------
# SOC — RED, escalation only
# ---------------------------------------------------------------------------

# Tool 20: soc.open_incident — RED, opens a security incident at a given severity
def soc_open_incident(sev: str, summary: str, idempotency_key: str) -> dict:
    cache_key = ("soc.open_incident", idempotency_key)
    if cache_key in _IDEMPOTENCY_CACHE:
        return {**_IDEMPOTENCY_CACHE[cache_key], "idempotent_replay": True}
    incident_id = f"INC-{2000 + len(INCIDENTS)}"
    INCIDENTS.append({"incident_id": incident_id, "sev": sev, "summary": summary})
    result = {"status": "success", "incident_id": incident_id}
    _IDEMPOTENCY_CACHE[cache_key] = result
    return {**result, "idempotent_replay": False}


# Tool 21: soc.page_oncall — RED, pages the on-call security team
def soc_page_oncall(team: str) -> dict:
    PAGES.append({"team": team})
    return {"status": "success", "paged_team": team}
