# Base de investigación fundamental

Esta base amplía los datos normalizados de Apple sin cambiar la consulta point-in-time pública de
cinco hechos, sus identidades deterministas ni las fórmulas y diagnósticos existentes.

## Catálogos separados

El catálogo `core` conserva exactamente ingresos, resultado neto, activos, pasivos y patrimonio. El
catálogo adicional de investigación normaliza conceptos oficiales `us-gaap` para:

- rentabilidad operativa: beneficio bruto y resultado operativo;
- generación y uso de caja: flujo de caja operativo, capex, dividendos y recompras;
- reinversión: R&D, SG&A y stock-based compensation;
- liquidez y capital de trabajo: efectivo, activos y pasivos corrientes, inventario, cuentas por
  cobrar y cuentas por pagar;
- deuda y liquidez ampliada: deuda a corto y largo plazo y valores negociables corrientes y no
  corrientes.

Los pagos de capex, dividendos y recompras conservan el importe positivo publicado por SEC. La
normalización no invierte su signo ni interpreta todavía esos importes como flujo de caja libre o
retorno al accionista.

## Semántica histórica

Cada observación conserva filing, accession number, aceptación, periodo, fuente, tag y RawRecord.
Los hechos de duración trimestral solo se aceptan cuando representan un trimestre discreto. Los
acumulados de seis o nueve meses no se convierten implícitamente en trimestres; esa derivación deberá
tener fórmula, inputs y versión propios.

Company Facts facilita comparabilidad, pero el filing oficial sigue siendo la referencia ante una
ambigüedad. No se unen tags parecidos ni extensiones particulares sin una regla explícita y probada.

## Métricas derivadas disponibles

El motor read-only calcula actualmente 18 métricas descriptivas:

- márgenes bruto, operativo, neto, de flujo operativo y de flujo de caja libre;
- conversión de resultado neto a flujo operativo y a flujo de caja libre;
- flujo de caja libre contable y capex sobre flujo operativo;
- current ratio, cash ratio, capital de trabajo y activos líquidos netos de deuda a largo plazo;
- R&D, SG&A y stock-based compensation sobre ingresos;
- dividendos más recompras y esas distribuciones sobre flujo de caja libre.

Cada definición publica `formula`, unidad, campos requeridos, limitaciones y `algorithm_version`.
Cada resultado incluye el valor exacto `Decimal`, el periodo, `available_at` y el UUID de cada
observación SEC usada. Un input ausente se registra en la cobertura; un denominador no positivo no
produce infinito ni una interpretación implícita. Revisiones con igual disponibilidad y semántica
contradictoria detienen la consulta.

Sobre ese resultado se construye una lente histórica independiente que conserva todos los puntos y
calcula media, rango, variación frente al período disponible anterior, variación del horizonte y
CAGR. El CAGR se limita a series anuales en USD con extremos positivos, usa días transcurridos y
365.2425 días por año. No se anualizan flujos trimestrales ni se denomina "estabilidad" a un score:
la dispersión se muestra como rango exacto.

Para consultar los ejercicios anuales disponibles en el workspace permanente:

```bash
cd ~/projects/investment-analyst
.venv/bin/python scripts/query_aapl_fundamental_research.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --known-at 2026-07-19T23:59:59Z \
  --frequency annual \
  --limit 20
```

La consulta abre el workspace en solo lectura y emite JSON versionado. `--start` y `--end` aceptan
fechas inclusivas `YYYY-MM-DD`; `--frequency` acepta `annual` o `quarterly`.

## Desarrollo analítico posterior

Graham, Lynch y Buffett se presentarán como lentes empresariales independientes con fórmulas y
evidencia visibles. Graham requiere historial de beneficios, balance, dividendos, valoración y
margen de seguridad; Buffett añade owner earnings claramente diferenciado del FCF contable,
retornos sobre capital, consistencia operativa, deuda y asignación de capital; Lynch requiere
clasificar la empresa —crecimiento lento, estable, rápido, cíclica, recuperación o activo oculto—
antes de interpretar crecimiento, inventario, deuda, caja o múltiplos. La categoría será evidencia
explicable, no una etiqueta inferida de un único ratio.

Dalio pertenece a una lente macro y de regímenes, no a los fundamentales SEC de una empresa. Sus
relaciones entre crecimiento, inflación, liquidez, tipos y clases de activo tendrán fuentes,
frecuencias y vintages separados. Tampoco se mezclarán con el dominio
[`Cazatiburones`](cazatiburones.md), que exige filings de propiedad o evidencia on-chain propia.

No se generará una puntuación conjunta de "gurús", recomendación o valor intrínseco falsamente
preciso. Todavía faltan series de crecimiento, retornos sobre capital, datos por acción, valoración,
clasificación empresarial y sus ajustes sectoriales explícitos.

El mantenimiento económico de capex no es un hecho SEC directamente observable. Por ello, el FCF
contable y cualquier aproximación a owner earnings deberán permanecer claramente diferenciados.
