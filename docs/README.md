# Índice documental

Este índice es la entrada única a la documentación tracked del repositorio. La disposición completa
de los 110 Markdown tracked (107 de la base más los tres documentos nuevos) y de los 3 Markdown
protegidos no tracked vive en el [registro de continuidad](data_chassis_continuity_plan.md); aquí
sólo se enlaza la documentación vigente y el archivo histórico.

## Operación actual

- [Readiness operativa v2](v2_operational_readiness.md): matriz V1–V6, caminos críticos, retención
  verificable y estado de entrega para retomar `FUNDAMENTALS-COVERAGE`, que sigue siendo el único
  `NEXT`.
- [Eficiencia de lectura](read_path_efficiency.md): firma `read-dependency-signature-v1`, caché
  acotada, proyección v2, telemetría `critical-read-performance-v1` y reapertura de vista.
- [Continuidad](data_chassis_continuity_plan.md): registro histórico de 46–48; `DATA-CHASSIS` está
  `DONE` y la readiness v2 documenta la retención y limpieza vigentes.
- [Runbook de recuperación histórica](historical_recovery_runbook.md): evidencia histórica de
  selección sellada, importación bajo writer único, backup sucesor reutilizable y restore remoto.
- [Retención y lista concreta 2026-10-23](planning_context/storage_retirement_2026-10-23.md):
  referencia histórica de cobertura retenida, último consumidor y propuesta de retiro.

- [Interfaz local y operación continua](local_interface.md)
- [Readiness operacional y recuperación](operational_readiness.md)
- [Aceptación operacional exact-SHA](release_acceptance.md)
- [Workspace local](workspace.md) y [workspaces persistentes](workspaces.md)
- [Almacenamiento local](storage.md)
- [Continuidad, retención y neutralidad de activos](data_chassis_continuity_plan.md)
- [Reincorporación selectiva de v1](data_chassis_v1_reintegration_plan.md)

## Arquitectura y contratos

- [Arquitectura](architecture.md)
- [Fachada de aplicación](application_facade.md) y [runtime](application_runtime.md)
- [Contratos de datos](data_contracts.md)
- [Catálogo de activos](asset_catalog.md), [preferencias](asset_preferences.md) y
  [cobertura del universo](universe_coverage.md)
- [Runtime por capacidades](capability_driven_runtime.md)
- [Eficiencia de lectura](read_path_efficiency.md) y
  [presupuesto de memoria del scheduler](scheduler_memory_budget.md)
- [Investigación histórica](historical_research_data.md) y
  [estrategia de información](research_information_strategy.md)

## Mercado, fundamentales y eventos

- [Historia Alpaca](alpaca_history.md), [Coinbase diario](coinbase_history.md),
  [Coinbase intradía](coinbase_intraday.md) e [historia unificada](unified_market_history.md)
- [Estadísticas de mercado](market_statistics.md), [diagnóstico de mercado](market_diagnostic.md) y
  [comparación de mercado](market_comparison.md)
- [Derivados cripto Deribit](crypto_derivatives.md)
- [Fundamentos SEC](sec_edgar_foundation.md), [observaciones](sec_fundamental_observations.md),
  [métricas](sec_fundamental_metrics.md), [diagnóstico](sec_fundamental_diagnostic.md) y
  [point-in-time](sec_fundamental_point_in_time.md)
- [Corpus documental SEC](sec_document_corpus.md), [línea temporal](sec_document_timeline.md) y
  [propiedad declarada](sec_ownership.md)
- [Posiciones institucionales 13F](sec_institutional_holdings.md) y
  [semántica institucional](sec_institutional_semantics.md)
- [Cazatiburones](cazatiburones.md), [screening y alertas](automated_screening_alerts.md) y
  [valoración corporativa PIT](corporate_valuation_point_in_time.md)
- [FRED/ALFRED point-in-time](fred_alfred_point_in_time.md) y
  [registro SMV/BVL](smv_bvl_registry.md)

## Desarrollo y gobernanza

- [Desarrollo](development.md), [dependencias](dependency_management.md) y
  [integración continua](continuous_integration.md)
- [Protocolo de desarrollo](development_protocol.md) y [guía del repositorio](../AGENTS.md)
- [Diseño de la interfaz local](local_interface_design_system.md) y
  [referencias de interfaz profesional](professional_analysis_interface.md)

## Archivo y contexto histórico

- [Plan de cierre anterior](data_chassis_closure_plan.md) y
  [arranque v2](data_chassis_v2_fresh_start_plan.md)
- [Runbook de migración 43](data_chassis_43_migration_runbook.md),
  [backup/cutover 44](data_chassis_44_backup_cutover_runbook.md) y
  [aceptación 45](data_chassis_45_acceptance_runbook.md)
- [Archivo del runbook de migración retirado](archive/data_chassis_45_migration_runbook_20261021.md)
- [Incidente de spill WSL](incidents/data_chassis_45_wsl_spill_20261016.md)
- [Decisiones de revisión técnica externa](planning_context/external_technical_review_decisions_2026-10-21.md)
  y [respuesta de revisión](planning_context/claude_review_response.md)
- [Investigación cripto](research/crypto/crypto_research_synthesis_2026-08.md)

## Regla de mantenimiento

Un documento nuevo entra por este índice y por el registro de continuidad en el mismo cambio. Un
documento histórico se conserva como referencia; no se reescribe para aparentar un estado que no
ocurrió. La retirada física de documentos sólo ocurre en el tramo de limpieza autorizado y después
de que el índice y sus referencias entrantes hayan sido reconciliados.
