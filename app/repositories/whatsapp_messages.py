from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.whatsapp_message import WhatsAppInboundMessage
from app.schemas.whatsapp import InboundWhatsAppMessage


class WhatsAppMessageRepository:
    async def insert_inbound_messages(
        self,
        session: AsyncSession,
        messages: Sequence[InboundWhatsAppMessage],
    ) -> dict[str, UUID]:
        """Insert a batch atomically and map inserted Meta IDs to internal IDs."""
        if not messages:
            return {}

        values = [
            {
                "meta_message_id": message.meta_message_id,
                "sender_wa_id": message.sender_wa_id,
                "recipient_phone_number_id": message.recipient_phone_number_id,
                "message_type": message.message_type,
                "text_body": message.text_body,
                "message_timestamp": message.message_timestamp,
                "raw_payload": message.raw_payload,
            }
            for message in messages
        ]
        statement = (
            insert(WhatsAppInboundMessage)
            .values(values)
            .on_conflict_do_nothing(index_elements=[WhatsAppInboundMessage.meta_message_id])
            .returning(WhatsAppInboundMessage.meta_message_id, WhatsAppInboundMessage.id)
        )
        result = await session.execute(statement)
        return dict(result.tuples().all())
