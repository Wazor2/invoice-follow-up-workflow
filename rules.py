"""Business rules engine for AR Copilot.

Priority scoring (plan §13), invoice eligibility (plan §12),
payment evidence rules, and decision logic.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from models import (
    DecisionAction, PaymentStatus, PaymentVerification,
    PriorityLevel, WorkflowState, HARD_STOP_STATES,
)


# ── Priority scoring (plan §13) ────────────────────────────────────────────

def calculate_priority_score(days_overdue: int, outstanding_amount: float,
                             previous_followups: int = 0) -> float:
    """priority_score = 2 × days_overdue + 0.02 × amount + 40 if followups ≥ 2"""
    score = 2 * days_overdue + 0.02 * outstanding_amount
    if previous_followups >= 2:
        score += 40
    return round(score, 2)


def priority_level_from_score(score: float) -> PriorityLevel:
    if score >= 131:
        return PriorityLevel.CRITICAL
    if score >= 71:
        return PriorityLevel.HIGH
    if score >= 31:
        return PriorityLevel.MEDIUM
    return PriorityLevel.LOW


def compute_priority(days_overdue: int, outstanding_amount: float,
                     previous_followups: int = 0) -> tuple[float, PriorityLevel, str]:
    """Returns (score, level, reasoning)."""
    score = calculate_priority_score(days_overdue, outstanding_amount, previous_followups)
    level = priority_level_from_score(score)
    reasoning = (
        f"Score {score}: 2×{days_overdue} days + 0.02×₹{outstanding_amount:,.0f}"
        + (f" + 40 (≥2 follow-ups)" if previous_followups >= 2 else "")
        + f" → {level.value}"
    )
    return score, level, reasoning


# ── Invoice eligibility (plan §12) ──────────────────────────────────────────

def is_eligible(invoice: dict) -> tuple[bool, str]:
    """Check if invoice is eligible for follow-up processing."""
    status = invoice.get("status", "").upper()
    if status not in ("PENDING", "PARTIALLY_PAID"):
        return False, f"Status is {status}; must be PENDING or PARTIALLY_PAID"

    outstanding = float(invoice.get("outstanding_amount", 0))
    if outstanding <= 0:
        return False, f"Outstanding amount is {outstanding}; must be > 0"

    due = invoice.get("due_date", "")
    if not due:
        return False, "No due_date set"

    try:
        due_date = date.fromisoformat(due)
    except (ValueError, TypeError):
        return False, f"Invalid due_date: {due}"

    if due_date >= date.today():
        return False, f"Due date {due} is today or in the future; not yet overdue"

    return True, "Invoice is overdue with positive balance"


def has_valid_email(email: str | None) -> bool:
    if not email or not email.strip():
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


# ── Payment evidence classification (payment-safety §8) ────────────────────

PAID_KEYWORDS = [
    "paid", "payment made", "transferred", "remitted",
    "utr", "transaction id", "receipt attached", "neft", "rtgs", "imps",
]
PROMISE_KEYWORDS = [
    "will pay", "payment by", "scheduled", "process payment",
    "will transfer", "paying tomorrow", "next week",
]
DISPUTE_KEYWORDS = [
    "incorrect", "wrong amount", "dispute", "duplicate invoice",
    "not received", "credit note", "overcharged", "billing error",
]


def classify_reply_text(text: str) -> tuple[str, float]:
    """Rule-based reply classifier. Returns (intent, confidence)."""
    lower = text.lower()

    paid_hits = sum(1 for kw in PAID_KEYWORDS if kw in lower)
    promise_hits = sum(1 for kw in PROMISE_KEYWORDS if kw in lower)
    dispute_hits = sum(1 for kw in DISPUTE_KEYWORDS if kw in lower)

    if paid_hits > 0 and paid_hits >= dispute_hits:
        return "paid", min(0.5 + paid_hits * 0.15, 0.95)
    if dispute_hits > 0:
        return "dispute", min(0.5 + dispute_hits * 0.15, 0.95)
    if promise_hits > 0:
        return "payment_promised", min(0.5 + promise_hits * 0.15, 0.90)

    return "no_response", 0.3


# ── Decision engine (plan §12 rules) ───────────────────────────────────────

def decide(invoice: dict, evidence: dict | None = None,
           high_value_threshold: float = 100_000,
           max_follow_ups: int = 2) -> tuple[DecisionAction, str]:
    """Decide the correct next action given invoice data and evidence.
    
    Returns (action, reason).
    """
    # Missing contact
    if not has_valid_email(invoice.get("email")):
        return DecisionAction.MISSING_INFO, "Customer email is absent or invalid"

    # Check payment evidence
    if evidence:
        intent = evidence.get("intent", "")
        if intent == "paid":
            return (DecisionAction.RECONCILE_PAYMENT,
                    f"Payment evidence found: {evidence.get('summary', 'customer claims payment')}")
        if intent == "dispute":
            return (DecisionAction.ESCALATE,
                    f"Dispute detected: {evidence.get('summary', 'customer disputes invoice')}")
        if intent == "payment_promised":
            return (DecisionAction.WAIT,
                    f"Payment promised: {evidence.get('summary', 'customer promises to pay')}")

    outstanding = float(invoice.get("outstanding_amount", 0))
    previous_followups = int(invoice.get("previous_followups", 0))

    # High-value rule
    if outstanding >= high_value_threshold:
        return (DecisionAction.ESCALATE,
                f"High-value invoice (₹{outstanding:,.0f} ≥ threshold ₹{high_value_threshold:,.0f}); requires senior approval")

    # Repeated follow-up rule
    if previous_followups >= max_follow_ups:
        return (DecisionAction.ESCALATE,
                f"{previous_followups} previous follow-ups (≥{max_follow_ups}); escalation required")

    return DecisionAction.FOLLOW_UP, "No payment, dispute, or blocking condition found; follow-up eligible"


# ── State-transition guard (payment-safety §6) ─────────────────────────────

# Valid transitions
VALID_TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"COLLECTED", "MISSING_INFO"},
    "COLLECTED": {"ENRICHED", "MISSING_INFO"},
    "ENRICHED": {"CLASSIFIED", "RESOLVED_PENDING_RECONCILIATION", "ESCALATED",
                 "PAYMENT_CLAIMED", "DISPUTED", "MISSING_INFO"},
    "CLASSIFIED": {"PLANNED", "AWAITING_APPROVAL", "RESOLVED_PENDING_RECONCILIATION",
                   "ESCALATED", "PAYMENT_CLAIMED", "DISPUTED", "MISSING_INFO"},
    "PLANNED": {"AWAITING_APPROVAL"},
    "AWAITING_APPROVAL": {"APPROVED", "REJECTED", "ESCALATED",
                          "RESOLVED_PENDING_RECONCILIATION", "PAYMENT_CLAIMED",
                          "DISPUTED", "SEND_BLOCKED", "MISSING_INFO"},
    "APPROVED": {"SENDING", "RESOLVED_PENDING_RECONCILIATION", "PAYMENT_CLAIMED",
                 "DISPUTED", "SEND_BLOCKED"},
    "SENDING": {"SENT", "SEND_FAILED", "SEND_BLOCKED"},
    "SENT": {"SHEET_UPDATED", "FOLLOW_UP_SENT", "CLOSED"},
    "SHEET_UPDATED": {"CLOSED", "FOLLOW_UP_SENT"},
    "FOLLOW_UP_SENT": {"CLOSED", "NEW"},  # Can start a new cycle
    "SEND_FAILED": {"SENDING", "SEND_BLOCKED", "ESCALATED"},
    # Hard-stop states generally don't transition to sending
    "PAYMENT_CLAIMED": {"RESOLVED_PENDING_RECONCILIATION", "PAID_CONFIRMED", "CLOSED"},
    "RESOLVED_PENDING_RECONCILIATION": {"PAID_CONFIRMED", "CLOSED",
                                        "AWAITING_APPROVAL"},  # if reconciliation shows not paid
    "PAID_CONFIRMED": {"CLOSED"},
    "DISPUTED": {"ESCALATED", "CLOSED", "AWAITING_APPROVAL"},
    "ESCALATED": {"CLOSED", "AWAITING_APPROVAL"},
    "SEND_BLOCKED": {"ESCALATED", "CLOSED", "AWAITING_APPROVAL"},
    "MISSING_INFO": {"COLLECTED", "CLOSED"},
    "BLOCKED": {"CLOSED", "COLLECTED"},
    "CLOSED": set(),
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    allowed = VALID_TRANSITIONS.get(from_state, set())
    return to_state in allowed


def can_send(case: dict) -> tuple[bool, str]:
    """Payment-safety §6: check all pre-send conditions."""
    state = case.get("state", "")
    if state not in ("APPROVED", "SENDING"):
        return False, f"State is {state}; must be APPROVED or SENDING"

    if case.get("approval_status") != "APPROVED":
        return False, f"Approval status is {case.get('approval_status')}; must be APPROVED"

    # Version check
    approved_ver = case.get("approved_version")
    gen_ver = case.get("reminder_generation_version", 1)
    if approved_ver is not None and approved_ver != gen_ver:
        return False, f"Approved version ({approved_ver}) != generation version ({gen_ver}); evidence changed"

    # Payment verification freshness
    pv_status = case.get("payment_verification_status", "")
    if pv_status != "VERIFIED_CLEAR_TO_CONTACT":
        return False, f"Payment verification is {pv_status}; must be VERIFIED_CLEAR_TO_CONTACT"

    # Payment hold
    hold = case.get("payment_hold_until")
    if hold:
        try:
            hold_date = datetime.fromisoformat(hold)
            if hold_date > datetime.now(hold_date.tzinfo):
                return False, f"Payment hold active until {hold}"
        except (ValueError, TypeError):
            pass

    return True, "All pre-send checks passed"
