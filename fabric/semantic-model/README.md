# Zava Fleet Analytics — Direct Lake semantic model (TMDL, as code)

This folder is the **source-of-truth definition** for the Zava **Direct Lake on OneLake**
semantic model, authored as **TMDL** so it is diff-friendly and deployable through Fabric Git
integration or the Fabric REST API. It is created / updated programmatically by
[`../scripts/30_create_semantic_model.py`](../scripts/30_create_semantic_model.py) (Step 14)
using `semantic-link-labs` (`sempy_labs`) + the TOM wrapper.

## What it binds to (Direct Lake on OneLake)

A single shared expression (`definition/expressions.tmdl`) connects to **one Fabric Lakehouse**
(the Step-10 workspace Lakehouse) via the `AzureStorage.DataLake` OneLake connector. All tables
are `mode: directLake` — **no DirectQuery fallback, no import**. The model reads:

| Schema | Source | Tables |
|---|---|---|
| `gold` | Zava certified gold (mirrored catalog / OneLake shortcut, Steps 11–12) | `dim_site`, `dim_vehicle`, `dim_customer`, `fact_rental` |
| `thin_gold` | Step-13 thin Fabric gold — **V-Ordered Delta** aggregates | `agg_revenue_by_site`, `agg_fleet_utilization_by_site_month`, `agg_idle_vehicles_by_site`, `agg_one_way_flows`, `agg_maintenance_cost_by_site` |

> The `__ZAVA_WORKSPACE_ID__` / `__ZAVA_LAKEHOUSE_ID__` tokens in `expressions.tmdl` and the
> `schemaName` values are substituted at deploy time from `deploy_config.json` / CLI flags. They
> are GUIDs / names — **never secrets**.

## Folder layout

```
semantic-model/
├── .platform                      # Fabric item metadata (Git integration)
├── definition.pbism               # Semantic model definition properties
└── definition/
    ├── database.tmdl              # compatibilityLevel: 1604
    ├── model.tmdl                 # culture, blue-Zava + AI-prep annotations, table/role refs
    ├── expressions.tmdl           # Direct Lake on OneLake shared connection (AzureStorage.DataLake)
    ├── relationships.tmdl         # Zava entity relationships (star schema)
    ├── roles/
    │   └── CityManager.tmdl       # demo RLS role (city-scoped)
    └── tables/
        ├── dim_site.tmdl          # Sites (conformed dim + Geography hierarchy + map data categories)
        ├── dim_vehicle.tmdl       # Vehicles (+ VehicleClass, Fleet hierarchy)
        ├── dim_customer.tmdl      # Customers (PII-like email/phone for governance demo)
        ├── fact_rental.tmdl       # Rentals (central fact; Reservations + Payments rolled in)
        ├── agg_revenue_by_site.tmdl
        ├── agg_fleet_utilization_by_site_month.tmdl
        ├── agg_idle_vehicles_by_site.tmdl
        ├── agg_one_way_flows.tmdl
        └── agg_maintenance_cost_by_site.tmdl
```

## Relationships (Zava entity model)

Sites is the central conformed dimension. Rentals (`fact_rental`) carries the Reservation link
(`reservation_id`), settled Payments revenue (`revenue_usd`), and the one-way flag, so the
Sites ↔ Vehicles ↔ Customers ↔ Reservations ↔ Rentals ↔ Payments ↔ Maintenance chain is fully
addressable. Each fact fans into `dim_site` with a many-to-one, single-direction relationship
(`crossFilteringBehavior: oneDirection`); `agg_one_way_flows.return_site_id` adds an **inactive**
relationship for `USERELATIONSHIP()` flow analysis.

## Measures (KPIs)

Revenue · Revenue per Rental · **Revenue per Site** · Fleet Utilization % · Idle Vehicle Count ·
Idle Vehicle % · One-Way Rentals / Trips · Maintenance Cost (labor + parts) · Active Vehicles ·
Total Miles Driven · Average Rental Duration.

## Security & AI

- **RLS** — `CityManager` role (demo, city-scoped). Role *members* are assigned via the Power BI
  REST API / XMLA at deploy time (not expressible in TMDL).
- **OLS** — customer `email` / `phone` are hidden from `CityManager` by the deploy script via TOM
  `set_ols` (the columns are annotated `Zava_PII` here).
- **Prep for AI** — model-level `Zava_AIPrep` annotation + table/column descriptions help the
  Fabric Data Agent (Step 17) answer natural-language questions accurately.

## Deploy

```bash
# Preview the model + every sempy_labs call without auth or mutation:
python fabric/scripts/30_create_semantic_model.py --dry-run

# Create/refresh the Direct Lake model from this TMDL + the gold/thin-gold tables:
python fabric/scripts/30_create_semantic_model.py
```
