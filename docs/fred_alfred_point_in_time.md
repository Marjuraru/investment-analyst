# Conector point-in-time FRED/ALFRED

## Alcance del primer corte

La herramienta puede importar una instantánea histórica de una serie oficial de FRED para una fecha
de vintage explícita y reconstruir, sin red, el último valor que era conocible en un `known_at`.
ALFRED aporta la semántica de tiempo real y revisiones a través del parámetro `vintage_dates` del
endpoint oficial de observaciones.

Este corte establece primero los contratos de seguridad y reproducibilidad:

- una serie y un vintage por ejecución;
- valores sin transformación del proveedor (`units=lin`, `output_type=1`);
- `Decimal` exacto desde el texto de FRED y `None` únicamente para el marcador oficial `.`;
- checksum SHA-256 del cuerpo recibido e identidad UUID determinista;
- RawRecords append-only, idempotentes y con la respuesta JSON completa;
- `source_id` independiente por serie;
- consulta local que selecciona la última revisión disponible y falla si dos respuestas del mismo
  vintage discrepan;
- API key solo en la URL enviada por HTTPS, nunca en la URL persistida, el JSON de salida o los
  errores controlados.

Los registros macro conservan `asset_id=None`. No son precios de AAPL, BTC ni otro instrumento y no
crean observaciones, métricas o diagnósticos de activos. El esquema del workspace no cambia.

## Disponibilidad point-in-time

ALFRED expresa el vintage con precisión de fecha, pero no proporciona en este endpoint una hora
histórica de publicación. Para evitar look-ahead intradía:

- un vintage histórico se considera disponible a las `00:00 UTC` del día siguiente;
- si se importa un vintage del día actual, se usa la hora real de recepción;
- una consulta solo utiliza registros cuyo `available_at <= known_at`.

Esta regla es conservadora y se declara como `availability_precision=day-conservative`. Una futura
fuente con timestamp oficial de publicación podrá tener un contrato propio sin reinterpretar estos
registros.

## Obtener una API key gratuita

Todas las solicitudes del API de FRED requieren una API key personal de 32 caracteres. Se obtiene
desde la [documentación oficial de API keys](https://fred.stlouisfed.org/docs/api/api_key.html).
No se debe escribir en el repositorio ni incluir en comandos compartidos.

En la terminal WSL de VS Code:

```bash
cd ~/projects/investment-analyst
source .venv/bin/activate
read -rsp "FRED API key: " FRED_API_KEY
echo
export FRED_API_KEY
```

El valor permanece solo en esa terminal. Al cerrarla desaparece.

## Importar dos vintages

El workspace debe estar inicializado. Este ejemplo importa dos vistas históricas de `GDP`; el rango
de observaciones es inclusivo:

```bash
.venv/bin/python scripts/fetch_fred_vintage.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --series-id GDP \
  --vintage-date 2020-01-15 \
  --start 1950-01-01 \
  --end 2020-01-01

.venv/bin/python scripts/fetch_fred_vintage.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --series-id GDP \
  --vintage-date 2020-02-15 \
  --start 1950-01-01 \
  --end 2020-01-01
```

Cada salida debe indicar `traceability_verified: true`. Repetir exactamente una respuesta conserva
el registro original y reporta `raw_records_reused: 1`.

## Consultar lo que se conocía

```bash
.venv/bin/python scripts/query_fred_point_in_time.py \
  --workspace ~/.local/share/investment-analyst/workspaces/default \
  --series-id GDP \
  --known-at 2020-02-16T00:00:00Z \
  --start 1950-01-01 \
  --end 2020-01-01
```

La consulta es completamente local. Cada valor devuelve vintage, rango de tiempo real,
`available_at` y UUID de los RawRecords que lo respaldan.

## Límites explícitos y siguiente etapa

Todavía no se incluyen:

- descubrimiento automático de todas las fechas de revisión mediante `series/vintagedates`;
- catálogo curado de inflación, tipos, empleo, crédito y liquidez con unidades y frecuencia;
- metadatos de serie, reanudación por lotes o recibos de cobertura;
- normalización macro columnar, Parquet, relaciones con activos, UI o diagnósticos;
- ejecución automática en el scheduler;
- interpretación causal, predicción o recomendación.

La siguiente etapa debe añadir primero el catálogo versionado y la enumeración paginada de vintages,
con límites de volumen y pruebas de reanudación. Después podrá diseñarse almacenamiento macro
normalizado sin forzarlo dentro del contrato de observaciones de activos.
