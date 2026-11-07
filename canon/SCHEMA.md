---
created: 2026-05-24T00:00:00-04:00
last_modified: 2026-05-24T00:00:00-04:00
ttl_tier: 1
expires: annual
urgency: normal
owner: COVENANT
last_reviewed: 2026-05-24
activation_status: active
confidence: 0.95
last_verified: 2026-05-24T00:00:00-04:00
verified_by: COVENANT
authored_by: COVENANT
approved_by: Founder
approval_gate: cluster_F_2026-05-24
supersedes: —
superseded_by: —
is_intermediate_packet: false
reusable_in: [memory-governance, graph-ingest, agent-recall, schema-validation]
---

# Infinite Brain — Locked Schema (v1, 2026-05-24)

Authored by COVENANT, approved by Founder via Cluster F audit on 2026-05-24. This is the enforceable schema for every file under `06_MEMORY_SYSTEM/0611_LLM_WIKI/nodes/` and `entities/`. Reconciles `0001_ENTITY_SCHEMA.md`, `0005_NODE_TAXONOMY.md`, the existing templates, and the COVENANT-canonical concept node `infinite_brain_architecture.md`.

## 1. Filename Conventions

### Nodes
```
nodes/<dir>/mem_YYYYMMDD_<slug>_<8hex>.md
```
- `dir` per Directory Mapping in §4
- `slug` = lowercase, underscore-separated, max 6 words
- `<8hex>` = first 8 chars of sha1(slug + iso-date) or random — must be unique across vault
- Example: `mem_20260101_project_example_a1b2c3d4.md`

### Entities
```
entities/<dir>/<CANONICAL_NAME>.md
```
- `CANONICAL_NAME` = the stable proper name (e.g. `COVENANT.md`, `Arcturion_Group.md`, `Founder_Example.md`)
- Subdirs: `agents/`, `companies/`, `people/`, `projects/`, `concepts/`, `systems/`

## 2. Required Frontmatter — Nodes

Every node file under `nodes/<dir>/` MUST carry:

| Field | Allowed values | Required |
|---|---|---|
| `id` | `mem_YYYYMMDD_<slug>_<8hex>` | yes |
| `title` | string | yes |
| `node_type` | per §3 closed vocabulary | yes |
| `status` | `draft` \| `active` \| `deprecated` \| `superseded` \| `archived` | yes |
| `created` | ISO 8601 with `-04:00` offset | yes |
| `last_modified` | ISO 8601 with `-04:00` offset | yes |
| `last_reviewed` | `YYYY-MM-DD` | yes |
| `owner` | agent name (usually `COVENANT`) | yes |
| `author` | agent name | yes |
| `source_agent` | agent name | yes |
| `ttl_tier` | `0` \| `1` \| `2` \| `3` | yes |
| `expires` | `annual` \| `90 days` \| `30 days` \| `never` | yes |
| `confidence` | `low` \| `medium` \| `high` \| `verified` | yes |
| `sensitivity` | `public` \| `internal` \| `confidential` \| `secret-reference` | yes |
| `governance_tier` | `0` \| `1` \| `2` \| `3` \| `4` \| `5` | yes |
| `approval_required` | `none` \| `founder` \| `covenant` \| `domain_owner` | yes |
| `canonical_sources` | list (may be empty `[]`) | yes |
| `canonical_files` | list of repo-relative paths | yes |
| `related_agents` | list | yes |
| `related_projects` | list | yes |
| `keywords` | list | yes |
| `supersedes` | list (use `[]` when none — never `—`) | yes |
| `superseded_by` | list | yes |

### Optional node frontmatter

| Field | Use |
|---|---|
| `family` | per `0005_NODE_TAXONOMY.md` §Family Tag — required for non-document node types |
| `path` | repo-relative path to the actual artifact (for code/application/automation nodes) |
| `approval_gate` | reference to the approval gate id that authorized the node |

## 3. Required Frontmatter — Entities

Every entity file under `entities/<dir>/` MUST carry:

| Field | Required |
|---|---|
| `name` | yes |
| `entity_type` | yes — per closed vocab below |
| `created` | yes |
| `last_modified` | yes |
| `ttl_tier` | yes |
| `expires` | yes |
| `urgency` | yes |
| `owner` | yes |
| `last_reviewed` | yes |
| `activation_status` | yes (`active` \| `staged` \| `dormant` \| `archived`) |
| `confidence` | yes — decimal `0.00`–`1.00` |
| `last_verified` | yes — ISO 8601 |
| `verified_by` | yes |
| `authored_by` | yes |
| `canonical_sources` | yes (list, may be empty) |
| `supersedes` | yes (`—` or list) |
| `superseded_by` | yes (`—` or list) |
| `is_intermediate_packet` | yes (`true` \| `false`) |
| `reusable_in` | yes (list) |

Closed `entity_type` vocabulary: `agent`, `founder`, `company`, `project`, `policy`, `tool`, `concept`, `system`, `source`, `decision`, `incident`, `preference`.

## 4. Subdirectory Taxonomy

### `nodes/`

| Dir | Node types accepted | What lives here |
|---|---|---|
| `concepts/` | Concept, Pillar | Reusable models, doctrines, architectures |
| `decisions/` | Decision | Explicit founder/COVENANT choices with rationale |
| `facts/` | Fact | Verifiable claims about state of the universe |
| `policies/` | Policy | Enforceable rules with governance tier |
| `playbooks/` | Playbook | Repeatable procedures |
| `preferences/` | Preference | Founder/agent operating preferences |
| `constraints/` | Constraint | Legal/technical/brand/financial/ethical limits |
| `patterns/` | Pattern | Recurring signals, behaviors, workflows |
| `hypotheses/` | Hypothesis | Testable unproven beliefs |
| `questions/` | Question | Unresolved inquiries with owner |
| `reflections/` | Reflection | Postmortem lessons, self-improvement observations |
| `sources/` | Source | Documents, URLs, transcripts, artifacts cited by other nodes |
| `metrics/` | Metric | Measured value + timestamp + source |
| `system/` | Agent, Asset, Event, Task, Project | System-level operating units that don't fit elsewhere |

### `entities/`

| Dir | What lives here |
|---|---|
| `agents/` | One file per constellation agent (COVENANT, ALTAIR, ARCTURUS, …) |
| `companies/` | Arcturion Group, EKLYPTYK, Arcturion Capital, Dark Ascendant, vendors |
| `people/` | Founder, household, advisors, employees, contacts of standing |
| `projects/` | Active project compiled views |
| `concepts/` | Compiled concept summaries (distinct from atomic `nodes/concepts/`) |
| `systems/` | Memory system, hardware, infrastructure system summaries |

## 5. Required Body Sections — Nodes

Every node MUST contain these H2 sections in order:

1. `## Summary` — one paragraph
2. `## Claim / Content` — the atomic memory
3. `## Evidence` — table with `Source | Support`
4. `## Relationships` — table with `Subject | Verb | Object | Confidence | Source`
5. `## Implications` — what agents should do differently
6. `## Open Questions` — or `- None.`
7. `## Maintenance` — review trigger

## 6. Required Body Sections — Entities

1. `## Current Summary`
2. `## Canonical Sources` — table
3. `## Key Claims` — table with confidence
4. `## Relations` — table
5. `## Open Questions`
6. `## Maintenance Notes`

## 7. Validation Rules (enforceable)

A file passes COVENANT validation iff:

- ✅ Frontmatter parses as YAML
- ✅ Every `required` field above is present and non-empty
- ✅ `id` matches `^mem_\d{8}_[a-z0-9_]+_[a-f0-9]{4,8}$` (nodes only)
- ✅ `node_type` is in §3 closed vocabulary (nodes)
- ✅ `entity_type` is in §3 closed vocabulary (entities)
- ✅ `last_verified` (entities) or `last_reviewed` (nodes) is within `expires` window
- ✅ At least one row in `Relationships` / `Relations` OR `canonical_files` non-empty OR `canonical_sources` non-empty (no orphans)
- ✅ All seven (nodes) or six (entities) body sections present
- ✅ No secrets in frontmatter or body (no raw passwords, API keys, tokens, OAuth codes)

A file FAILS validation on any missing required field — logged to `_validation_<date>.log` and surfaced to COVENANT.

## 8. Authority

- This SCHEMA.md supersedes any conflicting guidance in `0001_ENTITY_SCHEMA.md`, `templates/NODE_TEMPLATE.md`, or `templates/ENTITY_TEMPLATE.md` until those templates are updated to match.
- Updates require COVENANT review + Founder approval (Tier 1 governance).
- Validation runs on every COVENANT graph audit; failures block promotion.

## 9. References

- `06_MEMORY_SYSTEM/0611_LLM_WIKI/0000_README.md`
- `06_MEMORY_SYSTEM/0611_LLM_WIKI/0001_ENTITY_SCHEMA.md`
- `06_MEMORY_SYSTEM/0611_LLM_WIKI/0005_NODE_TAXONOMY.md`
- `06_MEMORY_SYSTEM/0611_LLM_WIKI/0006_RELATIONSHIP_TAXONOMY.md`
- `06_MEMORY_SYSTEM/0611_LLM_WIKI/nodes/concepts/infinite_brain_architecture.md`
