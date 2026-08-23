# Readiness operacional y rehearsal de recuperación

## Alcance

`operational-readiness-report-v1` convierte el estado operacional persistido en una decisión
binaria, repetible y fail-closed. La sonda no ejecuta scheduler, observers, providers, refresh,
reconciliación ni writers. Sólo usa `WorkspaceService.inspect` y los `load()` públicos del
scheduler, alertas operativas, screening analítico y operaciones manuales.

La decisión es descriptiva:

- `PASS`: el snapshot leído satisface los joins y políticas declarados;
- `NOT_READY`: el snapshot es válido, pero uno o más reason codes acotados impiden readiness;
- error: el input o el estado no puede leerse como un snapshot válido y estable.

No es un health check HTTP, una recomendación financiera ni una autorización para detener el
servicio, respaldar, restaurar, habilitar `systemd`, marcar el PR ready o fusionar.

## Uso read-only

El workspace debe existir y estar inicializado. El corte requiere UTC explícito:

```bash
.venv/bin/python scripts/check_operational_readiness.py \
  --workspace <workspace-inicializado> \
  --since 2026-08-01T00:00:00Z \
  --min-local-dates 3
```

El exit code es `0` para `PASS`, `3` para `NOT_READY` válido y `2` para input/estado inválido,
ilegible o cambiante. La salida válida contiene parámetros efectivos, resumen acotado, reason
codes ordenados, fingerprint de evidencia y fingerprint semántico del reporte. No contiene rutas,
payloads financieros, mensajes arbitrarios de proveedor ni secretos.

La sonda exige:

- al menos un job con intentos terminales en el mínimo de fechas locales solicitado;
- cero intentos `running` posteriores al corte y cero operaciones manuales `queued`/`running`;
- categorías conocidas, presupuesto diario, secuencia, retry policy y backoff coherentes;
- exactamente las cuatro reglas `OperationalRuleId` por intento terminal observado, con job,
  activo, provider, dominio, estado y timestamps coincidentes;
- exactamente un `AnalyticalMonitorReceipt` por intento, `screened` con sus resultados exactos o
  `skipped` con razón explícita y cero resultados;
- ausencia de screenings, receipts o resultados huérfanos/duplicados.

En un receipt `screened`, `processed_at` se une a `computed_at` de cada resultado y la evidencia
conserva su `known_at` efectivo, pero no se compara por orden con `attempt.completed_at`: son
lecturas independientes de wall clock, no un reloj monotónico. En un receipt `skipped`, que no
produce resultados, `processed_at` debe coincidir exactamente con `attempt.completed_at`. No se
aplica tolerancia ni ventana temporal en ninguno de los dos casos.

Un fallo terminal conocido puede coexistir con `PASS` cuando queda explícitamente clasificado,
respeta su policy y tiene ambos observadores completos. No se convierte en éxito ni se excluye.
Una categoría legacy, retry no permitido, presupuesto excedido, join incompleto o trabajo activo
produce `NOT_READY`.

Scheduler, alertas operativas y receipts analíticos son archivos requeridos. La ausencia estable de
`manual_operation_state_v1.json` conserva la semántica pública de documento vacío y se informa con
`manual_operation_state_present=false`; ninguna otra ausencia se interpreta como cero. Antes y
después de los `load()`, la sonda compara identidad, tamaño, mtime y, para los documentos JSON,
SHA-256. Un cambio concurrente termina con error `snapshot_changed`.

## Evidencia reutilizada por OPS-8

OPS-8 no repite jobs costosos para fabricar datos. Reutiliza la aceptación humana de
[Issue #85](https://github.com/Marjuraru/investment-analyst/issues/85): SMV real PASS con attempt
`7bd8725f-fe70-4ff5-91b7-04d20b654a11`, delta peak RSS atribuible de 64 724 kB, sin OOM/SIGKILL,
y restart idle con workspace UUID `19cf83b5-8df3-4663-9369-4b61f86eceea` y cuatro endpoints 200.

El historial vivo reconciliado por [OPS-8/#89](https://github.com/Marjuraru/investment-analyst/issues/89)
registró desde el corte declarado 719 intentos terminales en 18 fechas locales, cero `running`,
cuatro screenings y un receipt por intento. Esa observación orienta el gate, pero no se versiona
como snapshot: la sonda debe revalidar el estado vivo durante la aceptación HUMAN.

Los dos fallos Deribit `provider_contract_error` conocidos son no reintentables y permanecen
visibles. Sólo son compatibles con `PASS` si siguen terminales, sin retry posterior, con freshness
y observers coherentes. El peak global de systemd cercano a 6,2 GiB no se atribuye al job SMV ni se
convierte en un presupuesto inventado; queda como deuda separada de medición por job antes de fijar
un límite de memoria.

## Rehearsal HUMAN exact-SHA

BUILD y AUDIT no ejecutan este procedimiento. Una persona lo realiza antes del merge, sobre el SHA
candidato auditado y desde su release/worktree aislado:

1. Revalidar Issue/PR/SHA, BUILD y AUDIT PASS, CI, release viva, UUID, servicio
   `active/running + disabled`, loopback 200, scheduler idle y cola manual idle.
2. Ejecutar la sonda sobre el workspace permanente con el corte y mínimo de #89. Registrar sólo
   fingerprint, conteos agregados, fechas y reason codes; debe devolver `PASS`.
3. Crear una raíz única con `mktemp -d` bajo scratch; `backup` no debe existir y `restored` debe ser
   nuevo. Nunca usar el checkout OPS-2 ni una ruta bajo el workspace fuente.
4. Detener una sola vez `investment-analyst.service`, releer `inactive` y abortar sin backup si no
   queda detenido.
5. Ejecutar los scripts protegidos `backup_workspace.py` y `restore_workspace.py`. El origen
   detenido es sólo lectura; el restore publica únicamente el destino nuevo en scratch.
6. Comparar workspace UUID, backup ID, manifest/inventario ordenado `path,size,sha256`, versiones,
   cuatro conteos y `traceability_verified=true`. La sonda sobre el restore debe producir el mismo
   fingerprint semántico que el origen.
7. En una ruta `finally`, arrancar exactamente una vez, releer `active/running` y comprobar overview,
   candidate-notifications, market y fundamentals 200 con UUID preservado.
8. Sólo después de todos los gates, ejecutar `systemctl --user enable investment-analyst.service`
   sin `--now`, y releer `active/running + enabled`.
9. Limpiar únicamente la raíz scratch explícita y confirmar que no quedan backup, restore ni
   temporales. Publicar evidencia compacta exact-SHA sin rutas privadas ni secretos.

Si una etapa posterior al stop falla, el único rollback permitido es un start compensado y su
relectura. Si el enablement creado por este rehearsal queda incoherente, se deshabilita sólo cuando
puede probarse que la misma aceptación lo creó. Nunca se restaura sobre el origen, se edita el
workspace, se repite un restart independiente ni se usa `enable --now`.

## Frontera de readiness y release

`ANALYST-READINESS` sólo pasa a `DONE` cuando BUILD, CI, AUDIT, rehearsal HUMAN exact-SHA y merge
terminan PASS. OPS-8 no completa `RELEASE-ACCEPTANCE`: el soak de 72 horas, restart de laptop,
benchmarks, accesibilidad y aceptación integral permanecen en la ruta futura. `SEC-CORPUS` sólo se
vuelve elegible después de integrar la transición de OPS-8 en `main`.
