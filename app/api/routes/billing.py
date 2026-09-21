from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedApplication
from app.db.session import get_db_session
from app.models.billing import BillingAccount, MessageUsage
from app.schemas.billing import BalanceResponse, UsageItem, UsageResponse

router = APIRouter(prefix="/api/v1/billing-accounts", tags=["Billing"])


def format_minor(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    absolute = abs(amount)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


async def _account(session: AsyncSession, application_id, external_id: str) -> BillingAccount:
    account = await session.scalar(
        select(BillingAccount).where(
            BillingAccount.application_id == application_id,
            BillingAccount.external_id == external_id,
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Billing account not found")
    return account


@router.get(
    "/{external_id}/balance",
    response_model=BalanceResponse,
    summary="Get a billing account balance",
    description=(
        "Returns the prepaid balance, active reservation total, and currently available amount "
        "for an account owned by the authenticated application. Monetary `*_minor` values use "
        "the currency's minor unit. Wallet mutation remains operator-only."
    ),
    response_description="Current prepaid wallet balance and reservations.",
    responses={
        401: {"description": "Missing or invalid application API key."},
        404: {"description": "Billing account not found for this application."},
    },
)
async def get_balance(
    external_id: str,
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> BalanceResponse:
    account = await _account(session, application.id, external_id)
    return BalanceResponse(
        billing_account=account.external_id,
        currency=account.currency,
        balance_minor=account.balance_minor,
        balance=format_minor(account.balance_minor),
        reserved_minor=account.reserved_minor,
        available_minor=account.balance_minor - account.reserved_minor,
    )


@router.get(
    "/{external_id}/usage",
    response_model=UsageResponse,
    summary="List billing account usage",
    description=(
        "Lists immutable accepted-message usage attributed to an application-owned billing "
        "account. The recorded billing mode distinguishes customer-funded from platform-funded "
        "usage. For SMS, `sms_page_count` records accepted billable pages."
    ),
    response_description="Paginated usage and customer-charge totals for the selected filters.",
    responses={
        401: {"description": "Missing or invalid application API key."},
        404: {"description": "Billing account not found for this application."},
    },
)
async def get_usage(
    external_id: str,
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
    channel: str | None = None,
    billing_category: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UsageResponse:
    account = await _account(session, application.id, external_id)
    filters = [MessageUsage.billing_account_id == account.id]
    if created_from is not None:
        filters.append(MessageUsage.created_at >= created_from)
    if created_to is not None:
        filters.append(MessageUsage.created_at < created_to)
    if channel is not None:
        filters.append(MessageUsage.channel == channel)
    if billing_category is not None:
        filters.append(MessageUsage.billing_category == billing_category)
    items = list(
        (
            await session.scalars(
                select(MessageUsage)
                .where(*filters)
                .order_by(MessageUsage.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    totals = (
        await session.execute(
            select(
                func.count(), func.coalesce(func.sum(MessageUsage.customer_price_minor), 0)
            ).where(*filters)
        )
    ).one()
    return UsageResponse(
        billing_account=account.external_id,
        currency=account.currency,
        total_messages=int(totals[0]),
        total_customer_charge_minor=int(totals[1]),
        items=[
            UsageItem(
                id=item.id,
                outbound_message_id=item.outbound_message_id,
                channel=item.channel,
                message_kind=item.message_kind,
                billing_category=item.billing_category,
                billing_mode=item.billing_mode,
                sms_page_count=item.sms_page_count,
                currency=item.currency,
                customer_price_minor=item.customer_price_minor,
                created_at=item.created_at,
            )
            for item in items
        ],
        limit=limit,
        offset=offset,
    )
