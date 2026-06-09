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

1. Tooling: `az` CLI, `databricks` CLI, Python 3.10+, and the demo Python deps
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
Databricks mirroring** (R10 §2.3). Instead, the Step-12 script
(`fabric/scripts/20_create_shortcut.py`) creates a **OneLake shortcut** straight to the
Databricks-managed ADLS Gen2 storage that backs the table (**sub-pattern 2A**). To create
that shortcut you must first obtain the concrete `abfss://` path of the managed storage.

### Procedure (sub-pattern 2A — shortcut managed storage)

Run the following in a **Databricks SQL editor / notebook** against the pipeline output:

```sql
-- DESCRIBE DETAIL returns a 'location' column carrying the abfss:// managed-storage path.
-- For a UC MANAGED table the path is under the catalog/schema managed-storage root, in the
-- hashed `__unitystorage` layout (see below).
DESCRIBE DETAIL zava.curated.rentals_curated;

-- Alternatively, inspect the table's extended metadata (look for "Storage Location"):
DESCRIBE EXTENDED zava.curated.rentals_curated;
```

- The `location` value looks like:
  `abfss://<container>@<storage-account>.dfs.core.windows.net/<root>/__unitystorage/schemas/<guid>/tables/<guid>`
  (R10 §2.2). UC adds **hashed `__unitystorage/...` subdirectories** so every managed entity
  has a unique, non-obvious location; the fully-qualified path is tracked as the table's
  **Storage Location** in Unity Catalog.
- Feed that `abfss://` path into the Step-12 shortcut creation (point the shortcut at the
  table directory — at least one level below the container, never the container root — per
  R10 §5.3).

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
