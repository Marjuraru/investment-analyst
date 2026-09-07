# Sistema de diseño de la interfaz local (`local-interface-design-system-v1`)

Este documento describe el sistema visual introducido por el Work Block
`UI-1` para la interfaz local estática servida por `local_web.py`. Cubre
tokens, rampa de ink, densidad, gramática de ausencia y reloj de sesión, y
declara explícitamente qué verifican y qué no verifican las pruebas que lo
acompañan.

El sistema es exclusivamente una capa de presentación. No añade capacidad,
no toca cálculo, storage, proveedores ni semántica point-in-time, y no
introduce puntuación agregada, veredicto combinado, ranking ni un total
que fusione mercado y fundamentales en ningún punto de la interfaz.

## Tokens

Todo color usado en `styles.css`, `index.html` o `app.js` se resuelve
desde una custom property declarada una sola vez en `tokens.css`, bajo
`:root` (tema claro) y otra vez bajo `:root[data-theme="dark"]` (tema
oscuro). Ningún otro archivo declara un literal de color (`#hex`,
`rgb()`, `hsl()`), con exactamente tres excepciones estrechas y
verificadas por prueba, que por restricción de la plataforma web no
pueden referenciar una custom property: el valor inicial de
`<meta name="theme-color">` (metadato de chrome del navegador,
sobrescrito por `applyTheme()` al cargar), el icono favicon/avatar
autocontenido en data-URI, y los tres valores por defecto de
`<input type="color">` de las SMA (que `app.js` sobrescribe de inmediato
con valores derivados de `designToken()` al cargar). Las tres están
fijadas a los valores de token que reflejan, y una prueba de contrato
verifica ese anclaje, no sólo la excepción.

Dos tokens tipográficos completan el sistema: `--font-sans` y
`--figure-font`, ambos pilas del sistema operativo (`ui-sans-serif` /
`ui-monospace` con sus respaldos). No se carga ninguna fuente web: sin
`@font-face`, sin `<link>` a un host de fuentes, sin binario vendorizado.
IBM Plex Sans/Mono, la familia nombrada en la propuesta visual aprobada,
queda diferida a un bloque posterior, explícitamente acotado, que
vendorice y versione los archivos binarios; este bloque ships con la pila
del sistema, que conserva densidad, contraste y cifras tabulares sin
capacidad nueva.

### Rampa de ink

Cuatro niveles, de más fuerte a más silencioso: `--ink-strong`, `--ink`,
`--muted-strong`, `--muted`. Cada nivel alcanza al menos 4,5:1 contra
`--surface`, `--surface-subtle` y `--canvas` en ambos temas, incluido el
nivel terciario (el más silencioso), que antes de este bloque no cumplía
el umbral en al menos una combinación tema/superficie nunca antes
ejercitada por una prueba.

### Tokens semánticos y de acento

`--accent`/`--accent-dark` llevan el énfasis interactivo; `--on-accent` y
`--on-focus` son tokens nuevos que garantizan 4,5:1 para texto renderizado
*sobre* `--accent`/`--accent-dark` y `--focus` respectivamente — los
valores existentes de accent/focus estaban calibrados para texto sobre
superficie, no como relleno de un botón o del skip-link, y reutilizarlos
directamente fallaba el contraste en tema oscuro (blanco sobre el
`--accent` del tema oscuro daba 2,43:1).

`--positive`, `--warning`, `--negative` y sus pares `-ink`/`-soft` no
cambian respecto de la paleta preexistente. `--blocked-ink`/
`--blocked-soft` son el único par semántico genuinamente nuevo, usado por
la marca `blocked` de la gramática de ausencia (ver abajo); los otros
cuatro estados de ausencia reutilizan tokens neutros/de acento/de
advertencia ya existentes, distinguidos por forma en vez de por un matiz
nuevo.

### Tokens del riel de navegación y del bloque de código

El riel de navegación y las superficies de reporte `<pre>`/`<code>` son
permanentemente oscuras sin importar el tema activo de la aplicación (se
renderizan sobre `--surface-dark`, que a su vez tiene un valor de tema
claro y uno de tema oscuro, ambos oscuros). Sus tokens de ink
(`--rail-ink-*`, `--code-ink`, `--code-border`) están por eso declarados
con el mismo valor en ambos bloques de tema — verificado contra el
`--surface-dark` propio de cada tema, no asumido.

### Colores de comparación y de las SMA

`--compare-series-1..5` y `--series-sma-5/20/50` son colores categóricos y
decorativos de gráfico. `app.js` nunca repite su valor hexadecimal: los
lee en tiempo de ejecución vía `getComputedStyle` (`designToken()` /
`COMPARISON_PALETTE`), de modo que el archivo de tokens sigue siendo la
única fuente incluso para colores consumidos por SVG dibujado desde
JavaScript.

## Densidad

Filas de tabla de 25 px con `padding:0 4px` y una regla de 1 px
(`--line`) como único separador; base tipográfica de 12 px; etiqueta de
9,5 px en versal con `letter-spacing:.09em`. Ver «Convergencia con el
lienzo (`UI-3`)» abajo para el origen de estos valores y por qué la
«retícula de 4 px» que proponía el sistema original **no** es lo que el
lienzo aprobado dibuja. Los tokens `--shadow`/`--shadow-accent` de la
versión anterior de este documento ya no existen: `UI-3` retira la
elevación decorativa por completo (ver esa misma sección).

## Gramática de ausencia

Cinco marcas reutilizables y mutuamente distinguibles —
`.absence-mark.<kind>` — para las cinco formas en que un valor puede
estar no disponible. Cada variante difiere por **tres** señales
independientes a la vez: un glifo de icono, un estilo de borde y un par
de color, de modo que ningún estado se codifica únicamente por color y
ningún par de estados colapsa entre sí:

| Kind | Rótulo | Icono | Borde | Par de color |
| --- | --- | --- | --- | --- |
| `missing` | Sin evidencia | `○` | dashed | `--muted-strong` / `--neutral-soft` |
| `not-evaluable` | No evaluable | `≈` | dotted | `--accent-dark` / `--accent-soft` |
| `not-applicable` | No aplica | `∅` | solid | `--muted` / `--surface-subtle` |
| `overdue` | Vencida | `▲` | solid | `--warning-ink` / `--warning-soft` |
| `blocked` | Bloqueada | `⊘` | double | `--blocked-ink` / `--blocked-soft` |

Ninguna de las cinco se representa jamás como `0`, una celda vacía o un
guion sin significado: cada una lleva su propio rótulo, y `blocked`/
`not-evaluable` llevan además una cadena de motivo declarado (ver abajo).

### Dónde la gramática ya está conectada a datos vivos

- **`missing`** — el corte `known_at` de la cabecera y el detalle
  colapsado de trazabilidad, antes de que exista una ejecución completa
  (`renderKnownAtCut(null)`).
- **`not-evaluable`** y **`blocked`** — toda métrica de valoración cuyo
  estado no es `evaluated`. El `reason_code` real de la métrica (ya
  producido por el pipeline de valoración, p. ej. `price_unavailable`,
  `fundamentals_unavailable`) decide la marca: `market_not_configured` y
  `fundamentals_not_configured` se renderizan como `blocked` con ese
  motivo como texto declarado; cualquier otro `reason_code` se renderiza
  como `not-evaluable`. Éste es exactamente el mecanismo que dispararía
  un activo del catálogo sin proveedor de mercado o de fundamentales
  configurado — la misma condición que produciría BVL (hoy sólo
  registrado, sin binding de mercado ni de fundamentales) si alguna vez
  fuera seleccionable desde el buscador de esta pantalla. **No lo es
  hoy**: `market_assets()` filtra el catálogo a únicamente los activos con
  capacidad de mercado diario, y ampliar ese filtro es un cambio en
  `application/market_universe.py` (catálogo/aplicación), fuera de la
  strict delta allowlist de este bloque. El mecanismo de gramática de
  ausencia que este bloque entrega es exactamente el que BVL ejercería;
  hacer que BVL mismo sea alcanzable desde esta pantalla queda
  explícitamente fuera de alcance aquí, y se deja anotado como trabajo de
  seguimiento para PLAN — no es algo que este bloque haya reinterpretado
  u omitido en silencio.
- **`not-applicable`** — una métrica de valoración cuyo estado es
  `not_applicable`, y la fila del propio benchmark en la tabla de
  comparación multiactivo (correlación/beta contra sí mismo).
- **`overdue`** — el flag `due` propio del scheduler, cuando una próxima
  ejecución ya pasó su hora programada.

Cualquier otro guion "—" preexistente en la interfaz (hay muchos, en
pantallas explícitamente fuera del alcance de este bloque) queda sin
cambios. Este bloque no afirma haber barrido cada guion de una interfaz
de 10 mil líneas; entrega la gramática, prueba sus reglas, y la conecta a
los casos vivos más claros y representativos dentro de las pantallas ya
en alcance.

## Corte `known_at`

`#known-at-cut-value`, en la cabecera persistente (compartida por todas
las vistas ruteadas, ya que esta interfaz es una SPA de secciones por
ancla bajo un único encabezado compartido), muestra el `known_at`
efectivo de la última ejecución completa, o la marca `missing` antes de
que exista una. El `#known-at-status` preexistente, sepultado dentro del
`<details>` colapsado de "Trazabilidad", se mantiene sincronizado por la
misma llamada a `renderKnownAtCut()`, de modo que ambas superficies nunca
divergen.

## Reloj de sesión

El reloj de sesión de NYSE reutiliza `NYSE_SESSION_STATES`,
`NYSE_CORE_OPEN_MINUTES` y `NYSE_CORE_CLOSE_MINUTES` exactamente como ya
existían en `app.js`; este bloque no reimplementa ni amplía esa lógica de
frontera. Añade tres cosas encima: un punto de estado que cambia de
**forma** (círculo relleno cuando está abierta, anillo hueco en el resto
de los casos) además de color; la hora de pared de Nueva York (que ya se
mostraba); y una cuenta regresiva hasta la próxima frontera
(`newYorkRegularSessionRemainingMinutes`), calculada a partir de las
mismas dos constantes. El reloj y su nota visualmente oculta declaran
ambos, en la propia interfaz, que sólo se modela la sesión regular de
lunes a viernes: sin calendario de feriados, sin cierre anticipado.

`UI-9` conserva esas tres lecturas e IDs, pero las reúne en una única franja del mismo renglón
del encabezado en escritorio. La franja no fuerza el ancho completo de la grilla ni lleva una regla
superior; en anchos estrechos puede envolver sin eliminar fecha, zona, estado, tiempo restante o la
nota accesible. Sigue siendo un cálculo local, sin petición de red ni calendario nuevo.

## Cifras

`body` ya aplicaba `font-variant-numeric: tabular-nums` globalmente; este
bloque añade la mitad que faltaba — una fuente de cifras monoespaciada
(`var(--figure-font)`) y alineación a la derecha — a toda superficie
numérica estructurada ya en alcance: la utilidad `.figure` (usada por el
valor de métrica de valoración y por el gráfico de comparación de
mercado), `.metric-value`, `.fundamental-research-metric-value`,
`.asset-price` y `.chart-table-scroll td`. El redondeo en todas ellas es
sólo de presentación: cada exportación JSON/CSV sigue serializando el
objeto de payload subyacente, nunca un valor reconstruido a partir de su
texto redondeado.

## Armazón de tableros (`UI-2`)

`UI-2` sustituye la página única acumulativa por un **armazón de seis
tableros**: `mesa`, `activo`, `tecnico`, `revisar`, `cazatiburones` y
`sistema`. Un único registro en `app.js` (`BOARD_REGISTRY`) declara los seis
`board_id` con su etiqueta y su estado construido/no construido; la
navegación, el enrutado (`activateBoard()`, `boardIdFromLocationHash()`) y la
gramática `not-built` se derivan exclusivamente de ese registro, nunca de una
segunda lista paralela.

Exactamente un tablero es visible a la vez, alternado con el atributo nativo
`hidden` -- nunca con la clase `.hidden` ni con `style.display`, que el reset
general de la superficie ya gobierna para todo lo demás. El tablero activo es
enlazable y restaurable desde el fragmento de la URL (`#activo`, `#revisar`,
…), y su enlace de navegación lleva `aria-current="page"`.

Toda sección y todo control ya entregado por bloques anteriores se **movió
intacto** a un tablero declarado: los once `id` de sección de la base
(`resumen`, `mercado`, `derivados-crypto`, `fundamentales`, `valoracion`,
`analisis`, `report-area`, `comparacion-mercado`, `operacion`,
`candidate-inbox-panel`, `alert-inbox-panel`) siguen presentes exactamente
una vez cada uno, y cada uno pertenece a exactamente un tablero. Los dos
paneles de bandeja (`candidate-inbox-panel`, `alert-inbox-panel`) dejaron de
ser `<details>` colapsados: son ahora el contenido directo del tablero
`revisar`, y su carga bajo demanda se disparó antes por el evento `toggle`
del panel y ahora se dispara al activarse ese tablero, exactamente una vez
por activación.

### Gramática `not-built`, aislada de la gramática de ausencia

`UI-2` declaró `cazatiburones` como el único tablero `not-built`, reservando
la lectura de la actividad institucional 13F y del corpus documental SEC,
ya transportados por el servidor, para cuando su interfaz se conectara.
`UI-4` conecta esa lectura y cierra la reserva: los seis tableros de
`BOARD_REGISTRY` son ahora `built: true` y `board-cazatiburones-not-built`
deja de existir en el marcado. `renderNotBuiltBoards()` y `.board-not-built`
no se eliminan -- siguen siendo genéricos sobre cualquier entrada futura con
`built: false`, exactamente como los declaró `UI-2` -- pero hoy no
renderizan nada porque cero tableros lo declaran.

`not-built` **no es una sexta marca de ausencia**: una marca de ausencia
(`missing`, `not-evaluable`, `not-applicable`, `overdue`, `blocked`)
describe un dato que falta bajo un corte `known_at` ya vigente; `not-built`
describe una capacidad de producto que todavía no existe, con su propia
clase CSS (`.board-not-built`) y su
propia función de render (`renderNotBuiltBoards()`), separadas por completo
de `.absence-mark` y de `renderAbsenceMark()`. Las cinco marcas de ausencia
declaradas por `UI-1` siguen siendo exactamente cinco.

### Camino de lectura conectado (`UI-4`)

`cazatiburones` presenta tres lecturas descriptivas independientes para el
activo seleccionado y el corte `known_at` global vigente, cada una servida
por un endpoint de solo lectura ya integrado por `SEC-CORPUS` (#159/#160) y
consumido sin modificar transporte ni contratos:

- **Actividad declarada** (`GET /api/v1/cazatiburones/declared-activity`) --
  `insider_features` y `beneficial_features` en dos contenedores separados
  (`#cazatiburones-insider-features`, `#cazatiburones-beneficial-features`),
  nunca combinados en una fila o total común.
- **Observaciones institucionales 13F**
  (`GET /api/v1/cazatiburones/institutional-observations`) -- filas as-filed
  por `manager_cik`/`report_id`/`cusip`, con `total_matching`, la página
  solicitada (`offset`/`limit`) y `truncated` expuestos como cobertura de
  página, nunca como total del dominio.
- **Línea temporal documental SEC** (`GET /api/v1/sec-document-timeline`) --
  entradas por revisión separadas en dos contenedores por familia
  (`#cazatiburones-timeline-asset-document`,
  `#cazatiburones-timeline-filer-document`), con `accession`,
  `is_amendment`, `available_at`, `content_sha256`, y `matched_count`,
  `returned_count`, `legacy_records_excluded`, `truncated` visibles.

Las tres peticiones son `GET` puras: el tablero no dispara refresh,
escritura, provider ni acceso al workspace permanente, y comparte
exclusivamente el corte de `#report-known-at` y `selectedMarketAsset` ya
declarados por bloques anteriores -- no expone un segundo selector de
activo ni un segundo control de corte.

Cuando el activo seleccionado no tiene presentación SEC corporativa
habilitada (`hasFundamentals && fundamentalMode === "corporate"`), el
tablero presenta una única marca `not-applicable`, nunca un error de red ni
una ausencia de datos: la pregunta "¿esto aplica a este activo?" es
distinta de "¿este dato existe bajo este corte?".

El estado de cada lectura se traduce a la gramática de ausencia ya
declarada, nunca a `0`, `—` o una celda vacía: un grupo de features, de
observaciones o de entradas de línea temporal vacío -- o
`state: "missing"` en la línea temporal -- se presenta como `missing`; un
`DescriptiveMetric` o un `comparison_status` en `not_evaluable` se presenta
como `not-evaluable`; y cualquier campo opcional ausente en un registro
individual (`declared_nature`, `security_title`, `table`, `event_date`,
`report_date`) se presenta como `missing`, nunca como un guion sin
significado.

### Carga por activación y estados (`UI-5`)

El armazón conserva una única matriz tablero→peticiones. `activateBoard()`
resuelve el tablero, fija su visibilidad con `hidden` y dispara sólo la fila
visible; una marca en memoria por sesión evita repetir una carga al volver al
mismo tablero. `loadMarketAssets()` y `loadAssetPreferences()` son cargas de
armazón compartidas y se resuelven antes de la primera carga diferida.

| Tablero | Peticiones diferidas | Estados que puede mostrar |
| --- | --- | --- |
| `mesa` | `GET /api/v1/overview`, `GET /api/v1/candidate-notifications`, `GET /api/v1/cazatiburones/notifications?family=institutional&limit=5`, `GET /api/v1/cazatiburones/notifications?family=activity&limit=5`, `GET /api/alerts?limit=5`, `GET /api/v1/universe-coverage` | vacío mientras no existe snapshot, cargando, ausente si no hay ejecución elegible, error operativo, snapshot disponible; las tres bandejas declaran total y no acusados, y las dos Cazatiburones distinguen outbox bloqueada, sin novedades, truncación o error; el universo muestra sin corte, cargando, error o matriz disponible |
| `activo` | `GET /api/listed-company-report`, `GET /api/market-chart` (o intradía), `GET /api/fundamental-trend`, `GET /api/fundamental-analysis` | vacío inicial, cargando por superficie, ausente según la gramática `missing`/`not-evaluable`, error vigente o evidencia disponible |
| `tecnico` | ninguna | vacío de armazón; sus cargas pertenecen a bloques posteriores |
| `revisar` | `GET /api/candidates?limit=50`, `GET /api/alerts?limit=50` | cargando, vacío sin elementos, error de bandeja o lista disponible |
| `cazatiburones` | `GET /api/v1/cazatiburones/declared-activity`, `GET /api/v1/cazatiburones/institutional-observations`, `GET /api/v1/sec-document-timeline` | cargando, `not-applicable` para un activo sin corpus corporativo, ausente bajo el corte, error vigente o lectura disponible |
| `sistema` | ninguna | vacío de armazón; la operación permanece bajo demanda |

El activo seleccionado y el único `known_at` global son parte de la identidad
de cada carga de `activo`. Cambiar cualquiera invalida las marcas de cargado y
vuelve a pedir sólo el tablero visible. Cada carga de `activo` comparte una
secuencia de activación y comprueba esa secuencia, el activo y el corte antes
de pintar o aplicar su estado de error; una respuesta superada se descarta en
silencio. La invalidación no crea persistencia entre recargas, un segundo
reloj, un segundo selector ni un corte por tablero.

### Mesa: tres columnas y matriz por dominio (`UI-9`)

El lienzo aprobado define una composición que aprovecha el rail global ya existente, sin duplicarlo
dentro de `mesa`:

1. **Columna principal y aside.** `#board-mesa` contiene una columna principal fluida con
   **Novedades de las bandejas** seguida de **Universo**, y un `aside` fijo de 340 px con
   **Cobertura del corte**, **Fuentes bloqueadas** e **Incidencias**, en ese orden. Antes de que la
   lectura pierda espacio, la composición colapsa a una columna; la matriz es el único elemento que
   puede desplazarse horizontalmente.
2. **Datos sin reordenar su significado.** Los IDs operativos de cobertura, programación,
   trazabilidad y candidatos permanecen en Cobertura/Fuentes bloqueadas; los de alertas e
   incidencias permanecen en Incidencias. El corte `known_at` y el reloj siguen compartidos en la
   cabecera. Sin score, ranking ni recomendación: la jerarquía sale de densidad, orden y
   tipografía, nunca de un número héroe.
3. **Seis tableros, no nueve.** `UI-9` no crea, renombra ni elimina ningún tablero;
   `BOARD_REGISTRY` conserva sus seis entradas.

#### Novedades: tres familias con caminos de lectura separados

La capa lleva el título visible **Novedades de las bandejas**: describe el contenido de las
bandejas locales sin afirmar que esté filtrado por el corte global.

La capa "qué cambió" separa tres bloques con procedencia y rótulo propios -- institucional 13F,
actividad declarada y reglas analíticas -- sin conteo combinado ni orden entre ellos. Verificado
sobre el código antes de escribir una sola línea de este bloque:

- **Reglas analíticas** usa `GET /api/v1/candidate-notifications`; **institucional 13F** y
  **actividad declarada** usan, respectivamente, `GET /api/v1/cazatiburones/notifications?family=institutional&limit=5`
  y `GET /api/v1/cazatiburones/notifications?family=activity&limit=5`. Cada familia tiene contador,
  lista y secuencia de petición propios: no se fusionan, ordenan ni suman entre sí, y ninguna lectura
  es por activo.
- Las tres familias expresan su bandeja con `total` y `pending_count`, no como novedades «desde el
  corte anterior». Las dos bandejas Cazatiburones muestran `blocked` sólo si la outbox no está
  configurada; si está habilitada y `total` es cero dicen «sin novedades», y si `truncated` es
  verdadero muestran `returned` de `total`. Esas representaciones y un error de petición son
  distintas. Ninguna de las tres bandejas está acotada por el `known_at` global, y la capa lo
  declara de forma visible.
- `GET /api/alerts` no es una cuarta familia de novedades: sirve `OperationalRuleId`
  (`operation.job_failed`, `job_interrupted`, `job_skipped`, `job_coverage_incomplete`), es decir,
  incidencias del scheduler. Pertenece a **Incidencias** (`#mesa-incidents-list`), no a
  "Novedades", y su renderizado en `mesa` es de solo lectura -- sin los botones de transición que
  sí tiene la bandeja interactiva de `revisar`.

`loadMesaAnalyticalNews`, `loadMesaCazatiburonesNewsFamilies` y `loadMesaIncidents` no llevan
`known_at` como parámetro de consulta -- estas cargas devuelven estado local ya materializado, no una proyección point-in-time del
corte -- así que `invalidateDeferredBoardLoads()` no necesita descartarlos por cambio de corte;
`loadedBoardIds.clear()` ya fuerza su recarga la próxima vez que `mesa` se activa.

#### Universo: tres dominios, última evidencia y cinco marcas compactas

`loadMesaUniverseCoverage` consulta `GET /api/v1/universe-coverage` exactamente una vez por
activación, nunca por activo. La ventana de cuatro fechas es una regla única, determinista y
visible junto a la matriz (`#mesa-universe-window`), derivada exclusivamente del corte global
(`#report-known-at`), nunca de un control que el analista no ve:

- `market_end`/`fundamental_end` = el último día UTC completamente transcurrido al corte (la
  fecha del corte menos un día calendario -- el propio corte nunca cae dentro de su día en curso).
- `market_start`/`fundamental_start` = 365 días antes de ese fin.
- `frequency` = `annual`.

La matriz tiene una fila por activo devuelto y exactamente seis columnas: `Activo`, `Dominio`,
`Mercado`, `Fund.`, `Valor.`, `Última evidencia`. Sus únicas celdas de estado son las tres
capacidades analíticas que la vista consulta: mercado, fundamentales y valoración corporativa.
`Dominio` usa sólo el mapa exhaustivo de presentación de `asset_class` (`equity`, `etf`, `crypto`),
nunca ticker, exchange o disponibilidad. Cada celda resuelve sin ambigüedad, mapeando
`capability`/`evidence`/edad a la gramática compacta:

| `capability` | `evidence` | edad vs. ventana (365 días) | Marca |
| --- | --- | --- | --- |
| `not_applicable` | (cualquiera) | (cualquiera) | `not-applicable` ("No aplica") |
| `not_configured` / `not_implemented` | (cualquiera) | (cualquiera) | `blocked` ("Bloqueada") |
| `supported` | `missing` | (cualquiera) | `missing` ("Sin evidencia") |
| `supported` | `not_queried` | (cualquiera) | `missing` ("Sin evidencia") |
| `supported` | `present` | ≤ 365 días (`reference_age_days`, o `latest_input_age_days` si el primero es nulo) | `fresh` -- "Al día" |
| `supported` | `present` | > 365 días, o edad desconocida | `overdue` ("Vencida") |

`capability` se evalúa antes de `evidence`, y evidencia antes de edad: `not_queried` es "Sin
evidencia" sólo para una capacidad soportada y los estados de capacidad conservan su prioridad. La
gramática específica de la matriz usa exactamente `fresh`, `overdue`, `missing`, `blocked` y
`not-applicable`; cada marca mide 7 px y combina forma, borde o trama y relleno, conserva nombre
accesible y no repite el rótulo visible en cada celda. Una sola leyenda visible declara "Al día",
"Vencida", "Sin evidencia", "Bloqueada" y "No aplica". No se modifica la gramática global de
cinco `.absence-mark`.

`Última evidencia` toma el máximo válido de `reference_at` y `latest_input_available_at` de
mercado, fundamentales y valoración corporativa; se formatea con la utilidad temporal existente.
Sin una de esas fechas usa ausencia accesible. Nunca toma `computed_at`, reloj de render ni
`bvl_registry`.

`Registro BVL` no es una celda ni dominio analítico. La única lectura visible está en Fuentes
bloqueadas y deriva de `asset.bvl_registry` de esta misma respuesta: enumera aplicables, evidencia
presente, evidencia ausente, no consultada, sin configurar, no implementada y no aplica. No añade
una petición, no colapsa esos estados y no afirma mercado/fundamentales BVL ni score.

Cazatiburones, Documentos y Derivados por activo no son columnas de esta matriz: el contrato
integrado los devuelve en `additional_capabilities_not_queried`, y `mesa` los enumera como texto
declarado (`#mesa-universe-not-queried`), nunca como columna vacía ni como petición adicional. Las
`limitations` por activo se acumulan íntegras, sin normalizar a un booleano, bajo la matriz
(`#mesa-universe-limitations`), incluso en ausencia o error (`colSpan=6`).

#### Traslado del panel de preferencias

`asset-preferences-panel` se traslada íntegro -- controles, formulario y comportamiento -- del
tablero `mesa` al tablero `sistema`, junto al resto de la operación. La Mesa se consulta; no se
configura desde ella. Ningún endpoint, parámetro ni contrato cambia: `update_asset_preferences` y
`/api/v1/asset-preferences` son exactamente los mismos que antes de este bloque.

### Marco global y alcance de activo (`UI-8`)

`UI-8` fija una composición única para las seis vistas sin crear un séptimo tablero, sin mover sus
secciones de datos y sin cambiar ningún endpoint. La cabecera persistente conserva sólo la identidad
de la aplicación, el corte global `known_at`, el reloj de mercado, salud, tema y verificación. La
identidad y el selector del activo viven en una única barra de alcance: aparece únicamente en
`activo`, `tecnico` y `cazatiburones`; no se filtra hacia `mesa`, `revisar` ni `sistema`. El riel
lateral contiene exclusivamente `#board-nav`.

La composición queda regida por estas siete reglas verificables:

1. No hay número héroe: la lectura nunca se reduce a un score, ranking o veredicto.
2. Mercado, fundamentales, valoración y Cazatiburones conservan dominios paralelos, separados por
   reglas verticales y pies de procedencia cuando comparten una vista.
3. El corte global visible es el único control permanente de tiempo; una vista no inventa un corte
   adicional ni lo oculta.
4. La densidad usa la retícula de 4 px donde la composición la permite, filas de 25 px, reglas de
   1 px y aire entre bloques; no introduce tarjetas, sombras ni elevación decorativa.
5. No carga fuentes web; las tablas largas se virtualizan cuando corresponda y los paneles diferidos
   conservan el objetivo de p95 menor de 100 ms y menos de 20 KiB para su lectura inicial.
6. Estado, forma, relleno y rótulo se combinan para accesibilidad, con contraste AA, foco visible y
   navegación completa por teclado.
7. Claro y oscuro son estados de primera clase; el tema oscuro mantiene el grafito cálido de la
   rampa existente, no una variante azulada o una inversión automática.

La barra de alcance reúne identidad, cotización, clasificación y combobox conservando sus IDs y
actualizaciones operativas. Dentro de `activo` añade el único `tablist` de subpestañas, en este orden:
Mercado, Derivados, Fundamentales, Valoración y Análisis. Son `button[type=button]`, no anclas: no
modifican el fragmento de URL ni vuelven a activar un tablero. Cada una conserva su atributo de
capacidad; exactamente una queda seleccionada y exactamente su sección asociada queda visible con
`hidden`. Si la capacidad del activo oculta la selección vigente, el retorno determinista es Mercado.
Valoración conserva su carga diferida en la primera activación, no en cada cambio entre pestañas.

La estructura por vista queda explícita: `mesa` mantiene cabecera y un riel; su destino de tres
columnas y el retiro de la columna BVL de la matriz pertenecen a `UI-9`. `activo` entrega en este
bloque la barra y sus subpestañas. `tecnico` comparte sólo la barra de alcance; su búsqueda específica
queda para `UI-10`. `revisar` conserva su disposición actual y queda para `UI-10`. `cazatiburones`
comparte la barra de alcance pero sus filtros pertenecen a `UI-11`. Esas entregas futuras no alteran
el hecho de que `SEC-CORPUS` sigue siendo la única ruta `NEXT`.

### Rejilla y densidad del lienzo, ahora en tokens

`--canvas-gutter`, `--canvas-row-gap`, `--canvas-block-gap` y
`--canvas-density` son los primeros tokens no cromáticos de este sistema:
igual que `--sidebar-width`, son valores de layout theme-invariantes,
declarados con el mismo valor en `:root` y en `:root[data-theme="dark"]` bajo
la misma disciplina de paridad que todo color de este documento.

### Deuda declarada

La división de JavaScript y CSS por componentes (punto 5.4 de
`docs/basic_functional_release_plan.md`) queda explícitamente fuera de este
bloque: el registro de tableros crea la costura que la abaratará, pero
hacerla aquí habría duplicado el tamaño de un diff que ya mueve toda la
superficie.

## Convergencia con el lienzo (`UI-3`)

`UI-1` entregó el contrato del sistema visual y `UI-2` el armazón de seis
tableros; ninguno de los dos cambió el lenguaje visual del interior de las
secciones. `UI-3` sustituye ese lenguaje por el del lienzo aprobado, sin
mover una sola sección y sin tocar el servidor: es una migración de
presentación, verificable por contrato estático.

### Regla mecánica: R ≥ G ≥ B

Todo token de superficie, ink, regla y acento declarado en `tokens.css`
cumple **R ≥ G ≥ B** sobre sus propios canales RGB, en ambos temas, sin una
sola excepción silenciosa. Dos familias quedan explícitamente exentas, por
nombre, nunca por omisión:

- **Los cuatro estados semánticos** — `--positive`, `--warning`,
  `--negative`, `--blocked-ink` y sus pares `-soft`/`-ink`. Su color
  significa dirección o estado, no temperatura de marca, y el lienzo
  tampoco los hace cálidos.
- **La familia categórica de gráfico** — `--series-sma-5/20/50`,
  `--series-revenue`, `--series-net-income` y `--compare-series-1..5`.
  Estos colores existen para permanecer **mutuamente distinguibles** entre
  sí sobre el mismo gráfico: colapsar cinco series de comparación o tres
  ventanas SMA en tonos de un solo matiz destruiría exactamente la
  legibilidad para la que existen. `--series-close` **no** está exenta: es
  la única línea de precio primaria, así que lleva el mismo lenguaje de
  acento cálido que cualquier otro acento singular de la interfaz.

### Contraste conservado, con la desviación de `ink4` declarada

Los cuatro niveles de la rampa de ink (`--ink-strong`, `--ink`,
`--muted-strong`, `--muted`) siguen alcanzando 4,5:1 contra `--surface`,
`--surface-subtle` y `--canvas` en ambos temas — el contrato ya integrado
por `UI-1` pasa **sin modificarse**. El cuarto nivel (`--muted`, el
equivalente de «ink4» del lienzo) **no** adopta el literal del lienzo:
calculado con luminancia relativa WCAG, ese literal falla 4,5:1 contra las
tres superficies en ambos temas (3,71/4,08/4,26 en claro; 3,86/4,13/4,32 en
oscuro). Este bloque conserva su matiz y ajusta la luminancia hasta cruzar
el umbral: `#6d685e` en tema claro mide 5,31/5,54/4,83 contra
canvas/surface/surface-subtle, y `#8a8275` en tema oscuro mide 4,89/4,57
contra canvas/surface. El contrato de contraste gana sobre el literal del
lienzo, y la desviación queda documentada con sus números, no silenciada.

### Gramática de superficie: reglas, no elevación

`box-shadow` desaparece de toda superficie de datos y de panel. Sobreviven
únicamente dos declaraciones, ambas ya integradas por bloques anteriores y
ninguna decorativa: el realce interior del logotipo del rail
(`inset 0 1px var(--rail-overlay-strong)`) y la barra del tablero activo en
la navegación (`inset 3px 0 var(--rail-active-bar)`). Una tercera categoría
—el anillo de foco— no tiene ninguna instancia viva porque el foco ya se
expresa con `outline`, una propiedad distinta que la prohibición de
`box-shadow` no alcanza.

`border-radius` desaparece de paneles, tarjetas, tablas, insignias y filas.
Sobreviven sólo dos categorías, ambas nombradas: los puntos circulares de
estado o viñeta (`.status-dot`, `.session-status-dot`, la viñeta de
`.badge`, la viñeta de `.limitations-list` y el indicador de serie
`.legend-swatch`) y los controles de formulario nativos (`<input>` de
texto, número y color; `<select>`). El conteo total —2 `box-shadow`, 13
`border-radius`— es una regla de contrato ejecutable, no una prosa
descriptiva: cualquier reintroducción de elevación o esquina redondeada
fuera de esas listas lo hace fallar.

### Densidad de fila como tokens

`--row-height` (25px), `--row-padding-inline` (4px), `--row-rule-width`
(1px), `--base-font-size` (12px), `--label-font-size` (9,5px) y
`--label-tracking` (.09em) son los valores reales de fila del lienzo,
declarados una vez y consumidos por las filas de datos de
`.chart-table-scroll` (mercado y fundamentales) y `.valuation-history-table`.
La «retícula de 4 px» que proponía la versión original del sistema **no**
es lo que el lienzo dibuja: sus separaciones reales son 8, 9, 10, 13, 18,
20, 26 y 30 px, no todas múltiplos de 4. Este documento declara sólo lo que
el lienzo hace de verdad —altura de fila, separador de 1 px, padding
horizontal de fila, base tipográfica— y no inventa una retícula que el
propio lienzo incumple. La aplicación de estos tokens a cada patrón de
lista o de tarjeta de la interfaz (bandejas, preferencias, reglas de
screening) queda fuera de este bloque: la superficie donde ya existen
`<table>` reales con filas de datos literales es la evidencia mínima
suficiente y verificable de esta sección; retroadaptar cada patrón visual
de lista es trabajo de seguimiento, no silenciado, sólo no ejecutado aquí.

### Rail y ancho lateral

`--sidebar-width` pasa de 212px a los 158px del lienzo. El punto de ruptura
intermedio que antes lo reducía a 190px en pantallas medianas quedó
retirado: era una reducción relativa al valor anterior, y con el nuevo
valor base ya menor habría sido un **aumento** incoherente.

### Tipografía: IBM Plex sigue diferida

El lienzo carga IBM Plex Sans/Mono desde `fonts.googleapis.com`. La regla
`test_no_external_network_reference_in_static_surface`, integrada por
`UI-1`, lo prohíbe. Este bloque adopta únicamente las **métricas** de
densidad del lienzo sobre la pila tipográfica del sistema ya declarada
(`--font-sans`/`--figure-font`); vendorizar y versionar los binarios de IBM
Plex sigue siendo un bloque posterior y acotado, tal como ya declaró
`UI-1`.

## Qué no son estas pruebas

`tests/unit/frontend/test_design_system.py` es una suite de contrato
estática sobre el texto de `.css`/`.html`/`.js` ya shippeado. Verifica
paridad de tokens, ausencia de literales de color fuera de `tokens.css`,
contraste WCAG calculado para los pares que lista este documento, la
distinción por forma/color/icono de la gramática de ausencia, ausencia de
cualquier referencia de red externa, y la presencia de las reglas de
cifras tabulares/monoespaciadas/alineadas a la derecha. **No** levanta un
navegador, no renderiza una página, y no puede ver el layout computado
real, el pintado ni una captura de pantalla real en ningún tema. No es
regresión visual, y no puede detectar una regresión de maquetación que
mantenga verdadera cada regla anterior mientras se ve mal. Una suite real
de regresión visual necesitaría un navegador y dependencias nuevas, y es
su propio Work Block, autorizado por separado.
