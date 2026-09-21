import argparse
import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError

from app.core.api_keys import generate_api_key
from app.core.config import get_settings
from app.db.session import async_session_factory
from app.models.billing import (
    BillingAccount,
    BillingReservation,
    MessageUsage,
    PricingRule,
    WalletTransaction,
)
from app.models.message_template import MessageTemplate
from app.models.messaging_application import MessagingApiKey, MessagingApplication
from app.models.reconciliation import BillingException, MessageReconciliationAttempt
from app.models.whatsapp_outbound_message import OutboundMessage
from app.services.advanta_client import AdvantaAPIError, AdvantaClient
from app.services.billing import (
    BillingService,
    CurrencyMismatchError,
    major_to_minor,
    normalize_currency,
)
from app.services.billing_exceptions import (
    BillingExceptionInsufficientBalanceError,
    BillingExceptionNotFoundError,
    BillingExceptionResolutionConflictError,
    BillingExceptionService,
    InvalidBillingExceptionError,
)
from app.services.reconciliation import (
    InvalidReconciliationTransitionError,
    ProviderMessageIdRequiredError,
    ReconciliationNotFoundError,
    ReconciliationService,
    ReconciliationTooRecentError,
)
from app.services.sms import InvalidSmsError, sms_template_parameters
from app.services.template_parameters import (
    InvalidTemplateSchemaError,
    semantic_parameter_names,
    validate_parameter_schema,
)

SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TEMPLATE_KEY_PATTERN = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
EXTERNAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


async def _application(session, slug: str) -> MessagingApplication:
    application = await session.scalar(
        select(MessagingApplication).where(MessagingApplication.slug == slug)
    )
    if application is None:
        raise SystemExit(f"Application not found: {slug}")
    return application


async def create_application(args: argparse.Namespace) -> None:
    if not SLUG_PATTERN.fullmatch(args.slug):
        raise SystemExit("slug must contain lowercase letters, numbers, and single hyphens")
    async with async_session_factory() as session, session.begin():
        if await session.scalar(
            select(MessagingApplication).where(MessagingApplication.slug == args.slug)
        ):
            raise SystemExit(f"Application already exists: {args.slug}")
        session.add(
            MessagingApplication(name=args.name, slug=args.slug, description=args.description)
        )
    print(f"Created application: {args.slug}")


async def list_applications(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        applications = list(
            (
                await session.scalars(
                    select(MessagingApplication).order_by(MessagingApplication.slug)
                )
            ).all()
        )
    print("id\tslug\tname\tstatus\tbilling_required\tcreated_at")
    for application in applications:
        print(
            f"{application.id}\t{application.slug}\t{application.name}\t"
            f"{application.status}\t{application.billing_required}\t{application.created_at}"
        )


async def show_application(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        print(
            f"id={application.id}\nslug={application.slug}\nname={application.name}\n"
            f"description={application.description or '-'}\nstatus={application.status}\n"
            f"billing_required={application.billing_required}\n"
            f"created_at={application.created_at}\nupdated_at={application.updated_at}"
        )


async def set_application_billing_policy(args: argparse.Namespace) -> None:
    required = args.command == "require-application-billing"
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        application.billing_required = required
    policy = "billing-required" if required else "unbilled traffic allowed"
    print(f"Application {args.application} is now {policy}")


async def create_api_key(args: argparse.Namespace) -> None:
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        raw_key, prefix, key_hash = generate_api_key()
        session.add(
            MessagingApiKey(
                application_id=application.id, name=args.name, key_prefix=prefix, key_hash=key_hash
            )
        )
    print("Store this key now. It cannot be retrieved again.")
    print(raw_key)


async def list_api_keys(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        keys = list(
            (
                await session.scalars(
                    select(MessagingApiKey)
                    .where(MessagingApiKey.application_id == application.id)
                    .order_by(MessagingApiKey.created_at)
                )
            ).all()
        )
    print("id\tprefix\tname\tstatus\tcreated_at\tlast_used_at\trevoked_at")
    for key in keys:
        print(
            f"{key.id}\t{key.key_prefix}\t{key.name}\t{key.status}\t{key.created_at}\t"
            f"{key.last_used_at or '-'}\t{key.revoked_at or '-'}"
        )


async def revoke_api_key(args: argparse.Namespace) -> None:
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        key_uuid = None
        try:
            key_uuid = UUID(args.key)
        except ValueError:
            pass
        identity = (
            MessagingApiKey.id == key_uuid
            if key_uuid is not None
            else MessagingApiKey.key_prefix == args.key
        )
        key = await session.scalar(
            select(MessagingApiKey).where(
                MessagingApiKey.application_id == application.id, identity
            )
        )
        if key is None:
            raise SystemExit("API key not found for application")
        if key.status == "revoked":
            print(f"API key already revoked: {key.key_prefix}")
            return
        key.status = "revoked"
        key.revoked_at = datetime.now(UTC)
        print(f"Revoked API key: {key.key_prefix}")


def _schema(value: str) -> dict[str, Any]:
    try:
        return validate_parameter_schema(json.loads(value))
    except (json.JSONDecodeError, InvalidTemplateSchemaError) as exc:
        raise SystemExit(f"Invalid parameter schema: {exc}") from exc


def _validate_template_mapping_fields(
    *,
    channel: str,
    provider: str,
    provider_template_name: str | None,
    language: str | None,
    sms_body: str | None,
    sms_route: str | None,
    parameter_schema: dict[str, Any],
    billing_category: str,
    billing_mode: str,
) -> None:
    if channel == "whatsapp":
        if provider != "meta":
            raise SystemExit("Only provider 'meta' is supported for WhatsApp")
        if (
            not provider_template_name
            or not provider_template_name.strip()
            or len(provider_template_name) > 512
        ):
            raise SystemExit("provider template name must contain 1 to 512 characters")
        if not language or not re.fullmatch(r"[A-Za-z]{2,3}(?:_[A-Za-z]{2})?", language):
            raise SystemExit("language must be a Meta language code such as en or en_US")
        if sms_body is not None or sms_route is not None:
            raise SystemExit("WhatsApp templates must not specify SMS configuration")
    elif channel == "sms":
        if provider != "advanta":
            raise SystemExit("SMS templates require provider 'advanta'")
        if provider_template_name is not None or language is not None:
            raise SystemExit("SMS templates must not specify provider template name or language")
        if not sms_body or sms_route not in {"standard", "transactional"}:
            raise SystemExit("SMS templates require --sms-body and a valid --sms-route")
        try:
            if sms_template_parameters(sms_body) != parameter_schema["body"]:
                raise SystemExit("SMS placeholders must match declared body parameters in order")
        except InvalidSmsError as exc:
            raise SystemExit(str(exc)) from exc
        if parameter_schema["buttons"]:
            raise SystemExit("SMS templates do not support button parameters")
    else:
        raise SystemExit("channel must be whatsapp or sms")
    if billing_category not in {"utility", "authentication", "marketing"}:
        raise SystemExit("billing category must be utility, authentication, or marketing")
    if billing_mode not in {"customer", "platform"}:
        raise SystemExit("billing mode must be customer or platform")


async def create_template(args: argparse.Namespace) -> None:
    if len(args.key) > 100 or not TEMPLATE_KEY_PATTERN.fullmatch(args.key):
        raise SystemExit("template key must use lowercase words separated by underscores")
    parameter_schema = _schema(args.parameter_schema)
    _validate_template_mapping_fields(
        channel=args.channel,
        provider=args.provider,
        provider_template_name=args.provider_template_name,
        language=args.language,
        sms_body=args.sms_body,
        sms_route=args.sms_route,
        parameter_schema=parameter_schema,
        billing_category=args.billing_category,
        billing_mode=args.billing_mode,
    )
    try:
        async with async_session_factory() as session, session.begin():
            application = await _application(session, args.application)
            session.add(
                MessageTemplate(
                    application_id=application.id,
                    template_key=args.key,
                    channel=args.channel,
                    provider=args.provider,
                    provider_template_name=args.provider_template_name,
                    language_code=args.language,
                    sms_body=args.sms_body,
                    provider_route=args.sms_route,
                    description=args.description,
                    parameter_schema=parameter_schema,
                    billing_category=args.billing_category,
                    billing_mode=args.billing_mode,
                )
            )
    except IntegrityError as exc:
        raise SystemExit("Template already exists for this application and channel") from exc
    print(f"Created template mapping: {args.application}/{args.key}/{args.channel}")


async def update_template(args: argparse.Namespace) -> None:
    sms_body_arg = getattr(args, "sms_body", None)
    sms_route_arg = getattr(args, "sms_route", None)
    supplied = {
        "provider": args.provider,
        "provider_template_name": args.provider_template_name,
        "language_code": args.language,
        "description": args.description,
        "parameter_schema": args.parameter_schema,
        "billing_category": args.billing_category,
        "billing_mode": args.billing_mode,
        "sms_body": sms_body_arg,
        "provider_route": sms_route_arg,
    }
    if all(value is None for value in supplied.values()):
        raise SystemExit("At least one template field must be supplied for update")
    parameter_schema = _schema(args.parameter_schema) if args.parameter_schema is not None else None
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        template = await session.scalar(
            select(MessageTemplate).where(
                MessageTemplate.application_id == application.id,
                MessageTemplate.template_key == args.key,
                MessageTemplate.channel == args.channel,
            )
        )
        if template is None:
            raise SystemExit("Template not found for application")
        provider = args.provider if args.provider is not None else template.provider
        provider_template_name = (
            args.provider_template_name
            if args.provider_template_name is not None
            else template.provider_template_name
        )
        language = args.language if args.language is not None else template.language_code
        billing_category = (
            args.billing_category
            if args.billing_category is not None
            else template.billing_category
        )
        billing_mode = args.billing_mode if args.billing_mode is not None else template.billing_mode
        sms_body = sms_body_arg if sms_body_arg is not None else getattr(template, "sms_body", None)
        sms_route = (
            sms_route_arg
            if sms_route_arg is not None
            else getattr(template, "provider_route", None)
        )
        effective_schema = parameter_schema or template.parameter_schema
        _validate_template_mapping_fields(
            channel=template.channel,
            provider=provider,
            provider_template_name=provider_template_name,
            language=language,
            sms_body=sms_body,
            sms_route=sms_route,
            parameter_schema=effective_schema,
            billing_category=billing_category,
            billing_mode=billing_mode,
        )
        template.provider = provider
        template.provider_template_name = provider_template_name
        template.language_code = language
        template.billing_category = billing_category
        template.billing_mode = billing_mode
        template.sms_body = sms_body
        template.provider_route = sms_route
        if args.description is not None:
            template.description = args.description
        if parameter_schema is not None:
            template.parameter_schema = parameter_schema
    print(f"Updated template mapping: {args.application}/{args.key}/{args.channel}")


async def list_templates(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        templates = list(
            (
                await session.scalars(
                    select(MessageTemplate)
                    .where(MessageTemplate.application_id == application.id)
                    .order_by(MessageTemplate.template_key)
                )
            ).all()
        )
    print(
        "id\tkey\tchannel\tcategory\tbilling_mode\tprovider\tprovider_name\t"
        "language\troute\tstatus\tparameters"
    )
    for template in templates:
        names = semantic_parameter_names(template.parameter_schema)
        print(
            f"{template.id}\t{template.template_key}\t{template.channel}\t"
            f"{template.billing_category or '-'}\t{template.billing_mode}\t{template.provider}\t"
            f"{template.provider_template_name}\t"
            f"{template.language_code or '-'}\t{getattr(template, 'provider_route', None) or '-'}\t"
            f"{template.status}\t{','.join(names) or '-'}"
        )


async def set_template_status(args: argparse.Namespace) -> None:
    status = "active" if args.command == "enable-template" else "disabled"
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        template = await session.scalar(
            select(MessageTemplate).where(
                MessageTemplate.application_id == application.id,
                MessageTemplate.template_key == args.key,
                MessageTemplate.channel == args.channel,
            )
        )
        if template is None:
            raise SystemExit("Template not found for application")
        template.status = status
    print(f"Template {args.key} is now {status}")


async def _billing_account(session, application_id, external_id: str) -> BillingAccount:
    account = await session.scalar(
        select(BillingAccount).where(
            BillingAccount.application_id == application_id,
            BillingAccount.external_id == external_id,
        )
    )
    if account is None:
        raise SystemExit("Billing account not found for application")
    return account


async def create_billing_account(args: argparse.Namespace) -> None:
    if not EXTERNAL_ID_PATTERN.fullmatch(args.external_id):
        raise SystemExit("external ID contains unsupported characters or is too long")
    currency = normalize_currency(args.currency)
    try:
        async with async_session_factory() as session, session.begin():
            application = await _application(session, args.application)
            session.add(
                BillingAccount(
                    application_id=application.id,
                    external_id=args.external_id,
                    name=args.name,
                    currency=currency,
                )
            )
    except IntegrityError as exc:
        raise SystemExit("Billing account already exists for this application") from exc
    print(f"Created billing account: {args.application}/{args.external_id}")


async def list_billing_accounts(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        accounts = list(
            (
                await session.scalars(
                    select(BillingAccount)
                    .where(BillingAccount.application_id == application.id)
                    .order_by(BillingAccount.external_id)
                )
            ).all()
        )
    print("id\texternal_id\tname\tstatus\tmode\tcurrency\tbalance_minor\treserved_minor")
    for account in accounts:
        print(
            f"{account.id}\t{account.external_id}\t{account.name}\t{account.status}\t"
            f"{account.billing_mode}\t{account.currency}\t{account.balance_minor}\t"
            f"{account.reserved_minor}"
        )


async def show_billing_account(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        account = await _billing_account(session, application.id, args.external_id)
        print(
            f"id={account.id}\nexternal_id={account.external_id}\nname={account.name}\n"
            f"status={account.status}\nbilling_mode={account.billing_mode}\n"
            f"currency={account.currency}\nbalance_minor={account.balance_minor}\n"
            f"reserved_minor={account.reserved_minor}"
        )


async def set_billing_account_status(args: argparse.Namespace) -> None:
    new_status = "suspended" if args.command == "suspend-billing-account" else "active"
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        account = await _billing_account(session, application.id, args.external_id)
        account.status = new_status
    print(f"Billing account {args.external_id} is now {new_status}")


async def credit_billing_account(args: argparse.Namespace) -> None:
    try:
        amount_minor = major_to_minor(args.amount)
        currency = normalize_currency(args.currency)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        async with async_session_factory() as session, session.begin():
            application = await _application(session, args.application)
            account = await _billing_account(session, application.id, args.external_id)
            balance = await BillingService().credit(
                session,
                account,
                amount_minor=amount_minor,
                currency=currency,
                reference=args.reference,
            )
    except CurrencyMismatchError as exc:
        raise SystemExit("Credit currency does not match billing account currency") from exc
    except IntegrityError as exc:
        raise SystemExit("Top-up reference already used for this billing account") from exc
    print(f"Credited {amount_minor} minor units; resulting balance_minor={balance}")


async def show_balance(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        account = await _billing_account(session, application.id, args.external_id)
        print(
            f"{account.currency} balance_minor={account.balance_minor} "
            f"reserved_minor={account.reserved_minor} "
            f"available_minor={account.balance_minor - account.reserved_minor}"
        )


async def list_wallet_transactions(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        account = await _billing_account(session, application.id, args.external_id)
        entries = list(
            (
                await session.scalars(
                    select(WalletTransaction)
                    .where(WalletTransaction.billing_account_id == account.id)
                    .order_by(WalletTransaction.created_at.desc())
                    .limit(args.limit)
                )
            ).all()
        )
    print("id\ttype\tamount_minor\tcurrency\treference_type\treference_id\tcreated_at")
    for entry in entries:
        print(
            f"{entry.id}\t{entry.type}\t{entry.amount_minor}\t{entry.currency}\t"
            f"{entry.reference_type}\t{entry.reference_id}\t{entry.created_at}"
        )


def _date_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def create_pricing_rule(args: argparse.Namespace) -> None:
    if args.message_kind == "template" and args.billing_category is None:
        raise SystemExit("template pricing requires --billing-category")
    if args.message_kind == "text" and args.billing_category is not None:
        raise SystemExit("text pricing must not specify --billing-category")
    try:
        price_minor = major_to_minor(args.price)
        currency = normalize_currency(args.currency)
        start = _date_time(args.effective_from) if args.effective_from else datetime.now(UTC)
        end = _date_time(args.effective_to) if args.effective_to else None
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if end is not None and end <= start:
        raise SystemExit("effective-to must be after effective-from")
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        await session.execute(
            select(MessagingApplication)
            .where(MessagingApplication.id == application.id)
            .with_for_update()
        )
        overlap = await session.scalar(
            select(PricingRule.id).where(
                PricingRule.application_id == application.id,
                PricingRule.channel == args.channel,
                PricingRule.message_kind == args.message_kind,
                PricingRule.billing_category.is_(None)
                if args.billing_category is None
                else PricingRule.billing_category == args.billing_category,
                PricingRule.currency == currency,
                PricingRule.status == "active",
                or_(PricingRule.effective_to.is_(None), PricingRule.effective_to > start),
                True if end is None else PricingRule.effective_from < end,
            )
        )
        if overlap:
            raise SystemExit("An overlapping active pricing rule already exists")
        rule = PricingRule(
            application_id=application.id,
            channel=args.channel,
            message_kind=args.message_kind,
            billing_category=args.billing_category,
            currency=currency,
            customer_price_minor=price_minor,
            effective_from=start,
            effective_to=end,
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
    print(f"Created pricing rule: {rule_id}")


async def list_pricing_rules(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        rules = list(
            (
                await session.scalars(
                    select(PricingRule)
                    .where(PricingRule.application_id == application.id)
                    .order_by(PricingRule.effective_from.desc())
                )
            ).all()
        )
    print("id\tchannel\tkind\tcategory\tcurrency\tprice_minor\tstatus\tfrom\tto")
    for rule in rules:
        print(
            f"{rule.id}\t{rule.channel}\t{rule.message_kind}\t"
            f"{rule.billing_category or '-'}\t{rule.currency}\t{rule.customer_price_minor}\t"
            f"{rule.status}\t{rule.effective_from}\t{rule.effective_to or '-'}"
        )


async def advanta_balance(args: argparse.Namespace) -> None:
    settings = get_settings()
    if (
        not settings.advanta_base_url
        or not settings.advanta_api_key
        or not settings.advanta_partner_id
    ):
        raise SystemExit("Advanta provider is not configured")
    client = AdvantaClient(
        base_url=settings.advanta_base_url,
        api_key=settings.advanta_api_key,
        partner_id=settings.advanta_partner_id,
        sender_id=settings.advanta_sender_id,
    )
    try:
        result = await client.get_balance()
    except AdvantaAPIError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Advanta SMS credit balance: {result.display_credit}")


async def disable_pricing_rule(args: argparse.Namespace) -> None:
    async with async_session_factory() as session, session.begin():
        application = await _application(session, args.application)
        rule = await session.scalar(
            select(PricingRule).where(
                PricingRule.id == UUID(args.rule_id),
                PricingRule.application_id == application.id,
            )
        )
        if rule is None:
            raise SystemExit("Pricing rule not found for application")
        rule.status = "disabled"
    print(f"Disabled pricing rule: {args.rule_id}")


async def monthly_usage_summary(args: argparse.Namespace) -> None:
    try:
        start = datetime.strptime(args.period, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit("period must use YYYY-MM") from exc
    end = datetime(start.year + (start.month == 12), (start.month % 12) + 1, 1, tzinfo=UTC)
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        account = await _billing_account(session, application.id, args.external_id)
        rows = (
            await session.execute(
                select(
                    MessageUsage.channel,
                    MessageUsage.billing_category,
                    func.count(),
                    func.sum(MessageUsage.customer_price_minor),
                )
                .where(
                    MessageUsage.billing_account_id == account.id,
                    MessageUsage.created_at >= start,
                    MessageUsage.created_at < end,
                )
                .group_by(MessageUsage.channel, MessageUsage.billing_category)
            )
        ).all()
    print(f"Billing Account: {account.name}\nPeriod: {args.period}")
    total_count = total_charge = 0
    for channel, category, count, charge in rows:
        print(f"{channel.title()} {(category or 'text').title():<20} {count:>8}")
        total_count += count
        total_charge += charge
    print(
        f"Total messages {total_count}\nTotal charge {account.currency} {total_charge} minor units"
    )


async def list_uncertain_messages(args: argparse.Namespace) -> None:
    settings = get_settings()
    threshold = datetime.now(UTC) - timedelta(
        minutes=settings.billing_uncertain_reconcile_after_minutes
    )
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        service = ReconciliationService()
        if args.stale_only:
            rows = await service.list_stale(
                session,
                application.id,
                older_than=threshold,
                limit=args.limit,
                offset=args.offset,
            )
        else:
            rows = await service.list_uncertain(
                session, application.id, limit=args.limit, offset=args.offset
            )
    if args.stale_only:
        print(
            "filter=stale-only\t"
            f"threshold_minutes={settings.billing_uncertain_reconcile_after_minutes}\t"
            f"cutoff={threshold.isoformat()}"
        )
    print(
        "message_id\tbilling_account\tstatus\tamount_minor\tcurrency\tcreated_at\tprovider_id\treconciliation"
    )
    for message, reservation, account in rows:
        print(
            f"{message.id}\t{account.external_id if account else '-'}\t{message.status}\t"
            f"{reservation.amount_minor if reservation else 0}\t"
            f"{reservation.currency if reservation else '-'}\t"
            f"{reservation.created_at if reservation else message.created_at}\t"
            f"{message.provider_message_id or '-'}\t{message.reconciliation_status or '-'}"
        )


async def show_uncertain_message(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        row = (
            await session.execute(
                select(OutboundMessage, BillingReservation, BillingAccount)
                .outerjoin(
                    BillingReservation, BillingReservation.outbound_message_id == OutboundMessage.id
                )
                .outerjoin(
                    BillingAccount, BillingAccount.id == BillingReservation.billing_account_id
                )
                .where(
                    OutboundMessage.id == UUID(args.message_id),
                    OutboundMessage.application_id == application.id,
                    OutboundMessage.status == "uncertain",
                )
            )
        ).one_or_none()
        if row is None:
            raise SystemExit("Uncertain message not found for application")
        message, reservation, account = row
        attempts = list(
            (
                await session.scalars(
                    select(MessageReconciliationAttempt)
                    .where(MessageReconciliationAttempt.outbound_message_id == message.id)
                    .order_by(MessageReconciliationAttempt.created_at)
                )
            ).all()
        )
        exception = await session.scalar(
            select(BillingException).where(BillingException.outbound_message_id == message.id)
        )
    print(
        f"message_id={message.id}\napplication={args.application}\n"
        f"billing_account={account.external_id if account else '-'}\nstatus={message.status}\n"
        f"reconciliation_status={message.reconciliation_status or '-'}\n"
        f"reservation_status={reservation.status if reservation else '-'}\n"
        f"amount_minor={reservation.amount_minor if reservation else 0}\n"
        f"currency={reservation.currency if reservation else '-'}\n"
        f"created_at={reservation.created_at if reservation else message.created_at}\n"
        f"provider_message_id={message.provider_message_id or '-'}\n"
        f"billing_exception={exception.status if exception else '-'}"
    )
    for attempt in attempts:
        print(
            f"attempt={attempt.status}\tmethod={attempt.method}\treason={attempt.reason_code}\t"
            f"at={attempt.completed_at}"
        )


async def reconcile_uncertain_message(args: argparse.Namespace) -> None:
    if args.outcome == "accepted" and not args.provider_message_id:
        raise SystemExit("--provider-message-id is required for accepted outcome")
    try:
        async with async_session_factory() as session:
            application = await _application(session, args.application)
            application_id = application.id
            await session.rollback()
            message = await ReconciliationService().reconcile(
                session,
                application_id=application_id,
                message_id=UUID(args.message_id),
                outcome=args.outcome,
                reason=args.reason,
                provider_message_id=args.provider_message_id,
            )
    except ReconciliationNotFoundError as exc:
        raise SystemExit("Uncertain message not found for application") from exc
    except ProviderMessageIdRequiredError as exc:
        raise SystemExit("Provider message ID is required for accepted outcome") from exc
    except InvalidReconciliationTransitionError as exc:
        raise SystemExit("Message cannot transition to the requested outcome") from exc
    print(f"Reconciliation outcome recorded: {message.id} {message.reconciliation_status}")


async def release_uncertain_reservation(args: argparse.Namespace) -> None:
    settings = get_settings()
    try:
        async with async_session_factory() as session:
            application = await _application(session, args.application)
            application_id = application.id
            await session.rollback()
            message = await ReconciliationService().release_stale(
                session,
                application_id=application_id,
                message_id=UUID(args.message_id),
                reason=args.reason,
                threshold_minutes=settings.billing_uncertain_reconcile_after_minutes,
                force=args.force,
            )
    except ReconciliationNotFoundError as exc:
        raise SystemExit("Uncertain message not found for application") from exc
    except ReconciliationTooRecentError as exc:
        raise SystemExit("Reservation is newer than the configured safety threshold") from exc
    except InvalidReconciliationTransitionError as exc:
        raise SystemExit(
            "Only uncertain messages with active reservations can be released"
        ) from exc
    print(f"Uncertain reservation released: {message.id}")


async def list_billing_exceptions(args: argparse.Namespace) -> None:
    async with async_session_factory() as session:
        application = await _application(session, args.application)
        rows = await BillingExceptionService().list(
            session,
            application_id=application.id,
            status=args.status,
            limit=args.limit,
            offset=args.offset,
        )
    print(
        "exception_id\tbilling_account\tmessage_id\ttype\tamount_minor\tcurrency\tstatus\tcreated_at\tresolved_at"
    )
    for exception, account, message in rows:
        print(
            f"{exception.id}\t{account.external_id}\t{message.id}\t{exception.type}\t"
            f"{exception.amount_minor}\t{exception.currency}\t{exception.status}\t"
            f"{exception.created_at}\t{exception.resolved_at or '-'}"
        )


async def show_billing_exception(args: argparse.Namespace) -> None:
    try:
        async with async_session_factory() as session:
            application = await _application(session, args.application)
            exception, account, message, reservation = await BillingExceptionService().get(
                session,
                application_id=application.id,
                exception_id=UUID(args.exception_id),
            )
    except BillingExceptionNotFoundError as exc:
        raise SystemExit("Billing exception not found for application") from exc
    print(
        f"exception_id={exception.id}\napplication={args.application}\n"
        f"billing_account={account.external_id}\nmessage_id={message.id}\n"
        f"type={exception.type}\nstatus={exception.status}\n"
        f"amount_minor={exception.amount_minor}\ncurrency={exception.currency}\n"
        f"provider_message_id={message.provider_message_id or '-'}\n"
        f"reservation_status={reservation.status}\ncreated_at={exception.created_at}\n"
        f"exception_reason={exception.reason}\n"
        f"resolution_reason={exception.resolution_reason or '-'}\n"
        f"resolved_at={exception.resolved_at or '-'}"
    )


async def resolve_billing_exception(args: argparse.Namespace) -> None:
    try:
        async with async_session_factory() as session:
            application = await _application(session, args.application)
            application_id = application.id
            await session.rollback()
            exception = await BillingExceptionService().resolve(
                session,
                application_id=application_id,
                exception_id=UUID(args.exception_id),
                resolution=args.resolution,
                reason=args.reason,
            )
    except BillingExceptionNotFoundError as exc:
        raise SystemExit("Billing exception not found for application") from exc
    except BillingExceptionInsufficientBalanceError as exc:
        raise SystemExit("Insufficient available balance; exception remains open") from exc
    except BillingExceptionResolutionConflictError as exc:
        raise SystemExit("Billing exception already has a different terminal resolution") from exc
    except InvalidBillingExceptionError as exc:
        raise SystemExit("Billing exception is not eligible for this resolution") from exc
    print(f"Billing exception resolved: {exception.id} {exception.status}")


async def doctor(args: argparse.Namespace) -> None:
    settings = get_settings()
    checks: list[tuple[str, str]] = []
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        checks.append(("Database connection", "OK"))
        head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        checks.append(("Alembic revision", "OK" if revision == head else f"MISMATCH ({revision})"))
    except Exception as exc:
        checks.append(("Database connection", f"FAILED ({type(exc).__name__})"))
        checks.append(("Alembic revision", "NOT CHECKED"))
    for label, configured in (
        ("WhatsApp phone-number ID", settings.whatsapp_phone_number_id),
        ("WhatsApp access token", settings.whatsapp_access_token),
        ("WhatsApp app secret", settings.whatsapp_app_secret),
        ("WhatsApp verify token", settings.whatsapp_verify_token),
    ):
        checks.append((label, "configured" if configured else "NOT CONFIGURED"))
    public_url = settings.public_webhook_base_url
    checks.append(("Public webhook base URL", "configured" if public_url else "NOT CONFIGURED"))
    if public_url and args.check_reachability:
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                response = await client.get(f"{public_url.rstrip('/')}/health")
            state = f"reachable (HTTP {response.status_code})"
        except httpx.HTTPError as exc:
            state = f"UNREACHABLE ({type(exc).__name__})"
        checks.append(("Public health reachability", state))
    for label, result in checks:
        print(f"{label:<32} {result}")
    print(
        "Outbound sends may succeed without a public webhook URL, but inbound and "
        "delivery/read callbacks require Meta to reach /webhooks/whatsapp."
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Fazilabs Messaging administration")
    commands = result.add_subparsers(dest="command", required=True)
    application = commands.add_parser("create-application")
    application.add_argument("--name", required=True)
    application.add_argument("--slug", required=True)
    application.add_argument("--description")
    application.set_defaults(handler=create_application)
    application_list = commands.add_parser("list-applications")
    application_list.set_defaults(handler=list_applications)
    application_show = commands.add_parser("show-application")
    application_show.add_argument("--application", required=True)
    application_show.set_defaults(handler=show_application)
    for name in ("require-application-billing", "allow-application-unbilled"):
        billing_policy = commands.add_parser(name)
        billing_policy.add_argument("--application", required=True)
        billing_policy.set_defaults(handler=set_application_billing_policy)

    key = commands.add_parser("create-api-key")
    key.add_argument("--application", required=True)
    key.add_argument("--name", required=True)
    key.set_defaults(handler=create_api_key)
    key_list = commands.add_parser("list-api-keys")
    key_list.add_argument("--application", required=True)
    key_list.set_defaults(handler=list_api_keys)
    key_revoke = commands.add_parser("revoke-api-key")
    key_revoke.add_argument("--application", required=True)
    key_revoke.add_argument("--key", required=True, help="Key UUID or non-secret prefix")
    key_revoke.set_defaults(handler=revoke_api_key)

    template = commands.add_parser("create-template")
    template.add_argument("--application", required=True)
    template.add_argument("--key", required=True)
    template.add_argument("--channel", choices=("whatsapp", "sms"), default="whatsapp")
    template.add_argument("--provider", default="meta")
    template.add_argument("--provider-template-name")
    template.add_argument("--language")
    template.add_argument("--sms-body")
    template.add_argument("--sms-route", choices=("standard", "transactional"))
    template.add_argument("--description")
    template.add_argument("--parameter-schema", default='{"body": []}')
    template.add_argument(
        "--billing-category", choices=("utility", "authentication", "marketing"), required=True
    )
    template.add_argument("--billing-mode", choices=("customer", "platform"), default="customer")
    template.set_defaults(handler=create_template)
    template_update = commands.add_parser("update-template")
    template_update.add_argument("--application", required=True)
    template_update.add_argument("--key", required=True)
    template_update.add_argument("--channel", default="whatsapp", choices=("whatsapp", "sms"))
    template_update.add_argument("--provider", choices=("meta", "advanta"))
    template_update.add_argument("--provider-template-name")
    template_update.add_argument("--language")
    template_update.add_argument("--sms-body")
    template_update.add_argument("--sms-route", choices=("standard", "transactional"))
    template_update.add_argument("--description")
    template_update.add_argument("--parameter-schema")
    template_update.add_argument(
        "--billing-category", choices=("utility", "authentication", "marketing")
    )
    template_update.add_argument("--billing-mode", choices=("customer", "platform"))
    template_update.set_defaults(handler=update_template)
    template_list = commands.add_parser("list-templates")
    template_list.add_argument("--application", required=True)
    template_list.set_defaults(handler=list_templates)
    for name in ("disable-template", "enable-template"):
        template_status = commands.add_parser(name)
        template_status.add_argument("--application", required=True)
        template_status.add_argument("--key", required=True)
        template_status.add_argument("--channel", default="whatsapp")
        template_status.set_defaults(handler=set_template_status)

    billing_create = commands.add_parser("create-billing-account")
    billing_create.add_argument("--application", required=True)
    billing_create.add_argument("--external-id", required=True)
    billing_create.add_argument("--name", required=True)
    billing_create.add_argument("--currency", required=True)
    billing_create.set_defaults(handler=create_billing_account)
    billing_list = commands.add_parser("list-billing-accounts")
    billing_list.add_argument("--application", required=True)
    billing_list.set_defaults(handler=list_billing_accounts)
    billing_show = commands.add_parser("show-billing-account")
    billing_show.add_argument("--application", required=True)
    billing_show.add_argument("--external-id", required=True)
    billing_show.set_defaults(handler=show_billing_account)
    for name in ("suspend-billing-account", "activate-billing-account"):
        billing_status = commands.add_parser(name)
        billing_status.add_argument("--application", required=True)
        billing_status.add_argument("--external-id", required=True)
        billing_status.set_defaults(handler=set_billing_account_status)
    credit = commands.add_parser("credit-billing-account")
    credit.add_argument("--application", required=True)
    credit.add_argument("--external-id", required=True)
    credit.add_argument("--amount", required=True)
    credit.add_argument("--currency", required=True)
    credit.add_argument("--reference", required=True)
    credit.set_defaults(handler=credit_billing_account)
    balance = commands.add_parser("show-balance")
    balance.add_argument("--application", required=True)
    balance.add_argument("--external-id", required=True)
    balance.set_defaults(handler=show_balance)
    transactions = commands.add_parser("list-wallet-transactions")
    transactions.add_argument("--application", required=True)
    transactions.add_argument("--external-id", required=True)
    transactions.add_argument("--limit", type=int, choices=range(1, 101), default=50)
    transactions.set_defaults(handler=list_wallet_transactions)
    pricing_create = commands.add_parser("create-pricing-rule")
    pricing_create.add_argument("--application", required=True)
    pricing_create.add_argument("--channel", choices=("whatsapp", "sms"), default="whatsapp")
    pricing_create.add_argument("--message-kind", choices=("text", "template"), required=True)
    pricing_create.add_argument(
        "--billing-category", choices=("utility", "authentication", "marketing")
    )
    pricing_create.add_argument("--currency", required=True)
    pricing_create.add_argument("--price", required=True)
    pricing_create.add_argument("--effective-from")
    pricing_create.add_argument("--effective-to")
    pricing_create.set_defaults(handler=create_pricing_rule)
    pricing_list = commands.add_parser("list-pricing-rules")
    pricing_list.add_argument("--application", required=True)
    pricing_list.set_defaults(handler=list_pricing_rules)
    pricing_disable = commands.add_parser("disable-pricing-rule")
    pricing_disable.add_argument("--application", required=True)
    pricing_disable.add_argument("--rule-id", required=True)
    pricing_disable.set_defaults(handler=disable_pricing_rule)
    provider_balance = commands.add_parser("advanta-balance")
    provider_balance.set_defaults(handler=advanta_balance)
    summary = commands.add_parser("monthly-usage-summary")
    summary.add_argument("--application", required=True)
    summary.add_argument("--external-id", required=True)
    summary.add_argument("--period", required=True)
    summary.set_defaults(handler=monthly_usage_summary)
    uncertain_list = commands.add_parser("list-uncertain-messages")
    uncertain_list.add_argument("--application", required=True)
    uncertain_list.add_argument("--limit", type=int, choices=range(1, 101), default=50)
    uncertain_list.add_argument("--offset", type=int, default=0)
    uncertain_list.add_argument("--stale-only", action="store_true")
    uncertain_list.set_defaults(handler=list_uncertain_messages)
    uncertain_show = commands.add_parser("show-uncertain-message")
    uncertain_show.add_argument("--application", required=True)
    uncertain_show.add_argument("--message-id", required=True)
    uncertain_show.set_defaults(handler=show_uncertain_message)
    uncertain_reconcile = commands.add_parser("reconcile-uncertain-message")
    uncertain_reconcile.add_argument("--application", required=True)
    uncertain_reconcile.add_argument("--message-id", required=True)
    uncertain_reconcile.add_argument(
        "--outcome", choices=("accepted", "rejected", "unknown"), required=True
    )
    uncertain_reconcile.add_argument("--provider-message-id")
    uncertain_reconcile.add_argument("--reason", required=True)
    uncertain_reconcile.set_defaults(handler=reconcile_uncertain_message)
    uncertain_release = commands.add_parser("release-uncertain-reservation")
    uncertain_release.add_argument("--application", required=True)
    uncertain_release.add_argument("--message-id", required=True)
    uncertain_release.add_argument("--reason", required=True)
    uncertain_release.add_argument("--force", action="store_true")
    uncertain_release.set_defaults(handler=release_uncertain_reservation)
    exception_list = commands.add_parser("list-billing-exceptions")
    exception_list.add_argument("--application", required=True)
    exception_list.add_argument("--status", choices=("open", "resolved_charged", "resolved_waived"))
    exception_list.add_argument("--limit", type=int, choices=range(1, 101), default=50)
    exception_list.add_argument("--offset", type=int, default=0)
    exception_list.set_defaults(handler=list_billing_exceptions)
    exception_show = commands.add_parser("show-billing-exception")
    exception_show.add_argument("--application", required=True)
    exception_show.add_argument("--exception-id", required=True)
    exception_show.set_defaults(handler=show_billing_exception)
    exception_resolve = commands.add_parser("resolve-billing-exception")
    exception_resolve.add_argument("--application", required=True)
    exception_resolve.add_argument("--exception-id", required=True)
    exception_resolve.add_argument("--resolution", choices=("charge", "waive"), required=True)
    exception_resolve.add_argument("--reason", required=True)
    exception_resolve.set_defaults(handler=resolve_billing_exception)

    diagnostics = commands.add_parser("doctor")
    diagnostics.add_argument("--check-reachability", action="store_true")
    diagnostics.set_defaults(handler=doctor)
    return result


def main() -> None:
    args = parser().parse_args()
    asyncio.run(args.handler(args))


if __name__ == "__main__":
    main()
