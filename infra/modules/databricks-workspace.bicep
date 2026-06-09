// ============================================================================
// Zava demo — Azure Databricks workspace (Premium tier for Unity Catalog)
// Resource: Microsoft.Databricks/workspaces
//
// Premium SKU is REQUIRED for Unity Catalog (R8 §2.2). The workspace attaches
// the Access Connector managed identity and initializes its default catalog as
// a Unity Catalog. Region defaults to East US 2 (plan §1.7).
//
// Source: R8 §2.3 (research/2026-06-08-r8-databricks-as-code.md)
//   https://learn.microsoft.com/en-us/azure/templates/microsoft.databricks/workspaces
// ============================================================================

@description('Name of the Azure Databricks workspace.')
param workspaceName string

@description('Azure region (default: East US 2 per plan §1.7).')
param location string = resourceGroup().location

@description('Databricks SKU. Must be premium — Unity Catalog requires the Premium plan (R8 §2.2).')
@allowed([
  'premium'
])
param sku string = 'premium'

@description('Name of the Databricks-managed resource group (auto-provisioned, holds the workspace VNet/NSG/etc.).')
param managedResourceGroupName string = '${workspaceName}-managed-rg'

@description('Resource ID of the Access Connector for Azure Databricks (system-assigned MI bridge to Unity Catalog storage).')
param accessConnectorId string

@description('When true, disables public network access on the workspace (defense-in-depth; default false for the base demo).')
param disablePublicNetworkAccess bool = false

@description('Tags applied to the workspace.')
param tags object = {}

resource workspace 'Microsoft.Databricks/workspaces@2024-05-01' = {
  name: workspaceName
  location: location
  tags: tags
  sku: {
    name: sku // 'premium' — required for Unity Catalog (R8 §2.2)
  }
  properties: {
    managedResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', managedResourceGroupName)
    accessConnector: {
      id: accessConnectorId
      identityType: 'SystemAssigned'
    }
    defaultCatalog: {
      initialType: 'UnityCatalog'
    }
    publicNetworkAccess: disablePublicNetworkAccess ? 'Disabled' : 'Enabled'
  }
}

@description('Workspace URL (e.g. adb-<id>.<n>.azuredatabricks.net) — consumed by Databricks CLI/REST steps (8, 9).')
output workspaceUrl string = workspace.properties.workspaceUrl

@description('ARM resource ID of the Databricks workspace.')
output workspaceId string = workspace.id

@description('Name of the Databricks workspace.')
output workspaceName string = workspace.name
