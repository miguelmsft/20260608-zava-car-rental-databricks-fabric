# Zava Car Rental — Databricks + Fabric Data Foundation Demo

## What this repo is
A **public, reusable demo** showing how **Azure Databricks** and **Microsoft Fabric** integrate to form a modern, governed data foundation. Built for a fictional company, **Zava**, a large car-rental company headquartered in **Seattle, WA**, with sites across multiple US cities. Zava's data engineering team uses Azure Databricks; business users use Power BI. The demo shows how Fabric accelerates their data foundation and how **Microsoft Purview** provides end-to-end governance.

Customers clone this repo and **redeploy the demo in their own environment**, so **everything must be as programmatic and reusable as possible** (Bicep, REST APIs, SDKs, notebooks, CLI). Manual UI steps are allowed **only** where automation is not yet possible, and must be clearly documented.

## The four demo pillars (equal weight)
1. **No-copy mirroring** — mirror Azure Databricks Unity Catalog into Fabric **OneLake** (zero ETL, near-real-time).
2. **Direct Lake reporting** — Power BI **Direct Lake** over the mirrored data, with a thin consumption-layer calculation, feeding an impressive report.
3. **Ontology insights** — a Fabric **semantic/ontology layer** over the business entities (sites, vehicles, customers, rentals) surfaced for **natural-language insights** (e.g., a **Fabric Data Agent**) beyond Power BI.
4. **Governance** — **Policy Weaver** syncs Databricks Unity Catalog access policies to Fabric; **Microsoft Purview** provides end-to-end lineage, cataloging, classification, and DLP.

The Databricks medallion pipeline (raw → bronze → silver → gold → **certified data asset**) is intentionally **simple** — it's the setup, not the star. The **integration and Fabric side are the focus**.

## Business value to convey (the "why")
- **Agility** — faster insights from data.
- **Observability** — identify/track data pipelines, automate environments.
- **Efficiency** — remove the spaghetti of access/data duplication; technical development agility.
- **Governance** — consistent, end-to-end security and governance.
- It's about the **value** customers get, not the tools. Tools are the medium.

## Audience
Mixed: data engineers, BI/business users, and decision makers. The demo must be **impressive yet simple** and easy to follow.

## Technology defaults & conventions
- **Cloud:** Azure. Default subscription is the MCAPS one (name starts with `ME-MngEnvMCAP`). Identity: `admin@MngEnvMCAP422553.onmicrosoft.com`. Do **not** use `migmartinez@microsoft.com` / HNLI-DEV.
- **Region:** a US region with availability for **ALL** required services/features (finalized in research). Prefer **East US 2 / West US 3**.
- **IaC:** **Bicep** for Azure resources (not ARM JSON). A Fabric **Terraform** provider may be used where it is the most reliable programmatic option for Fabric artifacts (research will decide).
- **Databricks as code:** Databricks **Asset Bundles** + notebooks + **Unity Catalog**; workspace provisioned via Bicep where possible.
- **Fabric as code:** Fabric **REST APIs** / **`fabric-cli`** / **`semantic-link-labs`**; deployment pipelines + Git integration where useful.
- **Languages:** **Python** (primary scripting), **Bicep**, **SQL**, **PySpark** (Databricks notebooks).
- **Provisioning flexibility:** deployment must allow provisioning Databricks **and/or** Fabric fresh, **OR** using **existing** Databricks/Fabric resources if provided.
- **Company:** Zava (fictional). Industry: car rental. **Branding: blue tones.**
- **Security:** **Public repo — NO secrets committed.** Use Key Vault, OIDC / managed identity, service principals, and placeholders. Redact secrets in all docs/logs.

## Report & data conventions
- Synthetic Zava data entities: **Sites, Vehicles, VehicleClasses, Customers, Reservations, Rentals, Payments, Maintenance, Telematics**.
- Report wow-factors: **multi-city maps, decomposition trees, forecasting**; blue Zava theme.
- Show how a **certified data asset** in Databricks becomes governed business insight via OneLake / Fabric / Power BI.

## Repo conventions
- Keep code **reusable and parameterized** — no hardcoded resource names; use parameter files / env config.
- **Document every manual click-through step** where automation isn't possible.
- Each major component gets a clear, easy-to-understand README.

## Orchestration artifacts (not part of the shipped demo, gitignored)
- `demo-status.md` — orchestration state.
- `research/` — research reports (working material).
- `agent-reviews/` — QA review files.
- `plan.md` — implementation plan (source of truth during build).
