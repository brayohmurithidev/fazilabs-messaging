# Fazilabs Messaging

Fazilabs Messaging is centralized messaging infrastructure for Fazilabs products. Consumer
applications submit semantic messages through one authenticated API; Messaging owns provider
integration, delivery state, and billing.

The service is built with FastAPI, SQLAlchemy/asyncpg, PostgreSQL, Alembic, HTTPX, Pydantic, and
Python 3.12 or later.

## Responsibility boundary

The consuming application owns:

- the business event and decision to notify;
- recipient selection and domain data;
- semantic template selection and named parameters;
- links or access tokens placed in template parameters;
- safe source metadata; and
- a stable idempotency identity for each logical send.

Fazilabs Messaging owns:

- Meta and Advanta integration and credentials;
- sender identities and channel/provider mappings;
- approved provider templates and SMS rendering;
- delivery webhooks and status;
- pricing, wallet reservations, charging, usage, and reconciliation.

**New template = configuration/data. New messaging capability = code.** Consumers never receive
Meta access tokens, WhatsApp Phone Number IDs, Advanta credentials, provider-template internals,
wallet internals, or reconciliation internals.

## Supported channels

| Channel | Provider | Public sends | Provider-specific behavior |
| --- | --- | --- | --- |
| WhatsApp | Meta WhatsApp Cloud API | Free-form text and approved semantic templates | BODY parameters, dynamic URL buttons, inbound events, and sent/delivered/read callbacks |
| SMS | Advanta | Semantic templates only | Kenyan number normalization, standard/transactional route, page analysis, DLR callbacks |

An SMS route and template billing mode are independent mapping properties. A transactional SMS can
be customer-funded, and a standard SMS can be platform-funded.

## API overview

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/` | Public | Service metadata |
| `GET` | `/health` | Public | Process liveness only |
| `GET` | `/ready` | Public | Application and PostgreSQL readiness |
| `POST` | `/api/v1/messages/text` | Application API key | Send WhatsApp text |
| `POST` | `/api/v1/messages/template` | Application API key | Send a WhatsApp or SMS template |
| `GET` | `/api/v1/messages/{message_id}` | Application API key | Get an application-owned message |
| `GET` | `/api/v1/messages` | Application API key | List application-owned messages |
| `GET` | `/api/v1/templates` | Application API key | List active semantic template capabilities |
| `GET` | `/api/v1/billing-accounts/{external_id}/balance` | Application API key | Read wallet balance and reservations |
| `GET` | `/api/v1/billing-accounts/{external_id}/usage` | Application API key | Read accepted-message usage |
| `GET`, `POST` | `/webhooks/whatsapp` | Provider-facing | Meta verification and events |
| `POST` | `/webhooks/advanta` | Provider-facing | Advanta delivery reports |

Application and API-key administration, wallet mutation, pricing, template mutation, and financial
reconciliation are CLI-only operator capabilities.

## Authentication

Protected routes require:

```text
Authorization: Bearer <application-api-key>
```

An operator creates an application key with:

```bash
uv run python -m app.cli create-api-key \
  --application local-demo \
  --name local-development
```

Messaging generates keys in the `fzmsg_<prefix>_<secret>` form. The raw value is printed only at
creation, belongs in the consuming application's secret environment, and must never be committed or
logged. Messaging stores only its secure hash. Use `list-api-keys` and `revoke-api-key` for rotation.

Billing accounts, messages, templates, and API keys are always scoped to the authenticated
application.

## Idempotency

Every send requires a caller-generated, non-secret `Idempotency-Key` header. It identifies one
logical business operation; Messaging does not generate it.

- The same key and identical semantic request returns the original message.
- The replay makes no second provider call, reservation, debit, or usage record.
- The same key with any changed request field returns HTTP `409`.
- After an uncertain HTTP outcome, retry with the original key and exact request.
- Do not generate a new random key on each transport retry.

Examples of caller-defined keys include `demo-results-001`,
`results:<publication_id>:<recipient_id>`, and `invoice:<invoice_id>:<recipient_id>`. They are not
secrets.

## Sending messages

Set local shell variables without committing their values:

```bash
export MESSAGING_BASE_URL=http://127.0.0.1:8000
export MESSAGING_API_KEY=<MESSAGING_API_KEY>
```

### WhatsApp text

`POST /api/v1/messages/text` supports WhatsApp only:

```bash
curl --fail-with-body "$MESSAGING_BASE_URL/api/v1/messages/text" \
  -H "Authorization: Bearer $MESSAGING_API_KEY" \
  -H "Idempotency-Key: demo-text-001" \
  -H "Content-Type: application/json" \
  --data '{
    "channel": "whatsapp",
    "to": "254700000000",
    "billing_account": "school-example-001",
    "text": "Your requested information is ready.",
    "metadata": {"source_type": "request", "source_id": "request-example-001"}
  }'
```

### WhatsApp template

```bash
curl --fail-with-body "$MESSAGING_BASE_URL/api/v1/messages/template" \
  -H "Authorization: Bearer $MESSAGING_API_KEY" \
  -H "Idempotency-Key: demo-results-whatsapp-001" \
  -H "Content-Type: application/json" \
  --data '{
    "channel": "whatsapp",
    "to": "254700000000",
    "template": "student_results_ready",
    "parameters": {
      "parent_name": "Amina",
      "student_name": "Baraka",
      "term": "Term 2",
      "results_path": "example-access-token"
    },
    "billing_account": "school-example-001",
    "metadata": {
      "source_type": "student_result",
      "source_id": "result-example-001"
    }
  }'
```

The fixed portion of a dynamic Meta URL is configured in the approved provider template. The
consumer supplies only the named dynamic value; it never constructs Meta component syntax.

### SMS template

```bash
curl --fail-with-body "$MESSAGING_BASE_URL/api/v1/messages/template" \
  -H "Authorization: Bearer $MESSAGING_API_KEY" \
  -H "Idempotency-Key: demo-results-sms-001" \
  -H "Content-Type: application/json" \
  --data '{
    "channel": "sms",
    "to": "254700000000",
    "template": "student_results_ready",
    "parameters": {
      "parent_name": "Amina",
      "student_name": "Baraka",
      "term": "Term 2",
      "results_url": "https://example.com/results/example-access-token"
    },
    "billing_account": "school-example-001",
    "metadata": {
      "source_type": "student_result",
      "source_id": "result-example-001"
    }
  }'
```

SMS recipients accept supported Kenyan `07…`, `01…`, `2547…`, `2541…`, `+2547…`, and `+2541…`
forms and are stored/sent in canonical `254…` form. SMS mappings render named values server-side;
missing or extra parameters fail before provider submission.

The current Fazilabs Advanta account rules are 160 characters per page, at most 6 pages/960
characters, and no emoji. Spaces and punctuation count. Empty, emoji-containing, and oversized
messages are rejected before wallet reservation or provider submission. These are confirmed account
commercial/provider rules, not universal assumptions about every SMS provider.

Successful SMS responses expose `sms_character_count` and `sms_page_count`; these fields are null
for WhatsApp. The Advanta `standard` or `transactional` route is template configuration, never a
request field. The server-owned sender ID cannot be overridden by consumers.

## Templates

A semantic key describes intent, while each application can have a different mapping for each
channel. For example, `student_results_ready/whatsapp` can use three BODY values and a dynamic URL
button named `results_path`, while `student_results_ready/sms` can render a text body using
`results_url`. Consumers should query `GET /api/v1/templates` and use the parameter names returned
for the chosen channel.

Example WhatsApp registration:

```bash
uv run python -m app.cli create-template \
  --application local-demo \
  --key student_results_ready \
  --channel whatsapp \
  --provider meta \
  --provider-template-name student_results_ready \
  --language en \
  --parameter-schema '{"body":["parent_name","student_name","term"],"buttons":[{"index":0,"type":"url","parameter":"results_path"}]}' \
  --billing-category utility \
  --billing-mode customer
```

Example SMS registration:

```bash
uv run python -m app.cli create-template \
  --application local-demo \
  --key student_results_ready \
  --channel sms \
  --provider advanta \
  --sms-route standard \
  --sms-body 'Hi {{parent_name}}, {{student_name}} {{term}} results: {{results_url}}' \
  --parameter-schema '{"body":["parent_name","student_name","term","results_url"]}' \
  --billing-category utility \
  --billing-mode customer
```

Use `update-template` to change mapping configuration in place and `enable-template` or
`disable-template` for availability. Provider template names, language, SMS bodies, and button
indices are intentionally absent from send requests.

## Billing

Messaging uses application-scoped billing accounts, prepaid wallets, effective-dated pricing rules,
reservations, immutable usage, and append-only wallet transactions.

- `billing_mode=customer`: the customer's wallet funds the message. When billing is required, the
  caller supplies `billing_account`; Messaging resolves the price and reserves the full expected
  charge before the provider call. SMS pricing is multiplied by rendered page count.
- `billing_mode=platform`: Fazilabs funds the message. No customer wallet is reserved or debited,
  but accepted usage and known provider cost remain tracked. An optional account is attribution
  only.

`MessagingApplication.billing_required` and template `billing_mode` have distinct roles. The
application policy prevents customer-funded traffic from omitting a prepaid account while preserving
legacy unbilled behavior only for applications explicitly configured to allow it. A platform-funded
template can send without a customer account even when application billing is required. Callers
cannot choose or override template billing mode.

Provider acceptance is the accounting point. On acceptance, the reservation becomes a debit and one
usage record is created. A confirmed submission rejection releases the reservation with no debit or
usage. An ambiguous submission retains the reservation and records the message as `uncertain` for
operator reconciliation. Later handset-delivery failure does not automatically refund an accepted
message.

Provider cost and customer selling price are independent. Advanta provider cost is calculated per
accepted SMS page only when configured; unknown cost remains null rather than being invented as zero.
Example prices in this README are test-only, never production/commercial pricing.

## Message lifecycle

Public message states are:

- `pending`: persisted before provider submission;
- `submitting`: claimed for exactly one provider attempt, which may be in flight;
- `sent`: the provider accepted the submission;
- `delivered`: a provider callback reported terminal delivery;
- `read`: a WhatsApp callback reported that the recipient read it;
- `failed`: submission was definitively rejected, or a later delivery callback reported failure;
- `uncertain`: provider acceptance could not safely be determined.

```text
pending ── claim ──> submitting ── accepted ──> sent ── callback ──> delivered ── WhatsApp callback ──> read
                        │
                        ├── confirmed rejection ──> failed (customer reservation released)
                        ├── ambiguous outcome ────> uncertain (reservation retained; operator reconciliation)
                        └── claim lease expired ──> uncertain (via dispatch recovery; never resent)
```

An uncertain message is never blindly submitted again. Exact idempotent replay returns its existing
record. Operators investigate provider evidence before recording an accepted, rejected, or unknown
reconciliation outcome.

## Webhooks

Meta calls `GET /webhooks/whatsapp` to verify a subscription and
`POST /webhooks/whatsapp` for inbound messages and status updates. POST bodies require a valid
`X-Hub-Signature-256` using the configured Meta app secret. Public HTTPS and a current callback URL
are required for real callbacks.

Advanta calls `POST /webhooks/advanta` with delivery reports. Known message IDs update lifecycle
state idempotently; unknown IDs and states are safely acknowledged without corrupting data. The
current Advanta information supplies no trustworthy callback signature mechanism. Do not invent one:
restrict ingress by reverse proxy/network allowlisting where operationally possible, monitor the
endpoint, and add provider-supported verification when available.

Without reachable provider callbacks, a provider may have accepted or delivered a message while its
local lifecycle remains at an earlier state.

## Local development

Prerequisites:

- Python 3.12 or later;
- [`uv`](https://docs.astral.sh/uv/);
- PostgreSQL reachable through an asyncpg URL.

Redis, a queue worker, and persistent filesystem storage are not used by the current application.
The repository does not provide Docker or Docker Compose files; start PostgreSQL using your local
development tooling.

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Replace every placeholder in `.env`; never commit `.env`. `APP_PUBLIC_WEBHOOK_BASE_URL` may remain
empty for mock-only development. For real provider callbacks, expose the local API through a trusted
HTTPS tunnel and update provider callback configuration to the current public URL.

Check the operator plane without making a provider send:

```bash
uv run python -m app.cli doctor
uv run alembic current
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
```

`/health` is a lightweight liveness check and does not access PostgreSQL. `/ready` performs a
minimal database connectivity check and returns HTTP `503` with a sanitized response when the
database is unavailable.

`doctor --check-reachability` makes an HTTP health request to the configured public URL. The plain
`doctor` command does not contact providers.

## Testing

Integration tests require an explicitly isolated PostgreSQL database in
`APP_TEST_DATABASE_URL`. Test setup rejects URLs whose database name does not contain `test`; never
point tests at development or production data.

```bash
export APP_TEST_DATABASE_URL='postgresql+asyncpg://<user>:<password>@127.0.0.1:5432/fazilabs_messaging_test'
export APP_DATABASE_URL="$APP_TEST_DATABASE_URL"
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run alembic check
git diff --check
```

Provider tests use HTTP mocks/fakes and must never contact Meta or Advanta.

## CLI operations

Run `uv run python -m app.cli --help` and per-command `--help` for complete flags. Major groups are:

- Applications: `create-application`, `list-applications`, `show-application`,
  `require-application-billing`, `allow-application-unbilled`.
- API keys: `create-api-key`, `list-api-keys`, `revoke-api-key`.
- Templates: `create-template`, `update-template`, `list-templates`, `enable-template`,
  `disable-template`.
- Billing accounts: `create-billing-account`, `list-billing-accounts`,
  `show-billing-account`, `activate-billing-account`, `suspend-billing-account`,
  `credit-billing-account`, `show-balance`, `list-wallet-transactions`,
  `monthly-usage-summary`.
- Pricing: `create-pricing-rule`, `list-pricing-rules`, `disable-pricing-rule`.
- Advanta operator balance: `advanta-balance`. This is provider credit, not a customer wallet.
- Dispatch recovery: `dispatch-sweep` runs one bounded recovery pass (see
  [Dispatch recovery sweeper](#dispatch-recovery-sweeper)); production runs it from a systemd timer.
- Uncertain messages: `list-uncertain-messages` (all by default),
  `list-uncertain-messages --stale-only`, `show-uncertain-message`,
  `reconcile-uncertain-message`, `release-uncertain-reservation`.
- Billing exceptions: `list-billing-exceptions`, `show-billing-exception`,
  `resolve-billing-exception`.
- Diagnostics: `doctor` and optional `doctor --check-reachability`.

Reconciliation and wallet commands are operator actions with financial consequences. Investigate
provider evidence first and retain an external audit reason/reference.

## Swagger and OpenAPI

With the API running locally:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

Swagger's **Authorize** control accepts the raw application API key as a bearer credential. Example
requests use fictional numbers and tokens. Provider-facing webhooks intentionally do not use the
consumer API-key scheme.

Production deployment endpoints are:

- Machine API base: `https://api.messaging.fazicore.app`
- Swagger UI: `https://messaging.fazicore.app/docs`
- ReDoc: `https://messaging.fazicore.app/redoc`
- OpenAPI JSON: `https://messaging.fazicore.app/openapi.json`
- Liveness: `https://api.messaging.fazicore.app/health`
- Readiness: `https://api.messaging.fazicore.app/ready`
- Meta callback: `https://api.messaging.fazicore.app/webhooks/whatsapp`
- Advanta callback: `https://api.messaging.fazicore.app/webhooks/advanta`

These hostnames are reverse-proxy/deployment configuration. They are not embedded in messaging
business logic.

## Configuration

Settings use the `APP_` prefix. Store production values in a secret manager or protected deployment
environment, not source control.

| Variable | Purpose |
| --- | --- |
| `APP_NAME` | Runtime service name returned at `/` |
| `APP_ENVIRONMENT` | `development`, `test`, `staging`, or `production` |
| `APP_DEBUG` | FastAPI debug behavior; must be false in staging/production |
| `APP_HOST`, `APP_PORT` | Server bind defaults for deployment tooling |
| `APP_LOG_LEVEL` | Root structured-log level |
| `APP_DATABASE_URL` | PostgreSQL SQLAlchemy/asyncpg URL; required in staging/production |
| `APP_WHATSAPP_VERIFY_TOKEN` | Meta subscription verification secret |
| `APP_WHATSAPP_APP_SECRET` | Meta webhook signature secret |
| `APP_WHATSAPP_ACCESS_TOKEN` | Meta Cloud API credential |
| `APP_WHATSAPP_PHONE_NUMBER_ID` | Server-owned WhatsApp sender identity |
| `APP_WHATSAPP_API_VERSION` | Meta Graph API version |
| `APP_PUBLIC_WEBHOOK_BASE_URL` | Public HTTPS base URL used by diagnostics/operations |
| `APP_ADVANTA_BASE_URL` | Advanta API base URL |
| `APP_ADVANTA_API_KEY` | Advanta credential |
| `APP_ADVANTA_PARTNER_ID` | Advanta partner credential |
| `APP_ADVANTA_SENDER_ID` | Server-owned Advanta sender ID |
| `APP_ADVANTA_PROVIDER_COST_PER_PAGE_MINOR` | Nullable trusted provider-cost snapshot per SMS page |
| `APP_ALLOW_LIVE_PROVIDER_SENDS` | Live Meta/Advanta egress switch; see below |
| `APP_BILLING_UNCERTAIN_RECONCILE_AFTER_MINUTES` | Safety age used by stale reconciliation operations |
| `APP_TEST_DATABASE_URL` | Test-only isolated PostgreSQL URL consumed by integration tests |

Configured provider credentials are never, by themselves, permission to contact Meta or Advanta.
Live provider clients are built only in `app/services/provider_egress.py`, which both the HTTP API
and the CLI use:

| `APP_ENVIRONMENT` | `APP_ALLOW_LIVE_PROVIDER_SENDS` unset | `true` | `false` |
| --- | --- | --- | --- |
| `test` | blocked | rejected at startup | blocked |
| `development` | blocked | allowed | blocked |
| `staging` / `production` | allowed | allowed | blocked |

When blocked, send endpoints return `503` before persisting a message, and the `dispatch-sweep`
and `advanta-balance` commands exit without contacting a provider. Settings still load `.env`, so
overriding only some variables inline keeps the `.env` credentials but not live egress.

Production/staging settings currently validate all Meta credentials even if only SMS is intended.
Advanta settings are optional at startup; an SMS attempt returns provider-unavailable if incomplete.
Deployment validation must therefore check required channel configuration explicitly.

## Deployment

The current runtime is one stateless ASGI API process plus PostgreSQL. A minimal first deployment
should run a pinned repository build under Uvicorn behind Nginx, with environment secrets injected
at runtime and Alembic run as a separate release step. Both public hostnames proxy to the same
FastAPI deployment.

Example process command:

```bash
uv run uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips 127.0.0.1
```

This command is for Nginx running on the same host. Keep Uvicorn private and trust forwarded headers
only from the proxy address; never use `--forwarded-allow-ips '*'`. If the proxy moves to a distinct
private host, replace the bind and trust values with the specific private network addresses.

The production Nginx examples are:

- [`deployment/nginx/fazilabs-messaging.conf.example`](deployment/nginx/fazilabs-messaging.conf.example)
- [`deployment/nginx/fazilabs-messaging-proxy.conf.example`](deployment/nginx/fazilabs-messaging-proxy.conf.example)

Nginx terminates HTTPS, redirects HTTP to HTTPS, rejects unknown hosts, applies a 1 MiB request-body
limit, and provides bounded edge request rates. `messaging.fazicore.app` exposes only documentation;
`api.messaging.fazicore.app` exposes `/api/v1/*`, `/webhooks/*`, `/health`, and `/ready`. Certificate
paths are placeholders; this repository does not provision DNS or certificates.

Do not run multiple migration jobs concurrently. The service has no startup migration, queue worker,
or local persistent-data requirement. It does require the dispatch recovery timer described in
[Dispatch recovery sweeper](#dispatch-recovery-sweeper). PostgreSQL is the system of record and needs
managed backups and restore testing.

Before production, configure Meta's callback as
`https://api.messaging.fazicore.app/webhooks/whatsapp` and Advanta's DLR callback as
`https://api.messaging.fazicore.app/webhooks/advanta`. Use `/health` for liveness and `/ready` for
traffic admission/readiness. The selected platform must aggregate JSON stdout/stderr logs and alert
on API errors, provider failures, uncertain messages, billing exceptions, database health, and
webhook failure or silence.

## Dispatch recovery sweeper

Every send is persisted as `pending`, claimed as `submitting` for exactly one provider attempt, then
recorded with its outcome. If the API process dies between those steps, the row is stranded:

- a `pending` row older than `APP_DISPATCH_PENDING_GRACE_SECONDS` (60) was never submitted, so it
  is safe to submit exactly once;
- a `submitting` row whose claim lease (`APP_DISPATCH_CLAIM_LEASE_SECONDS`, 90) expired may or may
  not have reached the provider, so it becomes `uncertain` and is **never** submitted again.

`dispatch-sweep` performs one bounded pass of both. It claims and dispatches abandoned `pending` rows
one at a time (at most `APP_DISPATCH_SWEEP_BATCH_SIZE`, 25, per pass), each immediately before its
own provider attempt, and re-verifies the claim right before calling the provider. A lease can
therefore never expire before its provider attempt has started, whatever the batch size or provider
timeout. Concurrent passes are safe: rows are claimed with `SKIP LOCKED` and every status change is
compare-and-swap. Nothing performs this recovery unless the sweep runs, so production must schedule
it.

Scheduling uses [`deployment/systemd`](deployment/systemd/README.md):

- `fazilabs-messaging-dispatch-sweep.service`: a oneshot running
  `.venv/bin/python -m app.cli dispatch-sweep` as the API's user, from the API's checkout, with the
  API's `EnvironmentFile`. It contains no secrets.
- `fazilabs-messaging-dispatch-sweep.timer`: starts it about once per minute. systemd does not start a
  run while the previous one is still active, and `Persistent=true` runs one catch-up pass after
  downtime.

The sweep contacts providers, so it obeys the same provider-egress rule as the API: it refuses and
exits non-zero in `test`, and in `development` unless `APP_ALLOW_LIVE_PROVIDER_SENDS=true`. Because it
reads the API's `.env`, production behavior matches the API's.

Operations on the host:

```bash
systemctl list-timers fazilabs-messaging-dispatch-sweep.timer   # last and next run
systemctl status fazilabs-messaging-dispatch-sweep.service      # last run result
journalctl -u fazilabs-messaging-dispatch-sweep.service --since "1 hour ago"
```

Each pass logs one JSON `dispatch_sweep_completed` event (`dispatched_from_pending`,
`expired_submitting_to_uncertain`, `dispatch_outcomes`, `duration_ms`), or `dispatch_sweep_failed`
(`error_type` only) / `dispatch_sweep_refused` with a non-zero exit, which leaves the service
`failed` until the next successful pass. Alert when no `dispatch_sweep_completed` event appears for
several minutes, on repeated failures, and on any `expired_submitting_to_uncertain` above zero.

Rows moved to `uncertain` appear in `list-uncertain-messages`. **Never resend an uncertain message
blindly** (for example with a new idempotency key): the provider may already have accepted it. Use
`show-uncertain-message` and provider evidence, then `reconcile-uncertain-message`.

To pause recovery safely, run `sudo systemctl stop fazilabs-messaging-dispatch-sweep.timer` (add
`disable` to keep it off across reboots). Stopping only pauses recovery: stranded rows stay as they
are until it resumes. Do not kill a running pass unless it is hung; if you do, at most the one row
it was submitting becomes `uncertain` after its lease.

## Security and operational notes

- Keep consumer and provider credentials server-side; rotate them through the deployment secret
  store and revoke old application keys.
- Do not log authorization headers, message bodies, recipients, template parameter values, result
  URLs/tokens, or provider request payloads.
- Require production HTTPS and restrict direct backend ingress to the reverse proxy.
- Meta POST callbacks are signature-verified before JSON parsing. Advanta DLR verification is a
  known limitation. The Nginx example includes a disabled allowlist block: enable it only after
  Advanta confirms stable source CIDRs; do not guess provider addresses. Until then, retain HTTPS,
  exact-path routing, request-size limits, rate limits, monitoring, and restricted backend ingress.
- Treat idempotency keys as non-secret business identifiers and avoid sensitive data in them.
- Never automatically retry `uncertain` sends; use evidence-based reconciliation.
- No CORS, trusted-host middleware, or application-level rate limiter is currently configured.
  Browser access is not required for normal server-to-server consumers. Enforce host/ingress and
  initial rate controls at the edge until application policy is added.
- `/docs`, `/redoc`, and `/openapi.json` are public by default. Decide explicitly whether the
  production edge keeps them public, restricts them, or disables them.

## Current limitations and future work

- Advanta DLR callbacks lack provider-verified authentication in the available contract.
- There is no bulk send, automatic cross-channel fallback, queue, or automatic uncertain
  reconciliation.
- There is no built-in application rate limiting or deployment manifest/container image.
- Provider delivery-report polling exists as a client capability but no polling daemon is included.
