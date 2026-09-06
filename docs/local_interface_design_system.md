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

`cazatiburones` es el único tablero `not-built` de este bloque: reserva la
lectura de la actividad institucional 13F y del corpus documental SEC, ya
transportados por el servidor, para cuando `UI-3` conecte su interfaz.
`not-built` **no es una sexta marca de ausencia**: una marca de ausencia
(`missing`, `not-evaluable`, `not-applicable`, `overdue`, `blocked`)
describe un dato que falta bajo un corte `known_at` ya vigente; `not-built`
describe una capacidad de producto que todavía no existe, con su propia
clase CSS (`.board-not-built`) y su propia función de render
(`renderNotBuiltBoards()`), separadas por completo de `.absence-mark` y de
`renderAbsenceMark()`. Las cinco marcas de ausencia declaradas por `UI-1`
siguen siendo exactamente cinco.

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
