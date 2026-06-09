# `databricks/` — Unity Catalog medallion + Lakeflow pipeline (as code)

This folder is the **Azure Databricks** side of the demo: a deliberately **simple** medallion
pipeline (raw → bronze → silver → **gold** → *certified data asset*) plus one **Lakeflow**
pipeline that produces the curated source for ingestion **Variation 2**. Everything is packaged
as a **Databricks Asset Bundle** so a customer can redeploy it as code.

> The medallion is **the setup, not the star** — it exists to produce a clean, governed,
> certified gold asset that the **Fabric** side then mirrors, reports on, and reasons over.
>
> **Public repo — NO secrets.** Host URLs are placeholders; auth is via `databricks auth`
> (OAuth) / service principal / managed identity acquired at runtime.

---

## Layout

| Path | What it is |
|---|---|
| `bundle/databricks.yml` | The **Databricks Asset Bundle** — jobs, the Lakeflow pipeline, dev/prod targets, and variables. The single deployable unit. |
| `notebooks/00_generate_synthetic_data.py` | Lands the Step-3 synthetic Zava entities into the **raw** schema (imports the `data/` generators synced into the bundle). |
| `notebooks/10_bronze.py` | Raw → **bronze** (auditable ingest copy). |
| `notebooks/20_silver.py` | Bronze → **silver** (cleaned / typed / conformed). |
| `notebooks/30_gold.py` | Silver → **gold**: conformed dimensions, a central `fact_rental`, and four KPI tables (revenue, fleet utilization, one-way flows, maintenance cost). Managed Delta → **mirrorable into Fabric**. |
| `uc/01_metastore_external_access.sql` | Documents/verifies the **metastore prerequisite**: enable *External data access* so Fabric mirroring can read UC Delta (manual/REST — see below). |
| `uc/02_catalog_schema.sql` | Creates the `zava` catalog + `raw`/`bronze`/`silver`/`gold` schemas. |
| `uc/03_grants_mirroring.sql` | Grants for the data-engineering identity **and** the Fabric connector identity to mirror gold (Variation 1). |
| `uc/04_certify_gold.sql` | **Certifies** the gold asset via UC tags + comments + ownership (UC has no first-class "certified" flag). |
| `uc/05_access_policies.sql` | The **row filter** (site managers see only their city) + **column mask** (customer email PII) that Policy Weaver later syncs into Fabric (Step 21). |
| `pipelines/lakeflow_sdp.py` | The **Lakeflow** Spark Declarative Pipeline — produces curated UC **managed** outputs (a `rentals_curated` materialized view + a `telematics_curated` streaming table). The **Variation-2 source**. |
| `pipelines/lakeflow_sink_external.py` | Optional **sub-pattern 2B**: sink the curated output to an **owned external location** for a stable shortcut path and cleaner governance boundary. |
| `config/databricks_config.sample.json` | Config template (workspace, catalog, managed storage, seed). Copy to a gitignored `databricks_config.json` and fill placeholders. |

---

## The simple medallion (raw → bronze → silver → gold → certified)

```
data/ synthetic entities ─► raw ─► bronze ─► silver ─► gold ─► CERTIFIED asset
  (Step 3 generators)        │       │         │         │        (tags + comment
                             │       │         │         │         + ownership)
                          land    audit     clean     conformed dims,
                          copy    copy    /typed   fact_rental, KPI tables
                                                        └──────────► mirrored into Fabric
                                                                     OneLake (Variation 1)
```

The **gold** tables are standard **managed Delta** — the only object type Fabric *Mirrored
Azure Databricks Catalog* supports (streaming tables, materialized views, and views are **not**
mirrorable, which is exactly why the Lakeflow curated outputs use Variation 2 instead).

---

## The Lakeflow pipeline (Variation-2 source)

`pipelines/lakeflow_sdp.py` builds curated UC **managed** objects from the silver layer:

- `<catalog>.curated.rentals_curated` — a **materialized view** (rentals + `site_city`, used by
  the row-filter access policy).
- `<catalog>.curated.telematics_curated` — a **streaming table** (vehicle telemetry).

Because these are streaming tables / materialized views, they are **excluded from Fabric
mirroring** (R10 §2.3). Fabric reaches them via a **OneLake shortcut** onto the
Databricks-managed ADLS Gen2 storage that backs the table (**sub-pattern 2A**). The concrete
`abfss://` path is discovered at run time — see the **path-discovery** procedure in
[`docs/runbook-end-to-end.md`](../docs/runbook-end-to-end.md). For a stable, owned path use
`pipelines/lakeflow_sink_external.py` (**sub-pattern 2B**) instead.

> ⚠️ **Governance seam (Variation 2).** A direct-storage shortcut **bypasses Unity Catalog
> enforcement at the storage layer** — the row filter / column mask in `05_access_policies.sql`
> do **not** follow the data through a 2A shortcut. Re-enforce in Fabric (OneLake security +
> storage RBAC). Full detail is in the runbook.

---

## UC access policies (governance source)

`uc/05_access_policies.sql` authors the two policies that prove the governance pillar:

- **Row filter** — a Zava **site manager** sees only their city's rentals (on
  `curated.rentals_curated.site_city`).
- **Column mask** — customer **email** PII is masked for non-authorized principals (on
  `gold.dim_customer.email`).

Both use **Policy-Weaver-compatible** patterns (`IS_ACCOUNT_GROUP_MEMBER(...)` → literal),
so `scripts/governance/policy-weaver/run_policy_weaver.py` (Step 21) can faithfully sync them
into **Fabric OneLake Security** for the mirrored (Variation 1) data. Replace the account-group
placeholders (`zava_seattle_mgr`, `zava_pii_authorized`, …) with the groups provisioned in your
tenant before running.

---

## How to run

**Prerequisites**

1. The [`infra/`](../infra/README.md) deployment created (or you supplied) a **Premium**
   Databricks workspace + Access Connector + ADLS Gen2 with Unity Catalog.
2. Copy `config/databricks_config.sample.json` → `config/databricks_config.json` (gitignored)
   and fill placeholders (`host_url`, `resource_id`, `access_connector_id`).
3. **Manual/REST prerequisite:** a metastore admin enables **External data access** on the
   metastore (see `uc/01_metastore_external_access.sql`) so Fabric mirroring can read UC Delta.
4. Replace the **group/principal placeholders** in `uc/03_grants_mirroring.sql`,
   `uc/04_certify_gold.sql`, and `uc/05_access_policies.sql` with real account groups/principals.
5. Set `warehouse_id` (and the dev/prod `host`) in `bundle/databricks.yml` for your workspace.

**Deploy + run the bundle**

```bash
# from databricks/bundle/
databricks bundle validate                       # static validation
databricks bundle deploy                 [-t prod]   # deploy jobs + pipeline to a target

databricks bundle run zava_uc_setup      [-t prod]   # 1) create catalog/schemas + mirroring grants (once)
databricks bundle run zava_medallion_pipeline        # 2) 00 -> 10 -> 20 -> 30  (build gold)
databricks bundle run zava_certify_gold              # 3) tag/comment/own the certified gold asset
databricks bundle run zava_lakeflow_curated          # 4) build the curated Variation-2 source
```

Then apply the access policies and (later) sync them to Fabric:

```bash
# Run uc/05_access_policies.sql as the OWNER of the target tables (SQL editor or a SQL task).
```

> **Targets:** `dev` (default, `mode: development`, catalog `zava_dev`) and `prod`
> (`mode: production`, runs as a service principal, catalog `zava`). Pick with `-t`.

---

## Manual steps

The only Databricks UI/REST-only action is **enabling metastore External data access**
(`uc/01_metastore_external_access.sql`). It — and every other manual action in the demo — is
consolidated in [`docs/manual-steps.md`](../docs/manual-steps.md).
