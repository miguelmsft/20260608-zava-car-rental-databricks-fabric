<!--
  NOTE: This README is an initial skeleton created during the planning phase.
  It will be expanded after the implementation plan is approved and finalized
  after implementation (with real endpoints, setup steps, and run commands).
-->

# Zava Car Rental — Accelerating a Databricks Data Foundation with Microsoft Fabric

> **Status: 🚧 Under construction.** This repository is being built. Content below is a placeholder and will be completed as the demo is implemented.

## The scenario
**Zava** is a (fictional) large car-rental company headquartered in **Seattle, WA**, with rental sites across multiple US cities. Zava's **data engineering team** builds its data foundation on **Azure Databricks**; Zava's **business users** live in **Power BI**. Zava wants to know **how Microsoft Fabric can accelerate their data foundation** — without ripping out Databricks — and how **Microsoft Purview** can give them end-to-end governance.

This demo shows that integration end-to-end: from a **certified data asset** produced by Databricks, to **governed, near-real-time business insight** in Fabric and Power BI.

## What this demo shows (four pillars)
1. **No-copy mirroring** — mirror Databricks **Unity Catalog** into Fabric **OneLake** (zero ETL).
2. **Direct Lake reporting** — **Power BI Direct Lake** over the mirrored data → an impressive report.
3. **Ontology insights** — a Fabric **semantic/ontology layer** over Zava's business entities, surfaced for **natural-language insights** beyond Power BI.
4. **Governance** — **Policy Weaver** syncs Unity Catalog access policies to Fabric; **Microsoft Purview** delivers end-to-end lineage, cataloging, classification, and DLP.

## Business value
- **Agility** — faster insights from data.
- **Observability** — see and track your data pipelines; automate environments.
- **Efficiency** — one governed copy of data instead of a spaghetti of access and duplication.
- **Governance** — consistent, end-to-end security and governance.

## How to run the demo
_Coming soon — full, scripted deployment instructions (Bicep + Python + Fabric/Databricks APIs) plus any documented manual steps._

## Repository layout
_Coming soon._

---
*Zava is a fictional company used for demonstration purposes only. All data in this repository is synthetic.*
