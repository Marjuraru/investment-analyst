# Estrategia gratuita y escalable para la Bolsa de Valores de Lima

## Objetivo y límites del prototipo

La expansión a Perú debe añadir activos de la Bolsa de Valores de Lima (BVL) sin alterar las
identidades, fuentes o algoritmos existentes de Apple y Bitcoin. La primera fase será gratuita,
descriptiva y local-first. No ofrecerá tiempo real, ejecución de órdenes, recomendaciones,
comparaciones convertidas de moneda ni un ranking de emisores.

La investigación realizada el 25 de julio de 2026 identifica dos autoridades complementarias:

- la [SMV Open Data](https://www.smv.gob.pe/SMV.OpenData.Web/) para el registro de empresas,
  valores y estados financieros con licencia ODbL declarada por cada conjunto;
- la BVL para precios y documentos de mercado. Su web advierte que la cotización pública tiene
  20 minutos de retraso, mientras que el tiempo real es un servicio separado.

Esta combinación es compatible con el núcleo independiente de proveedores: SMV aporta identidad y
fundamentales; BVL aporta mercado. Sus observaciones no se fusionan por conveniencia ni comparten una
fuente artificial.

## Fuentes candidatas

### Catálogo y fundamentales: SMV

Los conjuntos oficiales más útiles son:

- [Valores inscritos](https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Valores_Inscritos.aspx):
  emisor, nombre del valor, nemónico, ISIN, tipo, moneda, fecha de inscripción y última cotización
  publicada;
- [Empresas inscritas](https://www.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Empresas_Inscritas.aspx):
  razón social, RUC, sector y datos registrales;
- [Información financiera](https://www.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Informacion_Financiera.aspx)
  y [Balance general](https://www.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/Balance_General.aspx):
  estados y cuentas por empresa, período, moneda y tipo de información;
- [Cuentas principales](https://mvnet.smv.gob.pe/SMV.OpenData.Web/Views/Datasets/CtasPrinc.aspx):
  resumen financiero oficial.

Las páginas publican servicios SOAP/ASMX o WSDL y declaran ODbL. Antes de implementar un conector se
debe ejecutar una prueba de cobertura actual: algunas fichas muestran fechas de publicación antiguas
y una URL accesible no demuestra por sí sola que todos los períodos recientes estén disponibles.

### Mercado: BVL pública diferida

La BVL publica el
[boletín diario de renta variable](https://documents.bvl.com.pe/pubdif/boldia/stockq.htm) con
nemónico, moneda, apertura, cierre, máximo, mínimo, volumen, monto y operaciones. Las
[notas del boletín](https://documents.bvl.com.pe/pubdif/boldia/bolnota.htm) explican, entre otras
reglas, el tratamiento de derechos, montos mínimos y frecuencia de negociación. Estos documentos
son una evidencia oficial auditable y un respaldo útil para cierres diarios.

La aplicación web oficial consulta actualmente `https://dataondemand.bvl.com.pe` y expone
operaciones para emisores, valores, cotización diaria, histórico de índices, acciones corporativas y
estados financieros. Una comprobación de solo lectura devolvió cotizaciones para un nemónico real.
Sin embargo, no se encontró documentación pública estable, versionado de API ni una autorización
explícita para consumo automatizado. Por ello se clasifica como **endpoint interno observado**, no
como contrato de producción.

No se seleccionarán Yahoo Finance, scrapers comunitarios ni datasets revendidos como fuente
canónica del prototipo. Pueden servir para contrastes manuales, pero no sustituyen evidencia oficial
ni condiciones de uso claras.

## Arquitectura propuesta

### Identidad

- El `asset_id` interno debe identificar la clase de valor, no una URL ni un proveedor.
- El ISIN validado será el candidato principal a identidad estable; el nemónico BVL se conservará
  como alias versionado porque puede cambiar.
- No se fijará el formato final de nuevos IDs hasta comprobar unicidad, cobertura y cambios
  históricos en el catálogo SMV.
- Cada clase, moneda y mercado conserva identidad separada. Una cotización en PEN no se mezcla con
  otra en USD ni se convierte silenciosamente.
- Los nuevos activos y fuentes se registran en el catálogo central y se resuelven mediante
  `ApplicationRuntime`.

### Contratos de fuente

Las fuentes se mantendrán explícitamente separadas:

- registro de emisores y valores SMV;
- fundamentales SMV por tipo de estado, período y moneda;
- cotización BVL pública diferida;
- boletín diario BVL como evidencia de cierre o respaldo;
- una futura fuente BVL en tiempo real o proveedor pagado, con un nuevo `source_id`.

Cambiar a un proveedor pagado no reescribirá registros gratuitos ni reutilizará su identidad de
fuente. Un adaptador nuevo implementará el mismo contrato normalizado y permitirá comparar cobertura
antes de promoverlo.

### Tiempo, calidad y point-in-time

- El horario de mercado se interpreta en `America/Lima`; toda persistencia se normaliza a UTC.
- `observed_at`, período, `available_at` y fecha de recuperación permanecen separados.
- La cotización pública BVL se marca `delayed`; no se presentará como tiempo real.
- Una sesión sin operación no se inventa ni se trata automáticamente como hueco. La detección de
  feriados y sesiones requiere un calendario explícito.
- Los precios se conservan con `Decimal`, moneda y unidad originales.
- Los boletines y respuestas se guardan como `RawRecord` con URL, hash y metadatos de recuperación;
  normalizaciones revisadas crean nuevas versiones append-only.
- Las acciones corporativas se modelan como evidencia separada. No se declarará una serie
  “ajustada” hasta que exista una regla oficial y versionada para cada evento.

### Escala y uso gratuito responsable

- El catálogo completo puede actualizarse con baja frecuencia; los precios se consultan solo para
  una lista explícita de seguimiento configurada por el usuario.
- Los conectores aplicarán límites de rango, paginación, caché, reintentos acotados y una tasa
  conservadora.
- Se usarán respuestas condicionales o recibos deterministas cuando la fuente lo permita.
- La interfaz pedirá rangos acotados y agregará antes de renderizar, como ya hace con AAPL y BTC.
- No se incorporará una dependencia nueva hasta validar que la biblioteca estándar y los
  componentes existentes no cubran el protocolo requerido.

## Plan de ejecución

### Checkpoint operativo del 28 de julio de 2026

Este checkpoint histórico inició BVL sin adelantar identidades ni persistencia que entonces
dependían del catálogo SMV y de una revisión contractual:

1. Implementar un lector tipado del boletín diario oficial de renta variable que descargue el
   documento completo con un límite explícito, valide URL, tipo de contenido y estructura, y
   calcule su SHA-256.
2. Extraer la fecha publicada por BVL y la tabla de cotizaciones mediante sus encabezados, no por
   posiciones globales del HTML.
3. Normalizar exclusivamente dentro del reporte de inspección: nemónico, moneda original, fecha
   previa, OHLC, variación, mejores propuestas, promedio, cantidad, monto, operaciones, frecuencia
   y variación anual. Los números usarán `Decimal`; los puntos de relleno y celdas vacías serán
   ausencia, nunca cero.
4. Exponer una CLI de solo lectura que permita filtrar nemónicos y emita JSON sin incluir el HTML.
   La ejecución no escribirá en el workspace ni creará todavía `asset_id`, `RawRecord`,
   observaciones o series históricas.
5. Proteger con pruebas offline el éxito, datos ausentes, moneda, decimales, fecha, duplicados,
   cambios de estructura, tamaño máximo y filtrado.
6. Ejecutar el lector contra la fuente real y conservar en Git únicamente código, pruebas,
   documentación y metadatos reproducibles; no se versionará el documento descargado.

La lista inicial solicitada se resolverá contra identificadores oficiales. El boletín consultado el
28 de julio de 2026 confirmó `CVERDEC1`, `BVN`, `SCCO`, `VOLCABC1`, `MINSURI1` y `POMALCC1`.
“FCA” permanece sin mapear hasta obtener una coincidencia inequívoca en SMV/BVL. `BVN` y `SCCO` en
BVL no se confundirán con sus cotizaciones estadounidenses ya existentes: mercado, moneda, clase e
identidad permanecerán separados.

El criterio de salida de este checkpoint fue un reporte repetible, estricto, limitado y sin efectos
laterales que pudiera leer esos nemónicos desde el boletín vigente y fallar de forma visible ante
ambigüedad o cambio estructural. El checkpoint posterior ya incorporó catálogo y persistencia
point-in-time del registro SMV; no convirtió por ello el boletín BVL en una fuente persistente.

#### Ejecución del lector

El checkpoint se ejecuta con:

```bash
.venv/bin/python scripts/inspect_bvl_daily_bulletin.py
```

La lista predeterminada contiene los seis nemónicos confirmados. Puede reemplazarse de forma
explícita, por ejemplo:

```bash
.venv/bin/python scripts/inspect_bvl_daily_bulletin.py --symbols CVERDEC1 MINSURI1
```

El lector limita la respuesta a 5.000.000 de bytes y exige que no haya truncamiento, una única fecha
de publicación, la ruta HTTPS exacta, contenido HTML y una sola tabla con el contrato completo de
17 columnas. El reporte incluye fecha del boletín y recuperación, URL, tamaño y SHA-256 del
documento, número de filas detectadas, cotizaciones seleccionadas y nemónicos ausentes. No incluye
el HTML y declara `persistence_performed=false`.

Este comando es una inspección técnica puntual, no una autorización de redistribución ni una
dependencia de producción. Hasta resolver las condiciones aplicables al boletín, no se programa,
no recorre históricos y no escribe en el workspace permanente.

### Fase 0 — validación contractual gratuita

1. Registrar una matriz reproducible de disponibilidad, licencia, límites y campos de cada dataset
   SMV y documento BVL.
2. Ejecutar smoke tests de solo lectura sobre emisores, valores, fundamentales y boletines.
3. Solicitar o localizar confirmación de BVL sobre uso automatizado de `dataondemand`; hasta
   entonces no convertirlo en dependencia operativa.
4. Definir fixtures sanitizados y pruebas de cambios de nemónico, duplicidad de ISIN, monedas y
   revisiones.

Criterio de salida: al menos una ruta oficial de catálogo/fundamentales y una de cierres de mercado
con licencia o condiciones compatibles, cobertura reciente comprobada y sin credenciales pagadas.

#### Primer control reproducible

El comando siguiente ejecuta el primer smoke test de fase 0:

```bash
.venv/bin/python scripts/probe_peru_official_sources.py
```

Comprueba, en orden estable, la ficha de valores inscritos de SMV, el boletín diario de renta
variable BVL y sus notas. Cada lectura se limita a 65.536 bytes, exige HTTPS y el host oficial,
valida marcadores mínimos y emite un hash SHA-256 del prefijo inspeccionado. El JSON no contiene el
documento fuente y declara `persistence_performed=false`; por tanto, no toca el workspace ni crea
activos, observaciones o identidades prematuras.

El resultado diferencia disponibilidad de autorización: SMV se registra como Open Data ODbL,
mientras los dos documentos BVL permanecen en
`public_document_terms_review_required`. Este control completa la prueba técnica inicial de esas
tres rutas, pero no satisface por sí solo el criterio de salida de la fase 0 ni autoriza ingestión
automatizada del boletín completo.

### Fase 1 — catálogo peruano

Completada el 29 de julio de 2026:

1. Los adaptadores consultan empresas y valores por razón social mediante el formulario HTTPS
   oficial, sin usar el endpoint SOAP HTTP declarado en el WSDL.
2. El catálogo resuelve seis listings por `asset_id`, nemónico, ISIN completo y emisor.
3. El código de ocho caracteres devuelto bajo `CodigoISIN` se conserva como código abreviado; no se
   completa por heurística.
4. Las respuestas completas se persisten de forma append-only e idempotente y se consultan por
   `known_at`.
5. El refresh por lote conserva el progreso anterior ante fallos y la consulta local no crea
   precios, métricas ni scores.

Consulta [Registro SMV y universo BVL](smv_bvl_registry.md) para contratos, estados y comandos.

### Fase 2 — mercado diario diferido

1. Implementar el boletín BVL como primera ruta auditable de cierre.
2. Añadir una fuente API diferida solo si supera la fase contractual.
3. Normalizar OHLC, volumen, operaciones, moneda, calidad y disponibilidad.
4. Probar días sin negociación, feriados, acciones corporativas y revisiones.
5. Integrar el gráfico diario usando el servicio histórico común, sin fórmulas específicas por
   emisor.

Criterio de salida: consulta point-in-time, idempotencia, frescura explícita y comparación
reproducible entre API y boletín cuando ambas existan.

### Fase 3 — fundamentales SMV

1. Incorporar estados oficiales manteniendo empresa, período, tipo y moneda.
2. Crear métricas peruanas solo después de revisar taxonomías y unidades.
3. Mantener diagnósticos fundamentales separados del mercado.
4. Exponer fórmulas, limitaciones e inputs exactos.

### Fase 4 — operación y expansión

1. Scheduler independiente por mercado/fuente y zona horaria.
2. Estado de frescura por activo y cobertura del universo.
3. Backfill por ventanas acotadas y reanudables.
4. Adaptador pagado opcional para tiempo real, mayor historia o acciones corporativas, sin migrar ni
   borrar la evidencia gratuita.

## Riesgos abiertos

- La web BVL pública es diferida y el endpoint `dataondemand` no constituye todavía un contrato
  documentado.
- El HTML o PDF de boletines puede cambiar de estructura; el parser debe fallar de forma visible y
  conservar el documento original.
- SMV y BVL pueden usar identificadores, monedas o fechas distintas para el mismo valor.
- Baja liquidez y sesiones sin operaciones hacen incorrecto inferir huecos con un calendario diario
  genérico.
- Derechos, dividendos, splits y cambios de nemónico impiden prometer una serie ajustada sin una
  política explícita.

Estos riesgos bloquean una implementación improvisada, no el roadmap: cada uno tiene una fase de
validación antes de afectar el workspace permanente.
