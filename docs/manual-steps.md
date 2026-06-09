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

Every row in the consolidated table below uses these columns:

| Column | Meaning |
|---|---|
| **#** | Sequential index of the manual action (stable handle for runbooks / `deploy.py` pause points) |
| **Step** | Plan step that owns/introduces the manual action (phase shown by the section header) |
| **Action** | The exact manual action to perform |
| **Where (exact portal path)** | Concrete click‑path / portal URL |
| **Why** | Why it cannot be automated / why it is required (with R‑citation) |
| **Who/role** | The role that must perform it |

> **What is NOT here:** anything the repo automates (Bicep, Fabric/Databricks REST, SDKs,
> notebooks, CLI, `fabric-cicd`/Git, SQL grants, Reflex/Activator `EmailMessage` deploy‑as‑code).
> The default **Activator email** watch+act path (Step 19) is **deployed as code** — only its
> design‑mode rule *validation* is a (minor) UI check. The **optional Operations Agent** Teams
> path (Step 20) is the manual, Teams‑requiring alternative and is skipped entirely when
> `enable_operations_agent=false`.

---

## Phase 0 — Prerequisites (Step 2)

### 0.1 Fabric tenant settings (Step 2)

> All of the following are in **`app.fabric.microsoft.com` → ⚙ Settings → Admin portal →
> Tenant settings**. Use the search box to find each setting by name, set it to **Enabled**, and
> scope to the SP security group where noted. See
> [`docs/prerequisites.md` §3](./prerequisites.md#3-fabric-tenant-settings).

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 1 | 2 | Confirm/Enable **Service principals can call Fabric public APIs** (scoped to SP group) | Admin portal → Tenant settings → **Developer settings** | SP can't call Fabric public REST APIs otherwise (R7) | Fabric Admin |
| 2 | 2 | Enable **Service principals can create workspaces, connections, and deployment pipelines** (scoped to SP group) | Admin portal → Tenant settings → **Developer settings** | SP must create workspaces/connections/pipelines (R7) | Fabric Admin |
| 3 | 2 | Enable **Service principals can access read‑only admin APIs** (scoped to SP group) | Admin portal → Tenant settings → **Admin API settings** | Preflight read‑only admin checks (R7) | Fabric Admin |
| 4 | 2 | Enable **Service principals can access admin APIs used for updates** (scoped to SP group) | Admin portal → Tenant settings → **Admin API settings** | Admin API update operations (R7) | Fabric Admin |
| 5 | 2 | Enable **Enable new mirrored catalog items (preview)** | Admin portal → Tenant settings → **Microsoft Fabric** | Required to create the Mirrored Azure Databricks Catalog, Step 11 (R1) | Fabric Admin |
| 6 | 2 | Enable **Users can use Copilot and other features powered by Azure OpenAI** | Admin portal → Tenant settings → **Copilot and Azure OpenAI Service** | Gates Data Agent (Step 17) + Operations Agent (Step 20); the only Data‑Agent gate — no separate Data‑agent toggle exists (R3, R4) | Fabric Admin |
| 7 | 2 | Enable **Capacities can be designated as Fabric Copilot capacities** | Admin portal → Tenant settings → **Copilot and Azure OpenAI Service** | Designate the capacity for Copilot/Data Agent usage (R3) | Fabric Admin |
| 8 | 2 | Enable **Users can create Ontology (preview) items** | Admin portal → Tenant settings → **Microsoft Fabric** | Fabric IQ Ontology layer, Step 16 (R4) | Fabric Admin |
| 9 | 2 | Enable **User can create Graph** | Admin portal → Tenant settings → **Microsoft Fabric** | Graph (GA) the ontology auto‑creates, Step 16 (R11) | Fabric Admin |
| 10 | 2 | (Optional) Enable **All Power BI users can see "Set alert" button to create Fabric Activator alerts** | Admin portal → Tenant settings → **Microsoft Fabric** | Surfaces the "Set alert" button for Activator alerts, Step 19 (R11). No dedicated "Activator" toggle exists — Activator just needs an F‑capacity workspace | Fabric Admin |
| 11 | 2 | Enable **Enable Operations Agents (Preview)** — *only if `enable_operations_agent=true`* | Admin portal → Tenant settings → **Microsoft Fabric** | Optional Teams enhancement, Step 20 (R11 §8) | Fabric Admin |
| 12 | 2 | Enable **Users can synchronize workspace items with their Git repositories** | Admin portal → Tenant settings → **Git integration** | Git integration / `fabric-cicd` ALM (R7) | Fabric Admin |
| 13 | 2 | Enable **Users can access data stored in OneLake with apps external to Fabric** | Admin portal → Tenant settings → **OneLake settings** | External‑app OneLake access + Policy Weaver, Step 21 (R1, R5). OneLake data‑access roles are configured per item, not via a tenant toggle | Fabric Admin |
| 14 | 2 | Enable **Allow users to apply sensitivity labels for content** | Admin portal → Tenant settings → **Information protection** | Purview sensitivity labels + DLP across Fabric/Power BI, Step 22 (R6). Purview live view / tenant scans are configured on the Purview side | Fabric Admin |

> **Cross‑geo AI settings are NOT enabled for East US 2** (US region). Enable them only for a
> non‑US/non‑EU capacity. See
> [`docs/prerequisites.md` §3.3](./prerequisites.md#33-copilot--azure-openai--data-agent--group-copilot-and-azure-openai-service-r3-r4).

### 0.2 Service‑principal & identity setup (Step 2)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 15 | 2 | Create Entra **app registration** `zava-fabric-deploy-sp` (`az ad app create`) | Entra admin center → **App registrations** → New, or `az ad` CLI | Deploy SP identity for Bicep/Fabric automation (R7) | Entra Admin |
| 16 | 2 | Create the **service principal** for the app (`az ad sp create`) | Entra → **Enterprise applications**, or `az ad` CLI | SP object for the app registration (R7) | Entra Admin |
| 17 | 2 | Create a **client secret** → store in **Key Vault** (never in repo) | Entra → App registration → **Certificates & secrets** | Auth credential; public repo forbids committing it | Entra Admin |
| 18 | 2 | Assign **Contributor** (or Owner) on the target resource group to the SP | Azure portal → Resource group → **Access control (IAM)**, or `az role assignment` | SP must provision Azure resources (R7) | Sub Owner |
| 19 | 2 | Create **Entra security group** `zava-fabric-api-sps` and add the SP as a member | Entra admin center → **Groups** → New, or `az ad group` CLI | Fabric API tenant settings are scoped to a security group (R7 §7) | Entra Admin |
| 20 | 2 | **Scope** the four SP API toggles (rows 1–4 — two under Developer settings, two under Admin API settings) to `zava-fabric-api-sps` | Admin portal → Tenant settings → **Developer settings** + **Admin API settings** → *Specific security groups* | Least‑privilege SP API access (R7) | Fabric Admin |
| 21 | 2 | Add the deploy SP as **Admin/Member** of each Fabric workspace | Fabric workspace → **Manage access** (done/confirmed in Step 10) | SP needs workspace‑level access to deploy items (R7) | Fabric Admin |

> **Setup order:** app registration → SP → security group + membership → scope tenant settings →
> add SP to workspace. See [`docs/prerequisites.md` §4](./prerequisites.md#4-service-principals--identities).

### 0.3 Databricks‑side prerequisite (introduced by Step 2; surfaced again in Step 8)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 22 | 2 / 8 | Enable **External data access** on the Unity Catalog metastore | Databricks workspace → **Catalog** → ⚙ gear → **Metastore** → **Details** tab → enable **External data access** (account‑admin confirmation may be required in the Databricks account console) | Disabled by default; required for Fabric mirroring/credential vending (R8). The `EXTERNAL USE SCHEMA`/`USE CATALOG`/`USE SCHEMA`/`SELECT` grants are SQL‑automated by Step 8, not manual | Metastore Admin |

> See [`docs/prerequisites.md` §7](./prerequisites.md#7-databricks-side-prerequisites).

---

## Phase 1 — Azure Infrastructure (Step 4)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 23 | 4 | **Cost acknowledgement** — explicitly accept the F64 (~$138–300/mo with aggressive pausing) cost model before any deploy | `docs/cost.md` cost gate → confirm before running `scripts/deploy.py` (capacity create in Step 5) | Pre‑deploy financial gate; a deliberate human acknowledgement, not an automatable action (R9) | User / Sub Owner |

---

## Phase 3 — Fabric Ingestion (Steps 10–12)

### 3.1 Fabric Workspace Identity (Step 10; consumed by Step 7/12 hardening)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 24 | 10 | Create the **Fabric Workspace Identity** *if the REST `provisionIdentity` call is unsupported for the SP in your tenant* | Fabric workspace → **Workspace settings** → **Workspace identity** → **+ Workspace identity** | Workspace Identity REST provisioning can require one‑time tenant/admin consent and is **not always service‑principal‑supported** (R7/R10); `00_create_workspace.py` falls back to this UI step and logs it | Fabric Admin |
| 25 | 10 | Capture the Workspace Identity **object id** into `workspace.identity_object_id` in `deploy_config.json` | Fabric workspace → **Workspace settings** → **Workspace identity** (copy the identity's object id) | The object id does not exist until the identity is created; Step 12 binds the ADLS resource‑instance / trusted‑workspace rule (`network-hardening.bicep`, authored in Step 7) to it (R10) | Fabric Admin |

### 3.2 Variation 1 — Mirrored Azure Databricks Catalog (Step 11)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 26 | 11 | One‑time **Databricks connection OAuth consent** (sign in with the organizational account) when creating the mirrored‑catalog connection | Fabric workspace → **+ New item → Mirrored Azure Databricks Catalog** → connection dialog → **Sign in** (OAuth consent), or Workspace → **Manage connections and gateways** → the Databricks connection | The Databricks connection requires interactive OAuth consent for the organizational account — no SP/headless path (R1, R10) | User |
| 27 | 11 | Run the mirrored‑catalog **create with a user‑token** (the `deploy.py` pause point) — SP is not supported by the preview API | `10_create_mirrored_catalog.py` prompts for the user token; or Fabric workspace → **Mirrored Azure Databricks Catalog** create wizard over the `zava` gold schema (15‑min auto‑sync) | Mirrored‑catalog create REST is preview and **user‑token only** — no SP (R1, R7); orchestrator pauses here | User |

### 3.3 Variation 2 — secured OneLake shortcut (Step 12)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 28 | 12 | Any **connection OAuth consent** for the ADLS Gen2 shortcut (sign‑in / authorize the storage connection) | Fabric Lakehouse → **Get data → New shortcut → Azure Data Lake Storage Gen2** → connection → **Sign in / authorize**, or Workspace → **Manage connections and gateways** | The shortcut's ADLS connection may require interactive sign‑in/consent that the Shortcuts REST API can't perform headlessly (R10) | User |
| 29 | 12 | Review/assign the **OneLake security role** controlling read access to the shortcut data | Fabric Lakehouse → **Manage OneLake data access (roles)** → assign principals to the role | OneLake data‑access roles are configured per item in the portal (no tenant toggle); needed so the trusted Workspace Identity / report identities can read the hardened shortcut (R10) | Fabric Admin |

> The Step 12 ADLS network hardening (firewall default‑deny, resource‑instance rule bound to the
> Workspace Identity, disable public access) is **applied as code** via `network-hardening.bicep`
> — not a manual click. `scripts/deploy.py` runs the second Bicep pass passing **both**
> `fabricWorkspaceId` (the workspace GUID persisted into `workspace.workspace_id`, row 25) **and**
> `workspaceIdentityObjectId` (the identity object id persisted into `workspace.identity_object_id`,
> row 25). `fabric/scripts/00_create_workspace.py --write-config` persists both ids automatically;
> the orchestrator **fails fast** if either is still a `<PLACEHOLDER>` so the shortcut is never
> deployed against an open (default‑Allow) firewall.

### 3.4 BYO Databricks — secure the Variation‑2 shortcut storage (existing‑estate path)

> **Secured V2 hardening is fresh‑Databricks only.** When you bring your own Databricks workspace
> (`databricks_config.workspace.use_existing=true`), `infra/main.bicep` does **not** own the ADLS
> Gen2 account, so the hardening module is **explicitly skipped** (surfaced via the
> `networkHardeningSkippedForExistingDatabricks` Bicep output and logged by `deploy.py` as the
> `hardening_skipped` wave — never a silent no‑op). Apply the equivalent firewall lockdown on your
> own storage account manually or with your own IaC:

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 29a | 12 | On your existing UC managed‑storage account, set the firewall to **default‑deny** and add a **resource‑instance rule** for the trusted Fabric workspace (resourceId built with the fixed subscription `00000000-0000-0000-0000-000000000000`), then grant the **Workspace Identity** *Storage Blob Data Reader* | Storage account → **Networking** → *Enabled from selected virtual networks and IP addresses* → **Resource instances** (Microsoft.Fabric/workspaces) + Storage account → **Access control (IAM)** → add role assignment | The fresh‑path `network-hardening.bicep` cannot PATCH an account it did not create; replicate its default‑deny + trusted‑workspace rule + Workspace‑Identity RBAC so the BYO shortcut is equally secured (R10 §6.2–6.3) | Storage/Subscription Admin |

---

## Phase 4 — Direct Lake Reporting (Step 15)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 30 | 15 | Author the **advanced report visuals** (multi‑city **maps**, **decomposition trees**, **forecasting**, KPI cards; blue Zava theme) in **Power BI Desktop** as a PBIP/PBIR project | **Power BI Desktop** → open `fabric/report/*` PBIP → author visuals → save PBIR → commit; deployed to the workspace as code via `fabric-cicd`/Git (`50_deploy_report.py`) | These rich visuals are authored in the Desktop designer (a **tooling** step, not a tenant click); the PBIR project is then deployed programmatically (R2) | User (report author) |

---

## Phase 5 — Fabric IQ (Steps 16–20)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 31 | 16 | **Generate Ontology from the semantic model** (UI‑only generation — no REST trigger) | Fabric workspace → open the **Direct Lake semantic model** → **… (More options) → Generate ontology** (auto‑creates the Graph) | Ontology generation from a semantic model is UI‑only; there is no REST trigger (R4). Code‑first fallback: create the Ontology item via REST with a **graph‑source definition JSON** if UI‑gen wobbles | User |
| 32 | 17 | Attach the **Ontology as a Data Agent data source** *if the REST definition enum doesn't expose ontology in your tenant* | Fabric workspace → open the **Data Agent** → **Data sources → Add** → select the **Ontology/Graph** item | The ontology source `type` may not yet be exposed by the Data Agent REST definition enum in every tenant (R4, Open Question 2); core create/update is via the Data Agent REST API (R3). Graph‑source fallback otherwise — appendix entry only needed if the attach is required | User |
| 33 | 18 | Wire the **Eventstream source connection / Custom App credential** for the telematics feed | Fabric workspace → open the **Eventstream** → **Add source → Custom App / Azure Event Hubs** → create/authorize connection (copy the connection string into your sender; never commit it) | Eventstream source connection/credential creation may be UI‑assisted (R11; Open Question 3). Fallback: direct KQL `.ingest` of generated events (Assumption 9) | User |
| 34 | 19 | Validate the **Activator (Reflex) rule in design mode** (Monitor → Condition → Email action) | Fabric workspace → open the **Activator (Reflex)** item → **design mode** → verify the rule body against the live Reflex schema | Minor UI check: the Reflex item + `EmailMessage` action **deploy as code** (`78_create_activator_email.py`), but the rule body should be validated against the live schema (R11 §6c; Open Question 6). **No Teams required** | User |
| 35 | 20 | *(Optional — `enable_operations_agent=true` only)* Create the Operations Agent with a **user‑token** (no SP/MI) — the `deploy.py` pause point | `80_create_operations_agent.py` prompts for the user token (`POST …/operationsAgents`), or Fabric workspace → **Operations Agent** create | Operations Agent create REST is **user‑token only** — no SP/MI (R11 §8). Skipped entirely when the tenant has no Teams | User |
| 36 | 20 | *(Optional — `enable_operations_agent=true` only)* Install the **Fabric Operations Agent Teams app** and complete the live **Yes/No approval** card | **Microsoft Teams** → **Apps** → install **Fabric Operations Agent** → respond to the **Yes/No** approval card when a recommendation fires | The Operations Agent **requires Microsoft Teams** for its default notification and human‑in‑the‑loop approval; there is no native email channel (R11 §6/§8). The default Teams‑free path is the Step 19 Activator email | User |

---

## Phase 6 — Governance (Steps 21–22)

| # | Step | Action | Where (exact portal path) | Why | Who/role |
|---|---|---|---|---|---|
| 37 | 21 | Review/assign the **OneLake Security roles** Policy Weaver produced and complete any **identity consent** | Fabric Lakehouse → **Manage OneLake data access (roles)** → verify the mirrored UC row‑filter/column‑mask roles + principal assignments | OneLake Security role review/assignment and Graph identity‑resolution consent are UI‑assisted; Policy Weaver writes the roles but assignment/consent may need a human (R5/R1) | Fabric Admin |
| 38 | 22 | Create the **governance domain + data product** for the certified Zava gold asset and attach glossary terms | **`purview.microsoft.com`** (Unified Catalog) → **Data Management → Domains** → New domain → **Data products** → New; attach **Glossary** terms | Governance domains/data products are largely UI/PowerShell with only partial REST coverage (R6) | Data Steward / Purview Admin |
| 39 | 22 | Turn on the **Fabric live view** and run/confirm the **tenant scan** in Purview | **`purview.microsoft.com`** → **Data Map → Sources** → register **Microsoft Fabric** tenant → enable **live view** + run scan | The Fabric live‑view toggle and tenant scan registration are UI‑only on the Purview side (R6) | Purview Admin |
| 40 | 22 | Apply **sensitivity labels + DLP policy** on the synthetic PII columns and confirm downstream inheritance | **`purview.microsoft.com`** → **Information Protection → Labels / DLP policies** (labels surfaced in Fabric via tenant setting row 14) | Label/DLP authoring and the Fabric tenant admin settings are UI‑only; downstream inheritance is then demonstrated (R6) | Purview Admin / Fabric Admin |

---

## Coverage map (Steps 1–22)

| Step | Manual? | Rows | Notes |
|---|---|---|---|
| 1 | none | — | Config schema (automated) |
| 2 | yes | 1–22 | Tenant settings + SP/identity + Databricks metastore |
| 3 | none | — | Data generator (automated) |
| 4 | yes | 23 | Cost acknowledgement gate |
| 5 | none | — | Capacity is Bicep‑automatable |
| 6 | none | — | Bicep modules (automated) |
| 7 | yes | 24–25 | Workspace Identity (owned by Step 10; object id feeds Step 7's module via Step 12) |
| 8 | yes | 22 | Metastore external access (shared with Step 2 prereq) |
| 9 | none | — | 2A caveat is narrated, not a click |
| 10 | yes | 24–25 | Workspace Identity create + capture object id |
| 11 | yes | 26–27 | Databricks OAuth consent + user‑token mirror create |
| 12 | yes | 28–29 | Shortcut connection consent + OneLake role (hardening applied as code) |
| 13 | none | — | Thin gold (automated) |
| 14 | none | — | Semantic model (programmatic) |
| 15 | yes | 30 | Advanced visuals authored in Power BI Desktop |
| 16 | yes | 31 | Generate ontology from semantic model (UI‑only) |
| 17 | yes | 32 | Ontology→Data Agent source attach (tenant‑dependent) |
| 18 | yes | 33 | Eventstream source connection/credential |
| 19 | yes | 34 | Activator rule design‑mode validation (deploy‑as‑code; no Teams) |
| 20 | yes (optional) | 35–36 | Operations Agent user‑token + Teams app install / Yes‑No approval |
| 21 | yes | 37 | OneLake Security role review/assignment + identity consent |
| 22 | yes | 38–40 | Purview domains/data products, Fabric live‑view toggle, labels/DLP UI |

> **Single source of truth:** follow the numbered rows above in order. `scripts/deploy.py` pauses
> at the user‑token / UI rows (26–27, 31, 33, 35–36) with the matching instructions.
