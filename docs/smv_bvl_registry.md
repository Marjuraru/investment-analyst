# Registro SMV y universo BVL

## Alcance

Este módulo resuelve la identidad inicial de seis cotizaciones de la Bolsa de Valores de Lima sin
atribuirles todavía una serie de precios ni fundamentales. La SMV aporta evidencia registral del
emisor y de sus valores inscritos; la identidad completa de la cotización conserva un ISIN
corroborado en documentos oficiales BVL. Ambas fuentes mantienen contratos separados.

El universo configurado es:

| `asset_id` | Nemónico BVL | ISIN corroborado | Moneda |
| --- | --- | --- | --- |
| `equity:pe:bvl:bvn` | `BVN` | `US2044481040` | USD |
| `equity:pe:bvl:cverdec1` | `CVERDEC1` | `PEP646501002` | USD |
| `equity:pe:bvl:minsuri1` | `MINSURI1` | `PEP622005002` | PEN |
| `equity:pe:bvl:pomalcc1` | `POMALCC1` | `PEP779301006` | PEN |
| `equity:pe:bvl:scco` | `SCCO` | `US84265V1052` | USD |
| `equity:pe:bvl:volcabc1` | `VOLCABC1` | `PEP648014202` | PEN |

`BVN` y `SCCO` de BVL tienen IDs distintos de `equity:us:bvn` y `equity:us:scco`. Compartir
nemónico o ISIN económico no autoriza mezclar mercados, monedas, proveedores ni series.

## Fuente y contrato

El cliente usa exclusivamente los formularios HTTPS del
[Portal de Datos Abiertos SMV](https://mvnet.smv.gob.pe/SMV.OpenData.Web/):

- [Empresas inscritas](https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Empresas_Inscritas.aspx);
- [Valores inscritos](https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Valores_Inscritos.aspx).

Ambos datasets declaran licencia ODbL. El cliente realiza primero un `GET` acotado para obtener el
estado ASP.NET y después un `POST` `application/x-www-form-urlencoded` al mismo host, ruta y esquema
HTTPS. No utiliza el endpoint SOAP HTTP publicado por el WSDL. Cada respuesta está limitada a
2.000.000 de bytes y se validan código HTTP, redirección, tipo de contenido, UTF-8, encabezados,
ancho de filas y eco exacto de la razón social.

La columna denominada `CodigoISIN` devuelve actualmente códigos abreviados de ocho caracteres,
como `64650100`. El sistema los conserva como `reported_security_code`; no los completa por
heurística ni los presenta como ISIN. Los ISIN del catálogo tienen doce caracteres, checksum ISO
6166 válido y evidencia BVL independiente. En `BVN`, la consulta SMV del emisor no expone el
nemónico BVL `BVN`; por ello su estado máximo actual es `issuer_verified`, no `security_verified`.

## Persistencia y tiempo

Cada respuesta completa HTTPS se guarda como `RawRecord` append-only bajo una fuente de tipo
`registry`. El `record_key` conserva dataset, razón social, SHA-256 del cuerpo y SHA-256 del
contenido registral canónico. El payload conserva el HTML exacto recibido dentro de un sobre Base64
para evitar que el contrato JSON normalice whitespace; al leerlo se decodifica, vuelve a analizar y
verifica cuerpo, identidad, fuente, timestamps y semántica.

SMV no publica un timestamp histórico de disponibilidad para estas revisiones. Por tanto:

- `available_at` es la primera recuperación local verificada;
- una repetición con los mismos registros reutiliza la revisión aunque cambie el `VIEWSTATE`;
- un cambio semántico crea otra revisión append-only;
- dos revisiones distintas con el mismo `available_at` fallan como ambiguas;
- la consulta `known_at` nunca utiliza una recuperación posterior al corte;
- si valores inscritos falla después de persistir empresas inscritas, el primer progreso se
  conserva y la consulta lo muestra como parcial.

No se crean activos persistidos, observaciones de precios, métricas ni diagnósticos. Las identidades
viven en el catálogo versionado y se relacionan con evidencia local solo durante la consulta.

## Uso

Actualizar todo el lote configurado:

```bash
.venv/bin/python scripts/refresh_bvl_registry.py
```

Actualizar una cotización:

```bash
.venv/bin/python scripts/refresh_bvl_registry.py \
  --asset-id equity:pe:bvl:cverdec1
```

Consultar el universo local en un corte explícito:

```bash
.venv/bin/python scripts/query_bvl_registry.py \
  --known-at 2026-07-29T12:00:00Z
```

Los comandos aceptan también `--workspace RUTA` o `--root RUTA`. El refresh abre una sola conexión
writer para todo el lote y procesa los activos en orden estable; una falla tardía no borra activos
anteriores. Cuando varias cotizaciones comparten razón social, la consulta registral del emisor se
ejecuta una vez y se reutiliza para validar cada identidad. La consulta local abre almacenamiento
read-only y no construye transporte HTTP.

Estados expuestos:

- `not_imported`: no existe evidencia SMV elegible al corte;
- `partial`: solo uno de los datasets está disponible;
- `issuer_verified`: emisor SMV verificado, pero el valor BVL exacto no aparece en esa consulta;
- `security_verified`: emisor, nemónico, código abreviado, moneda e identidad configurada son
  compatibles;
- `security_mismatch`: la evidencia existe pero no confirma el valor configurado.

## Límite de la fase

Este módulo completa identidad y catálogo. El lector del boletín BVL continúa como inspección de
solo lectura porque sus condiciones de automatización y redistribución aún requieren resolución.
Hasta entonces las seis cotizaciones no aparecen como series de mercado en la interfaz ni se
programan en segundo plano.

## Validación real

El 29 de julio de 2026 se ejecutó el lote completo contra los dos formularios HTTPS oficiales en un
workspace temporal. La primera ejecución creó 12 `RawRecord` —dos por cada uno de los seis
emisores— y la repetición creó cero y reutilizó los 12. La reconstrucción local al corte
`2026-07-29T06:00:00Z` examinó y seleccionó los 12 registros con trazabilidad verificada:
`BVN` quedó `issuer_verified` y las otras cinco cotizaciones, `security_verified`. El workspace
permanente no participó en esta validación.
