import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Channel(StrEnum):
    WHATSAPP = "whatsapp"
    SMS = "sms"


class MessageStatus(StrEnum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class TextMessageRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "channel": "whatsapp",
                "to": "254700000000",
                "text": "Your requested information is ready.",
                "billing_account": "school-example-001",
                "metadata": {"source_type": "request", "source_id": "request-example-001"},
            }
        }
    )

    channel: Channel = Field(description="Must be `whatsapp`; free-form SMS is not supported.")
    to: str = Field(
        min_length=8,
        max_length=20,
        description="Recipient in international E.164 form; punctuation is normalized away.",
    )
    billing_account: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description=(
            "Application-scoped customer account identity. Required when application billing "
            "policy requires prepaid billing."
        ),
    )
    text: str = Field(min_length=1, max_length=4096, description="Nonblank WhatsApp text body.")
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional opaque source metadata, limited to 8 KiB.",
    )

    @field_validator("to")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized = re.sub(r"[\s()-]", "", value)
        if normalized.startswith("+"):
            normalized = normalized[1:]
        if not re.fullmatch(r"[1-9]\d{7,14}", normalized):
            raise ValueError("recipient must be an international E.164 number")
        return normalized

    @field_validator("channel")
    @classmethod
    def whatsapp_only(cls, value: Channel) -> Channel:
        if value is not Channel.WHATSAPP:
            raise ValueError("text messages currently support WhatsApp only")
        return value

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @model_validator(mode="after")
    def limit_metadata(self) -> "TextMessageRequest":
        if (
            self.metadata is not None
            and len(json.dumps(self.metadata, separators=(",", ":"), default=str).encode()) > 8192
        ):
            raise ValueError("metadata must not exceed 8 KiB")
        return self


class TemplateMessageRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "channel": "whatsapp",
                "to": "254700000001",
                "template": "student_results_ready",
                "parameters": {
                    "parent_name": "Amina",
                    "student_name": "Baraka",
                    "term": "Term 2",
                    "results_path": "example-access-token",
                },
                "billing_account": "school-example-001",
                "metadata": {
                    "source_type": "student_result",
                    "source_id": "result-example-001",
                },
            }
        }
    )

    channel: Channel = Field(description="Channel mapping to resolve: `whatsapp` or `sms`.")
    to: str = Field(
        min_length=8,
        max_length=20,
        description=(
            "Recipient. SMS accepts supported Kenyan 07/01, 2547/2541, or +2547/+2541 forms "
            "and stores canonical 254… form; WhatsApp expects international E.164 form."
        ),
    )
    billing_account: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description=(
            "Application-scoped attribution/funding account. Customer-funded mappings may require "
            "it; a supplied account never changes a platform-funded mapping into customer-funded."
        ),
    )
    template: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$",
        description="Semantic template key; the channel-specific provider mapping is server-side.",
    )
    parameters: dict[str, str] = Field(
        default_factory=dict,
        max_length=20,
        description=(
            "Named semantic values required by this channel mapping. Missing and extra names are "
            "rejected; raw provider component payloads are not accepted."
        ),
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Optional opaque source metadata, limited to 8 KiB.",
    )

    @model_validator(mode="after")
    def normalize_recipient(self) -> "TemplateMessageRequest":
        if self.channel is Channel.SMS:
            from app.services.sms import normalize_kenyan_sms_recipient

            self.to = normalize_kenyan_sms_recipient(self.to)
        else:
            self.to = TextMessageRequest.normalize_phone(self.to)
        return self

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, str]) -> dict[str, str]:
        for name, parameter in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise ValueError(f"invalid parameter name: {name}")
            if not parameter.strip():
                raise ValueError(f"parameter '{name}' must not be blank")
            if len(parameter) > 1024:
                raise ValueError(f"parameter '{name}' must not exceed 1024 characters")
        return value

    @model_validator(mode="after")
    def limit_metadata(self) -> "TemplateMessageRequest":
        if (
            self.metadata is not None
            and len(json.dumps(self.metadata, separators=(",", ":"), default=str).encode()) > 8192
        ):
            raise ValueError("metadata must not exceed 8 KiB")
        return self


class MessageResponse(BaseModel):
    id: UUID
    application: str
    channel: str
    status: MessageStatus = Field(
        description=(
            "Lifecycle state. `uncertain` means provider acceptance could not safely be proved or "
            "rejected; Messaging does not automatically resubmit it."
        )
    )
    recipient: str
    message_kind: str
    template: str | None
    provider_message_id: str | None
    source_type: str | None
    source_id: str | None
    metadata: dict[str, Any] | None
    created_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    failed_at: datetime | None
    sms_character_count: int | None = Field(
        default=None, description="Rendered SMS character count; null for WhatsApp."
    )
    sms_page_count: int | None = Field(
        default=None,
        description="Rendered SMS pages at 160 characters per page; null for WhatsApp.",
    )


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    limit: int
    offset: int
    total: int


class TemplateCapability(BaseModel):
    key: str
    channel: str
    language: str | None
    parameters: list[str]
    billing_mode: str = Field(
        description="`customer` uses customer-wallet funding; `platform` is funded by Fazilabs."
    )
    provider_route: str | None = Field(
        default=None,
        description="SMS route (`standard` or `transactional`); null for WhatsApp.",
    )


class TemplateCapabilityList(BaseModel):
    items: list[TemplateCapability]
