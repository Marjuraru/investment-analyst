# Catálogo versionado de activos

El catálogo separa el ID canónico estable de cada activo de los identificadores asignados por
proveedores externos. Un ticker es útil para mostrar, pero no constituye una identidad global
segura: el mismo texto puede reutilizarse entre mercados, proveedores o clases de activo. Los IDs
canónicos ya utilizados por el repositorio permanecen sin cambios.

El documento empaquetado `default_assets.v1.json` es estático, versionado y determinista. Contiene
Bitcoin, Apple y la lista inicial de acciones y ETF estadounidenses comprobada con el nivel gratuito
de Alpaca IEX: AMD, Barrick (`B`), BVN, CDE, HYMC, INTC, MSTR, MU, MUX, NEM, PLTR, SCCO, TSM,
GBTC, GLD e IBIT. No contiene credenciales, observaciones de mercado, parámetros de algoritmos,
recomendaciones ni valores que cambien con el tiempo.

## Modelo

`CatalogAsset` amplía el contrato `Asset` del núcleo con alias y vinculaciones de proveedor
explícitas. Los alias no distinguen mayúsculas y minúsculas, y no sustituyen el ID canónico. Cada
vinculación contiene:

- `provider`: a lower-case provider slug such as `alpaca`, `sec`, or `coinbase`;
- `namespace`: the provider's identifier kind, such as `symbol`, `cik`, or `product_id`;
- `identifier`: the exact external value;
- `capabilities`: sorted namespaced operations currently supported through that binding.

A binding identity is `(provider, namespace, identifier)`. The catalog rejects duplicate
canonical IDs and duplicate external identities. Reverse lookup therefore cannot silently choose
between two assets. Alias ambiguity is also reported explicitly.

SEC CIK values are strings. This preserves Apple's ten-digit CIK, including leading zeroes.
Coinbase product IDs and provider symbols are likewise preserved exactly instead of being
converted or normalized destructively.

## Consultas

List the complete packaged catalog:

```bash
python scripts/list_assets.py
```

Filter by asset class or capability:

```bash
python scripts/list_assets.py --asset-type equity
python scripts/list_assets.py --capability market.daily_bars
```

Resolve an alias or canonical ID:

```bash
python scripts/resolve_asset.py --alias aapl
python scripts/resolve_asset.py --asset-id crypto:btc-usd
```

Return a specific provider binding:

```bash
python scripts/resolve_asset.py \
  --asset-id equity:us:aapl \
  --provider sec \
  --namespace cik
```

## Universo visible

La fachada genera `market-asset-universe-v1` directamente desde el catálogo y las configuraciones
tipadas de proveedores. La interfaz consulta `/api/market-assets` al iniciar y construye su selector;
no conserva otra lista de símbolos. Añadir un activo Alpaca con `market.daily_bars` permite
reutilizar consulta, actualización, gráfico, estadísticas y diagnóstico de mercado sin añadir una
ruta HTTP por símbolo.

Apple conserva su actualización conjunta SEC + Alpaca. Los demás activos Alpaca usan un flujo
exclusivamente de mercado. Bitcoin conserva sus fuentes diaria e intradía independientes.

## Ampliación del catálogo

Para añadir un activo ya implementado, se edita el siguiente recurso JSON versionado con sus campos
`Asset`, alias únicos y vinculaciones de proveedor ordenadas. Cada capacidad usa un nombre
namespaced en minúsculas. Los IDs canónicos y las identidades externas deben ser globalmente
únicos. Los identificadores nuevos deben coincidir con las constantes y el comportamiento de la
implementación; las pruebas de compatibilidad deben importar esas constantes para detectar deriva
del catálogo.

El servicio carga el recurso empaquetado con `importlib.resources`, lo lee una vez por instancia y
construye índices inmutables en memoria. No accede a la red, DuckDB, un workspace ni variables de
entorno. Los pipelines resuelven el contexto mediante `ApplicationRuntime`; los clientes de
proveedor reciben únicamente identificadores ya validados.
