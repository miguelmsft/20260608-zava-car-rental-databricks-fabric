# Manual Steps Appendix — Zava Databricks + Fabric Demo

A **single consolidated list** of every manual (non‑automatable) action required to deploy the
demo. The repo automates everything it can (Bicep, REST APIs, SDKs, notebooks, CLI); the rows
below are the residual UI/admin actions that **cannot yet be automated** and must be performed
by hand. Each row names **where** (portal path), **why**, and **who** (role).

> **How this file is maintained:** this is the **consolidated** appendix. This file is **seeded**
> by Step 2 with the prerequisite rows (tenant settings + service‑principal creation). **Later
> steps append their own rows** to the relevant section below as they introduce new manual
> actions (e.g., mirrored‑catalog OAuth, Operations Agent create). Step 26 reconciles the full
> table. **Do not remove other steps' rows.**

> **Public repo — no secrets.** Use placeholders (`<TENANT_ID>`, `<SP_APP_ID>`,
> `<SUBSCRIPTION_ID>`, `<WORKSPACE_ID>`). Never paste real secrets here.

**Legend — Who/role:** `Fabric Admin` = Fabric Administrator · `Entra Admin` = App/Entra
administrator · `Sub Owner` = Azure subscription Owner/Contributor · `Metastore Admin` =
Databricks account/metastore admin · `User` = deploying user (`admin@MngEnvMCAP…`).

---

## Table schema

Every row uses these columns:

| Column | Meaning |
|---|---|
| **Step** | Plan step that owns/introduces the manual action |
| **Action** | The exact manual action to perform |
| **Where (portal path)** | Concrete click‑path / portal URL |
| **Why** | Why it cannot be automated / why it is required |
| **Who/role** | The role that must perform it |

---

## Manual steps

### A. Prerequisites — tenant settings (Step 2)

> All of the following are in **`app.fabric.microsoft.com` → ⚙ Settings → Admin portal →
> Tenant settings**. Use the search box to find each setting by name, set it to **Enabled**, and
> scope to the SP security group where noted. See [`docs/prerequisites.md` §3](./prerequisites.md#3-fabric-tenant-settings).

| Step | Action | Where (portal path) | Why | Who/role |
|---|---|---|---|---|
| 2 | Confirm/Enable **Service principals can call Fabric public APIs** (scoped to SP group) | Admin portal → Tenant settings → **Developer settings** | SP can't call Fabric public REST APIs otherwise (R7) | Fabric Admin |
| 2 | Enable **Service principals can create workspaces, connections, and deployment pipelines** (scoped to SP group) | Admin portal → Tenant settings → **Developer settings** | SP must create workspaces/connections/pipelines (R7) | Fabric Admin |
| 2 | Enable **Service principals can access read‑only admin APIs** (scoped to SP group) | Admin portal → Tenant settings → **Admin API settings** | Preflight read‑only admin checks (R7) | Fabric Admin |
| 2 | Enable **Service principals can access admin APIs used for updates** (scoped to SP group) | Admin portal → Tenant settings → **Admin API settings** | Admin API update operations (R7) | Fabric Admin |
| 2 | Enable **Enable new mirrored catalog items (preview)** | Admin portal → Tenant settings → **Microsoft Fabric** | Required to create Mirrored Azure Databricks Catalog, Step 11 (R1) | Fabric Admin |
| 2 | Enable **Users can use Copilot and other features powered by Azure OpenAI** | Admin portal → Tenant settings → **Copilot and Azure OpenAI Service** | Gates Data Agent (Step 17) + Operations Agent (Step 20); also the only Data‑Agent gate — no separate Data‑agent toggle exists (R3, R4) | Fabric Admin |
| 2 | Enable **Capacities can be designated as Fabric Copilot capacities** | Admin portal → Tenant settings → **Copilot and Azure OpenAI Service** | Designate the capacity for Copilot/Data Agent usage (R3) | Fabric Admin |
| 2 | Enable **Users can create Ontology (preview) items** | Admin portal → Tenant settings → **Microsoft Fabric** | Fabric IQ Ontology layer, Step 16 (R4) | Fabric Admin |
| 2 | Enable **User can create Graph** | Admin portal → Tenant settings → **Microsoft Fabric** | Graph (GA) the ontology auto‑creates, Step 16 (R11) | Fabric Admin |
| 2 | (Optional) Enable **All Power BI users can see "Set alert" button to create Fabric Activator alerts** | Admin portal → Tenant settings → **Microsoft Fabric** | Surfaces the "Set alert" button for Activator alerts, Step 19 (R11). No dedicated "Activator" toggle exists — Activator just needs an F‑capacity workspace | Fabric Admin |
| 2 | Enable **Enable Operations Agents (Preview)** — *only if `enable_operations_agent=true`* | Admin portal → Tenant settings → **Microsoft Fabric** | Optional Teams enhancement, Step 20 (R11 §8) | Fabric Admin |
| 2 | Enable **Users can synchronize workspace items with their Git repositories** | Admin portal → Tenant settings → **Git integration** | Git integration / `fabric-cicd` ALM (R7) | Fabric Admin |
| 2 | Enable **Users can access data stored in OneLake with apps external to Fabric** | Admin portal → Tenant settings → **OneLake settings** | External‑app OneLake access + Policy Weaver, Step 21 (R1, R5). OneLake data‑access roles are configured per item, not via a tenant toggle | Fabric Admin |
| 2 | Enable **Allow users to apply sensitivity labels for content** | Admin portal → Tenant settings → **Information protection** | Purview sensitivity labels + DLP across Fabric/Power BI, Step 22 (R6). Purview live view / tenant scans are configured on the Purview side | Fabric Admin |

> **Cross‑geo AI settings are NOT enabled for East US 2** (US region). Enable them only for a
> non‑US/non‑EU capacity. See [`docs/prerequisites.md` §3.3](./prerequisites.md#33-copilot--azure-openai--data-agent--group-copilot-and-azure-openai-service-r3-r4).

### B. Prerequisites — service‑principal & identity setup (Step 2)

| Step | Action | Where (portal path) | Why | Who/role |
|---|---|---|---|---|
| 2 | Create Entra **app registration** `zava-fabric-deploy-sp` (`az ad app create`) | Entra admin center → **App registrations** → New, or `az ad` CLI | Deploy SP identity for Bicep/Fabric automation (R7) | Entra Admin |
| 2 | Create the **service principal** for the app (`az ad sp create`) | Entra → **Enterprise applications**, or `az ad` CLI | SP object for the app registration (R7) | Entra Admin |
| 2 | Create a **client secret** → store in **Key Vault** (never in repo) | Entra → App registration → **Certificates & secrets** | Auth credential; public repo forbids committing it | Entra Admin |
| 2 | Assign **Contributor** (or Owner) on the target resource group to the SP | Azure portal → Resource group → **Access control (IAM)**, or `az role assignment` | SP must provision Azure resources (R7) | Sub Owner |
| 2 | Create **Entra security group** `zava-fabric-api-sps` and add the SP as a member | Entra admin center → **Groups** → New, or `az ad group` CLI | Fabric API tenant settings are scoped to a security group (R7 §7) | Entra Admin |
| 2 | **Scope** the four SP API toggles (Section A — two under Developer settings, two under Admin API settings) to `zava-fabric-api-sps` | Admin portal → Tenant settings → **Developer settings** + **Admin API settings** → *Specific security groups* | Least‑privilege SP API access (R7) | Fabric Admin |
| 2 | Add the deploy SP as **Admin/Member** of each Fabric workspace | Fabric workspace → **Manage access** (done/confirmed in Step 10) | SP needs workspace‑level access to deploy items (R7) | Fabric Admin |

> **Setup order:** app registration → SP → security group + membership → scope tenant settings →
> add SP to workspace. See [`docs/prerequisites.md` §4](./prerequisites.md#4-service-principals--identities).

### C. Databricks‑side prerequisites (introduced by Step 2; configured in Steps 6/8)

| Step | Action | Where (portal path) | Why | Who/role |
|---|---|---|---|---|
| 2 | Enable **External data access** on the Unity Catalog metastore | Databricks workspace → **Catalog** → ⚙ gear → **Metastore** → **Details** tab → enable **External data access** | Disabled by default; required for Fabric mirroring/credential vending (R8) | Metastore Admin |

> The `EXTERNAL USE SCHEMA` / `USE CATALOG` / `USE SCHEMA` / `SELECT` grants are executed as SQL
> by Step 8 (automatable), not manual; see [`docs/prerequisites.md` §7](./prerequisites.md#7-databricks-side-prerequisites).

---

<!-- Later steps append their manual-action rows below this line, grouped by step.
     Keep the table schema (Step | Action | Where (portal path) | Why | Who/role).
     Known upcoming manual actions (placeholders to be filled by their owning steps):
       - Step 11: one-time OAuth UI sign-in for Mirrored Azure Databricks Catalog (no SP support — R1/R7)
       - Step 20: Operations Agent create + Teams action wiring (user-token only, UI-assisted — R11)
       - Step 22: Microsoft Purview domains / scans / DLP policy setup (R6)
     Do NOT pre-fill these here in Step 2 — each owning step adds its own rows. -->

*End of seeded prerequisite rows. Later steps append below.*

### D. Fabric workspace + Workspace Identity (Step 10)

> Step 10 (`fabric/scripts/00_create_workspace.py`) automates workspace create/attach, F64
> capacity assignment, and Workspace Identity provisioning via the Fabric REST API
> (`POST /v1/workspaces/{id}/provisionIdentity`). The rows below are the residual UI/consent
> actions that the REST path **may** require depending on the tenant (R7/R10).

| Step | Action | Where (portal path) | Why | Who/role |
|---|---|---|---|---|
| 10 | Create the **Fabric Workspace Identity** *if the REST `provisionIdentity` call is unsupported for the SP in your tenant* | Fabric workspace → **Workspace settings** → **Workspace identity** → **+ Workspace identity** | Workspace Identity REST provisioning can require one-time tenant/admin consent and is **not always service-principal-supported** (R7/R10). The script falls back to this UI step and logs it | Fabric Admin |
| 10 | Capture the Workspace Identity **object id** into `workspace.identity_object_id` in `deploy_config.json` | Fabric workspace → **Workspace settings** → **Workspace identity** (copy the identity's object id) | The object id does not exist until the identity is created; Step 12 binds the ADLS resource-instance / trusted-workspace rule to it (R10) | Fabric Admin |

