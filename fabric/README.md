# `fabric/` — Microsoft Fabric items + deployment scripts (as code)

This folder is the **focus of the demo**: it takes the certified Databricks gold asset and turns
it into governed business insight in **Microsoft Fabric** — mirroring, **Direct Lake**
reporting, a semantic/**ontology** layer for natural-language insights, **Real-Time
Intelligence**, and a watch-and-act alerting layer. Everything is provisioned **as code** via
Fabric REST APIs / `fabric-cli` / `semantic-link-labs`, driven by `config/deploy_config.json`.

> **Public repo — NO secrets.** Scripts authenticate with `DefaultAzureCredential`
> (`az login` / managed identity / env service principal). The config holds **placeholders**
> only (no addresses, no ids that aren't yours).

---

## What's here

| Path | What it is |
|---|---|
| `config/deploy_config.sample.json` | The single config that drives every script: region, capacity, workspace, source catalog, ingestion variation, **feature flags**, RTI names, alert recipient, governance. Copy to gitignored `deploy_config.json`. |
| `scripts/00_create_workspace.py` | Creates/attaches the Fabric **workspace**, assigns the **F64 capacity**, provisions the **Workspace Identity**. |
| `scripts/10_create_mirrored_catalog.py` | **Variation 1** — zero-copy **Mirrored Azure Databricks Catalog** over the certified gold schema (no ETL, ~15-min auto-sync). |
| `scripts/20_create_shortcut.py` | **Variation 2** — a **OneLake shortcut** onto the Databricks-managed `abfss://` storage backing the Lakeflow curated tables (streaming table / materialized view — not mirrorable). |
| `scripts/40_build_thin_gold.py` | The **thin gold / consumption layer** — report-ready aggregate tables on top of the mirrored + shortcut data (the "thin calc" for Direct Lake). |
| `scripts/30_create_semantic_model.py` | The **Direct Lake** semantic model (`Zava Fleet Analytics`) over the thin gold. |
| `scripts/50_deploy_report.py` | Deploys the **Power BI report** (`Zava Fleet Dashboard`) bound to the Direct Lake model. |
| `scripts/60_create_ontology.py` | The Fabric **Ontology** (preview) over the business entities; auto-creates a **Graph** (GA). |
| `scripts/70_create_data_agent.py` | The Fabric **Data Agent** (GA) — natural-language Q&A over the semantic model + graph. |
| `scripts/75_create_eventhouse.py` | **Real-Time Intelligence** — **Eventhouse** + KQL Telematics table + **Eventstream**. |
| `scripts/78_create_activator_email.py` | **DEFAULT** watch+act — a Fabric **Activator** rule that emails the site manager on a telematics spike. **Teams-free**, code-deployable. |
| `scripts/80_create_operations_agent.py` | **OPTIONAL** watch+act — the **Operations Agent** (GA) adds LLM-reasoned recommendations via a **Teams** approval card. Runs only when `enable_operations_agent=true`. |
| `semantic-model/` | The semantic-model project (TMDL: tables, relationships, roles incl. `CityManager` RLS, expressions). Has its own [README](./semantic-model/README.md). |
| `report/` | The PBIP report project (executive overview, **multi-city map**, **decomposition tree**, **revenue forecast**), blue Zava theme. Has its own [README](./report/README.md). |
| `data-agent/` | The Data Agent item definition + few-shots. Has its own [README](./data-agent/README.md). |
| `ontology/ontology_definition.json` | The ontology entity/relationship definition. |
| `realtime/` | `eventhouse_setup.kql` + `eventstream_definition.json` for RTI. |
| `activator/reflex_entities.json` | The Activator (Reflex) rule definition for the email alert. |
| `operations-agent/Configurations.json` | The Operations Agent configuration (optional path). |
| `theme/zava-blue-theme.json` | The Zava blue Power BI theme. |

---

## The two ingestion variations

| | **Variation 1 — Mirror** | **Variation 2 — Managed-storage shortcut** |
|---|---|---|
| Script | `10_create_mirrored_catalog.py` | `20_create_shortcut.py` |
| Source | Certified **gold** managed Delta | Lakeflow **curated** managed tables (streaming table / MV) |
| Mechanism | **Mirrored Azure Databricks Catalog** (zero-copy, auto-sync) | **OneLake shortcut** to the backing `abfss://` storage |
| Governance | UC policies sync via **Policy Weaver** | UC policies **do not** follow storage — re-enforce in Fabric (the **lineage/governance seam**) |

Both surface in OneLake and feed the thin gold → Direct Lake → report path. See
[`docs/architecture.md`](../docs/architecture.md) and the path-discovery procedure in
[`docs/runbook-end-to-end.md`](../docs/runbook-end-to-end.md).

---

## Feature flags (`config/deploy_config.json`)

The `features` block gates the optional layers so you can run a lean or full demo:

| Flag | Default | Controls |
|---|---|---|
| `enable_ontology` | `true` | Ontology (the **only preview** item) + Graph. |
| `enable_data_agent` | `true` | The Data Agent (GA). |
| `enable_eventhouse` | `true` | Eventhouse / KQL / Eventstream (GA). |
| `enable_activator_email` | `true` | **Default, Teams-free** Activator email alert (GA). |
| `enable_operations_agent` | `false` | **Optional** Operations Agent + **Teams** card (GA, requires Teams). |

> **GA-only fallback:** if Ontology (preview) is unavailable, everything else — semantic model,
> report, Data Agent, and Activator email alerting — is **GA** and still works. The watch+act
> story works with **no Teams** via the default Activator email path.

---

## Run order

```text
00_create_workspace.py          # workspace + F64 capacity + Workspace Identity
10_create_mirrored_catalog.py   # Variation 1 (mirror certified gold)
20_create_shortcut.py           # Variation 2 (shortcut Lakeflow curated storage)
40_build_thin_gold.py           # thin consumption-layer aggregates
30_create_semantic_model.py     # Direct Lake semantic model
50_deploy_report.py             # Power BI report bound to the model
60_create_ontology.py           # ontology (preview) + graph (GA)        [enable_ontology]
70_create_data_agent.py         # Data Agent (GA) over model + graph     [enable_data_agent]
75_create_eventhouse.py         # Eventhouse + KQL + Eventstream (RTI)    [enable_eventhouse]
78_create_activator_email.py    # DEFAULT watch+act — Activator email     [enable_activator_email]
80_create_operations_agent.py   # OPTIONAL watch+act — Operations Agent   [enable_operations_agent]
```

> `40_build_thin_gold.py` is a **PySpark notebook** (run in a Fabric notebook on the workspace);
> the rest are Python REST scripts you run locally. The full narrated order — including
> Databricks/infra phases before this — is in
> [`docs/runbook-end-to-end.md`](../docs/runbook-end-to-end.md).

---

## How to run

```bash
# 1) Sign in (DefaultAzureCredential).
az login
az account set --subscription "<ME-MngEnvMCAP… subscription>"

# 2) Prepare config (copy + fill placeholders; never commit real values).
cp fabric/config/deploy_config.sample.json fabric/config/deploy_config.json
python scripts/config_schema.py --validate fabric/config/deploy_config.json \
    databricks/config/databricks_config.sample.json

# 3) Run the scripts in order (each is idempotent + honours its feature flag).
python fabric/scripts/00_create_workspace.py
python fabric/scripts/10_create_mirrored_catalog.py
# … and so on, in the run order above.
```

Each script resolves all identifiers from `deploy_config.json` (with CLI/env overrides) and is
**idempotent** (safe to re-run).

> **⛔ Cost gate.** The workspace runs on **F64** (~$11.52/hour while Active). Acknowledge
> [`docs/cost.md`](../docs/cost.md) before provisioning and **pause when idle**
> (`python scripts/pause_capacity.py`).

---

## Manual steps

Most of this folder is code-deployable. The few UI-only actions (e.g., the **optional**
Operations Agent / Teams + Activator wiring; confirming an Activator email arrives) are
consolidated in [`docs/manual-steps.md`](../docs/manual-steps.md). The **default** Activator
email path is minimal-manual by design.
