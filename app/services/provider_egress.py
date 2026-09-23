"""The only place live provider clients are constructed from settings.

Provider credentials being configured is never, by itself, permission to reach
Meta or Advanta. `Settings.live_provider_sends_allowed` decides that: always
blocked in `test`, blocked in `development` unless
`APP_ALLOW_LIVE_PROVIDER_SENDS=true`, and allowed in `staging`/`production`
unless explicitly set to `false`. Both the HTTP API and the CLI build their
clients here, so neither can reach a provider when egress is blocked.
"""

import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.services.advanta_client import AdvantaClient
from app.services.whatsapp_client import WhatsAppCloudAPIClient

logger = logging.getLogger(__name__)


class LiveProviderEgressBlockedError(Exception):
    def __init__(self, environment: str) -> None:
        super().__init__(
            f"Live provider egress is disabled in the {environment} environment; "
            "set APP_ALLOW_LIVE_PROVIDER_SENDS=true in development to opt in"
        )


@dataclass(frozen=True, slots=True)
class ProviderClients:
    whatsapp: WhatsAppCloudAPIClient | None
    advanta: AdvantaClient | None


def require_live_provider_egress(settings: Settings) -> None:
    if not settings.live_provider_sends_allowed:
        raise LiveProviderEgressBlockedError(settings.environment.value)


def build_provider_clients(settings: Settings) -> ProviderClients:
    """Build live clients, or none at all when live egress is not permitted.

    Without clients the messaging service raises `ProviderUnavailableError`
    before persisting anything, so a blocked send never reaches a provider.
    """
    if not settings.live_provider_sends_allowed:
        logger.warning(
            "live_provider_egress_blocked",
            extra={"environment": settings.environment.value},
        )
        return ProviderClients(whatsapp=None, advanta=None)
    whatsapp = None
    if all(
        (
            settings.whatsapp_api_version,
            settings.whatsapp_phone_number_id,
            settings.whatsapp_access_token,
        )
    ):
        whatsapp = WhatsAppCloudAPIClient(
            api_version=settings.whatsapp_api_version,
            phone_number_id=settings.whatsapp_phone_number_id,
            access_token=settings.whatsapp_access_token,
        )
    advanta = None
    if settings.advanta_base_url and settings.advanta_api_key and settings.advanta_partner_id:
        advanta = AdvantaClient(
            base_url=settings.advanta_base_url,
            api_key=settings.advanta_api_key,
            partner_id=settings.advanta_partner_id,
            sender_id=settings.advanta_sender_id,
        )
    return ProviderClients(whatsapp=whatsapp, advanta=advanta)
