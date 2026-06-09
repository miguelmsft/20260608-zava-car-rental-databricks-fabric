// ============================================================================
// Zava demo — USE-EXISTING-RESOURCES parameters (bring your own Databricks and/or
// Fabric). When useExisting* flags are true, the corresponding provisioning steps
// are SKIPPED and the supplied resource ids/urls are passed through:
//   * useExistingFabricCapacity=true  -> Step 5 emits no capacity (consumes id)
//   * useExistingDatabricks=true       -> Step 6 (Bicep workspace) is skipped;
//                                         preflight validates Premium + UC enabled.
//
// Copy to a local, gitignored `*.local.bicepparam` and replace every <PLACEHOLDER>
// with the real ARM ids / URLs for your existing resources. NO secrets here.
//
// NOTE: `infra/main.bicep` is authored in later steps (Step 6 / Step 12).
// ============================================================================
using '../main.bicep'

// ---- Location / naming ----
param location = 'eastus2'
param resourcePrefix = 'zava'

// ---- Fabric capacity (existing) ----
param useExistingFabricCapacity = true
param fabricCapacityName = 'zava-fabric-cap'
param fabricCapacitySku = 'F64'
param existingFabricCapacityId = '/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Fabric/capacities/<CAPACITY_NAME>'

// ---- Fabric capacity administrators (UPNs / object ids) ----
param fabricCapacityAdmins = [
  'admin@MngEnvMCAP422553.onmicrosoft.com'
]

// ---- Databricks workspace (existing) ----
param useExistingDatabricks = true
param databricksWorkspaceName = 'zava-databricks-ws'
param databricksSku = 'premium'
param existingDatabricksWorkspaceId = '/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Databricks/workspaces/<WORKSPACE_NAME>'
param existingDatabricksHostUrl = 'https://adb-<WORKSPACE_ID>.<N>.azuredatabricks.net'

// ---- ADLS Gen2 managed storage (Unity Catalog) ----
param managedStorageAccountName = 'zavauc'

// ---- Key Vault (secret references only; never inline secret values) ----
param keyVaultName = 'zava-kv'
