# DATA-CHASSIS-43: migración, recuperación y handoff

> Decisión PLAN 2026-10-21: [plan vigente de arranque v2 nuevo](data_chassis_v2_fresh_start_plan.md). Primero datos nuevos y aceptación; historia v1/F5 preservada e incorporación selectiva posterior. Esta decisión está planificada, no acredita activación. Las recetas de migración anteriores son históricas y no se ejecutan para el arranque nuevo.


Ejecutar desde el worktree aislado de #341 con
`PYTHONPATH=<worktree>/src` y el intérprete
`/home/marjuraru/projects/investment-analyst/.venv/bin/python`. El workspace
v1 de producción sigue siendo el formato activo. Este bloque no activa v2 ni
retira A, B, `target-v2`, `backup-v2` o `restore-v2`.

## Estado y evidencia reutilizable

El corte B es
`workspaces/data-chassis-43-rehearsal/source-copy-b-20261008`, con
`source_fingerprint` `574d4ad82a79f7981a789896f44348651716a8c0530a53046571daa2b8446334`.
Los recibos conservados son evidencia del corte, no evidencia de un ciclo v1
posterior:

| Operación | Recibo y resultado comprobado |
| --- | --- |
| Equivalencia | `ops/data-chassis-43/verify-20261012-02.json`: 928.043 raw, 520.180 observaciones, 1.947.523 métricas y 1.777 diagnósticos; digests de métricas y diagnósticos; checkpoints completos por familia. |
| Migración directa | `ops/data-chassis-43/migration-direct-20261012-15.json`: target v2 listo, páginas de máximo 256 y sin corpus histórico intermedio. |
| Recovery independiente | `ops/data-chassis-43/smoke-recovery-20261012-02.json`: backup de 676 volúmenes, restore en raíz disjunta, mismos conteos de raw, observaciones, métricas y diagnósticos, y mismo UUID de workspace target/restaurado. |
| Verificación posterior al cambio de memoria | servicio local `data-chassis-43-restored-verify.service`, 2026-10-13: `verify_complete()` terminó con 1.947.523 métricas y 1.777 diagnósticos; pico de memoria 2,0 GB y 0 B de swap. |

Antes de reutilizar un recibo, comprobar su binding: fingerprint de B, UUID y
estado del destino, manifest de backup, hashes de volúmenes y SHA/código del
verificador cuando aplique. Un cambio del lector o verificador invalida sólo la
cobertura semántica dependiente; no exige recapturar B ni recopiarlos.

## Preflight de capacidad

La reserva mínima es 5.000.000.000 bytes **más** los bytes de la siguiente
unidad. La medición debe venir del API de C: en la misma operación; un valor
viejo, `UNKNOWN` o sólo la reserva no autoriza nuevas escrituras.

```bash
PYTHONPATH="$PWD/src" /home/marjuraru/projects/investment-analyst/.venv/bin/python \
  scripts/migrate_workspace_v2.py budget \
  --destination <owned-target-v2> \
  --source-snapshot <owned-source-copy-B> \
  --report <owned-ops-root>/capacity-budget.json
```

El presupuesto separa bytes asignados existentes, nuevo crecimiento, auxiliares,
evidencia y reserva. Descuenta el destino existente y fija el intermedio
histórico en cero para transporte directo. Las cantidades aún no medidas se
registran como `UNKNOWN`, no como cero ni como una estimación. El último
presupuesto previo al rehearsal fue
`ops/data-chassis-43/capacity-budget-20261012-06.json`; su medición no
sustituye el preflight de una escritura futura.

## Reanudar sin perder progreso

`status` lee exclusivamente checkpoints y supervisor; no acredita equivalencia.
`verify` revalida origen y destino mediante las APIs de migración. Ambos deben
usar rutas owned, absolutas, regulares y disjuntas.

```bash
PYTHONPATH="$PWD/src" /home/marjuraru/projects/investment-analyst/.venv/bin/python \
  scripts/migrate_workspace_v2.py status \
  --destination <owned-target-v2> --report <owned-ops-root>/status.json

PYTHONPATH="$PWD/src" /home/marjuraru/projects/investment-analyst/.venv/bin/python \
  scripts/migrate_workspace_v2.py verify \
  --source <owned-source-copy-B> --destination <owned-target-v2> \
  --inventory <owned-inventory.jsonl> --report <owned-ops-root>/verify.json
```

Cada página, fragmento o volumen avanza después de escritura, `fsync`,
verificación necesaria y reemplazo atómico del checkpoint/journal. El cursor
nunca se adelanta. Ante interrupción, conservar B, destino parcial y los
checkpoints; reanudar la unidad pendiente. Ante capacidad insuficiente o medida
inválida, pausar únicamente la fase de escritura, conservar el progreso y
continuar pruebas read-only o de scratch que no escriban esos destinos.

## Backup, restore y rollback

El backup packed procesa inventario, journal, cierre, verificación y restore en
streaming. No materializa todo el inventario o journal en modelos Python. Un
resume comprueba nuevamente manifest, bindings, todos los volúmenes ya
confirmados y el checkpoint antes de reutilizar bytes restaurados. Los 676
volúmenes existentes se reutilizan si pasan esa verificación; un volumen
faltante, truncado, corrupto, con hash distinto, con cobertura duplicada o con
huecos falla cerrado y conserva las demás unidades válidas.

El restore sólo publica tras completar los hashes, longitudes, cobertura,
durabilidad y reapertura en una raíz temporal owned y estable por `operation_id`.
El rollback es previo a la publicación: se descarta únicamente el temporal
owned de la operación que no alcanzó publicación, nunca B, A, el target ni un
backup válido. La restauración publicada permanece como evidencia independiente
y no se repite de rutina.

Para ejecutar un rehearsal nuevo cuando exista presupuesto suficiente:

```bash
PYTHONPATH="$PWD/src" /home/marjuraru/projects/investment-analyst/.venv/bin/python \
  scripts/smoke_workspace_v2_migration.py \
  --source-snapshot <owned-source-copy-B> --scratch-root <owned-scratch-root> \
  --environment-file <approved-service.env> --output <owned-ops-root>/smoke.json \
  --page-limit 256 --reserve-bytes 5000000000 --resume \
  --migration-receipt <verified-migration-receipt.json>
```

No usar ese comando para repetir el rehearsal acreditado: el smoke de
2026-10-12 ya reutilizó la migración válida y restauró independientemente el
backup existente.

## Cobertura, criterio y estado

| Grupo | Evidencia observable y estado antes de candidato |
| --- | --- |
| A1, A2, A3, A10, I1, I3, N1, N3 | Recibos `verify-20261012-02.json`, `migration-direct-20261012-15.json`, `smoke-recovery-20261012-02.json`, pruebas de migración/inventario/recovery y verificación posterior al cambio. Pendiente: consolidado fresco del SHA candidato. |
| A4, I2, N2 | Seis consumidores con reuso anterior al engine; pruebas de `execution_reuse`, incremental runtime y pipelines de mercado, derivados, fundamental, valoración, pesos institucionales y eventos. Pendiente: consolidado fresco que enlace el SHA candidato y sus resultados. |
| A5, N3 | Backup packed, restore serial, resume, corrupción, volumen ausente/truncado, rollback y receipt real. Pendiente: consolidado fresco del SHA candidato. |
| A6, A7, N4, N5 | Pruebas de scheduler, observabilidad, cycle probe, release local y fixture temporal de observabilidad. Pendiente: consolidado fresco del SHA candidato. |
| A8 | Prueba de scheduler local y probe HYMC provider-only con reason cerrado y sin secretos. Pendiente: resultado vivo del probe en el consolidado. |
| A9 | Este runbook, ruta 43→44→45, smoke consolidado exact-SHA y CI Python 3.12. Pendiente: commit, draft PR, CI y handoff AUDIT. |

`PASS` sólo se asigna en el consolidado exact-SHA cuando el comando, entorno,
binding y resultado actual están presentes. La tabla evita repetir una migración,
backup o restore ya comprobado, pero no transforma nombres de archivos o conteos
en PASS.

## Publicación y ruta

Antes de publicar, ejecutar el guard vivo, revisar el diff permitido y correr
Ruff/formato y las pruebas focalizadas pertinentes. Tras el commit, ejecutar el
consolidado ligado al SHA exacto, abrir un único draft PR, esperar CI Python
3.12 verde y preparar el handoff de AUDIT independiente. BUILD no se
autoaudita ni activa v2.

La ruta permanece `43 → 44 → 45`:

- **43:** migración aislada, recovery, adopción, telemetría y candidato v1
  compatible.
- **44:** backup Drive cifrado y restore externo completo; sólo después,
  reconciliación y activación v2.
- **45:** déficits cuantificados, metas operacionales comparables y limpieza
  autorizada por rutas, bytes, propiedad y recuperación.

No se esperan los siete días operacionales de 44/45 para concluir el trabajo
implementable de 43, y no se borra historia para cumplir un gate.
