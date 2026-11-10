# Screening automático y alertas de candidatos

## Objetivo

El monitor evaluará en segundo plano condiciones de mercado, fundamentales, valoración, eventos y
operación. Cuando evidencia nueva cumpla una regla configurada, creará un candidato para revisión y
podrá notificar al usuario.

Una alerta no afirmará que un activo es una “buena inversión”, no recomendará comprar o vender y no
mezclará diagnósticos mediante una puntuación arbitraria. Su mensaje será equivalente a:

> Este activo cumple las condiciones configuradas para revisión. Comprueba la evidencia, cobertura
> y limitaciones antes de tomar una decisión.

## Decisión de implementación

El primer monitor numérico ya está integrado después del scheduler multi-activo. No depende de la
IA: las condiciones numéricas son más baratas, auditables y reproducibles mediante un motor
determinista.

La IA será un enriquecimiento opcional posterior. Podrá explicar documentos y contexto únicamente
después de una activación determinista o una acción explícita.

## Separación de responsabilidades

La secuencia prevista es:

1. actualizar una fuente;
2. persistir y validar evidencia;
3. publicar el conjunto de activos y dominios afectados;
4. evaluar únicamente reglas compatibles con esa evidencia nueva;
5. persistir el resultado de screening;
6. deduplicar y aplicar confirmación, histéresis y cooldown;
7. crear un evento de alerta;
8. enviar por los canales habilitados;
9. generar, si se solicitó, un resumen cualitativo separado.

El monitor no dibuja gráficos, no carga el frontend y no vuelve a consultar proveedores para
evaluar datos ya persistidos.

## Escalabilidad

El diseño se organiza por capacidades, no por símbolos:

- una regla declara familias y dominios compatibles;
- el catálogo resuelve activos y fuentes;
- el scheduler informa qué evidencia cambió;
- el motor evalúa en lote los activos afectados;
- los canales de notificación consumen una outbox independiente;
- añadir un proveedor o activo no crea otra ruta de alerta.

Los dominios permanecen separados:

- mercado cotizado;
- mercado cripto;
- fundamentales corporativos;
- fundamentales de fondos;
- fundamentales de red;
- macro;
- noticias y eventos;
- Cazatiburones;
- operación y calidad.

Una regla puede exigir condiciones de más de un dominio, pero cada resultado conserva las
evaluaciones separadas. No se calcula un score o diagnóstico combinado.

## Contratos conceptuales

Los contratos implementados usan un estado adicional
`state/analytical_screening_state_v1.json`. Es append-only para resultados, recibos y transiciones;
los eventos conservan su estado proyectado y nunca modifican el almacenamiento financiero.

La entrega local usa además `state/candidate_notification_outbox_state_v1.json`. Al reconciliar un
intento ya observado, cada candidato en estado `new` crea una recepción inmutable con identidad
UUID5 derivada del candidato. La outbox se reabre tras reinicio y no retroactúa sobre candidatos
vistos, descartados, silenciados o resueltos. El acuse explícito del usuario es una transición
append-only propia: no cambia el ciclo de vida del candidato ni vuelve a evaluar reglas, consultar
proveedores o abrir un canal externo.

La configuración usa `state/analytical_rule_registry_state_v1.json`. Cada cambio guarda una
revisión completa, encadenada por fingerprint y con escritura atómica privada. El catálogo
empaquetado continúa siendo la fuente de contratos inmutables —métrica, operador, algoritmo,
unidad, parámetros y calidad—; la interfaz solo permite modificar estado, umbrales,
confirmaciones y cooldown.

### Canal local Windows (desplegado, deshabilitado por defecto)

`ALERTS-DELIVERY` integra un único canal local de transporte, literal `windows_toast`, con
contratos estrictos `desktop-notification-config-v1`, `desktop-notification-delivery-v1` y
`desktop-notification-status-v1`. Vive en `state/desktop_notifications/` y no modifica candidatos,
reglas, outbox, métricas ni almacenamiento financiero.

`ALERTS-OPERATIONAL-ADOPTION` desplegó ese código integrado: el release `200db2d6` está activo desde
el 2026-10-31 y una única prueba `PRUEBA — sin señal de inversión` fue aceptada por Windows desde el
proceso del servicio (`accepted=true`, 1022 ms de ida y vuelta HTTP). La capacidad quedó verificada y
el canal se restituyó a `enabled=false` en la misma ventana; no se observó lectura humana ni se
entregó un candidato financiero. Es evidencia de transporte local: no acredita latencia estadística,
cobertura universal ni precisión predictiva.

- **Identidad.** `delivery_id` es UUID5 determinista sobre namespace constante, `workspace_uuid`,
  `notification_id`, canal y versión. `tag` y `group` de Windows son las dos mitades hexadecimales
  de esa identidad (16 caracteres cada una) y se conservan en todos los reintentos.
- **Activación.** El canal empieza deshabilitado. Habilitarlo registra la frontera `enabled_at` y
  una identidad propia `Marjuraru.InvestmentAnalyst` mediante un acceso directo del usuario; no
  encola el historial anterior. Deshabilitar cancela pendientes de esa habilitación sin borrar
  evidencia; reactivar fija una frontera nueva.
- **Elegibilidad.** Antes de admitir y de enviar se exige candidato `new`, sin acuse, creado
  después de la activación, regla `active` tanto en la evidencia de activación como en la
  configuración vigente y fingerprint compatible. Silencio, pausa, resolución, descarte o cambio
  incompatible cancelan con causa cerrada.
- **Reintentos.** Persistir la intención precede a cualquier subprocess y el resultado se persiste
  después. Un error probado antes del envío admite hasta tres intentos con esperas de 60 y 300
  segundos, activadas por el siguiente tick elegible, sin bucle de espera ni proceso permanente.
  Timeout, muerte tras invocar o resultado ilegible son `unknown` y **no se reenvían
  automáticamente**. La aceptación del sistema operativo no demuestra visualización ni lectura.
- **Lote y límites.** Como máximo cinco entregas y un proceso PowerShell por lote, con ritmo
  máximo de un lote por minuto. El puente usa un ejecutable fijo de Windows PowerShell y un script
  fijo del release, sin aceptar rutas ni comandos por HTTP; los datos viajan por stdin JSON UTF-8 y
  el XML se construye con escape, nunca evaluando títulos o cuerpos como código. No hay URLs
  remotas ni imágenes; la apertura apunta sólo a `http://127.0.0.1:8765`. Timeout total de 10
  segundos y salida máxima de 64 KiB por lote.
- **Presupuesto.** El directorio completo se limita a 8 MiB y 10 000 entregas retenidas. Al
  alcanzarlo se pausan admisión y lotes, el estado `budget_exceeded` queda visible y la bandeja se
  conserva; no se borra historia ni se elevan límites automáticamente.
- **Lectura.** `GET /api/v1/desktop-notifications` sólo lee estado y una página estable de entregas
  (`offset`/`limit`, 50 por defecto, 100 máximo). No ejecuta sondeos, no registra la identidad del
  sistema operativo y no escribe. Las acciones `configure` y `test` son POST explícitos; la prueba
  se limita a una por minuto y se rotula «PRUEBA — sin señal de inversión», sin crear un candidato
  financiero ficticio.

### Regla de screening

- `rule_id` estable y `rule_version`;
- nombre y explicación en español;
- estado borrador, silencioso, activo o pausado;
- familia de activo y universo;
- frecuencia y evento que dispara la evaluación;
- condiciones;
- cobertura mínima;
- frescura máxima;
- número de confirmaciones;
- histéresis de entrada y salida;
- cooldown;
- canales habilitados;
- fecha de creación y versión de configuración.

### Condición

- dominio y métrica;
- definición y versión de algoritmo esperadas;
- operador;
- umbral como `Decimal`, fecha, texto o estado tipado;
- unidad;
- frecuencia y horizonte;
- tratamiento explícito de ausencia;
- evidencia requerida;
- aplicabilidad por clase, mercado, sector o moneda.

### Resultado de condición

Cada condición es trivaluada:

- `met`: cumple;
- `not_met`: no cumple;
- `not_evaluable`: no existe evidencia suficiente o compatible.

El resultado conserva valor, umbral, período, `available_at`, `known_at`, frescura, limitación e
identidades de inputs. Un dato ausente no se convierte en cero ni en incumplimiento.

### Resultado de screening

- regla y versión;
- activo;
- corte efectivo;
- condiciones cumplidas, incumplidas y no evaluables;
- cobertura;
- evidencia exacta;
- estado activado o no activado;
- explicación determinista;
- identidad reproducible;
- timestamp de cálculo separado de disponibilidad.

### Resultados posteriores descriptivos

`descriptive-condition-outcomes-v1` es una lectura efímera, determinista y de solo lectura para las dos
reglas incrementales `market.technical.rsi-incremental-low-review` y
`market.technical.macd-incremental-positive-histogram-review`. No es un backtest de estrategia, no
persiste resultados y no crea señales, candidatos, notificaciones ni recomendaciones.

- **Entrada.** `asset_id`, `rule_id`, `expected_rule_fingerprint`, `start_date` y `end_date`
  inclusivos y `known_at` UTC. El rango admite como máximo 90 días calendario y exige
  `end_date 00:00 ≤ known_at < (end_date + 91 días) 00:00`.
- **Condición.** Para cada período diario del índice se elige la primera revisión compatible
  observada: contrato exacto de métrica, algoritmo, unidad, parámetros y calidad admitida, con
  `available_at ≥ as_of` y `available_at ≤ known_at`. Un empate semánticamente distinto es
  `ambiguous_metric`; una fila inválida es `invalid_metric`; la ausencia es `no_compatible_metric`.
  La condición se evalúa retrospectivamente con la configuración actual congelada por fingerprint; no
  se afirma que esa política existiera en cada fecha histórica.
- **Horizonte.** `forward_observed_close_intervals_5-v1` elige las primeras seis barras diarias
  publicadas cuyo `period_start ≥ eval_at`, `period_end ≤ known_at` y
  `period_end ≤ eval_at + 30 días`, con el mismo activo, fuente, frecuencia y moneda. El resultado
  `R = C5/C0 − 1` usa `Decimal` de 34 dígitos y `ROUND_HALF_EVEN`. Son cinco intervalos entre seis
  cierres observados; no son cinco días naturales ni cinco sesiones garantizadas. Los huecos,
  fines de semana y ausencias del proveedor se conservan y se declaran. Las barras Alpaca IEX no
  exponen `period_start`/`period_end` nativos: la proyección usa `observed_at` como inicio y un
  intervalo declarado de 24 h como fin.
- **Estados.** `available`, `pending_window` (faltan barras y `known_at < eval_at + 30 días`),
  `insufficient_observed_bars` (la ventana ya terminó), `ambiguous_close`, `invalid_close` y
  `condition_not_evaluable`. Una barra ambigua o inválida no se salta para buscar otra. La falta de
  barras no demuestra que el mercado no ocurrió.
- **Grupos.** `matched`, `not_matched` y `not_evaluable` particionan exactamente los períodos
  observados. Para los dos primeros se publican `n`, disponibles, ausencias por causa,
  positivos/negativos/ceros y media/mediana de R sólo sobre `available`; un grupo vacío produce
  `null`, nunca cero. No se calculan deltas, rankings, `p-values`, hit-rate, Sharpe, PnL ni señales;
  las ventanas pueden solaparse y las muestras no son independientes.
- **Identidad y límites.** UUID5 determinista sobre namespace
  `investment-analyst/descriptive-condition-outcomes-v1`, `workspace_uuid`, request, fingerprint,
  fuente/binding, versión del horizonte y evidencia material; no depende de rutas ni del reloj. La
  respuesta se limita a 262 144 bytes y falla sin truncar. La lectura usa una apertura `READ_ONLY`,
  índices paginados de hasta 256 entradas, como máximo 4 096 métricas relevantes, un único lote de
  cierres filtrado con un máximo de 4 096 observaciones y cero escrituras de dominio. Las métricas
  técnicas persistidas no incluyen un parámetro `frequency`; el lector exige una fuente diaria
  registrada y rechaza cualquier frecuencia declarada distinta de `day_1`. Los precios
  `adjustment-all` no son precios ejecutables y no incluyen dividendos, costes ni impuestos.

### Evento de alerta

- resultado que lo originó;
- primera y última activación;
- estado nuevo, visto, descartado, resuelto o silenciado;
- deduplication key;
- cooldown vigente;
- canales solicitados;
- estado de cada entrega;
- reintentos y error seguro;
- acuse o nota manual del analista.

La regla, resultado y entrega tendrán identidades distintas. Reintentar un canal nunca debe crear
otro evento analítico.

## Expresión de reglas

Las reglas usan grupos `all`, `any` y, cuando sea necesario, exclusiones explícitas. No emplean una
suma ponderada.

Una plantilla conceptual podría exigir:

- cobertura fundamental suficiente;
- FCF positivo;
- deuda dentro de un umbral configurado;
- cobertura de intereses disponible;
- valoración dentro de un umbral;
- drawdown de mercado determinado.

La salida muestra cada condición. Los umbrales serán configurables y versionados; el ejemplo no
constituye una regla recomendada universal.

Las plantillas se organizan por objetivo analítico —calidad, solvencia, crecimiento, valoración,
mercado, evento— y no por el nombre de un inversor. Una misma métrica no se duplica en varias
metodologías.

## Familias de alertas

### Operación y calidad

- provider o workspace degradado;
- actualización fallida;
- fuente desactualizada;
- cobertura incompleta;
- cambio estructural del proveedor;
- revisión contradictoria;
- interrupción o lock retenido;
- uso cercano a un límite operativo.

Estas serán las primeras alertas activas porque no interpretan una oportunidad financiera.

### Mercado

- drawdown;
- distancia o cruce de medias;
- expansión de volumen;
- volatilidad;
- máximos o mínimos;
- divergencia frente a benchmark;
- RSI Wilder, MACD, Bollinger o ATR mediante contratos independientes; RSI y MACD pueden alimentar
  únicamente reglas SILENT descriptivas, sin entrega, señal ni recomendación.

Una alerta diaria solo puede activarse después de actualizar barras diarias. “Inmediata” significa
inmediatamente después de ingerir evidencia nueva, no tiempo real.

La reconstrucción `daily-market-patterns-v1` de la vista técnica es una lectura descriptiva
independiente de estas reglas y de la outbox. Usa cierres diarios observados y las métricas RSI(14) e
histograma MACD(12,26,9) incrementales v2 ya persistidas, elige la última revisión disponible al
corte y rompe la continuidad ante una métrica ausente o incompatible. Describe el signo del
histograma (`positive`, `negative`, `neutral` o `unavailable`), tramos con censura de borde y
exactamente tres cruces: `macd_cross_positive` (`previo <= 0 < actual`), `macd_cross_negative`
(`previo >= 0 > actual`) y `rsi_cross_above_30` (`previo <= 30 < actual`). No es una regla, no usa
confirmaciones, histéresis, cooldown, scheduling, outbox ni canal, no modifica las siete plantillas y
no produce señal, recomendación ni instrucción. El rango público es inclusivo, máximo 180 días, con
`known_at` explícito y `end <= known_at.date()`; una lectura vacía declara ausencia de evidencia y no
se convierte en cero.

### Fundamentales

- crecimiento;
- cambio de margen;
- conversión de beneficio a caja;
- deuda, vencimientos o cobertura de intereses;
- ROIC;
- dilución;
- deterioro de liquidez;
- cambio de asignación de capital;
- clasificación empresarial nueva o pérdida de evidencia.

La frecuencia corresponde al filing y su disponibilidad, no al movimiento diario de precio.

### Valoración

- FCF yield;
- earnings yield;
- P/E, P/B o P/S;
- enterprise value y múltiplos;
- valoración frente a la propia historia;
- condiciones conjuntas de valoración, generación de caja y solvencia.

Estas reglas esperan la fase de valoración point-in-time para alinear precio y fundamentales sin
look-ahead.

### Eventos

- nuevo filing;
- acción corporativa;
- cambio de rating de calidad de datos;
- noticia o comunicación oficial;
- cambio 13F, Form 4 o 13D/13G;
- publicación o revisión macro;
- evento regulatorio cripto.

## Bajo consumo

El monitor debe:

- consultar métricas recientes y warm-up mínimo;
- evaluar en lote, evitando una consulta por activo;
- registrar una marca de avance por trabajo y dominio;
- omitir activos sin evidencia nueva;
- reutilizar métricas persistidas o resultados deterministas;
- no generar HTML, SVG o exportaciones;
- cargar columnas y rangos acotados;
- limitar concurrencia y memoria;
- ejecutar como proceso one-shot invocado por el scheduler;
- permanecer funcional con la interfaz cerrada.

Para una watchlist pequeña, el cálculo numérico es insignificante frente al coste de red. La IA no
se invoca durante ciclos sin candidatos.

## Control de ruido y falsos positivos

- cobertura mínima y frescura obligatoria;
- confirmación durante uno o más períodos;
- histéresis: umbrales distintos para activar y resolver;
- cooldown por regla y activo;
- deduplicación por regla, activo y estado de evidencia;
- reconocimiento de splits y acciones corporativas;
- reglas específicas por familia, sector y frecuencia;
- máximo de alertas por ciclo y canal;
- agrupación de eventos repetidos;
- modo silencioso antes de notificar;
- pausa y desactivación inmediata;
- historial de candidatos vistos, descartados o útiles.

El feedback manual no cambiará automáticamente umbrales ni entrenará un modelo durante la primera
versión.

## Replay histórico de reglas

Antes de habilitar una regla analítica:

- reproducirla con `known_at` históricos;
- usar únicamente inputs disponibles en cada corte;
- registrar cambios de universo y símbolos;
- distinguir evento detectado de retorno posterior;
- medir frecuencia, duplicación y cobertura;
- inspeccionar falsos positivos;
- no seleccionar umbrales exclusivamente por el mejor resultado histórico.

El replay point-in-time ya está implementado como una operación local de solo lectura. Reproduce
entre 20 y 500 cortes persistidos, selecciona revisiones por `available_at` en fundamentales y por
el `known_at` exacto en mercado, y simula confirmaciones, histéresis, cooldown, aperturas y
resoluciones. Devuelve frecuencia de coincidencias y cobertura, pero no calcula retorno posterior,
rentabilidad ni precisión predictiva.

Los cambios de una regla se aplican únicamente a evidencia nueva del scheduler. No reescriben
resultados, recibos o candidatos históricos. Para inspeccionar evidencia anterior se ejecuta este
replay explícito con la versión configurada actual. Un candidato de una versión anterior conserva
su fingerprint y no puede cerrarse silenciosamente con otra semántica; el analista puede resolverlo
desde la bandeja.

## Canales

Orden recomendado:

1. bandeja persistente y outbox local con acuse explícito;
2. notificación local del navegador o sistema;
3. Telegram o correo opcionales;
4. resumen diario o semanal.

El canal local del sistema está integrado en `ALERTS-DELIVERY` como `windows_toast`, cerrado por
defecto y con evidencia de transporte separada del acuse humano. Sigue siendo una entrega
descriptiva: no crea señales, recomendaciones ni ejecución. Los canales externos —Telegram, correo
o navegador— requieren un bloque posterior y configuración local secreta. Ninguna credencial se
escribe en la regla, workspace analítico, JSON público o logs. Toda entrega futura debe persistir
antes del envío y reintentar sin duplicar; el canal Windows ya aplica esa regla y trata la
incertidumbre como `unknown`, nunca como éxito.

## IA cualitativa opcional

La IA puede:

- resumir un filing o noticia;
- comparar documentos;
- señalar riesgos y cambios declarados;
- relacionar contexto textual con métricas ya calculadas;
- proponer preguntas de investigación;
- redactar una explicación en español con citas.

No puede:

- modificar observaciones o métricas;
- activar por sí sola una regla numérica;
- afirmar hechos sin fuente;
- ocultar el modelo o documentos usados;
- producir una recomendación de compra o venta;
- combinar dimensiones en una puntuación.

Cada ejecución conserva documentos, corte, modelo, proveedor, versión, parámetros, plantilla,
tokens, coste, citas y errores. Se aplican límites de presupuesto y protección contra instrucciones
maliciosas contenidas en documentos.

## Etapas de entrega

### Etapa A — Fundamento

Base local completada el 29 de julio de 2026 después del scheduler multi-activo:

- modelos y almacenamiento versionado;
- motor trivaluado;
- reglas operativas de job fallido, interrumpido, omitido o con cobertura incompleta;
- modo silencioso;
- bandeja local con transiciones auditadas `new → seen/dismissed/resolved/silenced`;
- recuperación automática auditada: un éxito posterior, completo y del mismo trabajo cambia las
  incidencias anteriores a `resolved` con actor `system_recovery`, sin borrar intentos, eventos ni
  transiciones;
- replay point-in-time.

El scheduler expone además `current`, `stale`, `incomplete` o `never_run` por trabajo usando la hora
del último chequeo exitoso y un umbral explícito. La siguiente ampliación añadirá alertas periódicas
por frescura y presupuestos observados por proveedor sin alterar resultados ya persistidos.

### Etapa B — Screening analítico

Primer corte determinista implementado localmente el 29 de julio de 2026; el catálogo empaquetado se
amplió después hasta las siete plantillas actuales:

- contratos estrictos de regla, condición, solicitud, resultado trivaluado y evidencia;
- motor puro sin I/O que recibe una instantánea métrica ya seleccionada point-in-time;
- validación de activo, clase, fuente, período común, disponibilidad, algoritmo, unidad, parámetros
  y calidad;
- identidad reproducible independiente de `computed_at`;
- siete plantillas empaquetadas `SILENT`: actividad relativa de mercado para acciones, ETF y cripto;
  RSI Wilder bajo; histograma MACD positivo; RSI diario incremental v2 bajo; histograma MACD diario
  incremental v2 positivo; balance, margen y crecimiento trimestral para acciones; y la misma
  revisión de balance, margen y crecimiento con evidencia anual;
- defaults `SILENT` versionados en el catálogo; Cazatiburones conserva sus reglas separadas y ninguna
  plantilla se activa por el solo hecho de estar integrada;
- ausencia como `not_evaluable`, sin convertirla en cero;
- mercado y fundamentales evaluados por separado, sin score ni recomendación;
- despacho fundamental por la frecuencia del intento: un job anual evalúa sólo reglas anuales y uno
  trimestral sólo reglas trimestrales; una regla sin frecuencia única coherente no se ejecuta y el
  intento se registra como omitido, sin fallback por ticker ni mezcla de períodos;
- antes de hidratar métricas fundamentales se seleccionan IDs por activo, claves, `available_to`,
  fuente y frecuencia, y sólo después se hidratan en lotes de hasta 256; no se lista el histórico
  completo para descartarlo en memoria;
- persistencia atómica de resultados, recibos por intento, candidatos y transiciones;
- conexión al scheduler únicamente tras éxito, cobertura completa y evidencia nueva;
- selección del corte métrico exacto por activo, fuente, `known_at`, período, algoritmo, parámetros,
  unidad y calidad;
- confirmaciones consecutivas por períodos distintos, histéresis de entrada/salida y cooldown;
- replay y reinicio idempotentes sin reconsultar proveedores ni releer métricas de intentos ya
  procesados;
- bandeja analítica separada de las incidencias operativas, con evidencia exacta y estados
  `new`, `seen`, `dismissed`, `resolved` o `silenced`;
- resolución automática con actor `system_evidence` cuando la evidencia cruza el umbral de salida.
- registro local versionado con locking optimista y restauración de valores iniciales sin borrar
  revisiones;
- editor compacto de estado, umbrales, histéresis, confirmaciones y cooldown;
- replay histórico acotado y de solo lectura por regla y activo;
- resolución dinámica de la versión vigente en cada nuevo intento del scheduler.

Las dos plantillas incrementales aceptan exclusivamente
`market-rsi-incremental-v2-decimal34` y `market-macd-incremental-v2-decimal34`; las cinco anteriores
conservan sus fingerprints, versiones, defaults y overrides. No existe alias, fallback, sustitución
de `rule_id` ni migración automática de configuraciones locales: la adopción es explícita desde el
editor o el replay. El monitor reutiliza las mismas claves de métrica ya solicitadas y el editor
muestra nombre, versión y estado sin cambios de JavaScript ni de API.

Pendiente en esta etapa: observar varios ciclos silenciosos reales de las reglas anuales,
trimestrales y técnicas incrementales antes de habilitar notificaciones financieras; las plantillas
permanecen `SILENT` y no se calibran con el resultado de un piloto.

### Etapa C — Valoración y notificaciones

- consumir las métricas de valoración point-in-time ya persistidas por el dominio independiente;
- reglas de valoración;
- notificación local del sistema (integrada y desplegada por `ALERTS-DELIVERY`; prueba de transporte
  aceptada y canal restituido a cerrado);
- Telegram, correo y resumen diario como canales futuros;
- outbox reanudable.

La entrega de valoración no activa todavía reglas, candidatos, outbox ni notificaciones. Un bloque
posterior podrá referenciar sus `MetricResult` y reason codes sin recalcular fórmulas ni convertir
missingness en cero.

### Etapa D — Eventos e IA

- filings, noticias y Cazatiburones;
- línea temporal;
- resumen cualitativo opcional;
- presupuestos y auditoría de IA.

## Estado reconciliado y medidas V1 (2026-10-31)

Integrado no equivale a desplegado ni operacional. El release `200db2d6` desplegado el 2026-10-31
contiene el canal local Windows cerrado por defecto, con identidad propia, journal append-only y
presupuesto acotado; una prueba de transporte fue aceptada desde el servicio y el canal se restituyó
a deshabilitado. No existe entrega financiera real ni lectura humana observada. Los bytes base
anteriores contienen el monitor, la outbox local, la bandeja y cuatro plantillas `SILENT`; no
contienen un canal de navegador o externo operativo ni una cadena predictiva validada.
`analytical_backtest` reproduce coincidencias, frecuencia y cobertura sobre 20–500 cortes
persistidos; no calcula retorno posterior, rentabilidad ni precisión predictiva. Este bloque no
cambia reglas, scheduler ni almacenamiento financiero.

`SCREENING-INCREMENTAL-COMPATIBILITY`/#387 (`ADVANCES`, sin cierre) añade las dos plantillas
incrementales `SILENT` y mantiene las cinco anteriores sin cambios. El probe real READ_ONLY del
2026-10-31 sobre el workspace `70c64668-1738-4dfe-ba1f-7e72026bb7fe` evaluó las seis combinaciones
QQQ/SOL/AAPL con el selector vigente: RSI `35.004533…`/`31.466109…`/`55.515477…` e histograma
`-3.081330…`/`-0.718932…`/`-0.211784…`, todas evaluables y `not_met` en el último corte. El replay
acotado a 20 cortes por regla y activo registró 35/47/11 cortes disponibles, sin `not_evaluable`, y
no persistió métricas, candidatos ni recibos. La diferencia de AAPL frente al probe de PLAN
(`61.514423…`) proviene de un corte posterior (`available_at` `2026-10-31T15:47:21…`), no de un
cambio de umbral. La fila `FUNDAMENTALS-COVERAGE` sigue `NEXT`; `V1-U1` mejora la elegibilidad
técnica de la muestra, mientras `V1-U3` y `V1-U4` siguen sin resultados posteriores ni validación
predictiva.

Las medidas de entrega y utilidad descriptiva son `V1-U2` y `V1-U3` en
[`basic_functional_release_plan.md`](basic_functional_release_plan.md). Permanecen
`UNKNOWN` hasta una ejecución real con el mismo release, universo y corte; un acuse de transporte no
se presenta como lectura humana y un candidato descartado por el analista no se contabiliza como
falso positivo financiero.

## Evidencia operativa V1 (2026-11-09, `V1-OPERATIONAL-ACCEPTANCE`, `ADVANCES`)

Evaluación read-only sobre la base y runtime `5e7323917be8abaf7257898fe8a0fd5e2c0d0518`, workspace
`70c64668-1738-4dfe-ba1f-7e72026bb7fe` y corte `2026-11-09T21:30:00Z`. No se marcó ningún
candidato como visto, no se acusó ninguna recepción, no se activó ningún canal y no se ejecutó un
refresh.

**Candidatos, outbox y alertas.** El registro analítico informa 313 resultados, 342 intentos
procesados, 5 candidatos (2 `new`, 3 `resolved`) y último candidato el 2026-11-05T20:30:02Z. La
outbox contiene 5 recepciones `pending`, con correspondencia uno a uno de `candidate_id` y cero IDs
duplicados en ese universo: NKE MACD incremental `8e6791ad-d60d-58c3-96c7-f9bbbdf8fece`, SOL RSI
incremental `ee9b255c-a307-5ea1-a523-c025b06b8b41`, AMD volumen relativo
`828b37f0-8db3-5eb8-a3cf-fdc779e4506d`, AAPL volumen relativo
`b6764277-df63-5221-918f-bc9adecd07bb` y BTC volumen relativo
`aee20db0-84b1-5196-a0aa-5593dea7e266`. La outbox no tiene ningún acuse; su estado `pending` es de
entrega, no el ciclo de vida del candidato. La bandeja de alertas operativas es distinta: 57
eventos, 47 `new`, sin mezclarse con los candidatos financieros. El resumen de la Mesa muestra 5
candidatos, 3 institucionales y 0 de actividad declarada.

### Fallos 13F del 2026-11-09

Los dos trabajos programados de Cazatiburones institucional fallaron y dejan la cobertura
incompleta; no se reejecutaron ni se materializaron datos en este bloque.

- **`sec:institutional:13f-cycle`.** Programado 2026-11-09T13:45:00Z, ejecutado
  16:30:42.689777Z–16:32:43.467138Z (120.777 ms), estado `failed`,
  `provider_contract_error` con mensaje `scheduled 13F cycle step failed: run_failed:StorageError`,
  `retryable=false`, 0 creados, pico RSS 834.408 KiB. Estado persistido: dataset
  2026-03-01..2026-05-31, SHA-256 `05f4da8f526cd471a387af45661a8cb3bbfaf345d3ec9c2cefd31349aa257e63`,
  snapshot `2e35473d-5b49-5080-ac19-be2998ee20de`, `manager_cursor=0`, 42 gestores,
  `last_processed_cik=0000895421`, ciclo 7. La etapa es la materialización de observaciones: un
  `StorageError` capturado por tipo en `_observe_asset`; el mensaje exacto de la excepción no se
  persiste. Último éxito: 2026-10-23, 47 creados, `coverage_complete=false`,
  `traceability_verified=true`.
- **`sec:institutional:13f-history`.** Programado 2026-11-09T14:00:00Z, ejecutado
  16:32:43.606531Z–16:32:52.416377Z (8.809 ms), estado `failed`,
  `provider_contract_error` con el mensaje
  `scheduled 13F history step failed: older_materialization_unverified`, `retryable=false`, 0
  creados, pico RSS 716.456 KiB. Estado
  persistido: `phase=ready`, dataset antiguo 2025-12-01..2026-02-28, snapshot
  `3818034b-ab18-51f8-8d8a-6eac0c7b930f`; dataset nuevo 2026-03-01..2026-05-31, snapshot
  `50b90260-1e19-502b-8b2a-eec33bee646a`; `target_cursor=6`, 20 objetivos, ciclo 6,
  `last_processed_cik=0000764068`. La etapa es la verificación de materialización del rol `older`
  (fallo de candidatos, de runs o de trazabilidad); el subcausante exacto no se persiste. Último
  éxito: 2026-10-23, 17 creados, 3 reutilizados, `coverage_complete=false`.

Las etapas e inputs son distintos: el ciclo falla en la materialización del dataset
2026-03-01..2026-05-31 y la historia falla al verificar la materialización antigua
2025-12-01..2026-02-28. Comparten proveedor SEC y la ruta de materialización, pero no hay evidencia
de que uno cause al otro; el inicio de la historia 0,14 s después del cierre del ciclo no demuestra
dependencia causal. La causa raíz de ambos es `UNKNOWN`: no se presume corrupción por la palabra
`StorageError`, ni error de proveedor, ni dato ausente. Efecto visible: 2 trabajos fallidos, 2
bloqueados, 2 incompletos, dos alertas operativas nuevas a las 16:32Z, última cobertura
institucional del 2026-10-23 y 3 notificaciones institucionales pendientes del 2026-10-21. Se
clasifican como **criterio de aceptación no satisfecho** para la frescura y cobertura 13F; la
reparación exige un bloque de producto con `src/**` autorizado y captura de la excepción real, y no
se declara resuelta aquí. El readiness estructural `PASS` del probe conserva
`failure_categories` con `provider_contract_error: 2`; no acredita cobertura sana.

### Canal externo y límites

`GET /api/v1/desktop-notifications` devuelve `desktop-notification-status-v1` con canal
`windows_toast`, `enabled=false`, `enabled_at=null`, `updated_at=2026-10-31T02:10:33.911525Z`,
`capability.available=true`, `last_test_accepted=true` (`2026-10-31T02:10:23.432705Z`), 0 entregas,
0 pendientes, 0 aceptadas y `budget_exceeded=false`; el presupuesto es 8 MiB y 10.000 entregas. La
bandeja local está operativa: 5 candidatos pendientes, 3 institucionales y 0 de actividad
declarada. El transporte probado no equivale a una entrega financiera actual ni a lectura humana:
la aceptación de alertas externas queda como gate explícito para el bloque final y no se cambia
ningún default hoy.

**Recorrido de navegador.** Chromium real sobre `http://127.0.0.1:8765` verificó Mesa (5
candidatos, 3 institucionales, 0 actividad; 40/40 activos de cobertura al expandir la tabla, con
las seis filas BVL bloqueadas) y los perfiles del tablero Activo: BTC cripto con Mercado+Derivados;
AAPL equity con Fundamentales y Valoración 11/11; AMD equity con 17/40 métricas trimestrales y
6/11; MOG.A equity con fundamentales anuales 17/40 y valoración 0/11; QQQ ETF sólo Mercado. GOOGL,
NKE y SOL se verificaron por `universe-coverage-v1`. La evidencia es del runtime base; el candidato
sólo cambia documentación y el test documental, por lo que la equivalencia de rutas productivas
base...head permite reutilizarla. No se observó entrega financiera ni lectura humana. Las
llamadas `400` de fundamentales para BTC/SOL/QQQ y las de MOG.A en frecuencia trimestral son
ausencias por perfil/frecuencia, no fallos de la bandeja; la consulta trimestral de MOG.A se repite
en anual para verificar su perfil soportado.

`route_transition:PREDICTIVE-RESEARCH:ADVANCES`

La reparación 13F de `SHADOW-AND-V1-RELEASE`/#434 (`ADVANCES`) mantiene separadas la observación
histórica y su reconstrucción: un único helper tipado tolera sólo el reloj de recuperación y una hora
de aceptación antigua mal etiquetada UTC que, interpretada en `America/New_York` con DST histórico,
convierte exactamente a la hora documental del mismo filing; cualquier otra diferencia sigue
conflicto. Los 58 conflictos reproducidos (46 de aceptación y 12 de recuperación) se leen con
`compatibility_notes`, conservando el objeto original y sin reescribir ni duplicar sus 212
observaciones objetivo. La enmienda A5 resolvió la selección compartida: las seis páginas se
preflightaron READ_ONLY con 0 claims y 0 observaciones previstas, y dos pasadas productivas con un
único writer dieron 0/0 nuevas, 243 claims y 338 observaciones con hashes idénticos, 96 witnesses y
`traceability_verified=true`; DB/WAL/workspace no cambiaron. El witness es efímero, no certifica la
disponibilidad antigua y los claims duplicados se conservan append-only. El canal externo sigue en
su preferencia actual (`disabled`), no se envían mensajes ni se acusa en nombre del usuario: V1-U2
no acredita acuse y el veredicto es **V1 NO ACEPTADA**. No hay señal, recomendación ni ejecución.

## Validación

Cada entrega prueba:

- misma evidencia y corte producen la misma identidad y resultado;
- evidencia futura no cambia una reconstrucción anterior;
- float, unidad, frecuencia, moneda o algoritmo incompatibles se rechazan;
- ausencia queda `not_evaluable`;
- reglas no aplicables a una familia no se ejecutan;
- revisión contradictoria falla de forma visible;
- repetición no duplica resultados ni alertas;
- un fallo de canal no pierde el evento;
- reintento de canal no duplica el mensaje;
- cooldown e histéresis sobreviven reinicios;
- fallos tardíos conservan progreso anterior;
- secretos no aparecen en errores o estados;
- memoria, latencia y número de consultas cumplen presupuestos;
- el servicio funciona con el navegador cerrado;
- el texto evita recomendación u orden.

## Criterio de salida

La primera versión estará lista cuando pueda operar varios días en modo silencioso, reconstruir cada
candidato, emitir como máximo una alerta por nuevo estado de evidencia, explicar condiciones y
limitaciones, funcionar sin IA y recuperarse de fallos de proveedor, proceso o canal.
