// ============================================================================
// Zava demo — ADLS Gen2 network hardening for the Variation-2 shortcut path
//
// Hardens the Unity-Catalog managed-storage account so that ONLY a named Fabric
// workspace (via its Workspace Identity) can traverse the storage firewall:
//   * networkAcls.defaultAction = Deny (firewall default-deny)
//   * networkAcls.resourceAccessRules[] = trusted-workspace resource instance rule
//     (each item = { tenantId, resourceId }) — R10 §6.2
//   * optional IP / virtual-network allow rules
//   * publicNetworkAccess Enabled (selected networks only) or Disabled
//   * Storage Blob Data Reader RBAC for the Fabric Workspace Identity — R10 §6.3
//
// IMPORTANT — AUTHORING vs APPLYING (plan §Step 7 scope note):
//   This module is AUTHORED in Step 7 but is wired CONDITIONALLY in main.bicep
//   (gated behind `applyNetworkHardening`, default FALSE). The firewall lockdown
//   is only actually APPLIED in Step 12, AFTER Step 10 creates the Fabric
//   Workspace Identity and its object id / the workspace resourceId are known.
//
//   No-op-until-identity-exists design: the firewall is only switched to
//   `defaultAction = Deny` once a trusted-workspace resource instance rule is
//   supplied (`fabricWorkspaceResourceId` + `fabricTenantId`). With the default
//   placeholder (empty) params the module parses/builds and leaves the firewall
//   open (`defaultAction = Allow`) — so it can never lock the account out before
//   the Workspace Identity rule exists. The RBAC role assignment is likewise
//   skipped while `workspaceIdentityObjectId` is empty.
//
// resourceAccessRules schema (R10 §6.2, verbatim from the ARM storageAccounts
// reference — property names exact):
//   "resourceAccessRules": [
//     { "tenantId": "<tenant-guid>",
//       "resourceId": "/subscriptions/00000000-0000-0000-0000-000000000000/
//                      resourcegroups/Fabric/providers/Microsoft.Fabric/
//                      workspaces/<workspace-guid>" }
//   ]
// Per Trusted workspace access, the Fabric workspace resourceId MUST use the
// fixed subscriptionId 00000000-0000-0000-0000-000000000000.
//
// Source: R10 §6.1–6.3 (research/2026-06-08-r10-ingestion-variations.md)
//   https://learn.microsoft.com/en-us/fabric/security/security-trusted-workspace-access
//   https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts
//
// Public repo — NO secrets. Access is identity-based (Workspace Identity + RBAC).
// ============================================================================

@description('ADLS Gen2 (HNS) storage account name to harden. Must be the SAME account created by storage-adls.bicep. 3–24 chars, lowercase letters and numbers only.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region (default: the resource group location). Must match the existing storage account region.')
param location string = resourceGroup().location

@description('Storage account SKU. Must match the existing account (Standard_LRS is the demo default).')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
  'Standard_GRS'
])
param storageSku string = 'Standard_LRS'

@description('Entra tenant id that owns the Fabric workspace (used in the trusted-workspace resource instance rule). Leave empty in the authoring phase; supplied in Step 12.')
param fabricTenantId string = ''

@description('Full ARM resourceId of the trusted Fabric workspace for the resource instance rule. MUST use the fixed subscriptionId 00000000-0000-0000-0000-000000000000, e.g. /subscriptions/00000000-0000-0000-0000-000000000000/resourcegroups/Fabric/providers/Microsoft.Fabric/workspaces/<workspace-guid>. Leave empty in the authoring phase; supplied in Step 12 (R10 §6.2).')
param fabricWorkspaceResourceId string = ''

@description('Object (principal) id of the Fabric Workspace Identity (created in Step 10). REQUIRED for the apply phase (Step 12) — grants Storage Blob Data Reader so the identity can read the shortcut target. Empty in the authoring phase makes the role assignment a no-op (R10 §6.3).')
param workspaceIdentityObjectId string = ''

@description('When true, set publicNetworkAccess = Disabled (private access only). When false, keep it Enabled so the firewall allows the trusted workspace + any IP/vnet rules. Trusted workspace access works with public access disabled (R10 §6.3).')
param disablePublicNetworkAccess bool = false

@description('Optional list of public IPv4 addresses / CIDR ranges to allow through the firewall (e.g. an admin egress IP for break-glass).')
param allowedIpRules array = []

@description('Optional list of subnet resource IDs (Microsoft.Network/virtualNetworks/subnets) to allow through the firewall.')
param allowedSubnetResourceIds array = []

@description('networkAcls.bypass for the storage firewall. Default "None" — access relies on the Fabric trusted-workspace resource instance rule (resourceAccessRules), NOT a broad Azure trusted-services exception. R10 §6.3: "Trusted service exception is discouraged... We recommend that you use resource instance rules." Use "Logging, Metrics" if platform diagnostics must traverse the firewall; do NOT set "AzureServices".')
@allowed([
  'None'
  'Logging'
  'Metrics'
  'Logging, Metrics'
])
param firewallBypass string = 'None'

@description('Tags applied to the storage account (should match storage-adls.bicep to avoid drift).')
param tags object = {}

// ---- Trusted-workspace rule + firewall derivation --------------------------
// A trusted-workspace resource instance rule is only emitted (and the firewall
// only switched to Deny) once BOTH the workspace resourceId and tenant id are
// supplied. This is the no-op-until-identity-exists guard: the authoring phase
// (empty params) leaves the firewall open so the account is never locked out
// before the Step 10 Workspace Identity rule is known.
var trustedWorkspaceRuleProvided = !empty(fabricWorkspaceResourceId) && !empty(fabricTenantId)

var resourceAccessRules = trustedWorkspaceRuleProvided
  ? [
      {
        tenantId: fabricTenantId
        resourceId: fabricWorkspaceResourceId
      }
    ]
  : []

var ipRules = [
  for ip in allowedIpRules: {
    value: ip
    action: 'Allow'
  }
]

var virtualNetworkRules = [
  for subnetId in allowedSubnetResourceIds: {
    id: subnetId
    action: 'Allow'
  }
]

// Lock the firewall (defaultAction = Deny) only once a trusted-workspace rule
// exists; otherwise keep it open (Allow) so authoring never self-locks.
var firewallDefaultAction = trustedWorkspaceRuleProvided ? 'Deny' : 'Allow'

// Storage Blob Data Reader — built-in role definition ID (constant across clouds).
// Least-privilege read access for the Fabric Workspace Identity (R10 §6.3).
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

// ----------------------------------------------------------------------------
// Storage account (re-declared to PATCH networkAcls onto the EXISTING account).
// Baseline properties mirror storage-adls.bicep so the hardening deployment does
// not drift the HNS / TLS / public-blob settings. ARM storage account deploys are
// incremental — this layers the firewall config onto the account from Step 6.
// Per R10 §6.2 the full storageAccounts resource carries properties.networkAcls.
// ----------------------------------------------------------------------------
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: storageSku
  }
  properties: {
    isHnsEnabled: true // Required for ADLS Gen2 / Unity Catalog (R8 §2.5) — kept to avoid drift.
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: disablePublicNetworkAccess ? 'Disabled' : 'Enabled'
    networkAcls: {
      // R10 §6.3: the broad Azure trusted-services exception is DISCOURAGED —
      // "This configuration isn't recommended... We recommend that you use
      // resource instance rules." Default bypass is 'None' so firewall traversal
      // relies solely on the Fabric trusted-workspace resourceAccessRules rule
      // (plus any explicit IP/VNet allow rules), not 'AzureServices'.
      bypass: firewallBypass
      defaultAction: firewallDefaultAction
      resourceAccessRules: resourceAccessRules
      ipRules: ipRules
      virtualNetworkRules: virtualNetworkRules
    }
  }
}

// Storage Blob Data Reader for the Fabric Workspace Identity, scoped to THIS
// account (R10 §6.3). Skipped while the identity object id is empty (authoring).
resource blobReaderAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(workspaceIdentityObjectId)) {
  name: guid(storageAccount.id, workspaceIdentityObjectId, storageBlobDataReaderRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: workspaceIdentityObjectId
    principalType: 'ServicePrincipal'
  }
}

// ---- Outputs ---------------------------------------------------------------
@description('ARM resource ID of the hardened storage account.')
output storageAccountId string = storageAccount.id

@description('Effective firewall default action (Deny once a trusted-workspace rule is supplied, otherwise Allow).')
output networkDefaultAction string = firewallDefaultAction

@description('True when a trusted-workspace resource instance rule was applied (apply phase, Step 12). False in the authoring phase.')
output trustedWorkspaceAccessApplied bool = trustedWorkspaceRuleProvided

@description('Number of trusted-workspace resource instance rules applied to the firewall.')
output resourceAccessRuleCount int = length(resourceAccessRules)

@description('Effective public network access setting (Disabled when locked down, otherwise Enabled).')
output publicNetworkAccess string = disablePublicNetworkAccess ? 'Disabled' : 'Enabled'
