// ============================================================================
// Zava demo — Azure infrastructure entry point (resource-group scope)
//
// Wires the Databricks data-foundation modules authored in Step 6:
//   * access-connector.bicep      (Unity Catalog storage managed identity)
//   * databricks-workspace.bicep  (Premium workspace for Unity Catalog)
//   * storage-adls.bicep          (ADLS Gen2 HNS + Blob Data Contributor RBAC)
//   * keyvault.bicep              (RBAC Key Vault for demo secrets, no secrets)
//
// Also wires the Fabric capacity module authored in Step 5:
//   * fabric-capacity.bicep        (Microsoft.Fabric/capacities — F64, East US 2)
//
// Fresh vs existing (plan §Step 1 "fresh vs existing paths"):
//   * useExistingDatabricks=false -> all Databricks resources provisioned.
//   * useExistingDatabricks=true  -> Databricks workspace/connector/ADLS are
//     SKIPPED; outputs pass through the supplied existing ids/urls.
//   * useExistingFabricCapacity=false -> Fabric capacity provisioned (Step 5).
//   * useExistingFabricCapacity=true  -> Fabric capacity SKIPPED; the capacity
//     id output passes through the supplied existing capacity id.
//
// Deploy (Phase 0 = author only; do NOT deploy yet):
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/params/dev.bicepparam
//
// Public repo — NO secrets in this template. Secrets are stored in Key Vault at
// runtime; storage/UC access is identity-based via the Access Connector MI.
// ============================================================================

targetScope = 'resourceGroup'

// ---- Location / naming -----------------------------------------------------
@description('Azure region for all resources. Default East US 2 (plan §1.7 — only US region supporting ALL capabilities).')
param location string = 'eastus2'

@description('Short prefix used for tagging / naming conventions (e.g. "zava").')
param resourcePrefix string = 'zava'

// ---- Fabric capacity (Step 5) ----------------------------------------------
@description('Name of the Fabric capacity (provisioned by the fabric-capacity module on the fresh path).')
param fabricCapacityName string = '${resourcePrefix}-fabric-cap'

@description('Fabric capacity SKU (e.g. F64). Wired by Step 5.')
param fabricCapacitySku string = 'F64'

@description('When true, an existing Fabric capacity is consumed (no capacity created). Wired by Step 5.')
param useExistingFabricCapacity bool = false

@description('ARM resource ID of an existing Fabric capacity (required when useExistingFabricCapacity=true). Wired by Step 5.')
param existingFabricCapacityId string = ''

@description('Fabric capacity administrators (UPNs / object ids). Wired by Step 5.')
param fabricCapacityAdmins array = []

// ---- Databricks workspace --------------------------------------------------
@description('When true, an EXISTING Databricks workspace is used and ALL Databricks resources in this template are skipped (Step 6 provisioning flag).')
param useExistingDatabricks bool = false

@description('Name of the Azure Databricks workspace (fresh path).')
param databricksWorkspaceName string = '${resourcePrefix}-databricks-ws'

@description('Databricks SKU. Must be premium — Unity Catalog requires the Premium plan (R8 §2.2).')
@allowed([
  'premium'
])
param databricksSku string = 'premium'

@description('ARM resource ID of an existing Databricks workspace (required when useExistingDatabricks=true).')
param existingDatabricksWorkspaceId string = ''

@description('Host URL of an existing Databricks workspace (required when useExistingDatabricks=true).')
param existingDatabricksHostUrl string = ''

@description('Name of the Access Connector for Azure Databricks (fresh path).')
param accessConnectorName string = '${resourcePrefix}-access-connector'

// ---- Existing Access Connector pass-through (used only when useExistingDatabricks=true) ----
// When bringing your own Databricks workspace you must ALSO supply the existing
// Unity Catalog Access Connector so downstream steps (UC storage credential,
// mirroring/shortcut) get a real ARM id + managed-identity principal id.
// These are IGNORED on the fresh path (the access-connector module supplies them).
@description('ARM resource ID of an existing Access Connector (required when useExistingDatabricks=true). Ignored on the fresh path.')
param existingAccessConnectorId string = ''

@description('Object (principal) ID of an existing Access Connector managed identity (required when useExistingDatabricks=true). Ignored on the fresh path.')
param existingAccessConnectorPrincipalId string = ''

// ---- ADLS Gen2 managed storage (Unity Catalog) -----------------------------
@description('ADLS Gen2 (HNS) storage account name for Unity Catalog managed storage. 3–24 chars, lowercase alphanumeric.')
param managedStorageAccountName string = '${resourcePrefix}uc'

@description('Container name for the Unity Catalog metastore/catalog root.')
param ucContainerName string = 'unitycatalog'

// ---- Key Vault -------------------------------------------------------------
@description('Name of the Key Vault for demo secrets (RBAC-enabled; no secrets in template).')
param keyVaultName string = '${resourcePrefix}-kv'

// ---- Common tags -----------------------------------------------------------
var commonTags = {
  project: 'zava-databricks-fabric-demo'
  environment: 'demo'
  managedBy: 'bicep'
}

// ============================================================================
// Databricks data foundation (skipped entirely when useExistingDatabricks=true)
// ============================================================================

// Access Connector first — its managed identity is consumed by the workspace
// (accessConnector property) and by the storage role assignment.
module accessConnector './modules/access-connector.bicep' = if (!useExistingDatabricks) {
  name: 'deploy-access-connector'
  params: {
    connectorName: accessConnectorName
    location: location
    tags: commonTags
  }
}

module databricksWorkspace './modules/databricks-workspace.bicep' = if (!useExistingDatabricks) {
  name: 'deploy-databricks-workspace'
  params: {
    workspaceName: databricksWorkspaceName
    location: location
    sku: databricksSku
    accessConnectorId: accessConnector.outputs.accessConnectorId
    tags: commonTags
  }
}

module storageAdls './modules/storage-adls.bicep' = if (!useExistingDatabricks) {
  name: 'deploy-storage-adls'
  params: {
    storageAccountName: managedStorageAccountName
    location: location
    containerName: ucContainerName
    accessConnectorPrincipalId: accessConnector.outputs.principalId
    tags: commonTags
  }
}

// ============================================================================
// Fabric capacity (skipped entirely when useExistingFabricCapacity=true).
// On the existing path the capacity id output passes through the supplied id.
// ============================================================================
module fabricCapacity './modules/fabric-capacity.bicep' = if (!useExistingFabricCapacity) {
  name: 'deploy-fabric-capacity'
  params: {
    capacityName: fabricCapacityName
    location: location
    skuName: fabricCapacitySku
    adminMembers: fabricCapacityAdmins
    tags: commonTags
  }
}

// ============================================================================
// Key Vault — demo secrets vault (always deployed; not a Databricks resource).
// ============================================================================
module keyVault './modules/keyvault.bicep' = {
  name: 'deploy-keyvault'
  params: {
    keyVaultName: keyVaultName
    location: location
    tags: commonTags
  }
}

// ============================================================================
// Outputs — ids/names/urls consumed by later steps (8, 9, 10, 12, 21).
// On the existing path, pass through the supplied existing ids/urls.
// ============================================================================

@description('Databricks workspace URL (host). Fresh: module output; existing: pass-through param.')
output databricksWorkspaceUrl string = useExistingDatabricks ? existingDatabricksHostUrl : databricksWorkspace.outputs.workspaceUrl

@description('ARM resource ID of the Databricks workspace. Fresh: module output; existing: pass-through param.')
output databricksWorkspaceId string = useExistingDatabricks ? existingDatabricksWorkspaceId : databricksWorkspace.outputs.workspaceId

@description('Name of the Databricks workspace (empty on existing path — name not required downstream).')
output databricksWorkspaceName string = useExistingDatabricks ? '' : databricksWorkspace.outputs.workspaceName

@description('ARM resource ID of the Access Connector. Fresh: module output; existing: pass-through param (never empty when using existing resources).')
output accessConnectorId string = useExistingDatabricks ? existingAccessConnectorId : accessConnector.outputs.accessConnectorId

@description('Object (principal) ID of the Access Connector managed identity. Fresh: module output; existing: pass-through param (never empty when using existing resources).')
output accessConnectorPrincipalId string = useExistingDatabricks ? existingAccessConnectorPrincipalId : accessConnector.outputs.principalId

@description('Name of the ADLS Gen2 managed-storage account (empty on existing path).')
output managedStorageAccountName string = useExistingDatabricks ? '' : storageAdls.outputs.storageAccountName

@description('ABFSS URI of the Unity Catalog metastore/catalog root (empty on existing path).')
output ucStorageContainerUri string = useExistingDatabricks ? '' : storageAdls.outputs.containerUri

@description('Key Vault URI for demo secrets (always provisioned).')
output keyVaultUri string = keyVault.outputs.keyVaultUri

@description('Name of the Key Vault.')
output keyVaultName string = keyVault.outputs.keyVaultName

// ---- Fabric capacity outputs (consumed by Step 4 pause/resume, Step 10 assign) ----
@description('ARM resource ID of the Fabric capacity. Fresh: module output; existing: pass-through param (never empty when using an existing capacity).')
output fabricCapacityId string = useExistingFabricCapacity ? existingFabricCapacityId : fabricCapacity.outputs.capacityId

@description('Name of the Fabric capacity. Fresh: module output; existing: the configured capacity name placeholder.')
output fabricCapacityName string = useExistingFabricCapacity ? fabricCapacityName : fabricCapacity.outputs.capacityName
