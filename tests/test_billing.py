import argparse

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes.messages import billing_http_error
from app.cli import parser
from app.schemas.messages import TemplateMessageRequest
from app.services.billing import (
    BillingAccountNotFoundError,
    BillingAccountSuspendedError,
    InsufficientBalanceError,
    PricingRuleNotFoundError,
    major_to_minor,
    normalize_currency,
)
from app.services.messaging import IdempotencyConflictError, canonical_payload_hash


def test_money_conversion_uses_exact_minor_units() -> None:
    assert major_to_minor("5000") == 500_000
    assert major_to_minor("4875.00") == 487_500
    for invalid in ("0", "-1", "1.001", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            major_to_minor(invalid)
    assert normalize_currency(" kes ") == "KES"
    with pytest.raises(ValueError):
        normalize_currency("KSh")


def test_billing_account_is_part_of_message_idempotency_identity() -> None:
    common = {
        "channel": "whatsapp",
        "to": "254700000001",
        "template": "student_results_ready",
        "parameters": {},
    }
    first = TemplateMessageRequest(**common, billing_account="school-a")
    second = TemplateMessageRequest(**common, billing_account="school-b")
    assert canonical_payload_hash(first) != canonical_payload_hash(second)


def test_billing_account_identifier_is_bounded() -> None:
    with pytest.raises(ValidationError):
        TemplateMessageRequest(
            channel="whatsapp",
            to="254700000001",
            billing_account="x" * 161,
            template="student_results_ready",
        )


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (BillingAccountNotFoundError(), 404, None),
        (BillingAccountSuspendedError(), 403, "billing_account_suspended"),
        (InsufficientBalanceError(), 402, "insufficient_messaging_balance"),
        (PricingRuleNotFoundError(), 409, None),
    ],
)
def test_billing_errors_are_safe_and_structured(error, status, code) -> None:
    response: HTTPException = billing_http_error(error)
    assert response.status_code == status
    if code:
        assert response.detail["code"] == code


def test_billing_cli_commands_are_registered() -> None:
    command_parser = parser()
    subparsers = next(
        action
        for action in command_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert {
        "create-billing-account",
        "list-billing-accounts",
        "show-billing-account",
        "suspend-billing-account",
        "activate-billing-account",
        "credit-billing-account",
        "show-balance",
        "list-wallet-transactions",
        "create-pricing-rule",
        "list-pricing-rules",
        "disable-pricing-rule",
        "monthly-usage-summary",
    }.issubset(subparsers.choices)


def test_idempotency_conflict_remains_a_distinct_domain_error() -> None:
    assert not isinstance(IdempotencyConflictError(), PricingRuleNotFoundError)
