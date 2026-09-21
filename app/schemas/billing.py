from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class BalanceResponse(BaseModel):
    billing_account: str
    currency: str
    balance_minor: int = Field(description="Ledger balance in the currency's minor unit.")
    balance: str = Field(description="Ledger balance formatted as a major-unit decimal string.")
    reserved_minor: int = Field(description="Funds held by active message reservations.")
    available_minor: int = Field(description="Balance minus active reservations, in minor units.")


class UsageItem(BaseModel):
    id: UUID
    outbound_message_id: UUID
    channel: str
    message_kind: str
    billing_category: str | None
    billing_mode: str = Field(description="Immutable `customer` or `platform` funding snapshot.")
    sms_page_count: int | None = Field(
        default=None, description="Accepted SMS page count; null for WhatsApp."
    )
    currency: str | None
    customer_price_minor: int = Field(description="Final customer charge in minor units.")
    created_at: datetime


class UsageResponse(BaseModel):
    billing_account: str
    currency: str
    total_messages: int
    total_customer_charge_minor: int
    items: list[UsageItem]
    limit: int
    offset: int
