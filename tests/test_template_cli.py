import argparse
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.cli as cli
from app.models.message_template import MessageTemplate
from app.models.messaging_application import MessagingApplication
from app.repositories.message_templates import MessageTemplateRepository


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class Session:
    def __init__(self, applications, templates):
        self.applications = applications
        self.templates = templates

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    def begin(self):
        return Transaction()

    async def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        values = set(statement.compile().params.values())
        if entity is MessagingApplication:
            return next(
                (application for slug, application in self.applications.items() if slug in values),
                None,
            )
        if entity is MessageTemplate:
            return next(
                (
                    template
                    for (application_id, key, channel), template in self.templates.items()
                    if application_id in values and key in values and channel in values
                ),
                None,
            )
        raise AssertionError(f"Unexpected entity: {entity}")

    async def scalars(self, statement):
        values = set(statement.compile().params.values())
        items = [
            mapping
            for (application_id, _key, _channel), mapping in self.templates.items()
            if application_id in values
        ]
        return SimpleNamespace(all=lambda: items)


class Factory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


def application(slug):
    return SimpleNamespace(id=uuid4(), slug=slug)


def template(application_id, **changes):
    values = {
        "id": uuid4(),
        "application_id": application_id,
        "template_key": "student_results_ready",
        "channel": "whatsapp",
        "provider": "meta",
        "provider_template_name": "student_results_ready",
        "language_code": "en",
        "description": "Body-only mapping",
        "parameter_schema": {"body": ["parent_name", "student_name", "term"]},
        "billing_category": "utility",
        "billing_mode": "customer",
        "status": "active",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def update_args(**changes):
    values = {
        "application": "demo-school",
        "key": "student_results_ready",
        "channel": "whatsapp",
        "provider": None,
        "provider_template_name": None,
        "language": None,
        "description": None,
        "parameter_schema": None,
        "billing_category": None,
        "billing_mode": None,
    }
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.mark.asyncio
async def test_update_template_adds_url_button_without_changing_identity(monkeypatch) -> None:
    app = application("demo-school")
    mapping = template(app.id)
    session = Session(
        {"demo-school": app},
        {(app.id, mapping.template_key, mapping.channel): mapping},
    )
    monkeypatch.setattr(cli, "async_session_factory", Factory(session))

    await cli.update_template(
        update_args(
            parameter_schema=(
                '{"body":["parent_name","student_name","term"],'
                '"buttons":[{"index":0,"type":"url","parameter":"results_path"}]}'
            )
        )
    )

    assert mapping.application_id == app.id
    assert mapping.template_key == "student_results_ready"
    assert mapping.channel == "whatsapp"
    assert mapping.parameter_schema == {
        "body": ["parent_name", "student_name", "term"],
        "buttons": [{"index": 0, "type": "url", "parameter": "results_path"}],
    }
    resolved = await MessageTemplateRepository().get(
        session, app.id, "student_results_ready", "whatsapp"
    )
    assert resolved is mapping
    assert resolved.parameter_schema == mapping.parameter_schema


@pytest.mark.asyncio
async def test_update_template_can_change_mapping_fields_but_not_identity(monkeypatch) -> None:
    app = application("demo-school")
    mapping = template(app.id)
    session = Session(
        {"demo-school": app},
        {(app.id, mapping.template_key, mapping.channel): mapping},
    )
    monkeypatch.setattr(cli, "async_session_factory", Factory(session))

    await cli.update_template(
        update_args(
            provider="meta",
            provider_template_name="student_results_ready",
            language="en",
            description="Dynamic URL mapping",
            billing_category="utility",
            billing_mode="platform",
        )
    )

    assert (mapping.application_id, mapping.template_key, mapping.channel) == (
        app.id,
        "student_results_ready",
        "whatsapp",
    )
    assert mapping.provider == "meta"
    assert mapping.provider_template_name == "student_results_ready"
    assert mapping.language_code == "en"
    assert mapping.description == "Dynamic URL mapping"
    assert mapping.billing_category == "utility"
    assert mapping.billing_mode == "platform"


@pytest.mark.asyncio
async def test_update_template_rejects_invalid_schema_before_mutation(monkeypatch) -> None:
    app = application("demo-school")
    mapping = template(app.id)
    session = Session(
        {"demo-school": app},
        {(app.id, mapping.template_key, mapping.channel): mapping},
    )
    monkeypatch.setattr(cli, "async_session_factory", Factory(session))

    with pytest.raises(SystemExit, match="Invalid parameter schema"):
        await cli.update_template(
            update_args(
                parameter_schema=(
                    '{"body":["results_path"],'
                    '"buttons":[{"index":0,"type":"url","parameter":"results_path"}]}'
                )
            )
        )
    assert mapping.parameter_schema == {"body": ["parent_name", "student_name", "term"]}


@pytest.mark.asyncio
async def test_update_template_rejects_missing_application(monkeypatch) -> None:
    monkeypatch.setattr(cli, "async_session_factory", Factory(Session({}, {})))
    with pytest.raises(SystemExit, match="Application not found: missing-app"):
        await cli.update_template(update_args(application="missing-app", language="en"))


@pytest.mark.asyncio
async def test_update_template_rejects_missing_or_other_application_template(monkeypatch) -> None:
    requested_app = application("demo-school")
    other_app = application("other-app")
    other_mapping = template(other_app.id)
    session = Session(
        {"demo-school": requested_app, "other-app": other_app},
        {(other_app.id, other_mapping.template_key, other_mapping.channel): other_mapping},
    )
    monkeypatch.setattr(cli, "async_session_factory", Factory(session))

    with pytest.raises(SystemExit, match="Template not found for application"):
        await cli.update_template(update_args(language="en_US"))
    assert other_mapping.language_code == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"provider": "other"}, "Only provider 'meta' is supported"),
        ({"provider_template_name": " "}, "provider template name must contain"),
        ({"language": "english"}, "language must be a Meta language code"),
        ({"billing_category": "other"}, "billing category must be"),
        ({"billing_mode": "other"}, "billing mode must be"),
    ],
)
async def test_update_template_rejects_invalid_mapping_fields(
    monkeypatch, changes, message
) -> None:
    app = application("demo-school")
    mapping = template(app.id)
    session = Session(
        {"demo-school": app},
        {(app.id, mapping.template_key, mapping.channel): mapping},
    )
    monkeypatch.setattr(cli, "async_session_factory", Factory(session))

    with pytest.raises(SystemExit, match=message):
        await cli.update_template(update_args(**changes))
    assert mapping.provider == "meta"
    assert mapping.provider_template_name == "student_results_ready"
    assert mapping.language_code == "en"
    assert mapping.billing_category == "utility"
    assert mapping.billing_mode == "customer"


def test_template_create_update_enable_disable_commands_remain_registered() -> None:
    command_parser = cli.parser()
    for command in ("create-template", "update-template", "enable-template", "disable-template"):
        parsed = command_parser.parse_args(
            [command, "--application", "demo-school", "--key", "student_results_ready"]
            + (
                [
                    "--provider-template-name",
                    "student_results_ready",
                    "--language",
                    "en",
                    "--billing-category",
                    "utility",
                ]
                if command == "create-template"
                else []
            )
        )
        assert parsed.command == command


def test_create_template_billing_mode_defaults_to_customer_and_accepts_platform() -> None:
    base = [
        "create-template",
        "--application",
        "demo-school",
        "--key",
        "password_reset",
        "--provider-template-name",
        "password_reset",
        "--language",
        "en",
        "--billing-category",
        "authentication",
    ]
    assert cli.parser().parse_args(base).billing_mode == "customer"
    assert cli.parser().parse_args([*base, "--billing-mode", "platform"]).billing_mode == "platform"
    with pytest.raises(SystemExit):
        cli.parser().parse_args([*base, "--billing-mode", "invalid"])


def test_sms_template_cli_accepts_provider_owned_configuration() -> None:
    args = cli.parser().parse_args(
        [
            "create-template",
            "--application",
            "demo",
            "--key",
            "generic_notice",
            "--channel",
            "sms",
            "--provider",
            "advanta",
            "--sms-body",
            "Hello {{name}}",
            "--sms-route",
            "transactional",
            "--parameter-schema",
            '{"body":["name"]}',
            "--billing-category",
            "utility",
        ]
    )
    assert args.channel == "sms"
    assert args.provider == "advanta"
    assert args.sms_route == "transactional"


@pytest.mark.asyncio
async def test_list_templates_displays_billing_mode(monkeypatch, capsys) -> None:
    app = application("demo-school")
    customer = template(app.id, billing_mode="customer")
    platform = template(
        app.id,
        template_key="password_reset",
        billing_category="authentication",
        billing_mode="platform",
    )
    session = Session(
        {"demo-school": app},
        {
            (app.id, customer.template_key, customer.channel): customer,
            (app.id, platform.template_key, platform.channel): platform,
        },
    )
    monkeypatch.setattr(cli, "async_session_factory", Factory(session))

    await cli.list_templates(argparse.Namespace(application="demo-school"))
    output = capsys.readouterr().out
    assert "billing_mode" in output
    assert "\tcustomer\t" in output
    assert "\tplatform\t" in output
