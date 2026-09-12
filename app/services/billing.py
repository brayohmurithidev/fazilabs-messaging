import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import (
    BillingAccount,
    BillingReservation,
    MessageUsage,
    PricingRule,
    WalletTransaction,
)
from app.models.whatsapp_outbound_message import OutboundMessage

logger = logging.getLogger(__name__)


class BillingAccountNotFoundError(Exception):
    pass


class BillingAccountSuspendedError(Exception):
    pass


class PricingRuleNotFoundError(Exception):
    pass


class AmbiguousPricingRuleError(Exception):
    pass


class InsufficientBalanceError(Exception):
    pass


class CurrencyMismatchError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BillingQuote:
    account: BillingAccount
    pricing_rule: PricingRule


def normalize_currency(value: str) -> str:
    currency = value.strip().upper()
    if currency not in {"KES"}:
        raise ValueError("unsupported currency; currently supported: KES")
    return currency


def major_to_minor(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("amount must be a decimal number") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError("amount must be positive with at most two decimal places")
    return int(amount * 100)


class BillingService:
    async def quote_and_lock(
        self,
        session: AsyncSession,
        *,
        application_id: UUID,
        external_id: str,
        channel: str,
        message_kind: str,
        billing_category: str | None,
        at: datetime | None = None,
    ) -> BillingQuote:
        account = await session.scalar(
            select(BillingAccount)
            .where(
                BillingAccount.application_id == application_id,
                BillingAccount.external_id == external_id,
            )
            .with_for_update()
        )
        if account is None:
            raise BillingAccountNotFoundError
        logger.info(
            "billing_account_resolved",
            extra={"billing_account_id": str(account.id), "application_id": str(application_id)},
        )
        if account.status != "active":
            logger.info("billing_account_suspended", extra={"billing_account_id": str(account.id)})
            raise BillingAccountSuspendedError
        effective_at = at or datetime.now(UTC)
        rules = list(
            (
                await session.scalars(
                    select(PricingRule).where(
                        PricingRule.application_id == application_id,
                        PricingRule.channel == channel,
                        PricingRule.message_kind == message_kind,
                        PricingRule.billing_category.is_(None)
                        if billing_category is None
                        else PricingRule.billing_category == billing_category,
                        PricingRule.currency == account.currency,
                        PricingRule.status == "active",
                        PricingRule.effective_from <= effective_at,
                        or_(
                            PricingRule.effective_to.is_(None),
                            PricingRule.effective_to > effective_at,
                        ),
                    )
                )
            ).all()
        )
        if not rules:
            raise PricingRuleNotFoundError
        if len(rules) != 1:
            raise AmbiguousPricingRuleError
        rule = rules[0]
        if account.balance_minor - account.reserved_minor < rule.customer_price_minor:
            logger.info("insufficient_balance", extra={"billing_account_id": str(account.id)})
            raise InsufficientBalanceError
        return BillingQuote(account, rule)

    async def reserve(
        self, session: AsyncSession, quote: BillingQuote, outbound_message_id: UUID
    ) -> BillingReservation:
        quote.account.reserved_minor += quote.pricing_rule.customer_price_minor
        reservation = BillingReservation(
            billing_account_id=quote.account.id,
            outbound_message_id=outbound_message_id,
            pricing_rule_id=quote.pricing_rule.id,
            amount_minor=quote.pricing_rule.customer_price_minor,
            currency=quote.pricing_rule.currency,
        )
        session.add(reservation)
        logger.info(
            "billing_reservation_created",
            extra={
                "billing_account_id": str(quote.account.id),
                "outbound_message_id": str(outbound_message_id),
            },
        )
        return reservation

    async def charge(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        timestamp: datetime,
        *,
        allow_released: bool = False,
    ) -> bool:
        reservation = await session.scalar(
            select(BillingReservation)
            .where(BillingReservation.outbound_message_id == message.id)
            .with_for_update()
        )
        if reservation is None or reservation.status == "charged":
            return reservation is not None
        if reservation.status == "released" and not allow_released:
            raise RuntimeError("Cannot charge a released billing reservation")
        if reservation.status not in {"active", "released"}:
            raise RuntimeError("Cannot charge billing reservation")
        account = await session.scalar(
            select(BillingAccount)
            .where(BillingAccount.id == reservation.billing_account_id)
            .with_for_update()
        )
        if account is None:
            raise RuntimeError("Billing account disappeared while reservation was active")
        if reservation.status == "active":
            account.reserved_minor -= reservation.amount_minor
        elif account.balance_minor - account.reserved_minor < reservation.amount_minor:
            return False
        account.balance_minor -= reservation.amount_minor
        reservation.status = "charged"
        reservation.finalized_at = timestamp
        session.add(
            MessageUsage(
                application_id=message.application_id,
                billing_account_id=account.id,
                outbound_message_id=message.id,
                channel=message.channel,
                message_kind=message.message_kind,
                billing_category=message.billing_category,
                provider=message.provider,
                pricing_rule_id=reservation.pricing_rule_id,
                currency=reservation.currency,
                provider_cost_minor=None,
                customer_price_minor=reservation.amount_minor,
            )
        )
        session.add(
            WalletTransaction(
                billing_account_id=account.id,
                type="debit",
                amount_minor=reservation.amount_minor,
                currency=reservation.currency,
                reference_type="outbound_message",
                reference_id=str(message.id),
                description="Messaging usage charge",
            )
        )
        logger.info(
            "message_charge_recorded",
            extra={"billing_account_id": str(account.id), "outbound_message_id": str(message.id)},
        )
        return True

    async def release(self, session: AsyncSession, outbound_message_id: UUID) -> None:
        reservation = await session.scalar(
            select(BillingReservation)
            .where(BillingReservation.outbound_message_id == outbound_message_id)
            .with_for_update()
        )
        if reservation is None or reservation.status != "active":
            return
        account = await session.scalar(
            select(BillingAccount)
            .where(BillingAccount.id == reservation.billing_account_id)
            .with_for_update()
        )
        if account is None:
            raise RuntimeError("Billing account disappeared while reservation was active")
        account.reserved_minor -= reservation.amount_minor
        reservation.status = "released"
        reservation.finalized_at = datetime.now(UTC)

    async def credit(
        self,
        session: AsyncSession,
        account: BillingAccount,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
    ) -> int:
        locked = await session.scalar(
            select(BillingAccount).where(BillingAccount.id == account.id).with_for_update()
        )
        if locked is None:
            raise BillingAccountNotFoundError
        if currency != locked.currency:
            raise CurrencyMismatchError
        locked.balance_minor += amount_minor
        session.add(
            WalletTransaction(
                billing_account_id=locked.id,
                type="credit",
                amount_minor=amount_minor,
                currency=currency,
                reference_type="manual_topup",
                reference_id=reference,
                description="Manual administrator top-up",
            )
        )
        await session.flush()
        return locked.balance_minor

    async def balance_from_ledger(self, session: AsyncSession, account_id: UUID) -> int:
        credits = {"credit", "adjustment_credit"}
        value = await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (WalletTransaction.type.in_(credits), WalletTransaction.amount_minor),
                            else_=-WalletTransaction.amount_minor,
                        )
                    ),
                    0,
                )
            ).where(WalletTransaction.billing_account_id == account_id)
        )
        return int(value or 0)


def pricing_periods_overlap(left: PricingRule, right: PricingRule) -> bool:
    return bool(
        left.effective_from < (right.effective_to or datetime.max.replace(tzinfo=UTC))
        and right.effective_from < (left.effective_to or datetime.max.replace(tzinfo=UTC))
    )
