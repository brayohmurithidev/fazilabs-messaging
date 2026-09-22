import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Small JSON formatter suitable for application and platform logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for field in (
            "meta_message_id",
            "message_type",
            "outbound_status",
            "request_id",
            "application_id",
            "outbound_message_id",
            "idempotency_key",
            "channel",
            "status_from",
            "status_to",
            "claimed_by",
            "provider",
            "provider_message_id",
            "billing_account_id",
            "billing_reservation_id",
            "error_code",
            "reconciliation_method",
        ):
            if value := getattr(record, field, None):
                payload[field] = value
        return json.dumps(payload)


def configure_logging(log_level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)
