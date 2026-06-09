# Zava — End-to-End Runbook

This runbook captures cross-step operational procedures for the Zava Databricks + Fabric
demo. It is **narrated procedure**, not a click-list (consolidated manual UI steps live in
`docs/manual-steps.md`).

The first half is the **full end-to-end run procedure** (deploy order, what each phase does, how
to demo it). The second half is a reusable **path-discovery** sub-procedure referenced during the
Variation-2 phase. A separate **validation checklist** is maintained at the end of this file.

> **⛔ Cost gate first.** The demo runs on a **Fabric F64** capacity (**PAYG ~$11.52/hour while
> Active**). Read and acknowledge [`cost.md`](./cost.md) **before** provisioning the capacity, and
> **pause the moment you finish** (`python scripts/pause_capacity.py`).
>
> **Public repo — NO secrets.** All scripts authenticate at runtime via `az login` /
> `DefaultAzureCredential`; config files carry placeholders only.

---

## END-TO-END RUN PROCEDURE

### Prerequisites

1. Tooling: `az` CLI, `databricks` CLI, Python 3.11+ (`<3.13`), and the demo Python deps
   (`pip install -r data/requirements.txt`; Fabric scripts use `DefaultAzureCredential`).
   See [`prerequisites.md`](./prerequisites.md) for the full list.
2. Sign in to the **MCAPS** subscription (repo-convention identity):
   ```bash
   az login
   az account set --subscription "<ME-MngEnvMCAP… subscription>"
   ```
3. Prepare config (copy samples to gitignored real files, fill `<PLACEHOLDER>` tokens, validate):
   ```bash
   cp fabric/config/deploy_config.sample.json        fabric/config/deploy_config.json
   cp databricks/config/databricks_config.sample.json databricks/config/databricks_config.json
   python scripts/config_schema.py --validate \
       fabric/config/deploy_config.json databricks/config/databricks_config.json
   ```
4. Decide **fresh vs existing** for Databricks and Fabric (independent toggles — see
   [`infra/README.md`](../infra/README.md)). Default is **fresh**.

### Phase 0 — Synthetic data (local)

Generate the batch entities + telematics stream (Step 3). Deterministic; no cloud calls.

```bash
python data/generate_zava_data.py --out ./data/output
python data/generate_telematics_stream.py --batch-dir ./data/output --count 200 \
    --inject-spike --out ./data/output/telematics_stream.ndjson
```

**What it does:** produces the 9 FK-consistent Zava entities (CSV + Parquet) and a telematics
NDJSON feed with an injectable spike that later drives the watch+act demo. See
[`data/README.md`](../data/README.md).

### Phase 1 — Azure infrastructure (Bicep)

```bash
az group create -n zava-demo-rg -l eastus2
az deployment group create -g zava-demo-rg -f infra/main.bicep \
    -p infra/params/dev.local.bicepparam       # or existing-resources.local.bicepparam
```

**What it does:** provisions (fresh path) the **F64 Fabric capacity**, **Premium Databricks
workspace**, **Access Connector**, **ADLS Gen2** (Unity Catalog managed storage), and **Key
Vault**. On the existing path it skips the resources you bring and passes their ids through.
Network hardening is authored now but **applied later** (Phase 4). See
[`infra/README.md`](../infra/README.md).

### Phase 2 — Databricks medallion + Lakeflow (Asset Bundle)

**Manual prerequisite:** a metastore admin enables **External data access** on the metastore
(`databricks/uc/01_metastore_external_access.sql`) so Fabric mirroring can read UC Delta. Replace
group/principal placeholders in `uc/03`, `uc/04`, `uc/05` first.

```bash
# from databricks/bundle/
databricks bundle validate
databricks bundle deploy
databricks bundle run zava_uc_setup            # catalog + medallion schemas + mirroring grants
databricks bundle run zava_medallion_pipeline  # 00 -> 10 -> 20 -> 30  (build certified gold)
databricks bundle run zava_certify_gold        # tag/comment/own the certified asset
databricks bundle run zava_lakeflow_curated    # curated Variation-2 source (MV + streaming table)
```

Then apply the UC access policies (as the table OWNER): run `databricks/uc/05_access_policies.sql`.

**What it does:** builds raw → bronze → silver → **certified gold** (managed Delta — mirrorable)
and the **curated** Lakeflow outputs (not mirrorable → shortcut source). See
[`databricks/README.md`](../databricks/README.md).

### Phase 3 — Fabric workspace + ingestion

```bash
python fabric/scripts/00_create_workspace.py        # workspace + F64 + Workspace Identity
python fabric/scripts/10_create_mirrored_catalog.py # Variation 1: mirror certified gold
python fabric/scripts/20_create_shortcut.py         # Variation 2: shortcut curated storage
```

**Variation 2 needs the managed-storage `abfss://` path** — discover it with the **path-discovery
sub-procedure** below (the Lakeflow pipeline must have run at least once, which Phase 2 did).

**What it does:** creates the Fabric workspace bound to the F64 capacity with a Workspace
Identity, then lands both ingestion variations in **OneLake**. See
[`fabric/README.md`](../fabric/README.md).

### Phase 4 — Apply ADLS network hardening (Variation-2 lockdown)

Now that the Fabric **Workspace Identity** exists, re-deploy `infra/main.bicep` passing the
**Fabric workspace GUID** (and optional `workspaceIdentityObjectId`) so the storage firewall
locks to default-deny with a **trusted-workspace** rule:

```bash
az deployment group create -g zava-demo-rg -f infra/main.bicep \
    -p infra/params/dev.local.bicepparam \
    --parameters fabricWorkspaceId=<WORKSPACE_GUID> \
                 workspaceIdentityObjectId=<WORKSPACE_IDENTITY_OBJECT_ID>
```

**What it does:** closes the Variation-2 storage so only the trusted Fabric workspace reaches it.
This is the storage-side half of the governance seam (the Fabric-side half is OneLake security).

### Phase 5 — Consumption layer + report

```bash
python fabric/scripts/40_build_thin_gold.py         # thin consumption aggregates (Fabric notebook)
python fabric/scripts/30_create_semantic_model.py   # Direct Lake semantic model (Zava Fleet Analytics)
python fabric/scripts/50_deploy_report.py           # Power BI report (Zava Fleet Dashboard)
```

**What it does:** builds report-ready KPI aggregates, a **Direct Lake** semantic model over them,
and the blue-themed report (multi-city map, decomposition tree, revenue forecast).

### Phase 6 — Fabric IQ (ontology, graph, Data Agent)

```bash
python fabric/scripts/60_create_ontology.py         # ontology (preview) + graph (GA)  [enable_ontology]
python fabric/scripts/70_create_data_agent.py       # Data Agent (GA)                   [enable_data_agent]
```

**What it does:** adds the semantic/ontology layer and natural-language Q&A. **GA-only fallback:**
if Ontology (the only preview item) is unavailable, semantic model + report + Data Agent still
work.

### Phase 7 — Real-Time Intelligence + watch-and-act

```bash
python fabric/scripts/75_create_eventhouse.py       # Eventhouse + KQL + Eventstream    [enable_eventhouse]
python fabric/scripts/78_create_activator_email.py  # DEFAULT: Activator email alert    [enable_activator_email]
python fabric/scripts/80_create_operations_agent.py # OPTIONAL: Operations Agent (Teams) [enable_operations_agent]
```

**What it does:** stands up the telematics RTI pipeline and the **default, Teams-free** Activator
email alert. The Operations Agent is **optional** (`enable_operations_agent=true`) and is the only
path that needs **Teams** (and carries the East-US region exclusion — see
[`architecture.md`](./architecture.md) §8).

### Phase 8 — Governance

```bash
python scripts/governance/policy-weaver/run_policy_weaver.py   # UC policies -> OneLake Security (V1)
python scripts/governance/purview/setup_purview_scans.py       # catalog + lineage + classification
```

**What it does:** syncs the Databricks row filter + column mask into Fabric OneLake Security for
the mirrored (Variation-1) data, and catalogs/classifies both Databricks and Fabric in Purview.
Remember the **Variation-2 governance seam**: UC policies do **not** follow a storage shortcut —
Fabric-side security is the enforcement layer there (see the caveat in the path-discovery section).

### How to demo it (the story in ~5 moves)

1. **Agility** — show the certified gold mirrored into OneLake with **no ETL**, and the Direct
   Lake report rendering maps / decomposition tree / forecast.
2. **Insights beyond BI** — ask the **Data Agent** a business question; show it answers the same
   number as the matching Power BI measure (and the ontology query).
3. **Real-time watch+act** — inject the telematics spike; the **Activator email** lands for the
   site manager with **no Teams**. (Optionally show the Operations Agent **Teams** card.)
4. **Governance** — sign in as a constrained Seattle manager; the report, Data Agent, and OneLake
   all show only Seattle rentals with masked email — proving the Policy Weaver sync.
5. **Both variations** — show V1 (mirrored gold) and V2 (shortcut curated) both surfacing in the
   report, and call out the governance seam + its mitigation.

> **Pause when done:** `python scripts/pause_capacity.py`.

---

## Managed-storage PATH DISCOVERY — `abfss://` target for the Variation-2 OneLake shortcut

**When:** after the Step-9 Lakeflow pipeline (`databricks/pipelines/lakeflow_sdp.py`) has run
at least once and produced its curated **managed** outputs
(`<catalog>.curated.rentals_curated`, `<catalog>.curated.telematics_curated`).

**Why:** Variation 2 does **not** mirror these tables — streaming tables and materialized
views are **Unity Catalog managed tables** and are explicitly **excluded from Fabric Azure
Databricks mirroring** (R10 §2.3). The **default** flow surfaces the `telematics_curated`
**streaming table** (the V2 signal `40_build_thin_gold.py` consumes into
`agg_telematics_freshness`, which the semantic model + report bind to). The Step-12 script
(`fabric/scripts/20_create_shortcut.py`) creates a **OneLake shortcut** straight to the
Databricks-managed ADLS Gen2 storage that backs the table (**sub-pattern 2A**), placing it in
the Lakehouse **`Tables`** area with name **`telematics_curated`** so Direct Lake recognizes it
and `spark.table("telematics_curated")` resolves by bare name. To create that shortcut you must
first obtain the concrete `abfss://` path of the managed storage.

### Procedure (sub-pattern 2A — shortcut managed storage)

Run the following in a **Databricks SQL editor / notebook** against the pipeline output:

```sql
-- DESCRIBE DETAIL returns a 'location' column carrying the abfss:// managed-storage path.
-- For a UC MANAGED table the path is under the catalog/schema managed-storage root, in the
-- hashed `__unitystorage` layout (see below).
DESCRIBE DETAIL zava.curated.telematics_curated;

-- Alternatively, inspect the table's extended metadata (look for "Storage Location"):
DESCRIBE EXTENDED zava.curated.telematics_curated;
```

- The `location` value looks like:
  `abfss://<container>@<storage-account>.dfs.core.windows.net/<root>/__unitystorage/schemas/<guid>/tables/<guid>`
  (R10 §2.2). UC adds **hashed `__unitystorage/...` subdirectories** so every managed entity
  has a unique, non-obvious location; the fully-qualified path is tracked as the table's
  **Storage Location** in Unity Catalog.
- Feed that `abfss://` path into the Step-12 shortcut creation (point the shortcut at the
  `telematics_curated` table directory — at least one level below the container, never the
  container root — per R10 §5.3). Set `deploy_config.json` `shortcut.name="telematics_curated"`
  and `shortcut.path="Tables"` (the sample default) so the created shortcut matches the
  notebook read + model/report binding.

### ⚠️ Governance caveat (R10 §5.1 — narrate this, it is not a click)

A direct-storage shortcut **bypasses Unity Catalog enforcement at the storage layer**:

> "Unity Catalog policies such as RLS/CLM or ABAC are not enforced at the storage layer and
> will not be applied if a connection is used to directly access storage."
> ("Unity Catalog privileges are not enforced when users access data files from external
> systems.")

Consequences and mitigations:

1. **UC RLS / column masks / ABAC are NOT enforced** through a 2A shortcut (the Step-9
   `05_access_policies.sql` row filter + column mask do **not** follow the data). **Re-enforce
   access in Fabric** — OneLake security + Fabric workspace permissions — and at the storage
   account via **RBAC**, with a least-privilege connection identity (prefer Workspace
   Identity / service principal over account keys).
2. **Path-stability caveat:** the `__unitystorage` hash layout is an **internal, non-contractual**
   UC path. UC may compact/relocate files (e.g., predictive optimization), and dropping a
   managed table deletes its storage after ~8 days. **Avoid destructive operations on the
   source** and monitor for path drift; re-run `DESCRIBE DETAIL` if the shortcut breaks.
3. Policy Weaver / OneLake-security policy mapping is a **mirroring** feature (R5) and does
   **not** automatically apply to a raw-storage shortcut — Fabric-side security must be set
   explicitly.

### Cleaner alternative (sub-pattern 2B — owned external location)

If path stability and a clean governance boundary matter (production), use the **Lakeflow
sink to an external location** (`databricks/pipelines/lakeflow_sink_external.py`): a
`dp.create_sink(format="delta", ...)` + `@dp.append_flow` writes curated rentals to an
**owned `abfss://` path** you control (or a downstream job / CTAS does). Discover that stable
path the same way — but against the **external** table, which is **not** under
`__unitystorage` and does not drift:

```sql
DESCRIBE DETAIL zava.curated.rentals_curated_ext;   -- stable, owned abfss:// path
```

The 2A vs 2B trade-off (R10 §5.1): **2A** is fastest and matches the demo's stated intent of
shortcutting Databricks-managed storage directly — accept the governance/path caveats and pin
Fabric-side controls. **2B** costs an extra write step (sink/job, append-only semantics) but
yields a stable path and a cleaner governance boundary. Either way, UC policies do **not**
follow the data through a direct-storage shortcut — Fabric/OneLake security is the enforcement
layer for Variation 2.

---

## END-TO-END VALIDATION CHECKLIST

> **Purpose.** A presenter (or an automated live-tester, in the later deployment phase) follows
> this to **prove the whole pipeline works end-to-end** against the deployed demo in **East US 2**.
> Tick each `- [ ]` as you confirm the **observable success signal**. This checklist does **not**
> deploy anything — run the *END-TO-END RUN PROCEDURE* above first, then validate here.
>
> **Manual UI steps are not duplicated** — where a check requires a portal click, it points to the
> numbered entry in [`docs/manual-steps.md`](./manual-steps.md).
>
> **Region:** all checks assume the workspace/capacity are in **East US 2**. The **only** path that
> carries an East-US region exclusion is the OPTIONAL Operations Agent (Teams) — see
> [`architecture.md`](./architecture.md) §8.

### How to use this checklist

- **Default (Teams-free) demo:** complete sections **A, B, C, E, F, G, H** and the default rows in
  **I**. You can skip every item tagged *(OPTIONAL — Teams)*.
- **Full demo (Teams available):** also complete section **D** and the optional rows in **I**, which
  require `features.enable_operations_agent=true` in `fabric/config/deploy_config.json`.
- **Feature gates** (in `fabric/config/deploy_config.json` → `features`): `enable_ontology`,
  `enable_data_agent`, `enable_eventhouse`, `enable_activator_email` (all **default true**) and
  `enable_operations_agent` (**default false**). Ontology is the **only preview** item; everything
  else is GA (drives the section **G** fallback).

---

### A. Full happy path (ordered) — each stage: command/script → observable success signal

Run these in order. The command column references **real repo scripts/notebooks**; the signal is
what you must see before ticking the box.

- [ ] **A0 — Synthetic data.** `python data/generate_zava_data.py --out ./data/output` then
  `python data/generate_telematics_stream.py --batch-dir ./data/output --count 200 --inject-spike --out ./data/output/telematics_stream.ndjson`.
  **Signal:** 9 FK-consistent entity files (CSV + Parquet) under `./data/output`, and the generator
  prints `[telematics] spike window: events [...] ... elevated events=N`.
- [ ] **A1 — Databricks medallion (raw→bronze→silver→gold certified).** From `databricks/bundle/`:
  `databricks bundle run zava_uc_setup` → `databricks bundle run zava_medallion_pipeline` →
  `databricks bundle run zava_certify_gold`.
  **Signal:** the `00 → 10 → 20 → 30` notebook chain succeeds; the certified gold table exists in UC
  with the certification tag/comment/owner set by `databricks/uc/04_certify_gold.sql`.
- [ ] **A2 — Lakeflow curated pipeline (Variation-2 source).** `databricks bundle run zava_lakeflow_curated`.
  **Signal:** `<catalog>.curated.rentals_curated` (materialized view) and
  `<catalog>.curated.telematics_curated` (streaming table) are populated.
- [ ] **A3 — UC access policies applied.** As the table OWNER, run `databricks/uc/05_access_policies.sql`
  (row filter + column mask). **Signal:** `SET ROW FILTER` / `SET MASK` succeed; querying as a
  non-`zava_seattle_mgr` / non-`zava_pii_authorized` principal shows filtered rows + masked email.
- [ ] **A4 — Fabric workspace + Workspace Identity.** `python fabric/scripts/00_create_workspace.py`.
  **Signal:** workspace bound to the F64 capacity exists; Workspace Identity present (confirm via
  [`manual-steps.md`](./manual-steps.md) #3.1 if reviewing in UI).
- [ ] **A5 — Ingestion V1 (mirror certified gold).** `python fabric/scripts/10_create_mirrored_catalog.py`.
  **Signal:** a Mirrored Azure Databricks Catalog item appears in OneLake and the gold tables show
  rows (UI confirm: [`manual-steps.md`](./manual-steps.md) #3.2).
- [ ] **A6 — Ingestion V2 (OneLake shortcut to curated storage).**
  `python fabric/scripts/20_create_shortcut.py` (use the `abfss://` path from the
  *Managed-storage PATH DISCOVERY* sub-procedure above).
  **Signal:** a OneLake shortcut to the curated table directory exists and previews curated rows
  (UI confirm: [`manual-steps.md`](./manual-steps.md) #3.3).
- [ ] **A7 — Thin gold / aggregations.** `python fabric/scripts/40_build_thin_gold.py`.
  **Signal:** the `agg_*` consumption tables (e.g. `agg_revenue_by_site`,
  `agg_fleet_utilization_by_site_month`, `agg_idle_vehicles_by_site`) are built in the Lakehouse.
- [ ] **A8 — Direct Lake semantic model.** `python fabric/scripts/30_create_semantic_model.py`.
  **Signal:** the **Zava Fleet Analytics** semantic model deploys in **Direct Lake** mode with
  measures (`Site Revenue`, `Fleet Utilization %`, `Idle Vehicle Count`) and the `CityManager` RLS
  role + OLS email/phone masking applied.
- [ ] **A9 — Power BI report.** `python fabric/scripts/50_deploy_report.py`.
  **Signal:** the **Zava Fleet Dashboard** renders the multi-city map, decomposition tree, and
  revenue forecast over the Direct Lake model (report author step: [`manual-steps.md`](./manual-steps.md) #30).
- [ ] **A10 — Ontology + graph.** `python fabric/scripts/60_create_ontology.py` *(requires
  `enable_ontology=true`)*. **Signal:** the Ontology item + Graph are generated from the semantic
  model (UI generation: [`manual-steps.md`](./manual-steps.md) #31; the item definition is
  `fabric/ontology/ontology_definition.json`).
- [ ] **A11 — Data Agent.** `python fabric/scripts/70_create_data_agent.py` *(requires
  `enable_data_agent=true`)*. **Signal:** a Data Agent with the semantic-model **and** graph data
  sources answers a natural-language question (attach source if needed: [`manual-steps.md`](./manual-steps.md) #32).
- [ ] **A12 — Eventhouse + KQL + Eventstream.** `python fabric/scripts/75_create_eventhouse.py`
  *(requires `enable_eventhouse=true`)*. **Signal:** Eventhouse + `zava_rt` KQL DB + `Telematics`
  table created (DDL from `fabric/realtime/eventhouse_setup.kql`) and an Eventstream (from
  `fabric/realtime/eventstream_definition.json`) with a `CustomEndpoint` source exists. Wire the
  source credential per [`manual-steps.md`](./manual-steps.md) #33.
- [ ] **A13 — Activator EMAIL alert (DEFAULT, Teams-free).**
  `python fabric/scripts/78_create_activator_email.py` *(requires `enable_activator_email=true`)*.
  **Signal:** a Reflex item (definition `fabric/activator/reflex_entities.json`) deploys with an
  `EmailMessage` action targeting `alerting.site_manager_email`; the `idle_minutes > 120` rule is
  valid in design mode ([`manual-steps.md`](./manual-steps.md) #34). **No Teams involved.**
- [ ] **A14 — Operations Agent (OPTIONAL — Teams).**
  `python fabric/scripts/80_create_operations_agent.py` *(only when `enable_operations_agent=true`;
  user-token create + Teams app per [`manual-steps.md`](./manual-steps.md) #35–#36)*.
  **Signal:** Operations Agent (config `fabric/operations-agent/Configurations.json`) created.
  *Skip entirely on the default path.*
- [ ] **A15 — Policy Weaver.** `python scripts/governance/policy-weaver/run_policy_weaver.py`
  (config `scripts/governance/policy-weaver/policy_weaver_config.yaml`).
  **Signal:** the UC row filter + column mask are synced into OneLake Security data-access roles for
  the **mirrored (V1)** data (role review: [`manual-steps.md`](./manual-steps.md) #37).
- [ ] **A16 — Purview.** `python scripts/governance/purview/setup_purview_scans.py`.
  **Signal:** Databricks + Fabric are cataloged with lineage/classification (UI: domains/data
  product/live-view/labels per [`manual-steps.md`](./manual-steps.md) #38–#40; see
  `scripts/governance/purview/lineage_runbook.md`).

---

### B. Happy-path consistency check (one business question, three surfaces)

**Business question:** *"Which Zava sites have the most idle vehicles right now?"* The **same ranked
numbers** must appear in all three surfaces below.

- [ ] **B1 — Power BI measure.** In the **Zava Fleet Dashboard**, read the **`Idle Vehicle Count`**
  measure (table `agg_idle_vehicles_by_site`) broken down by site. **Record** the top site and its
  count.
- [ ] **B2 — Data Agent.** Ask the Data Agent *"Which sites have the most idle vehicles?"*.
  **Signal:** the returned ranking + counts **match B1** (this is the seeded few-shot
  `fabric/data-agent/.../graph-ZavaFleetOntologyGraph/fewshots.json`).
- [ ] **B3 — Ontology / graph query.** Run the graph query
  `MATCH (v:Vehicle)-[:located_at]->(s:RentalSite) WHERE v.Status = 'idle' RETURN s.SiteName, COUNT(v) AS IdleVehicles ORDER BY IdleVehicles DESC`
  against the Graph. **Signal:** the top site + count **match B1 and B2** — three surfaces, one
  number. ✅ tick only if all three agree.

> If `enable_ontology=false`, B3 is unavailable — see section **G** for the GA-only fallback (B1 + B2
> still match).

---

### C. Real-time watch+act — DEFAULT (Teams-free email)

- [ ] **C1 — Inject the telematics spike.** Generate an idle spike and push it to the Eventstream
  custom endpoint:
  `python data/generate_telematics_stream.py --batch-dir ./data/output --count 200 --inject-spike --spike-type idle --out ./data/output/telematics_stream.ndjson`,
  then replay the NDJSON into the Eventstream `CustomEndpoint` (connection string copied in the UI —
  [`manual-steps.md`](./manual-steps.md) #33; the endpoint is never committed). **Signal:** rows with
  `is_spike: true` and `idle_minutes > 120` land in the `Telematics` KQL table.
- [ ] **C2 — Activator email fires.** **Signal:** an **email** arrives at
  `alerting.site_manager_email` (the site manager) for the idle-vehicle condition. **Confirm NO
  Microsoft Teams message, card, or channel is involved** — the action is a first-class
  `EmailMessage` only.

---

### D. Real-time watch+act — OPTIONAL (Teams) — *only if `enable_operations_agent=true`*

- [ ] **D1 — (OPTIONAL — Teams) Same spike → Operations Agent recommendation.** With the Operations
  Agent deployed (A14) and the Teams app installed ([`manual-steps.md`](./manual-steps.md) #36),
  re-run the C1 spike. **Signal:** a **Microsoft Teams** recommendation/approval **card** appears for
  the site manager with a Yes/No human-in-the-loop action. *Skip on the default path.*

---

### E. Governance — constrained user sees identical restrictions in 3 places

**Test user setup:** add the constrained test user to the **`zava_seattle_mgr`** group **only** (and
**not** to `zava_pii_authorized`) — the same account groups consumed by
`databricks/uc/05_access_policies.sql` and woven by Policy Weaver (A15). The user must see **only
Seattle rentals** with **email masked** in every surface below.

- [ ] **E1 — Direct Lake report.** Signed in as the constrained user, the **Zava Fleet Dashboard**
  shows **only Seattle** rows (CityManager RLS) with **email masked** (OLS). 
- [ ] **E2 — Data Agent.** The same user asks a cross-city question; the Data Agent returns **only
  Seattle** data with masked PII — identical restriction to E1.
- [ ] **E3 — OneLake.** Browsing the **mirrored (V1)** Lakehouse data in OneLake as the same user
  shows **only Seattle** rows with masked email — the OneLake Security roles Policy Weaver produced
  (review/assign: [`manual-steps.md`](./manual-steps.md) #37). ✅ tick only if E1 = E2 = E3.

> Variation-2 caveat (R10 §5.1): UC policies do **not** follow the direct-storage shortcut —
> Fabric-side OneLake security + storage RBAC are the enforcement layer there (see the
> *PATH DISCOVERY* governance caveat above). Validate V2 restriction via the Fabric-side controls.

---

### F. Both ingestion variations surface in the report

- [ ] **F1 — V1 (mirrored gold).** Confirm a report visual / table sourced from the **mirrored
  certified gold** (A5) renders data.
- [ ] **F2 — V2 (shortcut streaming/MV).** Confirm a report visual / table sourced from the **OneLake
  shortcut** over the curated streaming table + materialized view (A6) renders data. ✅ tick when
  **both** V1 and V2 are visible in the same report.

---

### G. Resilience / GA-only fallback (Ontology — the only preview item — disabled)

Set `features.enable_ontology=false` in `fabric/config/deploy_config.json` (skip A10 and the graph
data source). The **all-GA** path must still deliver:

- [ ] **G1 — Semantic model + report.** A8 + A9 still build and render (GA Direct Lake).
- [ ] **G2 — Data Agent.** A11 still answers the business question using the **semantic-model** data
  source alone (no graph). B1 ↔ B2 numbers still match.
- [ ] **G3 — Activator email alerting.** A13 + section **C** still deliver the site-manager **email**
  (GA Eventhouse binding — `reflex_entities.json` binds to the Eventhouse, not the ontology). ✅ tick
  when report + Data Agent + Activator email all work with Ontology off.

---

### H. Cost hygiene (pause capacity at the end)

- [ ] **H1 — Pause the F64 capacity.** Run `python scripts/pause_capacity.py` (preview first with
  `python scripts/pause_capacity.py --dry-run`; pass `--subscription`, `--resource-group`,
  `--capacity` if not auto-resolved). **Signal:** the capacity reports **Paused/Suspended** — billing
  stops. *Do this the moment the demo ends* (see [`cost.md`](./cost.md)).

---

### I. Manual confirmations (presenter sign-off)

- [ ] **I1 — DEFAULT:** the **Activator email** arrived at the site manager **without any Teams**
  involvement (re-confirms C2).
- [ ] **I2 — (OPTIONAL — Teams):** the **Operations Agent Teams card** appeared and the Yes/No
  approval worked (re-confirms D1). *Skip on default path.*
- [ ] **I3 — (OPTIONAL — preview):** **ontology generation** produced the expected Graph/entities
  (re-confirms A10). *Skip when `enable_ontology=false`.*

---

### Timings to capture

Record wall-clock duration for each, for the demo script and capacity-cost estimate:

- [ ] **T1** — Synthetic data generation (A0).
- [ ] **T2** — Databricks medallion + certify (A1–A3).
- [ ] **T3** — Fabric ingestion: V1 mirror (A5) **and** V2 shortcut (A6) — time each.
- [ ] **T4** — Thin gold + semantic model + report (A7–A9).
- [ ] **T5** — Fabric IQ: ontology + Data Agent (A10–A11).
- [ ] **T6** — RTI stand-up (A12–A13) and **spike-to-email latency** (C1 → C2).
- [ ] **T7** — Governance: Policy Weaver + Purview (A15–A16).
- [ ] **T8** — Total elapsed **Active** capacity time (drives the [`cost.md`](./cost.md) estimate).

### Screenshots to capture for the demo script

Capture these stills (in order) to assemble the screenshot-based demo script:

- [ ] **S1** — Certified gold in OneLake (mirrored, **no ETL**) — the "agility" opener.
- [ ] **S2** — Zava Fleet Dashboard: multi-city **map**, **decomposition tree**, revenue **forecast**.
- [ ] **S3** — The B1/B2/B3 trio side-by-side (Power BI measure ↔ Data Agent ↔ graph) showing the
  **same number**.
- [ ] **S4** — The **Activator email** in the site manager's inbox (and the design-mode rule),
  **with no Teams**.
- [ ] **S5** — *(OPTIONAL — Teams)* the Operations Agent **Teams** recommendation card.
- [ ] **S6** — Governance: constrained Seattle user seeing only Seattle + masked email in the
  **report, Data Agent, and OneLake** (E1–E3).
- [ ] **S7** — Report showing **V1 and V2** data together (F1 + F2).
- [ ] **S8** — Capacity **Paused** confirmation (H1).
