# `infra/` — Azure infrastructure (Bicep)

This folder provisions the **Azure foundation** for the Zava Car Rental demo with **Bicep**
(no ARM JSON). One entry-point template (`main.bicep`) wires a handful of small modules and
exposes a single switch for each major resource so you can deploy **fresh** or **reuse
existing** Databricks / Fabric resources.

> **Region:** everything defaults to **East US 2** — the only US region that supports *all*
> required capabilities together (see [`docs/architecture.md`](../docs/architecture.md) §region
> and `plan.md` §1.7). **West US** is the documented drop-in backup.
>
> **Public repo — NO secrets.** This template never contains secrets. Storage and Unity
> Catalog access are **identity-based** (the Access Connector managed identity); demo secrets
> live in **Key Vault** and are fetched at runtime via `az login` / managed identity.

---

## What the Bicep deploys

| Module | Resource | Purpose |
|---|---|---|
| `modules/fabric-capacity.bicep` | `Microsoft.Fabric/capacities` — **F64**, East US 2 | The Fabric capacity that runs mirroring, Direct Lake, Data Agent, ontology, Real-Time Intelligence. F64 is the SKU that runs **all** AI/Copilot features (R9). |
| `modules/access-connector.bicep` | **Access Connector for Azure Databricks** (managed identity) | The Unity Catalog storage credential identity. Its managed identity is granted access to the ADLS Gen2 account so UC reads/writes data without keys. |
| `modules/databricks-workspace.bicep` | **Azure Databricks workspace** (Premium) | Premium is required for Unity Catalog (R8 §2.2). Bound to the Access Connector. |
| `modules/storage-adls.bicep` | **ADLS Gen2** (HNS) + **Blob Data Contributor** RBAC | Unity Catalog managed storage. The Access Connector identity gets the role assignment (identity-based, no account keys). |
| `modules/network-hardening.bicep` | Storage **firewall** lockdown + **Fabric trusted-workspace** resource-instance rule | ADLS hardening: default-deny firewall + a trusted-workspace rule so the Fabric Workspace Identity can reach the managed storage (needed for the Variation-2 shortcut). **Authored here, applied later** (see below). |
| `modules/keyvault.bicep` | **Key Vault** (RBAC-enabled) | Vault for demo secrets. Always deployed; never contains secrets in the template. |

Outputs (`main.bicep`) feed later phases: Databricks workspace URL/id, Access Connector id +
principal id, ADLS account name + container URI, Key Vault URI/name, Fabric capacity id/name,
and the network-hardening status flags.

---

## The fresh-vs-existing toggle

`main.bicep` has two independent switches so you can mix-and-match:

| Switch | `false` (fresh — default) | `true` (bring your own) |
|---|---|---|
| `useExistingDatabricks` | Provisions the Access Connector, Databricks workspace, and ADLS Gen2. | **Skips all Databricks resources.** You must supply `existingDatabricksWorkspaceId`, `existingDatabricksHostUrl`, `existingAccessConnectorId`, and `existingAccessConnectorPrincipalId`; their values pass straight through to the outputs. |
| `useExistingFabricCapacity` | Provisions the F64 Fabric capacity. | **Skips capacity creation.** You supply `existingFabricCapacityId`; it passes through to the output. |

Two ready-made parameter files capture the two common shapes:

- **`params/dev.bicepparam`** — **fresh** provisioning (both switches `false`). The default path.
- **`params/existing-resources.bicepparam`** — **bring-your-own** (both switches `true`) with
  `<PLACEHOLDER>` ARM ids/URLs to fill in.

> Copy either file to a **local, gitignored** `*.local.bicepparam` and replace the
> `<PLACEHOLDER>` tokens before deploying. Do **not** commit real ids.

---

## Key parameters

| Parameter | Default | Notes |
|---|---|---|
| `location` | `eastus2` | Region for all resources. Switch to `westus` for the backup region. |
| `resourcePrefix` | `zava` | Prefix used to name resources (`zava-fabric-cap`, `zava-databricks-ws`, …). |
| `fabricCapacitySku` | `F64` | Capacity SKU (R9 — F64 runs all AI features). |
| `fabricCapacityAdmins` | `[]` (set in params) | Capacity admins (UPNs / object ids). |
| `databricksSku` | `premium` | **Must** be Premium for Unity Catalog. |
| `managedStorageAccountName` | `zavauc` | ADLS Gen2 account (3–24 lowercase alphanumeric chars). |
| `keyVaultName` | `zava-kv` | RBAC Key Vault for demo secrets. |
| `applyNetworkHardening` | `false` | See **network hardening** below. |

---

## Network hardening — authored here, applied later

`network-hardening.bicep` locks the ADLS firewall to **default-deny** and adds a **Fabric
trusted-workspace** resource-instance rule so the Fabric Workspace Identity (created later in
`fabric/scripts/00_create_workspace.py`) can read the managed storage for the **Variation-2**
shortcut.

It is intentionally a **no-op on a first/standard deploy**: it only engages once a **real
Fabric workspace GUID** is supplied (`fabricWorkspaceId`) — or when you explicitly set
`applyNetworkHardening=true`. The template constructs the exact R10 §6.2 trusted-workspace
resourceId from that GUID (using the fixed Fabric subscription id), defaults the tenant to the
deployment tenant, and only then flips public network access to **Disabled** (or
selected-networks when break-glass IP/subnet rules are supplied). This two-phase design means:

1. **First deploy** (no Fabric workspace yet): storage is open enough to bootstrap — hardening
   stays off.
2. **After the Fabric workspace + Workspace Identity exist**: re-deploy `main.bicep` passing the
   workspace GUID (and optional `workspaceIdentityObjectId`) to apply the lockdown.

See [`docs/runbook-end-to-end.md`](../docs/runbook-end-to-end.md) for where this fits in the run
order, and the inline header in `main.bicep` for the full derivation logic.

---

## How to deploy

```bash
# 0) Sign in to the MCAPS subscription (identity per repo conventions).
az login
az account set --subscription "<ME-MngEnvMCAP… subscription>"

# 1) Create / pick a resource group in the chosen region.
az group create -n zava-demo-rg -l eastus2

# 2) Validate the template against your local params.
az deployment group validate \
  -g zava-demo-rg \
  -f infra/main.bicep \
  -p infra/params/dev.local.bicepparam

# 3) Deploy (fresh path).
az deployment group create \
  -g zava-demo-rg \
  -f infra/main.bicep \
  -p infra/params/dev.local.bicepparam
```

For the **bring-your-own** path, point `-p` at your filled-in
`existing-resources.local.bicepparam` instead.

> **⛔ Cost gate.** F64 bills **PAYG ~$11.52/hour while Active**. Read and acknowledge
> [`docs/cost.md`](../docs/cost.md) **before** deploying the capacity, and **pause when idle**
> with `python scripts/pause_capacity.py`.

---

## Manual steps

The Bicep itself has **no required manual UI steps** — it is fully `az deployment`-driven.
Manual actions appear only *after* infra (e.g., enabling Unity Catalog "External data access"
on the metastore for mirroring). All UI-only actions across the demo are consolidated in
[`docs/manual-steps.md`](../docs/manual-steps.md).
