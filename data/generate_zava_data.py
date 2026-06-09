#!/usr/bin/env python3
"""
generate_zava_data.py — Synthetic Zava Car Rental batch data generator (Phase 0, local only).

Zava is a fictional car-rental company headquartered in Seattle, WA, with sites
across multiple US cities. This script generates a fully synthetic, referentially
consistent relational dataset for the demo's medallion pipeline and Power BI report.

Entities produced (9):
    VehicleClasses, Sites, Vehicles, Customers, Reservations,
    Rentals, Payments, Maintenance, Telematics

Design goals:
    * Deterministic — a fixed integer seed (default 42, matching `databricks_config.data_seed`)
      yields byte-identical row counts and key sets across runs.
    * Referentially consistent — every foreign key resolves to a parent row.
    * KPI-ready — columns encode: fleet utilization, revenue/site, idle vehicles,
      one-way flows, maintenance cost.
    * Synthetic PII-like columns (customer email/phone) for label / DLP / column-mask demos.
      All values are clearly synthetic (@example.com, 555 area codes).

NOTHING here calls Azure / Databricks / Fabric or requires authentication.

Usage:
    python data/generate_zava_data.py --out ./data/output
    python data/generate_zava_data.py --out ./_tmp --customers 500 --vehicles 300 --rentals 2000 --format both
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------------
# Reference / lookup data (curated, deterministic — Seattle HQ + multi-US-city footprint)
# --------------------------------------------------------------------------------------

# (city, state, lat, long, is_hq). Seattle is the HQ. Real-ish coordinates for map visuals.
CITIES = [
    ("Seattle", "WA", 47.6062, -122.3321, True),
    ("Bellevue", "WA", 47.6101, -122.2015, False),
    ("Portland", "OR", 45.5152, -122.6784, False),
    ("San Francisco", "CA", 37.7749, -122.4194, False),
    ("Los Angeles", "CA", 34.0522, -118.2437, False),
    ("Las Vegas", "NV", 36.1699, -115.1398, False),
    ("Phoenix", "AZ", 33.4484, -112.0740, False),
    ("Denver", "CO", 39.7392, -104.9903, False),
    ("Dallas", "TX", 32.7767, -96.7970, False),
    ("Chicago", "IL", 41.8781, -87.6298, False),
    ("Atlanta", "GA", 33.7490, -84.3880, False),
    ("Miami", "FL", 25.7617, -80.1918, False),
    ("New York", "NY", 40.7128, -74.0060, False),
    ("Boston", "MA", 42.3601, -71.0589, False),
]

# Vehicle classes: class_name, category, daily_base_rate (USD), seats, is_ev
VEHICLE_CLASSES = [
    ("Economy", "Car", 39.0, 5, False),
    ("Compact", "Car", 45.0, 5, False),
    ("Midsize", "Car", 55.0, 5, False),
    ("Fullsize", "Car", 65.0, 5, False),
    ("Standard SUV", "SUV", 85.0, 5, False),
    ("Full-size SUV", "SUV", 110.0, 7, False),
    ("Luxury", "Luxury", 145.0, 5, False),
    ("Minivan", "Van", 95.0, 7, False),
    ("Pickup Truck", "Truck", 90.0, 5, False),
    ("Electric Compact", "EV", 60.0, 5, True),
    ("Electric SUV", "EV", 105.0, 5, True),
]

# Make/model pool keyed by category, for realistic vehicle rows.
MODELS = {
    "Car": [("Toyota", "Corolla"), ("Honda", "Civic"), ("Nissan", "Sentra"),
            ("Hyundai", "Elantra"), ("Toyota", "Camry"), ("Honda", "Accord")],
    "SUV": [("Toyota", "RAV4"), ("Honda", "CR-V"), ("Ford", "Explorer"),
            ("Chevrolet", "Tahoe"), ("Jeep", "Grand Cherokee")],
    "Luxury": [("BMW", "5 Series"), ("Mercedes-Benz", "E-Class"), ("Audi", "A6")],
    "Van": [("Chrysler", "Pacifica"), ("Honda", "Odyssey"), ("Toyota", "Sienna")],
    "Truck": [("Ford", "F-150"), ("Chevrolet", "Silverado"), ("Ram", "1500")],
    "EV": [("Tesla", "Model 3"), ("Tesla", "Model Y"), ("Ford", "Mustang Mach-E"),
           ("Hyundai", "Ioniq 5")],
}

COLORS = ["White", "Black", "Silver", "Gray", "Blue", "Red"]
LOYALTY_TIERS = ["None", "Silver", "Gold", "Platinum"]
PAYMENT_METHODS = ["Visa", "Mastercard", "Amex", "Discover", "Corporate"]
MAINT_TYPES = ["Oil Change", "Tire Rotation", "Brake Service", "Battery",
               "Transmission", "Engine Diagnostic", "Body Repair", "Recall"]
# Telematics / diagnostic fault codes (OBD-II-like). 'P0420' = catalyst, etc.
FAULT_CODES = ["P0300", "P0420", "P0171", "B0010", "C0561", "P0128", "U0100"]

# Synthetic name pools (NOT real people — fully synthetic combinations).
FIRST_NAMES = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley",
               "Jamie", "Avery", "Quinn", "Drew", "Reese", "Skyler", "Cameron",
               "Dakota", "Hayden", "Emerson", "Finley", "Rowan", "Sawyer",
               "Blake", "Charlie", "Devon", "Elliot", "Frankie", "Gray"]
LAST_NAMES = ["Rivera", "Chen", "Patel", "Nguyen", "Johnson", "Garcia", "Kim",
              "Brown", "Martinez", "Lee", "Davis", "Lopez", "Wilson", "Anderson",
              "Thomas", "Taylor", "Moore", "Jackson", "Khan", "Singh", "Cohen",
              "Okafor", "Murphy", "Schmidt", "Rossi", "Yamamoto"]


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def _dt(rng, start: datetime, end: datetime, size: int) -> np.ndarray:
    """Vectorized random datetimes between start and end (deterministic with rng)."""
    span = (end - start).total_seconds()
    offsets = rng.integers(0, int(span), size=size)
    return np.array([start + timedelta(seconds=int(o)) for o in offsets])


def _iso(series: pd.Series) -> pd.Series:
    """Format datetime series as ISO-8601 UTC strings (stable for CSV/Parquet)."""
    return pd.to_datetime(series, utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------
# Entity builders
# --------------------------------------------------------------------------------------

def build_vehicle_classes() -> pd.DataFrame:
    rows = []
    for i, (name, cat, rate, seats, is_ev) in enumerate(VEHICLE_CLASSES, start=1):
        rows.append({
            "vehicle_class_id": f"VC{i:03d}",
            "class_name": name,
            "category": cat,
            "daily_base_rate_usd": rate,
            "seats": seats,
            "is_ev": is_ev,
        })
    return pd.DataFrame(rows)


def build_sites(rng, n_sites: int) -> pd.DataFrame:
    rows = []
    # Always include one site per curated city first (HQ = Seattle), then add extras.
    base = list(CITIES)
    chosen = []
    for c in base:
        chosen.append(c)
        if len(chosen) >= n_sites:
            break
    # If more sites than cities requested, add extra sites in existing cities (e.g. Airport vs Downtown).
    extra_idx = 0
    while len(chosen) < n_sites:
        chosen.append(base[extra_idx % len(base)])
        extra_idx += 1

    suffix_cycle = ["Downtown", "Airport", "North", "South", "Eastside"]
    city_counts: dict[str, int] = {}
    for i, (city, state, lat, lng, is_hq) in enumerate(chosen, start=1):
        n = city_counts.get(city, 0)
        city_counts[city] = n + 1
        suffix = "HQ" if is_hq and n == 0 else suffix_cycle[n % len(suffix_cycle)]
        # jitter coordinates slightly for distinct sites in the same city
        jlat = float(lat + rng.normal(0, 0.02))
        jlng = float(lng + rng.normal(0, 0.02))
        rows.append({
            "site_id": f"S{i:03d}",
            "site_code": f"{state}{i:03d}",
            "site_name": f"Zava {city} {suffix}",
            "city": city,
            "state": state,
            "latitude": round(jlat, 5),
            "longitude": round(jlng, 5),
            "is_hq": bool(is_hq and n == 0),
            "parking_capacity": int(rng.integers(40, 200)),
            "opened_date": (datetime(2015, 1, 1) + timedelta(days=int(rng.integers(0, 2500)))).date().isoformat(),
        })
    return pd.DataFrame(rows)


def build_vehicles(rng, n_vehicles: int, sites: pd.DataFrame, classes: pd.DataFrame) -> pd.DataFrame:
    site_ids = sites["site_id"].to_numpy()
    rows = []
    class_records = classes.to_dict("records")
    for i in range(1, n_vehicles + 1):
        vc = class_records[int(rng.integers(0, len(class_records)))]
        make, model = MODELS[vc["category"]][int(rng.integers(0, len(MODELS[vc["category"]])))]
        home = str(site_ids[int(rng.integers(0, len(site_ids)))])
        year = int(rng.integers(2019, 2025))
        # Status mix: most available/rented, a few idle, a few in maintenance.
        status = rng.choice(
            ["available", "rented", "idle", "maintenance"],
            p=[0.50, 0.33, 0.10, 0.07],
        )
        # current_site differs from home for some vehicles (one-way drift) — still a valid site.
        current = home if rng.random() < 0.75 else str(site_ids[int(rng.integers(0, len(site_ids)))])
        rows.append({
            "vehicle_id": f"V{i:05d}",
            "vin": f"ZAVA{i:011d}",          # synthetic VIN-like token
            "vehicle_class_id": vc["vehicle_class_id"],
            "make": make,
            "model": model,
            "model_year": year,
            "color": COLORS[int(rng.integers(0, len(COLORS)))],
            "license_plate": f"ZV{rng.integers(1000, 9999)}{chr(65 + int(rng.integers(0,26)))}",
            "home_site_id": home,
            "current_site_id": current,
            "status": str(status),
            "odometer_miles": int(rng.integers(1500, 90000)),
            "acquired_date": (datetime(2019, 1, 1) + timedelta(days=int(rng.integers(0, 1800)))).date().isoformat(),
        })
    return pd.DataFrame(rows)


def build_customers(rng, n_customers: int, sites: pd.DataFrame) -> pd.DataFrame:
    # Customer home city distribution drawn from site cities (markets Zava serves).
    cities = sites[["city", "state"]].drop_duplicates().to_dict("records")
    rows = []
    for i in range(1, n_customers + 1):
        fn = FIRST_NAMES[int(rng.integers(0, len(FIRST_NAMES)))]
        ln = LAST_NAMES[int(rng.integers(0, len(LAST_NAMES)))]
        city = cities[int(rng.integers(0, len(cities)))]
        # SYNTHETIC PII-like columns (for DLP / label / column-mask demos). Clearly fake.
        email = f"{fn.lower()}.{ln.lower()}{i}@example.com"
        phone = f"+1-555-{rng.integers(100,999):03d}-{rng.integers(1000,9999):04d}"
        rows.append({
            "customer_id": f"C{i:06d}",
            "first_name": fn,
            "last_name": ln,
            "email": email,                     # synthetic PII-like
            "phone": phone,                     # synthetic PII-like
            "loyalty_tier": LOYALTY_TIERS[int(rng.choice(len(LOYALTY_TIERS), p=[0.45, 0.30, 0.18, 0.07]))],
            "home_city": city["city"],
            "home_state": city["state"],
            "join_date": (datetime(2018, 1, 1) + timedelta(days=int(rng.integers(0, 2400)))).date().isoformat(),
        })
    return pd.DataFrame(rows)


def build_reservations(rng, n_res: int, customers, sites, classes,
                       start: datetime, end: datetime) -> pd.DataFrame:
    cust_ids = customers["customer_id"].to_numpy()
    site_ids = sites["site_id"].to_numpy()
    class_ids = classes["vehicle_class_id"].to_numpy()
    reserved_at = _dt(rng, start, end, n_res)
    rows = []
    for i in range(n_res):
        pickup = reserved_at[i] + timedelta(days=int(rng.integers(1, 30)))
        ret = pickup + timedelta(days=int(rng.integers(1, 14)))
        pickup_site = str(site_ids[int(rng.integers(0, len(site_ids)))])
        # ~18% one-way reservations
        return_site = pickup_site if rng.random() > 0.18 else str(site_ids[int(rng.integers(0, len(site_ids)))])
        status = rng.choice(["fulfilled", "cancelled", "noshow", "booked"], p=[0.72, 0.13, 0.05, 0.10])
        rows.append({
            "reservation_id": f"R{i+1:07d}",
            "customer_id": str(cust_ids[int(rng.integers(0, len(cust_ids)))]),
            "vehicle_class_id": str(class_ids[int(rng.integers(0, len(class_ids)))]),
            "pickup_site_id": pickup_site,
            "return_site_id": return_site,
            "reserved_at": reserved_at[i],
            "pickup_date": pickup.date().isoformat(),
            "planned_return_date": ret.date().isoformat(),
            "status": str(status),
        })
    df = pd.DataFrame(rows)
    df["reserved_at"] = _iso(df["reserved_at"])
    return df


def build_rentals(rng, reservations: pd.DataFrame, vehicles: pd.DataFrame,
                  customers, sites, classes, start, end) -> pd.DataFrame:
    """Rentals derive mostly from fulfilled reservations; a fraction are walk-ins."""
    veh = vehicles.to_dict("records")
    veh_by_class: dict[str, list] = {}
    for v in veh:
        veh_by_class.setdefault(v["vehicle_class_id"], []).append(v)
    all_site_ids = sites["site_id"].to_numpy()
    cust_ids = customers["customer_id"].to_numpy()
    class_ids = classes["vehicle_class_id"].to_numpy()

    fulfilled = reservations[reservations["status"] == "fulfilled"].to_dict("records")

    rows = []
    rid = 0
    one_way_count = 0
    for res in fulfilled:
        rid += 1
        vclass = res["vehicle_class_id"]
        pool = veh_by_class.get(vclass) or veh
        vehicle = pool[int(rng.integers(0, len(pool)))]
        pickup_ts = pd.to_datetime(res["pickup_date"]).to_pydatetime() + timedelta(hours=int(rng.integers(7, 20)))
        planned_return = pd.to_datetime(res["planned_return_date"]).to_pydatetime() + timedelta(hours=int(rng.integers(7, 20)))
        # actual return: usually on/near planned; sometimes overdue
        ret_jitter = int(rng.integers(-12, 36))
        return_ts = planned_return + timedelta(hours=ret_jitter)
        is_open = rng.random() < 0.08  # some rentals still open (no return yet)
        pickup_site = res["pickup_site_id"]
        return_site = res["return_site_id"]
        is_one_way = pickup_site != return_site
        if is_one_way:
            one_way_count += 1
        odo_out = int(rng.integers(2000, 85000))
        miles = int(rng.integers(40, 1800))
        rows.append({
            "rental_id": f"RN{rid:08d}",
            "reservation_id": res["reservation_id"],
            "customer_id": res["customer_id"],
            "vehicle_id": vehicle["vehicle_id"],
            "vehicle_class_id": vclass,
            "pickup_site_id": pickup_site,
            "return_site_id": return_site,
            "pickup_ts": pickup_ts,
            "planned_return_ts": planned_return,
            "return_ts": (None if is_open else return_ts),
            "odometer_out": odo_out,
            "odometer_in": (None if is_open else odo_out + miles),
            "miles_driven": (None if is_open else miles),
            "is_one_way": is_one_way,
            "status": ("open" if is_open else "closed"),
        })

    # Walk-in rentals (no reservation) — ~10% additional.
    n_walkin = max(1, int(len(fulfilled) * 0.10))
    for _ in range(n_walkin):
        rid += 1
        vclass = str(class_ids[int(rng.integers(0, len(class_ids)))])
        pool = veh_by_class.get(vclass) or veh
        vehicle = pool[int(rng.integers(0, len(pool)))]
        pickup_ts = _dt(rng, start, end, 1)[0] + timedelta(hours=int(rng.integers(7, 20)))
        days = int(rng.integers(1, 10))
        planned_return = pickup_ts + timedelta(days=days)
        return_ts = planned_return + timedelta(hours=int(rng.integers(-12, 36)))
        pickup_site = str(all_site_ids[int(rng.integers(0, len(all_site_ids)))])
        return_site = pickup_site if rng.random() > 0.18 else str(all_site_ids[int(rng.integers(0, len(all_site_ids)))])
        is_one_way = pickup_site != return_site
        if is_one_way:
            one_way_count += 1
        odo_out = int(rng.integers(2000, 85000))
        miles = int(rng.integers(40, 1800))
        rows.append({
            "rental_id": f"RN{rid:08d}",
            "reservation_id": None,
            "customer_id": str(cust_ids[int(rng.integers(0, len(cust_ids)))]),
            "vehicle_id": vehicle["vehicle_id"],
            "vehicle_class_id": vclass,
            "pickup_site_id": pickup_site,
            "return_site_id": return_site,
            "pickup_ts": pickup_ts,
            "planned_return_ts": planned_return,
            "return_ts": return_ts,
            "odometer_out": odo_out,
            "odometer_in": odo_out + miles,
            "miles_driven": miles,
            "is_one_way": is_one_way,
            "status": "closed",
        })

    # Guarantee the one-way-flow KPI has data even on tiny scales.
    if one_way_count == 0 and rows:
        r = rows[0]
        alt = [s for s in all_site_ids if s != r["pickup_site_id"]]
        if alt:
            r["return_site_id"] = str(alt[0])
            r["is_one_way"] = True
            one_way_count = 1

    df = pd.DataFrame(rows)
    for col in ("pickup_ts", "planned_return_ts", "return_ts"):
        df[col] = _iso(df[col])
    df.attrs["one_way_count"] = one_way_count
    return df


def build_payments(rng, rentals: pd.DataFrame, classes: pd.DataFrame) -> pd.DataFrame:
    rate_by_class = dict(zip(classes["vehicle_class_id"], classes["daily_base_rate_usd"]))
    rows = []
    pid = 0
    closed = rentals[rentals["status"] == "closed"]
    for r in closed.to_dict("records"):
        pid += 1
        pickup = pd.to_datetime(r["pickup_ts"])
        ret = pd.to_datetime(r["return_ts"])
        days = max(1, int((ret - pickup).total_seconds() // 86400))
        base = round(rate_by_class.get(r["vehicle_class_id"], 50.0) * days, 2)
        one_way_fee = 75.0 if r["is_one_way"] else 0.0
        taxes_fees = round((base + one_way_fee) * 0.135, 2)
        total = round(base + one_way_fee + taxes_fees, 2)
        rows.append({
            "payment_id": f"P{pid:08d}",
            "rental_id": r["rental_id"],
            "customer_id": r["customer_id"],
            "pickup_site_id": r["pickup_site_id"],   # denormalized for revenue/site KPI
            "rental_days": days,
            "base_amount_usd": base,
            "one_way_fee_usd": one_way_fee,
            "taxes_fees_usd": taxes_fees,
            "total_amount_usd": total,
            "currency": "USD",
            "payment_method": PAYMENT_METHODS[int(rng.integers(0, len(PAYMENT_METHODS)))],
            "payment_ts": _iso(pd.Series([ret + timedelta(minutes=int(rng.integers(5, 120)))]))[0],
            "status": "captured",
        })
    return pd.DataFrame(rows)


def build_maintenance(rng, vehicles: pd.DataFrame, start, end, rate=0.35) -> pd.DataFrame:
    """A fraction of vehicles have maintenance records; supports maintenance-cost KPI."""
    veh = vehicles.to_dict("records")
    rows = []
    mid = 0
    for v in veh:
        n_events = int(rng.poisson(rate))
        for _ in range(n_events):
            mid += 1
            opened = _dt(rng, start, end, 1)[0]
            dur_h = int(rng.integers(2, 96))
            still_open = rng.random() < 0.12
            closed = None if still_open else opened + timedelta(hours=dur_h)
            mtype = MAINT_TYPES[int(rng.integers(0, len(MAINT_TYPES)))]
            labor = round(float(rng.integers(80, 900)), 2)
            parts = round(float(rng.integers(20, 1500)), 2)
            rows.append({
                "maintenance_id": f"M{mid:07d}",
                "vehicle_id": v["vehicle_id"],
                "site_id": v["current_site_id"],   # serviced at current site
                "maintenance_type": mtype,
                "fault_code": (FAULT_CODES[int(rng.integers(0, len(FAULT_CODES)))]
                               if mtype in ("Engine Diagnostic", "Battery", "Recall") else None),
                "opened_ts": opened,
                "closed_ts": closed,
                "labor_cost_usd": labor,
                "parts_cost_usd": parts,
                "total_cost_usd": round(labor + parts, 2),
                "odometer_at_service": int(v["odometer_miles"]) - int(rng.integers(0, 5000)),
                "status": ("open" if still_open else "closed"),
            })
    if not rows:  # guarantee at least one record for the maintenance-cost KPI
        v = veh[0]
        rows.append({
            "maintenance_id": "M0000001", "vehicle_id": v["vehicle_id"], "site_id": v["current_site_id"],
            "maintenance_type": "Oil Change", "fault_code": None,
            "opened_ts": start, "closed_ts": start + timedelta(hours=3),
            "labor_cost_usd": 120.0, "parts_cost_usd": 45.0, "total_cost_usd": 165.0,
            "odometer_at_service": int(v["odometer_miles"]), "status": "closed",
        })
    df = pd.DataFrame(rows)
    closed_mask = df["closed_ts"].notna()
    df["opened_ts"] = _iso(df["opened_ts"])
    # Format closed timestamps where present; leave open records as null.
    df["closed_ts"] = _iso(df["closed_ts"]).where(closed_mask, None)
    return df


def build_telematics_snapshot(rng, vehicles: pd.DataFrame, sites: pd.DataFrame,
                              snapshot_ts: datetime) -> pd.DataFrame:
    """Latest-known telematics snapshot per vehicle (batch table).
    The live feed is produced separately by generate_telematics_stream.py."""
    site_coord = sites.set_index("site_id")[["latitude", "longitude"]].to_dict("index")
    rows = []
    for i, v in enumerate(vehicles.to_dict("records"), start=1):
        site_id = v["current_site_id"]
        coord = site_coord.get(site_id, {"latitude": 47.6, "longitude": -122.3})
        status = v["status"]
        if status == "idle":
            idle = int(rng.integers(120, 1440))
            ignition = "off"
        elif status == "rented":
            idle = int(rng.integers(0, 20))
            ignition = "on"
        elif status == "maintenance":
            idle = int(rng.integers(60, 600))
            ignition = "off"
        else:
            idle = int(rng.integers(0, 90))
            ignition = "off"
        fault = (FAULT_CODES[int(rng.integers(0, len(FAULT_CODES)))]
                 if (status == "maintenance" or rng.random() < 0.05) else None)
        rows.append({
            "telematics_id": f"T{i:06d}",
            "vehicle_id": v["vehicle_id"],
            "site_id": site_id,
            "snapshot_ts": _iso(pd.Series([snapshot_ts]))[0],
            "ignition_state": ignition,
            "idle_minutes": idle,
            "odometer_miles": int(v["odometer_miles"]),
            "fault_code": fault,
            "latitude": round(float(coord["latitude"] + rng.normal(0, 0.01)), 5),
            "longitude": round(float(coord["longitude"] + rng.normal(0, 0.01)), 5),
            "speed_mph": (int(rng.integers(0, 75)) if ignition == "on" else 0),
            "fuel_or_soc_pct": int(rng.integers(5, 100)),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# Orchestration / IO
# --------------------------------------------------------------------------------------

def write_entity(df: pd.DataFrame, name: str, out_dir: str, fmt: str) -> list[str]:
    paths = []
    if fmt in ("csv", "both"):
        p = os.path.join(out_dir, f"{name}.csv")
        df.to_csv(p, index=False)
        paths.append(p)
    if fmt in ("parquet", "both"):
        p = os.path.join(out_dir, f"{name}.parquet")
        df.to_parquet(p, index=False)
        paths.append(p)
    return paths


def load_seed_from_config(explicit_seed: int | None) -> int:
    """Use --seed if given; else try databricks_config.data_seed; else default 42."""
    if explicit_seed is not None:
        return explicit_seed
    for candidate in (
        os.path.join("databricks", "config", "databricks_config.json"),
        os.path.join("databricks", "config", "databricks_config.sample.json"),
    ):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if "data_seed" in cfg:
                    return int(cfg["data_seed"])
            except (json.JSONDecodeError, ValueError, OSError):
                pass
    return 42


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic Zava car-rental batch data.")
    ap.add_argument("--out", default="./data/output", help="Output directory.")
    ap.add_argument("--seed", type=int, default=None,
                    help="Deterministic seed (default: databricks_config.data_seed or 42).")
    ap.add_argument("--format", choices=["csv", "parquet", "both"], default="both")
    ap.add_argument("--sites", type=int, default=18)
    ap.add_argument("--vehicles", type=int, default=600)
    ap.add_argument("--customers", type=int, default=1500)
    ap.add_argument("--reservations", type=int, default=4000)
    ap.add_argument("--days", type=int, default=365, help="History window length in days.")
    args = ap.parse_args()

    seed = load_seed_from_config(args.seed)
    rng = np.random.default_rng(seed)

    os.makedirs(args.out, exist_ok=True)
    end = datetime(2025, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=args.days)

    print(f"[zava] seed={seed} out={args.out} format={args.format}")
    print(f"[zava] history window: {start.date()} -> {end.date()}")

    classes = build_vehicle_classes()
    sites = build_sites(rng, args.sites)
    vehicles = build_vehicles(rng, args.vehicles, sites, classes)
    customers = build_customers(rng, args.customers, sites)
    reservations = build_reservations(rng, args.reservations, customers, sites, classes, start, end)
    rentals = build_rentals(rng, reservations, vehicles, customers, sites, classes, start, end)
    payments = build_payments(rng, rentals, classes)
    maintenance = build_maintenance(rng, vehicles, start, end)
    telematics = build_telematics_snapshot(rng, vehicles, sites, end)

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

    manifest = {"seed": seed, "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "history_start": start.date().isoformat(), "history_end": end.date().isoformat(),
                "entities": {}}
    print("\n[zava] row counts:")
    for name, df in entities.items():
        write_entity(df, name, args.out, args.format)
        manifest["entities"][name] = {"rows": int(len(df)), "columns": list(df.columns)}
        print(f"  {name:18s} {len(df):>8d} rows")

    manifest["one_way_rentals"] = int(rentals.attrs.get("one_way_count", 0))
    with open(os.path.join(args.out, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # ---- Inline referential-integrity assertions (fail-fast) ----
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
    _check(manifest["one_way_rentals"] > 0, "at least one one-way rental for the one-way-flow KPI")

    print(f"\n[zava] referential integrity: OK")
    print(f"[zava] one-way rentals: {manifest['one_way_rentals']}")
    print(f"[zava] wrote manifest: {os.path.join(args.out, '_manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
