"""`dispatch-sweep` CLI: logging, exit status, and provider-egress gating.

The sweeper and database session are replaced with fakes and every live
provider client's network layer is a tripwire, so nothing here can reach a
provider or a database.
"""

import argparse
import json
import logging
import sys
from contextlib import asynccontextmanager

import pytest

from app import cli
from app.core.config import Environment, Settings
from app.core.logging import JsonFormatter
from app.services.advanta_client import AdvantaClient
from app.services.dispatch_sweeper import SweepResult
from app.services.whatsapp_client import WhatsAppCloudAPIClient

PROVIDER_CREDENTIALS = {
    "whatsapp_verify_token": "fake-verify-token",
    "whatsapp_app_secret": "fake-app-secret",
    "whatsapp_access_token": "fake-access-token",
    "whatsapp_phone_number_id": "000000000",
    "whatsapp_api_version": "v99.0",
    "advanta_base_url": "https://advanta.invalid",
    "advanta_api_key": "fake-advanta-key",
    "advanta_partner_id": "fake-partner",
}


def settings_for(environment: Environment, **overrides) -> Settings:
    values = PROVIDER_CREDENTIALS | overrides
    if environment is Environment.PRODUCTION:
        values.setdefault(
            "database_url", "postgresql+asyncpg://postgres:postgres@localhost:5432/production"
        )
    return Settings(_env_file=None, environment=environment, **values)


@pytest.fixture(autouse=True)
def provider_tripwire(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("live provider network dispatch must not happen")

    monkeypatch.setattr(WhatsAppCloudAPIClient, "_send", forbidden)
    monkeypatch.setattr(AdvantaClient, "_post", forbidden)


class FakeSweeper:
    instances: list["FakeSweeper"] = []
    error: Exception | None = None

    def __init__(self, messaging_service, **kwargs) -> None:
        self.messaging_service = messaging_service
        self.kwargs = kwargs
        FakeSweeper.instances.append(self)

    async def sweep_once(self, session):
        if FakeSweeper.error is not None:
            raise FakeSweeper.error
        return SweepResult(
            dispatched_from_pending=0,
            expired_submitting_to_uncertain=2,
            dispatch_outcomes={},
        )


@pytest.fixture
def fake_sweep(monkeypatch):
    FakeSweeper.instances = []
    FakeSweeper.error = None

    @asynccontextmanager
    async def session_factory():
        yield object()

    monkeypatch.setattr(cli, "DispatchSweeper", FakeSweeper)
    monkeypatch.setattr(cli, "async_session_factory", session_factory)
    return FakeSweeper


def json_events(caplog) -> list[dict]:
    formatter = JsonFormatter()
    return [json.loads(formatter.format(record)) for record in caplog.records]


@pytest.mark.asyncio
async def test_successful_sweep_logs_summary_with_zero_counts(monkeypatch, fake_sweep, caplog):
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(Environment.PRODUCTION))
    with caplog.at_level(logging.INFO):
        await cli.dispatch_sweep(argparse.Namespace())

    [summary] = [e for e in json_events(caplog) if e["message"] == "dispatch_sweep_completed"]
    assert summary["dispatched_from_pending"] == 0
    assert summary["expired_submitting_to_uncertain"] == 2
    assert summary["dispatch_outcomes"] == {}
    assert summary["sweeper_id"].startswith("sweeper-cli:")
    assert isinstance(summary["duration_ms"], int)
    assert summary["level"] == "INFO"


@pytest.mark.asyncio
async def test_production_sweep_uses_the_configured_live_clients(monkeypatch, fake_sweep):
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(Environment.PRODUCTION))
    await cli.dispatch_sweep(argparse.Namespace())

    [sweeper] = fake_sweep.instances
    assert isinstance(sweeper.messaging_service.client, WhatsAppCloudAPIClient)
    assert isinstance(sweeper.messaging_service.advanta_client, AdvantaClient)
    assert sweeper.messaging_service.claim_identity == sweeper.kwargs["sweeper_id"]


@pytest.mark.asyncio
async def test_operational_failure_exits_non_zero_without_leaking_detail(
    monkeypatch, fake_sweep, caplog
):
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(Environment.PRODUCTION))
    fake_sweep.error = ConnectionRefusedError("postgresql://dev:SECRET-PASSWORD@db/messaging")

    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exit_info:
        await cli.dispatch_sweep(argparse.Namespace())

    assert exit_info.value.code == 1
    [failure] = [e for e in json_events(caplog) if e["message"] == "dispatch_sweep_failed"]
    assert failure["level"] == "ERROR"
    assert failure["error_type"] == "ConnectionRefusedError"
    assert "SECRET-PASSWORD" not in caplog.text
    assert not any(e["message"] == "dispatch_sweep_completed" for e in json_events(caplog))


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [Environment.TEST, Environment.DEVELOPMENT])
async def test_blocked_environments_refuse_with_non_zero_exit(
    monkeypatch, fake_sweep, caplog, environment
):
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(environment))

    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exit_info:
        await cli.dispatch_sweep(argparse.Namespace())

    assert exit_info.value.code not in (0, None)
    assert fake_sweep.instances == []
    assert [e["message"] for e in json_events(caplog)] == ["dispatch_sweep_refused"]


@pytest.mark.asyncio
async def test_development_explicit_opt_in_is_structurally_allowed(monkeypatch, fake_sweep):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: settings_for(Environment.DEVELOPMENT, allow_live_provider_sends=True),
    )
    await cli.dispatch_sweep(argparse.Namespace())
    assert len(fake_sweep.instances) == 1


@pytest.mark.asyncio
async def test_production_explicitly_disabled_refuses(monkeypatch, fake_sweep):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: settings_for(Environment.PRODUCTION, allow_live_provider_sends=False),
    )
    with pytest.raises(SystemExit) as exit_info:
        await cli.dispatch_sweep(argparse.Namespace())
    assert exit_info.value.code not in (0, None)
    assert fake_sweep.instances == []


def test_main_configures_json_logging_before_running_the_command(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sys, "argv", ["app.cli", "dispatch-sweep"])
    monkeypatch.setattr(cli, "get_settings", lambda: settings_for(Environment.PRODUCTION))
    monkeypatch.setattr(cli, "configure_logging", lambda level: calls.append(f"logging:{level}"))

    async def fake_command(args):
        calls.append("command")

    monkeypatch.setattr(cli, "dispatch_sweep", fake_command)
    cli.main()
    assert calls == ["logging:INFO", "command"]
