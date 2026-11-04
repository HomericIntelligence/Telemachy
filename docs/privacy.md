# Privacy & PII Handling in Telemachy Workflows

## Scope

Telemachy is a workflow engine. It does not store, classify, or anonymise the
free-text fields that workflow authors put into a workflow YAML. A
`WorkflowState` exists only in memory for the duration of a run; Telemachy
writes no local state file and no local audit log. This document tells authors
and operators what happens to that text and how to keep personal data out of
the data flow.

## What text leaves the Telemachy process

The table below reflects Telemachy's current code paths. **Maintenance note:**
update this table whenever the data flow changes — in particular, the
`NatsMonitor` subscriber consumes Agamemnon's task-lifecycle events; any new
sink must revise this table. The qualifying method name is the stable anchor,
so line numbers are not recorded.

| Field                  | Sent to Agamemnon?                                                                                              | Logged by Telemachy? | Log level (where)                                                                                                         |
| ---------------------- | --------------------------------------------------------------------------------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `metadata.name`        | No                                                                                                              | Yes                  | INFO (`WorkflowExecutor._run`)                                                                                            |
| `metadata.description` | No                                                                                                              | No                   | —                                                                                                                         |
| `agents[].name`        | Yes (agent create)                                                                                              | Yes                  | DEBUG (`WorkflowExecutor._provision_one_agent`)                                                                           |
| `agents[].model`       | Yes (agent create — as `programArgs`, see `AgamemnonClient._create_local_agent`)                                | No                   | —                                                                                                                         |
| `teams[].name`         | Yes (team create)                                                                                               | Yes                  | INFO (`WorkflowExecutor._create_team_only`)                                                                               |
| `tasks[].subject`      | Yes (task create)                                                                                               | Yes                  | INFO (`WorkflowExecutor._submit_tasks_with_deps`); WARNING when a dependency fails (same method)                           |
| `tasks[].description`  | Yes (task create — see `AgamemnonClient.create_task`, `agamemnon_client.py`)                                    | No                   | —                                                                                                                         |

## Author guidance

- Treat `subject` and `description` as text that will be transmitted to
  Agamemnon. Treat `subject` additionally as text that will appear in
  Telemachy logs at INFO and may be persisted by your operator's log
  aggregation. Do not paste:
  - personal names, email addresses, phone numbers
  - government / account / customer IDs
  - secrets, API keys, or credentials
- Reference data by stable opaque IDs (e.g. `customer_id=42`) rather than
  identifying values where possible.
- If a task genuinely requires personal data (e.g. processing a support
  ticket), see "Operator controls" below and coordinate with your operator.

## Operator controls

Telemachy ships no built-in redaction filter — adding one in-tree would
require a heuristic regex that risks redacting legitimate content (model
names, agent names, task subjects). Operators have these levers instead:

- **`LOG_LEVEL=WARNING`** — suppresses the INFO-level subject logging in
  `executor.py:304-305`. WARNING-level logs still emit subjects when a
  dependency fails (`executor.py:264-268`), so this is a coarse but
  effective lever.
- **Log-shipping-side scrubbing** — apply your existing log-aggregation
  scrubber (Vector, Fluent Bit, Logstash, GCP Cloud Logging redaction
  configs, etc.) to the `telemachy` logger stream. Operators already
  using one of these tools should configure it there rather than rely on
  Telemachy for content classification.
- **Agamemnon-side controls** — Agamemnon's own retention, access control,
  and audit logging are out of scope for Telemachy; consult its
  documentation for your deployed instance.

## GDPR & lawful basis

Telemachy does not determine the purpose or means of any personal data its
operators choose to put into a workflow — it relays text the author wrote
to the Agamemnon instance the operator configured. Operators that process
personal data through Telemachy are the controllers for that processing
and must establish their own lawful basis, retention policy, data-subject
request workflow, and data-processing agreement with the Agamemnon
operator. This document does not constitute legal advice; consult your
own counsel.

## See also

- `docs/adr/004-pii-handling-stance.md`
- `SECURITY.md`
- `AGENTS.md` § Agent Guardrails
