# Catálogo versionado de activos

El catálogo separa el ID canónico estable de cada activo de los identificadores asignados por
proveedores externos. Un ticker es útil para mostrar, pero no constituye una identidad global
segura: el mismo texto puede reutilizarse entre mercados, proveedores o clases de activo. Los IDs
canónicos ya utilizados por el repositorio permanecen sin cambios.

Un binding Alpaca puede añadir el namespace opcional `history_start` para declarar el primer día
que pertenece inequívocamente al activo y es ingerible bajo el contrato del feed. Barrick cambió
de `GOLD` a `B` al comenzar la negociación del 9 de mayo de 2025; el feed IEX representa ese día
de transición con cero volumen, cero operaciones y VWAP cero. Su borde se fija en `2025-05-10`:
excluye el símbolo `B` del emisor anterior y el placeholder, sin inventar un VWAP ni debilitar la
validación común. El source ID y el formato del workspace no cambian.

El documento empaquetado `default_assets.v1.json` es estático, versionado y determinista. Contiene
Bitcoin, Apple y la lista inicial de acciones y ETF estadounidenses comprobada con el nivel gratuito
de Alpaca IEX: AMD, Barrick (`B`), BVN, CDE, HYMC, INTC, MSTR, MU, MUX, NEM, PLTR, SCCO, TSM,
GBTC, GLD e IBIT. También contiene seis identidades de cotización BVL verificadas:
`CVERDEC1`, `BVN`, `SCCO`, `VOLCABC1`, `MINSURI1` y `POMALCC1`. No contiene credenciales,
observaciones de mercado, parámetros de algoritmos, recomendaciones ni valores que cambien con el
tiempo.

## Modelo

`CatalogAsset` amplía el contrato `Asset` del núcleo con alias y vinculaciones de proveedor
explícitas. Los alias no distinguen mayúsculas y minúsculas, y no sustituyen el ID canónico. Cada
vinculación contiene:

- `provider`: identificador en minúsculas como `alpaca`, `sec` o `coinbase`;
- `namespace`: tipo de identificador del proveedor, como `symbol`, `cik` o `product_id`;
- `identifier`: valor externo exacto;
- `capabilities`: operaciones disponibles, ordenadas y con namespace.

La identidad de una vinculación es `(provider, namespace, identifier)`. El catálogo rechaza IDs
canónicos e identidades externas duplicadas; una búsqueda inversa no puede elegir silenciosamente
entre dos activos. La ambigüedad entre alias también se informa de forma explícita.

Los CIK de SEC son texto para conservar los diez dígitos y sus ceros iniciales. Los IDs de producto
de Coinbase y símbolos de proveedores también se conservan exactamente, sin conversiones
destructivas.

Los criptoactivos deben declarar `crypto_profile`: `bitcoin`, `ethereum`, `altcoin`, `stablecoin` o
`wrapped`; la ausencia invalida el catálogo. Bitcoin, Ethereum y altcoins permanecen como perfiles
analíticos separados. Stablecoin y wrapped se traducen explícitamente a `unsupported` y no heredan
análisis de Bitcoin. El catálogo productivo conserva únicamente Bitcoin. Los otros perfiles se
prueban sintéticamente para fijar el límite sin anunciar ingestión productiva.

Las cotizaciones BVL usan vinculaciones `bvl/mnemonic`, `bvl/isin`, `smv/legal_name` y, cuando la
consulta de valores lo expone, `smv/security_code`. Este último tiene ocho caracteres y no se
presenta como ISIN. Los ISIN completos superan checksum ISO 6166 y se corroboran con documentos BVL.
`smv/legal_name` declara `is_unique: false`: una razón social identifica al emisor y puede
corresponder a varias clases o cotizaciones. Los identificadores de cotización conservan unicidad;
una resolución inversa no única falla explícitamente en vez de elegir un activo.
`equity:pe:bvl:bvn` y `equity:pe:bvl:scco` son distintos de `equity:us:bvn` y
`equity:us:scco`; la resolución exacta por proveedor y namespace no es ambigua. Los aliases
humanos de las cotizaciones peruanas incluyen el mercado para no cambiar el significado histórico
de `BVN` o `SCCO` sin contexto.

La configuración SEC deriva IDs de fuente distintos por ticker
(`sec-edgar:<ticker>:submissions` y `sec-edgar:<ticker>:companyfacts`). Apple conserva sin cambios
`sec-edgar:aapl:submissions` y `sec-edgar:aapl:companyfacts`; un futuro emisor no puede reutilizar
accidentalmente esos IDs. El transporte, snapshots raw, normalización de los cinco hechos
corporativos base, consulta point-in-time, cinco métricas y diagnóstico descriptivo reciben una
`SecAssetConfiguration` y aíslan activo, CIK, fuentes e identidades. Las versiones históricas de
Apple se conservan; los nuevos emisores usan versiones genéricas separadas cuando la versión forma
parte de la identidad.

La tendencia, la investigación ampliada de 40 métricas, su historial y las secciones analíticas
reciben el emisor configurado y conservan aislados activo, fuente, inputs y cachés. AAPL mantiene sus
contratos `aapl-fundamental-trend-v1`, `aapl-fundamental-research-v2`,
`aapl-fundamental-research-history-v2` y `aapl-fundamental-analysis-v1`; otro emisor usa
respectivamente las versiones genéricas v2, v3, v3 y v2.

Esto no aplica fundamentales corporativos a cada símbolo por defecto. Una acción queda habilitada
automáticamente cuando declara juntas las capacidades SEC de Submissions y Company Facts y sus
vinculaciones exactas de ticker, CIK y taxonomía contable superan la resolución tipada. La fachada read-only puede
consultar ese emisor por su `asset_id` y el refresh genérico puede ingerirlo con una sola conexión
writer, sin añadir otra lista de activos en el código. AMD declara ticker `AMD` y CIK `0000002488`,
verificados contra
[SEC EDGAR](https://www.sec.gov/Archives/edgar/data/2488/000000248826000018/0000002488-26-000018-index.htm).
Intel declara ticker `INTC` y CIK `0000050863`, contrastados con el índice oficial de compañías de
SEC. MSTR, MU y PLTR declaran de la misma forma sus CIK oficiales `0001050446`, `0000723125` y
`0001321655`. CDE, HYMC, MUX, NEM y SCCO declaran `0000215466`, `0001718405`, `0000314203`,
`0001164727` y `0001001838`. Tras validar filings reales e idempotencia, los diez emisores US-GAAP
declaran `has_fundamentals=true` y conservan `refresh_kind=market_only`: mercado y SEC se ejecutan
como writers independientes. Apple es el único activo con `complete_analysis`.

El perfil IFRS anual habilita Barrick (`B`, CIK `0000756894`), BVN ADR (CIK `0001013131`) y TSM
(CIK `0001046179`) mediante conceptos `ifrs-full` comparables y formularios 20-F/40-F. No interpreta
6-K como trimestre ni pasa IFRS por el normalizador US-GAAP. Cada descriptor publica
`fundamental_frequencies=["annual"]`, y el backend rechaza una frecuencia trimestral antes de leer
o escribir. Las identidades BVL continúan separadas: sus fundamentales SMV necesitan otro adaptador
de documentos, moneda y disponibilidad y no se envían al conector SEC.

## Consultas

Listar el catálogo empaquetado completo:

```bash
python scripts/list_assets.py
```

Filtrar por clase de activo o capacidad:

```bash
python scripts/list_assets.py --asset-type equity
python scripts/list_assets.py --capability market.daily_bars
```

Resolver un alias o ID canónico:

```bash
python scripts/resolve_asset.py --alias aapl
python scripts/resolve_asset.py --asset-id crypto:btc-usd
```

Consultar una vinculación específica:

```bash
python scripts/resolve_asset.py \
  --asset-id equity:us:aapl \
  --provider sec \
  --namespace cik
```

## Universo visible

La fachada genera `market-asset-universe-v3` directamente desde el catálogo y las configuraciones
tipadas de proveedores. Conserva los campos de v2 y añade `fundamental_frequencies`, derivado del
perfil contable configurado; los clientes que validaban literalmente v2 deben adoptar el nuevo
contrato. No cambia el formato del workspace.

La interfaz consulta `/api/market-assets` al iniciar y construye su selector; no conserva otra lista
de símbolos. Añadir un activo Alpaca con `market.daily_bars` permite reutilizar consulta,
actualización, gráfico, estadísticas y diagnóstico de mercado sin añadir una ruta HTTP por símbolo.

Apple conserva su actualización conjunta SEC + Alpaca. Los demás activos Alpaca usan un flujo
exclusivamente de mercado y, cuando declaran SEC, un refresh fundamental independiente por CLI o
interfaz. Sus descriptores exponen fundamentales sin convertir mercado y fundamentales en un
diagnóstico combinado. Bitcoin conserva sus fuentes diaria e intradía independientes.

## Familias de análisis

El perfil se deriva de `asset_class`, exchange y capacidades declaradas; no supone que todos los
activos pertenezcan a Estados Unidos:

- una acción pertenece a `listed_company`, usa mercado `listed_security` y fundamentales
  `corporate`;
- un ETF pertenece a `listed_fund`, comparte el contrato de mercado cotizado pero usa fundamentales
  `investment_fund`, no ratios empresariales aplicados por conveniencia;
- una criptomoneda pertenece a `cryptoasset`, usa mercado `crypto_spot` y futuros fundamentales
  `crypto_network`.

Una futura acción de BVL seguirá siendo `listed_company` aunque cotice en Perú y use PEN. Sus
identificadores BVL/SMV y capacidades de datos se declararán en vinculaciones propias. Esto permite
reutilizar el motor analítico corporativo cuando exista normalización compatible, sin hacer pasar
documentos peruanos por el conector SEC.

`market_data_configured` y `fundamental_data_configured` solo describen fuentes declaradas. No
afirman que un pipeline todavía inexistente esté activo. `has_fundamentals` se conserva como señal
de que la aplicación actual puede servir el análisis fundamental completo.

## Ampliación del catálogo

Para añadir un activo ya implementado, se edita el siguiente recurso JSON versionado con sus campos
`Asset`, alias únicos y vinculaciones de proveedor ordenadas. Cada capacidad usa un nombre
namespaced en minúsculas. Los IDs canónicos y las identidades externas deben ser globalmente
únicos. Los identificadores nuevos deben coincidir con las constantes y el comportamiento de la
implementación; las pruebas de compatibilidad deben importar esas constantes para detectar deriva
del catálogo.

Las configuraciones de activos listados exigen nombre, clase, moneda y bolsa explícitos. No existen
valores predeterminados de Apple/NASDAQ/USD que puedan contaminar una incorporación de BVL u otro
mercado.

El servicio carga el recurso empaquetado con `importlib.resources`, lo lee una vez por instancia y
construye índices inmutables en memoria. No accede a la red, DuckDB, un workspace ni variables de
entorno. Los pipelines resuelven el contexto mediante `ApplicationRuntime`; los clientes de
proveedor reciben únicamente identificadores ya validados.
