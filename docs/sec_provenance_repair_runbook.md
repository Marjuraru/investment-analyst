# Procedencia de adquisición SEC en un workspace v2

Este runbook es la autoridad única para conservar dos adquisiciones válidas de un mismo raw SEC y
para recuperar el refresh sin reescribir raws ni observaciones. No introduce adquisiciones, esquemas
SQL, fórmulas, versiones del normalizador ni interpretaciones point-in-time nuevas.

## Frontera del contrato

`sec-acquisition-provenance-v1` es un artefacto JSON tipado, inmutable y append-only, guardado en
`storage/v2/sec-provenance/` del mismo workspace:

| Ruta | Contenido |
| --- | --- |
| `acquisitions/<id[:2]>/<id[2:4]>/<id>.json` | metadata completa de la adquisición original y los bindings de sus observaciones |
| `bindings/<obs[:2]>/<obs[2:4]>/<obs>.json` | referencia mínima `observation_id → identity_digest` |

La identidad (`identity_digest`) es el SHA-256 canónico de `schema`, UUID de destino, `raw_record_id`,
hash canónico del raw original y bindings ordenados. El sobre guarda además `content_digest_sha256`
del artefacto completo. La fecha de creación y el SHA del ejecutor viven en el recibo, nunca en el
artefacto.

El artefacto no duplica el payload financiero: conserva su checksum, su digest canónico y la
`SourceReference` completa. La reconstrucción del raw original se hace en memoria con el payload ya
persistido en v2, y su hash canónico debe coincidir con `original_raw_sha256`.

Lo prohibido sigue prohibido: no se usa `RawRecord` como contenedor de metadata interna fingiendo
una adquisición, no se modifican `RawRecord` ni `NormalizedObservation`, no se reescribe el reloj
compartido de un raw actual y no se toca evidencia histórica para que encaje.

## Estados válidos de un raw

| Estado | Significado |
| --- | --- |
| `CURRENT_VALID` | todas sus observaciones se reconstruyen con la adquisición actual |
| `HISTORICAL_PROVENANCE_REQUIRED` | algunas sólo se reconstruyen con una adquisición preservada, y la evidencia prueba cada binding |
| `UNREFERENCED_OR_INDIRECT` | no tiene observaciones dependientes directas; se cuentan sus referencias indirectas antes de cerrarlo |
| `UNRESOLVED` | falta prueba, el payload difiere o una observación no se reconstruye: se presenta con diferencias por campo |

Una ausencia en v1 no es por sí misma un conflicto: un raw nuevo de v2 sin dependientes históricos es
`UNREFERENCED_OR_INDIRECT`. Sólo se exige v1 cuando hay que probar un grupo histórico.

## Comandos

```bash
# 1. Clasificación read-only del universo SEC declarado (v1 + v2)
PYTHONPATH=$PWD/src .venv/bin/python scripts/repair_sec_provenance.py classify \
  --workspace <workspace-v2> --source-workspace <workspace-v1> \
  --executor-sha <full-sha>

# 2. Plan de evidencia sellado (read-only; escribe sólo el JSON del plan)
PYTHONPATH=$PWD/src .venv/bin/python scripts/repair_sec_provenance.py evidence-plan \
  --workspace <workspace-v2> --source-workspace <workspace-v1> \
  --executor-sha <full-sha> --base-sha <declared-base-sha> --output <ops>/evidence-plan.json

# 3. Publicación de evidencia (no abre v1; no escribe datos financieros)
PYTHONPATH=$PWD/src .venv/bin/python scripts/repair_sec_provenance.py evidence-apply \
  --workspace <workspace-v2> --executor-sha <full-sha> \
  --plan <ops>/evidence-plan.json --receipt <ops>/evidence-receipt.json

# 4. Verificación desde el workspace destino únicamente
PYTHONPATH=$PWD/src .venv/bin/python scripts/repair_sec_provenance.py evidence-verify \
  --workspace <workspace-v2> --executor-sha <full-sha> --plan <ops>/evidence-plan.json
```

`evidence-apply` abre la base en modo read-only y sólo publica archivos bajo `storage/v2/sec-provenance/`
más el recibo externo. `evidence-verify` y el runtime normal no aceptan ni abren la ruta v1.

Los modos antiguos `plan`, `apply` y `verify` de `sec-provenance-repair-plan-v1` siguen existiendo
como diagnóstico de la corrección temporal estrecha. **No son una alternativa aplicable a este
defecto**: su plan sellado para el workspace activo no admite entradas.

## Admisión de un grupo histórico

Se admite un artefacto sólo si se cumplen todas las condiciones:

1. el raw actual y el original comparten `record_id`, emisor, `source_id`, `schema_version`,
   `source.record_key`, checksum de payload y payload completo;
2. cada observación ligada existe idéntica en v1 (mismo `observation_id` y mismo hash canónico);
3. la adquisición original se reproduce en memoria y su hash coincide con `original_raw_sha256`;
4. el normalizador vigente reconstruye **exactamente** cada observación ligada con su
   `submissions_record_id`, su reloj de normalización y su versión de transformación.

Un fallo se clasifica con diferencias por campo (`observation:<id>:content-differs-from-v1`,
`raw:payload`, `submissions:<id>:absent`, …), no con un contador agregado.

## Lectura en runtime

1. Se reconstruye primero con la adquisición actual.
2. Sólo ante una diferencia de procedencia se consulta el binding de esa observación concreta.
3. Se validan secuencialmente: workspace, raw, emisor, source, schema, checksum y digest del payload
   persistido, hash de la observación, submissions, reloj, versión de transformación y hash del raw
   original.
4. Se reconstruye con el normalizador sin cambiar la comparación completa. Si algo no coincide, se
   rechaza; no hay caída a v1 ni reparación automática.

La caché de reconstrucción se indexa por identidad de evidencia, submissions y reloj de
normalización, y está acotada por número de grupos. Una evidencia borrada, truncada o corrupta falla
cerrado; nunca se aceptan hashes recalculados a partir de un archivo alterado como prueba.

## Idempotencia, reanudación y rollback

- Publicar dos veces el mismo artefacto no crea archivos ni bytes; un artefacto con el mismo id y
  bytes distintos se rechaza.
- Una interrupción después del artefacto y antes del recibo se reanuda sin duplicar archivos.
- Un recibo existente que ligue otro plan o ejecutor se rechaza.
- Un rollback de código no borra evidencia: el runtime anterior vuelve a la limitación para ese
  grupo histórico, nunca convierte la validación en sana.
- El backup por inventario incluye `storage/v2/sec-provenance/**` automáticamente;
  `validate_sec_provenance_evidence(workspace_root)` detecta artefactos corruptos, bindings huérfanos
  y bindings incompletos antes de aceptar un restore.

## Evidencia ejecutada — 2026-10-27

Workspace `70c64668-1738-4dfe-ba1f-7e72026bb7fe`; v1 retenido `19cf83b5-8df3-4663-9369-4b61f86eceea`;
base `601d12a6f2242b87032d34fcb4e45652e4be34f6`; el SHA exacto del candidato que publicó la
evidencia queda en el marcador BUILD del PR.

| Medición | Resultado |
| --- | --- |
| Clasificación read-only | 329 referencias: 29 `CURRENT_VALID`, 1 `HISTORICAL_PROVENANCE_REQUIRED`, 299 `UNREFERENCED_OR_INDIRECT`, 0 `UNRESOLVED`; 25.733 observaciones actuales y 69 históricas |
| Plan sellado | `40db8f4d11a226cd76b2a59ae3fb7da36af3422e178dcc4d02810fcf2d43814a`; 1 artefacto, 69 bindings, 48.076 B, 0 conflictos |
| Apply | 70 archivos, 48.076 B; verificación 69/69 reconstruidas, 0 divergentes |
| Replay | 0 archivos, 0 B |
| Refresh AAPL | 2 documentos, 5.669 hechos examinados, 1.286 seleccionados, 0 observaciones y 0 raws creados, 155 métricas reutilizadas, diagnóstico `positive` con cobertura 1,00 |
| Replay AAPL con otro reloj | `known_at=2026-10-27T22:30:00Z`, 0 creados, 155 métricas reutilizadas |
| AMD (control sin evidencia) | 871 observaciones reutilizadas, 78 métricas reutilizadas, 0 creados |
| Datos financieros preexistentes | observaciones 203.351, diagnósticos 1.911, raws 869.736 y métricas 42.149 con digest idéntico antes/después |
| Runtime sin v1 | 1.188 muestras de descriptores durante el refresh real: 0 accesos a `workspaces/default` |

El refresh creó 11 métricas nuevas de valoración AAPL en el corte `2026-10-26`, contabilizadas aparte.

## Limitaciones

- IEX sigue siendo cobertura de una bolsa; esta evidencia es de procedencia SEC, no de cobertura.
- La evidencia acredita una adquisición ya persistida; no adelanta `available_at` ni `known_at`
  financieros.
- No se borra v1 en este procedimiento: su retirada requiere inventario y readback propios.
- Un grupo sin evidencia y sin reconstrucción actual sigue fallando cerrado; no se rellena con cero.
