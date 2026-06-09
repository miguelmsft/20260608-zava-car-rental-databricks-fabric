# Zava Car Rental — Accelerating a Databricks Data Foundation with Microsoft Fabric

A **public, reusable demo** that shows how **Azure Databricks** and **Microsoft Fabric** integrate to form a modern, governed data foundation — with **Microsoft Purview** providing end-to-end governance. Clone it, point it at your own Azure subscription, and redeploy the whole story programmatically.

> *Zava is a **fictional** company. Every dataset in this repository is **synthetic**, generated for demonstration purposes only — no real customers, vehicles, or transactions.*

---

## The scenario

**Zava** is a large car-rental company headquartered in **Seattle, WA**, with rental sites across multiple US cities. Zava's **data engineering team** builds its data foundation on **Azure Databricks**; its **business users** live in **Power BI**.

Zava wants to know one thing: **how can Microsoft Fabric accelerate their governed data foundation — without ripping out Databricks?** This demo answers that end-to-end, taking a **certified gold data asset** produced in Databricks Unity Catalog and turning it into **governed, near-real-time business insight** in Fabric, Power BI, and an agentic semantic layer.

---

## What this demo shows — the four pillars (equal weight)

| Pillar | What it delivers |
| --- | --- |
| **A — No-copy mirroring** | Mirror Databricks **Unity Catalog** Delta tables into Fabric **OneLake** with **zero ETL** and no data duplication. |
| **B — Direct Lake reporting** | A **Power BI Direct Lake** report built directly on the mirrored gold data — interactive, near-real-time, no import refresh. |
| **C — Fabric IQ ontology + agents** | A **Fabric IQ ontology** + **Graph** over Zava's business entities, surfaced through a **Data Agent** (and an optional **Operations Agent**) for semantic, agentic, natural-language insight beyond Power BI. |
| **D — Governance** | **Policy Weaver** syncs Unity Catalog access policies to OneLake; **Microsoft Purview** delivers deep end-to-end lineage, cataloging, classification, and DLP. |

The Databricks medallion pipeline (raw → bronze → silver → gold → **certified data asset**) is intentionally **simple** — it's the setup, not the star. The **integration and Fabric side are the focus**.

### Two ingestion variations for Pillar A

Fabric can pull Databricks data into OneLake two ways, and this demo deploys **both** so you can see when each applies:

1. **Mirroring** — for standard **Unity Catalog-managed Delta tables**, use the native **Mirrored Azure Databricks Catalog** item. Near-real-time metadata sync, zero copy, zero ETL.
2. **OneLake shortcut** — for tables where mirroring isn't yet available (e.g. **Spark Declarative Pipelines / Lakeflow** outputs), create a **OneLake shortcut** directly to the Databricks-managed **ADLS Gen2** storage. The shortcut is secured per Microsoft's **Fabric → ADLS network-access guidance** (trusted-workspace access / hardened storage networking).

---

## Business value (the "why")

It's about the **value** customers get, not the tools — the tools are just the medium.

- **Agility** — faster insights from data; the path from certified asset to business answer collapses from days to minutes.
- **Observability** — see and track your data pipelines; automate environment provisioning and reporting.
- **Efficiency** — **one governed copy** of data instead of a spaghetti of access grants and duplicated extracts.
- **Data Governance** — consistent, end-to-end security, lineage, and policy from Databricks through Fabric and Power BI.

---

## Prerequisites

Read **[`docs/prerequisites.md`](docs/prerequisites.md)** and the infra guide in **[`infra/README.md`](infra/README.md)** before deploying. In short, you will need:

- An **Azure subscription** with rights to create resource groups and assign roles.
- A **Microsoft Fabric F64 capacity** in **East US 2** (the finalized demo region — see **[`docs/cost.md`](docs/cost.md)** and the region rationale in **[`docs/architecture.md`](docs/architecture.md)**).
- An **Azure Databricks workspace** — either **provisioned fresh** by this repo's Bicep, or an **existing** workspace you already operate.
- **Service principals / admin rights** for Azure, Fabric, and Databricks (Power BI / Fabric admin consent is required for a few documented steps).

> ⚠️ Review the **[cost checkpoint](docs/cost.md)** before you deploy — an F64 capacity is a real, billable resource. Pause or tear down when you're done (see Quick-start below).

---

## Quick-start (fresh vs. existing)

Everything is driven by a single wave-ordered orchestrator, **[`scripts/deploy.py`](scripts/deploy.py)**, reading two JSON configs.

### 1. Configure

Copy the committed samples and fill in your own values (no secrets — names, ids, and placeholders only):

```bash
cp fabric/config/deploy_config.sample.json        fabric/config/deploy_config.json
cp databricks/config/databricks_config.sample.json databricks/config/databricks_config.json
# edit both with your subscription, region, capacity, and workspace details
```

### 2. Preflight

Run read-only preflight checks (**[`scripts/preflight_checks.py`](scripts/preflight_checks.py)**) to surface gaps before any change is made:

```bash
python scripts/preflight_checks.py \
    --config fabric/config/deploy_config.json \
    --databricks-config databricks/config/databricks_config.json
# add --strict to promote every warning to a failure (CI gate)
```

### 3. Preview, then deploy

`deploy.py` is **idempotent** and honours **fresh-vs-existing** flags and optional **feature gates**. Always dry-run first — it performs **no auth and no changes**, printing the full ordered plan and every command:

```bash
# Preview the full ordered plan (safe — no auth, no mutation):
python scripts/deploy.py --dry-run

# Deploy everything, pausing at documented manual steps:
python scripts/deploy.py \
    --config fabric/config/deploy_config.json \
    --databricks-config databricks/config/databricks_config.json
```

**Fresh vs. existing** — skip the waves you don't need:

```bash
# Use an EXISTING Databricks workspace (skip the Databricks build):
python scripts/deploy.py --skip-databricks --config fabric/config/deploy_config.json \
    --databricks-config databricks/config/databricks_config.json

# Use EXISTING Azure infra / capacity (skip the Bicep waves):
python scripts/deploy.py --skip-azure ...

# Already ran preflight? Skip it:
python scripts/deploy.py --skip-preflight ...

# Resume at a specific wave after a manual step (e.g. the semantic model):
python scripts/deploy.py --start-at semantic_model ...

# Run unattended in CI (auto-acknowledge manual PAUSE prompts):
python scripts/deploy.py --non-interactive ...
```

Other useful flags: `--skip <wave_key>` (repeatable), `--databricks-target dev|prod`, `--resource-group`, `--subscription`, `--bicep-params`, `--verbose`.

### 4. Tear down (or pause)

When you're finished, clean up with **[`scripts/teardown.py`](scripts/teardown.py)** (dry-run first):

```bash
python scripts/teardown.py --dry-run --config fabric/config/deploy_config.json
python scripts/teardown.py --yes --config fabric/config/deploy_config.json
# optional: --delete-capacity --delete-resource-group to remove the F64 capacity / RG too
```

> The full, narrated walkthrough — including every manual UI step — lives in **[`docs/runbook-end-to-end.md`](docs/runbook-end-to-end.md)** and **[`docs/manual-steps.md`](docs/manual-steps.md)**.

---

## Repository layout

| Path | Purpose |
| --- | --- |
| **[`infra/`](infra/README.md)** | Azure infrastructure as **Bicep** — resource group, Fabric F64 capacity, Databricks workspace, storage, networking. |
| **[`databricks/`](databricks/README.md)** | **Unity Catalog** medallion pipeline + **Lakeflow / Spark Declarative Pipeline**, as code (Asset Bundles + notebooks). |
| **[`fabric/`](fabric/README.md)** | Microsoft **Fabric** items + deployment scripts — mirroring, shortcut, Direct Lake model, report, ontology, agents, governance. |
| **[`data/`](data/README.md)** | **Synthetic** Zava data generators (Sites, Vehicles, Customers, Reservations, Rentals, Payments, Maintenance, Telematics). |
| **[`scripts/`](scripts/deploy.py)** | The deployment orchestrator (`deploy.py`), `preflight_checks.py`, `teardown.py`, and capacity pause/resume helpers. |
| **[`docs/`](docs/architecture.md)** | Architecture, prerequisites, cost, end-to-end runbook, and the consolidated Manual Steps Appendix. |

---

## Documentation index

- **[`docs/architecture.md`](docs/architecture.md)** — architecture diagram, value framing, and the East US 2 region rationale.
- **[`docs/prerequisites.md`](docs/prerequisites.md)** — everything you need before you deploy.
- **[`docs/cost.md`](docs/cost.md)** — cost checkpoint to read and acknowledge **before** deploying.
- **[`docs/runbook-end-to-end.md`](docs/runbook-end-to-end.md)** — the full, scripted demo walkthrough.
- **[`docs/manual-steps.md`](docs/manual-steps.md)** — the consolidated Manual Steps Appendix (every UI-only step in one place).
- Component guides: **[`infra/README.md`](infra/README.md)** · **[`databricks/README.md`](databricks/README.md)** · **[`fabric/README.md`](fabric/README.md)** · **[`data/README.md`](data/README.md)**.

---

*Zava is a fictional company used for demonstration purposes only. All data in this repository is synthetic — it contains no real personal, financial, or operational information.*
