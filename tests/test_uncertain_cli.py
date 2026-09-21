import argparse
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import cli


class Factory:
    @asynccontextmanager
    async def __call__(self):
        yield SimpleNamespace()


def row(message_id, created_at):
    return (
        SimpleNamespace(
            id=message_id,
            status="uncertain",
            created_at=created_at,
            provider_message_id=None,
            reconciliation_status=None,
        ),
        SimpleNamespace(amount_minor=100, currency="KES", created_at=created_at),
        SimpleNamespace(external_id="demo-account"),
    )


@pytest.mark.asyncio
async def test_list_uncertain_defaults_to_all_recent_and_old_with_application_isolation(
    monkeypatch, capsys
) -> None:
    application_id = uuid4()
    other_application_id = uuid4()
    recent_id, old_id, other_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)

    class Service:
        async def list_uncertain(self, session, requested_application_id, **kwargs):
            assert requested_application_id == application_id
            assert requested_application_id != other_application_id
            return [row(old_id, now - timedelta(days=2)), row(recent_id, now)]

        async def list_stale(self, *args, **kwargs):
            raise AssertionError("default listing must not apply the stale filter")

    async def application(session, slug):
        assert slug == "demo-school"
        return SimpleNamespace(id=application_id)

    monkeypatch.setattr(cli, "async_session_factory", Factory())
    monkeypatch.setattr(cli, "_application", application)
    monkeypatch.setattr(cli, "ReconciliationService", Service)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(billing_uncertain_reconcile_after_minutes=1440),
    )
    await cli.list_uncertain_messages(
        argparse.Namespace(application="demo-school", stale_only=False, limit=50, offset=0)
    )
    output = capsys.readouterr().out
    assert str(old_id) in output
    assert str(recent_id) in output
    assert str(other_id) not in output
    assert "stale-only" not in output


@pytest.mark.asyncio
async def test_stale_only_excludes_recent_and_surfaces_threshold(monkeypatch, capsys) -> None:
    application_id = uuid4()
    recent_id, old_id = uuid4(), uuid4()
    now = datetime.now(UTC)

    class Service:
        async def list_uncertain(self, *args, **kwargs):
            raise AssertionError("stale-only must use the stale query")

        async def list_stale(self, session, requested_application_id, *, older_than, **kwargs):
            assert requested_application_id == application_id
            assert older_than < now
            return [row(old_id, now - timedelta(days=2))]

    async def application(session, slug):
        return SimpleNamespace(id=application_id)

    monkeypatch.setattr(cli, "async_session_factory", Factory())
    monkeypatch.setattr(cli, "_application", application)
    monkeypatch.setattr(cli, "ReconciliationService", Service)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(billing_uncertain_reconcile_after_minutes=1440),
    )
    await cli.list_uncertain_messages(
        argparse.Namespace(application="demo-school", stale_only=True, limit=50, offset=0)
    )
    output = capsys.readouterr().out
    assert str(old_id) in output
    assert str(recent_id) not in output
    assert "filter=stale-only" in output
    assert "threshold_minutes=1440" in output
    assert "cutoff=" in output


def test_uncertain_cli_registers_explicit_stale_only_flag() -> None:
    args = cli.parser().parse_args(
        ["list-uncertain-messages", "--application", "demo-school", "--stale-only"]
    )
    assert args.stale_only is True
