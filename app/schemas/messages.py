import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Channel(StrEnum):
    WHATSAPP = "whatsapp"


class TextMessageRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "channel": "whatsapp",
                "to": "254700000001",
                "text": "Your requested information is ready.",
                "metadata": {"source_type": "student", "source_id": "student-123"},
            }
        }
    )

    channel: Channel
    to: str = Field(min_length=8, max_length=20)
    billing_account: str | None = Field(default=None, min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=4096)
    metadata: dict[str, Any] | None = None

    @field_validator("to")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized = re.sub(r"[\s()-]", "", value)
        if normalized.startswith("+"):
            normalized = normalized[1:]
        if not re.fullmatch(r"[1-9]\d{7,14}", normalized):
            raise ValueError("recipient must be an international E.164 number")
        return normalized

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
                    "parent_name": "Jane",
                    "student_name": "Brian",
                    "term": "Term 2",
                },
                "metadata": {"source_type": "student_result", "source_id": "result-123"},
            }
        }
    )

    channel: Channel
    to: str = Field(min_length=8, max_length=20)
    billing_account: str | None = Field(default=None, min_length=1, max_length=160)
    template: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
    parameters: dict[str, str] = Field(default_factory=dict, max_length=20)
    metadata: dict[str, Any] | None = None

    @field_validator("to")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return TextMessageRequest.normalize_phone(value)

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
    status: str
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


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    limit: int
    offset: int
    total: int


class TemplateCapability(BaseModel):
    key: str
    channel: str
    language: str
    parameters: list[str]


class TemplateCapabilityList(BaseModel):
    items: list[TemplateCapability]
