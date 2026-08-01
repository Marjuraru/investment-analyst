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

Favoritos y activos programados siempre pertenecen a la watchlist. El documento admite como máximo
100 activos, 1 000 revisiones y 4 MiB; al alcanzar un límite rechaza la escritura sin recortar el
historial. Las escrituras usan un temporal privado, `fsync` y reemplazo atómico. Un archivo corrupto,
incompatible, no regular o no escribible produce un error acotado y nunca se repara ni reescribe al
arrancar.

Si el archivo no existe, el servicio deriva en memoria el estado efectivo desde las opciones CLI:

- sin `--schedule-asset`, todos los activos de mercado resolubles quedan en watchlist y programados;
- con una o más opciones `--schedule-asset`, solo esos activos quedan seleccionados;
- esta semilla no crea el archivo por sí sola.

La primera actualización explícita crea la primera revisión, incluso si coincide con la semilla.
Después, el estado persistido manda sobre `--schedule-asset`. Un update semánticamente idéntico es
idempotente y no crea otra revisión. `--no-scheduler` desactiva la ejecución sin borrar ni modificar
preferencias.

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
conteos y una proyección compacta por activo. Proveedor, fuentes, frecuencias y capacidades se
derivan del catálogo actual; no se guardan como preferencias mutables. Los favoritos se ordenan
primero sin ocultar activos no favoritos o no disponibles.

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
scheduler reemplaza el tuple ordenado bajo un lock corto y cada tick usa un snapshot inmutable.

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

El backup inventaría el documento como cualquier archivo regular de `state/`; restore conserva sus
bytes, revisión y fingerprint. Un backup sin preferencias continúa siendo válido. Los contratos y
errores no incluyen credenciales, payloads de proveedor, headers ni valores arbitrarios de
excepciones.
