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

### Fase 1 — catálogo peruano

1. Crear adaptadores SMV para empresas y valores.
2. Resolver identidad por ISIN y aliases BVL en el catálogo central.
3. Persistir evidencia cruda y observaciones registrales de forma idempotente.
4. Añadir una consulta local del universo sin precios ni scores.

Criterio de salida: repetición equivalente con cero identidades nuevas, revisión distinta
append-only y trazabilidad completa hasta la respuesta SMV.

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
