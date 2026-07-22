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
- deuda y liquidez ampliada: commercial paper, deuda a largo plazo corriente y no corriente,
  valores negociables y pasivos por arrendamientos financieros y operativos;
- retornos y eficiencia: resultado antes de impuestos, gasto fiscal, gasto por intereses y PP&E
  neto, manteniendo separados los saldos de cierre y los hechos de duración;
- datos por acción: EPS diluido declarado, acciones diluidas promedio y acciones en circulación al
  cierre.

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

## Política de cobertura

Cada ampliación audita primero todos los conceptos presentes en el snapshot oficial y prioriza los
que aportan información analítica, tienen unidad y semántica temporal verificables y conservan
cobertura histórica suficiente. Un concepto no se normaliza solo por existir: Company Facts también
contiene notas, supuestos, conceptos discontinuados y datos cuya agregación sería incorrecta.

El catálogo tipado registra cada concepto aceptado, su tag, unidad y tipo de período. Los conceptos
ausentes, unidades inesperadas, acumulados trimestrales y contextos incompatibles quedan contados en
la cobertura en lugar de convertirse silenciosamente en ceros. Así se busca extraer toda la
información útil posible sin sacrificar comparabilidad, trazabilidad ni coherencia contable.

## Métricas derivadas disponibles

El motor read-only calcula actualmente 40 métricas descriptivas:

- márgenes bruto, operativo, neto, de flujo operativo y de flujo de caja libre;
- conversión de resultado neto a flujo operativo y a flujo de caja libre;
- flujo de caja libre contable y capex sobre flujo operativo;
- current ratio, cash ratio, capital de trabajo y activos líquidos netos de deuda a largo plazo;
- deuda financiera corriente y total, deuda neta, pasivos por arrendamientos y obligaciones
  financieras totales; vencimiento corriente, deuda sobre activos, patrimonio y FCF, deuda neta
  sobre FCF y cobertura de intereses;
- tasa fiscal efectiva, rotación de activos y activos fijos, rentabilidad sobre activos y patrimonio
  al cierre y ROIC aproximado sobre capital al cierre;
- R&D, SG&A y stock-based compensation sobre ingresos;
- dividendos más recompras y esas distribuciones sobre flujo de caja libre;
- EPS diluido, ingresos y flujo de caja libre por acción diluida, acciones diluidas promedio y
  acciones en circulación.

Cada definición publica `formula`, unidad, campos requeridos, limitaciones y `algorithm_version`.
Cada resultado incluye el valor exacto `Decimal`, el periodo, `available_at` y el UUID de cada
observación SEC usada. Un input ausente se registra en la cobertura; un denominador no positivo no
produce infinito ni una interpretación implícita. Revisiones con igual disponibilidad y semántica
contradictoria detienen la consulta.

Sobre ese resultado se construye una vista histórica independiente que conserva todos los puntos y
calcula media, rango, variación frente al período disponible anterior, variación del horizonte y
CAGR. El CAGR se limita a series anuales de nivel —USD, USD por acción o acciones— con extremos
positivos, usa días transcurridos y 365.2425 días por año. No se anualizan flujos trimestrales ni se
denomina "estabilidad" a un score: la dispersión se muestra como rango exacto.

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

## Análisis fundamental unificado

Las métricas inspiradas por distintas metodologías de análisis se presentan por utilidad, no por el
nombre de un inversor. El resultado histórico exacto se divide en ocho secciones no solapadas:

- crecimiento y datos por acción;
- rentabilidad;
- retornos y eficiencia;
- calidad del beneficio;
- liquidez y balance;
- deuda y solvencia;
- caja y reinversión;
- asignación de capital.

Cada una de las 40 métricas aparece una sola vez. La cobertura cuenta evidencia existente en el
último período, series históricas y comparaciones posibles; nunca se interpreta como puntuación,
calidad o recomendación. Las influencias de Graham, Buffett y Lynch permanecen como referencias de
investigación para ampliar datos y reglas, pero no crean paneles duplicados ni métodos rivales en la
interfaz.

La clasificación empresarial muestra desde el inicio las categorías crecimiento lento, empresa
estable, crecimiento rápido, cíclica, recuperación y activo oculto. Aunque ya existen series por
acción, todavía faltan reglas de crecimiento comparable, sensibilidad cíclica, evidencia de
reestructuración y valoración de activos; por ello, el contrato devuelve explícitamente
`insufficient_evidence` y no inventa una categoría.

El endpoint local `/api/fundamental-analysis` devuelve `aapl-fundamental-analysis-v1` e incorpora sin
alteraciones el historial `aapl-fundamental-research-history-v2`, por lo que cada métrica conserva
fórmula, versión, valor `Decimal` e identidades de observación.

## Desarrollo analítico posterior

El enfoque macro y de regímenes pertenece a un dominio separado de los fundamentales SEC de una
empresa. Sus relaciones entre crecimiento, inflación, liquidez, tipos y clases de activo tendrán fuentes,
frecuencias y vintages separados. Tampoco se mezclarán con el dominio
[`Cazatiburones`](cazatiburones.md), que exige filings de propiedad o evidencia on-chain propia.

No se generará una puntuación conjunta de "gurús", recomendación o valor intrínseco falsamente
preciso. La siguiente ampliación fundamental requiere crecimiento comparable, retornos sobre saldos
promedio, valoración, clasificación empresarial y sus ajustes sectoriales explícitos.

El mantenimiento económico de capex no es un hecho SEC directamente observable. Por ello, el FCF
contable y cualquier aproximación a owner earnings deberán permanecer claramente diferenciados.
