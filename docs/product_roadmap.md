# Hoja de ruta estratégica del producto

## Propósito y definición de producto completo

Esta hoja de ruta ordena las ampliaciones necesarias para convertir el MVP local actual en una
herramienta profesional completa de apoyo al análisis. No modifica los límites del producto:
mercado, fundamentales, macro, noticias y actividad de participantes conservan significado,
evidencia y diagnósticos independientes. La aplicación no ejecuta órdenes, no administra dinero y
no convierte sus resultados en una recomendación personalizada o puntuación agregada.

Una primera versión completa deberá:

- seleccionar, actualizar, consultar y analizar todos los activos configurados;
- aplicar contratos distintos a empresas, ETF y criptoactivos;
- incorporar mercados estadounidenses, BVL, fundamentales corporativos, macro, noticias y
  actividad declarada de participantes mediante fuentes identificadas;
- funcionar manualmente y en segundo plano con trabajos reanudables por activo y proveedor;
- conservar revisiones, moneda, período y semántica point-in-time;
- explicar fórmulas, condiciones, inputs, cobertura, frescura y limitaciones;
- ofrecer gráficos, comparación, búsqueda, alertas de candidatos y exportaciones desde una
  interfaz compacta en español;
- continuar funcionando sin un LLM, un proveedor pagado o una conexión permanente;
- poder instalarse, respaldarse, restaurarse y supervisarse en una laptop o equipo dedicado.

La IA cualitativa y la investigación predictiva son módulos opcionales posteriores. No son
dependencias del núcleo determinista.

## Estado de referencia

Checkpoint auditado: 28 de julio de 2026, commit `9b3881e`.

### Funcional

- persistencia local DuckDB/Parquet, identidades deterministas y evidencia append-only;
- mercado diario para el universo gratuito Alpaca IEX y BTC-USD/Coinbase;
- base intradía BTC-USD de un minuto y nueve agregaciones locales;
- fundamentales SEC completos para AAPL y diez emisores genéricos US-GAAP;
- 40 métricas fundamentales, historia, comparación y clasificación explicable cuando la evidencia
  alcanza;
- gráficos diarios, semanales, mensuales e intradía, velas, escala logarítmica, zoom, arrastre y
  tres SMA configurables;
- interfaz local, exportaciones, cachés, compresión, scheduler Apple y servicio `systemd --user`;
- dependencias reproducibles, Ruff, Pytest, cobertura, auditoría y CI.

### Parcial

- BVL: sondeo oficial y lector tipado del boletín diario, todavía sin identidad SMV, persistencia,
  histórico o interfaz;
- operación multi-activo: refresh manual generalizado, pero programación persistente centrada en
  Apple;
- fundamentales extranjeros: TSM, Barrick y BVN requieren IFRS; BVL requiere SMV;
- FRED/ALFRED: fundamento point-in-time desarrollado en una rama paralela, todavía sin integrar en
  la línea actual.

### Diseñado, no implementado

- catálogo y fundamentales SMV;
- scheduler multi-proveedor;
- indicadores técnicos adicionales, comparación y plantillas;
- fundamentales de red para cripto;
- catálogo macro y workspace de historia larga;
- corpus y búsqueda de noticias;
- dominio Cazatiburones;
- screening automático, bandeja de alertas y notificaciones;
- IA cualitativa opcional y laboratorio predictivo;
- autenticación o exposición remota segura.

## Alcance inicial de activos

El catálogo actual cubre mercado estadounidense para AAPL, AMD, Barrick (`B`), BVN, CDE, HYMC,
INTC, MSTR, MU, MUX, NEM, PLTR, SCCO, TSM, GBTC, GLD e IBIT, además de BTC-USD. La cobertura
fundamental no es igual para todos.

La expansión BVL debe resolver mediante evidencia oficial `CVERDEC1`, `BVN`, `SCCO`, `VOLCABC1`,
`MINSURI1` y `POMALCC1`. “FCA” permanece sin mapear. `ABX` debe tratarse como alias histórico de
Barrick cuando se confirme su vigencia, no como instrumento duplicado. “TBC” debe aclararse antes de
añadirlo; no se asumirá que significa BTC.

## Dependencia de integración antes de ampliar

Los PR #10, #11 y #12 forman una pila de ramas. El trabajo multiemisor y BVL actual está respaldado
en GitHub, pero todavía no forma parte de `main`. Antes de añadir otro dominio:

1. integrar el fundamento intradía/multi-activo en orden;
2. reconstruir ramas limpias sobre el `main` actualizado, evitando force-pushes innecesarios;
3. publicar el trabajo multiemisor y BVL mediante un PR propio;
4. trasladar FRED/ALFRED sobre la base consolidada;
5. ejecutar CI y smoke local después de cada squash.

El criterio de salida es que `main` contenga toda la funcionalidad aceptada y que ningún commit
único dependa únicamente de una rama lateral.

## Fase 1 — Identidad y catálogo BVL/SMV

1. Implementar adaptadores tipados para empresas y valores inscritos de SMV.
2. Validar ISIN, emisor, clase, moneda, mercado, nemónico y vigencias.
3. Conservar el ISIN como candidato principal a identidad estable y el nemónico como alias
   versionado.
4. Mantener separadas las cotizaciones BVL de BVN y SCCO de sus instrumentos estadounidenses.
5. Resolver la lista inicial y dejar cualquier identidad ambigua como no disponible.
6. Persistir respuestas oficiales y observaciones registrales de forma append-only.
7. Exponer una consulta local del universo peruano sin atribuirle todavía precios o fundamentales.

Criterio de salida: repetición equivalente sin identidades nuevas, revisión distinta append-only,
ausencia de colisiones y trazabilidad completa a SMV.

## Fase 2 — Mercado diario BVL

1. Persistir el boletín exacto como `RawRecord` solamente después de resolver su uso permitido.
2. Normalizar OHLC, precio anterior, propuestas, promedio, volumen, monto, operaciones y moneda.
3. Distinguir cero, ausencia, propuesta sin operación y sesión sin negociación.
4. Marcar cobertura diferida y no consolidada.
5. Añadir consulta point-in-time y gráfico mediante el servicio histórico común.
6. Integrar los activos BVL en el catálogo visible sin rutas específicas por emisor.
7. Añadir calendario oficial únicamente bajo un contrato versionado.
8. Evaluar boletines fechados para backfill solo cuando términos, estructura y cobertura sean
   compatibles.

Si la automatización histórica de BVL no queda autorizada, la alternativa inicial será importación
manual de documentos oficiales o captura diaria hacia adelante. El endpoint interno
`dataondemand` no será una dependencia de producción sin documentación o autorización.

## Fase 3 — Orquestación multi-activo

El scheduler debe dejar de representar una ejecución Apple y pasar a ejecutar trabajos explícitos
por activo, proveedor, dominio, frecuencia y zona horaria.

Trabajos previstos:

- mercado diario Alpaca;
- fundamentales SEC;
- Coinbase diario e intradía;
- BVL diario;
- catálogo y fundamentales SMV;
- FRED/ALFRED;
- noticias y filings cuando existan sus conectores.

Capacidades requeridas:

- actualizar el activo seleccionado o toda una watchlist;
- locks de writer y estado por trabajo;
- planificación por cobertura, no por repetición ciega;
- rate limiting, reintentos acotados y backoff;
- reanudación mediante recibos verificables;
- frescura y estado por activo/fuente;
- recuperación tras reinicios;
- persistencia del progreso exitoso previo a un fallo;
- panel operativo y health check generalizados.

Esta fase es prerequisito para alertas automáticas y funcionamiento 24/7.

## Fase 4 — Fundamentales corporativos completos

### US-GAAP

Ampliar progresivamente:

- ROA, ROE y ROIC con saldos promedio;
- crecimiento de 3, 5 y 10 años;
- estabilidad de márgenes y flujos como estadísticas, no score;
- accruals, conversión a caja y dilución;
- vencimientos y estructura de deuda cuando exista evidencia comparable;
- market cap y enterprise value point-in-time;
- P/E, P/B, P/S, EV/ventas, EV/EBIT y EV/EBITDA cuando apliquen;
- FCF yield, earnings yield y PEG con denominadores y limitaciones explícitas;
- valoración frente a la propia historia;
- owner earnings como aproximación separada del FCF contable.

### IFRS

Crear un adaptador separado para TSM, Barrick, BVN ADR y otros emisores extranjeros. Los conceptos
`ifrs-full` no pasarán por el normalizador US-GAAP.

### SMV

Después del catálogo peruano:

- incorporar estados oficiales por empresa, período, tipo y moneda;
- revisar taxonomía, signo y unidad;
- reutilizar una métrica únicamente si los inputs son contablemente equivalentes;
- crear definiciones peruanas versionadas cuando no exista equivalencia.

### Clasificación empresarial

Completar crecimiento lento, estable y rápido; cíclica; recuperación; y activo oculto. Las tres
últimas requieren evidencia sectorial, macro, de reestructuración o de composición de activos. No
se deducirán solo de ratios.

Las influencias de Graham, Buffett y Lynch se documentan en la selección y definición de métricas,
pero la interfaz continúa organizando por tipo de análisis. Dalio corresponde al dominio macro y de
regímenes, no a ratios corporativos artificiales.

## Fase 5 — Herramientas de mercado y comparación

Incorporar por contratos pequeños y probados:

- EMA configurables;
- RSI;
- MACD;
- Bandas de Bollinger;
- ATR y volatilidad histórica;
- beta y correlación frente a un benchmark identificado;
- recuperación de drawdown;
- indicadores de volumen solo cuando la cobertura lo permita;
- comparación normalizada de varios activos;
- benchmarks por mercado y sector;
- acciones corporativas y eventos en la gráfica;
- anotaciones y plantillas locales.

Cada indicador declara fórmula, ventana, frecuencia, warm-up, inputs, unidad, versión y limitación.
Ninguno utiliza observaciones posteriores al corte. Los indicadores se habilitan por paneles
configurables para no saturar la vista.

## Fase 6 — Cripto como familia propia

1. Programar refresh diario e intradía incremental.
2. Añadir backfill por ventanas reanudables y recibos de cobertura.
3. Generalizar el catálogo a otros criptoactivos solamente cuando se seleccionen fuentes.
4. Diseñar fundamentales de red separados de mercado: oferta, emisión, actividad, fees, seguridad,
   distribución y concentración.
5. Incorporar eventos regulatorios y noticias.
6. Evaluar métricas on-chain bajo fuente, cadena, timestamp y evidencia propios.

Una etiqueta heurística de wallet no se interpreta como identidad institucional confirmada.

## Fase 7 — Macro y larga historia

1. Integrar el fundamento FRED/ALFRED existente.
2. Crear catálogo versionado de inflación, crecimiento, tipos, curva, empleo, crédito, liquidez,
   dólar y commodities.
3. Enumerar vintages, actualizar en lotes y reanudar.
4. Añadir variables peruanas oficiales mediante BCRP/INEI cuando corresponda.
5. Construir un panel descriptivo de regímenes inspirado en relaciones macro de crecimiento,
   inflación, liquidez y tipos.
6. Mantener este dominio separado de fundamentales y mercado de un activo.

La historia anterior a la cotización de una empresa vive en un workspace de investigación mediante
índices, macro y factores. French, Shiller y otras fuentes requieren catálogo de licencia,
transformación y vintage. CRSP u otra fuente pagada se evaluará solo si su utilidad justifica el
coste.

## Fase 8 — Noticias y eventos

La primera etapa es determinista:

- SEC y filings;
- SMV/BVL;
- bancos centrales y organismos estadísticos;
- relaciones con inversores;
- GDELT como descubrimiento, no confirmación.

El corpus conserva `event_at`, `published_at`, `available_at` y `retrieved_at`, licencia, URL,
identificador externo, hash, idioma, entidades y revisiones. Debe deduplicar sindicación y
correcciones, permitir búsqueda local y mostrar una línea temporal sin inferir causalidad.

## Fase 9 — Cazatiburones

Versión inicial para acciones:

- Form 13F;
- Forms 3, 4 y 5;
- Schedules 13D y 13G;
- cambios de posiciones reportadas;
- entradas, salidas y concentración;
- transacciones de insiders;
- propiedad directa e indirecta;
- demora real entre evento, filing y disponibilidad.

Para BVL se evaluarán participaciones significativas y comunicaciones SMV. Cripto tendrá otro
contrato on-chain. No existirá un “score de tiburones”.

## Fase 10 — Screening y alertas

El diseño completo se encuentra en
[`automated_screening_alerts.md`](automated_screening_alerts.md). La detección numérica será
determinista, point-in-time y de bajo consumo. Una alerta señalará un candidato para revisión y
explicará condiciones y evidencia; no afirmará que debe comprarse o venderse.

La implementación se divide en:

1. alertas operativas y modo silencioso después del scheduler multi-activo;
2. reglas de mercado y fundamentales con métricas existentes;
3. reglas de valoración, técnicas y plantillas configurables;
4. eventos, filings, noticias y Cazatiburones;
5. resumen opcional mediante IA solo después de una activación determinista.

## Fase 11 — Interfaz profesional completa

- watchlists y favoritos;
- ficha rápida del activo, cobertura y frescura;
- comparación multi-activo;
- indicadores configurables;
- evolución fundamental y valoración histórica;
- contexto macro;
- noticias, eventos y Cazatiburones;
- bandeja de candidatos y alertas;
- plantillas guardadas;
- búsqueda global;
- exportaciones exactas.

La interfaz conserva español consistente, términos familiares de mercado, decimales por unidad,
dirección visual inequívoca, teclado, tecnologías de asistencia, responsive y tema oscuro. Los
paneles pesados se cargan bajo demanda.

## Fase 12 — IA cualitativa opcional

Solo después del corpus de noticias:

- adaptador independiente del proveedor;
- ejecución bajo demanda o activada por una alerta;
- presupuesto y límite de tokens;
- modelo, versión, parámetros y plantilla registrados;
- documentos exactos, citas y afirmaciones no verificadas;
- protección contra prompt injection;
- salida separada de resultados cuantitativos.

La aplicación seguirá funcionando sin IA. La IA no modifica observaciones, decide por sí sola una
oportunidad ni produce una recomendación o puntuación conjunta.

## Fase 13 — Investigación predictiva

En workspace separado:

- features point-in-time;
- particiones walk-forward;
- baselines simples;
- costes y latencia;
- supervivencia, deslistados y cambios de universo;
- drift;
- evaluación fuera de muestra;
- registro de experimentos;
- intervalos de incertidumbre.

Una predicción seguirá siendo investigación, no una orden o recomendación.

## Fase 14 — Operación 24/7 y release

Después de estabilizar el scheduler multi-fuente:

- backup y restauración probados;
- logs acotados y métricas de latencia, errores y frescura;
- watchdog y recuperación;
- inicio automático de WSL o despliegue ARM64;
- benchmark antes de elegir un SBC;
- empaquetado para Orange Pi u otro equipo;
- acceso remoto solo con VPN, autenticación y TLS;
- actualización y rollback compatibles con el workspace.

El servidor local no se expondrá directamente en `0.0.0.0`.

## Puerta sistemática de calidad

Cada cambio debe completar:

1. alcance, contrato y análisis de compatibilidad;
2. modelos tipados y versiones explícitas;
3. pruebas offline con fixtures mínimos;
4. integración en workspace temporal;
5. point-in-time, idempotencia, identidad, revisión y aislamiento;
6. fallos parciales y reanudación;
7. Ruff y formato;
8. Pytest completo y cobertura;
9. auditoría de dependencias, configuración y secretos;
10. smoke real sin tocar primero el workspace permanente;
11. repetición real para verificar reutilización;
12. benchmarks de tiempo, memoria y tamaño;
13. pruebas HTTP, navegador, responsive y accesibilidad cuando apliquen;
14. revisión de diff, commit, push, PR y CI remoto;
15. smoke posterior a la fusión en `main`.

Los nuevos conectores y módulos de almacenamiento críticos deberían superar 90 % de cobertura
local, sin elevar artificialmente el mínimo global antes de cubrir el código histórico.

## Optimización transversal

- refrescos incrementales y recibos de cobertura;
- una conexión writer por ejecución;
- lotes por proveedor y rate limiting centralizado;
- cachés acotadas e invalidadas por fuente;
- consultas DuckDB por columnas y rangos;
- Parquet particionado para macro, noticias y corpus grandes;
- cálculos rodantes limitados al warm-up necesario;
- agregación previa a enviar décadas al navegador;
- tablas bajo demanda y virtualizadas;
- evaluación de alertas solo sobre evidencia nueva;
- límites de memoria, respuesta y concurrencia;
- backfills por ventanas reanudables;
- migraciones verificadas mediante backup y restauración.

## Orden inmediato recomendado

1. consolidar la pila de PR actual;
2. integrar la rama multiemisor y BVL;
3. reubicar FRED/ALFRED sobre esa base;
4. implementar identidad y catálogo SMV;
5. persistir e integrar mercado BVL;
6. generalizar el scheduler;
7. activar la primera base de alertas operativas y screening silencioso.
