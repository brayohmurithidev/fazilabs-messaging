import argparse
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import cli
from app.cli import _schema, parser
from app.services.advanta_client import AdvantaAPIError, AdvantaBalance


def test_admin_commands_are_registered() -> None:
    command_parser = parser()
    subparsers = next(
        action
        for action in command_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert {
        "create-template",
        "update-template",
        "list-applications",
        "show-application",
        "require-application-billing",
        "allow-application-unbilled",
        "list-templates",
        "disable-template",
        "enable-template",
        "list-api-keys",
        "revoke-api-key",
        "doctor",
    }.issubset(subparsers.choices)


def test_cli_parameter_schema_validation() -> None:
    assert _schema('{"body":["parent_name","student_name"]}')["body"] == [
        "parent_name",
        "student_name",
    ]
    with pytest.raises(SystemExit, match="Invalid parameter schema"):
        _schema('{"header":["image"]}')
    assert _schema(
        '{"body":["parent_name"],"buttons":[{"index":0,"type":"url","parameter":"results_path"}]}'
    )["buttons"] == [{"index": 0, "type": "url", "parameter": "results_path"}]


@pytest.mark.asyncio
async def test_advanta_balance_cli_prints_only_parsed_credit(monkeypatch, capsys) -> None:
    class Client:
        def __init__(self, **kwargs):
            pass

        async def get_balance(self):
            return AdvantaBalance(Decimal("800.00"))

    monkeypatch.setattr(cli, "AdvantaClient", Client)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            advanta_base_url="https://advanta.invalid",
            advanta_api_key=object(),
            advanta_partner_id=object(),
            advanta_sender_id="FAZILABS",
        ),
    )
    await cli.advanta_balance(argparse.Namespace())
    assert capsys.readouterr().out == "Advanta SMS credit balance: 800.00\n"


@pytest.mark.asyncio
async def test_advanta_balance_cli_surfaces_sanitized_provider_error(monkeypatch) -> None:
    class Client:
        def __init__(self, **kwargs):
            pass

        async def get_balance(self):
            raise AdvantaAPIError("Advanta balance request failed: invalid credentials (code 1006)")

    monkeypatch.setattr(cli, "AdvantaClient", Client)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            advanta_base_url="https://advanta.invalid",
            advanta_api_key=object(),
            advanta_partner_id=object(),
            advanta_sender_id="FAZILABS",
        ),
    )
    with pytest.raises(SystemExit, match="invalid credentials"):
        await cli.advanta_balance(argparse.Namespace())
