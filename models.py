"""Pydantic request / data models for AR Copilot."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


# ── Workflow states (plan §11 + payment-safety §2) ──────────────────────────

class WorkflowState(str, Enum):
    NEW = "NEW"
    COLLECTED = "COLLECTED"
    ENRICHED = "ENRICHED"
    CLASSIFIED = "CLASSIFIED"
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    SENT = "SENT"
    SHEET_UPDATED = "SHEET_UPDATED"
    CLOSED = "CLOSED"
    RESOLVED_PENDING_RECONCILIATION = "RESOLVED_PENDING_RECONCILIATION"
    ESCALATED = "ESCALATED"
    MISSING_INFO = "MISSING_INFO"
    SEND_FAILED = "SEND_FAILED"
    BLOCKED = "BLOCKED"
    # Payment-safety additions
    PAYMENT_CLAIMED = "PAYMENT_CLAIMED"
    PAID_CONFIRMED = "PAID_CONFIRMED"
    DISPUTED = "DISPUTED"
    SEND_BLOCKED = "SEND_BLOCKED"
    FOLLOW_UP_SENT = "FOLLOW_UP_SENT"


# Hard-stop states: no reminder send is allowed from these
HARD_STOP_STATES = {
    WorkflowState.PAYMENT_CLAIMED,
    WorkflowState.RESOLVED_PENDING_RECONCILIATION,
    WorkflowState.PAID_CONFIRMED,
    WorkflowState.DISPUTED,
    WorkflowState.SEND_BLOCKED,
    WorkflowState.CLOSED,
    WorkflowState.ESCALATED,
    WorkflowState.BLOCKED,
}


class PaymentStatus(str, Enum):
    UNPAID = "UNPAID"
    PAYMENT_CLAIMED = "PAYMENT_CLAIMED"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    PAID_CONFIRMED = "PAID_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class PaymentVerification(str, Enum):
    VERIFIED_CLEAR = "VERIFIED_CLEAR_TO_CONTACT"
    PAID = "PAID"
    PENDING = "PENDING"
    FAILED = "FAILED"
    STALE = "STALE"
    CONFLICT = "CONFLICT"


class PriorityLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class DecisionAction(str, Enum):
    FOLLOW_UP = "FOLLOW_UP"
    RECONCILE_PAYMENT = "RECONCILE_PAYMENT"
    ESCALATE = "ESCALATE"
    MISSING_INFO = "MISSING_INFO"
    WAIT = "WAIT"
    HUMAN_REVIEW = "HUMAN_REVIEW"


# ── Strict base ──────────────────────────────────────────────────────────────

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ── LLM structured-output schemas ───────────────────────────────────────────

class PriorityResult(StrictModel):
    priority: Literal["high", "medium", "low"]
    reasoning: str = Field(min_length=5, max_length=500)


class DraftResult(StrictModel):
    subject: str = Field(min_length=3, max_length=180)
    body: str = Field(min_length=20, max_length=5000)


class ReplyClassification(StrictModel):
    intent: Literal[
        "paid", "payment_promised", "dispute",
        "incorrect_contact", "no_response", "other"
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(max_length=500)


# ── API request / response schemas ──────────────────────────────────────────

class DraftEdit(BaseModel):
    subject: str = Field(min_length=3, max_length=180)
    body: str = Field(min_length=20, max_length=5000)


class ApprovalRequest(BaseModel):
    approver: str = Field(default="manager", min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class RejectionRequest(BaseModel):
    approver: str = Field(default="manager", min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class EscalationRequest(BaseModel):
    approver: str = Field(default="manager", min_length=1, max_length=100)
    reason: str = Field(default="Escalated by manager", max_length=500)


class InvoiceStatusUpdate(BaseModel):
    status: Literal["PENDING", "PAID"]


class PaymentReconciliation(BaseModel):
    outcome: Literal["PAID_CONFIRMED", "PARTIAL_PAYMENT", "NOT_FOUND", "WRONG_INVOICE"]
    new_outstanding: float | None = None
    note: str = Field(default="", max_length=500)


# ── Settings (from .env) ────────────────────────────────────────────────────

class AppSettings(BaseModel):
    company_name: str = "Demo Company Pvt Ltd"
    high_value_threshold: float = 100_000
    max_follow_ups: int = 2
    reminder_interval_days: int = 7
    payment_grace_days: int = 3
    verification_freshness_seconds: int = 300  # 5 minutes
    email_sending_enabled: bool = False
    test_mode: bool = True
