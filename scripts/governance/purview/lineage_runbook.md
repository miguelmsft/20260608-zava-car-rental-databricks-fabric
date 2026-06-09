# Zava — Purview End-to-End Lineage Verification Runbook (Step 22, research R6)

> **Scope.** This runbook verifies **Microsoft Purview** lineage for the exact Zava pipeline:
>
> ```
> Databricks UC gold  →  OneLake mirror (Fabric Lakehouse)  →  Direct Lake semantic model  →  report / Data Agent
> ```
>
> It tells you **what to scan**, **which Purview asset to open**, the **expected nodes/edges** at each
> stage, and — most importantly — gives an **honest account of the Databricks→Fabric lineage SEAM**:
> the part that does **NOT** auto-stitch in Purview (R6 §4). Run this **after** both scans complete
> (`setup_purview_scans.py` registers + triggers them).
>
> **Companion automation:** `scripts/governance/purview/setup_purview_scans.py`
> (registers the Databricks UC + Fabric sources, runs the scans, creates glossary terms, applies the
> PII sensitivity label).

---

## 0. Preconditions

Before lineage can appear, both scans must succeed. Confirm:

| Prereq | How / where | Source |
|---|---|---|
| Purview account exists | Provisioned via Bicep (`Microsoft.Purview/accounts`), name in `governance.purview_account` | R6 §6.1 |
| Databricks UC source registered + scanned, **lineage extraction ON** | `setup_purview_scans.py --only scans` | R6 §6.3 |
| `system.access` schema enabled + grants on `system.access.table_lineage` / `column_lineage` | **Databricks UI/CLI (manual)** | R6 §2 |
| A **SQL Warehouse** running in the Databricks workspace | **Databricks (manual)** | R6 §2 |
| Fabric tenant source registered + scanned | `setup_purview_scans.py --only scans` | R6 §6.3 |
| Fabric tenant settings: SP read-only admin APIs + OneLake external apps | **Fabric Admin Portal (manual, UI-only)** | R6 §3, §6.6 |

> The **manual** rows above are UI/PowerShell-only and are consolidated in the manual-steps appendix
> (Step 25). They are **prerequisites** for the lineage you will verify below.

---

## 1. The Zava chain & where lineage is captured

| # | Stage | Captured by | Lineage mechanism (R6) |
|---|---|---|---|
| 1 | **Databricks UC gold** (`zava.gold.*`, e.g. `customer_rentals`) | **Databricks UC scan** (lineage ON) | UC notebook + `system.access` lineage — UC-**internal** only (R6 §4.2 Stage 1) |
| 2 | **OneLake mirror** (Fabric Lakehouse over the mirrored Databricks catalog) | **Fabric tenant scan** / live view | Item-level metadata; **no upstream** to Databricks (R6 §3, §4.3) |
| 3 | **SQL analytics endpoint** (auto-generated over the Lakehouse) | Fabric tenant scan | Fabric-internal edge from Lakehouse (R6 §4.5 Step 2) |
| 4 | **Direct Lake semantic model** | Fabric tenant scan (Power BI lineage) | Fabric/Power BI lineage edge from SQL endpoint (R6 §4.5 Step 2) |
| 5 | **Report** | Fabric tenant scan (Power BI lineage) | Semantic model → Report edge (R6 §4.2 Stage 3) |
| 6 | **Data Agent** | — | **Not a documented lineage-visible item type** (R6 §4.2 Stage 4) |

---

## 2. Stage 1 — Verify **Databricks UC** lineage (UC-internal)

1. **What to scan:** the **Azure Databricks Unity Catalog** source containing the `zava` metastore
   catalog, with **lineage extraction toggled ON** and the SQL Warehouse running (R6 §4.5 Step 1).
2. **Which Purview asset to open:** search for `zava.gold.customer_rentals`, **or** browse:
   `Unified Catalog → Browse → Azure Databricks Unity Catalog → <source> → zava → gold → customer_rentals`
   then open the **Lineage** tab (R6 §4.4).
3. **Expected lineage graph:**
   - **Upstream:** Databricks **notebook** nodes (shown as **numeric IDs**, not readable names —
     Databricks doesn't expose notebook names in UC system tables) connecting **silver** → **gold**
     tables (R6 §2, §4.5 Step 1).
   - **Downstream:** **nothing** — Databricks UC lineage **ends at the UC boundary** (R6 §4.5 Step 1).
4. **Column-level lineage:** toggle **"Switch to column-level lineage."** Expect silver→gold column
   mappings **only if notebooks were run interactively** — column lineage **may be missing when
   notebooks ran via Databricks jobs** (R6 §2 limitations, §4.5 Step 1).

**Known Databricks UC lineage limitations (R6 §2):** external-table lineage unsupported; deleted
source objects aren't auto-removed on rescan; partial lineage if not all objects are scanned.

---

## 3. Stage 2–5 — Verify **Fabric-internal** lineage

1. **What to scan:** the **Fabric tenant** source (admin-API access + OneLake external-app access
   enabled — R6 §4.5 Step 2).
2. **Which Purview asset to open:** search for the **Fabric Lakehouse** (the mirrored Databricks
   catalog) or the **semantic model**, then open the **Lineage** tab (R6 §4.4).
3. **Expected lineage — Lakehouse (the mirror):**
   - **Upstream:** **NONE.** The mirrored Databricks catalog will **NOT** appear as an upstream node.
     ⚠️ **This is the confirmed cross-system seam — see §4** (R6 §4.5 Step 2).
   - **Downstream:** **SQL analytics endpoint → Semantic model → Report(s)**.
4. **Expected lineage — Semantic model:** Upstream = Lakehouse / SQL analytics endpoint;
   Downstream = Reports, Dashboards (R6 §4.5 Step 2).
5. **Expected lineage — Report:** Upstream = Semantic model; Downstream = Dashboard (if pinned).

> **Note on depth (R6 §3):** For non-Power BI Fabric items, the scan captures **item-level** metadata
> and lineage. Lakehouse **table/file sub-item metadata** is in **preview**; sub-item **lineage is not
> supported**. Live view gives basic item discovery automatically but **no** sub-item schema/lineage.

---

## 4. ⚠️ The Databricks→Fabric SEAM — what will and won't auto-stitch (HONEST NOTE, R6 §4.3)

**The single most important fact in this runbook:** Purview does **NOT** automatically stitch
"Databricks UC table → Fabric Lakehouse (mirrored)" into one lineage graph. The two graphs —
Databricks-internal and Fabric-internal — remain **disconnected** in the Data Map. This is a
**verified, documented gap**, not a misconfiguration you can fix by re-scanning (R6 §4.3).

**Why (R6 §4.3):**
1. The **Databricks UC scan** captures **UC-internal** lineage only (notebooks linking UC tables).
2. The **Fabric tenant scan** captures **Fabric-internal** lineage only (Lakehouse → semantic model → report).
3. Microsoft's Fabric lineage docs state: *"For non-Power BI Fabric items, external data sources as
   upstream sources in lineage aren't yet supported."* So the mirrored Lakehouse will **not** show
   the Databricks UC table as an upstream source.

### What stitches vs. what doesn't (R6 §4.5 Step 3)

| Expected connection | Stitches in Purview? | Reason |
|---|:---:|---|
| Databricks UC gold table → Fabric Lakehouse (mirror) | ❌ **NO** | External upstream not supported for non-Power BI items — **the seam** |
| Fabric Lakehouse → SQL analytics endpoint | ✅ Yes | Both Fabric-internal |
| SQL analytics endpoint → Direct Lake semantic model | ✅ Yes | Power BI lineage from Fabric scan |
| Semantic model → Report | ✅ Yes | Standard Power BI lineage |
| Report → **Data Agent** | ❌ **NO** | Data Agent is **not a documented lineage-visible item type** (R6 §4.2 Stage 4) |

> **Two seams, not one.** Besides the Databricks→Fabric seam, the **Report → Data Agent** edge also
> does **not** appear: as of June 2026 there is no Microsoft documentation that Fabric Data Agent
> assets surface in the Data Map or produce lineage edges (R6 §4.2 Stage 4). Narrate the Data Agent
> as the consumption endpoint, but do **not** claim Purview shows its lineage.

### How to bridge the seam for the demo (documentation-based — R6 §4.3, §4.5 Step 4)

Automatic stitching is unavailable, so make the relationship **human-navigable**:

1. **Annotate the Fabric Lakehouse asset** in Purview's Unified Catalog with a description such as:
   > *"Source: Mirrored from Azure Databricks Unity Catalog metastore, catalog `zava`, schemas
   > `bronze`, `silver`, `gold`. See the Databricks UC source in this Data Map for upstream lineage."*
2. **Use glossary terms + the data product** (created by `setup_purview_scans.py` /
   the governance domain) to describe the Databricks→Fabric relationship **textually** — e.g. the
   *"Certified Rental Gold Asset"* term spans both the UC gold asset and its OneLake mirror.
3. **Monitor the roadmap** for cross-system lineage support for mirrored/shortcut sources.

These create a navigable cross-reference while automatic lineage stitching is unavailable — and they
let the demo tell an **honest** end-to-end governance story instead of implying a stitch that Purview
does not produce.

---

## 5. Verification checklist (maps to Step 22 verification criteria)

- [ ] **Catalog:** Databricks UC assets (`zava.gold.*`) **and** Fabric items (Lakehouse, semantic
      model, report) appear in the Unified Catalog (live view + scan) (R6 §3).
- [ ] **Databricks lineage:** `zava.gold.customer_rentals` Lineage tab shows **upstream notebook
      nodes (numeric IDs) from silver→gold** and **no downstream** beyond the UC boundary (R6 §4.5 Step 1).
- [ ] **Fabric lineage:** Lakehouse Lineage tab shows **downstream** SQL endpoint → semantic model →
      report, and **NO upstream Databricks node** (the seam) (R6 §4.5 Step 2).
- [ ] **Label:** the **"Highly Confidential \ PII"** sensitivity label is visible on the labeled
      semantic model / report (applied via SetLabelsAsAdmin) (R6 §6.4).
- [ ] **Classification:** PII columns on the UC gold table show **system classifications**
      (auto-applied during the UC scan — e.g. Person Name, Email, US SSN, Credit Card) (R6 §5).
- [ ] **Seam documented:** the Fabric Lakehouse asset carries the **Databricks-origin description**
      and the runbook's seam note is acknowledged (R6 §4.5 Step 4).
- [ ] **DLP** (manual, PowerShell/UI): a **custom** Fabric DLP policy for SSN / Credit Card has been
      created and evaluated — *not* automated here (R6 §5, §6.6).

---

## 6. Honest summary for the demo narrative

- ✅ **Within Databricks:** Purview shows silver→gold lineage through notebooks (column-level when
  run interactively).
- ✅ **Within Fabric/Power BI:** Purview shows Lakehouse → SQL endpoint → Direct Lake semantic model
  → report lineage.
- ❌ **Across the boundary:** Purview does **NOT** auto-connect the Databricks UC gold table to its
  OneLake mirror, and does **NOT** show the Data Agent as a lineage node. Bridge both seams with
  descriptions, glossary terms, and the data product — and **say so** in the demo (R6 §4.3, §4.5).

> Sources: `research/2026-06-08-r6-purview-governance.md` §2 (Databricks UC connector + limitations),
> §3 (Fabric connector + live view), §4 (end-to-end lineage, the verified gap, and the verification
> runbook), §5 (classification, labels, DLP), §6 (programmatic coverage + endpoints).
