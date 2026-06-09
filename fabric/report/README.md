# Zava Fleet Dashboard — Power BI report (PBIP / PBIR, as code)

This folder is the **source-of-truth definition** for the Zava car-rental Power BI report,
authored as a **Power BI Project (PBIP)** in the **enhanced report format (PBIR)** so it is
diff-friendly, Git-integrable, and deployable through the Fabric REST API. It is created /
updated programmatically by
[`../scripts/50_deploy_report.py`](../scripts/50_deploy_report.py) (plan Step 15).

The report **binds to the Step-14 Direct Lake on OneLake semantic model** `Zava Fleet Analytics`
(see [`../semantic-model/`](../semantic-model/README.md)) and applies the
**blue Zava theme** [`../theme/zava-blue-theme.json`](../theme/zava-blue-theme.json).

## Pages

| # | Page (`name`) | Purpose | Key visuals | Code-deployable? |
|---|---|---|---|---|
| 1 | `executive-overview` | KPI landing + "certified Databricks asset → governed Power BI insight" story | 4 KPI **cards** (Total Revenue, Fleet Utilization %, Idle Vehicle Count, Maintenance Cost), Revenue-by-City bar chart, title + story textboxes | ✅ Yes — standard PBIR JSON |
| 2 | `multi-city-map` | Zava sites across US cities (Seattle HQ + others) | **Map** bubble visual (city / latitude / longitude / Site Revenue) | ⚠️ Authored in Desktop, deployed as PBIR |
| 3 | `decomposition-tree` | Revenue breakdown by State → City → Vehicle Class | **Decomposition tree** | ⚠️ Authored in Desktop, deployed as PBIR |
| 4 | `revenue-forecast` | Revenue trend with forecast overlay | **Line chart + forecast** (Analytics pane) | ⚠️ Authored in Desktop, deployed as PBIR |

## ⚠️ Advanced visuals are authored in Power BI Desktop (R2)

Per research **R2 §5.5**, the **map**, **decomposition tree**, and **forecasting** visuals are best
**authored and validated in Power BI Desktop** (with the *enhanced report format / PBIR preview*
toggle enabled), then **committed and deployed as PBIR** — this is a *tooling* step (Desktop on the
author's machine), **not** a Fabric tenant click-through.

> "Advanced visuals — including maps, decomposition trees, and forecasting overlays — are best
> authored in Power BI Desktop and then deployed/edited as PBIR." — R2 §5.1 / §5.5

The committed `visual.json` files for those three visuals are **structurally valid PBIR placeholders**:
they carry the intended Zava **field bindings** (so Desktop authoring can complete them quickly) and a
`_zavaAuthoringNote` describing exactly what is finalized in Desktop (map render engine + bubble
sizing; decomposition-tree AI-split / expansion; forecast length / confidence band / seasonality).
Everything else on these pages — and the **entire Executive Overview page** — is fully deployable as
code with no Desktop step.

**Desktop authoring workflow (one-time, per visual change):**

1. Open `Zava Fleet Dashboard.pbip` in **Power BI Desktop** (enable *File → Options → Preview features
   → Store reports using enhanced metadata format (PBIR)*).
2. The report opens already bound to the `Zava Fleet Analytics` semantic model.
3. Author / refine the **map**, **decomposition tree**, and **forecast** visuals on their pages.
4. **Save** — Desktop validates and rewrites the PBIR JSON in place.
5. Commit the updated `definition/**` files. Re-deploy with `50_deploy_report.py` (idempotent).

## Folder layout (PBIR)

```
report/
├── Zava Fleet Dashboard.pbip                 # PBIP project file
└── Zava Fleet Dashboard.Report/
    ├── .platform                             # Fabric item metadata (type: Report)
    ├── definition.pbir                       # binds report -> semantic model (see token below)
    ├── definition/
    │   ├── version.json
    │   ├── report.json                       # theme collection (blue Zava) + report settings
    │   └── pages/
    │       ├── pages.json                     # page order + active page
    │       ├── executive-overview/
    │       │   ├── page.json
    │       │   └── visuals/                   # title, story, 4 KPI cards, revenue-by-city
    │       ├── multi-city-map/                # map (Desktop-authored)
    │       ├── decomposition-tree/            # decomposition tree (Desktop-authored)
    │       └── revenue-forecast/              # line + forecast (Desktop-authored)
    └── StaticResources/
        └── RegisteredResources/
            └── zava-blue-theme.json           # report-local copy of the canonical theme
```

## Semantic-model binding (token substitution — no secrets)

`definition.pbir` references the semantic model by a **placeholder token**:

```json
"connectionString": "semanticmodelid=__ZAVA_SEMANTIC_MODEL_ID__"
```

`50_deploy_report.py` resolves the real semantic-model GUID at deploy time (from `deploy_config.json`
/ CLI / the live workspace) and substitutes the token before sending the parts to the Fabric REST API.
After import it also calls `sempy_labs.report.report_rebind(...)` to guarantee the binding. The token
is a GUID placeholder — **never a secret**.

## Theme (blue Zava)

The canonical theme is [`../theme/zava-blue-theme.json`](../theme/zava-blue-theme.json). A copy is kept
under `StaticResources/RegisteredResources/` so the PBIR project is self-contained and opens cleanly in
Desktop. `50_deploy_report.py` injects the **canonical** theme content into the deployed registered
resource, keeping the single source of truth in `fabric/theme/`.

## Deploy

```bash
# Preview every Fabric REST call + assembled parts without auth or mutation (safe):
python fabric/scripts/50_deploy_report.py --dry-run

# Create/update the report in the Step-10 workspace, bound to the Step-14 model:
python fabric/scripts/50_deploy_report.py
```

The deploy is **idempotent** — it finds the existing report by name and calls `updateDefinition`
(in-place update), otherwise it creates the report. No secrets are read from config; auth is acquired
at runtime via `DefaultAzureCredential` (`az login` / managed identity / service principal) or the
ambient Fabric-notebook identity.
