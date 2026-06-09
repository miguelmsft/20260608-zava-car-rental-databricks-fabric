// ============================================================================
// Zava demo — FRESH PROVISIONING parameters (Databricks + Fabric created new).
// Region: East US 2 (plan.md §1.7 — only US region supporting ALL capabilities,
// incl. Operations Agent). Fabric capacity SKU: F64 (AI/Data Agent/ontology — R9).
//
// This is the default deployment path. Copy to a local, gitignored
// `*.local.bicepparam` and replace <PLACEHOLDER> tokens with real values before
// deploying. NO secrets here — secrets come from Key Vault / `az login` at runtime.
//
// NOTE: `infra/main.bicep` is authored in later steps (Step 6 / Step 12). This
// parameter file is the contract those modules consume.
// ============================================================================
using '../main.bicep'

// ---- Location / naming ----
param location = 'eastus2'
param resourcePrefix = 'zava'

// ---- Fabric capacity (fresh) ----
param fabricCapacityName = 'zava-fabric-cap'
param fabricCapacitySku = 'F64'
param useExistingFabricCapacity = false
param existingFabricCapacityId = ''

// ---- Fabric capacity administrators (UPNs / object ids) ----
// Default admin per repo conventions; replace with your own as needed.
param fabricCapacityAdmins = [
  'admin@MngEnvMCAP422553.onmicrosoft.com'
]

// ---- Databricks workspace (fresh) ----
param useExistingDatabricks = false
param databricksWorkspaceName = 'zava-databricks-ws'
param databricksSku = 'premium'
param existingDatabricksWorkspaceId = ''
param existingDatabricksHostUrl = ''

// ---- ADLS Gen2 managed storage (Unity Catalog) ----
param managedStorageAccountName = 'zavauc'

// ---- Key Vault (secret references only; never inline secret values) ----
param keyVaultName = 'zava-kv'
