// ============================================================================
// Zava demo — Key Vault for demo secrets (RBAC-enabled)
// Resource: Microsoft.KeyVault/vaults
//
// RBAC-authorization model (no access policies). NO secrets are created in this
// template — the public repo never commits secrets. Secret values (e.g. a
// Databricks PAT, SP client secret) are written at RUNTIME via `az keyvault
// secret set` / managed identity after deployment (R8; repo security policy).
//
//   https://learn.microsoft.com/en-us/azure/templates/microsoft.keyvault/vaults
// ============================================================================

@description('Name of the Key Vault. 3–24 chars, alphanumeric and hyphens, globally unique.')
@minLength(3)
@maxLength(24)
param keyVaultName string

@description('Azure region (default: East US 2 per plan §1.7).')
param location string = resourceGroup().location

@description('Azure AD tenant ID for the Key Vault (defaults to the deployment tenant).')
param tenantId string = subscription().tenantId

@description('Key Vault SKU.')
@allowed([
  'standard'
  'premium'
])
param skuName string = 'standard'

@description('Enable purge protection. Recommended for production; default false to keep the demo easy to tear down.')
param enablePurgeProtection bool = false

@description('Soft-delete retention in days.')
@minValue(7)
@maxValue(90)
param softDeleteRetentionInDays int = 7

@description('Tags applied to the Key Vault.')
param tags object = {}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: skuName
    }
    tenantId: tenantId
    // RBAC model — grant roles (Key Vault Secrets User/Officer) to identities at
    // runtime; no inline access policies and no secrets committed here.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    enablePurgeProtection: enablePurgeProtection ? true : null
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

@description('ARM resource ID of the Key Vault.')
output keyVaultId string = keyVault.id

@description('Name of the Key Vault.')
output keyVaultName string = keyVault.name

@description('Key Vault URI (https://<name>.vault.azure.net/) — used by runtime secret-set scripts and consumers.')
output keyVaultUri string = keyVault.properties.vaultUri
