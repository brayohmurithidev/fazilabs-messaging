# Fazilabs Messaging Platform

Fazilabs Messaging Platform is a reusable, application-facing service for customer communications. Fazilabs systems authenticate independently, submit domain-neutral message requests, and receive platform-owned message IDs. WhatsApp Cloud API is the only implemented channel; SMS and email can be added behind the same messaging boundary later.

```text
School Management ─┐
Invoicing ─────────┼─> /api/v1/messages ─> MessagingService ─> WhatsApp/Meta
Other applications ┘
```

The service uses FastAPI, async SQLAlchemy 2, PostgreSQL/asyncpg, Alembic, HTTPX, Pydantic v2, uv, pytest, and Ruff. Existing signed Meta webhook verification, inbound persistence, inbound idempotency, and outbound Cloud API transport are preserved.

## Setup

Create a PostgreSQL database, copy `.env.example` to `.env`, and set:

```env
APP_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/whatsapp_agent
APP_WHATSAPP_VERIFY_TOKEN=...
APP_WHATSAPP_APP_SECRET=...
APP_WHATSAPP_ACCESS_TOKEN=...
APP_WHATSAPP_PHONE_NUMBER_ID=...
APP_WHATSAPP_API_VERSION=v26.0
APP_PUBLIC_WEBHOOK_BASE_URL=https://current-public-host.example
```

Meta credentials belong only to this platform. Calling-application keys are database records and must never be placed in global environment configuration.

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

OpenAPI is available at `/docs` and `/openapi.json`.

## Bootstrap an application

There is intentionally no public credential-creation endpoint. Run the administrative CLI:

```bash
uv run python -m app.cli create-application \
  --name "School Management System" \
  --slug school-management

uv run python -m app.cli create-api-key \
  --application school-management \
  --name development
```

The second command prints a `fzmsg_...` key once with the warning `Store this key now. It cannot be retrieved again.` Only its prefix and a salted scrypt hash are stored. Revoked keys fail immediately; disabled applications receive HTTP 403. API requests use `Authorization: Bearer <messaging-api-key>`.

## Send a text message

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages/text \
  -H 'Authorization: Bearer <messaging-api-key>' \
  -H 'Idempotency-Key: school:42:student:812:results:2026-term2' \
  -H 'Content-Type: application/json' \
  -d '{
    "channel": "whatsapp",
    "to": "254700000001",
    "text": "Your requested information is ready.",
    "metadata": {
      "source_type": "student",
      "source_id": "student-812",
      "school_id": "school-42"
    }
  }'
```

Only WhatsApp is accepted. Recipients are normalized to international digits, text is limited to 4,096 characters, metadata to 8 KiB, and idempotency keys to a constrained 8–200 character format. Production business-initiated WhatsApp conversations may require an approved Meta template; free-form text is intended for controlled testing and valid customer-service windows.

`Idempotency-Key` is unique per application in PostgreSQL. An identical retry returns the existing message and never calls Meta again. Reusing a key for a different canonical payload returns HTTP 409. The insert uses `ON CONFLICT`, so concurrency is protected by the database rather than an in-memory check.

Responses contain the platform UUID, application slug, normalized recipient, lifecycle status, and provider message ID. They never include credentials or raw provider responses.

## Query messages

```text
GET /api/v1/messages/{message_id}
GET /api/v1/messages?status=sent&channel=whatsapp&source_type=student&limit=50&offset=0
```

List filters include status, channel, source type/id, `created_from`, and `created_to`. An authenticated application can only read its own rows; cross-application records appear not found and never appear in lists.

## WhatsApp templates

Business-initiated WhatsApp communication generally requires a template approved by Meta. Template lifecycle is deliberately split:

```text
Create and obtain approval in Meta
→ map it to an application-owned internal key in Fazilabs Messaging
→ calling application sends using only the internal key
```

Meta owns creation, approval, rejection, category decisions, and provider names. This platform stores mappings, validates named parameters, constructs Cloud API payloads, and sends approved templates. It never creates or modifies templates in Meta.

Mappings are application-scoped, allowing different provider wording for the same internal key in different products. Create one after Meta approval:

```bash
uv run python -m app.cli create-template \
  --application school-management \
  --key student_results_ready \
  --channel whatsapp \
  --provider meta \
  --provider-template-name fazi_student_results_ready_v1 \
  --language en_US \
  --billing-category utility \
  --parameter-schema '{"body":["parent_name","student_name","term"]}'
```

Only ordered BODY text parameters are supported. Calling applications provide named values; the platform converts them into Meta's positional order. Missing and extra parameters are rejected with HTTP 422. Non-empty header or button schemas are rejected because those component types are not yet implemented.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages/template \
  -H 'Authorization: Bearer <messaging-api-key>' \
  -H 'Idempotency-Key: results:student-812:2026-term2' \
  -H 'Content-Type: application/json' \
  -d '{
    "channel": "whatsapp",
    "to": "254700000001",
    "template": "student_results_ready",
    "parameters": {
      "parent_name": "Jane",
      "student_name": "Brian",
      "term": "Term 2"
    },
    "metadata": {
      "source_type": "student_result",
      "source_id": "result-001"
    }
  }'
```

Each outbound row snapshots the internal key, parameter values, provider template name, and language used. Later mapping changes therefore do not erase what was actually requested and sent. Exact retries use the existing application-scoped PostgreSQL idempotency constraint and never call Meta twice.

## Multi-tenant metering and prepaid billing

`MessagingApplication` identifies an integrating product and owns its API keys.
`BillingAccount` identifies one customer of that product. For example,
`school-management` is one application while School A, School B, and School C are
three application-scoped billing accounts. The stable `(application, external_id)`
pair prevents one application from resolving another application's customer.

Calling applications provide `billing_account` as the tenant's stable external ID.
They do not calculate prices, mutate balances, know Meta pricing or credentials, or
select provider template names. Requests that omit `billing_account` remain explicitly
unbilled legacy/internal traffic for backward compatibility. Historical outbound rows
are not assigned synthetic accounts.

Money is stored as integer minor units. `KES 100.00` is `10000` minor units. KES is
the only enabled currency in this phase. `wallet_transactions` is the immutable source
of truth: amounts are positive and the transaction type determines credit or debit.
Corrections require compensating entries. Cached `balance_minor` and `reserved_minor`
are maintained transactionally but do not replace the ledger.

Pricing is application-scoped and configurable by channel, message kind, explicit
template billing category, currency, price, status, and effective period. Active
periods for the same selector cannot overlap. Text requires an explicit text rule and
is never implicitly free. No production selling price or Meta cost is seeded.

The prepaid lifecycle is:

```text
lock BillingAccount
→ resolve one effective PricingRule
→ verify balance_minor - reserved_minor
→ reserve funds and outbound idempotency row atomically
→ call Meta after commit
→ on a valid provider message ID, atomically mark sent + record usage + debit wallet
```

The account-row lock serializes reservations, so active reservations and charges
cannot exceed prepaid funds. Confirmed provider rejection releases the reservation
without charging. Timeout or an ID-less/malformed success is `uncertain` and retains
the reservation. Delivery/read updates and webhook replays do not charge again. There
is no automatic refund for later delivery failure.

Insufficient funds return HTTP 402 with `insufficient_messaging_balance` before Meta
is called. A suspended account returns HTTP 403 without disabling other tenants.

Administrative mutations remain local CLI operations:

```bash
uv run python -m app.cli create-billing-account \
  --application school-management --external-id <school-uuid> \
  --name "Example Academy" --currency KES

uv run python -m app.cli credit-billing-account \
  --application school-management --external-id <school-uuid> \
  --amount 5000.00 --currency KES --reference manual-topup-001

uv run python -m app.cli create-pricing-rule \
  --application school-management --channel whatsapp --message-kind template \
  --billing-category utility --currency KES --price <configured-price>
```

Related commands are `list-billing-accounts`, `show-billing-account`,
`suspend-billing-account`, `activate-billing-account`, `show-balance`,
`list-wallet-transactions`, `list-pricing-rules`, `disable-pricing-rule`, and
`monthly-usage-summary --period YYYY-MM`. Top-up references are unique per account.

Authenticated applications have read-only access to:

```text
GET /api/v1/billing-accounts/{external_id}/balance
GET /api/v1/billing-accounts/{external_id}/usage?from=...&to=...&channel=whatsapp&billing_category=utility&limit=50&offset=0
```

Usage snapshots the customer price and pricing rule at provider acceptance. Provider
cost is a separate nullable field and may be reconciled later. The billing account is
part of the canonical idempotency payload: exact retries do not reserve, debit, meter,
or call Meta twice; changing it under the same key returns HTTP 409.

A School Management client therefore needs only the platform URL, application key,
school ID, recipient, internal template key, parameters, and idempotency key:

```python
await messaging.send_template(
    billing_account=str(school.id),
    template="student_results_ready",
    to=parent.phone,
    parameters={
        "parent_name": parent.name,
        "student_name": student.name,
        "term": term.name,
        "results_url": secure_url,
    },
    idempotency_key=f"results:{publication.id}:{parent.id}",
    metadata={"source_type": "student_result", "source_id": str(publication.id)},
)
```

Current limitations: no payment gateway, automated top-up, postpaid invoices,
VAT/tax engine, automated provider-cost reconciliation, dashboard, automatic
uncertain-reservation resolution, SMS, or email.

Active capabilities are available to the authenticated application at `GET /api/v1/templates`; provider names are omitted from that public response.

Template administration:

```bash
uv run python -m app.cli list-templates --application school-management
uv run python -m app.cli disable-template --application school-management \
  --key student_results_ready
uv run python -m app.cli enable-template --application school-management \
  --key student_results_ready
```

Suggested internal School Management keys are `student_results_ready`, `fee_payment_reminder`, `attendance_alert`, and `school_announcement`. Suggested invoicing keys are `invoice_created`, `payment_reminder`, and `payment_received`. These are recommendations only; no provider templates are created automatically.

## API-key lifecycle and safe rotation

```bash
uv run python -m app.cli list-api-keys --application school-management
uv run python -m app.cli create-api-key \
  --application school-management --name local-development-2
uv run python -m app.cli revoke-api-key \
  --application school-management --key <key-uuid-or-prefix>
```

Listings contain UUID, non-secret prefix, name, status, and timestamps—never raw keys or hashes. Revoking an already-revoked key is safe. Rotate without downtime by creating a replacement, updating the calling application, verifying it works, and only then revoking the old UUID or prefix. Creating a replacement never automatically revokes a working key.

## Local webhook readiness

Outbound Cloud API calls can succeed while inbound and delivery/read callbacks fail to reach a developer laptop. For complete local testing, run both:

```bash
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
ngrok http 8000
```

Set `APP_PUBLIC_WEBHOOK_BASE_URL` to the current externally reachable HTTPS origin and configure Meta's callback as:

```text
https://CURRENT_PUBLIC_HOST/webhooks/whatsapp
```

An ngrok hostname may change between sessions. It is only a development option; production should use its permanent public HTTPS endpoint. Check local prerequisites without sending a message or changing Meta:

```bash
uv run python -m app.cli doctor
uv run python -m app.cli doctor --check-reachability
```

Reachability is distinct from service health, so `/health` remains backward compatible even when a local tunnel is unavailable.

## Webhook and lifecycle

Meta uses `GET/POST /webhooks/whatsapp`. Verification uses the configured verify token, and POST bodies require a valid `X-Hub-Signature-256` HMAC before parsing. Inbound messages are persisted idempotently and are never auto-replied to.

Status callbacks map Meta message IDs to outbound rows. The monotonic policy is `pending/uncertain → sent → delivered → read`; duplicate or older callbacks do nothing. `failed` is terminal and stores only a sanitized failure summary. Unknown provider IDs are safely acknowledged. Raw inbound webhook subsets remain stored where already justified; new outbound requests do not store raw provider responses.

## Uncertain-message reconciliation

`uncertain` means the provider call outcome could not be determined locally: Meta may
or may not have accepted it. Timeouts after request transmission, dropped connections,
malformed success responses, and success responses without a provider message ID are
ambiguous. This differs from pending, confirmed sent, delivered/read, and confirmed
failed states.

The platform never blindly resends an uncertain message. A resend could duplicate a
message that Meta accepted before the response was lost. Its prepaid reservation stays
active: total balance is unchanged, available balance excludes the reservation, and no
usage/debit exists.

Meta Cloud API does not provide this platform a reliable lookup by local message UUID
or idempotency key. The explicit provider capability is therefore “lookup unavailable.”
Resolution relies on a webhook that can be matched using a captured Meta message ID or
an operator reviewing external evidence. If no provider ID was captured, an otherwise
valid late webhook cannot be linked to the local row automatically.

Operator commands are:

```bash
uv run python -m app.cli list-uncertain-messages --application school-management
uv run python -m app.cli show-uncertain-message \
  --application school-management --message-id <uuid>
uv run python -m app.cli reconcile-uncertain-message \
  --application school-management --message-id <uuid> \
  --outcome unknown --reason provider_lookup_unavailable
uv run python -m app.cli reconcile-uncertain-message \
  --application school-management --message-id <uuid> \
  --outcome accepted --provider-message-id <wamid> --reason provider_console_confirmed
uv run python -m app.cli reconcile-uncertain-message \
  --application school-management --message-id <uuid> \
  --outcome rejected --reason provider_rejection_confirmed
uv run python -m app.cli release-uncertain-reservation \
  --application school-management --message-id <uuid> \
  --reason acceptance_could_not_be_confirmed
```

`APP_BILLING_UNCERTAIN_RECONCILE_AFTER_MINUTES` defaults to 1440. It only controls
stale discovery and release eligibility; it never schedules or automatically releases
anything. `--force` is an explicit operator override for a newer reservation.

Accepted resolution requires a provider message ID and atomically marks sent, charges
once, writes one usage row, and consumes the reservation. Rejected resolution marks
failed and releases without charging. Unknown resolution records an audit attempt and
keeps the hold. Manual release records a distinct `released` reconciliation status,
marks the external lifecycle failed, and is idempotent.

If a linkable callback proves acceptance after manual release, provider truth is still
recorded. The platform charges from currently available prepaid funds when possible.
If those funds are no longer available, it leaves the wallet non-negative, creates one
open `late_provider_acceptance_after_release` billing exception. The exception snapshots
the original amount and currency; later pricing-rule changes do not alter it. It does
not silently create postpaid debt or resend.

Billing exceptions are Fazilabs operator concerns and have no application-key write API.
They remain permanently auditable as `open`, `resolved_charged`, or `resolved_waived`:

```bash
uv run python -m app.cli list-billing-exceptions \
  --application school-management --status open
uv run python -m app.cli show-billing-exception \
  --application school-management --exception-id <uuid>
uv run python -m app.cli credit-billing-account \
  --application school-management --external-id <school-id> \
  --amount <major-units> --currency KES --reference <unique-topup-reference>
uv run python -m app.cli resolve-billing-exception \
  --application school-management --exception-id <uuid> \
  --resolution charge --reason wallet_replenished
uv run python -m app.cli resolve-billing-exception \
  --application school-management --exception-id <uuid> \
  --resolution waive --reason customer_service_waiver
```

A charge resolution locks and validates the provider-accepted outbound, exception,
released reservation, and wallet account; it then writes one historical-price usage row
and one debit in the same transaction. Insufficient available balance leaves the
exception open and makes no financial change. A waiver writes no usage or debit. Same-
resolution replays are idempotent, while attempts to reverse charged/waived history are
rejected. Wallet credits never settle exceptions automatically.

Operator SOP: (1) list open exceptions, (2) inspect the provider evidence and snapshot,
(3) decide charge or waive, (4) top up explicitly if charging requires funds, (5) run the
resolution command, and (6) verify exception status plus balance and monthly usage.
Current limitations are deliberate: no automatic settlement, partial adjustment, debt,
postpaid receivable, payment gateway, invoice, dashboard, or scheduler.

Reconciliation locks outbound message → reservation → billing account. Exception
resolution locks outbound message → exception → reservation → billing account. Final status,
usage, debit, reservation, audit, and exception changes share one local transaction.
Database uniqueness preserves one reservation, usage charge, debit reference, and
late-acceptance exception per outbound message. Webhook and CLI replays are safe.

## Migrations and checks

Historical revisions are unchanged. Revision `20260829_0006` adds application ownership, parameter schemas, provider/language fields, and outbound template audit snapshots without deleting existing messaging rows.

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic check
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Set `APP_TEST_DATABASE_URL` to an isolated database whose name contains `test` to
enable PostgreSQL idempotency and billing concurrency tests. Never use the development
`whatsapp_agent` database. One exact setup is:

```bash
createdb fazilabs_messaging_test
APP_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fazilabs_messaging_test \
  uv run alembic upgrade head
APP_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/fazilabs_messaging_test \
  uv run pytest tests/test_whatsapp_postgres.py tests/test_message_api_postgres.py \
  tests/test_billing_postgres.py
```

## Security and privacy

- Never log API keys, authorization headers, Meta tokens, message bodies, or full phone numbers.
- API keys are shown once, salted and strongly hashed at rest, and independently revocable.
- Meta access tokens are platform secrets, never caller credentials.
- Provider diagnostics are sanitized and length-limited.
- Payload and field limits provide an initial abuse boundary; application-level distributed rate limiting is intentionally deferred.
- Metadata is JSONB and domain-neutral. This service has no School, Student, Invoice, Parent, Customer, CRM, or end-user account models.

## Real application test (manual only)

First replace any exposed development API key using the safe rotation procedure above. Create and obtain Meta approval for one genuine template, then map its exact approved name/language with `create-template`. Start FastAPI and a reachable HTTPS tunnel, send one `/api/v1/messages/template` request with a unique idempotency key, and confirm `sent → delivered`. Open the message to optionally confirm `read_at`. Repeat the exact request and confirm the same platform UUID is returned with no duplicate delivery.

Do not run that external test from automated tests. SMS, email, an admin UI, distributed rate limiting, media headers, and template buttons remain future work.
