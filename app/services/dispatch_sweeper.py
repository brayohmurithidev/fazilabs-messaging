import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.outbound_messages import OutboundMessageRepository
from app.services.messaging import MessagingService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    dispatched_from_pending: int
    expired_submitting_to_uncertain: int


class DispatchSweeper:
    """Recovers messages stranded by a process crash around provider submission.

    Two independent, narrowly-scoped responsibilities, matching the approved
    M0 design. Neither one is a reconciliation decision: this class only
    detects lifecycle facts (never claimed; claim lease expired) that the
    existing, evidence-based `ReconciliationService` remains responsible for
    resolving once a row reaches `uncertain`.

    1. Claim and dispatch `pending` rows nobody has claimed within the grace
       period. These are provably never submitted to a provider -- safe to
       submit now, exactly once, using the same guarded outcome handling as
       the synchronous request path.
    2. Move `submitting` rows whose claim lease has expired to `uncertain`.
       A provider submission may have happened; the row is NEVER resubmitted,
       NEVER auto-charged, and NEVER auto-released here.
    """

    def __init__(
        self,
        messaging_service: MessagingService,
        repository: OutboundMessageRepository | None = None,
        *,
        sweeper_id: str | None = None,
        pending_grace_seconds: int,
        claim_lease_seconds: int,
        batch_size: int,
    ) -> None:
        self.messaging_service = messaging_service
        self.repository = repository or OutboundMessageRepository()
        self.sweeper_id = sweeper_id or f"sweeper:{uuid4().hex[:12]}"
        self.pending_grace_seconds = pending_grace_seconds
        self.claim_lease_seconds = claim_lease_seconds
        self.batch_size = batch_size

    async def sweep_once(self, session: AsyncSession) -> SweepResult:
        dispatched = await self._sweep_abandoned_pending(session)
        expired = await self._sweep_expired_submitting(session)
        return SweepResult(
            dispatched_from_pending=dispatched, expired_submitting_to_uncertain=expired
        )

    async def _sweep_abandoned_pending(self, session: AsyncSession) -> int:
        older_than = datetime.now(UTC) - timedelta(seconds=self.pending_grace_seconds)
        async with session.begin():
            claimed = await self.repository.claim_abandoned_pending(
                session,
                older_than=older_than,
                claimed_by=self.sweeper_id,
                lease_seconds=self.claim_lease_seconds,
                limit=self.batch_size,
            )
        for message in claimed:
            logger.info(
                "outbound_claimed",
                extra={
                    "outbound_message_id": str(message.id),
                    "claimed_by": self.sweeper_id,
                    "status_from": "pending",
                    "status_to": "submitting",
                    "channel": message.channel,
                },
            )
            # Each dispatch opens and commits its own outcome transaction(s);
            # a crash partway through this loop leaves the remaining claimed
            # rows durably `submitting`, to be resolved by the next sweep's
            # expiry check -- never resubmitted.
            await self.messaging_service.dispatch_claimed_from_row(session, message)
        return len(claimed)

    async def _sweep_expired_submitting(self, session: AsyncSession) -> int:
        async with session.begin():
            expired = await self.repository.sweep_expired_submitting(session, limit=self.batch_size)
        for message in expired:
            logger.info(
                "outbound_lifecycle_detected",
                extra={
                    "outbound_message_id": str(message.id),
                    "status_from": "submitting",
                    "status_to": "uncertain",
                    "claimed_by": message.claimed_by,
                },
            )
        return len(expired)
