"""Who may move money: deterministic rules from Finance. The LLM has no say here.

  - Auto-pay is allowed up to Rs 200 per dispute, at most once per rider per (IST) day.
  - Bigger or repeat disputes wait for ops approval.
  - Suspicious / unclear disputes are escalated, never paid automatically.
  - If PaySwift cannot be read we cannot reconcile, so a human approves.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings

AUTO_PAY = "auto_pay"
APPROVAL = "approval"
ESCALATE = "escalate"
NONE = "none"


@dataclass(frozen=True)
class AuthDecision:
    action: str
    reason_code: str
    reason: str

    def as_dict(self) -> dict:
        return {"action": self.action, "reason_code": self.reason_code, "reason": self.reason}


def authorize(
    *,
    owed: int,
    settings: Settings,
    auto_payments_today: int,
    repeat_of_paid_items: bool,
    payswift_verified: bool,
    suspicious_reason: str | None = None,
) -> AuthDecision:
    if owed <= 0:
        return AuthDecision(NONE, "nothing_owed", "No verified amount is owed.")
    if suspicious_reason:
        return AuthDecision(ESCALATE, "suspicious", suspicious_reason)
    if not payswift_verified:
        return AuthDecision(
            APPROVAL, "payswift_unverified",
            f"Rs {owed} owed, but PaySwift could not be read to reconcile earlier payouts; needs a human.",
        )
    if repeat_of_paid_items:
        return AuthDecision(
            APPROVAL, "repeat_dispute",
            f"Rs {owed} owed on items the desk already paid once (repeat dispute).",
        )
    if owed > settings.auto_pay_max_per_dispute:
        return AuthDecision(
            APPROVAL, "over_auto_limit",
            f"Rs {owed} is above the Rs {settings.auto_pay_max_per_dispute} auto-pay limit per dispute.",
        )
    if auto_payments_today >= settings.auto_pay_max_per_rider_per_day:
        return AuthDecision(
            APPROVAL, "daily_auto_limit",
            f"Rs {owed} owed, but this rider already had {auto_payments_today} automatic payment(s) today "
            f"(limit {settings.auto_pay_max_per_rider_per_day} per day).",
        )
    return AuthDecision(AUTO_PAY, "within_auto_limits", f"Rs {owed} verified and within auto-pay limits.")
