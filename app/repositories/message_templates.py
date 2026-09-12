from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message_template import MessageTemplate


class MessageTemplateRepository:
    async def get(
        self, session: AsyncSession, application_id: UUID, key: str, channel: str
    ) -> MessageTemplate | None:
        return await session.scalar(
            select(MessageTemplate).where(
                MessageTemplate.application_id == application_id,
                MessageTemplate.template_key == key,
                MessageTemplate.channel == channel,
            )
        )

    async def list_active(
        self, session: AsyncSession, application_id: UUID
    ) -> list[MessageTemplate]:
        return list(
            (
                await session.scalars(
                    select(MessageTemplate)
                    .where(
                        MessageTemplate.application_id == application_id,
                        MessageTemplate.status == "active",
                    )
                    .order_by(MessageTemplate.template_key)
                )
            ).all()
        )
