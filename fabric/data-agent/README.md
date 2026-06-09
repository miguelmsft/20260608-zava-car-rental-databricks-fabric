# Zava Fleet Insights — Fabric Data Agent (GA) item definition

This folder is the **item definition** for the Zava **Fabric Data Agent (GA)** created by
[`fabric/scripts/70_create_data_agent.py`](../scripts/70_create_data_agent.py) (plan **Step 17**).
The Data Agent is the *"insights beyond Power BI"* pillar — it answers plain-English questions
over Zava's governed data using generative AI (NL2DAX / NL2GQL), under the calling user's
identity and permissions (RLS/CLS honored — R3 §5.4).

## What it grounds on (R3 / R4)

| Data source | Definition `type` | Query language | Origin |
|---|---|---|---|
| **Zava Fleet Analytics** | `semantic_model` | NL2DAX | Step 14 Direct Lake semantic model |
| **Zava Fleet Ontology Graph** | `graph` | NL2GQL | Step 16 ontology's **auto-created** managed Graph (GA) |

> **Ontology attach is a documented UI step (R4 §6 / Limitation 6).** The Data Agent
> definition REST enum exposes `graph` and `semantic_model`, **but not `ontology`**, so the
> ontology cannot be attached *as an ontology source (NL2Ontology)* programmatically in the
> current schema. The **graph source** (the ontology's auto-created Graph) is the documented
> **programmatic path** to ontology-backed reasoning and is what this definition uses. To add
> the ontology directly for NL2Ontology routing, use the Fabric UI (see plan Step 17 manual
> note; consolidated into `docs/manual-steps.md` by Step 25).

## Layout (per R3 §4.2 item-definition format)

```
Files/Config/
├── data_agent.json                                       # top-level $schema (dataAgent 2.1.0)
├── draft/
│   ├── stage_config.json                                 # aiInstructions (agent instructions)
│   ├── semantic_model-ZavaFleetAnalytics/
│   │   └── datasource.json                                # NL2DAX binding (no few-shots: not supported for semantic models)
│   └── graph-ZavaFleetOntologyGraph/
│       ├── datasource.json                                # NL2GQL binding (graph source = ontology's auto Graph)
│       └── fewshots.json                                  # example NL -> GQL pairs
└── publish_info.json                                      # publish metadata
```

The deploy script reads these **draft** parts plus `data_agent.json` / `publish_info.json`,
substitutes deploy-time tokens (see below), and assembles the Base64 `definition.parts`. The
**published** stage is produced by mirroring the draft parts (R3 §9: there is no separate
"publish" REST endpoint — publishing is achieved by deploying a definition that contains the
`published/` stage parts via `updateDefinition`).

### Few-shot scope (R3 §5.2)

Example query pairs are **not supported for Power BI semantic model** data sources, so only the
**graph** source carries a `fewshots.json` (NL → GQL). Semantic-model accuracy comes from the
model's **Prep for AI** configuration, not agent-level instructions.

## Deploy-time tokens (substituted by `70_create_data_agent.py`; never committed as real ids)

| Token | Replaced with |
|---|---|
| `__ZAVA_WORKSPACE_ID__` | Step-10 workspace GUID |
| `__ZAVA_SEMANTIC_MODEL_ARTIFACT_ID__` | Step-14 semantic model item id (resolved by name) |
| `__ZAVA_SEMANTIC_MODEL_DISPLAY_NAME__` | Resolved semantic model display name |
| `__ZAVA_GRAPH_ARTIFACT_ID__` | Step-16 ontology's auto-created Graph item id (resolved by name/type) |
| `__ZAVA_GRAPH_DISPLAY_NAME__` | Resolved Graph display name |

When `features.enable_ontology=false` (or no Graph is resolvable), the `graph-*` parts are
**dropped** and the agent is deployed with the semantic-model source only — so it still works
even when the only preview item (ontology) is unavailable.

## Endpoints (R3 §4.1)

```
POST /v1/workspaces/{workspaceId}/dataAgents                              (create w/ definition)
GET  /v1/workspaces/{workspaceId}/dataAgents                             (list / find-by-name)
GET  /v1/workspaces/{workspaceId}/dataAgents/{id}                        (get)
POST /v1/workspaces/{workspaceId}/dataAgents/{id}/updateDefinition       (idempotent update)
POST /v1/workspaces/{workspaceId}/dataAgents/{id}/getDefinition          (inspect deployed sources)
```

**No secrets** live here: only item names, descriptions, instructions, and placeholder tokens.
Authentication is acquired at runtime via `DefaultAzureCredential` / `az login`.
