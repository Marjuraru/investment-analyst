# DATA-CHASSIS-44: backup externo, cutover y capacidad física

> Decisión PLAN 2026-10-21: [plan vigente de arranque v2 nuevo](data_chassis_v2_fresh_start_plan.md). Primero datos nuevos y aceptación; historia v1/F5 preservada e incorporación selectiva posterior. Esta decisión está planificada, no acredita activación. Las recetas de migración anteriores son históricas y no se ejecutan para el arranque nuevo.


Documento histórico de 44. La receta vigente y las dependencias actuales de retiro están en
[`data_chassis_45_acceptance_runbook.md`](data_chassis_45_acceptance_runbook.md). Los estados de
entrada siguientes describen su ejecución original; no autorizan recrear copias ya retiradas.

Este runbook opera sobre los artefactos ya acreditados de DATA-CHASSIS-43.
No autoriza repetir la migración, recomprimir los 676 volúmenes, borrar el
workspace permanente ni sustituir los guards vivos.

## Estado de entrada reutilizable

- `target-v2` y `backup-v2` son artefactos del ensayo 43 que se conservan.
- El backup packed contiene 676 volúmenes sellados, inventario JSONL y journal
  vinculados por sus manifests y recibos de 43.
- B fue retirado mediante lifecycle con sus checkpoints y recibos; su
  complemento permanece fuera del árbol retirado. `restore-v2` se conserva
  como candidato restaurado hasta que el cutover real complete sus guards.
- El workspace `default` sigue siendo historia permanente y no entra en la
  limpieza de este bloque.

Antes de reutilizar un recibo, enlazarlo con su SHA, UUID, fingerprint,
manifest, parámetros y entorno. Un conteo o un nombre de archivo no acredita
una operación distinta.

## Incidente de capacidad física del 2026-10-13

El ensayo retiró datos locales reutilizables, pero Windows no recuperó
automáticamente el espacio correspondiente. La causa fue el disco dinámico de
WSL (`ext4.vhdx`): liberar bloques dentro de ext4 modifica el uso lógico de
Linux, pero el archivo VHDX puede mantener sus bloques físicos asignados en C:.

Las mediciones comprobadas fueron:

| Medición | Antes de la compactación administrativa | Después |
| --- | ---: | ---: |
| Tamaño lógico del VHDX | 134.851.067.904 B (125,59 GiB) | 87.785.734.144 B (81,76 GiB) |
| Reducción atribuible del VHDX | — | 47.065.333.760 B (43,83 GiB) |
| C: libre observado | Variable durante el diagnóstico; mínimo medido 6.378.061.824 B | 68.489.121.792 B en la primera lectura posterior |

El espacio libre de C: puede cambiar por trabajo ajeno al bloque, por lo que
la diferencia entre dos lecturas de C: no se atribuye íntegramente al proyecto.
La reducción del tamaño del VHDX sí es la evidencia física de la recuperación.

En el momento del diagnóstico, los consumidores conocidos dentro de WSL
incluían aproximadamente 16,08 GB asignados del workspace `default`, 10,60 GB
del rehearsal 43 (`target-v2`, `backup-v2` y complemento), 8,57 GB de cachés,
8,60 GB de paquetes/sesiones de Codex y otros proyectos. Esos valores son
inventario operativo, no autorización para limpiarlos. En particular, no se
borran cachés, workspaces ni paquetes compartidos por antigüedad.

El intento de usar `wsl --manage ... --set-sparse true` fue rechazado por WSL
sin `--allow-unsafe`. No se fuerza ese modo: WSL advierte posible daño de datos.

## Regla de presupuesto

Cada fase que escriba debe medir C: mediante el lector de capacidad vigente en
la misma operación. Requiere:

```text
capacidad física libre >= 5.000.000.000 B de reserva + crecimiento de la siguiente unidad
```

El cutover y restore externo requieren además el piso físico de 25.000.000.000
B declarado por el Work Block. Un valor viejo, una estimación de tamaño lógico,
o alcanzar sólo la reserva no permite escribir. Ante una lectura inválida o un
déficit, la fase de escritura pausa y conserva su checkpoint; continúan sólo
trabajos seguros que no escriban el destino.

El presupuesto registra separadamente: datos ya asignados, crecimiento de la
unidad, temporales acotados, controles/evidencia, reserva, pico de copias
simultáneas y capacidad física de C:. No usa `du` por página ni infiere bytes
físicos a partir de conteos.

## Prevención durante backup y restore

1. Reutilizar los volúmenes sellados válidos; no recopiarlos ni recomprimirlos.
2. Transportar directamente a Drive mediante objetos por digest y publicar el
   manifest remoto sólo después de confirmar objetos y controles.
3. Consumir inventario, journal, cierre, verificación y restore de forma
   incremental. No materializar JSONL, modelos Pydantic ni árboles globales.
4. Mantener una o dos unidades de transferencia/cache como máximo y los dos
   slots v2 autorizados. No crear un histórico intermedio.
5. Registrar bytes examinados, nuevos, comprimidos, enviados y reutilizados,
   junto con RSS/HWM, I/O, unidad y checkpoint.
6. Tras una fase grande, medir de nuevo C: y el VHDX. La liberación lógica no
   se contabiliza como capacidad física hasta que Windows la muestre.

## Mantenimiento VHDX fuera de la operación de datos

La compactación no se ejecuta durante una migración, backup, restore o writer
activo. Es una operación administrativa separada y sólo procede cuando los
guards, los recibos y los checkpoints prueban que puede detenerse WSL sin
perder trabajo. Antes: sincronizar escrituras, registrar `git status`, comprobar
que no hay writer y medir VHDX/C:. Después: reabrir WSL, comprobar los mismos
bindings y volver a medir.

En Windows, una cuenta con privilegio administrativo ejecuta:

```powershell
wsl --shutdown
diskpart
```

En DiskPart se selecciona exclusivamente el VHDX de la distribución registrada
en `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss` y se ejecuta
`compact vdisk`. Nunca se compacta un path inferido, un VHDX de Docker ni una
distribución que siga activa. La operación se registra con tamaño VHDX y C:
antes/después, código de salida y resultado de la comprobación posterior.

## Drive, restore y cierre

El remoto requiere OAuth de la cuenta del usuario, carpeta dedicada con ID
verificado, cuota suficiente, `crypt` y custodia del password/salt fuera de
WSL, C: y la misma carpeta Drive. No se guardan tokens o secretos en Git,
recibos o logs. La restauración real se hace desde Drive a una raíz vacía sin
acceso a los volúmenes locales, verifica cada unidad en su pasada de lectura y
publica sólo tras cobertura, reapertura y validación semántica.

Antes de candidato: ejecutar guard vivo, pruebas focalizadas, mediciones y
smoke con los bindings del SHA. Después del commit, CI del SHA exacto y AUDIT
independiente deciden los pasos siguientes; este runbook no activa v2 por sí
solo.

### Probe P1 del 2026-10-13

Se verificó `rclone` v1.75.2 contra el remoto `ia_drive_crypt` ya anclado a
la carpeta dedicada. La consulta de cuota devolvió 5 TiB totales, 4,907 TiB
libres y 49,073 GiB usados. Un objeto propio de 32 bytes fue subido cifrado,
leído con igualdad byte a byte y eliminado de su misma ruta. El archivo local
de configuración tiene modo `0600`.

No se registran tokens, secretos, contraseña ni ID de carpeta en este recibo.
Antes de la carga masiva, el usuario debe conservar las dos claves de `crypt`
en un gestor externo a WSL, C: y la carpeta Drive. Esa custodia no se infiere
de que el remoto responda ni se sustituye por la ofuscación de `rclone`.

## Corte finito y activación preparada

El catch-up usa `WorkspaceV2CutoverService`: primero se obtiene un
`FrozenSourceCut` sólo después de cerrar el writer mediante su flujo normal.
El corte vincula root, UUID, formato, fecha de creación y fingerprint. El
checkpoint `workspace-v2-cutover-checkpoint-v1` se publica antes de invocar
los importadores existentes; estos conservan sus propios cursores por página.
Si el fingerprint cambia, el corte queda inválido y no se aplica sobre el
destino. Esta operación no clona el workspace ni copia una DB/WAL viva.

`LocalReleaseService` mantiene `deployment_state.json` v1 legible y añade el
compañero tipado `deployment_workspace_binding.json`. El binding incluye SHA,
root, UUID, formato y corte del workspace. Por tanto un mismo SHA con otro
`--workspace` retargetea la unidad en vez de devolver un no-op. El rollback
normal rechaza volver de un binding v2 a uno v1 salvo autorización explícita
para el caso previo a escrituras v2; después de una escritura, se conserva el
workspace v2 o se recupera con código compatible.

El paquete distribuible incluye tanto `001_initial.sql` como
`002_workspace_v2.sql`. El smoke de wheel instalado crea v1 y v2 desde un CWD
externo y ejecuta backup/restore v2 pequeño para comprobar que la migración v2
está dentro del artefacto, no sólo disponible desde el checkout.

## Pendiente operacional antes del rollout real

El rollout sobre `default` se ejecuta únicamente desde `main` integrado y con
un guard vivo. Requiere un corte F actual, writer cerrado, al menos 25 GB
físicos libres en C: además de la reserva y crecimiento de la unidad, una
medición fresca del lector de capacidad y un recibo de delta Drive. El ensayo
aislado no acredita una activación productiva. `default` y el v1 congelado se
conservan hasta la decisión del bloque 45.
