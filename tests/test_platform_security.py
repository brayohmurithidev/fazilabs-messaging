from app.core.api_keys import api_key_prefix, generate_api_key, verify_api_key
from app.models.message_template import MessageTemplate
from app.models.messaging_application import MessagingApiKey
from app.models.whatsapp_outbound_message import OutboundMessage


def test_api_key_is_one_way_hashed() -> None:
    raw, prefix, encoded = generate_api_key()
    assert raw not in encoded
    assert prefix == api_key_prefix(raw)
    assert verify_api_key(raw, encoded)
    assert not verify_api_key(raw + "wrong", encoded)


def test_api_key_model_has_no_plaintext_column() -> None:
    assert "key_hash" in MessagingApiKey.__table__.columns
    assert "raw_key" not in MessagingApiKey.__table__.columns
    assert "api_key" not in MessagingApiKey.__table__.columns


def test_idempotency_is_enforced_for_each_application_in_database() -> None:
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in OutboundMessage.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("application_id", "idempotency_key") in unique_columns


def test_template_keys_are_unique_within_application_and_channel() -> None:
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in MessageTemplate.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("application_id", "template_key", "channel") in unique_columns
