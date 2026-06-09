# Cost Checkpoint — Read & Acknowledge **Before** Deploying (Step 5)

> **⛔ COST GATE.** The Zava demo provisions a **Microsoft Fabric F64 capacity**, which bills
> **pay-as-you-go (PAYG) at ~$11.52/hour** while it is **running (Active)**. Before you run the
> Fabric capacity Bicep module (**Step 5**), you must read this page and **explicitly acknowledge
> the cost model**. The deployment is designed for **aggressive pause/resume** — if you leave the
> capacity running 24/7 you will pay **~$8,410/month**. Used as intended (paused except when
> demoing) the realistic cost is **~$138–$300/month**.
>
> All figures below are sourced to research report **R9**
> (`research/2026-06-08-r9-fabric-capacity-region-cost.md`), retrieved from the Azure Retail
> Prices API on **2026-06-08**. **Prices change over time — verify with the
> [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/) before committing.**

---

## 1. The pre-deploy cost acknowledgement gate

Before proceeding to **Step 5 (Bicep — Fabric capacity module)** the deploying customer **must
acknowledge**:

1. **F64 PAYG bills ~$11.52/hour whenever the capacity is Active.** (≈ $276.48/day, ≈ $8,410/month
   if never paused.)
2. **Compute billing only stops when the capacity is paused (suspended).** Use
   `scripts/pause_capacity.py` whenever the demo is idle.
3. **OneLake storage keeps billing even while paused** (~$0.03/GB/month hot tier — negligible for
   demo datasets; ~$1.50/month for ~50 GB).
4. **You — not Microsoft — own these Azure charges.** The demo defaults to F64 because it is the
   only SKU that runs **all** demo features (Copilot, Data Agent, Free-license Power BI viewers)
   with no workarounds.

> **How to acknowledge:** confirm the four points above (e.g., a checked box in your runbook or an
> approval comment in your deployment ticket) **before** invoking the Step 5 capacity deployment.
> The orchestrator / `deploy.py` should treat Step 5 as gated on this acknowledgement.

---

## 2. F64 hourly & monthly cost model (R9 §6)

Fabric CU consumption is priced at **$0.18 per CU per hour**. F64 = 64 CUs.

| SKU | CUs | $/Hour (PAYG) | $/Day (24h) | $/Month (730h) |
|-----|-----|---------------|-------------|----------------|
| F2 | 2 | $0.36 | $8.64 | $262.80 |
| F32 | 32 | $5.76 | $138.24 | $4,204.80 |
| **F64** ⭐ | **64** | **$11.52** | **$276.48** | **$8,409.60** |
| F128 | 128 | $23.04 | $552.96 | $16,819.20 |

> **F64 = 64 CU × $0.18/CU/hr = $11.52/hour.** Source: Azure Retail Prices API (East US,
> `priceType eq 'Consumption'`, base meter `Power BI Capacity Usage CU` = $0.18/CU/hr),
> accessed 2026-06-08 (R9 §6). The $0.18/CU/hr rate is identical across the demo's candidate
> regions (East US 2 / West US).

---

## 3. The ~$138–$300/month "aggressive pausing" scenario (R9 §6)

The demo does **not** need 24/7 uptime. With disciplined pause/resume:

| Scenario | Hours/Day | Days/Week | Monthly Hours | F64 Monthly Cost |
|----------|-----------|-----------|---------------|------------------|
| Always-on (do **not** do this) | 24 | 7 | 730 | $8,409.60 |
| Business hours (8h/day, weekdays) | 8 | 5 | ~176 | ~$2,027 |
| Active development (4h/day, weekdays) | 4 | 5 | ~88 | ~$1,014 |
| Sporadic demo use (2h/day, 3×/week) | 2 | 3 | ~26 | **~$300** |
| **Minimal demo** (1h/day, 3×/week) | 1 | 3 | ~12 | **~$138** |

**Target operating range for this demo: ~$138–$300/month** (paused except when actively demoing or
developing), plus ~$1.50/month OneLake storage. To stay in this range, **pause the moment you are
done**:

```bash
# Stop compute billing as soon as the demo/dev session ends
python scripts/pause_capacity.py            # suspend the capacity
# ...later, just before the next session...
python scripts/resume_capacity.py           # bring it back to Active
```

---

## 4. OneLake storage + Copilot / AI CU notes (R9 §6)

- **OneLake storage** is billed PAYG per GB and **does not consume Fabric CUs**:
  Hot **$0.03/GB/month**, Cool $0.02, Cold $0.00, BCDR Hot $0.05 (East US, 2026-06-08).
  For a ~50 GB demo dataset that is ~**$1.50/month** — negligible.
- **⚠️ OneLake storage continues to bill while the capacity is paused.** Pausing stops **compute
  (CU)** billing only; all CU transactions are rejected when paused, so no CUs are consumed, but the
  stored bytes are still charged at the PAYG per-GB rate (R9 §5).
- **Copilot / AI CU consumption** is billed at the same **$0.18/CU/hour** as other workloads
  (metered as "Copilot and AI Capacity Usage CU"). Consumption per request is tiny: a typical
  Copilot request (~6.67 CU-minutes) uses only ~1 CU-minute of an hour. On F64 (1,536 CU-hours/day)
  you can run **>13,800 Copilot requests/day** before exhausting capacity — so for demo usage the
  AI features add **no meaningful incremental cost** beyond the hours the capacity is Active (R9 §6).

---

## 5. The 60-day Fabric trial capacity option — and its exclusions (R9 §7)

A free **Fabric trial capacity** is available and can cut early costs to **$0**, but it **cannot run
the full demo**:

| Property | Value |
|----------|-------|
| Duration | **60 days** |
| Capacity | **F4 (4 CUs) or F64 (64 CUs)** — depends on eligibility |
| OneLake storage | Up to 1 TB |
| Cost | **Free** |

**❌ Trial capacity does NOT support (hard blockers for the full demo):**

- **Copilot** — not supported on trial (Azure OpenAI Service unavailable on trial SKUs).
- **Fabric Data Agent** — not supported on trial (R9 §7, R11).
- **Operations Agent** and other **AI functions/services** — not supported on trial (R9 §7, R11).

> "Copilot and Trusted Workspace Access aren't supported. AI Experiences such as Data agent, AI
> functions and AI services aren't supported." — *Fabric trial capacity* docs (R9 §7).

**✅ Trial CAN be used** for the data-engineering setup: Mirrored Databricks Catalog, Direct Lake,
deployment pipelines, OneLake security, and Power BI reports.

**Recommended phased approach (R9 §8):**

1. **Phase 1 — Data engineering:** use the **free 60-day trial** for mirroring, Direct Lake,
   pipelines (Copilot/Data Agent/Operations Agent **not** available).
2. **Phase 2 — AI features:** provision **paid F64** for Copilot, Data Agent, Ontology, Operations
   Agent; use **aggressive pause/resume**.
3. **Phase 3 — Demo day:** resume F64, run the demo, **pause immediately after**.

---

## 6. Region & West US backup note (R9 §3, §8)

The demo defaults to **East US 2** (`region` in `deploy_config.json`), with **West US as the
supported backup region** (the config schema accepts `{eastus2, westus}`). Both regions:

- support **all** required Fabric workloads, Mirrored Databricks Catalog, Ontology (preview), and
  Azure Databricks; and
- process **Copilot / Data Agent AI locally** (no cross-region routing).

> If East US 2 lacks capacity/quota or a required feature at deploy time, **fall back to West US**
> (zero Fabric footnotes, local Copilot AI). The PAYG CU rate ($0.18/CU/hr) is the same in both
> regions, so the cost model above is unchanged. East US and South Central US are **rejected**
> (Operations Agent unsupported — plan §1.7).

---

## 7. Pause when idle — operational guidance

**The single most important cost control is pausing the capacity when you are not using it.**

| Action | Command | Effect |
|--------|---------|--------|
| **Pause (suspend)** when idle | `python scripts/pause_capacity.py` | Stops **compute (CU)** billing. Content becomes unavailable until resumed. OneLake storage still bills (~$1.50/mo). |
| **Resume** before a session | `python scripts/resume_capacity.py` | Returns capacity to **Active**; billing resumes at ~$11.52/hr. |
| Preview without mutating | add `--dry-run` to either script | Prints the intended action and current state; makes **no** changes. |

Both scripts are **idempotent** (re-running pause on an already-paused capacity is a no-op) and read
the capacity identity from `deploy_config.json` (`capacity.name` / `capacity.existing_capacity_id`),
authenticating via `DefaultAzureCredential` / `az login`. See `scripts/pause_capacity.py` and
`scripts/resume_capacity.py`.

> 💡 For unattended cost control, you can also wire these into an **Azure Automation runbook** on a
> schedule (auto-pause nightly / weekends) — see R9 §5.

---

### Source

All cost, region, trial, and pause/resume facts on this page are sourced to research report **R9**:
`research/2026-06-08-r9-fabric-capacity-region-cost.md` (Azure Retail Prices API + Microsoft Learn,
accessed 2026-06-08). Pricing is volatile — re-verify before committing real spend.
