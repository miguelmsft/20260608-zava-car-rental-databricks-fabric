# Zava Data Dictionary

Synthetic data only. All values are fictional; "PII-like" columns are clearly fake
(`@example.com` emails, `+1-555-...` phones) and exist solely for governance
(classification / DLP / column-mask) demos.

Machine-readable JSON Schemas live alongside this file (`*.schema.json`). Each schema
carries `x-primary-key`, `x-foreign-keys`, and (where relevant) `x-pii-like-columns`
and `x-alert-thresholds` annotations.

## Entities (batch — `generate_zava_data.py`)

| Entity | PK | Key foreign keys | Purpose |
|---|---|---|---|
| **VehicleClasses** | `vehicle_class_id` | — | Rate card (daily rates → revenue). |
| **Sites** | `site_id` | — | Locations (Seattle HQ + US cities), lat/long for maps. |
| **Vehicles** | `vehicle_id` | `vehicle_class_id`→VehicleClasses, `home_site_id`/`current_site_id`→Sites | The fleet; `status` drives idle KPI. |
| **Customers** | `customer_id` | — | Renters; synthetic PII-like `email`/`phone`. |
| **Reservations** | `reservation_id` | `customer_id`, `vehicle_class_id`, `pickup_site_id`, `return_site_id` | Bookings (intent). |
| **Rentals** | `rental_id` | `reservation_id`(nullable), `customer_id`, `vehicle_id`, `pickup_site_id`, `return_site_id` | Central fact; `is_one_way` flag. |
| **Payments** | `payment_id` | `rental_id`, `customer_id`, `pickup_site_id` | Revenue; `pickup_site_id` denormalized for revenue/site. |
| **Maintenance** | `maintenance_id` | `vehicle_id`, `site_id` | Service events; `total_cost_usd`. |
| **Telematics** | `telematics_id` | `vehicle_id`, `site_id` | Latest snapshot per vehicle (batch). |

## Live event (stream — `generate_telematics_stream.py`)

| Event | Event time | Foreign keys | Purpose |
|---|---|---|---|
| **Telematics Event** | `ts` | `vehicle_id`→Vehicles, `site_id`→Sites | NDJSON feed for Eventstream/Eventhouse; carries `is_spike` and alertable `idle_minutes` / `fault_code`. |

## KPI → source-column map

| KPI | Source columns |
|---|---|
| Fleet utilization | `Rentals.pickup_ts`/`return_ts`/`status`, `Vehicles.home_site_id`, `Sites.parking_capacity` |
| Revenue / site | `Payments.total_amount_usd` grouped by `Payments.pickup_site_id` (→Sites) |
| Idle vehicles | `Vehicles.status='idle'`, `Telematics.idle_minutes`, `TelematicsEvent.idle_minutes` |
| One-way flows | `Rentals.is_one_way`, `Rentals.pickup_site_id`→`Rentals.return_site_id` |
| Maintenance cost | `Maintenance.total_cost_usd` (= `labor_cost_usd` + `parts_cost_usd`) by `vehicle_id`/`site_id` |
