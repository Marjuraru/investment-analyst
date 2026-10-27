# Reparación de procedencia SEC de un workspace v2

Este runbook es la autoridad única de operación para corregir `source.retrieved_at` de un raw SEC ya
persistido en un workspace v2 cuando el workspace v1 retenido demuestra el instante original. No
introduce una adquisición, un esquema, una fórmula ni una interpretación point-in-time nueva.

## Alcance admitido

La herramienta sólo puede reescribir `source.retrieved_at` de un raw de `sec-edgar:<ticker>:` ya
existente. `available_at`, `received_at`, `event_time`, `payload`, `source.checksum_sha256`,
`source.record_key`, `record_id`, `asset_id` y `schema_version` permanecen intactos. El checksum
canónico del blob cambia sólo como consecuencia del campo corregido y se actualiza mediante
`RawV2TemporalRepair`.

Queda fuera de alcance: reescribir observaciones, importar de nuevo historia, borrar raws, tocar
métricas o diagnósticos, cambiar fórmulas, versiones de algoritmo, catálogo, endpoints o feeds.

## Precondiciones

1. Workspace v1 retenido y legible; se abre siempre `READ_ONLY`.
2. Workspace v2 destino con formato `2`.
3. Ventana sin writer para `apply`: el servicio debe estar detenido o sin lock. `plan` y `verify`
   funcionan en `READ_ONLY` y pueden ejecutarse con el servicio activo.
4. SHA completo del candidato que ejecuta la operación (`--executor-sha`). Queda sellado en el plan.
5. Rutas de plan y recibo nuevas; la herramienta nunca sobrescribe evidencia previa.

## Comandos

```bash
# 1. Inventario y plan sellado (read-only)
PYTHONPATH=$PWD/src .venv/bin/python scripts/repair_sec_provenance.py plan \
  --workspace <workspace-v2> --source-workspace <workspace-v1> \
  --executor-sha <full-sha> --output <ops>/sec-provenance-repair-plan.json

# 2. Aplicación exacta (ventana sin writer)
PYTHONPATH=$PWD/src .venv/bin/python scripts/repair_sec_provenance.py apply \
  --workspace <workspace-v2> --source-workspace <workspace-v1> \
  --executor-sha <full-sha> --plan <plan.json> --receipt <ops>/receipt.json

# 3. Verificación estructural (read-only, repetible)
PYTHONPATH=$PWD/src .venv/bin/python scripts/repair_sec_provenance.py verify \
  --workspace <workspace-v2> --source-workspace <workspace-v1> \
  --executor-sha <full-sha> --plan <plan.json>
```

`plan` imprime el digest, el número de referencias, entradas y conflictos, y los IDs de ambos
conjuntos. `apply` exige que el estado vivo sea exactamente el `before` sellado o ya el `after`
sellado; un tercer estado rechaza sin escribir. `verify` sólo devuelve `0` si cada raw quedó en su
estado `after` y todas sus observaciones dependientes se reconstruyen exactamente.

Sin `--asset`, el inventario cubre los emisores SEC declarados por el catálogo (20 en la base
vigente). El plan siempre recorre Company Facts y Submissions de cada emisor y no hidrata blobs de
otras fuentes: no recorre 13F, documentos completos ni respaldos.

## Criterio de admisión por raw

Se admite una entrada sólo si se cumplen todas las condiciones:

1. el raw existe en v2 con el mismo `record_id` y en v1 con idéntico emisor, `source_id`,
   `schema_version`, `source.record_key`, checksum de payload y payload completo;
2. el estampado temporal difiere y el único campo que explica la diferencia es
   `source.retrieved_at`;
3. sustituir ese campo por el valor de v1 reconstruye **exactamente todas** las observaciones
   dependientes del raw, usando el normalizador vigente y el par Company Facts/Submissions que cada
   observación referencia;
4. ningún otro consumidor cambia de estado (`available_at`, valores, unidades y versiones intactos).

Cuando 3 falla se clasifica conflicto y el raw no se muta. La aplicación es por raw: un conflicto no
bloquea la corrección de otro raw admitido, y no existe rollback global.

## Clasificación de conflictos

| Razón | Significado |
| --- | --- |
| `evidence-missing` | v1 no conserva un raw con esa identidad |
| `payload-mismatch` | la identidad existe pero el payload no es idéntico |
| `no-dependents` | ninguna observación depende del raw: no hay defecto demostrable que corregir |
| `dependent-clocks-incompatible` | parte de las observaciones se reconstruye y parte no: dos linajes de reloj sobre un mismo raw |
| `reconstruction-diverges` | ninguna observación dependiente se reconstruye con el valor original |

Un raw con conflicto impide afirmar reparación completa del workspace. El conflicto se presenta con
IDs, conteos e impacto; nunca se resuelve inventando una representación de adquisiciones durante
BUILD.

## Idempotencia, reanudación y evidencia

- El plan sella `plan_digest` (SHA-256 canónico) que liga workspace destino, workspace origen,
  emisores, UUIDs ordenados, checksums before/after, estampados before/after, evento, digest de
  dependientes y prueba de reconstrucción.
- `apply` verifica el plan completo antes de escribir y escribe el recibo una sola vez con el
  resultado por raw (`changed`). Reejecutar devuelve `changed=0` sin tocar blobs.
- Un recibo existente que ligue otro plan o ejecutor se rechaza.
- Los blobs anteriores permanecen en el workspace como evidencia; no se ejecuta GC.
- La firma de dependencias de lectura incluye `source_retrieved_at`, por lo que la reparación
  invalida cachés por el cambio pertinente y no por ignorar firmas.

## Resultado vivo del 2026-10-27

El inventario read-only sobre `active-v2-20261021` (workspace `70c64668-…`) y el v1 retenido
(`19cf83b5-…`) cubrió 330 raws SEC comparables: 323 idénticos y 3 con estampado distinto.

| Raw | Emisor | Dependientes | Resultado |
| --- | --- | --- | --- |
| `ddf69926-344d-577c-aea7-c75018dc11f0` | AAPL Company Facts | 1355 | `dependent-clocks-incompatible` |
| `caa19cbd-ef8c-590b-a2f0-f7c9e4121c07` | AMD Company Facts | 871 | `dependent-clocks-incompatible` |
| `5caa7db7-2790-580b-bb6a-55150c68023b` | AMD Submissions | 0 | `no-dependents` |

En AAPL, 1286 observaciones exigen `2026-10-21T20:49:52.509067Z` y 69 exigen
`2026-08-01T04:06:18.471994Z`; 43 de esas 69 son candidatas del refresh y fallan hoy. En AMD las 871
dependientes exigen `2026-10-22T01:55:28.905318Z`. Sustituir el campo por el valor de v1 repararía
las 69 de AAPL y rompería las 1286 restantes; en AMD rompería las 871. Por eso el plan sellado no
contiene entradas admitidas y no se mutó ningún raw.

Decisión requerida de PLAN: definir qué linaje de procedencia es autoritativo cuando un mismo raw
canónico fue adquirido dos veces con el mismo payload y relojes distintos. Las opciones conocidas
—reescribir observaciones, reimportar historia o aceptar la divergencia— están prohibidas por el
Work Block vigente; no se ejecutó ninguna.

## Limitaciones

- IEX no interviene aquí: es un procedimiento de procedencia SEC, no de cobertura de mercado.
- La reparación no crea observaciones, no recalcula métricas ni diagnósticos y no modifica
  valoración.
- Un raw con `no-dependents` no se corrige: su `retrieved_at` vigente es evidencia legítima de la
  adquisición que lo creó.
- Este procedimiento no sustituye un backup del workspace ni cubre ingestas posteriores; el paquete
  de recuperación, si se autoriza, se declara aparte con su propio presupuesto y readback.
