import pytest
from pydantic import ValidationError

from app.schemas.messages import TemplateMessageRequest, TextMessageRequest


def test_phone_is_normalized() -> None:
    request = TextMessageRequest(channel="whatsapp", to="+254 700 000 001", text="hello")
    assert request.to == "254700000001"


@pytest.mark.parametrize("number", ["0700000000", "+123", "not-a-phone", "+0123456789"])
def test_invalid_phone_is_rejected(number: str) -> None:
    with pytest.raises(ValidationError):
        TextMessageRequest(channel="whatsapp", to=number, text="hello")


def test_unsupported_channel_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TextMessageRequest(channel="sms", to="254700000001", text="hello")


def test_text_and_metadata_limits() -> None:
    with pytest.raises(ValidationError):
        TextMessageRequest(channel="whatsapp", to="254700000001", text="x" * 4097)
    with pytest.raises(ValidationError):
        TextMessageRequest(
            channel="whatsapp", to="254700000001", text="hello", metadata={"large": "x" * 9000}
        )


def test_template_request_normalizes_phone_and_limits_values() -> None:
    request = TemplateMessageRequest(
        channel="whatsapp",
        to="+254 700 000 001",
        template="student_results_ready",
        parameters={"parent_name": "Jane"},
    )
    assert request.to == "254700000001"
    with pytest.raises(ValidationError):
        TemplateMessageRequest(
            channel="whatsapp",
            to="254700000001",
            template="student_results_ready",
            parameters={"parent_name": "x" * 1025},
        )


def test_template_request_rejects_invalid_key_channel_and_metadata() -> None:
    for changes in (
        {"template": "Invalid Template"},
        {"channel": "email"},
        {"metadata": {"large": "x" * 9000}},
    ):
        values = {
            "channel": "whatsapp",
            "to": "254700000001",
            "template": "student_results_ready",
            "parameters": {},
        }
        values.update(changes)
        with pytest.raises(ValidationError):
            TemplateMessageRequest(**values)
