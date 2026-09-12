from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class BalanceResponse(BaseModel):
    billing_account: str
    currency: str
    balance_minor: int
    balance: str
    reserved_minor: int
    available_minor: int


class UsageItem(BaseModel):
    id: UUID
    outbound_message_id: UUID
    channel: str
    message_kind: str
    billing_category: str | None
    currency: str
    customer_price_minor: int
    created_at: datetime


class UsageResponse(BaseModel):
    billing_account: str
    currency: str
    total_messages: int
    total_customer_charge_minor: int
    items: list[UsageItem]
    limit: int
    offset: int
