#!/usr/bin/env python3
"""
generate_telematics_stream.py — Synthetic Zava telematics event feed (Phase 0, local only).

Emits per-vehicle telematics events suitable for ingestion into a Microsoft Fabric
**Eventstream / Eventhouse (KQL)** source for the Real-Time Intelligence demo
(plan Steps 18-20). Each event is a single JSON object (one event per line / NDJSON
when written to file or stdout), so it can feed an Eventstream "Custom endpoint" /
sample-data path, or be replayed by a small pusher.

Event shape (one telematics reading):
    {
      "event_id": "...",            # unique per event
      "vehicle_id": "V00012",       # FK -> Vehicles.vehicle_id
      "site_id": "S003",            # FK -> Sites.site_id (nearest/home site)
      "ts": "2025-01-01T12:00:03Z", # event time (advances with the feed)
      "ignition_state": "off",      # on | off
      "idle_minutes": 7,            # minutes the vehicle has been idle
      "odometer_miles": 41233,
      "fault_code": null,           # OBD-II-like code or null
      "latitude": 47.6062,
      "longitude": -122.3321,
      "speed_mph": 0,
      "fuel_or_soc_pct": 64,
      "is_spike": false             # true while inside the injected spike window
    }

THE SPIKE WINDOW (watch + act demo, Steps 19-20)
------------------------------------------------
The feed normally emits "calm" telematics (low idle_minutes, mostly null fault codes).
With `--inject-spike`, a *bounded* window is injected during which a subset of vehicles
report a clearly elevated condition that downstream rules / agents alert on:
    * --spike-type idle   -> idle_minutes jumps far above the alert threshold
                             (idle-vehicle / under-utilized-asset alert)
    * --spike-type fault  -> fault_code populated + maintenance-relevant flags
                             (overdue-maintenance / fault alert)
    * --spike-type both   -> both conditions (default)
The window is defined by --spike-start (event index or seconds offset) and
--spike-duration. Events inside carry "is_spike": true so a count over the interval
verifies the elevation. A Fabric Activator rule (Step 19) or Operations Agent
(Step 20) watches for idle_minutes > threshold / non-null fault_code and acts.

Deterministic but time-advancing: with a fixed --seed the *content* is reproducible;
`ts` advances from --start-ts (default: now, or fixed via --start-ts for reproducible files).

NOTHING here calls Azure / Databricks / Fabric or requires authentication.

Usage:
    # 200 events to stdout, normal feed
    python data/generate_telematics_stream.py --count 200

    # inject an idle+fault spike between event 50 and 90, write NDJSON to a file
    python data/generate_telematics_stream.py --count 200 --inject-spike \
        --spike-start 50 --spike-duration 40 --out ./_tmp/telematics_stream.ndjson

    # continuous-ish feed at ~10 events/sec to stdout (bounded by --count)
    python data/generate_telematics_stream.py --rate 10 --count 100 --inject-spike
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np

# OBD-II-like fault codes (kept in sync with batch generator).
FAULT_CODES = ["P0300", "P0420", "P0171", "B0010", "C0561", "P0128", "U0100"]

# Alert thresholds the downstream Activator / Operations Agent rule uses (Steps 19-20).
IDLE_ALERT_THRESHOLD_MIN = 120     # idle_minutes above this => idle-vehicle alert
DEFAULT_SITE_COORD = (47.6062, -122.3321)  # Seattle HQ fallback


def load_fleet(batch_dir: str, n_fallback: int, rng) -> list[dict]:
    """Load (vehicle_id, site_id, lat, long, odometer) from batch output if present,
    so the stream's FKs resolve against the batch entities. Falls back to a synthetic
    fleet when batch output is unavailable (e.g., stream-only smoke test)."""
    vehicles_csv = os.path.join(batch_dir, "vehicles.csv")
    sites_csv = os.path.join(batch_dir, "sites.csv")
    fleet: list[dict] = []
    if os.path.exists(vehicles_csv) and os.path.exists(sites_csv):
        import csv
        site_coord: dict[str, tuple[float, float]] = {}
        with open(sites_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                site_coord[row["site_id"]] = (float(row["latitude"]), float(row["longitude"]))
        with open(vehicles_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid = row.get("current_site_id") or row.get("home_site_id")
                lat, lng = site_coord.get(sid, DEFAULT_SITE_COORD)
                fleet.append({
                    "vehicle_id": row["vehicle_id"],
                    "site_id": sid,
                    "lat": lat,
                    "lng": lng,
                    "odometer": int(float(row.get("odometer_miles", 30000))),
                })
        source = f"batch dir '{batch_dir}'"
    else:
        # Synthetic fallback fleet (still deterministic via rng).
        for i in range(1, n_fallback + 1):
            fleet.append({
                "vehicle_id": f"V{i:05d}",
                "site_id": f"S{(i % 18) + 1:03d}",
                "lat": DEFAULT_SITE_COORD[0] + float(rng.normal(0, 1.5)),
                "lng": DEFAULT_SITE_COORD[1] + float(rng.normal(0, 1.5)),
                "odometer": int(rng.integers(2000, 90000)),
            })
        source = f"synthetic fallback ({n_fallback} vehicles)"
    print(f"[telematics] fleet source: {source}; {len(fleet)} vehicles", file=sys.stderr)
    return fleet


def make_event(rng, vehicle: dict, ts: datetime, in_spike: bool, spike_type: str) -> dict:
    """Build one telematics event. When in_spike, elevate idle/fault per spike_type."""
    odo = vehicle["odometer"] + int(rng.integers(0, 50))
    if in_spike and spike_type in ("idle", "both"):
        idle = int(rng.integers(IDLE_ALERT_THRESHOLD_MIN + 60, IDLE_ALERT_THRESHOLD_MIN + 720))
        ignition = "off"
        speed = 0
    else:
        idle = int(rng.integers(0, 45))
        ignition = "on" if rng.random() < 0.4 else "off"
        speed = int(rng.integers(0, 70)) if ignition == "on" else 0

    if in_spike and spike_type in ("fault", "both"):
        fault = FAULT_CODES[int(rng.integers(0, len(FAULT_CODES)))]
    else:
        fault = FAULT_CODES[int(rng.integers(0, len(FAULT_CODES)))] if rng.random() < 0.03 else None

    return {
        "event_id": str(uuid.UUID(int=(int(rng.integers(0, 2**63)) << 64) | int(rng.integers(0, 2**63)))),
        "vehicle_id": vehicle["vehicle_id"],
        "site_id": vehicle["site_id"],
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ignition_state": ignition,
        "idle_minutes": idle,
        "odometer_miles": odo,
        "fault_code": fault,
        "latitude": round(vehicle["lat"] + float(rng.normal(0, 0.005)), 5),
        "longitude": round(vehicle["lng"] + float(rng.normal(0, 0.005)), 5),
        "speed_mph": speed,
        "fuel_or_soc_pct": int(rng.integers(5, 100)),
        "is_spike": bool(in_spike),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a synthetic Zava telematics event feed.")
    ap.add_argument("--batch-dir", default="./data/output",
                    help="Directory with vehicles.csv/sites.csv (for FK-consistent IDs).")
    ap.add_argument("--count", type=int, default=200, help="Total events to emit.")
    ap.add_argument("--rate", type=float, default=0.0,
                    help="Events/sec for live pacing (0 = emit as fast as possible).")
    ap.add_argument("--seed", type=int, default=42, help="Deterministic content seed.")
    ap.add_argument("--out", default="-", help="Output file (NDJSON) or '-' for stdout.")
    ap.add_argument("--start-ts", default=None,
                    help="ISO start time for the 'ts' field (default: now, UTC). "
                         "Set a fixed value for reproducible files.")
    ap.add_argument("--ts-step-sec", type=float, default=1.0,
                    help="Seconds the event clock advances per event.")
    ap.add_argument("--fallback-vehicles", type=int, default=50,
                    help="Synthetic fleet size when batch output is unavailable.")
    # spike controls
    ap.add_argument("--inject-spike", action="store_true",
                    help="Inject a bounded idle/fault spike window for the watch+act demo.")
    ap.add_argument("--spike-start", type=int, default=None,
                    help="Event index where the spike begins (default: ~40%% in).")
    ap.add_argument("--spike-duration", type=int, default=None,
                    help="Number of events the spike lasts (default: ~25%% of count).")
    ap.add_argument("--spike-type", choices=["idle", "fault", "both"], default="both")
    ap.add_argument("--spike-fraction", type=float, default=0.5,
                    help="Fraction of events inside the window that are elevated (0-1).")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    fleet = load_fleet(args.batch_dir, args.fallback_vehicles, rng)
    if not fleet:
        print("[telematics] ERROR: empty fleet", file=sys.stderr)
        return 2

    if args.start_ts:
        base_ts = datetime.fromisoformat(args.start_ts.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    else:
        base_ts = datetime.now(timezone.utc).replace(tzinfo=None)

    # Resolve spike window.
    spike_start = args.spike_start if args.spike_start is not None else int(args.count * 0.4)
    spike_dur = args.spike_duration if args.spike_duration is not None else max(1, int(args.count * 0.25))
    spike_end = spike_start + spike_dur

    sink = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")
    emitted = 0
    spike_events = 0
    try:
        for i in range(args.count):
            vehicle = fleet[int(rng.integers(0, len(fleet)))]
            ts = base_ts + timedelta(seconds=args.ts_step_sec * i)
            in_window = args.inject_spike and (spike_start <= i < spike_end)
            # only a fraction of in-window events are elevated (realistic burst)
            elevated = in_window and (rng.random() < args.spike_fraction)
            event = make_event(rng, vehicle, ts, elevated, args.spike_type)
            if elevated:
                spike_events += 1
            sink.write(json.dumps(event) + "\n")
            emitted += 1
            if args.rate and args.rate > 0:
                time.sleep(1.0 / args.rate)
        sink.flush()
    finally:
        if sink is not sys.stdout:
            sink.close()

    where = "stdout" if args.out == "-" else args.out
    print(f"[telematics] emitted {emitted} events -> {where}", file=sys.stderr)
    if args.inject_spike:
        print(f"[telematics] spike window: events [{spike_start},{spike_end}) "
              f"type={args.spike_type}; elevated events={spike_events} "
              f"(idle>{IDLE_ALERT_THRESHOLD_MIN}min and/or fault_code set)", file=sys.stderr)
    else:
        print("[telematics] no spike injected (normal feed)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
