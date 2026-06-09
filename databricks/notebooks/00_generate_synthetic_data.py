# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Load synthetic Zava data into the RAW layer
# MAGIC
# MAGIC Loads the **Step-3 synthetic data generator** (`data/generate_zava_data.py`) directly
# MAGIC into Unity Catalog **managed Delta** tables under `zava.raw.*`. This is the source of
# MAGIC the medallion: `raw → bronze → silver → gold → certified asset`.
# MAGIC
# MAGIC - **Deterministic** — same `seed` (default 42, matching `databricks_config.data_seed`)
# MAGIC   yields byte-identical entities (R3 generator contract).
# MAGIC - **Referentially consistent** — every FK resolves; the notebook re-runs the
# MAGIC   generator's fail-fast integrity assertions before writing.
# MAGIC - **Idempotent** — each table is written with `overwrite`, so re-running the bundle
# MAGIC   produces no duplicates.
# MAGIC
# MAGIC > The generator runs locally with `pandas`/`numpy` (no cloud calls). We import its
# MAGIC > `build_*` functions and convert the resulting pandas DataFrames to Spark, rather
# MAGIC > than duplicating any generation logic here.

# COMMAND ----------

# MAGIC %md ## Parameters

# COMMAND ----------

dbutils.widgets.text("catalog", "zava", "Unity Catalog catalog (databricks_config.catalog)")
dbutils.widgets.text("raw_schema", "raw", "RAW medallion schema name")
dbutils.widgets.text("seed", "42", "Deterministic seed (databricks_config.data_seed)")
dbutils.widgets.text("sites", "18", "Number of Sites")
dbutils.widgets.text("vehicles", "600", "Fleet size")
dbutils.widgets.text("customers", "1500", "Customer count")
dbutils.widgets.text("reservations", "4000", "Reservation count")
dbutils.widgets.text("days", "365", "History window length (days)")
dbutils.widgets.text(
    "generator_path",
    "",
    "Path to the repo 'data' dir containing generate_zava_data.py (blank = auto-detect)",
)

catalog = dbutils.widgets.get("catalog")
raw_schema = dbutils.widgets.get("raw_schema")
seed = int(dbutils.widgets.get("seed"))
n_sites = int(dbutils.widgets.get("sites"))
n_vehicles = int(dbutils.widgets.get("vehicles"))
n_customers = int(dbutils.widgets.get("customers"))
n_reservations = int(dbutils.widgets.get("reservations"))
n_days = int(dbutils.widgets.get("days"))
generator_path = dbutils.widgets.get("generator_path").strip()

print(f"catalog={catalog} raw_schema={raw_schema} seed={seed} sites={n_sites} vehicles={n_vehicles} "
      f"customers={n_customers} reservations={n_reservations} days={n_days}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Locate + import the Step-3 generator
# MAGIC Tries the explicit `generator_path` widget first, then a set of common locations
# MAGIC (repo checked out as a Databricks Git folder, bundle workspace path, or sibling
# MAGIC `data/` dir). The generator file is `generate_zava_data.py` from repo Step 3.

# COMMAND ----------

import importlib.util
import os

_CANDIDATES = [
    generator_path,
    os.path.join(generator_path, "data") if generator_path else "",
    "../data",                          # deployed bundle layout: files/notebooks -> files/data
    "../../data",                       # databricks/notebooks/ -> repo/data (local checkout)
    "../../../data",
    "./data",
    "/Workspace/Repos/zava/20260608_zava_databricks_fabric/data",
    "/Workspace/Repos/zava/databricks-fabric/data",
]


def _load_generator():
    tried = []
    for base in _CANDIDATES:
        if not base:
            continue
        candidate = os.path.abspath(os.path.join(base, "generate_zava_data.py"))
        tried.append(candidate)
        if os.path.exists(candidate):
            spec = importlib.util.spec_from_file_location("generate_zava_data", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            print(f"Loaded generator from: {candidate}")
            return module
    raise FileNotFoundError(
        "Could not locate generate_zava_data.py. Set the 'generator_path' widget to the "
        "repo 'data' directory (where Step-3 lives). Tried:\n  " + "\n  ".join(tried)
    )


gen = _load_generator()

# COMMAND ----------

# MAGIC %md ## Build the 9 entities (mirrors the Step-3 generator's `main()` orchestration)

# COMMAND ----------

from datetime import datetime, timedelta, timezone

import numpy as np

rng = np.random.default_rng(seed)
end = datetime(2025, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
start = end - timedelta(days=n_days)

classes = gen.build_vehicle_classes()
sites = gen.build_sites(rng, n_sites)
vehicles = gen.build_vehicles(rng, n_vehicles, sites, classes)
customers = gen.build_customers(rng, n_customers, sites)
reservations = gen.build_reservations(rng, n_reservations, customers, sites, classes, start, end)
rentals = gen.build_rentals(rng, reservations, vehicles, customers, sites, classes, start, end)
payments = gen.build_payments(rng, rentals, classes)
maintenance = gen.build_maintenance(rng, vehicles, start, end)
telematics = gen.build_telematics_snapshot(rng, vehicles, sites, end)

entities = {
    "vehicle_classes": classes,
    "sites": sites,
    "vehicles": vehicles,
    "customers": customers,
    "reservations": reservations,
    "rentals": rentals,
    "payments": payments,
    "maintenance": maintenance,
    "telematics": telematics,
}

for name, pdf in entities.items():
    print(f"  {name:18s} {len(pdf):>8d} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Referential-integrity assertions (fail-fast, mirrors the generator)
# MAGIC Guarantees that what we land in `raw` is FK-consistent before any downstream layer
# MAGIC depends on it.

# COMMAND ----------

site_ids = set(sites["site_id"])
veh_ids = set(vehicles["vehicle_id"])
cust_ids = set(customers["customer_id"])
class_ids = set(classes["vehicle_class_id"])
rental_ids = set(rentals["rental_id"])


def _check(cond, msg):
    if not cond:
        raise AssertionError(f"Referential integrity FAILED: {msg}")


_check(set(vehicles["home_site_id"]) <= site_ids, "Vehicles.home_site_id -> Sites")
_check(set(vehicles["current_site_id"]) <= site_ids, "Vehicles.current_site_id -> Sites")
_check(set(vehicles["vehicle_class_id"]) <= class_ids, "Vehicles.vehicle_class_id -> VehicleClasses")
_check(set(rentals["vehicle_id"]) <= veh_ids, "Rentals.vehicle_id -> Vehicles")
_check(set(rentals["pickup_site_id"]) <= site_ids, "Rentals.pickup_site_id -> Sites")
_check(set(rentals["return_site_id"]) <= site_ids, "Rentals.return_site_id -> Sites")
_check(set(rentals["customer_id"]) <= cust_ids, "Rentals.customer_id -> Customers")
_check(set(payments["rental_id"]) <= rental_ids, "Payments.rental_id -> Rentals")
_check(set(maintenance["vehicle_id"]) <= veh_ids, "Maintenance.vehicle_id -> Vehicles")
_check(set(maintenance["site_id"]) <= site_ids, "Maintenance.site_id -> Sites")
_check(set(telematics["vehicle_id"]) <= veh_ids, "Telematics.vehicle_id -> Vehicles")
_check(int(rentals.attrs.get("one_way_count", 0)) > 0, "at least one one-way rental")

print("referential integrity: OK")

# COMMAND ----------

# MAGIC %md ## Write each entity to `<catalog>.raw.<entity>` (managed Delta, overwrite)

# COMMAND ----------

spark.sql(f"USE CATALOG {catalog}")
spark.sql(f"USE SCHEMA {raw_schema}")

for name, pdf in entities.items():
    sdf = spark.createDataFrame(pdf)
    (
        sdf.write
        .format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(f"{catalog}.{raw_schema}.{name}")
    )
    print(f"wrote {catalog}.{raw_schema}.{name}: {sdf.count()} rows")

# COMMAND ----------

# MAGIC %md ## Verify

# COMMAND ----------

display(spark.sql(f"SHOW TABLES IN {catalog}.{raw_schema}"))
print("raw.rentals count:", spark.table(f"{catalog}.{raw_schema}.rentals").count())
