# Durable Fleet epic registration

`register-fleet-epic` describes a reviewed workflow under an existing GitHub
epic and publishes its registration through JetStream. It does not execute
tasks. Agamemnon owns the resulting orchestration graph and task decisions;
Hephaestus owns implementation/review labels. This command never writes
`state:*` labels or uses the local workflow state store.

The legacy `register-epic` API and command remain available for their existing
callers. They create a fresh epic and use Core NATS; they do not satisfy the
Fleet durability contract. Fleet callers must select this explicit command or
`telemachy.fleet_registration.register_fleet_epic`.

## Operator contract

1. Provide a reviewed workflow and a separately created canonical epic issue.
   Automatic first-epic creation remains an unresolved intake gate: GitHub's
   issue-create API supplies no transaction with which this producer can
   atomically establish its own durable registration record. This command
   never creates or recreates that epic.
2. Give exactly one registration process ownership of that epic. The CLI
   requires `--exclusive-writer` as an assertion of this deployment condition;
   it does not acquire a distributed lock. GitHub issue-body PATCH is not an
   atomic compare-and-swap API. Concurrent writers to this block are unsupported.
3. Add the following empty opt-in block to the reviewed epic, using the same
   key on every retry. The producer refuses an absent, wrong, duplicate, or
   malformed marker and preserves all bytes outside its block.

   ```html
   <!-- telemachy:fleet-registration:v1 key=reviewed-idea -->
   <!-- /telemachy:fleet-registration:v1 -->
   ```

4. Configure backend `GITHUB_TOKEN`, `NATS_URL`, and optional `NATS_CLIENT_TOKEN`.
   TLS is required by default. `REQUIRE_TLS=false` is an explicit local
   development setting. No credentials are written into issues or command
   output. The GitHub transport does not follow redirects or use proxy
   environment variables.
5. Provision the existing `homeric-pipeline` stream through the supported
   infrastructure path. Fleet requires file storage, limits retention, no
   expiration, discard-new policy, and a duplicate window of at least 120
   seconds. This command validates that configuration; it never creates,
   replaces, or changes a stream. Missing/incompatible streams fail closed.
   A positive `MaxMsgsPerSubject` also requires `DiscardNewPerSubject=true`;
   otherwise NATS can evict a previous registration even with stream-level
   discard-new. A full compatible stream rejects new work while retaining the
   prior message; the outbox remains pending until publication is confirmed.
6. Preview without credentials, network requests, or mutations:

   ```bash
   just fleet-register reviewed.yaml --repo OWNER/REPO --epic-issue 42 \
     --registration-key reviewed-idea --dry-run
   ```

7. Run the registration under the single-writer deployment condition:

   ```bash
   just fleet-register reviewed.yaml --repo OWNER/REPO --epic-issue 42 \
     --registration-key reviewed-idea --exclusive-writer
   ```

8. On an unconfirmed result, retry with the same repository, epic, key, and
   exact workflow. Never reset a `creating` phase to authorize another issue
   creation. `unresolved_create_requires_reconciliation` means a request may
   have succeeded and its issue cannot yet be located; operator reconciliation
   is required. Pagination, authentication, rate limiting, or network errors
   are not evidence that an issue is absent.

## Durable boundaries

The issue block has schema `hi/telemachy/fleet-registration/v1`. It stores a
frozen workflow digest, deterministic child identities, creation progress,
the exact publication envelope, and the confirmed broker receipt. It holds
registration/outbox progress only, not task execution state. The block also
contains issue links for every confirmed child.

- Before creating any child, a confirmed GitHub write records its `creating`
  intent. A later invocation searches exact child markers across open and
  closed issues. It can adopt one matching child but cannot issue another
  create after an unresolved intent. Duplicate markers, changed child bodies,
  or a changed workflow under the same epic are conflicts.
- A GitHub write is acknowledged only by a successful API response containing
  the exact expected issue number and body. Lost responses leave the previous
  phase unconfirmed; the next invocation rereads authoritative issue state.
  The HTTP adapter never retries a mutation automatically.
- The `publish_pending` phase includes the complete frozen `hi/v1` envelope
  before any broker publish. The subject is
  `hi.pipeline.epic.{epic_key}.registered`. A stable `Nats-Msg-Id` is derived
  from the canonical repository, epic number, and workflow digest. Retries
  send the same UTF-8 canonical JSON bytes with `Nats-Expected-Stream` set.
- Only an actual JetStream PubAck with the expected stream and positive
  sequence number allows a `published` GitHub write. Core `publish`/`flush`
  cannot supply this acknowledgment. A lost receipt write replays the pending
  envelope on restart without recreating children.

Publication is at least once. Broker duplicate suppression is finite;
Agamemnon's durable receiver must also deduplicate by canonical repository and
epic identity and confirm its own persistence before acknowledging delivery.
A producer receipt proves broker storage, not Agamemnon task admission or work
completion. Completed registration retries do not republish automatically.

Issue listing is bounded to 100 pages by default, with a 30-second enumeration
budget. Reaching a bound fails closed. The issue block is limited to 60,000
UTF-8 bytes. Larger workflows need a separately designed durable storage
contract; there is no memory-only fallback.

## Local verification

Use an already provisioned environment with the repository's declared
dependencies. The private-broker tests require an existing `nats-server`
binary and never install dependencies or contact production services.

```bash
just fleet-registration-test /path/to/python
just fleet-registration-integration /path/to/python /path/to/nats-server
```

The tests cover controlled GitHub write failures, uncertain issue creation,
closed-child reconciliation, exact-envelope retries, body/phase conflicts,
CLI gating, real private JetStream PubAck and duplicate receipts, receipt-write
loss, and incompatible/missing stream rejection. GitHub authority in these
tests is controlled; live GitHub admission and the full research-to-work flow
remain unproven.

## Native receiver contract

Build Agamemnon's fixture-only target with `just fleet-epic-import-build` in its
standalone checkout. From this checkout, invoke the explicit cross-repo harness:

```bash
just fleet-native-contract /path/to/Agamemnon/build/fleet/fleet_epic_import \
  /path/to/nats-server /private/new-output-directory /path/to/python
```

The output directory must not already exist. The harness calls actual Fleet
registration and publication against a fresh private broker and controlled GitHub
issues. It checks identical durable-outbox/broker/native-handler bytes, a failed
producer receipt write and duplicate PubAck retry without new children, native
write-before-dispatch/ACK, restart replay, and a canonical parent wakeup. An
altered capture must fail before admission. The native replay changes only its
transport deduplication header to test receiver idempotence while retaining the
exact producer body. Broker storage and process output are private; no live
GitHub requests, worker execution, or production configuration are involved.
This explicit command does not add another repository's binary to default pytest.

The Linux CI image provisions nats-server 2.10.24 from its official Linux amd64
release and verifies the pinned SHA-256 before extraction. The existing CI test
job builds that image before running the complete suite; broker tests remain
mandatory. This is a development image dependency, not a Telemachy application
runtime dependency or an additional Pixi platform. Local runs still require a
provisioned broker binary.
