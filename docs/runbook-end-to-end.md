# Zava — End-to-End Runbook

This runbook captures cross-step operational procedures for the Zava Databricks + Fabric
demo. It is **narrated procedure**, not a click-list (consolidated manual UI steps live in
`docs/manual-steps.md`).

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
