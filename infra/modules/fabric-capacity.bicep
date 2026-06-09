// ============================================================================
// Zava demo — Microsoft Fabric capacity (F-SKU) for the data-foundation demo.
// Resource: Microsoft.Fabric/capacities
//
// The Fabric capacity is the compute pool powering all Fabric workloads used by
// this demo (mirroring, Direct Lake, ontology, Data Agent, Real-Time
// Intelligence). The demo default is F64 in East US 2 (R9: F64 natively unlocks
// Copilot / Data Agent / Free-license Power BI viewing without a separate
// Copilot Capacity construct).
//
// This module is invoked ONLY on the fresh path (capacity.use_existing=false).
// On the existing path main.bicep skips this module entirely and passes through
// the supplied existing capacity id (see infra/main.bicep outputs).
//
// Source: R9 §"Bicep Template — Microsoft.Fabric/capacities"
//   research/2026-06-08-r9-fabric-capacity-region-cost.md
//   https://learn.microsoft.com/en-us/azure/templates/microsoft.fabric/capacities
//
// Public repo — NO secrets. Admin members are UPNs / object ids supplied via the
// parameter file as placeholders.
// ============================================================================

@description('Name of the Fabric capacity. 3–63 chars, lowercase alphanumeric, must start with a letter.')
@minLength(3)
@maxLength(63)
param capacityName string

@description('Azure region for the Fabric capacity (default: East US 2 per plan §1.7 — only US region supporting ALL demo capabilities).')
param location string = 'eastus2'

@description('Fabric capacity SKU name. Default F64 (R9 — natively unlocks Copilot / Data Agent / Free-license viewing).')
@allowed([
  'F2'
  'F4'
  'F8'
  'F16'
  'F32'
  'F64'
  'F128'
  'F256'
  'F512'
  'F1024'
  'F2048'
])
param skuName string = 'F64'

@description('Fabric capacity administrators — array of UPN strings and/or AAD object ids. Placeholders only in committed param files.')
param adminMembers array

@description('Tags applied to the Fabric capacity (cost tracking / governance).')
param tags object = {}

// Fabric capacities are always created with sku.tier = 'Fabric' (R9 key-properties
// table). The administration.members array designates the capacity admins.
resource fabricCapacity 'Microsoft.Fabric/capacities@2023-11-01' = {
  name: capacityName
  location: location
  sku: {
    name: skuName
    tier: 'Fabric'
  }
  properties: {
    administration: {
      members: adminMembers
    }
  }
  tags: tags
}

@description('ARM resource ID of the Fabric capacity — consumed by the Fabric workspace capacity assignment (Step 10) and pause/resume scripts (Step 4).')
output capacityId string = fabricCapacity.id

@description('Name of the Fabric capacity.')
output capacityName string = fabricCapacity.name
