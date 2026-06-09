# Zava Synthetic Data Generators

Phase 0 (local only) data foundation for the **Zava Car Rental — Databricks + Fabric**
demo. Two generators produce **100% synthetic** data:

1. **`generate_zava_data.py`** — deterministic **batch** relational dataset (9 entities)
   written to `data/output/` as CSV **and** Parquet. This feeds the Databricks medallion
   pipeline (raw → bronze → silver → gold → certified asset) and the Power BI report.
2. **`generate_telematics_stream.py`** — a **live telematics event feed** (NDJSON) for
   Microsoft Fabric **Real-Time Intelligence** (Eventstream → Eventhouse/KQL), including an
   **injectable spike window** that drives the Activator / Operations-Agent watch+act demo.

> **No cloud calls. No authentication. No secrets.** Everything runs locally with
> `pandas` / `numpy` / `pyarrow`. All people/plates/VINs are fabricated; the only
> "PII-like" fields (`Customers.email`, `Customers.phone`) are obviously fake
> (`@example.com`, `+1-555-…`) and exist purely for Purview classification / DLP /
> column-mask demos.

---

## Quick start

```bash
pip install -r data/requirements.txt

# 1) Batch entities -> data/output/ (CSV + Parquet)
python data/generate_zava_data.py --out ./data/output

# 2) Telematics stream (NDJSON) using the batch fleet for FK-consistent IDs
python data/generate_telematics_stream.py --batch-dir ./data/output --count 200 \
    --inject-spike --out ./data/output/telematics_stream.ndjson
```

### Determinism / seed

Both generators are deterministic. The seed resolves in this order:

1. `--seed <int>` if provided,
2. else `databricks_config.data_seed` (from `databricks/config/databricks_config*.json`, Step 1) if present,
3. else **`42`**.

Two runs with the same seed and parameters produce **byte-identical** output.

---

## `generate_zava_data.py`

Generates the nine entities **VehicleClasses, Sites, Vehicles, Customers, Reservations,
Rentals, Payments, Maintenance, Telematics** and a `_manifest.json` (seed, row counts,
columns, one-way count).

| Flag | Default | Meaning |
|---|---|---|
| `--out` | `./data/output` | Output directory. |
| `--seed` | resolved (see above) | Deterministic seed. |
| `--format` | `both` | `csv`, `parquet`, or `both`. |
| `--sites` | `18` | Number of Sites (Seattle HQ first, then US cities; extra sites reuse cities as Downtown/Airport/…). |
| `--vehicles` | `600` | Fleet size. |
| `--customers` | `1500` | Customer count. |
| `--reservations` | `4000` | Reservation count (fulfilled ones become Rentals; ~10% walk-ins added). |
| `--days` | `365` | History window length (ends 2025-01-01). |

The script runs **inline referential-integrity assertions** after writing and fails
fast if any FK does not resolve or if no one-way rentals exist.

### Output files

`vehicle_classes`, `sites`, `vehicles`, `customers`, `reservations`, `rentals`,
`payments`, `maintenance`, `telematics` — each as `.csv` and/or `.parquet`, plus
`_manifest.json`.

---

## Entity model (ERD)

```
VehicleClasses ──┐
                 ├─< Vehicles >── home_site_id / current_site_id ──┐
Sites ───────────┤                                                 │
   ▲   ▲   ▲     ├─< Reservations >── pickup_site_id/return_site_id │
   │   │   │     │        │                                         │
   │   │   │     │        └──(fulfilled)──┐                         │
   │   │   │     │                        ▼                         │
   │   │   └─────┴──< Rentals >── vehicle_id ──────────────────────┘
   │   │                │   │
   │   │                │   └─< Payments >  (pickup_site_id denormalized)
   │   └────────────────┴─< Maintenance >── vehicle_id / site_id
   └────────────────────────< Telematics >── vehicle_id / site_id

Customers ──< Reservations, Rentals, Payments
```

Relationships (FK → PK):

- `Vehicles.vehicle_class_id` → `VehicleClasses.vehicle_class_id`
- `Vehicles.home_site_id`, `Vehicles.current_site_id` → `Sites.site_id`
- `Reservations.{customer_id, vehicle_class_id, pickup_site_id, return_site_id}` → their parents
- `Rentals.{customer_id, vehicle_id, vehicle_class_id, pickup_site_id, return_site_id}` → parents;
  `Rentals.reservation_id` → `Reservations` (nullable for walk-ins)
- `Payments.{rental_id, customer_id, pickup_site_id}` → parents
- `Maintenance.{vehicle_id, site_id}` → parents
- `Telematics.{vehicle_id, site_id}` → parents

Machine-readable schemas: [`schema/*.schema.json`](./schema) and the
[data dictionary](./schema/data_dictionary.md).

---

## How the data maps to the demo KPIs

| KPI (report / ontology / agent) | How it is computed | Source columns |
|---|---|---|
| **Fleet utilization** | rented vehicle-days ÷ available vehicle-days per site/period | `Rentals.pickup_ts/return_ts/status`, `Vehicles.home_site_id`, `Sites.parking_capacity` |
| **Revenue / site** | Σ `total_amount_usd` grouped by site | `Payments.total_amount_usd`, `Payments.pickup_site_id` → `Sites` |
| **Idle vehicles** | vehicles with high idle time / `status='idle'` | `Vehicles.status`, `Telematics.idle_minutes`, `TelematicsEvent.idle_minutes` |
| **One-way flows** | pickup-site → return-site movement matrix | `Rentals.is_one_way`, `Rentals.pickup_site_id`, `Rentals.return_site_id` |
| **Maintenance cost** | Σ `total_cost_usd` by vehicle/site | `Maintenance.total_cost_usd` (`labor_cost_usd` + `parts_cost_usd`) |

`Customers.email` / `Customers.phone` are the **governance-demo** columns (label, DLP,
column-mask). `Sites.latitude/longitude` power the **multi-city map** wow-visual.

---

## `generate_telematics_stream.py`

Emits one JSON telematics event per line (**NDJSON**) to stdout or a file — the shape a
Fabric **Eventstream** custom endpoint / sample feed expects, landing in an
**Eventhouse (KQL)** table for Real-Time Intelligence (plan Step 18). Event schema:
[`schema/telematics_event.schema.json`](./schema/telematics_event.schema.json).

By default it reads `--batch-dir` (`vehicles.csv` + `sites.csv`) so the stream's
`vehicle_id`/`site_id` resolve against the batch entities. If those files are absent it
falls back to a synthetic fleet (still deterministic) for stream-only smoke tests.

| Flag | Default | Meaning |
|---|---|---|
| `--batch-dir` | `./data/output` | Source of FK-consistent vehicle/site IDs. |
| `--count` | `200` | Total events to emit. |
| `--rate` | `0` | Events/sec for live pacing (0 = as fast as possible). |
| `--seed` | `42` | Deterministic content seed. |
| `--out` | `-` | NDJSON file path, or `-` for stdout. |
| `--start-ts` | now (UTC) | ISO start for `ts`; set fixed for reproducible files. |
| `--ts-step-sec` | `1.0` | Seconds the event clock advances per event. |
| `--fallback-vehicles` | `50` | Synthetic fleet size when batch output is missing. |
| `--inject-spike` | off | Turn on the bounded spike window. |
| `--spike-start` | ~40% in | Event index where the spike begins. |
| `--spike-duration` | ~25% of count | Number of events the spike lasts. |
| `--spike-type` | `both` | `idle`, `fault`, or `both`. |
| `--spike-fraction` | `0.5` | Fraction of in-window events that are elevated. |

### The spike window (watch + act demo — Steps 19–20)

The feed is normally **calm**: `idle_minutes` in 0–45 and `fault_code` almost always
`null`. With `--inject-spike`, a **bounded window** `[spike_start, spike_start+spike_duration)`
injects a clearly elevated condition that downstream rules/agents alert on:

- `--spike-type idle` → `idle_minutes` jumps **far above 120** (the alert threshold) →
  **idle-vehicle / under-utilized-asset** alert.
- `--spike-type fault` → `fault_code` is populated (OBD-II-like) →
  **overdue-maintenance / fault** alert.
- `--spike-type both` (default) → both conditions.

Every elevated event carries `"is_spike": true`, so a simple count over the interval
**verifies the elevation**. The alert threshold is encoded as
`x-alert-thresholds.idle_minutes_alert = 120` in the event schema; the Fabric
**Activator** rule (Step 19, default, Teams-free email) and the optional **Operations
Agent** (Step 20, Teams) watch for `idle_minutes > 120` and/or a non-null `fault_code`
and act.

Example:

```bash
# 200 events, a 40-event idle+fault spike starting at event 80, reproducible timestamps
python data/generate_telematics_stream.py --batch-dir ./data/output --count 200 \
    --inject-spike --spike-start 80 --spike-duration 40 \
    --start-ts 2025-01-01T00:00:00Z --out ./data/output/telematics_stream.ndjson

# Verify the elevated window:
python -c "import json; r=[json.loads(l) for l in open('./data/output/telematics_stream.ndjson')]; \
s=[e for e in r if e['is_spike']]; print('elevated', len(s), \
'idle>120', sum(e['idle_minutes']>120 for e in s), \
'with fault', sum(bool(e['fault_code']) for e in s))"
```

---

## Notes

- Public repo: keep all data synthetic; never add real PII or secrets.
- Zava branding is **blue tones** — relevant to the report theme (Step 15), not the data.
- `data/output/` and other generated artifacts are not required to be committed; treat
  them as build output.
