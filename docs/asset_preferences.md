# Preferencias persistentes de activos

La watchlist, los favoritos y la selección de actualización automática viven en un único contrato
local por workspace. No cambian el catálogo, las capacidades, los identificadores de fuente, las
fórmulas ni el formato v1 del workspace.

## Estado y precedencia

El archivo `state/asset_preferences_state_v1.json` contiene revisiones append-only
`asset-preferences-revision-v1`. Cada revisión tiene UUID, hora UTC, revisión padre, fingerprint
SHA-256 determinista y entradas ordenadas por `asset_id`. Cada entrada declara por separado:

- `watchlist`;
- `favorite`;
- `scheduled_refresh`.

Favoritos y activos programados siempre pertenecen a la watchlist. Cada documento admite como
máximo 100 activos, 1 000 revisiones y 4 MiB. Antes de llegar al límite —900 revisiones o 3 MiB en
el estado activo— las revisiones antiguas pasan a segmentos inmutables bajo
`state/asset_preferences_state_v1_archives/` y permanecen encadenadas a las 100 revisiones activas.
Cada referencia fija nombre, tamaño, SHA-256, extremos, conteo y revisión padre. Tanto el estado
activo como cada segmento se rechazan antes de leer si exceden 4 MiB; después se validan esquema,
hash, cadena global, identidades y timestamps.

El segmento se publica primero mediante temporal privado, `fsync` y reemplazo atómico; solo después
se publica el estado que lo referencia. Una interrupción puede dejar un segmento huérfano inocuo,
pero nunca un estado visible que apunte a un segmento ausente. El nombre determinista permite que
un reintento reutilice únicamente bytes idénticos. Un archivo corrupto, incompatible, no regular o
no escribible produce un error acotado y nunca se repara ni reescribe al arrancar.

Si el archivo no existe, el servicio deriva en memoria el estado efectivo desde las opciones CLI:

- sin `--schedule-asset`, todos los activos de mercado resolubles quedan en watchlist y programados;
- con una o más opciones `--schedule-asset`, solo esos activos quedan seleccionados;
- esta semilla no crea el archivo por sí sola.

La primera actualización explícita crea la primera revisión, incluso si coincide con la semilla.
Después, el estado persistido manda sobre `--schedule-asset`. Un update semánticamente idéntico es
idempotente y no crea otra revisión. `--no-scheduler` desactiva la ejecución sin borrar ni modificar
preferencias: `scheduled_refresh` conserva la intención configurada,
`effective_scheduled_refresh=false`, y los conteos de activos y jobs efectivos son cero en API,
overview e interfaz.

Un activo añadido posteriormente al catálogo aparece disponible pero no seleccionado en una
watchlist persistida. Un ID guardado que deje de estar disponible permanece visible, conserva sus
elecciones históricas y queda fuera de la ejecución; una actualización no puede modificarlo ni
eliminarlo accidentalmente.

## API optimista

```text
GET /api/v1/asset-preferences
PUT /api/v1/asset-preferences
```

GET devuelve `asset-preferences-view-v1`: origen `cli_seed` o `persisted`, revisión, fingerprint,
conteos y proyecciones `asset-preference-projection-v1` compactas por activo. Proveedor, fuentes,
frecuencias y capacidades se derivan del catálogo actual; no se guardan como preferencias mutables.
Los favoritos se ordenan primero sin ocultar activos no favoritos o no disponibles.

PUT acepta únicamente `asset-preferences-update-v1`, con `expected_revision_id`,
`expected_fingerprint` y entradas completas. Extras, schema desconocido, booleanos ambiguos,
duplicados, orden no determinista, activos no resolubles y favoritos/programados fuera de watchlist
se rechazan antes de escribir. Una revisión obsoleta devuelve `409 asset_preferences_conflict`.
Las rutas no esperan proveedores ni adquieren el writer financiero.

Ejemplo abreviado para la primera actualización:

```json
{
  "schema_version": "asset-preferences-update-v1",
  "expected_revision_id": null,
  "expected_fingerprint": "<fingerprint devuelto por GET>",
  "entries": [
    {
      "asset_id": "crypto:btc-usd",
      "watchlist": true,
      "favorite": true,
      "scheduled_refresh": true
    }
  ]
}
```

## Reconciliación del scheduler

La frontera preferencias→jobs vuelve a construir el registro solo con el universo, capacidades y
configuración operativa ya validada. Construir o publicar ese registro no llama proveedores. El
scheduler reemplaza el tuple ordenado bajo un lock corto y cada tick usa un snapshot inmutable. Antes
de iniciar cada callback del snapshot vuelve a reclamar su `job_id` bajo el mismo lock: un job
retirado que aún estaba en cola se omite y no crea intento.

Una actualización no cancela el callback que ya está activo. Si su job fue retirado, el intento
termina y se conserva, pero no se programa otra vez. Reactivar el activo reconstruye el mismo
`job_id`; el historial y los `attempt_id` anteriores permanecen en
`multi_asset_schedule_state_v1.json`. No existe una segunda cola ni un segundo writer.

Con scheduler habilitado debe quedar al menos un activo disponible programado. Con
`--no-scheduler`, una selección vacía es válida y queda lista para una decisión posterior.

## Interfaz, backup y seguridad

El panel «Watchlist y automatización» usa controles nativos de formulario, etiquetas accesibles,
estado anunciado y una tabla desplazable en móvil. Distingue watchlist, favorito y programación;
un cambio se confirma o informa conflicto. `localStorage` no guarda estas preferencias.

El backup inventaría el documento y todos sus segmentos como archivos regulares de `state/`; restore
conserva sus bytes, cadena completa, revisión y fingerprint. Un backup sin preferencias continúa
siendo válido. Los contratos y errores no incluyen credenciales, payloads de proveedor, headers ni
valores arbitrarios de excepciones.
