import argparse

import pytest

from app.cli import _schema, parser


def test_admin_commands_are_registered() -> None:
    command_parser = parser()
    subparsers = next(
        action
        for action in command_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert {
        "create-template",
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
