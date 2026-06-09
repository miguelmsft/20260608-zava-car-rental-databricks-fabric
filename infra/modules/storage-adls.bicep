// ============================================================================
// Zava demo — ADLS Gen2 storage for Unity Catalog managed storage + RBAC
// Resources:
//   Microsoft.Storage/storageAccounts (StorageV2 + isHnsEnabled=true)
//   .../blobServices/default + container (UC metastore/catalog root)
//   Microsoft.Authorization/roleAssignments (Access Connector MI ->
//     Storage Blob Data Contributor, scoped to this account)
//
// Hierarchical namespace (HNS) is REQUIRED for ADLS Gen2 / Unity Catalog
// external + managed storage (R8 §2.5). The Access Connector's system-assigned
// managed identity is granted Storage Blob Data Contributor so UC storage
// credentials can read/write the metastore root (R8 §2.6). No keys/secrets are
// emitted — access is identity-based.
//
// Source: R8 §2.5–2.6 (research/2026-06-08-r8-databricks-as-code.md)
//   https://learn.microsoft.com/en-us/azure/storage/blobs/create-data-lake-storage-account
// ============================================================================

@description('ADLS Gen2 (HNS) storage account name. 3–24 chars, lowercase letters and numbers only.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region (default: East US 2 per plan §1.7).')
param location string = resourceGroup().location

@description('Container name for the Unity Catalog metastore/catalog root.')
param containerName string = 'unitycatalog'

@description('Storage account SKU. Standard_LRS is sufficient for the demo metastore.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
  'Standard_GRS'
])
param storageSku string = 'Standard_LRS'

@description('Object (principal) ID of the Access Connector managed identity to grant Storage Blob Data Contributor. Leave empty to skip the role assignment (e.g. when wiring the MI later).')
param accessConnectorPrincipalId string = ''

@description('Tags applied to the storage account.')
param tags object = {}

// Storage Blob Data Contributor — built-in role definition ID (constant across clouds).
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: storageSku
  }
  properties: {
    isHnsEnabled: true // Required for ADLS Gen2 / Unity Catalog (R8 §2.5)
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
}

// Role assignment scoped to THIS storage account: Access Connector MI ->
// Storage Blob Data Contributor (R8 §2.6). Skipped when no principal supplied.
resource blobContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(accessConnectorPrincipalId)) {
  name: guid(storageAccount.id, accessConnectorPrincipalId, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: accessConnectorPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('ARM resource ID of the ADLS Gen2 storage account.')
output storageAccountId string = storageAccount.id

@description('Name of the ADLS Gen2 storage account.')
output storageAccountName string = storageAccount.name

@description('ABFSS URI of the Unity Catalog metastore/catalog root container.')
output containerUri string = 'abfss://${containerName}@${storageAccountName}.dfs.${environment().suffixes.storage}/'

@description('DFS (ADLS Gen2) endpoint of the storage account.')
output dfsEndpoint string = storageAccount.properties.primaryEndpoints.dfs
