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

// ---- ADLS network hardening (Step 7 authored; APPLIED in Step 12) ----------
// AUTHORING + conditional wire-in only. The firewall lockdown + trusted-workspace
// resource instance rule are actually APPLIED in Step 12, AFTER Step 10 creates
// the Fabric Workspace Identity. Default OFF so the standard deploy is unchanged.
@description('When true, FORCE-apply the ADLS network-hardening module (firewall default-deny + Fabric trusted-workspace rule). Default FALSE. NOTE (Step 12): supplying a real Fabric Workspace GUID (fabricWorkspaceId) or full workspace resourceId (fabricWorkspaceResourceId) ALSO auto-enables hardening for the Variation-2 path — see applyNetworkHardeningEffective below (plan §Step 7 scope note / §Step 12).')
param applyNetworkHardening bool = false

@description('Entra tenant id owning the trusted Fabric workspace (for the resource instance rule). When empty, defaults to the deployment tenant tenant().tenantId (R10 §6.2 — the tenant that owns the Fabric workspace). Override only for cross-tenant scenarios. Supplied/derived in Step 12.')
param fabricTenantId string = ''

@description('GUID of the trusted Fabric workspace created in Step 10 (NOT the capacity). When set, the R10 trusted-workspace resourceId is CONSTRUCTED with the fixed Fabric subscriptionId 00000000-0000-0000-0000-000000000000 — see fabricWorkspaceResourceIdEffective. Supplied in Step 12 from the Step-10 workspace output. Leave empty in the authoring phase.')
param fabricWorkspaceId string = ''

@description('Full ARM resourceId of the trusted Fabric workspace (subscriptionId MUST be 00000000-0000-0000-0000-000000000000). OPTIONAL escape hatch — prefer fabricWorkspaceId (just the GUID) so the resourceId is built per R10 §6.2 and cannot be mis-typed. When both are set this full value wins. Supplied in Step 12 (R10 §6.2).')
param fabricWorkspaceResourceId string = ''

@description('Object (principal) id of the Fabric Workspace Identity (created in Step 10). Granted Storage Blob Data Reader during Step 12 hardening (R10 §6.3).')
param workspaceIdentityObjectId string = ''

@description('Operator OVERRIDE to FORCE publicNetworkAccess = Disabled during hardening. Default FALSE. NOTE (Step 12): even when FALSE, the hardening-applied path now AUTOMATICALLY locks down public network access once the trusted-workspace rule exists — Disabled when no break-glass IP/VNet rules are supplied, or "selected networks" (Enabled + defaultAction=Deny) when they are. See disableStoragePublicNetworkAccessEffective. Set TRUE to force Disabled even with break-glass rules present.')
param disableStoragePublicNetworkAccess bool = false

@description('Optional public IPv4 addresses / CIDR ranges to allow through the storage firewall (break-glass).')
param allowedStorageIpRules array = []

@description('Optional subnet resource IDs to allow through the storage firewall.')
param allowedStorageSubnetResourceIds array = []

// ---- Key Vault -------------------------------------------------------------
@description('Name of the Key Vault for demo secrets (RBAC-enabled; no secrets in template).')
param keyVaultName string = '${resourcePrefix}-kv'

// ---- Common tags -----------------------------------------------------------
var commonTags = {
  project: 'zava-databricks-fabric-demo'
  environment: 'demo'
  managedBy: 'bicep'
}

// ---- Step 12 — trusted-workspace resource-instance-rule derivation (R10 §6.2) ----
// R10 §6.2 (verbatim mechanism): the Fabric workspace resourceId for a storage
// resource instance rule MUST use the FIXED subscriptionId
// 00000000-0000-0000-0000-000000000000 and the literal resourcegroups/Fabric path:
//   /subscriptions/00000000-0000-0000-0000-000000000000/resourcegroups/Fabric/
//     providers/Microsoft.Fabric/workspaces/<workspace-guid>
// We CONSTRUCT it from the Step-10 workspace GUID (fabricWorkspaceId) rather than
// inventing/hand-typing a resourceId. A full fabricWorkspaceResourceId override
// still wins for cross-tenant / non-standard cases.
var fabricTrustedSubscriptionId = '00000000-0000-0000-0000-000000000000'

var fabricWorkspaceResourceIdFromGuid = empty(fabricWorkspaceId)
  ? ''
  : '/subscriptions/${fabricTrustedSubscriptionId}/resourcegroups/Fabric/providers/Microsoft.Fabric/workspaces/${fabricWorkspaceId}'

// Effective Fabric workspace resourceId fed to the hardening module: the explicit
// full id when supplied, else built from the Step-10 workspace GUID per R10 §6.2.
var fabricWorkspaceResourceIdEffective = !empty(fabricWorkspaceResourceId)
  ? fabricWorkspaceResourceId
  : fabricWorkspaceResourceIdFromGuid

// Tenant id is AVAILABLE in the Bicep — default to the deployment tenant (the tenant
// that owns the Fabric workspace) rather than requiring a hand-entered value (R10 §6.2).
var fabricTenantIdEffective = empty(fabricTenantId) ? tenant().tenantId : fabricTenantId

// "Flip the gate on for the Variation-2 path" (plan §Step 12): hardening applies when
// EITHER the explicit applyNetworkHardening flag is set OR a real Step-10 Fabric
// Workspace Identity (GUID or full resourceId) is supplied. The standard deploy
// (no identity supplied, flag false) stays a no-op — main.bicep is unchanged for it.
var applyNetworkHardeningEffective = (applyNetworkHardening || !empty(fabricWorkspaceResourceIdEffective)) && !useExistingDatabricks

// ---- Step 12 — public-network-access lockdown derivation (R10 §6.3) ----------
// The firewall only switches to defaultAction = Deny once a real trusted-workspace
// resource instance rule is supplied (the module's no-op-until-identity-exists gate
// keys off the SAME fabricWorkspaceResourceIdEffective). To make the hardening a
// GENUINE lockdown — not a Deny firewall sitting behind publicNetworkAccess=Enabled
// with no resource rule — we must disable/restrict public network access on exactly
// that condition, never before (a fresh deploy with no Fabric identity must not be
// locked out). plan §Step 12 / R10 §6.3.
var trustedWorkspaceRuleSupplied = !empty(fabricWorkspaceResourceIdEffective)

// Break-glass IP / VNet allow rules require "selected networks" mode: publicNetworkAccess
// stays Enabled while defaultAction = Deny, otherwise the IP/subnet allow rules are
// ignored (publicNetworkAccess = Disabled blocks ALL public traffic, including allowed
// IPs). With NO break-glass rules we fully Disable public network access. An explicit
// disableStoragePublicNetworkAccess=true forces Disabled regardless (operator override).
var hasBreakGlassNetworkRules = !empty(allowedStorageIpRules) || !empty(allowedStorageSubnetResourceIds)

// Effective lockdown: only ever true once the trusted-workspace rule exists (so the
// firewall is simultaneously defaulting to Deny and the trusted rule is the access path).
// Within the hardening-applied path: Disabled by default, or selected-networks (Enabled +
// Deny) when break-glass rules are present, unless the operator explicitly forces Disabled.
var disableStoragePublicNetworkAccessEffective = applyNetworkHardeningEffective && trustedWorkspaceRuleSupplied && (disableStoragePublicNetworkAccess || !hasBreakGlassNetworkRules)

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
// ADLS network hardening — AUTHORED in Step 7, APPLIED in Step 12.
// Step 12 flips the gate on for the Variation-2 path: the module is instantiated
// when applyNetworkHardeningEffective is true (explicit flag OR a real Step-10
// Fabric Workspace Identity supplied), passing the R10 §6.2 resource-instance rule
// (constructed workspace resourceId + deployment tenant id). The standard deploy
// (no identity, flag false) leaves the account untouched. dependsOn ensures the
// firewall layers AFTER the base account from storage-adls.bicep.
// ============================================================================
module networkHardening './modules/network-hardening.bicep' = if (applyNetworkHardeningEffective) {
  name: 'deploy-network-hardening'
  params: {
    storageAccountName: managedStorageAccountName
    location: location
    fabricTenantId: fabricTenantIdEffective
    fabricWorkspaceResourceId: fabricWorkspaceResourceIdEffective
    workspaceIdentityObjectId: workspaceIdentityObjectId
    disablePublicNetworkAccess: disableStoragePublicNetworkAccessEffective
    allowedIpRules: allowedStorageIpRules
    allowedSubnetResourceIds: allowedStorageSubnetResourceIds
    tags: commonTags
  }
  dependsOn: [
    storageAdls
  ]
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

// ---- Network hardening outputs (Step 7 authored; meaningful after Step 12) ----
@description('True when the ADLS network hardening + trusted-workspace rule were applied (Step 12). False while hardening is off or the trusted-workspace params are placeholders.')
output networkHardeningApplied bool = applyNetworkHardeningEffective ? networkHardening.outputs.trustedWorkspaceAccessApplied : false

@description('Effective storage firewall default action after hardening (Deny once locked down in Step 12, otherwise the account default).')
output storageNetworkDefaultAction string = applyNetworkHardeningEffective ? networkHardening.outputs.networkDefaultAction : 'Allow'
