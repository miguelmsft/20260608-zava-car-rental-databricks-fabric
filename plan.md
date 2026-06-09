# Implementation Plan — Zava Car Rental: Databricks + Fabric Data Foundation Demo

> **Source of truth during build.** This plan is a living document and a progress tracker.
> Step status markers: `⬜ Not started`, `🔄 In progress`, `✅ Done`, `⏸️ Blocked`.
> Every step carries an explicit **Status:** field (in addition to the marker in its heading).
> Research citations use the report IDs **R1–R11** in `research/2026-06-08-r*.md`.

---

## 1. System Overview

**Plan slug:** `zava-databricks-fabric-data-foundation`

### 1.1 What is being built

A **public, reusable demo** that shows how **Azure Databricks** and **Microsoft Fabric** combine into a single, governed, near-real-time data foundation for a fictional large car-rental company, **Zava** (Seattle HQ + multiple US-city sites). Zava's data engineers work in Azure Databricks (Unity Catalog medallion); Zava's business users work in Power BI. The demo proves that Fabric *accelerates* the existing Databricks estate **without ripping it out**, and that **Microsoft Purview** + **Policy Weaver** deliver end-to-end governance. Everything is deployable as code (Bicep, Python/REST, Databricks Asset Bundles, PySpark, `semantic-link-labs`, Fabric REST/`fabric-cicd`), with every unavoidable manual UI step explicitly documented so a customer can clone the repo and redeploy in their own tenant.

The narrative arc: a **certified gold data asset** produced in Databricks becomes **governed business insight** in Fabric — a Direct Lake Power BI report, natural-language answers from a Fabric Data Agent, an ontology/graph for cross-entity reasoning, and a proactive Operations Agent watching live telematics — all under one consistent security and lineage story.

### 1.2 Business value framing (the "why" — value, not tools)

| Value | How the demo shows it |
|---|---|
| **Agility** | Zero-ETL no-copy mirroring + Direct Lake → business insight minutes after data lands, no pipeline rebuild. |
| **Observability** | Lineage in Purview, mirroring sync state, pipeline run state, and an Operations Agent that watches live data and proactively acts. |
| **Efficiency** | One governed copy of data (no duplication/egress), code-first redeploy, certified asset reused across BI + AI consumers. |
| **Governance** | Policy Weaver replicates UC access policies into OneLake security; Purview gives catalog, classification, sensitivity labels, DLP, and lineage. |

### 1.3 Capabilities (the four equal pillars)

1. **No-copy mirroring** — mirror Databricks Unity Catalog → Fabric OneLake (zero ETL, ~15-min metadata sync) **with TWO ingestion variations** (see §1.4).
2. **Direct Lake reporting** — Power BI Direct Lake on OneLake over the landed data + a thin Fabric aggregation layer → an impressive blue-themed Zava report (multi-city maps, decomposition trees, forecasting).
3. **Ontology insights (Fabric IQ — GA workload)** — a layered approach: semantic model (GA) → **Ontology (preview**, GA expected "in the coming months" — R4) → auto **Graph (GA)** → **Fabric Data Agent (GA)** for NL Q&A, **plus** a proactive **"watch + act"** layer over a real-time **Eventhouse/KQL** telematics feed. The watch+act default is the **Fabric Activator (GA) native Email alert — Teams-free** (email the site manager when an idle-vehicle / overdue-maintenance threshold trips, deployed as code), with the **Operations Agent (GA)** offered as an **optional, Teams-requiring enhancement** (LLM-reasoned recommendation + Teams Yes/No approval card) — R11 §6.
4. **Governance** — **Policy Weaver** (UC access → OneLake security) + **deep Microsoft Purview** (catalog, lineage, sensitivity labels, DLP).

### 1.4 Two ingestion variations (Pillar 1) — per R10

- **Variation 1 — Mirroring (standard UC Delta).** The medallion **gold "certified data asset"** is mirrored into OneLake via the **Mirrored Azure Databricks Catalog** item (R1).
- **Variation 2 — Spark Declarative Pipelines (Lakeflow/DLT).** Streaming tables / materialized views **cannot be mirrored** (R1, R10), so a **OneLake shortcut to the Databricks-managed ADLS Gen2 storage** is created and **secured** per R10's "Enable network security access for your ADLS Gen2 account" hardening. Default sub-pattern is **2A** (shortcut directly to managed storage, matching demo intent, with the documented UC-bypass governance caveat); **2B** (Lakeflow sink → dedicated external location → shortcut) is documented as the cleaner-governance alternative.

Both variations feed the **same** downstream: Direct Lake → semantic model → ontology → Data Agent → report → governance.

### 1.5 Key constraints

- **Region:** **East US 2** — the single US region that supports **ALL** required capabilities together (see §1.7 region decision + verification matrix). Note: **East US is explicitly rejected** because the **Operations Agent (GA) is unavailable in East US** (R11 §8 region exclusion: "available in Microsoft Fabric regions, excluding South Central US and East US"). The optional Operations Agent is GA but retains this region exclusion; the **default Activator (GA) email path** is available in all Fabric regions.
- **Capacity:** **F64 recommended** for Data Agent/Copilot/ontology (R9); 60-day trial capacity usable for non-AI data-engineering work only (trial excludes Copilot/Data Agent/AI **and** Operations Agent — R9, R11). Pause/resume for cost control. Pre-deploy cost checkpoint required.
- **Subscription/identity:** MCAPS subscription (name starts `ME-MngEnvMCAP`); identity `admin@MngEnvMCAP422553.onmicrosoft.com`. Do **not** use `migmartinez@microsoft.com` / HNLI-DEV.
- **Provisioning flexibility:** must support **fresh** provisioning of Databricks and/or Fabric **OR** **existing** resources if provided (parameterized).
- **Public repo:** **NO secrets committed.** Use Key Vault, OIDC / managed identity, service principals, placeholders. Redact secrets in all docs/logs.
- **Branding:** Zava **blue tones**. All data **synthetic**.

### 1.6 Non-functional requirements (defaults flagged as assumptions where unstated)

| NFR | Target | Source |
|---|---|---|
| Performance | Direct Lake interactive (<3 s typical visuals on F64 guardrails); mirroring metadata sync ~15 min (R1); Operations Agent evaluates on a fixed cadence (default ~5 min — R11). | R1, R9, R11 |
| Security/auth | SP + OIDC/managed identity for automation; **user-token** only where APIs require it (mirroring create, Operations Agent create — R1/R7/R11); Key Vault for any secret; no secrets in repo. | R7, R10, R11 |
| Reliability | Idempotent deploy scripts (re-runnable); explicit teardown; capacity pause/resume; retry/backoff for long-running Fabric REST + 15-min mirror waits. | R7, R9 |
| Observability | Purview lineage/catalog; deploy logs; mirroring & pipeline run-state checks; Operations Agent. | R6, R11 |
| Compliance | Synthetic data only; sensitivity labels + DLP demonstrated on non-real PII. | R6 |
| Cost | ~$138–300/mo with aggressive pausing (R9); pre-deploy cost checkpoint. | R9 |

### 1.7 Region decision & capability verification matrix (resolves R11 vs East US conflict)

**Decision: East US 2.** It is the only US region where every required capability is available *together*, including the Operations Agent, with **local** Azure OpenAI processing for Copilot/Data Agent (no cross-region routing).

| Required capability | East US 2 supported? | Verifying report + evidence |
|---|---|---|
| All Fabric workloads | ✅ | R9 §3 US Region Feature Matrix (line 174) |
| Mirrored Azure Databricks Catalog | ✅ | R9 line 174 |
| Direct Lake | ✅ | R9 line 84 (all F SKUs) + line 174 |
| Copilot / Data Agent (local AI) | ✅ local | R9 line 174 (✅ local) + line 149 (East US 2 listed as a local Azure OpenAI datacenter) |
| Ontology (preview, GA imminent) / Graph (GA) | ✅ | R9 line 174 (only South Central US is ❌) |
| **Operations Agent (GA)** | ✅ | R11 §8 — "available in Microsoft Fabric regions, **excluding South Central US and East US**" → East US 2 is supported |
| Real-Time Intelligence (Eventhouse/KQL/Eventstream) | ✅ | Core Fabric workload, available in all Fabric regions (R9 line 174 "All Fabric Workloads") |
| Azure Databricks | ✅ | R9 line 174 |

**Why not the alternatives:** **East US** fails the Operations Agent requirement (R11 line 150). **South Central US** fails Ontology (R9 line 178, ❌). **West US 2 / West US 3** support Operations Agent but route Copilot/Data Agent AI to another US region (R9 lines 176–177), adding latency/cross-region nuance. **West US** is a viable backup (all ✅ local) but East US 2 is preferred per repo conventions (East US 2 / West US 3) and proximity defaults. No capability is left unmet, so **no region-split or region-constrained-optional fallback is required** for the primary path; West US is documented as the drop-in backup region in `docs/cost.md`/`docs/architecture.md`.

---

## 2. Architecture

### 2.1 Architecture diagram (layered flow, both ingestion variations)

```
                          ┌──────────────────────────── AZURE DATABRICKS (existing-or-fresh) ────────────────────────────┐
                          │  Unity Catalog (Premium, UC-enabled)                                                          │
                          │                                                                                               │
 Synthetic Zava data ───► │  raw ─► bronze ─► silver ─► GOLD (certified data asset)   ◄── Variation 1 source             │
 (data/ generator)        │                                   │  UC tags/comments = CERTIFIED                            │
                          │                                   │                                                          │
                          │  Lakeflow Spark Declarative Pipeline (SDP/DLT)                                               │
                          │        └─► streaming tables / materialized views  ◄── Variation 2 source                     │
                          │                  │ (UC-managed ADLS Gen2 storage, abfss://__unitystorage/...)                │
                          └──────────────────┼───────────────────────────────┼──────────────────────────────────────────┘
                                             │ V1: Mirrored ADB Catalog       │ V2: OneLake shortcut to ADLS Gen2
                                             │ (metadata-only, ~15-min sync)  │ (secured: trusted workspace access,
                                             │                                │  Workspace Identity, firewall, no public)
                          ┌──────────────────▼────────────────────────────────▼────────────────────── MICROSOFT FABRIC (F64, East US 2) ──┐
                          │  OneLake                                                                                                       │
                          │   ├── Mirrored Azure Databricks Catalog (V1)   ── shortcuts → Delta gold                                       │
                          │   └── Lakehouse + ADLS Gen2 shortcut (V2)      ── Delta streaming/MV                                           │
                          │                          │                                                                                     │
                          │         thin Fabric gold / aggregation layer (V-Order; heavy calcs)  [R2]                                      │
                          │                          │                                                                                     │
                          │   Power BI Semantic Model (Direct Lake on OneLake, GA)  ──►  Power BI Report (PBIP/PBIR; maps, dec-tree, fcst) │
                          │                          │                                                                                     │
                          │   Fabric IQ (GA workload):  Ontology (preview, GA imminent) ──auto──► Graph (GA)                                │
                          │                          │                                                                                     │
                          │   Fabric Data Agent (GA, NL Q&A via REST)                                                                       │
                          │                                                                                                                 │
                          │   Real-Time Intelligence:  Eventstream (telematics feed) ─► Eventhouse / KQL DB+table                           │
                          │                                         │                                                                       │
                          │   WATCH+ACT (DEFAULT, Teams-free): Fabric Activator (GA) Email rule on Eventhouse prop / Ontology entity        │
                          │        ─► email site manager  (+ optional Fabric-item action: run pipeline/notebook → write work-order row)     │
                          │   WATCH+ACT (OPTIONAL, needs Teams): Operations Agent (GA) KustoDatabase source ─► Teams Yes/No approval card    │
                          └──────────────┬───────────────────────────────────────────────────────────────────────────────────────────────┘
                                         │
   GOVERNANCE (cross-cutting):           │
   • Policy Weaver (Beta v0.4.0): UC grants/row-filters/column-masks ─► OneLake Security roles  [R5]
   • Microsoft Purview: scan Databricks UC + Fabric tenant; catalog, classification, sensitivity labels, DLP;
     lineage (NOTE: Databricks→Fabric lineage does NOT auto-stitch — narrated "seam") [R6]
```

Legend: **existing components** = Azure Databricks workspace/UC if customer supplies one; **new components** = everything provisioned by this repo (Fabric capacity/workspace, mirrored catalog, shortcuts, semantic model, report, ontology, graph, Eventhouse/Eventstream, agents, Policy Weaver run, Purview scans).

### 2.2 File structure (new/changed areas)

```
infra/                              # Azure resources — Bicep (R8, R9)
  main.bicep                        # Orchestrating template (subscription/RG scope)
  modules/
    fabric-capacity.bicep           # Microsoft.Fabric/capacities (F64, East US 2), admin members
    databricks-workspace.bicep      # Microsoft.Databricks/workspaces (Premium)
    access-connector.bicep          # Microsoft.Databricks/accessConnectors (managed identity)
    storage-adls.bicep              # ADLS Gen2 (HNS) for UC managed storage + external location
    network-hardening.bicep         # Storage firewall, resource instance rules, disable public access (R10)
    keyvault.bicep                  # Key Vault for any required secret material
  params/
    dev.bicepparam                  # Fresh-provision parameters (East US 2, F64)
    existing-resources.bicepparam   # Bring-your-own Databricks/Fabric parameters
  README.md
databricks/                         # Databricks as code (R8, R10)
  bundle/databricks.yml             # Asset Bundle (DABs) root, dev/prod targets
  uc/
    01_metastore_external_access.sql# Enable external data access at metastore (mirroring prereq)
    02_catalog_schema.sql           # Catalog/schema creation (zava)
    03_grants_mirroring.sql         # EXTERNAL USE SCHEMA + USE CATALOG/SCHEMA + SELECT grants
    04_certify_gold.sql             # UC tags/comments → CERTIFIED gold asset
    05_access_policies.sql          # Row filters + column masks (Policy Weaver source policies)
  notebooks/
    00_generate_synthetic_data.py   # Loads synthetic Zava data into raw
    10_bronze.py                    # raw → bronze
    20_silver.py                    # bronze → silver
    30_gold.py                      # silver → gold (Variation 1 certified asset)
  pipelines/
    lakeflow_sdp.py                 # Variation 2: Spark Declarative Pipeline (streaming tables/MVs)
    lakeflow_sink_external.py       # Variation 2B (optional): sink → dedicated external location
  config/databricks_config.sample.json
  README.md
data/                               # Synthetic data generator + schema (R conventions)
  generate_zava_data.py             # Deterministic synthetic generator (blue-brand, US cities)
  generate_telematics_stream.py     # Synthetic real-time telematics event feed (Operations Agent source)
  schema/                           # Entity schemas + KPI definitions
  README.md
fabric/                             # Fabric as code (R1–R4, R7, R10, R11)
  config/deploy_config.sample.json  # Workspace, capacity, source bindings, feature flags
  scripts/
    00_create_workspace.py          # Create/attach Fabric workspace + assign capacity + Workspace Identity
    10_create_mirrored_catalog.py   # Variation 1: Mirrored ADB Catalog (REST; user-token)
    20_create_shortcut.py           # Variation 2: OneLake ADLS Gen2 shortcut (REST)
    30_create_semantic_model.py     # Direct Lake model via semantic-link-labs
    40_build_thin_gold.py           # Fabric aggregation layer (V-Order) for heavy calcs
    50_deploy_report.py             # Deploy PBIP/PBIR via fabric-cicd / Git
    60_create_ontology.py           # Ontology (preview): REST definition + doc UI-gen fallback
    70_create_data_agent.py         # Data Agent (GA) via Fabric Data Agent REST API (primary)
    75_create_eventhouse.py         # RTI: Eventhouse + KQL DB/table + Eventstream (Activator + Operations Agent source)
    78_create_activator_email.py    # DEFAULT watch+act: Fabric Activator (Reflex) native Email alert, Teams-free (deploy as code)
    80_create_operations_agent.py   # OPTIONAL watch+act: Operations Agent (GA, Teams) via OperationsAgent REST (KustoDatabase source)
  semantic-model/                   # TMDL/PBIP project for the Direct Lake model
  report/                           # PBIR report project (Desktop-authored)
  ontology/ontology_definition.json # Ontology item definition (graph-source fallback)
  data-agent/                       # Data Agent item definition (instructions, sources, examples)
  realtime/
    eventhouse_setup.kql            # KQL DB/table DDL + ingestion mapping for telematics
    eventstream_definition.json     # Eventstream item definition (telematics feed → Eventhouse)
  activator/reflex_entities.json    # DEFAULT: Activator/Reflex definition (EmailMessage action + optional Fabric-item work-order)
  operations-agent/Configurations.json # OPTIONAL: OperationsAgentV1 definition (shouldRun, KustoDatabase source, actions)
  theme/zava-blue-theme.json        # Power BI blue theme
  README.md
scripts/                            # Cross-cutting orchestration (R7, R9)
  deploy.py                         # Idempotent end-to-end orchestrator (waves)
  preflight_checks.py               # Tenant settings, SP, capacity, region, config-schema validation
  config_schema.py                  # Canonical config loader + validator (used by deploy/preflight)
  pause_capacity.py / resume_capacity.py
  teardown.py
  governance/
    policy-weaver/policy_weaver_config.yaml + run_policy_weaver.py   # R5
    purview/setup_purview_scans.py + lineage_runbook.md             # R6
docs/                               # Customer-facing guidance
  prerequisites.md
  manual-steps.md                   # Consolidated Manual Steps Appendix
  architecture.md
  cost.md                           # Cost checkpoint + pause/resume guidance + West US backup region note
  runbook-end-to-end.md
README.md                           # Top-level (updated in Phase 7)
```

---

## 3. Tech Stack

### 3.1 Existing codebase analysis

Greenfield scaffold. The repo currently contains only orchestration/working material (`README.md` skeleton, `demo-status.md`, `research/`, `agent-reviews/`, `.github/copilot-instructions.md`) and empty target folders (`infra/`, `databricks/`, `fabric/`, `data/`, `scripts/`, `docs/`). No existing language/build/test conventions to align to beyond the conventions file. Therefore: stack is **selected** per the conventions and research, and all assumptions are surfaced in §9.

### 3.2 Recommended stack

| Component | Choice | Rationale |
|---|---|---|
| Azure IaC | **Bicep** | Mandated by conventions; `Microsoft.Fabric/capacities`, `Microsoft.Databricks/*`, ADLS, Key Vault all supported (R8, R9). |
| Databricks objects | **Databricks SQL + CLI/Terraform**, **Asset Bundles (DABs)** | UC objects are NOT Bicep resources (R8); DABs deploy notebooks/jobs/Lakeflow pipelines. |
| Synthetic data | **Python (PySpark in notebooks + local generator)** | Deterministic, reproducible, no external data. |
| Fabric items | **Fabric REST + `fabric-cli` (`fab`) + `fabric-cicd` + `semantic-link-labs`** | Per conventions/R7; covers workspace, mirroring, shortcuts, semantic model, report, Eventhouse, agents. |
| Semantic model | **`semantic-link-labs` `generate_direct_lake_semantic_model()` + TOM** | One-call Direct Lake model over Mirrored ADB Catalog; programmatic measures/RLS (R2). |
| Report | **Power BI Desktop → PBIP/PBIR**, deployed via `fabric-cicd`/Git | Advanced visuals (maps, decomposition tree, forecasting) authored in Desktop, deployed as code (R2). |
| Ontology/Graph | **Fabric IQ (GA workload): Ontology (preview, GA imminent) + Graph (GA)** | Layered Option C; REST definition + documented UI-gen fallback (R4). |
| NL insights | **Fabric Data Agent (GA)** | Multi-source NL Q&A; **created via the Fabric Data Agent REST API** (`POST /v1/workspaces/{id}/dataAgents` + definition), with `fabric-cicd`/Git as secondary ALM path (R3, R7). |
| Real-time source | **RTI Eventhouse + KQL DB/table + Eventstream** | Shared `KustoDatabase` source for time-series telematics — feeds the Activator email rule and the optional Operations Agent (R11). |
| Watch+act (default) | **Fabric Activator (GA) — native Email action, Teams-free** | Deployed as code via Reflex REST (`POST …/reflexes`) + `ReflexEntities.json` `EmailMessage` action; monitors the same Eventhouse properties + Ontology entities; no Teams dependency (R11 §6c). |
| Watch+act (optional) | **Operations Agent (GA)** | LLM-reasoned recommendation + Teams Yes/No approval; OperationsAgent REST (`shouldRun`, user-token); **requires Microsoft Teams** (R11 §6, §8). |
| Access policy sync | **Policy Weaver v0.4.0 (`pip install policy-weaver`)** | UC grants/filters/masks → OneLake security (R5). |
| Governance | **Microsoft Purview (Unified Catalog + Data Map)** | Catalog, lineage, labels, DLP across Databricks UC + Fabric (R6). |
| Languages | **Python, Bicep, SQL, PySpark, KQL** | Per conventions (+ KQL for the Eventhouse). |
| CI/CD | **GitHub Actions + OIDC** (optional, documented) | Unattended deploy where APIs allow SP; manual/user-token where required (R7). |

### 3.3 Target region & capacity plan

- **Region:** **East US 2** (see §1.7 verification matrix — only US region supporting ALL capabilities incl. Operations Agent, with local Copilot/Data Agent AI). **East US is rejected** (Operations Agent unavailable — R11 line 150). **West US** is the documented drop-in backup.
- **Capacity:** F64 (`Microsoft.Fabric/capacities`, SKU `F64`) in East US 2. Pay-as-you-go ~$11.52/hr (R9). 60-day **trial capacity** documented as an option for **non-AI** phases only (no Copilot/Data Agent/Operations Agent on trial — R9, R11).
- **Pause/resume:** `scripts/pause_capacity.py` / `resume_capacity.py` (suspend/resume on `Microsoft.Fabric/capacities`). Cost guidance in `docs/cost.md`. **Pre-deploy cost checkpoint** is a gated step (Step 4).

### 3.4 Required Fabric tenant settings (admin — user has rights)

- Service principals can use Fabric APIs (scoped to a security group).
- Mirroring: **"Enable new mirrored catalog items (preview)"** (R1).
- Copilot/AI + Data Agent enabled; **Fabric IQ (GA workload)** enabled; **ontology (preview)** setting (R3, R4).
- **Graph (GA)** tenant setting (ontology auto-creates the graph — R4/R11).
- **Fabric Activator** (Reflex) enabled — the default Teams-free Email alert path; **Operations Agent (GA)** admin switch + **Real-Time Intelligence** settings for the optional Teams path (tenant UI may still label Operations Agent "(preview)" pending doc refresh — it is GA per the Build 2026 Azure blog); **Copilot + Azure OpenAI** enabled (needed only for the optional Operations Agent); cross-geo AI only outside US/EU (not required for East US 2 — R11).
- Git integration + deployment pipelines enabled (R7).
- OneLake security (preview) settings (R1, R5).
- Purview Fabric live view / tenant scan settings (R6).

### 3.5 Service principals & identities

- **Deploy SP** — Bicep/Fabric automation (Fabric API access via security group; Contributor on RG).
- **Databricks access connector managed identity** — UC storage credential (R8).
- **Fabric Workspace Identity** — trusted workspace access to hardened ADLS Gen2 (R10).
- **Policy Weaver identity** — Databricks SDK read + Microsoft Graph + Fabric `dataAccessRoles` write (R5).
- **User identity** (`admin@MngEnvMCAP...`) — required for user-token-only operations (mirroring create, Operations Agent create — R1, R7, R11).

---

## 4. Prerequisites (captured in `docs/prerequisites.md`)

1. Azure subscription `ME-MngEnvMCAP...`; user `admin@MngEnvMCAP422553.onmicrosoft.com` with Contributor + Fabric admin.
2. Fabric admin rights to set tenant settings (§3.4) — confirmed available.
3. Deploy service principal created + added to the Fabric-API security group.
4. Tooling (pinned minimum versions — see §7): Azure CLI + Bicep, Python ≥ 3.11, Databricks CLI, `ms-fabric-cli` (`fab`), `fabric-cicd`, `semantic-link-labs`, `policy-weaver`, Power BI Desktop (for report authoring). **Microsoft Teams account is OPTIONAL** — required only for the optional Operations Agent enhancement (Step 20); the default Fabric Activator Email alert path needs no Teams (R11 §6).
5. Decision: **fresh** vs **existing** Databricks/Fabric (drives which Bicep params + steps run).
6. **Cost checkpoint** acknowledged (§3.3, Step 4).
7. No secrets in repo; Key Vault + placeholders used everywhere.

---

## 5. Step-by-Step Implementation

Each step lists **Status**, **Files**, **Depends on**, **Tasks**, **Verification**, and **Manual steps**.

### Phase 0 — Foundations

#### Step 1: Repo config schema contract, conventions & parameterization — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/config/deploy_config.sample.json`, `databricks/config/databricks_config.sample.json`, `scripts/config_schema.py`, `infra/params/dev.bicepparam`, `infra/params/existing-resources.bicepparam`, `.gitignore` (update)
**Depends on:** none

**Tasks:**
- [ ] Implement the **canonical config schema contract** below in `scripts/config_schema.py` (a single loader/validator used by `deploy.py`, `preflight_checks.py`, and Fabric scripts). Validation is **fail-fast**: on any missing required field, wrong type, bad enum value, or unmet conditional, raise a clear error naming the exact JSON path (e.g., `deploy_config.capacity.existing_capacity_id is required when capacity.use_existing=true`).
- [ ] Author the two sample configs (`deploy_config.sample.json`, `databricks_config.sample.json`) with placeholder values only (no secrets); secrets are referenced by Key Vault name or acquired via `az login` at runtime — never stored in config.
- [ ] Add `*.local.json`, `*.tfvars`, `.env`, secret patterns to `.gitignore`.
- [ ] Document the fresh-vs-existing switch semantics (which steps are skipped when bringing your own resources — see "fresh vs existing paths" below).

**Config schema contract — `deploy_config.sample.json` (Fabric/orchestration):**

| Field path | Type | Req? | Example | Used by | Validation rule |
|---|---|---|---|---|---|
| `region` | string | ✅ | `"eastus2"` | Steps 5,10,21 | Must be in supported set `{eastus2, westus}`; default `eastus2`. East US/South Central US rejected (§1.7). |
| `capacity.sku` | string | ✅ | `"F64"` | Step 5 | Regex `^F(2\|4\|8\|16\|32\|64\|128\|...)$`; AI phases require ≥ F64 unless Copilot Capacity configured. |
| `capacity.name` | string | ✅ | `"zava-fabric-cap"` | Steps 5,10 | 1–63 chars, lowercase/alnum/hyphen. |
| `capacity.use_existing` | bool | ✅ | `false` | Steps 5,10 | If `true`, `existing_capacity_id` required. |
| `capacity.existing_capacity_id` | string | cond. | `"/subscriptions/.../capacities/x"` | Step 5 | Required iff `use_existing=true`; must be a valid ARM resource id. |
| `workspace.name` | string | ✅ | `"zava-fabric-ws"` | Step 10 | Non-empty. |
| `workspace.use_existing` | bool | ✅ | `false` | Step 10 | If `true`, `existing_workspace_id` required. |
| `workspace.existing_workspace_id` | string (GUID) | cond. | `"<guid>"` | Step 10 | Required iff `use_existing=true`; GUID format. |
| `workspace.identity_object_id` | string (GUID) | optional | `"<guid>"` | Steps 7,12 | Populated **after** Workspace Identity creation (Step 10); placeholder until then. |
| `source.databricks_catalog` | string | ✅ | `"zava"` | Steps 11,16,17 | Non-empty. |
| `source.gold_schema` | string | ✅ | `"gold"` | Steps 11,13 | Non-empty. |
| `ingestion.variation` | enum | ✅ | `"2A"` | Steps 9,12 | One of `{2A, 2B}`; default `2A`. |
| `features.enable_ontology` | bool | ✅ | `true` | Step 16 | — |
| `features.enable_data_agent` | bool | ✅ | `true` | Step 17 | — |
| `features.enable_eventhouse` | bool | ✅ | `true` | Step 18 | If `enable_activator_email=true` or `enable_operations_agent=true`, must be `true` (shared Eventhouse/`KustoDatabase` source). |
| `features.enable_activator_email` | bool | ✅ | `true` | Step 19 | Default `true` (Teams-free watch+act). Requires `enable_eventhouse=true`. |
| `features.enable_operations_agent` | bool | ✅ | `false` | Step 20 | Optional Teams enhancement. Requires `enable_eventhouse=true`, `region∈{eastus2,westus}`, and a Microsoft Teams account. |
| `realtime.eventhouse_name` | string | cond. | `"zava-eh"` | Step 18 | Required iff `enable_eventhouse=true`. |
| `realtime.kql_database_name` | string | cond. | `"zava_rt"` | Steps 18,19,20 | Required iff `enable_eventhouse=true`. |
| `realtime.kql_table_name` | string | cond. | `"Telematics"` | Steps 18,19,20 | Required iff `enable_eventhouse=true`. |
| `alerting.site_manager_email` | string | cond. | `"manager@zava.example"` | Step 19 | Required iff `enable_activator_email=true`; Activator `EmailMessage.sentTo` recipient (placeholder; no real address committed). |
| `governance.purview_account` | string | optional | `"zava-purview"` | Step 22 | Required iff Purview steps enabled. |
| `governance.policy_weaver_enabled` | bool | ✅ | `true` | Step 21 | — |

**Config schema contract — `databricks_config.sample.json` (Databricks):**

| Field path | Type | Req? | Example | Used by | Validation rule |
|---|---|---|---|---|---|
| `workspace.use_existing` | bool | ✅ | `false` | Steps 6,8 | If `true`, `host_url` + `resource_id` required. |
| `workspace.host_url` | string | cond. | `"https://adb-xxx.azuredatabricks.net"` | Steps 8,9 | Required iff `use_existing=true`; https URL. |
| `workspace.resource_id` | string | cond. | `"/subscriptions/.../workspaces/x"` | Step 6 | Required iff `use_existing=true`; ARM id. |
| `workspace.sku` | enum | ✅ | `"premium"` | Step 6 | Must be `premium` (UC requirement — R8). |
| `catalog` | string | ✅ | `"zava"` | Steps 8,9 | Non-empty. |
| `managed_storage_account` | string | ✅ | `"zavauc"` | Steps 6,9 | ADLS Gen2 (HNS) account name. |
| `access_connector_id` | string | optional | `"/subscriptions/.../accessConnectors/x"` | Step 6 | Populated by Bicep output on fresh path. |
| `data_seed` | int | ✅ | `42` | Steps 3,8 | Fixed seed for deterministic data. |

**Fresh vs existing paths:**
- **Fresh Databricks** (`databricks_config.workspace.use_existing=false`): Steps 6 (Bicep workspace/connector/ADLS/KV), 7, 8, 9 run fully.
- **Existing Databricks** (`use_existing=true`): Step 6 is **skipped** (pass-through `host_url`/`resource_id`/`access_connector_id`); preflight validates Premium + UC enabled; Steps 8/9 target the existing workspace.
- **Fresh Fabric** (`deploy_config.capacity.use_existing=false`): Step 5 creates capacity; Step 10 creates workspace + Workspace Identity.
- **Existing Fabric** (`use_existing=true`): Step 5 emits no capacity (consumes `existing_capacity_id`); Step 10 attaches to `existing_workspace_id`.

**Verification:**
- [ ] `python scripts/config_schema.py --validate fabric/config/deploy_config.sample.json databricks/config/databricks_config.sample.json` exits 0 (valid sample) and prints the resolved fresh-vs-existing plan.
- [ ] Negative: a config with `capacity.use_existing=true` but no `existing_capacity_id` exits non-zero with the exact path in the error message.
- [ ] Negative: `features.enable_operations_agent=true` with `enable_eventhouse=false` exits non-zero (conditional rule).
- [ ] Negative: `region="eastus"` exits non-zero with a message citing §1.7 (Operations Agent unsupported).
- [ ] `git grep -nE "(password|secret|pat|client_secret)\s*[:=]\s*['\"][^'\"]+" -- . ':!*.sample.json'` returns no real secrets.

**Manual steps:** none.

#### Step 2: Prerequisites, tenant-settings & SP setup docs — ✅ Done
**Status:** ✅ Done
**Files:** `docs/prerequisites.md`, `docs/manual-steps.md` (create skeleton)
**Depends on:** Step 1

**Tasks:**
- [ ] Document all prerequisites (§4) and the exact Fabric tenant settings to enable (§3.4) with click-paths (R1, R3, R4, R6, R7, R11).
- [ ] Document SP/identity creation (deploy SP, Fabric API security group) with `az ad`/portal steps.
- [ ] Seed `docs/manual-steps.md` with the Manual Steps Appendix table structure (populated incrementally by later steps).

**Verification:**
- [ ] Doc lists every tenant setting required by R1/R3/R4/R6/R7/R11 with portal path (including Graph + Operations Agent/RTI settings; note that any portal labels still showing "preview" lag the Build 2026 GA).
- [ ] A reviewer can follow SP creation end-to-end without external lookups.

**Manual steps:** Tenant-setting toggles and SP creation are inherently admin-portal actions — documented here and cross-listed in the appendix.

#### Step 3: Synthetic Zava data generator (batch + telematics stream) — ✅ Done
**Status:** ✅ Done
**Files:** `data/generate_zava_data.py`, `data/generate_telematics_stream.py`, `data/schema/*`, `data/README.md`
**Depends on:** Step 1

**Tasks:**
- [ ] Generate deterministic synthetic batch data for entities: **Sites, Vehicles, VehicleClasses, Customers, Reservations, Rentals, Payments, Maintenance, Telematics** (Seattle HQ + multi-US-city sites; lat/long for maps). Use `data_seed` from config.
- [ ] Encode KPI source columns: fleet utilization, revenue/site, idle vehicles, one-way flows, maintenance cost.
- [ ] Include a couple of synthetic "PII-like" columns (e.g., customer email/phone) for label/DLP/column-mask demos — clearly synthetic.
- [ ] Implement `generate_telematics_stream.py`: a deterministic-but-time-advancing **telematics event feed** (vehicle_id, site_id, ts, ignition_state, idle_minutes, odometer, fault_code) emitting JSON events suitable for an Eventstream/Eventhouse ingest; include an injectable "idle-vehicle spike" / "maintenance fault spike" window for the watch+act demo (Steps 19–20).
- [ ] Output batch to Parquet/CSV with a fixed seed; document row counts and referential integrity.

**Verification:**
- [ ] `python data/generate_zava_data.py --out ./_tmp` produces all 9 entity files.
- [ ] Referential integrity check: every `Rentals.vehicle_id` exists in `Vehicles`; every `Rentals.site_id` exists in `Sites`.
- [ ] Determinism: two runs with same seed produce byte-identical row counts and key sets.
- [ ] Edge: one-way rentals (pickup_site ≠ return_site) present for the one-way-flow KPI.
- [ ] `python data/generate_telematics_stream.py --rate 10 --inject-spike` emits well-formed JSON events and a clearly elevated idle/fault window (verifiable by a count over the spike interval).

**Manual steps:** none.

### Phase 1 — Azure Infrastructure (Bicep)

#### Step 4: Cost checkpoint + capacity pause/resume scripts — ✅ Done
**Status:** ✅ Done
**Files:** `docs/cost.md`, `scripts/pause_capacity.py`, `scripts/resume_capacity.py`
**Depends on:** Step 1

**Tasks:**
- [ ] Document F64 vs trial cost model and the ~$138–300/mo pausing scenarios (R9); state the **pre-deploy cost gate**; note **West US backup region**.
- [ ] Implement pause/resume against `Microsoft.Fabric/capacities` (suspend/resume) — idempotent, safe to re-run.
- [ ] Document trial-capacity caveat (no Copilot/Data Agent/Operations Agent on trial — R9, R11).

**Verification:**
- [ ] `python scripts/pause_capacity.py --capacity <name> --dry-run` prints intended action without mutating.
- [ ] Resume after pause returns capacity to `Active` (manual confirm against a real/placeholder capacity).
- [ ] `docs/cost.md` states the explicit cost-acknowledgement gate before Step 5.

**Manual steps:** Pre-deploy **cost acknowledgement** by the customer (documented gate).

#### Step 5: Bicep — Fabric capacity module — ✅ Done
**Status:** ✅ Done
**Files:** `infra/modules/fabric-capacity.bicep`, `infra/main.bicep` (wire-in), `infra/params/dev.bicepparam`
**Depends on:** Step 1, Step 4
**Provisioning flag:** skipped when `capacity.use_existing=true` (consume existing capacity id).

**Tasks:**
- [ ] Author `Microsoft.Fabric/capacities` (SKU `F64`, **East US 2**, admin members) per R9; parameterize SKU/region/name.
- [ ] Support "existing capacity" path (output existing resource id, no create).
- [ ] Output capacity id/name for Fabric workspace assignment.

**Verification:**
- [ ] `az bicep build --file infra/modules/fabric-capacity.bicep` succeeds (no errors).
- [ ] `az deployment group what-if` shows exactly one F64 capacity to create in **East US 2** (fresh path).
- [ ] Existing path: with `capacity.use_existing=true`, what-if shows no capacity creation.

**Manual steps:** none (capacity is Bicep-automatable — R9).

#### Step 6: Bicep — Databricks workspace, Access Connector, ADLS Gen2, Key Vault — ✅ Done
**Status:** ✅ Done
**Files:** `infra/modules/databricks-workspace.bicep`, `infra/modules/access-connector.bicep`, `infra/modules/storage-adls.bicep`, `infra/modules/keyvault.bicep`, `infra/main.bicep` (wire-in)
**Depends on:** Step 1
**Provisioning flag:** skipped when `databricks.workspace.use_existing=true`.

**Tasks:**
- [x] Author `Microsoft.Databricks/workspaces` (Premium SKU — UC requires it, R8), `Microsoft.Databricks/accessConnectors` (managed identity), ADLS Gen2 (HNS) for UC managed storage + external location, Key Vault (R8). Region East US 2.
- [x] Role assignments: Access Connector MI → Storage Blob Data Contributor on the ADLS account (R8).
- [x] Parameterize names/region; support existing-resource pass-through outputs.

**Verification:**
- [x] `az bicep build` succeeds for each module and `main.bicep`.
- [ ] `what-if` (fresh) shows Premium workspace + access connector + ADLS(HNS) + Key Vault + role assignment.
- [ ] Existing path: with `databricks.workspace.use_existing=true`, what-if creates no Databricks resources.

**Manual steps:** none.

**Implementation Notes (2026-06-08):** Authored 4 modules (`access-connector.bicep`, `databricks-workspace.bicep`, `storage-adls.bicep`, `keyvault.bicep`) + `infra/main.bicep` wiring. Resource shapes follow R8 §2.3–2.6: workspace `Microsoft.Databricks/workspaces@2024-05-01` (Premium SKU, `accessConnector` MI attach, `defaultCatalog.initialType=UnityCatalog`); access connector `@2024-05-01` system-assigned MI; ADLS `Microsoft.Storage/storageAccounts@2023-05-01` `StorageV2`+`isHnsEnabled=true` with Storage Blob Data Contributor role assignment (`ba92f5b4-…`) scoped to the storage account; Key Vault `@2023-07-01` RBAC-enabled, no secrets. `main.bicep` gates Databricks resources behind `!useExistingDatabricks` and emits pass-through outputs on the existing path (workspace url/id, access connector id, storage name, KV uri). Params are consistent with Step 1 `databricks_config` (sku=premium, managed_storage_account=zavauc, access_connector) and the `dev`/`existing-resources` bicepparam files. **Verification:** `az bicep build` (CLI 0.43.8) exits 0 with 0 errors for all 5 files; `az bicep build-params` validates both `.bicepparam` files against `main.bicep` (0 errors). Remaining lint warnings are intentional `no-unused-params` for the Fabric-capacity params (Step 5 wires them; declared here so the shared param files validate). `what-if` items left unchecked — Phase 0 is author-only (no deploy); what-if requires `az login` + a target RG, deferred. Local implementation and verification complete; awaiting reviewer verdict.

#### Step 7: Bicep — author ADLS network-hardening module (Variation 2) — ✅ Done
**Status:** ✅ Done
**Files:** `infra/modules/network-hardening.bicep`, `infra/main.bicep` (conditional wire-in, NOT applied here), `docs/manual-steps.md` (append)
**Depends on:** Step 6

> **Scope note (split per reviewer):** This step **authors and unit-validates** the hardening module only. It does **not** apply the firewall lockdown or the Fabric Workspace Identity resource-instance rule — that requires the Workspace Identity object id, which does not exist until Step 10. **Final application + identity-bound verification + negative test live in Step 12** (which depends on Step 10). This ordering removes the impossible "verify a Workspace-Identity rule before the identity exists" problem.

**Tasks:**
- [ ] Author the hardening module: storage firewall default-deny, **resource instance rules**, **trusted workspace access**, disable public network access (R10 — "Enable network security access for your ADLS Gen2 account").
- [ ] Parameterize `workspaceIdentityObjectId` (consumed by Step 12 after Step 10 creates it); design so the module is a no-op when the parameter is empty/placeholder (authoring without applying).
- [ ] Document the ordering dependency (Workspace Identity must exist before final lockdown is applied in Step 12).

**Verification:**
- [ ] `az bicep build --file infra/modules/network-hardening.bicep` succeeds.
- [ ] `what-if` with a **placeholder** identity param shows the module parses and would set firewall `defaultAction=Deny` + trusted-workspace-access flag (structural only — no identity rule asserted yet).
- [ ] Lint/param check: module exposes `workspaceIdentityObjectId` as a required input for the apply phase (Step 12).

**Manual steps:** **Fabric Workspace Identity creation** is UI-assisted (created in Step 10); its object id is fed into this module during Step 12. Appended to appendix.

### Phase 2 — Databricks Data Foundation

#### Step 8: Unity Catalog setup, grants, medallion notebooks, certified gold (Variation 1 source) — ✅ Done
**Status:** ✅ Done
**Files:** `databricks/uc/01_metastore_external_access.sql`, `databricks/uc/02_catalog_schema.sql`, `databricks/uc/03_grants_mirroring.sql`, `databricks/uc/04_certify_gold.sql`, `databricks/notebooks/00_generate_synthetic_data.py`, `databricks/notebooks/10_bronze.py`, `databricks/notebooks/20_silver.py`, `databricks/notebooks/30_gold.py`, `databricks/bundle/databricks.yml`
**Depends on:** Step 3, Step 6

**Tasks:**
- [ ] UC: create `zava` catalog + schemas (raw/bronze/silver/gold); enable **external data access at metastore** (mirroring prereq — R8).
- [ ] Grants for mirroring: `EXTERNAL USE SCHEMA` + `USE CATALOG` + `USE SCHEMA` + `SELECT` on target objects (R8).
- [ ] Simple medallion PySpark: raw→bronze→silver→gold (keep simple — it's the setup, not the star).
- [ ] Certify gold via **UC tags/comments** (e.g., `certified=true`) — note tags do **not** propagate to Fabric (R8).
- [ ] Package notebooks/jobs as a Databricks Asset Bundle (DABs) with dev/prod targets (R8).

**Verification:**
- [ ] `databricks bundle validate` passes for `databricks/bundle/databricks.yml`.
- [ ] After job run: `SELECT count(*) FROM zava.gold.<fact>` > 0 and matches generator row expectations.
- [ ] `DESCRIBE EXTENDED zava.gold.<table>` shows the `certified` tag/comment.
- [ ] Grants present: `SHOW GRANTS ON SCHEMA zava.gold` includes `EXTERNAL USE SCHEMA`.
- [ ] Edge: re-running the bundle is idempotent (no duplicate rows; uses overwrite/merge).

**Manual steps:** Enabling external data access at the **metastore** level may require account-admin confirmation in the Databricks account console (documented).

#### Step 9: Lakeflow Spark Declarative Pipeline (Variation 2 source) + managed-storage path discovery — ✅ Done
**Status:** ✅ Done
**Files:** `databricks/pipelines/lakeflow_sdp.py`, `databricks/pipelines/lakeflow_sink_external.py`, `databricks/uc/05_access_policies.sql`, `databricks/bundle/databricks.yml` (add pipeline), `docs/runbook-end-to-end.md` (append discovery snippet)
**Depends on:** Step 8

**Tasks:**
- [ ] Author a Lakeflow SDP pipeline producing **streaming tables / materialized views** from silver (curated) data (R10).
- [ ] Document `DESCRIBE DETAIL` to discover the `abfss://...__unitystorage/...` managed-storage path for the shortcut target (sub-pattern **2A**) — flag the UC-bypass governance caveat (R10).
- [ ] Provide the **2B** alternative: a Lakeflow **sink** (`dp.create_sink(format="delta", ...)` + `append_flow`) writing to a dedicated external location for cleaner governance (R10).
- [ ] Add UC **row filters + column masks** (e.g., site managers see only their city; mask customer email) — these become Policy Weaver source policies (R5).

**Verification:**
- [ ] `databricks bundle validate` passes with the pipeline added.
- [ ] Pipeline run produces a streaming table / MV; `DESCRIBE DETAIL <table>` returns a resolvable `abfss://` location.
- [ ] 2B path (if enabled): external-location Delta files exist at the owned path.
- [ ] Row filter active: querying as a restricted principal returns only that city's rows; masked column shows masked value.

**Manual steps:** none beyond Databricks auth; note the **2A governance caveat** (managed-storage shortcut bypasses UC enforcement) is narrated, not a click.

### Phase 3 — Fabric Ingestion (both variations)

#### Step 10: Create/attach Fabric workspace + assign capacity + Workspace Identity — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/00_create_workspace.py`, `fabric/config/deploy_config.sample.json` (extend), `docs/manual-steps.md` (append)
**Depends on:** Step 5, Step 2 (tenant settings)

**Tasks:**
- [ ] Create (or attach to existing) a Fabric workspace and assign the F64 capacity via Fabric REST / `fab` (R7).
- [ ] Create the **Fabric Workspace Identity** (UI-assisted) and capture its object id into `workspace.identity_object_id` for Step 12 hardening application (R10).
- [ ] Idempotent: re-running finds the existing workspace by name and updates capacity assignment.

**Verification:**
- [ ] `fab` / REST `GET workspaces` lists the Zava workspace bound to the F64 capacity in East US 2.
- [ ] Workspace Identity object id captured into config (placeholder until created).
- [ ] Existing path: with provided workspace id, no duplicate workspace is created.

**Manual steps:** **Workspace Identity creation** (Workspace settings → Workspace identity) is UI-assisted (R10) — appended to appendix.

#### Step 11: Variation 1 — Mirrored Azure Databricks Catalog — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/10_create_mirrored_catalog.py`, `docs/manual-steps.md` (append)
**Depends on:** Step 8, Step 10

**Tasks:**
- [ ] Create the **Mirrored Azure Databricks Catalog** item via REST (preview API; **user-token** — SP not supported, R1/R7) over the `zava` gold schema; enable 15-min auto-sync. Implement retry/backoff + token-refresh for the long-running create + first-sync wait.
- [ ] Document the one-time **Databricks connection OAuth consent** (UI) for the organizational account (R1, R10).
- [ ] Verify SQL analytics endpoint + OneLake shortcuts appear for gold tables.

**Verification:**
- [ ] REST `GET` shows the mirrored catalog item in `Running`/synced state.
- [ ] T-SQL `SELECT TOP 10` against the SQL analytics endpoint returns gold rows.
- [ ] Schema change in UC (add a column) appears in Fabric within ~15 min (or via manual refresh).

**Manual steps:** **Databricks connection OAuth consent** is a one-time UI step (R1); mirrored-catalog create requires **user-token** (no SP — R7). Appended to appendix.

#### Step 12: Variation 2 — secured OneLake shortcut + APPLY ADLS network hardening — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/20_create_shortcut.py`, `infra/main.bicep` (apply hardening with real Workspace Identity), `docs/manual-steps.md` (append)
**Depends on:** Step 9, Step 10, Step 7

> **Ordering note:** This step **applies** the Step 7 hardening module now that the Step 10 Workspace Identity object id exists. All identity-bound firewall verification and the negative (non-trusted-denied) test live here, not in Step 7.

**Tasks:**
- [ ] Create a Fabric Lakehouse and a **OneLake ADLS Gen2 shortcut** to the discovered managed-storage path (2A) or external location (2B) via the OneLake Shortcuts REST API (R10).
- [ ] **Apply** the `network-hardening.bicep` module passing the real `workspace.identity_object_id`: enable trusted workspace access, add the resource instance rule for the Fabric Workspace Identity, set firewall default-deny, disable public network access (R10).
- [ ] Confirm Direct Lake readability of the shortcut Delta (R2).

**Verification:**
- [ ] `az deployment group what-if` (apply phase) now shows the **resource instance rule for the real Fabric Workspace Identity** + `defaultAction=Deny` (achievable because Step 10 produced the identity).
- [ ] Shortcut lists in the Lakehouse; preview returns rows from the streaming table/MV.
- [ ] After firewall lockdown, Fabric (trusted) still reads; a **non-trusted client is denied** (negative test).
- [ ] Direct Lake test query over the shortcut succeeds (deferred full model in Step 14).

**Manual steps:** **OneLake security role assignment** and any **connection OAuth consent** for the shortcut are UI-assisted (R10). Appended to appendix.

### Phase 4 — Direct Lake Reporting

#### Step 13: Thin Fabric gold / aggregation layer — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/40_build_thin_gold.py`
**Depends on:** Step 11, Step 12

**Tasks:**
- [ ] Build a thin Fabric aggregation layer (V-Order) for heavy KPI calculations, since mirroring doesn't apply V-Order (R2) — e.g., daily fleet-utilization, revenue/site rollups.
- [ ] Source from mirrored gold (V1) and/or shortcut (V2); write Delta to a Fabric Lakehouse.
- [ ] Idempotent overwrite/merge.

**Verification:**
- [ ] Aggregation tables exist in OneLake with V-Order enabled.
- [ ] Row counts reconcile with Databricks gold within tolerance.
- [ ] Re-run is idempotent.

**Manual steps:** none.

#### Step 14: Direct Lake semantic model (semantic-link-labs + TOM) — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/30_create_semantic_model.py`, `fabric/semantic-model/*`
**Depends on:** Step 13

**Tasks:**
- [ ] Create the **Direct Lake on OneLake** semantic model via `generate_direct_lake_semantic_model()` with `source_type='MirroredAzureDatabricksCatalog'` (and/or the Lakehouse for V2) (R2).
- [ ] Add measures (fleet utilization, revenue/site, idle vehicles, one-way flows, maintenance cost), relationships, and RLS roles via TOM (`connect_semantic_model`) (R2).
- [ ] Add "Prep for AI"/AI data-schema annotations for Data Agent accuracy (R3).
- [ ] Serialize model as TMDL into `fabric/semantic-model/` for Git/code deployment.

**Verification:**
- [ ] Semantic model lists in the workspace as Direct Lake on OneLake (no DirectQuery fallback).
- [ ] A DAX query for a key measure (e.g., monthly fleet utilization for Seattle) returns expected value.
- [ ] RLS role test: a constrained principal sees only allowed rows.
- [ ] Edge: cold-cache transcoding query completes within Direct Lake guardrails on F64.

**Manual steps:** none (programmatic — R2).

#### Step 15: Power BI report (PBIP/PBIR) — blue Zava theme, wow visuals — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/report/*`, `fabric/theme/zava-blue-theme.json`, `fabric/scripts/50_deploy_report.py`
**Depends on:** Step 14

**Tasks:**
- [ ] Author the report in Power BI Desktop as PBIP/PBIR: **multi-city maps**, **decomposition trees**, **forecasting**, KPI cards; apply blue Zava theme (R2).
- [ ] Commit PBIR project; deploy via `fabric-cicd`/Git integration to the workspace (R2, R7).
- [ ] Show the "certified Databricks asset → governed Power BI insight" story on a landing page.

**Verification:**
- [ ] Report renders in the workspace bound to the Direct Lake model.
- [ ] Map visual plots multi-city sites; decomposition tree drills revenue by site/class; forecast line renders.
- [ ] `fabric-cicd` redeploy is idempotent (updates in place).

**Manual steps:** **Advanced visuals are authored in Power BI Desktop** (PBIR then deployed as code) — documented as a tooling step, not a tenant click (R2).

### Phase 5 — Fabric IQ (Ontology, Graph, Data Agent, Real-Time, Activator alerting + optional Operations Agent)

#### Step 16: Ontology (preview) + auto Graph (GA) — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/60_create_ontology.py`, `fabric/ontology/ontology_definition.json`, `docs/manual-steps.md` (append)
**Depends on:** Step 14

**Tasks:**
- [ ] Primary path: **generate Ontology from the semantic model** (UI-only generation — no REST trigger, R4) → auto-creates the Graph (GA).
- [ ] Code-first fallback: create the Ontology item via REST with a **definition JSON** (graph-source path is fully scriptable — R4) so the demo degrades gracefully if UI-gen wobbles.
- [ ] Define Zava business entities/relationships (Site, Vehicle, Customer, Rental, Reservation…) in the definition.

**Verification:**
- [ ] Ontology item + Graph item exist in the workspace.
- [ ] Graph shows entity nodes/edges for Zava (e.g., Customer→Rental→Vehicle→Site).
- [ ] Fallback test: REST-defined ontology (graph-source) creates without the UI-gen step.

**Manual steps:** **"Generate ontology from semantic model" is UI-only** (R4). Documented with click-path; REST graph-source fallback noted. Appended to appendix.

#### Step 17: Fabric Data Agent (GA) — NL insights via REST — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/70_create_data_agent.py`, `fabric/data-agent/*`, `docs/manual-steps.md` (append)
**Depends on:** Step 14, Step 16

**Tasks:**
- [ ] **Primary path — Fabric Data Agent REST API (per R3):** create the agent with `POST /v1/workspaces/{workspaceId}/dataAgents`, then publish the full definition via `POST .../dataAgents/{id}/updateDefinition` (base64 definition files; `$schema` dataAgent 2.1.0) — full CRUD: create/get/list/update/delete/get-definition/update-definition (R3 §4.1, endpoints lines 265–271). Ground on the semantic model (+ optionally lakehouse/ontology/graph via the definition `type` enum: `semantic_model`, `lakehouse`, `graph`, `mirrored_azure_databricks`, etc. — R3 line 133). Retrieve real `DataSourceElement.id`s via the Fabric REST API before assembling the definition (R3 line 358).
- [ ] **Secondary path — `fabric-cicd`/Git** for ALM/repeatable deployment of the same item definition (R3 supports Git integration + deployment pipelines; R7 ALM nuance). Use REST as the source-of-truth create; Git for promotion across environments.
- [ ] Add custom instructions + example NL→query pairs (R3).
- [ ] Document the **ontology-as-data-source attach** UI step if the REST enum doesn't yet expose ontology in your tenant (graph-source fallback otherwise — R4).
- [ ] Provide sample questions: "average vehicle utilization across Seattle this month", "which sites have the most idle vehicles", "top one-way flows".

**Verification:**
- [ ] `POST .../dataAgents` returns 201/accepted; `GET .../dataAgents/{id}` shows the published agent; `getDefinition` returns the expected sources.
- [ ] Data Agent answers a sample NL question with a correct, grounded numeric answer (matches the semantic-model measure).
- [ ] RLS honored: constrained identity gets constrained answers (R3).
- [ ] Redeploy (REST update-definition and/or `fabric-cicd`) is idempotent.

**Manual steps:** **Ontology data-source attach** to the Data Agent may be a UI step in some tenants (R4). Core create/update is via **Data Agent REST API** (R3); `fabric-cicd`/Git is the secondary ALM path. Appended to appendix only if the ontology attach is needed.

#### Step 18: Real-Time Intelligence — Eventhouse + KQL DB/table + Eventstream (Activator + Operations Agent source) — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/75_create_eventhouse.py`, `fabric/realtime/eventhouse_setup.kql`, `fabric/realtime/eventstream_definition.json`, `docs/manual-steps.md` (append)
**Depends on:** Step 10
**Provisioning flag:** runs only when `features.enable_eventhouse=true` (required when `enable_activator_email=true` or `enable_operations_agent=true`).

> **Decision (resolves former Open Question 3):** The watch+act layer is grounded on a **`KustoDatabase` (Eventhouse/KQL) source**, not ontology basic-properties. Rationale: the Zava "watch + act" scenario (idle-vehicle spike / maintenance-fault spike) is inherently **time-series**, and R11 (§2) states ontology monitoring supports **basic property values only** while time-series properties must bind to Eventhouse fields. A real-time Eventhouse path is therefore the correct and more impressive demo. The **same Eventhouse source feeds both** the default Activator email rule (Step 19) and the optional Operations Agent (Step 20). (Ontology grounding remains available for the Data Agent in Step 17, and Activator can also bind rules to Ontology business entities — R11 §6c.)

**Tasks:**
- [ ] Create an **Eventhouse** + **KQL database** (`realtime.kql_database_name`) and a **`Telematics` table** (`realtime.kql_table_name`) via Fabric REST / `fab`, using `eventhouse_setup.kql` for table DDL + ingestion mapping (columns: vehicle_id, site_id, ts, ignition_state, idle_minutes, odometer, fault_code).
- [ ] Create an **Eventstream** item (`eventstream_definition.json`) that ingests the synthetic telematics feed from `data/generate_telematics_stream.py` into the KQL table (custom-app/source → Eventhouse destination). Document the source connection (a Custom App/Event Hub endpoint or sample feed) and the one-time connection setup.
- [ ] Verify ingestion-time-based new-record detection works (R11 §2): confirm `ingestion_time()` advances as events arrive.
- [ ] Add a runbook snippet to drive the injectable spike window (from Step 3) for live demo.

**Verification:**
- [ ] REST `GET` lists the Eventhouse, KQL database, and Eventstream items in the workspace.
- [ ] After running the feed, KQL `Telematics | count` > 0 and `Telematics | summarize max(ingestion_time())` advances across two runs.
- [ ] Spike window query: `Telematics | where ts between (spike_start .. spike_end) | summarize avg(idle_minutes)` shows the elevated values (confirms a monitorable time-series signal for Steps 19–20).
- [ ] Idempotent: re-running create finds existing Eventhouse/KQL DB/Eventstream and does not duplicate.

**Manual steps:** **Eventstream source connection / Custom App credential** wiring may be UI-assisted (connection creation). Appended to appendix.

#### Step 19: Fabric Activator (Reflex) native Email alert — DEFAULT, Teams-free watch + act — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/78_create_activator_email.py`, `fabric/activator/reflex_entities.json`, `docs/manual-steps.md` (append)
**Depends on:** Step 18 (Eventhouse/KQL source). **Optional/conditional:** Step 16 (Ontology business entities) only when `enable_ontology=true` — used for richer ontology-entity binding. The default email rule binds to the Step 18 Eventhouse property and does **NOT** require Ontology (this preserves the GA-only fallback: Activator email keeps working even if the only preview item, Ontology, is unavailable).
**Provisioning flag:** runs only when `features.enable_activator_email=true` (default `true`; requires `enable_eventhouse=true`).

> **Decision (R11 §6):** This is the **default, Teams-free** watch+act path. The customer may not have Microsoft Teams, and the Operations Agent (even at GA) **requires Teams** for both its default notification and its Yes/No approval card, with **no native email channel**. Fabric **Activator** (a "Reflex" item, GA) has a **first-class Email action** with no Teams dependency, is **deployable as code** (Reflex REST API + `ReflexEntities.json` with an `EmailMessage` action), and can monitor the **same** Eventhouse properties **and** Fabric Ontology business entities. The Operations Agent layers on top as an optional enhancement (Step 20).

**Tasks:**
- [ ] Author the Activator/Reflex definition `fabric/activator/reflex_entities.json` whose rule action is **`EmailMessage`** (verbatim config props: `messageLocale`, `sentTo`, `copyTo`, `bCCTo`, `subject`, `headline`, `optionalMessage`, `additionalInformation` — R11 §6c). Recipient = `alerting.site_manager_email` (placeholder; no real address committed). Bind the rule to the Step 18 Eventhouse property (e.g., `idle_minutes`/`fault_code` threshold) and/or, when `enable_ontology=true`, to the Step 16 Ontology business entity (idle-vehicle / overdue-maintenance).
- [ ] Implement `78_create_activator_email.py`: create the Reflex item and deploy the definition **as code** via `POST /v1/workspaces/{id}/reflexes` with `definition.format="json"` and a single `ReflexEntities.json` part (InlineBase64) + `.platform` part (R11 §6c `deploy_email_reflex` pattern). Add retry/backoff; idempotent (find-existing-by-name, then update definition).
- [ ] Zava scenario: **email the site manager** when an idle-vehicle threshold trips (e.g., `idle_minutes > threshold`) or an overdue-maintenance / fault-spike condition fires (each rule a single condition — keep deterministic).
- [ ] **Optional Fabric-item action:** add an Activator `FabricItemInvocation` action targeting a Zava `DataPipeline`/`Notebook` that writes a **work-order row** (passing `SiteId`/`VehicleId` parameters) — Teams-free (R11 §6c/§9).
- [ ] Document the one UI-assisted nuance: authoring/validating the rule in Activator **design mode** (Monitor → Condition → Email Action) — but the item + definition deploy as code; verify the `ReflexEntities.json` rule body against the live Reflex schema before relying on the scripted path.

**Verification:**
- [ ] REST `GET /v1/workspaces/{id}/reflexes` lists the Zava Activator (Reflex) item; `getDefinition` returns the `ReflexEntities.json` part with the `EmailMessage` action.
- [ ] Driving the injectable spike window (Step 3/Step 18) sends an **email to the site manager WITHOUT any Microsoft Teams involvement** (confirm in the recipient mailbox / connector log).
- [ ] Optional Fabric-item action: a work-order row is written by the targeted pipeline/notebook when the rule fires.
- [ ] Idempotent: re-running create finds the existing Reflex and updates the definition (no duplicate item).

**Manual steps:** Authoring/validating the Activator rule in **design mode** is UI-assisted, though the Reflex item + `EmailMessage` definition deploy as code (R11 §6c). No Teams required. Appended to appendix.

#### Step 20: Operations Agent (GA) — OPTIONAL Teams enhancement (LLM-reasoned watch + act) — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/80_create_operations_agent.py`, `fabric/operations-agent/Configurations.json`, `docs/manual-steps.md` (append)
**Depends on:** Step 18 (Eventhouse/KQL source). **Optional/conditional:** Step 16 (Ontology) only when `enable_ontology=true` (richer enrichment). (Step 20 itself is optional — runs only when `enable_operations_agent=true`.)
**Provisioning flag:** runs only when `features.enable_operations_agent=true` (region ∈ {eastus2, westus} **and** a Microsoft Teams account is available). Default `false`.

> **Decision (R11 §6, §8):** The Operations Agent reached **GA at Microsoft Build 2026** (Azure blog, 2026-06-02). It is an **optional enhancement** layered on top of the default Activator email (Step 19) for tenants that **have Microsoft Teams**: it adds LLM-reasoned, concept-aware recommendations with a **Teams Yes/No approval** card. It still **requires Teams** (default notification + approval card) and **user-token** create (no SP/MI). Tenant portal/Learn pages may still render "(preview)" labels pending doc refresh — the feature is GA per the Azure blog.

**Tasks:**
- [ ] Author the `OperationsAgentV1` / `OperationsAgentDefinition` (`Configurations.json`) with instructions, a **`KustoDatabase` data source** bound to the Step 18 KQL `Telematics` table (the supported time-series path — R11), a **`FabricJobAction`** (run a rebalancing/maintenance pipeline) and/or other custom action, a **`messageDestination`** (`Recipient` or `TeamsChannel`; note the GA schema deprecated `goals`/`recipient` → `messageDestination`), and `shouldRun` run-state (R11 §4b/§8).
- [ ] Create via the **OperationsAgent REST API** (`POST /v1/workspaces/{id}/operationsAgents`; **user-token only** — no SP/MI, R11). Add retry/backoff + token-refresh. Use a Fabric variable-library reference for portable IDs across dev/test/prod (R11 §9).
- [ ] Zava scenario: monitor **idle-vehicle threshold** or **maintenance-fault spike** (each rule a single condition — `AND` unsupported, R11 §8) → recommend a rebalancing/maintenance action with a human-in-the-loop **Teams Yes/No** card.
- [ ] Document **Teams app install** (Fabric Operations Agent Teams app) + the live **Yes/No approval** as UI-assisted, and the tenant Copilot/Azure OpenAI enablement (R11 §8/§9).

**Verification:**
- [ ] REST `GET .../operationsAgents` shows the Operations Agent with `shouldRun: true` and a `KustoDatabase` source.
- [ ] Driving the injectable spike window (Step 3/Step 18) produces a **Teams** recommendation card (manual confirm; this path requires Teams — contrast with the Teams-free Step 19 email).
- [ ] `shouldRun: false` via Update Definition stops evaluation (run-state automatable — R11).

**Manual steps:** **Teams app install** + live **Yes/No approval** are UI-assisted; **requires Microsoft Teams**; create requires **user-token** (R11 §6/§8). This entire step is **skipped** when the tenant has no Teams (`enable_operations_agent=false`). Appended to appendix.

### Phase 6 — Governance

#### Step 21: Policy Weaver — UC access → OneLake security — ✅ Done
**Status:** ✅ Done
**Files:** `scripts/governance/policy-weaver/policy_weaver_config.yaml`, `scripts/governance/policy-weaver/run_policy_weaver.py`, `docs/manual-steps.md` (append)
**Depends on:** Step 9 (UC policies), Step 11, Step 12 (OneLake targets)

**Tasks:**
- [ ] Configure Policy Weaver v0.4.0 (`pip install policy-weaver`) for the single `zava` source catalog (one catalog per run — R5).
- [ ] Map UC grants/row-filters/column-masks → OneLake Security roles (identity resolution via Microsoft Graph; write via Fabric `dataAccessRoles` — R5).
- [ ] Document Beta status and "accelerator, as-is" caveat (R5).

**Verification:**
- [ ] Run creates OneLake Security roles mirroring the UC row filter (e.g., Seattle-only) and column mask.
- [ ] A constrained principal querying via OneLake/Direct Lake sees only permitted rows/masked columns.
- [ ] Re-run is idempotent (updates roles, no duplicates).

**Manual steps:** **OneLake Security role review/assignment** and identity consent are UI-assisted (R5/R1). Appended to appendix.

**Implementation Notes:**
- 2026-06-08 (Round 2 rework): Addressed reviewer 🟡 Important finding — `run_policy_weaver.py` now enforces the pinned Policy Weaver `0.4.0` version. Added `installed_version()` (stdlib `importlib.metadata.version("policy-weaver")`) and rewrote `ensure_installed()` to fail fast with an actionable message (`pip install policy-weaver==0.4.0`) when the installed distribution version is missing/undeterminable or != `0.4.0`; `--install` installs EXACTLY `policy-weaver==0.4.0`. Dry-run, env/Key Vault SP auth, no-secrets posture, and logging unchanged. Verified via `ast.parse`, a mismatch/correct/not-installed simulation, and a `--dry-run` smoke run. Local implementation and verification complete; awaiting reviewer verdict.

#### Step 22: Microsoft Purview — catalog, lineage, labels, DLP — ✅ Done
**Status:** ✅ Done
**Files:** `scripts/governance/purview/setup_purview_scans.py`, `scripts/governance/purview/lineage_runbook.md`, `docs/manual-steps.md` (append)
**Depends on:** Step 8, Step 11, Step 14, Step 15

**Tasks:**
- [ ] Register + scan the **Databricks UC** source (managed identity/SP/PAT) and the **Fabric tenant** (live view + tenant scan) (R6).
- [ ] Apply **sensitivity labels** + a **DLP** policy on synthetic PII columns; show downstream inheritance (R6).
- [ ] Create a governance domain + data product for the certified Zava gold asset; attach glossary terms (R6).
- [ ] **Lineage seam runbook:** explicitly document that Databricks→Fabric lineage does **NOT** auto-stitch — narrate the seam and how to verify each side (R6).

**Verification:**
- [ ] Databricks UC assets + Fabric items appear in the Unified Catalog.
- [ ] Sensitivity label visible on the labeled asset; DLP policy evaluated.
- [ ] Lineage shows UC-internal lineage and Fabric-internal lineage; runbook documents the non-stitched seam (R6).

**Manual steps:** **Governance domains/data products** are largely UI/PowerShell with partial REST; **Fabric live-view toggle** and **tenant admin settings** are UI-only (R6). Appended to appendix.

### Phase 7 — Orchestration, Docs, Validation

#### Step 23: End-to-end orchestrator, preflight & teardown — ✅ Done
**Status:** ✅ Done
**Files:** `scripts/deploy.py`, `scripts/preflight_checks.py`, `scripts/teardown.py`
**Depends on:** Steps 5–22

**Tasks:**
- [ ] `preflight_checks.py`: validate config via `config_schema.py` (the **authoritative region contract** — curated allow-list `eastus2` primary / `westus` backup, §1.7); verify tenant settings, SP access, capacity SKU **and live capacity state (Active/Paused/Resumed)**, tool versions; **region check** — surface config_schema's region verdict and explain *why* East US / South Central US are excluded (the optional Operations Agent (GA) is unavailable there; the Teams-free Activator email path is itself region-agnostic, but the full demo is validated only for `eastus2`/`westus`, so the allow-list is enforced unconditionally — no unreachable "would-pass" branch); when `enable_operations_agent=true`, **additionally** verify a Microsoft Teams account is available. The genuinely-conditional gate is the Teams check (R7, R9, R11).
- [ ] `deploy.py`: idempotent wave-based orchestration honoring fresh-vs-existing flags; pause/skip points at user-token/manual steps with clear prompts; retry/backoff wrappers for long-running Fabric REST + 15-min mirror waits.
- [ ] `teardown.py`: safe, ordered teardown (rollback for destructive ops; capacity pause/delete last) — confirm-before-destroy.

**Verification:**
- [ ] `python scripts/preflight_checks.py` passes against a configured tenant (or clearly reports gaps), including the **unconditional** region allow-list check (`eastus2`/`westus`), the **live capacity-state probe**, and the **conditional Teams check** (enforced only when `enable_operations_agent=true`).
- [ ] `deploy.py --dry-run` prints the full ordered plan with manual-step pauses.
- [ ] `teardown.py --dry-run` lists resources to remove in safe order; requires explicit `--yes` to execute.

**Manual steps:** Orchestrator **pauses** at each documented user-token/UI step (mirroring OAuth, Workspace Identity, ontology gen, Eventstream connection, Activator rule design-mode validation, and the optional Operations Agent Teams wiring).

#### Step 24: Component READMEs + architecture/cost docs — ✅ Done
**Status:** ✅ Done
**Files:** `infra/README.md`, `databricks/README.md`, `fabric/README.md`, `data/README.md`, `docs/architecture.md`, `docs/runbook-end-to-end.md`
**Depends on:** Steps 1–22

**Tasks:**
- [ ] One clear README per major component (purpose, how to run, parameters, manual steps).
- [ ] `docs/architecture.md` with the diagram + value framing + **East US 2 region rationale (§1.7)**; `docs/runbook-end-to-end.md` as the full demo script.

**Verification:**
- [ ] Each component README documents its run command + parameters + manual steps.
- [ ] Architecture doc matches the implemented file structure (no drift) and states the region decision + West US backup.

**Manual steps:** none.

#### Step 25: Top-level README update — ✅ Done
**Status:** ✅ Done
**Files:** `README.md`
**Depends on:** Step 24

**Tasks:**
- [ ] Replace the skeleton with: scenario, four pillars, two ingestion variations, business value, prerequisites, quick-start (fresh vs existing), repo layout, links to component READMEs + Manual Steps Appendix + cost doc.
- [ ] Keep the "synthetic data / fictional company" disclaimer.

**Verification:**
- [ ] README links resolve to existing files; quick-start matches `scripts/deploy.py` flags.
- [ ] No secrets/placeholders leaking real values.

**Manual steps:** none.

#### Step 26: Consolidated Manual Steps Appendix — ✅ Done
**Status:** ✅ Done
**Files:** `docs/manual-steps.md`
**Depends on:** Steps 2, 7, 10, 11, 12, 16, 17, 18, 19, 20, 21, 22

**Tasks:**
- [ ] Consolidate every UI-only action into one ordered appendix with exact click-paths and the step it belongs to (see §8 for the authoritative list). Note the **default Activator email path is code-deployable** (minimal manual), while the **optional Operations Agent Teams/Activator wiring** is the manual, Teams-requiring path.

**Verification:**
- [ ] Every manual step referenced by Steps 2–22 appears in the appendix exactly once.
- [ ] Each entry has a precise click-path and the reason automation isn't possible (with R-citation).

**Manual steps:** N/A (this is the catalog of them).

#### Step 27: END-TO-END VALIDATION — ✅ Done
**Status:** ✅ Done
**Files:** `docs/runbook-end-to-end.md` (validation checklist section)
**Depends on:** Steps 1–26

**Tasks:**
- [ ] Execute the full path on a real (or trial-where-allowed) capacity in **East US 2**: data → Databricks medallion + Lakeflow pipeline → V1 mirror + V2 shortcut → thin gold → semantic model → report → ontology/graph → Data Agent → Eventhouse/Eventstream → **Activator email alert (default)** → **Operations Agent (optional, if Teams available)** → Policy Weaver → Purview.
- [ ] Validate the GA-only fallback path (semantic model + report + Data Agent + **Activator email alerting** — all GA) works if **Ontology (the only preview item)** is unavailable.
- [ ] Capture timings and a screenshot-based demo script.

**Verification:**
- [ ] **Happy path:** a business question answered identically across Power BI measure ↔ Data Agent ↔ ontology query.
- [ ] **Real-time watch+act (default, Teams-free):** the injected telematics spike triggers an **Activator email** to the site manager with **no Teams involved**.
- [ ] **Real-time watch+act (optional, Teams):** if `enable_operations_agent=true`, the same spike produces an Operations Agent **Teams** recommendation card.
- [ ] **Governance:** a constrained user sees consistent row/column restrictions in Direct Lake report, Data Agent, and OneLake — proving Policy Weaver sync.
- [ ] **Both variations:** V1 (mirrored gold) and V2 (shortcut streaming/MV) both surface in the report.
- [ ] **Resilience:** with Ontology (preview) off, the GA-only fallback still delivers report + Data Agent + Activator email alerting.
- [ ] **Cost hygiene:** capacity paused at the end via `scripts/pause_capacity.py`.

**Manual steps:** Confirm the default **Activator email** arrived without Teams; if testing the optional path, confirm the Operations Agent **Teams** card and ontology generation produced expected output.

---

### Phase 9 — Remediation (Phase 4 E2E codebase review)

> Added 2026-06-08 after the Phase 4 `codebase-reviewer` audit (`agent-reviews/2026-06-08-codebase-review.md`) found cross-cutting integration seams that per-step reviews could not see. Two **serialized** remediation steps (both touch `scripts/deploy.py`, so they cannot run in parallel; Step 28 establishes the deploy/config contract, Step 29 conforms downstream artifacts to it).

#### Step 28: Wiring & config coherence remediation — ✅ Done
**Status:** ✅ Done
**Files:** `scripts/deploy.py`, `scripts/config_schema.py`, `fabric/config/deploy_config.sample.json`, `infra/main.bicep`, `infra/modules/network-hardening.bicep`, `fabric/scripts/00_create_workspace.py`, `databricks/bundle/databricks.yml`, `databricks/uc/05_access_policies.sql`, `docs/runbook-end-to-end.md`, `docs/manual-steps.md`, `scripts/test_preflight_checks.py`
**Depends on:** Steps 1–27

**Tasks:**
- [ ] **🔴 Hardening wired end-to-end:** `00_create_workspace.py` (Step 10) persists the **workspace GUID** and **Workspace Identity object ID** into the effective config; `deploy.py`'s hardening wave (`_bicep_command(apply_hardening=True)`) passes `fabricWorkspaceId` (+ `fabricWorkspaceResourceId` / tenant as required) and `workspaceIdentityObjectId` to Bicep so `network-hardening.bicep` actually adds the trusted-workspace rule + `defaultAction=Deny` + Workspace-Identity RBAC. **Fail fast** if either ID is missing when the V2 shortcut/hardening path is enabled.
- [ ] **🔴 Catalog default coherence:** align the Databricks catalog across `deploy.py` (`--databricks-target` default), `databricks.yml` (`dev`/`prod` `catalog` var), `fabric/config/deploy_config.sample.json` (`source.databricks_catalog`), docs, and governance so a vanilla end-to-end deploy uses **one** catalog name everywhere (prefer threading the resolved DAB-target catalog into the effective Fabric config as the single source of truth).
- [ ] **🟡 UC access policies automated:** add a DAB SQL job/task (e.g. `zava_access_policies`) that runs `databricks/uc/05_access_policies.sql` (params: `catalog`, `curated_schema`, `gold_schema`) **after** Lakeflow and **before** Policy Weaver; have `deploy.py` sequence/pause for it so Policy Weaver always syncs real UC row-filters/column-masks.
- [ ] **🟡 Config contract completeness:** extend `config_schema.py` + `deploy_config.sample.json` to cover every section the scripts read (`mirroring`, `shortcut`, `lakehouse`, `semantic_model`, `report`, `ontology`, `data_agent`, `operations_agent`), marking required/conditional fields + placeholders — at minimum `mirroring.databricks_connection_id`, `shortcut.connection_id`, and `shortcut.abfss_path`/`adls_location`+`adls_subpath`.
- [ ] **🟡 Existing-Databricks hardening:** either support hardening an existing ADLS account via explicit storage-account/RG params (remove the blanket `&& !useExistingDatabricks` skip), or clearly document that secured V2 hardening is fresh-path-only and provide a BYO manual/IaC procedure.
- [ ] **🟢 Docs:** update Python version mention to ≥3.11 (`docs/runbook-end-to-end.md`) to match the §7 contract; correct the `docs/manual-steps.md` hardening note to match how `deploy.py` now passes `workspace.identity_object_id`.

**Verification:**
- [ ] `python scripts/config_schema.py` self-tests pass; `python scripts/test_preflight_checks.py` passes with **new** tests: (a) selected DAB-target catalog == `deploy_config.source.databricks_catalog`; (b) every script-read config key is declared in schema/sample; (c) the hardening Bicep command includes workspace GUID + identity object ID; (d) `05_access_policies.sql` is reachable from the deploy plan.
- [ ] `az bicep build --file infra/main.bicep --stdout` still compiles (delete the `infra/main.json` artifact after).
- [ ] `python scripts/deploy.py --dry-run` prints a coherent plan with the access-policy step before governance and the hardening wave passing the IDs.

**Manual steps:** none (beyond those already in `docs/manual-steps.md`).

#### Step 29: V2 downstream visibility & thin-gold execution — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/scripts/40_build_thin_gold.py`, `fabric/scripts/30_create_semantic_model.py` (add the new tables to the generated model's table list), `fabric/semantic-model/definition/model.tmdl`, `fabric/report/` (forecast + a V2-derived visual; `page.json` if adding a visual), `scripts/deploy.py` (**Step-13 thin-gold wave invocation only** — deploy + run the notebook item by ID/name with params; Step 28 is committed so serialized editing is safe)
**Depends on:** Step 28

**Tasks:**
- [x] **🟡 Surface Variation 2 downstream:** add at least one V2-derived thin-gold table (from the Lakeflow/shortcut `rentals_curated` / `telematics_curated` curated outputs — e.g. a telematics-freshness or curated-rentals KPI), model it in the semantic model, and add a report visual so the demo **demonstrably** shows V2 shortcut data (not just V1 gold) in the report.
- [x] **🟡 Thin-gold executable as a Fabric item:** make Step 13 runnable — deploy `40_build_thin_gold.py` as a Fabric **Notebook item** and run it by item ID/name with explicit parameters (lakehouse, catalog/schema from Step 28's config), conforming to the `deploy.py` Step-13 invocation contract established in Step 28. (Coordinate with Step 28 on how `deploy.py` invokes it — Step 28 owns the `deploy.py` edit; this step makes the notebook deployable/parameterized to match.)
- [x] **🟢 Forecast visual backing:** add `agg_revenue_by_site_month` to the semantic model (TMDL) and bind the revenue-forecast visual to a true monthly revenue series (fix the current mismatched axis/value).

**Verification:**
- [x] Semantic model TMDL includes the V2-derived table(s) + `agg_revenue_by_site_month`; report references them (no orphan aggregates).
- [x] JSON/TMDL artifacts parse; report visual bindings resolve to model fields.
- [x] `python scripts/deploy.py --dry-run` Step-13 wave references a deployable Fabric notebook item (not a bare local path) with parameters.

**Manual steps:** none.

**Implementation Notes:** (2026-06-08) Local implementation and verification complete; awaiting reviewer verdict.
- Added V2-derived thin-gold aggregate `agg_telematics_freshness` in `40_build_thin_gold.py`, sourced from the Lakeflow `telematics_curated` **streaming table** (UC-managed, not mirrorable) via the Step-12 OneLake shortcut (new `read_curated()` helper + `source_curated_schema`/`src_*_curated` params). Modeled it (table + 3 measures, incl. `Telematics Vehicles Tracked`) in `30_create_semantic_model.py` and inline in `model.tmdl`; added a "V2 · Lakeflow Shortcut" card visual on `executive-overview` bound to `Telematics Vehicles Tracked`.
- Step-13 thin_gold wave now deploys+runs a Fabric **Notebook item**: `40_build_thin_gold.py` gained a `--deploy-and-run`/`--dry-run` driver (import-safe; only diverts on driver flags, no-op inside a Fabric notebook run) that emits `fab import` (create/update item) + `fab job run-sync <item> -P <params>`. `deploy.py`'s thin_gold wave changed to `KIND_FABRIC` invoking that driver with `--config` so dry-run prints the item path + params. Notebook parameter cell now also accepts `lakehouse`/`catalog`/`source_curated_schema`.
- `agg_revenue_by_site_month` added to the model (script + inline TMDL) and the revenue-forecast visual rebound to axis `revenue_month` + value `Monthly Revenue` (true monthly series).
- **Scope-compliance note:** the new model tables are defined **inline** in `model.tmdl` (the only semantic-model file in Step-29 scope) rather than as new `tables/*.tmdl` files; no `relationships.tmdl` edit (out of scope), so the two new aggregates are self-contained for their single-table visuals (no orphan report bindings). If the reviewer prefers externalized table files / explicit relationships, that needs a file-scope amendment.
- Verification run: `ast.parse` of the 3 Python files → OK; `json.load` of all `fabric/report/**/*.json` → OK; `deploy.py --dry-run` → exit 0, thin_gold wave prints `fab import` + `fab job run-sync /…/40_build_thin_gold.Notebook -P lakehouse=…,catalog=…,source_schema=…,target_schema=…,source_curated_schema=`; semantic-model dry-run now spans **11 tables** incl. `agg_revenue_by_site_month` + `agg_telematics_freshness`. No hardcoded secrets.

#### Step 30: V2 shortcut ↔ notebook coherence — ✅ Done
**Status:** ✅ Done
**Files:** `fabric/config/deploy_config.sample.json`, `fabric/scripts/20_create_shortcut.py`, `fabric/scripts/40_build_thin_gold.py`, `docs/runbook-end-to-end.md`, `docs/manual-steps.md`, `databricks/README.md`
**Depends on:** Steps 28, 29

**Tasks:**
- [x] **🟡 Align the V2 shortcut with the consumed table.** Step 29 made `40_build_thin_gold.py` read `telematics_curated` (the only V2 table actually consumed; `rentals_curated` is defined but unused), but the Step-12 shortcut config still defaults to `name="rentals_curated"` + `path="Files/curated"`. Make the default V2 shortcut surface **`telematics_curated`** as a **`Tables/`-area** shortcut so `spark.table("telematics_curated")` resolves: set `deploy_config.sample.json` `shortcut.name="telematics_curated"`, `shortcut.path="Tables"` (the script's own `DEFAULT_SHORTCUT_PARENT_PATH`), and update the `adls_subpath`/`abfss_path` placeholder + `_comment` to point at the `telematics_curated` managed-table location. Ensure `20_create_shortcut.py` has no `rentals_curated` hardcoding in defaults/help/docstrings that contradicts this.
- [x] **Resolve the orphan ref.** In `40_build_thin_gold.py`, remove or clearly mark `src_rentals_curated` as an optional/alternative V2 source (it is not consumed) so there is no dangling expectation; the consumed V2 signal is `telematics_curated → agg_telematics_freshness`.
- [x] **Docs coherence.** Update the runbook **PATH DISCOVERY** procedure (and any `docs/manual-steps.md` shortcut step) to discover/create the `telematics_curated` shortcut (Tables area), matching the notebook read + model/report binding. Keep the security/network caveats.
- [x] **🟢 Stale catalog doc.** `databricks/README.md` still says the `dev` target uses catalog `zava_dev`; update it to `zava` to match the Step-28 one-catalog contract (`databricks/bundle/databricks.yml`).

**Verification:**
- [x] `python scripts/deploy.py --dry-run` previews a V2 shortcut whose item/table name (`telematics_curated`, Tables area) exactly matches what `40_build_thin_gold.py` reads and the model/report bind to.
- [x] `python scripts/config_schema.py` self-test + `python scripts/test_preflight_checks.py` still pass; `deploy_config.sample.json` parses.
- [x] No doc references a `zava_dev` default catalog or a `rentals_curated`-only V2 path that contradicts the notebook.

**Manual steps:** the shortcut creation remains a documented manual/secured step (Step 12) — only the **target table name + placement** change to `telematics_curated` / `Tables`.

**Implementation Notes:** (2026-06-09) Local implementation and verification complete; awaiting reviewer verdict. Aligned the single V2 shortcut to `telematics_curated`/`Tables` in `deploy_config.sample.json` (name/path + telematics-specific placeholders & `_comment`); updated `20_create_shortcut.py` example abfss/`--shortcut-name` + name-derivation comment to `telematics_curated`. Marked `src_rentals_curated` in `40_build_thin_gold.py` as an explicit **optional/alternative, not-consumed** V2 source (paired with the runbook's 2B external-location alternative). Rewrote the runbook PATH DISCOVERY procedure to `DESCRIBE DETAIL zava.curated.telematics_curated` + create a Tables-area `telematics_curated` shortcut (all governance/network caveats preserved). Fixed `databricks/README.md` `dev` catalog `zava_dev → zava`. `manual-steps.md` §3.3 shortcut rows are table-name-agnostic (no `rentals_curated`/`Files/curated` reference) — no edit needed. Verification: JSON+AST parse OK; config_schema self-test ALL PASSED; preflight 32 tests / exit 0; dry-run prints `shortcuts/Tables/telematics_curated` and POST body `"name": "telematics_curated"` / `"path": "Tables"`; greps confirm no `zava_dev` and no `Files/curated`/`rentals_curated`-shortcut contradictions remain.

#### Step 31: Reproducible Python environment (clone-and-run) — ✅ Done
**Status:** ✅ Done
**Files:** `requirements.txt`, `scripts/setup_env.ps1`, `scripts/setup_env.sh`, `.gitignore`, `README.md`, `docs/prerequisites.md`
**Depends on:** Steps 1–30

**Context:** Plan §7 planned a pinned `requirements.txt`/`uv.lock` "during Step 1" that was never created — dependencies are only documented prose in READMEs. Preflight (2026-06-09) confirmed the local gap: `fab`, Databricks CLI, and `semantic-link-labs` not installed, and the host default Python is **3.14** while the contract is **≥3.11, <3.13**. This step makes environment setup a single repeatable command for anyone who clones the repo.

**Tasks:**
- [ ] **`requirements.txt`** (repo root) — the complete **pip** dependency set, pinned per §7: `semantic-link-labs>=0.8.0`, `ms-fabric-cli>=1.1.0` (provides `fab`), `fabric-cicd>=0.1.14`, `policy-weaver==0.4.0` (exact), `requests>=2.31.0`, `azure-identity` (pinned min), `azure-keyvault-secrets` (used by config/secret helpers), `PyYAML>=6.0` (Policy Weaver config), plus the data-generator libs (`pandas>=2.0`, `numpy>=1.24`, `pyarrow>=12.0`) — reference/supersede `data/requirements.txt` without contradiction. Header comment must note the **<3.13** Python constraint and that **Databricks CLI**, **Azure CLI**, **Bicep**, and **Power BI Desktop** are **NOT pip** (separate installs).
- [ ] **`scripts/setup_env.ps1` (Windows) + `scripts/setup_env.sh` (macOS/Linux)** — one-command bootstrap that is **idempotent** and **prefers `uv` when present** (`uv python install 3.12` → `uv venv --python 3.12 .venv` → `uv pip install -r requirements.txt`), and **falls back** to a system Python 3.12/3.11 via the `py` launcher / `python3.12` (`-m venv .venv` → `pip install -r requirements.txt`). It must: detect/validate the Python is ≥3.11,<3.13 (refuse 3.13+ with a clear message), upgrade pip, install `requirements.txt`, then **print next steps** (activate command + the non-pip tools still needed: Databricks CLI, and confirm `az`/`bicep`). Never hard-fail if `uv` is absent.
- [ ] **`.gitignore`** — ensure `.venv/` (and `uv.lock` policy decision) are handled; do not commit the venv.
- [ ] **README + `docs/prerequisites.md`** — add a "Set up the Python environment" quick-start: `./scripts/setup_env.ps1` (or `.sh`), then activate, then run `preflight_checks.py`. Keep the Databricks-CLI/Azure-CLI/Bicep/Power BI Desktop notes.

**Verification:**
- [ ] On this host: `scripts/setup_env.ps1` creates `.venv` on **Python 3.12**, installs all of `requirements.txt` successfully, and a subsequent `python scripts/preflight_checks.py` (from the venv) flips the three tool/package WARNs (`semantic-link-labs` importable; `fab` on PATH) to PASS (Databricks CLI remains a documented separate install).
- [ ] Re-running the script is idempotent (detects existing `.venv`, no error).
- [ ] `requirements.txt` installs cleanly under Python 3.12 (all pins resolvable, incl. `policy-weaver==0.4.0`).

**Manual steps:** Databricks CLI is a standalone binary (not pip) — document its install (winget/curl/brew) in prerequisites; Power BI Desktop remains a Windows GUI install for PBIP authoring.

---

## 6. Dependency Graph / Wave Grouping (parallelization)

```
Wave A (parallel, no deps):           Step 1, Step 3
Wave B (after Step 1):                Step 2, Step 4
Wave C (Azure infra, parallel):       Step 5 (needs 1,4) ‖ Step 6 (needs 1)
Wave D (author hardening module):     Step 7 (needs 6)            # authoring only; applied in Step 12
Wave E (Databricks):                  Step 8 (needs 3,6) → Step 9 (needs 8)
Wave F (Fabric base):                 Step 10 (needs 5,2)         # creates Workspace Identity
Wave G (Ingestion, parallel):         Step 11 (needs 8,10) ‖ Step 12 (needs 9,10,7)   # Step 12 APPLIES hardening using Step 10 identity
Wave H:                               Step 13 (needs 11,12) → Step 14 (needs 13) → Step 15 (needs 14)
Wave I (Fabric IQ + RTI):             Step 16 (needs 14) → Step 17 (needs 14,16) ;
                                      Step 18 (needs 10) → Step 19 (needs 18; +16 only if enable_ontology) [DEFAULT Activator email] ‖ Step 20 (needs 18; +16 only if enable_ontology) [OPTIONAL Operations Agent/Teams]
Wave J (Governance, parallel):        Step 21 (needs 9,11,12) ‖ Step 22 (needs 8,11,14,15)
Wave K (Orchestration/docs):          Step 23 (needs 5–22) → Step 24 → Step 25 ; Step 26 (needs manual-step producers)
Wave L (final):                       Step 27 (needs 1–26)
```

**Dependency consistency check (per-step Depends ↔ graph):** Step 7 depends only on Step 6 (authoring) and is no longer expected to verify a real Workspace Identity rule — that moved to Step 12, which depends on Step 7 **and** Step 10 (identity producer). Every step's verification is now achievable given its declared dependencies. Step 18 depends only on Step 10 (workspace) and can run in parallel with the Direct Lake chain. The watch+act layer splits into **Step 19 (default Activator email, Teams-free)** and **Step 20 (optional Operations Agent, needs Teams)** — both **hard-depend on Step 18 (KQL/Eventhouse source) only**; **Step 16 (Ontology) is an optional/conditional enrichment dependency applied only when `enable_ontology=true`.** This ensures the **default Activator email path is never blocked by the only preview item (Ontology)**, so the GA-only fallback (Steps 26/27) holds. They can run in parallel; Step 20 is skipped when the tenant has no Teams (`enable_operations_agent=false`).

Critical path: 1 → 6 → 8 → 9 → 12 → 13 → 14 → 15 → 16 → 17 → 23 → 27 (real-time branch 10 → 18 → 19 [‖ optional 20] runs in parallel and rejoins at 27).

---

## 7. Package Dependencies

> Versions are **pinned tested minimums** (not `latest`) to reduce drift on preview APIs for a public reusable demo. Capture exact resolved versions in a lockfile (`requirements.txt` with `==` pins / `uv.lock`) during Step 1. Bump deliberately, re-test, and record.

| Package / Tool | Pinned minimum (tested) | Purpose |
|---|---|---|
| Azure CLI | ≥ 2.61.0 | Provision Azure resources, acquire Fabric user tokens (R8, R9) |
| Bicep CLI | ≥ 0.28.0 | Build/what-if Azure modules (R8) |
| Python | ≥ 3.11, < 3.13 | Scripting; required by Policy Weaver (R5) |
| `ms-fabric-cli` (`fab`) | ≥ 1.1.0 | Fabric REST/CLI automation; Eventhouse/workspace (R7) |
| `fabric-cicd` | ≥ 0.1.14 | Deploy Fabric items (report, Data Agent ALM) from repo (R7) |
| `semantic-link-labs` (`sempy_labs`) | ≥ 0.8.0 | Direct Lake model generation + TOM (R2) |
| `policy-weaver` | == 0.4.0 (Beta, exact pin) | UC access → OneLake security (R5) |
| Databricks CLI | ≥ 0.218.0 | UC SQL + Asset Bundles (DABs) (R8) |
| Databricks Terraform provider | ~> 1.49 (optional) | UC objects alternative to CLI/SQL (R8) |
| `requests` | ≥ 2.31.0 | Fabric REST calls (Data Agent, Activator/Reflex, Operations Agent, Eventhouse) with retry/backoff |
| Power BI Desktop | ≥ 2.130 (2026 release) | Author PBIP/PBIR advanced visuals (R2) |
| Microsoft Purview portal/REST/PowerShell | n/a (SaaS) | Governance scans, domains, labels, DLP (R6) |

---

## 8. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Target region** | **East US 2** (West US backup) | Only US region supporting ALL capabilities together incl. the optional **Operations Agent (GA)** with **local** Copilot/Data Agent AI. East US rejected: Operations Agent excludes East US (R11 §8); South Central US rejected: Ontology unavailable (R9 line 178). Verification matrix in §1.7. |
| Ingestion shows two variations | V1 mirroring (gold certified) + V2 Lakeflow→shortcut | Demo requirement; mirroring can't handle streaming tables/MVs (R1, R10). |
| V2 default sub-pattern | 2A (shortcut managed storage) + document 2B | Matches stated demo intent; 2B offered for cleaner governance (R10). |
| Ontology approach | Option C layered (semantic model → ontology → graph → agents) + GA-only fallback | Microsoft's recommended Fabric IQ layering; **only Ontology remains preview** (Graph, Operations Agent, Data Agent, Activator are GA per Build 2026), so the GA-only fallback (semantic model + report + Data Agent + Activator email) is robust (R4, R11). |
| Direct Lake mode | Direct Lake on OneLake | No SQL-endpoint coupling/fallback; tighter OneLake security; mirrored tables are native Delta (R2). |
| Thin Fabric aggregation layer | Yes (V-Order) | Mirroring doesn't apply V-Order; heavy KPI calcs need it (R2). |
| Mirroring create auth | User-token + one-time OAuth UI | REST API preview lacks SP support for mirrored catalog (R1, R7). |
| **Data Agent deploy** | **Fabric Data Agent REST API (primary)** + `fabric-cicd`/Git (secondary ALM) | R3 documents full REST CRUD + definition (`POST /v1/workspaces/{id}/dataAgents`, update/get/list/delete/getDefinition/updateDefinition — R3 lines 265–271). The earlier "no public item-create REST" claim was incorrect. Git/deployment pipelines used for promotion across environments (R3, R7). |
| **Watch+act data source** | **`KustoDatabase` (Eventhouse/KQL) time-series source** | Scenario is time-series (idle/maintenance spikes); R11 (§2) limits ontology grounding to basic properties and requires time-series binding to Eventhouse fields. The **same Eventhouse source feeds both** the default Activator email rule and the optional Operations Agent. Decided up-front (resolves former Open Question 3); adds Step 18 RTI artifacts. |
| **Watch+act default channel** | **Fabric Activator (GA) native Email — Teams-free, deployed as code** (Step 19) | Customer may not have Teams; the Operations Agent (even at GA) requires Teams for its default notification + Yes/No approval and has **no native email**. Activator has a first-class **Email** action with no Teams dependency, deployable via Reflex REST (`POST …/reflexes`) + `ReflexEntities.json` `EmailMessage`, and monitors the same Eventhouse properties + Ontology entities (R11 §6c). |
| **Operations Agent** | **Optional Teams enhancement** (Step 20; OperationsAgent REST `shouldRun`, user-token) | GA at Build 2026 (R11 §8); layered on top of the default Activator email for LLM-reasoned recommendations + **Teams Yes/No** approval; **requires Teams** + user-token create; action wiring UI-assisted; skipped when no Teams (R11 §6, §8). |
| Access policy sync | Policy Weaver (Beta) | Microsoft-published accelerator; only one mapping UC→OneLake security today (R5). |
| Lineage stitching | Narrate the seam, don't fake it | Databricks→Fabric lineage does not auto-stitch (R6). |
| Capacity | F64 (+ trial caveat) | Full feature+AI coverage; F64 needed for Data Agent/Copilot without Copilot-Capacity workaround (R9). Trial excludes AI + Operations Agent (R9, R11). |
| Config contract | Single `config_schema.py` validator, fail-fast | One canonical schema across infra/Databricks/Fabric/orchestration; explicit fresh-vs-existing paths (Step 1). |
| Provisioning flexibility | Fresh or existing via flags | Conventions requirement; existing-resources params bypass create steps. |
| Secrets | Key Vault + OIDC/MI/SP + placeholders | Public repo, no secrets (conventions). |
| Dependency pinning | Tested minimum versions + lockfile | Preview APIs drift; reproducible redeploy for a public demo. |

---

## 9. Assumptions

1. **User has Fabric admin** to enable all tenant settings (§3.4). *Impact if wrong:* preview/AI items can't be created. *Validate:* `preflight_checks.py` checks each setting.
2. **F64 (or paid AI-capable capacity) is acceptable** for the demo run. *Impact:* trial capacity can't run Data Agent/Copilot/Operations Agent (R9, R11). *Validate:* cost checkpoint (Step 4).
3. **Ontology (preview) remains available in East US 2** at run time. *Note:* Graph, Operations Agent, Data Agent, RTI/Eventhouse, and Activator are all **GA** (per Build 2026 — R4, R11), so **only Ontology** carries preview-availability risk. *Impact:* fall back to the GA-only path (semantic model + report + Data Agent + **Activator email alerting**). *Validate:* Step 27 fallback test. Region feasibility is **resolved** (§1.7), not assumed — East US 2 supports the Operations Agent (R11) and all other capabilities (R9); this assumption concerns only ongoing **Ontology** preview availability, not region eligibility.
4. **Mirrored-catalog create still requires user-token** (preview API) at run time. *Impact:* a manual-pause point in `deploy.py`. *Validate:* attempt SP create in preflight; fall back to user-token.
5. **2A managed-storage path (`__unitystorage`) is resolvable via `DESCRIBE DETAIL`** at run time. *Impact:* switch to 2B sink-to-external. *Validate:* Step 9 verification.
6. **Policy Weaver v0.4.0 single-catalog limitation** is acceptable for the demo (one `zava` catalog). *Impact:* multi-catalog needs multiple runs. *Validate:* Step 21.
7. **Synthetic PII columns are sufficient** to demo labels/DLP/masks. *Impact:* none (all synthetic). *Validate:* Step 22.
8. **Customer-supplied existing Databricks is UC-enabled Premium** when bringing their own. *Impact:* mirroring prereqs fail otherwise. *Validate:* preflight checks workspace SKU + UC.
9. **Eventstream can ingest the synthetic telematics feed** via a Custom App/sample endpoint in the tenant. *Impact:* if blocked, fall back to direct KQL ingest (`.ingest`) of generated events for the demo. *Validate:* Step 18 verification.
10. **Microsoft Teams is NOT assumed.** The default watch+act path (Activator email, Step 19) needs no Teams; the optional Operations Agent (Step 20) is enabled only when the tenant has Teams (`enable_operations_agent=true`). *Impact if Teams absent:* Step 20 is simply skipped — the demo's watch+act story is fully delivered by the Activator email. *Validate:* preflight Teams check (Step 23) gated on `enable_operations_agent`.

---

## 10. Open Questions

1. **Exact OneLake security role granularity** Policy Weaver produces for the Zava row filter — resolve during Step 21 implementation; impacts the governance validation script.
2. **Whether the Data Agent REST definition enum exposes Ontology as a source** in the target tenant at run time — resolve in Step 17; REST CRUD itself is confirmed available (R3), but the ontology source `type` may require UI attach; fall back to graph-source + UI attach (R4).
3. **Eventstream source connector choice** (Custom App vs Event Hub vs sample data) for the telematics feed — resolve at Step 18; affects connection setup automation vs manual. (Data-source *type* for the watch+act layer is **decided**: `KustoDatabase`/Eventhouse — see §8.)
4. **CI/CD (GitHub Actions + OIDC) scope** — decide in Step 23 how much of the user-token/manual surface to leave out of unattended CI (R7).
5. **Trial-capacity demo split** — whether to script a "trial-only data-engineering" subset separate from the F64 AI path (R9); resolve at Step 4/Step 27.
6. **Exact `ReflexEntities.json` rule schema** for the Activator email/Fabric-item action in the target tenant — verify against the live Reflex definition schema before relying on the scripted path (R11 §6c); resolve at Step 19.

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Ontology preview wobble** (Ontology is the **only** preview item; Graph/Operations Agent/Data Agent/RTI-Eventhouse/Activator are GA per Build 2026) | Ontology-grounded NL/graph features unavailable mid-show | Modular design + GA-only fallback (semantic model + report + Data Agent + **Activator email alerting** — all GA); Step 27 explicitly validates fallback (R4, R11). |
| **No Microsoft Teams in the customer tenant** | Operations Agent (needs Teams) can't notify/approve | **Default to the Teams-free Fabric Activator Email alert** (Step 19, deployed as code); Operations Agent is an **optional** Step 20 skipped when `enable_operations_agent=false` (R11 §6). |
| **Region/Operations-Agent mismatch** (optional path only) | Optional Operations Agent can't be created in East US/South Central US | Operations Agent is now **GA** (lower risk) but retains the East US/South Central US exclusion; region decided as **East US 2**; preflight enforces the region+Teams check **only when `enable_operations_agent=true`**; the default Activator email path works in all Fabric regions (R11 §8). |
| **Mirroring REST is preview / no SP** | Unattended CI can't create mirror | User-token pause-point in `deploy.py`; documented OAuth UI step (R1, R7). |
| **Databricks→Fabric lineage doesn't auto-stitch** | Governance story looks incomplete | Narrate the seam honestly + lineage runbook showing both sides (R6). |
| **F64 cost** | Budget overrun | Pause/resume scripts, cost doc, pre-deploy cost gate, aggressive pausing (~$138–300/mo) (R9). |
| **2A managed-storage shortcut bypasses UC governance** | Security misperception | Document caveat prominently; offer 2B sink-to-external for clean governance; harden storage network (R10). |
| **Policy Weaver is Beta, single-catalog** | Unsupported/limited | Treat as accelerator; pin v0.4.0 exactly; one `zava` catalog; document "as-is" (R5). |
| **Storage lockdown ordering** (Workspace Identity must exist first) | Shortcut read fails / impossible verification | Step 7 authors module only; Step 12 applies it after Step 10 creates the identity; identity-bound + negative tests live in Step 12 (R10). |
| **Long-running Fabric REST / 15-min mirror waits** | Brittle live demo / token expiry | Retry/backoff + token-refresh wrappers in mirror, Data Agent, Eventhouse, Activator, Operations Agent scripts and `deploy.py`. |
| **Eventstream connection setup** | Real-time path stalls | Document Custom App/sample-feed setup; fall back to direct KQL `.ingest` of generated events (Assumption 9). |
| **Activator `ReflexEntities.json` schema drift** | Scripted email rule fails to deploy | Verify the rule body against the live Reflex definition schema before relying on the code path; design-mode UI authoring is the documented fallback (R11 §6c; Open Question 6). |
| **Manual steps drift / customer confusion** | Failed redeploy | Single consolidated Manual Steps Appendix (Step 26); orchestrator pauses with exact instructions. |
| **Existing-resource path mismatches** (non-UC/non-Premium Databricks) | Mirroring prereqs fail | Preflight validates UC + Premium; clear error messaging (R8). |
| **Dependency drift on preview APIs** | "Works on my machine" | Pinned minimum versions + lockfile (§7). |

---

*Zava is a fictional company used for demonstration purposes only. All data in this repository is synthetic. No secrets are committed; use Key Vault, OIDC/managed identity, service principals, and placeholders.*
