// ============================================================================
// Zava demo — Access Connector for Azure Databricks (Unity Catalog storage MI)
// Resource: Microsoft.Databricks/accessConnectors
//
// The Access Connector holds the SYSTEM-ASSIGNED managed identity that Unity
// Catalog uses to reach the ADLS Gen2 managed-storage account. Its principalId
// is granted Storage Blob Data Contributor on the storage account (see
// storage-adls.bicep). No secrets — identity-based access only.
//
// Source: R8 §2.4 (research/2026-06-08-r8-databricks-as-code.md)
//   https://learn.microsoft.com/en-us/azure/templates/microsoft.databricks/accessconnectors
// ============================================================================

@description('Name of the Access Connector for Azure Databricks.')
param connectorName string

@description('Azure region (default: East US 2 per plan §1.7).')
param location string = resourceGroup().location

@description('Tags applied to the Access Connector.')
param tags object = {}

resource accessConnector 'Microsoft.Databricks/accessConnectors@2024-05-01' = {
  name: connectorName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

@description('Resource ID of the Access Connector (consumed by the Databricks workspace and Unity Catalog storage credential).')
output accessConnectorId string = accessConnector.id

@description('Object (principal) ID of the Access Connector system-assigned managed identity — used for the Storage Blob Data Contributor role assignment.')
output principalId string = accessConnector.identity.principalId

@description('Name of the Access Connector.')
output connectorName string = accessConnector.name
