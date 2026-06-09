# Prerequisites — Zava Databricks + Fabric Demo

Everything a customer needs to have in place **before** running the deployment. This is a
**one‑time setup** per tenant/subscription. Most items are admin‑portal actions that cannot
yet be automated; they are documented here and cross‑listed in
[`docs/manual-steps.md`](./manual-steps.md) (the consolidated Manual Steps Appendix).

> **Public repo — no secrets.** All identifiers in this document are placeholders such as
> `<TENANT_ID>`, `<SUBSCRIPTION_ID>`, `<SP_APP_ID>`. Never commit real secrets, client
> secrets, PATs, or connection strings. Acquire secrets at runtime (`az login`, Key Vault).

---

## 0. Quick checklist

| # | Prerequisite | Who | Section |
|---|---|---|---|
| 1 | Azure subscription (MCAPS `ME-MngEnvMCAP…`) + roles | Subscription Owner/Contributor | [§1](#1-azure-subscription--identity) |
| 2 | Fabric Admin rights (to set tenant settings) | Fabric Administrator | [§2](#2-fabric-administrator-rights) |
| 3 | Fabric tenant settings enabled (§3.4 of plan) | Fabric Administrator | [§3](#3-fabric-tenant-settings) |
| 4 | Deploy service principal + Fabric‑API security group | Entra App Admin + Fabric Admin | [§4](#4-service-principals--identities) |
| 5 | Region (East US 2) + capacity (F64) decision | Deployer | [§5](#5-region--capacity) |
| 6 | Local tooling installed at pinned versions | Deployer | [§6](#6-tooling--pinned-versions) |
| 7 | Databricks‑side prerequisites (UC, Premium, external data access) | Databricks Account/Metastore Admin | [§7](#7-databricks-side-prerequisites) |
| 8 | Fresh‑vs‑existing decision + cost checkpoint acknowledged | Deployer | [§8](#8-deployment-decisions) |

---

## 1. Azure subscription & identity

- **Subscription:** the MCAPS subscription whose name starts with **`ME-MngEnvMCAP`**
  (e.g., `ME-MngEnvMCAP422553`). Do **not** use `migmartinez@microsoft.com` / HNLI‑DEV.
- **Identity:** `admin@MngEnvMCAP422553.onmicrosoft.com` (the deploying user). This user is
  also used for the **user‑token‑only** operations that have no service‑principal support
  (mirrored Databricks catalog create, Operations Agent create — see §4 and the Manual Steps
  Appendix).
- **Azure RBAC roles required for the deploying identity:**
  - **Owner** *or* **Contributor + User Access Administrator** on the target resource group
    (Contributor alone creates resources; UAA is needed to assign roles to managed identities
    such as the Databricks Access Connector and Fabric Workspace Identity).
  - If you can only obtain **Contributor**, the role assignments in Steps 6/10/12 must be
    performed separately by an Owner.
- **Fabric/Power BI role:** the deploying user must additionally be a **Fabric Administrator**
  (§2) to toggle tenant settings, and a workspace **Admin/Member** on each Fabric workspace.

Verify access (read‑only, Phase 0 does **not** deploy):

```bash
az login                       # interactive; uses admin@MngEnvMCAP…
az account show                # confirm the ME-MngEnvMCAP… subscription is selected
az account set --subscription "<SUBSCRIPTION_ID>"
```

---

## 2. Fabric Administrator rights

Setting the tenant settings in §3 and creating the SP security‑group scoping (§4) requires
the **Fabric Administrator** role (or Power Platform / Global Administrator). For this demo
the deploying user already has these rights.

- **Admin portal location:** [https://app.fabric.microsoft.com](https://app.fabric.microsoft.com)
  → **Settings** (gear icon, top right) → **Admin portal** → **Tenant settings**.
- Tenant settings can be scoped **for the entire organization** or **for specific security
  groups**. For this demo, scope the service‑principal settings to the dedicated security
  group created in §4 (least privilege), and the AI/feature settings to either the org or the
  same group.

> **Doc‑lag note:** Several portal toggles may still display a **"(preview)"** label even
> though the feature reached **GA at Microsoft Build 2026** (Operations Agent and Graph are GA;
> only **Ontology** remains genuinely preview). The "(preview)" suffix in the UI lags the
> announcement — enable the setting regardless of the label (R11 §8, R4).

---

## 3. Fabric tenant settings

Enable **all** of the following in **Admin portal → Tenant settings**. Each row gives the
group heading you will find it under, the exact setting name, why it is needed, and the
research source. These map to plan §3.4 and reports R1, R3, R4, R6, R7, R11.

> **Where:** `app.fabric.microsoft.com` → ⚙ **Settings** → **Admin portal** → **Tenant settings**.
> Use the **search box** at the top of the Tenant settings page to find a setting by name,
> then set the toggle to **Enabled** and (where applicable) scope it to your SP security group.

### 3.1 Service‑principal API access (R7)

The SP API toggles live in **two different groups** of Tenant settings. The public‑API and
creation toggles are under **Developer settings**; the admin‑API toggles are under
**Admin API settings**. (Verified against the current Tenant settings index — see sources.)

**Group: Developer settings**

| Setting (exact name) | Why | Scope |
|---|---|---|
| **Service principals can call Fabric public APIs** | Lets the deploy SP call the Fabric public REST APIs (item CRUD, workspace mgmt). Enabled by default for new tenants — confirm it is **On** and scoped to your group | Your SP security group (§4) |
| **Service principals can create workspaces, connections, and deployment pipelines** | Lets the deploy SP create workspaces, connections, and deployment pipelines. Disabled by default — must be enabled | Your SP security group |

**Group: Admin API settings**

| Setting (exact name) | Why | Scope |
|---|---|---|
| **Service principals can access read‑only admin APIs** | Read‑only admin API access (preflight checks) | Your SP security group |
| **Service principals can access admin APIs used for updates** | Admin API access for update operations | Your SP security group |

Source: [Tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index),
[Developer tenant settings](https://learn.microsoft.com/en-us/fabric/admin/service-admin-portal-developer),
[Admin API tenant settings](https://learn.microsoft.com/en-us/fabric/admin/service-admin-portal-admin-api-settings) (R7 §7).

### 3.2 Mirroring — group: **Microsoft Fabric** (R1)

| Setting (exact name) | Why | Scope |
|---|---|---|
| **Enable new mirrored catalog items (preview)** | Required to create the **Mirrored Azure Databricks Catalog** (Variation 1). Without it, mirror creation fails | Org or SP/user group |

Source: [Fabric tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index),
[Mirroring Azure Databricks Unity Catalog](https://learn.microsoft.com/en-us/fabric/database/mirrored-database/azure-databricks) (R1).

### 3.3 Copilot / Azure OpenAI + Data Agent — group: **Copilot and Azure OpenAI Service** (R3, R4)

| Setting (exact name) | Why | Scope |
|---|---|---|
| **Users can use Copilot and other features powered by Azure OpenAI** | Enables Copilot‑powered features **including the Fabric Data Agent** (Step 17) and the Operations Agent (Step 20). This is the toggle that gates Data Agent — there is **no separate "Data agent" tenant setting** | Org or AI user group |
| **Capacities can be designated as Fabric Copilot capacities** | Lets capacity admins designate the capacity for Copilot/Data Agent usage and billing consolidation | Org |

> **Data Agent note:** the current Tenant settings index has **no dedicated "create/share Data
> agent" toggle**. Data Agent creation is gated by **Users can use Copilot and other features
> powered by Azure OpenAI** above (plus a Copilot‑eligible capacity). Enabling that setting is
> sufficient for Step 17.

Cross‑geo settings — **NOT required for East US 2** (capacity is in a US region), enable only
if you deploy to a non‑US/non‑EU region:

| Setting | Needed when |
|---|---|
| **Data sent to Azure OpenAI can be processed outside your capacity's geographic region…** | Capacity outside US/EU |
| **Data sent to Azure OpenAI can be stored outside your capacity's geographic region…** | Capacity outside US/EU |
| **Conversation history stored outside your capacity's geographic region…** | Fully conversational agents outside US/EU |

Source: [Configure Fabric data agent tenant settings](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-tenant-settings),
[Tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index) (R3, R4).

### 3.4 Ontology + Graph (Fabric IQ) — group: **Microsoft Fabric** (R4, R11)

| Setting (exact name) | Why | Scope |
|---|---|---|
| **Users can create Ontology (preview) items** | Enables the Fabric IQ **Ontology** layer (Step 16). Genuinely preview | Org or AI user group |
| **User can create Graph** | Enables the **Graph (GA)** that the ontology auto‑creates (Step 16). Exact index name is "User can create Graph" (singular "User") | Org or AI user group |

Source: [Tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index),
[Tutorial Part 0 — Ontology environment setup](https://learn.microsoft.com/en-us/fabric/iq/ontology/tutorial-0-introduction) (R4).

### 3.5 Real‑Time Intelligence, Activator & Operations Agent — group: **Microsoft Fabric** (R11)

There is **no tenant setting literally named "Fabric Activator" / "Reflex".** Activator
alerting works on any **Fabric‑capacity (F‑SKU) workspace** for users with permission to
create Fabric items — no dedicated tenant toggle is required for the default Email alert path
(Step 19). The only Activator‑related tenant toggle is the "Set alert" button below. The
Operations Agent **does** have its own tenant toggle.

| Setting (exact name) | Why | Scope |
|---|---|---|
| **All Power BI users can see "Set alert" button to create Fabric Activator alerts** | Surfaces the **Set alert** button in Power BI reports so users can create Fabric Activator alerts. Optional for Step 19 (alerts can also be created directly from Real‑Time hub / Activator) | Org |
| **Enable Operations Agents (Preview)** | Enables creation of **operations agents** (Step 20) — the optional Teams‑requiring enhancement. By enabling it you accept the Preview Terms. Enable **only** if `features.enable_operations_agent=true` | Org or AI user group |

> The Operations Agent additionally needs **Copilot + Azure OpenAI** (§3.3) and a **Microsoft
> Teams account** (§6). It is **not** supported on trial capacities (R11 §3). The default
> Activator email path (Step 19) needs **no** Teams, **no** Copilot, and **no** dedicated tenant
> toggle — only an **F‑capacity workspace** and Fabric item‑create permission.

Source: [Tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index),
[Create and configure operations agents](https://learn.microsoft.com/en-us/fabric/real-time-intelligence/operations-agent),
[Create a rule in Fabric Activator](https://learn.microsoft.com/en-us/fabric/real-time-intelligence/data-activator/activator-create-activators) (R11 §3, §6, §8).

### 3.6 Git integration & deployment pipelines (R7)

| Setting (exact name) | Group | Why |
|---|---|---|
| **Users can synchronize workspace items with their Git repositories** | **Git integration** | Enables Git integration for Fabric items (`fabric-cicd`, ALM) |

> **Deployment pipelines:** the current Tenant settings index has **no "Users can create
> deployment pipelines" toggle** — deployment‑pipeline creation is GA and not gated by a named
> user tenant setting. For the **service principal** to create pipelines, the §3.1 Developer
> setting **Service principals can create workspaces, connections, and deployment pipelines**
> is what is required.

Source: [Tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index) (R7).

### 3.7 OneLake external access — group: **OneLake settings** (R1, R5)

| Setting (exact name) | Why |
|---|---|
| **Users can access data stored in OneLake with apps external to Fabric** | Enables external‑app access to OneLake data used by Policy Weaver and external clients (Step 21) |

> **OneLake security note:** OneLake **data‑access roles** (the "OneLake security" feature) are a
> **workspace/item‑level** capability configured per item — there is **no tenant setting literally
> named "OneLake security (preview)"** in the current index. The named tenant toggle here is
> **Users can access data stored in OneLake with apps external to Fabric**.

Source: [Tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index),
[OneLake data access control model (preview)](https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model) (R1, R5).

### 3.8 Purview / governance — group: **Information protection** (R6)

| Setting (exact name) | Why |
|---|---|
| **Allow users to apply sensitivity labels for content** | Enables Microsoft Purview Information Protection sensitivity labels (and label‑based DLP) on Fabric/Power BI content (Step 22) |

> **Purview live view / tenant scans note:** Microsoft Purview's **live view** and **tenant
> scans** for Fabric lineage/catalog are configured on the **Microsoft Purview side** (Data Map /
> Unified Catalog), not via a single Fabric tenant toggle of that name. The named Fabric tenant
> setting in this group is the sensitivity‑label one above; complete the Purview‑side prerequisites
> before enabling it.

Source: [Tenant settings index](https://learn.microsoft.com/en-us/fabric/admin/tenant-settings-index),
[Connect to and manage Azure Databricks Unity Catalog in Microsoft Purview](https://learn.microsoft.com/en-us/purview/register-scan-azure-databricks-unity-catalog) (R6 §1, §5).

---

## 4. Service principals & identities

Five distinct identities are used across the demo (plan §3.5). Only the **deploy SP** must be
created up front in this prerequisites phase; the others are created by later steps (Bicep /
scripts) and are listed here so the customer understands the full identity model.

### 4.1 Deploy service principal (create now)

Used by Bicep/Fabric automation (`fab`, `fabric-cicd`) for unattended deployment.

**Create the Entra app registration + SP (placeholders only):**

```bash
# 1. Create the app registration (returns appId = <SP_APP_ID>)
az ad app create --display-name "zava-fabric-deploy-sp"

# 2. Create the service principal for that app
az ad sp create --id "<SP_APP_ID>"

# 3. Create a client secret (DO NOT COMMIT — store in Key Vault / use at runtime only)
az ad app credential reset --id "<SP_APP_ID>" --display-name "deploy" --years 1
#    -> capture appId, password (secret), tenant into Key Vault, never into the repo
```

**Azure RBAC:** assign **Contributor** on the target resource group (Owner only if it must
create role assignments):

```bash
az role assignment create \
  --assignee "<SP_APP_ID>" \
  --role "Contributor" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>"
```

**Fabric API access (the critical part):** the deploy SP gets Fabric access **only via a
security group** that is scoped in the §3.1 tenant settings — *not* via API permissions on the
app registration.

```bash
# Create a security group for Fabric-API SPs and add the deploy SP as a member
az ad group create --display-name "zava-fabric-api-sps" \
  --mail-nickname "zava-fabric-api-sps"
az ad group member add --group "zava-fabric-api-sps" \
  --member-id "$(az ad sp show --id <SP_APP_ID> --query id -o tsv)"
```

Then (Fabric Admin) scope the four SP API toggles in §3.1 — two under **Developer settings**
("Service principals can call Fabric public APIs", "Service principals can create workspaces,
connections, and deployment pipelines") and two under **Admin API settings** ("Service
principals can access read‑only admin APIs", "Service principals can access admin APIs used for
updates") — to this `zava-fabric-api-sps` group, and add the SP as **Admin/Member** of each
Fabric workspace (Step 10).

> **Setup order (R7 §7):** (1) create app registration → (2) create SP → (3) create security
> group + add SP → (4) enable & scope tenant settings to the group → (5) add SP to each target
> workspace.

### 4.2 Other identities (created by later steps — listed for awareness)

| Identity | Created in | Purpose | Source |
|---|---|---|---|
| **Databricks Access Connector managed identity** | Step 6 (Bicep) | UC storage credential to ADLS Gen2 | R8 |
| **Fabric Workspace Identity** | Step 10 | Trusted‑workspace access to hardened ADLS Gen2 (Variation 2) | R10 |
| **Policy Weaver identity** (SP) | Step 21 | Databricks SDK read + Microsoft Graph (identity resolution) + Fabric `dataAccessRoles` write | R5 |
| **User identity** (`admin@MngEnvMCAP…`) | n/a (existing) | User‑token‑only ops: mirrored catalog create (R1), Operations Agent create (R11) | R1, R7, R11 |

**Policy Weaver identity API permissions (Step 21, for reference):** Microsoft Graph
`Directory.Read.All` (or equivalent) for identity resolution, Databricks account+workspace API
access, and Fabric write access to `dataAccessRoles`. Created and scoped in Step 21 — no action
needed now.

---

## 5. Region & capacity

- **Region: East US 2.** The only US region where **every** required capability is available
  *together*, including the optional **Operations Agent (GA)**, with **local** Azure OpenAI
  processing for Copilot/Data Agent (no cross‑region routing). Verification matrix in plan §1.7.
  - **East US is rejected** — Operations Agent excludes East US (R11 §8).
  - **South Central US is rejected** — Ontology unavailable (R9).
  - **West US** is the documented drop‑in backup region (all capabilities ✅ local).
- **Capacity: F64** (`Microsoft.Fabric/capacities`, SKU `F64`) in East US 2.
  - Pay‑as‑you‑go ≈ **$11.52/hour** ($0.18/CU/hour × 64 CUs) (R9).
  - F64 natively supports Copilot + Data Agent without the **Fabric Copilot Capacity**
    workaround required below F64 (R9 §2).
  - **Pause/resume** the capacity to control cost — see `scripts/pause_capacity.py` /
    `resume_capacity.py` and `docs/cost.md` (Step 4 cost checkpoint).
- **Trial capacity caveat:** a free **60‑day Fabric trial capacity** (F4/F64) can run the
  **non‑AI** data‑engineering phases only. It does **not** support Copilot, Data Agent, or the
  Operations Agent — so it cannot run the full demo (R9 §7, R11 §3).

---

## 6. Tooling — pinned versions

Install on the deployer machine. Versions are **pinned tested minimums** (not `latest`) to
reduce drift on preview APIs (plan §7). Capture exact resolved versions in a lockfile during
deployment.

| Tool / package | Pinned minimum | Purpose | Install |
|---|---|---|---|
| **Azure CLI** | ≥ 2.61.0 | Provision Azure resources; acquire Fabric user tokens | [docs](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| **Bicep CLI** | ≥ 0.28.0 | Build / what‑if Azure modules | `az bicep install` |
| **Python** | ≥ 3.11, < 3.13 | Primary scripting; required by Policy Weaver | [python.org](https://www.python.org/) |
| **`ms-fabric-cli` (`fab`)** | ≥ 1.1.0 | Fabric REST/CLI automation (workspace, Eventhouse) | `pip install ms-fabric-cli` |
| **`fabric-cicd`** | ≥ 0.1.14 | Deploy Fabric items (report, Data Agent ALM) from repo | `pip install fabric-cicd` |
| **`semantic-link-labs` (`sempy_labs`)** | ≥ 0.8.0 | Direct Lake model generation + TOM | `pip install semantic-link-labs` |
| **`policy-weaver`** | **== 0.4.0** (exact pin, Beta) | UC access → OneLake security | `pip install policy-weaver==0.4.0` |
| **Databricks CLI** | ≥ 0.218.0 | UC SQL + Asset Bundles (DABs) | [docs](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/cli/) |
| **Databricks Terraform provider** *(optional)* | ~> 1.49 | UC objects alternative to CLI/SQL | terraform registry |
| **`requests`** | ≥ 2.31.0 | Fabric REST calls (Data Agent, Activator, Eventhouse) | `pip install requests` |
| **Power BI Desktop** | ≥ 2.130 (2026 release) | Author PBIP/PBIR advanced visuals | [download](https://powerbi.microsoft.com/desktop/) |
| **Microsoft Purview** portal/REST/PowerShell | n/a (SaaS) | Governance scans, labels, DLP | purview.microsoft.com |

> **Microsoft Teams account is OPTIONAL** — required **only** for the optional Operations Agent
> enhancement (Step 20). The default Fabric Activator **Email** alert path (Step 19) needs no
> Teams (R11 §6).

### 6.1 Set up the Python environment (one command)

The **pip** packages above are captured in the tracked **[`requirements.txt`](../requirements.txt)**
at the repo root. Use the cross‑platform bootstrap script to create a virtual environment
(`.venv`) and install them all in **one command**. It is **idempotent** (re‑running reuses an
existing `.venv`) and **prefers [`uv`](https://docs.astral.sh/uv/)** when present — `uv`
provisions Python **3.12** automatically even if the host only has a newer interpreter — and
falls back to a system Python **3.12/3.11**:

```powershell
# Windows (PowerShell)
.\scripts\setup_env.ps1            # optional: -PythonVersion 3.11   |   -Help
.venv\Scripts\Activate.ps1
python scripts\preflight_checks.py
```

```bash
# macOS / Linux (bash)
./scripts/setup_env.sh             # optional: --python-version 3.11 |   --help
source .venv/bin/activate
python scripts/preflight_checks.py
```

> **Python contract: ≥ 3.11, < 3.13.** Policy Weaver (`policy-weaver==0.4.0`, Beta) does not
> support Python 3.13+ or 3.10‑. The scripts **validate** the interpreter and, when only an
> unsupported version is available and `uv` is absent, **fail with an actionable message**
> (install `uv`, or install Python 3.12) rather than building a broken venv.

**If you don't have `uv`,** install it once (recommended — it handles the Python version for you):

```powershell
winget install --id=astral-sh.uv -e        # Windows
# or:  irm https://astral.sh/uv/install.ps1 | iex
```

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# or:  brew install uv
```

#### Tools that are NOT pip (install separately)

`requirements.txt` covers only Python packages. The following are standalone tools and must be
installed via their own installers (rows in the table above):

| Tool | Install |
|---|---|
| **Databricks CLI** | `winget install Databricks.DatabricksCLI` (Windows) · `brew install databricks/tap/databricks` (macOS) · [docs](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/cli/) |
| **Azure CLI** (`az`) | [install docs](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) — confirm with `az version` |
| **Bicep** | `az bicep install` — confirm with `az bicep version` |
| **Power BI Desktop** | [download](https://powerbi.microsoft.com/desktop/) (Windows GUI — PBIP/PBIR authoring) |

---

## 7. Databricks‑side prerequisites

Required for the Mirrored Azure Databricks Catalog (Variation 1) and the secured shortcut
(Variation 2). On the **fresh** path these are configured by Steps 6/8/9; on the **existing**
path the customer must ensure they are already true (preflight validates them). Source: R8.

1. **Unity Catalog enabled** — automatically enabled for all Azure Databricks workspaces
   created after **2023‑11‑09**. Existing older workspaces must have UC enabled and a metastore
   attached.
2. **Premium SKU** — UC requires the **Premium** workspace SKU (`workspace.sku = "premium"`).
   Standard SKU is not supported.
3. **External data access enabled on the metastore** — disabled by default. A **metastore
   admin** must enable it (Catalog → ⚙ gear → **Metastore** → **Details** tab → enable
   **External data access**). No Bicep/Terraform resource exists; this is a UI/REST action.
   Source: [Enable external data access to Unity Catalog](https://learn.microsoft.com/en-us/azure/databricks/external-access/admin).
4. **Grants for the Fabric connecting principal** on the target catalog/schemas:
   `EXTERNAL USE SCHEMA` + `USE CATALOG` + `USE SCHEMA` + `SELECT`. `EXTERNAL USE SCHEMA` must
   be granted **explicitly by the catalog owner** (it is excluded from `ALL PRIVILEGES`):

   ```sql
   GRANT USE CATALOG        ON CATALOG  zava        TO `<fabric-connector-identity>`;
   GRANT USE SCHEMA         ON SCHEMA   zava.gold   TO `<fabric-connector-identity>`;
   GRANT SELECT             ON SCHEMA   zava.gold   TO `<fabric-connector-identity>`;
   GRANT EXTERNAL USE SCHEMA ON CATALOG zava        TO `<fabric-connector-identity>`;
   ```

5. **Databricks roles:** an **Account Admin** (or Metastore Admin) is needed to enable external
   data access and create the metastore; a **Workspace Admin** to run UC SQL and deploy bundles.

> **Note (R8):** Fabric metadata sync covers schema/table **additions and deletions only** —
> Unity Catalog **tags and comments do not propagate** to Fabric via the mirrored catalog. The
> "CERTIFIED" designation is shown in UC; surfacing it in Fabric is handled in later steps.

---

## 8. Deployment decisions

Confirm these before running the deployment (they drive which Bicep params + steps execute):

1. **Fresh vs existing Databricks** — `databricks_config.workspace.use_existing`. Fresh runs
   Steps 6–9 fully; existing skips Step 6 (pass‑through `host_url` / `resource_id` /
   `access_connector_id`) and preflight validates Premium + UC.
2. **Fresh vs existing Fabric** — `deploy_config.capacity.use_existing` /
   `workspace.use_existing`. Fresh creates capacity (Step 5) + workspace + Workspace Identity
   (Step 10); existing consumes `existing_capacity_id` / `existing_workspace_id`.
3. **Optional features** — `features.enable_operations_agent` (needs Teams + Copilot, §3.3/§3.5),
   `features.enable_ontology`, `features.enable_data_agent`, `features.enable_activator_email`
   (default `true`, Teams‑free).
4. **Cost checkpoint acknowledged** — review `docs/cost.md` and the F64 ≈ $11.52/hr figure;
   plan the pause/resume strategy (Step 4 is a gated cost checkpoint).
5. **No secrets in the repo** — all secrets via Key Vault + `az login` at runtime; placeholders
   everywhere in committed files.

---

## Sources

- Plan §3.3 (region/capacity), §3.4 (tenant settings), §3.5 (identities), §4 (prerequisites), §7 (versions).
- **R1** — Databricks↔Fabric mirroring (mirrored‑catalog tenant setting, OneLake security).
- **R3** — Fabric Data Agent (Copilot/Data Agent tenant settings).
- **R4** — Fabric Ontology + Graph (Fabric IQ tenant settings).
- **R5** — Policy Weaver (identity scopes: Databricks SDK + Graph + Fabric `dataAccessRoles`).
- **R6** — Purview governance (Fabric live view + tenant scan, sensitivity labels/DLP).
- **R7** — Fabric‑as‑code (SP setup process + Developer‑settings tenant settings).
- **R8** — Databricks‑as‑code (UC, Premium, external data access, EXTERNAL USE SCHEMA grants).
- **R9** — Capacity/region/cost (East US 2, F64, $11.52/hr, trial caveat).
- **R11** — Operations Agent (GA, Teams prerequisite, RTI/Activator tenant settings, trial exclusion).
