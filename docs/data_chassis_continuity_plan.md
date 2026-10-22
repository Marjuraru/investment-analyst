# Continuidad, retención y neutralidad de activos

Registro único de la secuencia `DATA-CHASSIS 46 → 47 → 48 → 49`. Autoridad vigente: Issue #349,
base `origin/main@b5e714ab87d269b01bf2e41c99cfde8ae807f2df`, `route_effect: ADVANCES`, policy
`HUMAN`. Este documento es evidencia y planificación; no autoriza importar, borrar, cambiar schema,
activar otro workspace ni declarar el cierre de `DATA-CHASSIS` o Q1–Q7.

`route_transition:DATA-CHASSIS:ADVANCES`

## 1. Estado observado y límites de esta medición

Medición read-only del 2026-10-22, antes del despliegue candidato de este bloque. No se adquirieron datos de proveedores, no se abrió un writer v1,
no se modificó el workspace activo y no se borró ningún payload.

| Superficie | Estado medido | Evidencia |
| --- | --- | --- |
| Release activa (medición previa al despliegue) | `daad58d97032dfaa9ae3023c9f7475d1b8687db6`; previa `54acfd644426e92034f7c42e29e1b3b06e97d8c4` | `runtime/deployment_state.json` |
| Workspace v2 | UUID `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; root `active-v2-20261021`; formato 2 | `runtime/deployment_workspace_binding.json` |
| DB v2 actual | `281.817.088 B` | `stat` sobre `storage/v2/index.duckdb` |
| Último inventario durable v2 | raw `235.416`; observaciones `24.920`; métricas `25.348`; diagnósticos `11` | `ops/data-chassis-45/fresh-start-20261021/final-inventory.json` (2026-10-21T23:54Z) |
| Backup restaurado c0080d96 | DB `252.456.960 B`, SHA-256 `bebdd2997292768ab6dbef5d1ca3d8973599b2ab2f7305ded1a0fb258d3d9f6c`; 144 volúmenes; `375.562.914 B` descargados | `remote-restore-closure.json` y `final-readback.json` (2026-10-22T04:54Z) |
| Delta posterior al backup | `29.360.128 B` entre DB restaurada y DB actual; no cubierto por c0080d96 | diferencia de tamaños; requiere backup nuevo antes de importar o retirar |
| Scheduler observado | 13 jobs, 3 activos; FRED unavailable y backlog 13F explícitos | Issue #349; no se reejecutó adquisición en este bloque |
| v1 local | UUID `19cf83b5-8df3-4663-9369-4b61f86eceea`; DB `10.802.704.384 B`; sin binding activo en runtime | `workspaces/default/manifest.json`, `stat` y `deployment_workspace_binding.json` |

La copia `source-copy-b-20261008` fue una copia de la misma identidad v1 y ya fue retirada el
2026-10-13 con recibo: `928.706` archivos y `14.337.762.116 B` confirmados
(`ops/data-chassis-44/b-retirement-20261013.json`). Su backup v2 `6c69b9df-d530-5502-8c95-5c9b756eec86`
(676 volúmenes) fue restaurado el 2026-10-14 con workspace `9ffeb53b-4342-4b4a-82d4-16c83d517d7b` y
conteos raw `928.043`, observaciones `520.180`, métricas `1.947.523` y diagnósticos `1.777`
(`backup_manifest.json` y `drive-restore-20261014.json`). Esa retirada es evidencia previa; no se
repite ni se amplía aquí.

### Neutralidad de selección

`app-core.js` ya no inicializa una selección por ticker. La selección inicial parte de `null` y
`resolveMarketAssetSelection` conserva una selección explícita válida o toma el primer activo del
orden contractual del catálogo. El catálogo vigente tiene 31 activos ordenados por `asset_id`; el
primero es `crypto:ada-usd` y `equity:us:aapl` ocupa la sexta posición. No se reordena el catálogo y
no se consulta, escribe ni programa un job AAPL implícito al seleccionar otro activo. Los adaptadores
Apple y `aapl_local_service.lock` permanecen como compatibilidad explícita, no como default.

## 2. Inventario documental

Inventario vivo: **107 Markdown tracked** en `git ls-files '*.md'` en la base, más **3 Markdown
protegidos no tracked** (`issue230_body.md`, `docs/storage_cleanup_decision_2026-10-11.md` y
`docs/external_technical_review_2026-10-21.md`). No hay duplicados exactos entre los 107 tracked.
La tabla incluye además los dos documentos nuevos de este bloque (`docs/README.md` y este registro);
el árbol final tendrá **109 Markdown tracked**.

Disposición: `PROTECTED` 17, `CANONICAL` 81, `ARCHIVE_REFERENCE` 8, `MERGE_INTO` 3 y `RETIRE` 0.
`ARCHIVE_REFERENCE` y `MERGE_INTO` son estados de retención: ningún archivo se borra en 46. Las
referencias entrantes y los consumidores en `scripts/` se calcularon sobre los bytes de la base; la
columna `Refs` cuenta documentos tracked que mencionan el nombre o la ruta.

| Documento | Decisión | Autoridad / consumidor | Refs | Scripts | Bytes |
| --- | --- | --- | ---: | --- | ---: |
| `docs/README.md` | CANONICAL | Índice documental único; nuevo en 46 | — | — | nuevo |
| `docs/data_chassis_continuity_plan.md` | CANONICAL | Registro único de continuidad, retención y neutralidad; nuevo en 46 | — | — | nuevo |
| `.agents/rules/investment-analyst-core.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 4 | scripts/check_workflow_guards.py | 9111 |
| `.agents/skills/audit/SKILL.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 2 | scripts/check_workflow_guards.py | 3709 |
| `.agents/skills/build/SKILL.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 2 | scripts/check_workflow_guards.py | 5077 |
| `.agents/skills/investment-block-flow/SKILL.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 2 | scripts/check_workflow_guards.py | 1683 |
| `.agents/skills/plan/SKILL.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 2 | scripts/check_workflow_guards.py | 3402 |
| `.agents/skills/ui/SKILL.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 1 | scripts/check_workflow_guards.py | 1666 |
| `.claude/commands/audit.md` | PROTECTED | Herramienta de cliente o plantilla de gobernanza; no es documentación de producto | 0 | — | 317 |
| `.claude/commands/build.md` | PROTECTED | Herramienta de cliente o plantilla de gobernanza; no es documentación de producto | 0 | — | 311 |
| `.claude/commands/plan-review.md` | PROTECTED | Herramienta de cliente o plantilla de gobernanza; no es documentación de producto | 0 | — | 303 |
| `.claude/commands/ui.md` | PROTECTED | Herramienta de cliente o plantilla de gobernanza; no es documentación de producto | 0 | — | 278 |
| `.github/pull_request_template.md` | PROTECTED | Herramienta de cliente o plantilla de gobernanza; no es documentación de producto | 0 | — | 286 |
| `AGENTS.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 16 | scripts/check_workflow_guards.py | 19346 |
| `CLAUDE.md` | PROTECTED | Herramienta de cliente o plantilla de gobernanza; no es documentación de producto | 0 | — | 1805 |
| `README.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 5043 |
| `docs/aapl_complete_snapshot.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3815 |
| `docs/aapl_workspace_bootstrap.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 6039 |
| `docs/alpaca_history.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2538 |
| `docs/application_facade.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 8676 |
| `docs/application_runtime.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 6743 |
| `docs/architecture.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 10190 |
| `docs/archive/data_chassis_45_migration_runbook_20261021.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 1 | — | 124628 |
| `docs/asset_catalog.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 11449 |
| `docs/asset_preferences.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 3 | — | 5837 |
| `docs/automated_screening_alerts.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 3 | — | 16669 |
| `docs/basic_functional_release_plan.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 6 | scripts/check_workflow_guards.py | 94340 |
| `docs/bvl_market_strategy.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 14206 |
| `docs/capability_driven_runtime.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 9881 |
| `docs/cazatiburones.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 5 | — | 20321 |
| `docs/cazatiburones_activity_events.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 1173 |
| `docs/cazatiburones_activity_metrics.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 6183 |
| `docs/cazatiburones_activity_observations.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 5682 |
| `docs/cazatiburones_activity_rules.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 6718 |
| `docs/cazatiburones_declared_activity.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 4883 |
| `docs/cazatiburones_institutional_change.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 925 |
| `docs/cazatiburones_institutional_composition.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 3310 |
| `docs/cazatiburones_institutional_composition_v2.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2422 |
| `docs/cazatiburones_institutional_concentration.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2426 |
| `docs/cazatiburones_institutional_effective_holdings.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2428 |
| `docs/cazatiburones_institutional_events.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 4928 |
| `docs/cazatiburones_institutional_metrics.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 1720 |
| `docs/cazatiburones_institutional_observations.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3492 |
| `docs/cazatiburones_institutional_weight.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 1732 |
| `docs/cazatiburones_notifications.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 5484 |
| `docs/cazatiburones_universe_activity.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 1771 |
| `docs/coinbase_history.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2812 |
| `docs/coinbase_intraday.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 4674 |
| `docs/consolidated_diagnostic_query.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 4967 |
| `docs/continuous_integration.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3900 |
| `docs/corporate_valuation_point_in_time.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 7022 |
| `docs/crypto_derivatives.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 7 | — | 8208 |
| `docs/data_chassis.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 3 | — | 117825 |
| `docs/data_chassis_43_migration_runbook.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 1 | — | 8478 |
| `docs/data_chassis_44_backup_cutover_runbook.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 0 | — | 9241 |
| `docs/data_chassis_45_acceptance_runbook.md` | MERGE_INTO | Consolidar en docs/data_chassis_continuity_plan.md; retirar sólo tras el tramo 49 | 3 | — | 5130 |
| `docs/data_chassis_baseline.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 3 | — | 30875 |
| `docs/data_chassis_closure_plan.md` | MERGE_INTO | Consolidar en docs/data_chassis_continuity_plan.md; retirar sólo tras el tramo 49 | 2 | — | 2912 |
| `docs/data_chassis_v1_reintegration_plan.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 5 | — | 6359 |
| `docs/data_chassis_v2_fresh_start_plan.md` | MERGE_INTO | Consolidar en docs/data_chassis_continuity_plan.md; retirar sólo tras el tramo 49 | 18 | — | 23974 |
| `docs/data_contracts.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 7108 |
| `docs/dependency_management.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 2778 |
| `docs/development.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 2328 |
| `docs/development_protocol.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 16 | scripts/check_workflow_guards.py | 37276 |
| `docs/fred_alfred_point_in_time.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 3 | — | 5330 |
| `docs/fundamental_research_foundation.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 9244 |
| `docs/historical_research_data.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 5639 |
| `docs/incidents/data_chassis_45_wsl_spill_20261016.md` | PROTECTED | Gobernanza o evidencia inmutable; el guard verifica su hash de base | 1 | — | 24036 |
| `docs/instrument_correspondence.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3051 |
| `docs/local_interface.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 4 | — | 80137 |
| `docs/local_interface_design_system.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 48255 |
| `docs/market_comparison.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 1431 |
| `docs/market_diagnostic.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3678 |
| `docs/market_statistics.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3969 |
| `docs/operational_readiness.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 17774 |
| `docs/operational_runner.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 11114 |
| `docs/planning_context/claude_review_response.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 0 | — | 41785 |
| `docs/planning_context/external_strategy_review_decisions_2026-09.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 0 | — | 13349 |
| `docs/planning_context/external_technical_review_decisions_2026-10-21.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 2 | — | 13676 |
| `docs/planning_context/product_feedback_decisions_2026-09.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 0 | — | 4254 |
| `docs/product_roadmap.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 4 | scripts/check_workflow_guards.py | 49548 |
| `docs/professional_analysis_interface.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 5873 |
| `docs/provider_asset_resolution.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3668 |
| `docs/read_path_efficiency.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2558 |
| `docs/release_acceptance.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 9919 |
| `docs/research/crypto/crypto_planner_brief.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 0 | — | 7183 |
| `docs/research/crypto/crypto_research_synthesis_2026-08.md` | ARCHIVE_REFERENCE | Evidencia histórica; consumidor: AUDIT/PLAN y trazabilidad de decisiones | 1 | — | 22881 |
| `docs/research_information_strategy.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 6777 |
| `docs/scheduler_memory_budget.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 6855 |
| `docs/sec_beneficial_ownership.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 2066 |
| `docs/sec_document_corpus.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 10256 |
| `docs/sec_document_timeline.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 6255 |
| `docs/sec_edgar_foundation.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3350 |
| `docs/sec_fundamental_diagnostic.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 4890 |
| `docs/sec_fundamental_metrics.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 4360 |
| `docs/sec_fundamental_observations.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2944 |
| `docs/sec_fundamental_point_in_time.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2896 |
| `docs/sec_institutional_holdings.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 17567 |
| `docs/sec_institutional_manager_universe.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 9583 |
| `docs/sec_institutional_semantics.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 3969 |
| `docs/sec_issuer_refresh.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 5763 |
| `docs/sec_ownership.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 1261 |
| `docs/simulated_pipeline.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 2366 |
| `docs/smv_bvl_registry.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 5814 |
| `docs/storage.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 1 | — | 4943 |
| `docs/unified_market_history.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 3000 |
| `docs/universe_coverage.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 2 | — | 3903 |
| `docs/workspace.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 5997 |
| `docs/workspaces.md` | CANONICAL | Contrato o guía vigente; consumidor: operación, desarrollo o consulta | 0 | — | 7324 |

Los tres Markdown protegidos no tracked conservan los hashes declarados por el Issue y se clasifican
`PROTECTED`: no se borran, mueven ni editan en 46. En los dos documentos nuevos, `Refs` y `Scripts`
figuran como `—` porque no existían en la base; sus referencias entrantes se crean con este cambio.
`issue230_body.md` `5cd428c38b50780ce750cc8ff629f31e9a34972a6d4cbe8d4ba3d63e6574453d`,
`docs/storage_cleanup_decision_2026-10-11.md`
`6886f2ea181ea2b8c548edecf8bd89a97f6a1f4fe05d8a611c0caf9bf1e2fdee` y
`docs/external_technical_review_2026-10-21.md`
`b67c1db23124504ebdba5eb1ce098a4cc05f7b60453dc78fc714b8d8aa344dc1`. El checkpoint dormante
`.worktrees/data-chassis-45/docs/incidents/data_chassis_45_planner_handoff_20261017.md` conserva
`c4b986b2bf98fe87f6d3db76e375a8fb1a1997ee3f4f05f75b469d54418050a2`; no se accedió a él como writer.

## 3. Inventario de almacenamiento local y remoto

Método: una pasada serial read-only con `du` (asignado y aparente), `stat`, `df`, `sha256sum` sólo
sobre candidatos seleccionados y `rclone size/lsf` para el remoto. No se hasheó el corpus completo ni
se abrió la base v2. Los bytes asignados y aparentes se presentan separados; no se convierten en
"ahorro" ni en espacio recuperable.

### Raíces locales

| Raíz | Asignado | Aparente | Decisión | Consumidor / evento |
| --- | ---: | ---: | --- | --- |
| `workspaces/default` (v1) | `16G` | `14G` | KEEP_ACTIVE | Fuente v1 para el archivo final de 47 y la selección de 48; retirada sólo en 49 con restore externo probado |
| `workspaces/data-chassis-43-rehearsal` | `8,3G` | `6,3G` | RETIRE_AFTER | Restore-v2 y recibos de 43/44; 47 debe demostrar que no contiene bytes únicos; retirada en 49 |
| `workspaces/active-v2-20261021` | `2,0G` | `1,3G` | KEEP_ACTIVE | Workspace operativo; DB v2 `281.817.088 B`; backup/restore por corte antes de importar |
| `ops` | `16G` | `16G` | Mixta | Evidencia de cortes; ver desglose |
| `runtime` | `380M` | `335M` | Mixta | Releases, mirror y herramientas; ver hardlinks |
| `projects/investment-analyst` + worktrees | `251M` (worktrees) | — | Mixta | Código y checkpoints; el primary no se suma para evitar doble conteo; ningún worktree se retira en 46 |
| `/mnt/c` | `449G` usados | `27G` libres | Sin acción | `df` del host; 95% usado; no autoriza limpieza ni compactación |
| `/` (WSL) | `93G` usados | `863G` libres | Sin acción | `df`; no es presupuesto de importación |

### `ops` por subraíz

| Ruta | Bytes | Decisión | Consumidor / condición |
| --- | ---: | --- | --- |
| `ops/data-chassis-45` | `16G` | Mixta | Evidencia de los cortes 45 y del arranque v2 |
| `ops/data-chassis-45/independent-reads-W-20261017` | `4,3G` | RETIRE_AFTER | Controles de lectura independiente; retirar en 49 sólo con manifiesto/digest retenido y sin consumidor |
| `ops/data-chassis-45/fresh-start-20261021` | `3,1G` | Mixta | Recibos, manifests y restore-probe; los recibos son ARCHIVE_ONLY |
| `ops/data-chassis-45/physical-delta-G3-20261021` | `2,3G` | RETIRE_AFTER | Evidencia de delta físico; digest `04a51aa...` registrado; retirar en 49 si ningún informe la abre |
| `ops/data-chassis-45/physical-delta-W2-20261018` | `2,3G` | RETIRE_AFTER | `files.jsonl` idéntico al control `1854b189...` de independent-reads; un solo consumidor retenido |
| `ops/data-chassis-45/recovery-W2-20261017` | `1,1G` | RETIRE_AFTER | Recuperación intermedia; 49 tras verificar que el backup final cubre su diferencia |
| `ops/data-chassis-45/prepare-W2-20261018` | `602M` | RETIRE_AFTER | Preparación intermedia; mismo gate que W2 |
| `ops/data-chassis-45/retire-W2-20261019` | `598M` | RETIRE_AFTER | Evidencia de retirada intermedia; recibo y digest primero |
| `ops/data-chassis-45/physical-delta-base-20261015` | `574M` | RETIRE_AFTER | Base de comparación; retirar en 49 si el informe final conserva el agregado y el hash |
| `ops/data-chassis-45/physical-delta-W-20261017` | `338M` | RETIRE_AFTER | Igual gate que la base de delta |
| `ops/data-chassis-45/physical-delta-pre-f-20261015` | `337M` | RETIRE_AFTER | Igual gate que la base de delta |
| `ops/data-chassis-45/cleanup-E0-20261018` | `54M` | RETIRE_AFTER | Evidencia de limpieza previa; recibo retenido |
| `ops/data-chassis-45/f-complement-20261015` | `54M` | RETIRE_AFTER | Complemento de corte; 49 tras cubrir su diferencia |
| `ops/data-chassis-44` | `411M` | Mixta | `drive-restore-staging` ~`402 MiB` aparentes (dos JSONL `421.403.653 B`) y recibos; ARCHIVE_ONLY hasta 47 |
| `ops/data-chassis-43` | `5,0M` | ARCHIVE_ONLY | Intentos, presupuestos y logs; trazabilidad de 43 |
| `ops/reports` | `21M` | ARCHIVE_ONLY | Baseline y ciclos; no se reescriben |
| `ops/samples` | `191M` | ARCHIVE_ONLY | Muestras del colector; 49 decide sin borrar evidencia única |
| `ops/deployment-backups` | `180K` | KEEP_ACTIVE | Configuración de despliegue; recuperación operativa |
| `ops/__pycache__` y `ops/*.bak-2026-09-24` | `100K` | RETIRE_AFTER | Regenerables; retirar en 49 sin afectar recibos |

### Archivos grandes seleccionados

| Archivo | Bytes | Decisión | Nota |
| --- | ---: | --- | --- |
| `workspaces/default/.../investment_analyst.duckdb` | `10.802.704.384` | KEEP_ACTIVE | Fuente v1; no se copia encima de v2 |
| `workspaces/data-chassis-43-rehearsal/restore-v2/.../index.duckdb` | `2.707.959.808` | RETIRE_AFTER | Restore previo; 47 comprueba cobertura única |
| `workspaces/active-v2-20261021/.../index.duckdb` | `281.817.088` | KEEP_ACTIVE | Activo; backup c0080d96 cubre sólo `252.456.960 B` |
| `ops/.../physical-delta-base-20261015/physical-delta-index.jsonl` | `600.982.252` | RETIRE_AFTER | Agregado/hash al informe final antes de 49 |
| `ops/.../physical-delta-G3-20261021/files.jsonl` | `354.365.084` | RETIRE_AFTER | Hash `04a51aa...`; distinto de W2 |
| `ops/.../physical-delta-W2-20261018/files.jsonl` | `354.348.683` | RETIRE_AFTER | Hash `1854b18...`; idéntico al control de independent-reads |
| `ops/.../independent-reads-W-20261017/physical-delta/controls/1854b189....jsonl` | `354.348.683` | RETIRE_AFTER | Duplicado exacto demostrado de W2; no borrar antes de 49 |
| `ops/.../physical-delta-W-20261017/files.jsonl` | `353.983.349` | RETIRE_AFTER | Mismo gate |
| `ops/.../independent-reads-W-20261017/physical-delta/controls/491f0f56....jsonl` | `353.983.349` | RETIRE_AFTER | Mismo tamaño y hash distinto; no es duplicado |
| `ops/data-chassis-44/drive-restore-staging/packed-backup-volumes.jsonl` | `233.899.521` | ARCHIVE_ONLY | Evidencia de restore 44; remoto retenido |
| `ops/data-chassis-44/drive-restore-staging/source-inventory.jsonl` | `187.504.132` | ARCHIVE_ONLY | Inventario fuente de 44 |
| `ops/data-chassis-45/fresh-start-20261021/restore-probe/.../index.duckdb` | `252.456.960` | ARCHIVE_ONLY | Byte-idéntico al backup c0080d96; prueba de restore |
| `workspaces/default/state/.../snapshot.json` (tres journals) | `16.672.326` / `14.915.630` / `8.054.579` | ARCHIVE_ONLY | Estado operativo v1; no se reintroduce como trabajo pendiente |

### Hardlinks y doble conteo

Los `du -sh` por directorio de `runtime/releases` se redondean (≈`281M`) y no representan el total
físico porque comparten site-packages por hardlink. Contabilizando por inodo único hay **14.582
inodos, `255.033.576 B` aparentes y `286.097.408 B` asignados** para el conjunto de releases. Por
tanto, el total físico no es la suma de los `du` por directorio. La pasada `-links +1` encontró
**3.837 archivos hardlinked en `runtime`** (site-packages de releases) y **0 en `ops` y
`workspaces`**; las DB y los JSONL grandes se contabilizan por archivo.

### Backup remoto cifrado

`rclone size ia_drive_crypt:` respondió `6.205` objetos y `8.608.398.417 B`. Es el conjunto remoto
completo, no el tamaño de un backup individual; no se usa como cifra de ahorro. La configuración
ligada por el recibo es `ia_drive_crypt`, fingerprint `955df275...` y `folder_id_sha256`
`238302db...`; no se imprimieron secretos.

| Categoría remota | Objetos inmediatos | Bytes inmediatos | Decisión |
| --- | ---: | ---: | --- |
| `manifests/` | 4 | `196.179` | ARCHIVE_ONLY; cubre generaciones |
| `controls/` | 3 | `421.404.329` | ARCHIVE_ONLY; controles de restore |
| `controls-v2/` | ≥5 directorios (listado truncado) | no medido | UNKNOWN; inventario acotado en 47 |
| `volumes/` | 1.020 | `2.761.955.827` | KEEP_ACTIVE; restore c0080d96 usó 144 |
| `complement-manifests/` | 1 | `14.530` | ARCHIVE_ONLY |
| `complement-objects/` | 44 | `280.000.941` | ARCHIVE_ONLY |
| `physical-delta/{controls,manifests,objects}/` | 3 directorios | no medido | UNKNOWN; 47 |
| `probes/` | 0 inmediatos | 0 | UNKNOWN; 47 verifica si está vacío o anidado |

### Ausencias y UNKNOWN con acción asignada

| Objeto | Estado | Acción / responsable / tramo |
| --- | --- | --- |
| `/mnt/c/temp/data_chassis_0_backup` | Ausente en la fecha; la decisión de 2026-10-11 no acreditaba su borrado | 47 verifica host, papelera y copia externa; no se infiere borrado ni se reconstruye una cifra |
| `/home/marjuraru/investment-analyst-cold-20260918-pre-deploy` | Ausente | 47 busca evidencia de retirada; si no existe, UNKNOWN permanente documentado |
| `source-copy-b-20261008` | Ausente; retirada con recibo 2026-10-13 | 47 verifica que el backup remoto 6c69b9df cubre sus diferencias |
| `restore-v2` | Recibo `phase=retired`, bytes aún presentes | 47 reconcilia bytes y consumidores; 49 retira sólo con cobertura externa |
| `controls-v2`, `physical-delta`, `probes` remotos | Tamaño no medido | 47 inventario acotado y hash de manifiestos; no se borra nada |
| Delta v2 posterior a c0080d96 | `29.360.128 B` no cubiertos | 47 ejecuta backup/restore nuevo antes de importar o retirar |
| Backlog 13F y FRED unavailable | Explícitos en el Issue | 47/48 resuelven por consumidor; no se declaran cero |
| `/mnt/c/temp/ia-canvas` | Presente y fuera del alcance del producto | 47 confirma propietario; no se toca en 46 |

## 4. Selección histórica por familia

v1 y v2 tienen identidades de workspace, universos y decisiones de exclusión distintos. Las cifras
se presentan lado a lado; **no se restan ni se convierten en porcentaje migrado**. La consulta v1 fue
read-only, con `threads=1`, `memory_limit=1GiB` y sin escritura.

| Familia | v1 observado (read-only) | v2 durable (último inventario) | Selección | Consumidor / tramo |
| --- | --- | --- | --- | --- |
| Raw 13F institucional | `830.286` registros (asset_id nulo) | backlog 13F explícito | IMPORT_LATER | Cazatiburones institucional; 47 piloto, 48 lotes |
| Raw mercado diario listado | `1.642–1.645` barras por activo de la muestra (AMD 1.645; TSM 1.642; BVN 1.642) | cobertura reciente por activo | IMPORT_LATER por ventana y warm-up | Diagnóstico de mercado/watchlist; 48 |
| Raw cripto diario | BTC `4.111`; ETH/ADA/LINK `658` cada uno | cobertura reciente | IMPORT_LATER por ventana | Diagnóstico cripto; 48 |
| Raw cripto `minute_1` | BTC `62.820` | excluido del rebuild v2 por decisión existente | ARCHIVE_ONLY | Sin importación; se conserva para trazabilidad |
| Observaciones fundamentales | `quarterly 19.293`; `annual 7.665` | cobertura SEC reciente | IMPORT_LATER por emisor y ventana | Fundamentales/valoración; 48 |
| Observaciones de mercado | `day_1 157.157`; `hour_1 30.320`; `minute_1 314.099`; `event 3.007` | `24.920` observaciones totales | IMPORT_LATER sólo mercado diario/eventos elegidos; minute histórico ARCHIVE_ONLY | Analítica/reportes; 47–48 |
| Métricas | `2.098.454` (market `1.481.321`; crypto `602.497`; cazatiburones `6.132`; valuation `5.501`; fundamental `3.003`) | `25.348` | Recomputar en v2 desde inputs; importar sólo artefactos referenciados que no puedan reproducirse | Analítica y reportes publicados; 47 decide caso a caso |
| Diagnósticos | `2.121` (market `1.899`; fundamental `222`) | `11` | IMPORT_LATER sólo si un reporte/consumidor los referencia; resto ARCHIVE_ONLY | Reportes y auditoría; 47–48 |
| Checkpoints incrementales | presentes en v1 | formato v2 distinto | ARCHIVE_ONLY; no importar | Trazabilidad; 47 |
| Preferencias | historial v1 | preferencias v2 por UUID | KEEP_ACTIVE v2; reaplicar por servicio, nunca overwrite | UI/scheduler; 47 verifica |
| Scheduler/outbox/acks | estado v1 | estado v2 vigente | ARCHIVE_ONLY; no reintroducir trabajo pendiente | Operación; 47 |
| Backups/controles | B y W/W2/G | c0080d96 y remoto | KEEP_ACTIVE/ARCHIVE_ONLY; no borrar | Recuperación; 47 verifica |
| Documentos/CAS | corpus SEC v1 | CAS v2 parcial | IMPORT_LATER de documentos seleccionados y sus dependencias | Corpus/timeline; 47 piloto, 48 lotes |

Cobertura de backup: el backup B `6c69b9df` conserva totales de la copia v1 migrada (raw `928.043`,
observaciones `520.180`, métricas `1.947.523`, diagnósticos `1.777`), pero su manifiesto no desagrega
esas cifras por familia/activo/rango. El backup c0080d96 cubre sólo el v2 nuevo. El workspace v1
`default` no tiene todavía un archivo externo final restaurado; obtenerlo y probar su restore es el
gate de 47. Por eso ninguna fila de esta tabla declara cobertura completa. El warm-up y las
dependencias transitivas se resuelven por consumidor en 47; no se asume un rango universal de días.

Rangos `available_at` v1 (UTC, consulta read-only): raw `2002-05-06T14:21:32Z` a
`2026-10-21T18:58:12Z`; observaciones `2009-08-03T19:45:52Z` a `2026-10-21T18:51:55Z`; métricas
`2010-08-03T18:16:00Z` a `2026-10-21T18:51:54Z`; diagnósticos `2025-04-17T10:16:18Z` a
`2026-10-21T18:51:32Z`. El baseline de 2026-09-15 (`294.498` raw y `1.236.466` métricas) es
histórico y no se usa como cifra actual.

UNKNOWN de selección: el rango exacto por activo/fuente dentro de cada familia y la lista cerrada de
artefactos referenciados no se derivan de un `GROUP BY` parcial. 47 debe producir la tabla paginada
por raíz/UUID/familia/activo/fuente/rango/`available_at`, con deduplicación por identidad y contenido,
antes de importar. Ninguna ausencia se completa con cero.

## 5. Secuencia obligatoria 46 → 49

| Tramo | Frontera y salida obligatoria | Entrada / qué no incluye |
| --- | --- | --- |
| **46, este bloque** | Formalización documental, selección neutral corregida, inventario/selección y calendario por eventos | Sin importación ni eliminación de datos grandes; no audita indefinidamente todo el producto |
| **47, archivo recuperable y piloto** | Cubrir el delta histórico único no respaldado; selección cerrada; piloto scratch no vacío de mercado+fundamental/documental, solape LIVE/HISTORICAL, conflictos/restart/PIT; representación autorizada por PLAN antes de BUILD | Inventario y presupuesto de 46; no copiar una DB encima del activo ni reparar F5 por rutina |
| **48, reincorporación útil** | Importar la selección por lotes acotados con checkpoints, writer único, prioridad por consumidor, sin cambiar fechas/IDs, igualdad de conjuntos/lineage y backup/restore por corte; consultas operativas | Piloto y contrato verificados; no importar todo el archivo por defecto; no volver a descargar historia reproducible sólo para simplificar |
| **49, limpieza física y cierre OBLIGATORIOS** | Ejecutar la lista literal de retiradas elegibles con hashes/recibos/último consumidor; verificar recuperación y servicio; medir bytes antes/después; reconciliar docs, Q1–Q7 y pendientes; todo retenido tiene motivo/evento o retención permanente explícita | Últimos consumidores satisfechos; sin borrar evidencia única ni datos UNKNOWN; compactación VHD aparte con mecanismo/ventana/autoridad concretos |

47 puede retirar staging propio ya verificado dentro de su futura lista literal; no se pospone todo
residuo a 49. 49 no se omite porque v2 ya esté activo ni porque "haya espacio suficiente". Un objeto
importante puede permanecer `ARCHIVE_ONLY` permanente: se explicita cobertura, ubicación y
recuperación; no se borra para cumplir una cuota. El cierre global sólo procede con criterios
comparables satisfechos o una nueva decisión explícita de alcance. Ninguna fila de este documento
constituye una promesa Q1–Q7 `MET`.

## 6. Calendario de retirada por evento

No se ejecuta ninguna retirada en 46. Cada candidato tiene owner, evento, prueba de recuperación y
tramo. "Recuperación" significa que el conjunto retenido cubre las diferencias por manifiesto/hash o
que el artefacto es reconstruible desde Git/`uv.lock`; no se infiere de nombre, fecha o tamaño.

| Candidato | Bytes | Último consumidor | Evento / gate | Prueba de recuperación | Owner / tramo | Decisión |
| --- | ---: | --- | --- | --- | --- | --- |
| `workspaces/default` v1 | `16G` | Archivo final y selección histórica | 47 archiva y restaura el delta; 48 importa y verifica el último consumidor | Backup externo final + restore independiente + manifiesto/hash; B no cubre todo v1 | BUILD_PRODUCT / 49 | RETIRE_AFTER |
| `workspaces/data-chassis-43-rehearsal` | `8,3G` | Evidencia de 43/44 y restore-v2 | 47 demuestra cero bytes únicos; 49 retira | Backup 6c69b9df restaurado (workspace 9ffeb53b) y recibos de lifecycle | BUILD_PRODUCT / 49 | RETIRE_AFTER |
| `ops/data-chassis-45/*` grandes deltas | `~12G` | Informes de cortes 45 | Agregado/hash retenido; ningún informe abre el archivo; 49 | Manifiesto y SHA-256 registrados; par duplicado `1854b18...` conservado en el otro path | BUILD_PRODUCT / 49 | RETIRE_AFTER |
| `ops/data-chassis-44/drive-restore-staging` | ~`402 MiB` aparentes | Evidencia de restore 44 | 47 confirma que el remoto retenido cubre el staging; 49 | Manifiesto remoto y recibos de restore 44 | BUILD_PRODUCT / 49 | RETIRE_AFTER |
| `ops/__pycache__` y `.bak` | `100K` | Ninguno | 49 | Regenerable; no contiene evidencia única | BUILD_PRODUCT / 49 | RETIRE_AFTER |
| Releases `0e93c6d`, `5fb973d`, `e10c592`, `dbbdfd0` | `~203M` | Recuperación de releases no referenciadas | 49 confirma release actual+previa y rebuild probado | SHA de Git + `uv.lock` + `stage` hermético | BUILD_PRODUCT / 49 | RETIRE_AFTER |
| Worktrees `data-chassis-43`, `44`, `dev-ci-timeout-45` | `~67M` | Git y evidencia de PR | Merge comprobado, cero cambios únicos y ningún checkpoint; 49 usa `git worktree remove` | Commit/ref de Git; sin `clean --force` ni `rm` | BUILD_PRODUCT / 49 | RETIRE_AFTER |
| Worktree `data-chassis-45` | `167M` | Checkpoint dormante protegido | No retirar hasta decisión humana/PLAN sobre el checkpoint | Git + checkpoint hash; hoy no hay cobertura de retirada | PLAN / 49 | CONFLICT/UNKNOWN |
| `~/.codex/worktrees/7d4a` y `d56c` | `27M` | Desconocido; no se pudo leer su estado | 49 identifica owner y cambios; no se toca en 46 | Git/owner; si hay cambios, no se retira | PLAN / 49 | UNKNOWN |
| Remoto `ia_drive_crypt` | `8,6G` | Recuperación de backups y controles | No se retira en 46–48; 49 sólo duplicados con restore del conjunto retenido | Manifiestos, controles y clave recuperable; restore independiente | BUILD_PRODUCT / 49 | KEEP_ACTIVE |
| `data_chassis_0_backup` y cold copy | ausentes | — | 47 verifica host/papelera/externo | No hay objeto que retirar; ausencia no es prueba de borrado | PLAN / 47 | UNKNOWN |
| `active-v2-20261021` | `2,0G` | Operación v2 | Nunca se retira en esta secuencia | Backup c0080d96 cubre sólo `252.456.960 B`; delta nuevo exige backup | Operación / — | KEEP_ACTIVE |

Las filas de `ops/data-chassis-45/*` son agregados de familia y no se suman con las subraíces del
inventario; esta tabla no produce un total físico. Los bytes de una fila no se suman con los de otra
cuando una contiene a la otra.

## 7. Evidencia ejecutada

Comandos read-only y resultados relevantes (2026-10-22):

- `sha256sum` de los cuatro checkpoints protegidos: coinciden exactamente con los hashes del Issue.
- `git worktree list` y `git status --short` de cada worktree: el writer 46 está en
  `codex/data-chassis-46`; 45 conserva su checkpoint untracked; 43, 44 y dev-ci están limpios.
- `du -sh`/`du -sh --apparent-size`: `ops` 16G/16G; `runtime` 380M/335M; `workspaces` 26G/21G;
  `default` 16G/14G; `rehearsal` 8,3G/6,3G; `active-v2` 2,0G/1,3G.
- `df -h`: `/` 863G libres; `C:` 27G libres (95% usado). No es una autorización de limpieza.
- `sha256sum` seleccionado: W2 `1854b189...` = control de independent-reads; G3 `04a51aa...`
  distinto; el control `491f0f56...` tiene el mismo tamaño que W2 pero hash distinto.
- `rclone size ia_drive_crypt:`: `6.205` objetos, `8.608.398.417 B`; `rclone lsf` por categoría.
- Consulta DuckDB v1 read-only: `929.781` raw; `531.541` observaciones; `2.098.454` métricas;
  `2.121` diagnósticos; rangos UTC indicados arriba.
- `pytest` focalizado: 9 pruebas nuevas y 371 pruebas existentes de frontend/preferencias/fachada
  pasaron antes de este registro.
- Smoke JS real: cinco casos ejecutados con Node sobre la función extraída de `app-core.js`
  (sin selección, sin AAPL, orden contractual, selección explícita y selección inválida).
- Verificación documental: 116 enlaces internos resueltos en los documentos modificados, 0 ausentes;
  la tabla contiene 109 filas únicas, exactamente los 107 tracked de la base más los dos documentos
  nuevos. El árbol final declara 109 Markdown tracked.

## 8. Riesgos y límites

- El backup c0080d96 no cubre los `29.360.128 B` posteriores; no se presenta como cobertura total.
- El backlog 13F y FRED unavailable son estados observados, no cero ni cobertura completa.
- El remoto cifrado puede depender de un token OAuth interactivo; su ausencia se tratará como
  `BUILD BLOCKED` de la recuperación, no como dato perdido.
- Los conteos v1 y v2 no son comparables por universo, UUID y reglas de exclusión; no se declara
  porcentaje migrado ni ahorro de almacenamiento.
- `C:` está al 95% de uso; una importación por lotes exige presupuesto medido y backup/restore por
  corte. No se compacta el VHD en 46.
- Los worktrees y checkpoints existentes se preservan; cualquier retirada usa el mecanismo Git
  soportado y ocurre en 49 con autorización humana.
- Apple permanece como compatibilidad explícita; la neutralidad no elimina adaptadores ni fuentes.
