# th-integrations

The service polls one corporate mailbox, validates tender attachments, and publishes a shared Job on `tender.job.created`. The Kafka key is the Job UUID and `source` is always `mail`. The service also hosts three read-only MCP tools backed by synthetic data.

## Local sandbox

From the repository root, run the following command with the project infrastructure settings. The example uses disposable local credentials.

```powershell
docker compose --env-file .env.example --profile integrations up -d --build --wait th-integrations
```

The `integrations` profile starts GreenMail with local SMTP on `127.0.0.1:3025` and serves the API at `http://127.0.0.1:8003`. Override the host ports through `TH_INTEGRATIONS_SMTP_HOST_PORT` and `TH_INTEGRATIONS_HOST_PORT`. A message addressed to `demo@example.test` must contain `Message-ID`, `From`, `Subject`, `Date`, and at least one supported attachment. Send the same Message-ID and attachment bytes again to verify deduplication.

Check `GET /api/v1/health/ready` for PostgreSQL availability and the installed schema. The service retries transient mail and broker failures in the background. For an authenticated status lookup, send `GET /api/v1/jobs?external_ref=<Message-ID>` with the Message-ID URL-encoded, including its angle brackets. The response contains `jobs`, an array of `job_id` and `published` fields. Distinct attachment sets under the same Message-ID may return multiple jobs. A job becomes published after Kafka acknowledges it and PostgreSQL records that acknowledgement.

Each Job document has a `storage_path` pointing to `GET /api/v1/documents/{file_id}` on the configured document origin. Both read endpoints expect the `Authorization: Bearer <TH_INTEGRATIONS_DOCUMENT_TOKEN>` header. In local Compose the synthetic token is `demo-integrations-document-token`. Consumers must be able to reach the configured origin and use the configured token.

## Attachment intake and delivery

Eligible attachments have a matching extension and content signature. The service accepts PDF, DOCX, PNG, JPEG, TIFF, GIF, BMP and WebP, with independent message and attachment byte limits. It checks the DOCX ZIP directory entries without extracting them. Ineligible attachments are omitted. If none are eligible, the email stays unread for mailbox review. The same behavior applies to malformed or oversized mail. File content is served as a download with its validated MIME type.

The service owns its PostgreSQL schema. A single transaction stores the Job, attachment bytes, and outbox record. The command `python -m tender_integrations migrate` installs the schema, and Compose runs it before starting the app. Each delivery attempt claims a pending event with a bounded lease. Kafka must acknowledge publication before the service can record delivery. A subsequent poll sets IMAP `\Seen` or Exchange `isRead` for that message. The process retries pending rows even during a mailbox outage. If Kafka acknowledges an event immediately before the process loses the database connection or exits, the same Job may be delivered again. Consumers deduplicate by `job_id`.

The deduplication key hashes the configured mailbox ID, the Message-ID, and the sorted attachment checksums with length framing. PostgreSQL enforces uniqueness across restarts, workers and Redis expiry. Redis caches confirmed mappings under `job:idempotency:{hash}` for seven days. The database confirms a cache hit before the service acknowledges a message. For OpenTelemetry export, set `TH_INTEGRATIONS_OTLP_TRACES_ENDPOINT` to the collector's full HTTP traces URL. The service preserves the ingestion trace in the Kafka headers.

## Mailbox settings

Copy the [settings example](.env.example) to `.env` in the service directory for local overrides. Supply the service settings as environment variables in containers. Keep the mailbox ID stable across redeployments of the same mailbox. Choose IMAP or Exchange Online.

- IMAP uses the `TH_INTEGRATIONS_IMAP__*` fields. Set `TH_INTEGRATIONS_EXCHANGE=null`. Use `implicit_tls` or `starttls` for a corporate mailbox. `plaintext` is for the local sandbox.
- Exchange Online uses Microsoft Graph app-only OAuth with `Mail.ReadWrite` scoped to the intended mailbox. Set `TH_INTEGRATIONS_IMAP=null` and omit the `TH_INTEGRATIONS_IMAP__*` variables. Configure `TH_INTEGRATIONS_EXCHANGE__GRAPH_URL` (the HTTPS API base, ending in `/v1.0`), `__TOKEN_URL`, `__TOKEN_SCOPE`, `__CLIENT_ID`, `__CLIENT_SECRET`, `__MAILBOX_USER` and `__FOLDER`. The folder may be the Graph `inbox` alias. The adapter reads MIME and updates the read flag using immutable IDs. Its Graph requests are covered by HTTP mock tests. [Microsoft Graph get message](https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0) · [Update message](https://learn.microsoft.com/en-us/graph/api/message-update?view=graph-rest-1.0)
- For an external mailbox, run the service and migration image with injected settings. The local Compose profile always starts its GreenMail fixture. For local changes to PostgreSQL credentials, also override `TH_INTEGRATIONS_DATABASE_DSN` in the service settings.

## MCP tools

The Streamable HTTP endpoint is `/api/v1/mcp`. Configure allowed Host and Origin headers through `TH_INTEGRATIONS_MCP_ALLOWED_HOSTS` and `TH_INTEGRATIONS_MCP_ALLOWED_ORIGINS`. The three tools accept a non-empty reference string and return structured data marked `simulated: true`.

| Tool | Argument | Result |
| --- | --- | --- |
| `getOrganizationProfile` | `profile_ref` | Sample permits, resources, and BYN prices |
| `getDocumentStatus` | `document_ref` | Sample electronic document status |
| `checkCounterparty` | `registration_number` | Sample legal name and activity flag |

## Checks

From `services/th-integrations`, run `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked pyright` and `uv run --locked pytest`. Build from the repository root with `docker build -f services/th-integrations/Dockerfile .`. The Dockerfile also provides a `test` target.


To run the full test suite against a local Compose project named `tenderhive-integrations-sandbox`, first start that project with the integrations profile and then run the test image on its network.

```powershell
docker build --target test -f services/th-integrations/Dockerfile -t tenderhive-integrations-check:test .
docker run --rm --network tenderhive-integrations-sandbox_default --env-file services/th-integrations/.env.example --env TH_INTEGRATIONS_TEST_SMTP_PORT=3025 tenderhive-integrations-check:test
```
