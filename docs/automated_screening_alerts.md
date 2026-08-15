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

La configuración usa `state/analytical_rule_registry_state_v1.json`. Cada cambio guarda una
revisión completa, encadenada por fingerprint y con escritura atómica privada. El catálogo
empaquetado continúa siendo la fuente de contratos inmutables —métrica, operador, algoritmo,
unidad, parámetros y calidad—; la interfaz solo permite modificar estado, umbrales,
confirmaciones y cooldown.

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

1. bandeja persistente en la aplicación;
2. notificación local del navegador o sistema;
3. Telegram o correo opcionales;
4. resumen diario o semanal.

Los canales externos requieren configuración local secreta. Ninguna credencial se escribe en la
regla, workspace analítico, JSON público o logs. La outbox se persiste antes del envío y reintenta
sin duplicar.

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

Primer corte determinista implementado localmente el 29 de julio de 2026:

- contratos estrictos de regla, condición, solicitud, resultado trivaluado y evidencia;
- motor puro sin I/O que recibe una instantánea métrica ya seleccionada point-in-time;
- validación de activo, clase, fuente, período común, disponibilidad, algoritmo, unidad, parámetros
  y calidad;
- identidad reproducible independiente de `computed_at`;
- plantilla silenciosa de actividad relativa de mercado para acciones, ETF y cripto;
- plantilla silenciosa trimestral de balance, margen y crecimiento para acciones;
- ausencia como `not_evaluable`, sin convertirla en cero;
- mercado y fundamentales evaluados por separado, sin score ni recomendación;
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

Pendiente en esta etapa: ampliar el catálogo de reglas y observar varios ciclos silenciosos reales
antes de habilitar cualquier canal externo.

### Etapa C — Valoración y notificaciones

- consumir las métricas de valoración point-in-time ya persistidas por el dominio independiente;
- reglas de valoración;
- Telegram, correo o notificación local;
- outbox reanudable;
- resumen diario.

La entrega de valoración no activa todavía reglas, candidatos, outbox ni notificaciones. Un bloque
posterior podrá referenciar sus `MetricResult` y reason codes sin recalcular fórmulas ni convertir
missingness en cero.

### Etapa D — Eventos e IA

- filings, noticias y Cazatiburones;
- línea temporal;
- resumen cualitativo opcional;
- presupuestos y auditoría de IA.

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
