"""Email templates for AR Copilot (plan §18)."""
from __future__ import annotations


def reminder_subject(invoice_id: str, currency: str, outstanding: float) -> str:
    return f"Reminder: Invoice {invoice_id} — Outstanding balance {currency} {outstanding:,.2f}"


def reminder_body(
    customer_name: str,
    invoice_id: str,
    due_date: str,
    currency: str,
    outstanding: float,
    invoice_link: str = "",
    company_name: str = "Demo Company Pvt Ltd",
) -> str:
    link_line = f"\nInvoice link: {invoice_link}\n" if invoice_link else ""
    return (
        f"Hello {customer_name},\n\n"
        "I hope you are well.\n\n"
        f"Our records show that invoice {invoice_id}, due on {due_date}, "
        f"has an outstanding balance of {currency} {outstanding:,.2f}.\n\n"
        "Could you please confirm the expected payment date? If payment has "
        "already been made, please disregard this reminder and share the payment "
        "reference so we can reconcile our records.\n"
        f"{link_line}\n"
        "Thank you,\n\n"
        f"{company_name}\n"
        "Accounts Receivable"
    )


def escalation_subject(invoice_id: str, reason: str = "Escalation") -> str:
    return f"[Escalation] Invoice {invoice_id} — {reason}"


def escalation_body(
    customer_name: str,
    invoice_id: str,
    due_date: str,
    currency: str,
    outstanding: float,
    reason: str = "",
    company_name: str = "Demo Company Pvt Ltd",
) -> str:
    return (
        f"Dear {customer_name},\n\n"
        f"This is regarding invoice {invoice_id}, due on {due_date}, "
        f"with an outstanding balance of {currency} {outstanding:,.2f}.\n\n"
        f"{reason}\n\n"
        "A member of our finance team will be in touch to discuss next steps.\n\n"
        "Regards,\n\n"
        f"{company_name}\n"
        "Accounts Receivable"
    )


def high_priority_subject(invoice_id: str) -> str:
    return f"Action requested: overdue invoice {invoice_id}"


def high_priority_body(
    customer_name: str,
    invoice_id: str,
    due_date: str,
    currency: str,
    outstanding: float,
    days_overdue: int,
    company_name: str = "Demo Company Pvt Ltd",
) -> str:
    return (
        f"Dear {customer_name},\n\n"
        f"Our records show invoice {invoice_id} for {currency} {outstanding:,.2f} "
        f"was due on {due_date} and is now {days_overdue} days overdue.\n\n"
        "Please arrange payment promptly or contact us if you believe "
        "this notice is in error.\n\n"
        "Regards,\n"
        f"{company_name}\n"
        "Accounts Receivable"
    )


def generate_draft(invoice: dict, priority_level: str,
                   company_name: str = "Demo Company Pvt Ltd") -> tuple[str, str]:
    """Generate (subject, body) based on priority and invoice data."""
    name = invoice.get("name") or invoice.get("customer_name") or "Customer"
    inv_id = invoice.get("invoice_id", "")
    due = invoice.get("due_date", "")
    currency = invoice.get("currency", "INR")
    outstanding = float(invoice.get("outstanding_amount", invoice.get("amount", 0)))
    days_overdue = int(invoice.get("days_overdue", 0))
    link = invoice.get("invoice_link", "")

    if priority_level in ("High", "Critical"):
        subject = high_priority_subject(inv_id)
        body = high_priority_body(name, inv_id, due, currency, outstanding,
                                  days_overdue, company_name)
    else:
        subject = reminder_subject(inv_id, currency, outstanding)
        body = reminder_body(name, inv_id, due, currency, outstanding,
                             link, company_name)

    return subject, body
