from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.whatsapp_outbound_message import OutboundMessage, OutboundMessageStatus


class OutboundMessageRepository:
    async def get_by_idempotency_key(
        self, session: AsyncSession, application_id: UUID, idempotency_key: str
    ) -> OutboundMessage | None:
        return await session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.application_id == application_id,
                OutboundMessage.idempotency_key == idempotency_key,
            )
        )

    async def reserve(self, session: AsyncSession, **values) -> tuple[OutboundMessage, bool]:
        statement = (
            insert(OutboundMessage)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_outbound_app_idempotency")
            .returning(OutboundMessage)
        )
        created = (await session.execute(statement)).scalar_one_or_none()
        if created is not None:
            return created, True
        existing = (
            await session.execute(
                select(OutboundMessage).where(
                    OutboundMessage.application_id == values["application_id"],
                    OutboundMessage.idempotency_key == values["idempotency_key"],
                )
            )
        ).scalar_one()
        return existing, False

    async def get_for_application(
        self, session: AsyncSession, application_id: UUID, message_id: UUID
    ) -> OutboundMessage | None:
        return await session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.id == message_id, OutboundMessage.application_id == application_id
            )
        )

    async def list_for_application(
        self, session: AsyncSession, application_id: UUID, *, filters: list, limit: int, offset: int
    ) -> tuple[list[OutboundMessage], int]:
        conditions = [OutboundMessage.application_id == application_id, *filters]
        query: Select = (
            select(OutboundMessage)
            .where(*conditions)
            .order_by(OutboundMessage.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await session.scalars(query)).all())
        total = await session.scalar(
            select(func.count()).select_from(OutboundMessage).where(*conditions)
        )
        return items, int(total or 0)

    async def mark_sent(
        self,
        session: AsyncSession,
        message_id: UUID,
        provider_message_id: str | None,
        timestamp: datetime,
    ) -> None:
        await session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.id == message_id)
            .values(
                status=OutboundMessageStatus.SENT,
                provider_message_id=provider_message_id,
                sent_at=timestamp,
                error_code=None,
                error_message=None,
            )
        )

    async def mark_error(
        self,
        session: AsyncSession,
        message_id: UUID,
        *,
        status: OutboundMessageStatus,
        error_code: str | None,
        error_message: str,
        timestamp: datetime,
    ) -> None:
        values = {"status": status, "error_code": error_code, "error_message": error_message[:1000]}
        if status == OutboundMessageStatus.FAILED:
            values["failed_at"] = timestamp
        await session.execute(
            update(OutboundMessage).where(OutboundMessage.id == message_id).values(**values)
        )


WhatsAppOutboundMessageRepository = OutboundMessageRepository
