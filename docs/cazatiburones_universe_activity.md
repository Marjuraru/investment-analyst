# Índice universo-wide de actividad declarada

`cazatiburones-universe-activity-v1` es una consulta local, read-only y point-in-time que
enumera los emisores corporativos SEC configurados o un subconjunto explícito del catálogo. Para
cada activo expone tres familias independientes: `insider`, `beneficial` e `institutional`.

Cada familia conserva `capability`, `evidence`, `statements`, `latest_available_at` y
`latest_age_days`. `statements` cuenta únicamente filas persistidas cuya `available_at` no supera
`known_at`. La familia institucional filtra exclusivamente el source ID de observaciones 13F;
no resuelve el reporte padre, correspondencias de instrumentos ni artefactos derivados.

La ausencia no se convierte en cero semántico: `not_configured`/`not_queried` identifica un activo
sin configuración SEC; `supported`/`missing` identifica una lectura válida sin filas elegibles;
`supported`/`not_queried` con `malformed_persisted_record` identifica una familia que no pudo
validarse sin ocultar el problema. Las enmiendas permanecen sin resolver y los conteos no se
combinan entre familias ni entre activos.

## Endpoint

```text
GET /api/v1/cazatiburones/universe-activity?known_at=2026-07-16T15:47:00Z
```

`known_at` es obligatorio. `asset_id` puede repetirse hasta 40 veces; si se omite, la consulta
usa todos los emisores corporativos con configuración SEC del catálogo. Un `asset_id` desconocido
falla cerrado. La respuesta incluye la versión y el hash del catálogo, la petición normalizada,
los activos ordenados y las limitaciones literales de cada activo.

Este índice no calcula métricas, score, ranking, señal, recomendación ni cartera efectiva. Las
consultas por activo y las capas posteriores del dominio permanecen independientes.
