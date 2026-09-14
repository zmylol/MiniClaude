# Constraint memory pilot data

This is a hand-authored synthetic mechanism check for recovering currently approved
settings after context compression. It contains **36 tasks, 6 categories, 3 episodes
per task, and 108 scored fields**. All task text is English. It is not a collection
of real user conversations or a benchmark of completed software changes.

## Public inputs and private evaluation labels

`tasks.jsonl` contains one JSON object per line:

| Field | Meaning |
| --- | --- |
| `id` | Unique task identifier, including category and scenario slug |
| `category` | Experimental situation; use for reporting, not answer selection |
| `query` | Request for the three currently approved settings and their evidence |
| `fields` | Three exact output field names |
| `episodes` | Three chronological arrays, each containing four events |

Every event contains `id`, `role`, `text`, and `decisions`. Event IDs are unique
within a task, not across the corpus. The original natural-language `text` explicitly
states each approved value. Public decision metadata contains no answer values.

Each decision contains:

- `key`: the affected field; its value must be read from the original event text.
- `status`: `confirmed` for an explicit user decision or `proposed` for an
  unaccepted assistant suggestion.
- `supersedes`: IDs of earlier events whose confirmed decision for **this key**
  is replaced. An event may confirm several keys; superseding one of its keys
  does not invalidate its other decisions.

**The decision arrays are idealized structured metadata.** They model a workflow
in which users explicitly maintain public decision records. Their correctness,
speaker attribution, scope, and approval status are supplied as assumptions in
this pilot. The metadata supplies only keys and lifecycle information, not selected
values. The experiment does not solve extraction of constraint keys or approval
from arbitrary natural language. Only user events carry confirmed decisions;
tool observations and assistant proposals are not user authorization.

A version-aware method must still read original evidence to recover a value;
the `no_evidence` ablation cannot copy a selected value from a decision header.
That ablation can still use any value retained in its summary, so it is not an
assumption of complete information loss.

All experimental groups must receive the same serialization of the public events,
including the same metadata, before any group-specific compression or retrieval.
Giving only one method the decision arrays would confound the comparison with
additional supervision. Archive access, retrieval cost, compression count, and
final context budget must also be reported explicitly by the runner.

`answers.json` is a separate evaluator-only object:

```json
{
  "category/scenario": {
    "field_name": {
      "value": "exact-string",
      "evidence_id": "e06",
      "obsolete_values": ["earlier-confirmed-string"]
    }
  }
}
```

The gold file must never enter model prompts, summarization, retrieval ranking,
or an experimental policy. The evidence ID is the **latest user confirmation**
of that field. After a reversion to an earlier value, the latest reversion event
is the gold evidence, rather than the original event with the same value.
`obsolete_values` contains the unique earlier confirmed values that differ from
the final value, in chronological order. This evaluator-only list supports scoring
resurrection of superseded choices. It was computed from the original authored
confirmed-value annotations before those values were removed from public metadata.
Unapproved proposals and values mentioned only in tool output are excluded. A
reverted choice matching the final value is also excluded, even if it was obsolete
temporarily. Fields with no obsolete choices have an empty list.

## Category coverage

| Category | Six scenarios | Intended distinction |
| --- | --- | --- |
| `latest_update` | API compatibility, backup retention, export containers, retry policy, locale fallback, telemetry sampling | A setting changes twice to three distinct values |
| `reverted_decision` | Database migration, styling, event transport, wire encoding, session authentication, package distribution | A → B → A; the final A needs current evidence |
| `proposal_not_approval` | Pagination, polling, thumbnails, account cleanup, notifications, deployment | A pending proposal must not overwrite a confirmed setting |
| `tool_output_not_authority` | Cache defaults, build target, listener port, date parsing, upload size, log filtering | Cached settings and tool advisories describe observations, not approval |
| `scoped_update` | Environment timeouts, responsive layout, operation retries, route indexing, regional units, role sessions | Update only the named scope while preserving the other scope |
| `unchanged_control` | Checksums, stable sorting, health routes, documentation, asset naming, configuration parsing | Preserve initial choices despite discussion and contrary examples |

Every non-control task contains at least one explicit revision. Every task contains
assistant discussion, tool output, and an unaccepted suggestion. The scoped tasks
use separate public keys for separate scopes; interpreting implicit scopes is
outside this pilot. Controls contain no confirmed revisions.

The corpus does not contain repeated padding. A runner may insert neutral tool
noise to produce longer contexts, but it must apply the same noise and episode
boundaries to each method and record the setting. More padding is not equivalent
to more independent tasks or more realistic long-horizon work.

## Validation and limits

`tests/unit/test_constraint_memory_dataset.py` independently checks category
balance, the absence of public metadata values, role authority, causal evidence
references, original-text gold grounding, reversions, and unchanged controls. It reads the files directly and
does not reuse a memory policy to validate them.

The 36 scenarios vary in subject and field meaning, but share a deliberately
regular conversation layout. Values are discrete strings; updates, rejections,
and scope limits are unusually explicit. Labels are authored rather than
independently adjudicated, and there is no held-out split. Consequently:

- Treat outcomes as a development-set pilot and a check of the experimental
  machinery, not evidence of generalization or publication-ready superiority.
- Field accuracy and all-fields-correct rate measure settings recovery, not real
  software task success. No implementation is executed by these tasks.
- An oracle that reads the private labels is a scorer check, not a learned model
  or proof of a memory algorithm's benefit. Lifecycle metadata can locate the
  latest evidence but does not itself expose the selected value.
- Further work needs independently reviewed natural conversations, irregular
  histories, hidden test tasks, matched resource accounting, actual model runs,
  and repeated trials with uncertainty estimates.
