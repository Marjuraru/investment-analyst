# Investment Analyst

## Propósito y límites

Herramienta personal local-first para recolectar, procesar, relacionar, visualizar y explicar información útil para el análisis de inversiones, diseñada para apoyar el criterio de un analista humano. Ofrece diagnósticos independientes de mercado y fundamentales con evidencia auditable point-in-time (`available_at`), identidades deterministas y persistencia append-only en DuckDB y Parquet.

- **Qué es:** una herramienta descriptiva y auditable para el seguimiento multi-activo de empresas, ETF y criptoactivos configurados en su catálogo central ([docs/universe_coverage.md](docs/universe_coverage.md)).
- **Qué no es:** no ejecuta operaciones, no administra capital, no se conecta operativamente con brokers, no provee asesoramiento financiero personalizado ni combina mercado y fundamentales en una puntuación agregada opaca.
- **Limitaciones de cobertura:** los datos gratuitos de acciones y ETF provienen de Alpaca IEX (cobertura de una sola bolsa, no SIP consolidado); Bitcoin y Ethereum spot proceden de Coinbase Exchange (un mercado de contado); los derivados proceden de Deribit ([docs/crypto_derivatives.md](docs/crypto_derivatives.md)); los fundamentales provienen de SEC EDGAR oficial; y la información macro de FRED/ALFRED oficial.

## Finalidad por capas

La herramienta tiene por finalidad declarada entregar alertas accionables **no personalizadas** y notificación rápida. Esta meta se alcanza mediante una evolución progresiva por capas gobernada por puertas de validación obligatorias, sin saltos directos ni atajos arquitectónicos:

`evidencia PIT → análisis → detección de oportunidades → señales/predicción validada → recomendación explícita y trazable → decisión humana/política → broker y ejecución controlada futura → posible automatización acotada posterior`

La capa de señal validada es la meta declarada del producto, alcanzable únicamente tras validación empírica obligatoria (objetivo/label versionados, purged walk-forward temporal fuera de muestra, holdout cronológico intacto, calibración, explicación y SHAP local, shadow mode continuo y criterio de rollback).

Las capas posteriores de recomendación y ejecución permanecen fuera de la finalidad declarada del producto. Una recomendación futura requeriría política explícita y trazabilidad separada; una ejecución operativa requeriría contratos de broker independientes, controles de riesgo y autorización explícita. El producto apoya el criterio analítico humano y no sustituye sus decisiones. Véase [docs/product_roadmap.md](docs/product_roadmap.md) y [docs/basic_functional_release_plan.md](docs/basic_functional_release_plan.md).

## Entorno mínimo y arranque

- **Requisitos:** Windows con WSL 2 o Linux Ubuntu 24.04, Python 3.12, Git y `uv 0.11.29`.
- **Instalación y sincronización:**

```bash
uv sync --locked --extra dev
```

- **Ejecución y pruebas:** consulte [docs/development.md](docs/development.md) para el conjunto completo de comprobaciones y [docs/local_interface.md](docs/local_interface.md) para la operación continua de la interfaz web local.

## Flujos principales e índice documental

- [docs/product_roadmap.md](docs/product_roadmap.md): hoja de ruta estratégica, finalidad por capas y puertas sistemáticas de calidad.
- [docs/basic_functional_release_plan.md](docs/basic_functional_release_plan.md): plan de cierre funcional, ruta crítica y estado de capacidades.
- [docs/architecture.md](docs/architecture.md): arquitectura híbrida local-first, separación estricta por capas y restricciones permanentes.
- [docs/universe_coverage.md](docs/universe_coverage.md): catálogo central de 37 identidades multi-activo, resolución de fuentes y matriz de cobertura.
- [docs/local_interface.md](docs/local_interface.md): interfaz web local, gráficos interactivos, fichas fundamentales y servicio `systemd`.
- [docs/automated_screening_alerts.md](docs/automated_screening_alerts.md): motor de screening determinista, reglas trivaluadas y bandeja de candidatos.
- [docs/sec_institutional_semantics.md](docs/sec_institutional_semantics.md): correspondencia, observaciones 13F y métricas as-filed del corpus SEC.
- [docs/crypto_derivatives.md](docs/crypto_derivatives.md): derivados Deribit para BTC y ETH, funding, DVOL y snapshots prospectivos.
- [docs/corporate_valuation_point_in_time.md](docs/corporate_valuation_point_in_time.md): valoración corporativa point-in-time y múltiplos descriptivos.
- [docs/development.md](docs/development.md): configuración del entorno, sincronización con `uv`, pruebas focalizadas y estándares de calidad.

## Advertencia

Esta herramienta no ejecuta operaciones y su salida no es asesoramiento financiero personalizado.
