# Plan de cierre de la versión básica funcional

## Objetivo

Este documento convierte la hoja de ruta amplia en una ruta crítica verificable. La versión básica
funcional debe permitir que un analista:

- seleccione una watchlist, actualice sus fuentes y conozca cobertura, frescura y fallos;
- consulte mercado y fundamentales por activo con evidencia point-in-time;
- consulte comparaciones diarias de 2–5 activos con evidencia PIT común, de solo lectura y sin
  alterar el plan de actualización ni los artefactos históricos;
- reciba candidatos deterministas para revisión sin una puntuación agregada ni recomendación;
- reciba una notificación deduplicada cuando exista evidencia nueva;
- consulte actividad declarada de insiders, propietarios relevantes e instituciones en una sección
  Cazatiburones independiente;
- solicite un resumen cualitativo opcional con citas sobre documentos persistidos;
- siga usando todas las funciones cuantitativas cuando la IA esté desactivada;
- cierre, reinicie, respalde y restaure el servicio sin perder trazabilidad.

Mercado, fundamentales, valoración, macro, eventos y análisis cualitativo conservan resultados
independientes. Esta versión básica no ejecuta órdenes, no administra dinero y no predice ni
recomienda comprar o vender. Es una frontera de esta release, no una autorización para adelantar
capas futuras: evidencia PIT → análisis → detección de oportunidades → señales/predicción validada
→ recomendación explícita y trazable → decisión humana/política → broker y ejecución controlada
futura.

Las restricciones permanentes son transparencia, auditabilidad, reproducibilidad, `available_at`,
`Decimal`, identidades deterministas, historia append-only, independencia de proveedores en
analytics y separación entre evidencia, análisis, señal, recomendación, decisión y ejecución.

## Referencia factual histórica

La planificación parte de `main` en `00188caf34e045cfb5ed79d62f0289a15e6bb265` (9 de agosto de
2026). El corte de julio, la rama `codex/fred-alfred-vintage-integration` y el PR #14 son contexto
histórico de la consolidación inicial; no se usan como estado vivo, evidencia operativa ni gate.

Este snapshot no es estado vivo. Desde entonces quedaron integrados el runtime por capacidades,
las preferencias persistentes de watchlist/favoritos/actualización programada, la valoración
corporativa point-in-time v1 para
empresas elegibles y el mercado spot diario Coinbase para BTC-USD y ETH-USD. El intradía sigue
siendo un contrato separado de BTC-USD. La valoración v1 mantiene sus estados `not_evaluable` o
`not_applicable` cuando faltan inputs compatibles y no cubre ETF ni cripto. VAL-1 añade historia
materializada de resultados evaluados; las reglas posteriores siguen fuera de alcance.

BTC y ETH incorporan además una familia productiva separada de derivados Deribit: funding horario,
DVOL diario, snapshots prospectivos, replay PIT y scheduler. El diagnóstico es descriptivo y sin
score; el backfill no se presenta como vintage y no se mezcla con Coinbase spot. Véase
[`crypto_derivatives.md`](crypto_derivatives.md).

La interfaz local entrega ese replay solamente para los activos que el catálogo declara elegibles:
el panel lazy conserva el corte visible, consulta 90 días UTC de evidencia persistida y muestra
cobertura, fuentes, ausencias, limitaciones e identidades sin ejecutar refresh ni usar Deribit desde
el navegador.

## Ruta táctica vigente

La ruta orienta priorización, no autorización. `ACTIVE` se deriva exclusivamente del único Issue
abierto con `workflow:active`; main no lo fija. Sólo main expresa el estado integrado: un candidato
puede proponer su transición post-merge mediante `route_effect` (`NONE`, `ADVANCES` o `COMPLETES`),
pero no vuelve verdadera la fila antes de fusionarse. En main, `DONE` requiere evidencia integrada;
`NEXT` es único salvo que todo esté bloqueado o diferido, y una desviación material se registra en
el Work Block con razón y evidencia viva. Los IDs pueden relacionarse con varios Work Blocks y
viceversa.

Hasta que esa secuencia termine y el candidato se fusione, el estado vivo integrado continúa como
`ANALYST-READINESS` | `BLOCKED`; la fila candidata sólo propone el estado post-merge. El antecedente
administrativo sigue siendo OPS-2/#66, supersedido con `insufficient_local_dates` sin completion ni
rehearsal.

| ID | Estado | Dependencia o condición | Evidencia integrada o límite |
| --- | --- | --- | --- |
| `DELIVERY-GOVERNANCE` | `DONE` | Propuesta post-merge de DEV-10 (`COMPLETES`): la ruta se vuelve integrada sólo al fusionar este PR. | DEV-8/#60, DEV-9/#62 y DEV-10/#64; el candidato no cambia main antes del merge. |
| `FOUNDATIONS-RUNTIME` | `DONE` | Runtime, watchlist, health y scheduler por capacidades. | BASE-18/#29, BASE-19/#31 y OPS-1/#37. |
| `MARKET-COMPARISON` | `DONE` | Muestra diaria UTC común, sin FX ni benchmarks sectoriales. | MKT-3/#56/#57: normalización, retorno, volatilidad, drawdown, correlación y beta v1. |
| `ANALYST-READINESS` | `DONE` | Propuesta post-merge de OPS-8 (`COMPLETES`): sólo se vuelve integrada tras BUILD, CI, AUDIT, rehearsal HUMAN exact-SHA y merge. | OPS-8/#89 añade la sonda fail-closed y el runbook; reutiliza #85 y exige backup/restore detenido antes del merge. |
| `VALUATION-HISTORY` | `DONE` | Historia materializada y reglas relativas explícitas compatibles con valoración PIT v1. | VAL-1/#70 aporta historia descriptiva; VAL-2/#72 propone percentil Decimal34 PIT sin señal ni recomendación. |
| `INDICATORS-AND-OUTBOX` | `DONE` | Propuesta post-merge de ALERT-1 (`COMPLETES`): la ruta se vuelve integrada sólo al fusionar este PR. | RSI/MACD/ATR, reglas silenciosas y outbox local con acuse durable; los canales externos siguen pendientes. |
| `RELEASE-ACCEPTANCE` | `DONE` | Reconciliación HUMAN de #93/#94; integrada antes de este bloque. | La aceptación finita y la recuperación ya fueron registradas. |
| `EQUITY-UNIVERSE` | `NEXT` | Candidate #95 propone `DONE` post-merge mediante contratos por capacidades. | AAPL mantiene adaptadores compatibles; no hay privilegio de flujo por ticker. |
| `SEC-CORPUS` | `PLANNED` | Candidate #95 propone `NEXT` sólo después de su merge. | Forms 3/4/5, 13D/13G y 13F no integrados. |
| `BVL-MARKET` | `BLOCKED` | Contrato de uso y fuente oficial autorizada. | No se infiere autorización para automatizar boletines. |
| `PREDICTIVE-RESEARCH` | `DEFERRED` | Carril explícito con PIT, label, baselines, validación temporal, holdout, shadow y rollback. | Universo survivorship-aware es condicional; on-chain, stablecoins/DeFi, multi-venue y derivados extra dependen del target. |

La transición propuesta por el candidate es literalmente `EQUITY-UNIVERSE` | `DONE` y
`SEC-CORPUS` | `NEXT`; ambas permanecen no integradas hasta su merge exacto en `main`.

## Diagnóstico de la estructura actual

### Base que debe conservarse

1. `ApplicationRuntime` centraliza workspace, catálogo, resolución de proveedores y apertura
   explícita de almacenamiento.
2. DuckDB y Parquet soportan el volumen actual sin justificar PostgreSQL ni microservicios.
3. Los contratos tipados separan raw, observaciones, métricas y diagnósticos.
4. Las fuentes se resuelven desde el catálogo y el scheduler ya trabaja por activo, proveedor,
   dominio y frecuencia.
5. El modelo append-only, las identidades deterministas, `available_at` y `known_at` son una base
   correcta para alertas, IA y futura investigación predictiva.
6. El motor de screening es puro, trivaluado y solo se ejecuta después de evidencia nueva.
7. La interfaz se sirve en loopback, no expone secretos y carga paneles analíticos desde contratos
   locales versionados.
8. Las únicas dependencias de producción son Pydantic y DuckDB. Esta superficie pequeña favorece
   operación continua y una futura instalación ARM64.

### Riesgos prioritarios

#### P0 — estabilidad operativa

- OPS-8 ya cerró readiness, recuperación detenida y enablement en su propio bloque integrado. La
  aceptación de release restante conserva esas evidencias y sólo repite la costura que cambie en el
  candidate exacto; no convierte una lectura histórica en evidencia de otro SHA.
- Los fallos permanentes permanecen visibles y no consumen retries; categorías legacy, joins
  incompletos, presupuesto excedido o trabajo pendiente fallan cerrado.

#### P1 — eficiencia y mantenibilidad

- `frontend/local_web.py` tiene 1.674 líneas y reúne protocolos, controlador, aplicación web,
  routing, validación HTTP, serialización y servidor.
- `app.js`, `styles.css` e `index.html` suman 8.402 líneas. La experiencia funciona, pero cada
  ampliación aumenta el riesgo de regresión y carga inicial.
- El controlador usa un único `RLock` para lecturas y escrituras. Un refresh de red prolongado puede
  bloquear consultas locales aunque la evidencia anterior siga siendo válida.
- La respuesta `/api/overview` incluye el detalle completo de 40 trabajos y ocupa casi 100 KiB,
  aunque el encabezado solo necesita conteos, próxima ejecución e incidencias.
- scheduler, alertas y screening cargan, validan y reescriben documentos JSON completos. Los tamaños
  actuales son pequeños, pero los límites configurados de 100.000 o 250.000 registros harían
  costoso cada cambio en una operación 24/7.
- el peak global systemd cercano a 6,2 GiB no está atribuido a un job y sigue como deuda de medición
  por job; no constituye un presupuesto ni invalida el delta SMV acotado aceptado en #85.
- persisten nombres, valores predeterminados y adaptadores heredados de Apple junto a rutas
  genéricas. `AAPL` no es solo un ejemplo en el código actual: conserva bootstrap, runner, estados,
  controlador y contratos privilegiados que deben migrar sin romper compatibilidad;
- los 40 trabajos se ejecutan secuencialmente y con horarios próximos. Falta un presupuesto
  central por proveedor, medición de latencia y distribución del trabajo.

#### P1 — cobertura funcional del analista

- la valoración corporativa PIT v1, su historia materializada local y reglas relativas explícitas
  están disponibles para empresas elegibles; falta cobertura adicional compatible;
- RSI Wilder, MACD y ATR son capacidades descriptivas independientes y trazables junto con EMA,
  Bollinger y la comparación diaria MKT-3; falta ampliar reglas y canales externos;
- la watchlist, los favoritos y la actualización programada ya son preferencias persistentes
  versionadas; faltan ampliaciones de experiencia, plantillas y operación observada a largo plazo;
- solo existen cuatro reglas analíticas empaquetadas;
- existe una outbox local con acuse; no existe canal de notificación externo;
- no existe todavía un corpus local de filings, comunicados y noticias;
- sin corpus no es posible implementar una IA auditable con citas;
- Cazatiburones está diseñado, pero aún no ingiere Forms 3/4/5, Schedules 13D/13G o 13F;
- BVL tiene identidad registral, pero mercado y fundamentales siguen bloqueados por fuente,
  autorización o adaptador.

#### P2 — expansiones que no deben bloquear esta versión

- historia desde 1950 y laboratorio predictivo;
- fundamentales on-chain de cripto;
- extensiones Cazatiburones para BVL y cripto;
- mercado y fundamentales BVL automatizados sin contrato de uso resuelto;
- exposición remota, aplicación móvil o servidor SBC;
- base vectorial, microservicios, Kubernetes o migración a PostgreSQL.

## Arquitectura objetivo mínima

```text
Catálogo + watchlist
        |
        v
Planificador de cobertura -> cola local de trabajos -> adaptadores de proveedores
        |                           |
        |                           v
        |                    writer único y acotado
        |                           |
        v                           v
estado operativo          DuckDB + Parquet append-only
                                    |
                +-------------------+-------------------+
                |                   |                   |
             mercado          fundamentales     macro/eventos/Cazatiburones
                |                   |                   |
                +------ resultados independientes -----+
                                    |
                          screening determinista
                                    |
                         candidatos + outbox local
                                    |
                 +------------------+------------------+
                 |                                     |
          interfaz y exportación              resumen IA opcional
                                                sobre corpus citado
```

La IA no se ubica dentro de ingestión, normalización, cálculo ni activación de reglas. Consume un
paquete de evidencia inmutable después de una acción del usuario o un candidato determinista.

La futura inferencia estadística tampoco altera esas capas. La estructura objetivo conserva tres
dominios: evidencia PIT en DuckDB/Parquet; cálculo e inferencia local; e interfaz semántica con LLM
opcional. No se requiere una base de datos ni servidor de inferencia cloud.

## Cambios arquitectónicos propuestos

### 1. Aplicación y HTTP

- crear casos de uso genéricos por activo y dominio y conservar los contratos `Aapl*` únicamente
  como adaptadores de compatibilidad;
- prohibir nuevas bifurcaciones por símbolo y especializar solo por capacidad, proveedor,
  taxonomía o familia de activo;
- reemplazar gradualmente bootstrap, runner, estado, controlador y defaults de AAPL por
  equivalentes genéricos con migración versionada;
- exigir pruebas de contrato cruzadas para AAPL, otro emisor US-GAAP, uno IFRS, un ETF y BTC antes
  de retirar cada adaptador histórico;
- dividir `local_web.py` en controlador, DTO HTTP, routers de lectura, comandos, errores y servidor;
- convertir los refresh manuales largos en comandos con identidad y estado; el POST devuelve el
  trabajo aceptado y la interfaz conserva la última lectura mientras el writer opera;
- publicar un overview compacto y mover el detalle de trabajos a un endpoint bajo demanda;
- invalidar cachés por activo y dominio, no vaciar todas las cachés después de cualquier refresh;
- mantener un solo writer, pero permitir lecturas de snapshots o cachés inmutables mientras se
  consulta la red;
- impedir que una ruta web componga directamente proveedores o repositorios.

No se cambia de framework HTTP en esta fase. Primero se reducen responsabilidades y se miden los
resultados; añadir una dependencia solo por routing no aporta valor al MVP.

### 2. Persistencia y consultas

- mantener DuckDB/Parquet para evidencia financiera;
- añadir consultas por proyección, activo, fuente, período y rango antes de materializar modelos;
- evitar devolver evidencia repetida en cada punto cuando el cliente puede recibir un índice
  versionado de identidades compartidas;
- diseñar una migración versionada desde los JSON operativos a un journal append-only con snapshot
  compacto, checksum y compacción; no editar ni convertir el workspace permanente sin backup,
  smoke temporal y restauración verificada;
- separar retención detallada, resumen por trabajo y auditoría histórica;
- particionar macro diaria, documentos y noticias por fuente y fecha cuando su volumen lo exija;
- añadir métricas del propio almacenamiento: bytes, filas nuevas, reutilizadas y duración por
  etapa.

### 3. Scheduler y automatización

- clasificar fallos como configuración, símbolo no soportado, límite, transitorio, contrato o datos
  incompletos; solo los transitorios consumen reintentos automáticos;
- resolver `B` y CDE antes de ampliar la watchlist;
- introducir presupuestos por proveedor: solicitudes por ventana, concurrencia, backoff y pausa
  explícita;
- distribuir los trabajos durante el día y priorizar por frescura y watchlist;
- no recalcular ni evaluar screening si no cambió la evidencia;
- conservar recibos de cobertura y progreso por ventana para backfills;
- mantener el job Deribit por activo elegible a +10 minutos, con ventana rolling de 90 días,
  freshness 36h y snapshot actual aun cuando los históricos estén cubiertos;
- crear un health resumido que diferencie datos desactualizados, fuente bloqueada y fallo del
  producto;
- registrar tiempo, memoria aproximada, bytes y resultado de cada trabajo sin secretos.

### 4. Núcleo analítico

El cierre básico debe añadir, en contratos independientes:

1. mercado: RSI, MACD, ATR y extensiones de volatilidad/Bollinger con ventanas configurables; EMA
   MKT-2 ya está disponible como métrica descriptiva independiente;
2. comparación: retorno, volatilidad, drawdown, correlación y beta contra un benchmark identificado;
3. valoración: market cap, enterprise value, P/E, P/B, P/S, EV/ventas, EV/EBITDA, FCF yield y
   earnings yield cuando los denominadores sean compatibles;
4. fundamentales: crecimiento de 3/5/10 años, saldos promedio para ROA/ROE/ROIC, dilución,
   conversión a caja, deuda y estabilidad de márgenes;
5. screening: reglas separadas de mercado, fundamentales y valoración, cada una con evidencia,
   estado no evaluable, confirmaciones, histéresis y replay.

Valoración es un dominio propio que alinea precio y fundamentales por disponibilidad, moneda,
acciones y período. No modifica ni combina los diagnósticos de mercado y fundamentales.

### 5. Interfaz

- añadir watchlists y favoritos persistidos localmente mediante un contrato versionado;
- mostrar primero símbolo, precio, cambio, frescura, cobertura, incidencias y candidatos;
- cargar detalle operativo, tablas históricas, evidencia y paneles avanzados solo al abrirlos;
- dividir JavaScript y CSS por componentes sin cambiar la experiencia visual validada;
- virtualizar tablas largas y no crear miles de nodos DOM;
- mantener redondeo solo de presentación y `Decimal` exacto en exportaciones;
- añadir comparación y plantillas configurables sin saturar el gráfico principal;
- hacer visible si un activo tiene mercado, fundamentales, valoración, macro y documentos, sin
  mostrar paneles vacíos.

### 6. Cazatiburones

El primer alcance forma parte de la ruta básica porque reutiliza el corpus SEC requerido por la IA:

1. corpus versionado de filings con accession number, emisor, declarante, formulario y timestamps;
2. Forms 3/4/5 para propiedad y transacciones de insiders;
3. Schedules 13D/13G para propiedad beneficiaria y sus enmiendas;
4. Form 13F para posiciones trimestrales institucionales, después de resolver CUSIP y clases;
5. métricas, eventos y reglas propias sin mezclar mercado o fundamentales;
6. línea temporal y evidencia visible antes del resumen mediante IA.

La implementación detallada se mantiene en [`cazatiburones.md`](cazatiburones.md). Las extensiones
SMV y on-chain permanecen fuera de esta primera entrega porque necesitan fuentes e identidades
distintas.

Las anomalías de filings se evalúan localmente. Volumen y order book no se usan como evidencia
institucional con la cobertura IEX actual; requieren un contrato futuro de microestructura y una
fuente consolidada o de profundidad identificada.

### 7. Carril predictivo local

No se añadirá XGBoost o LightGBM directamente al runtime. Primero se creará un experimento aislado
que compare ambos, y baselines más simples, mediante:

1. objetivo y label versionados con horizonte temporal explícito;
2. matriz de features PIT producida por DuckDB y materializada en Parquet;
3. snapshot de universo que incluya deslistados y cambios de identidad cuando aplique;
4. entrenamiento e inferencia local reproducibles;
5. manifest con datos, features, hiperparámetros, semilla, librerías y artefacto;
6. validación purged walk-forward, calibración y holdout cronológico final;
7. TreeSHAP local solo para modelos y observaciones aceptados;
8. shadow mode y rollback antes de cualquier alerta.

Se seleccionará como máximo un booster. Si no supera los baselines fuera de muestra o no cumple
presupuestos de memoria/ARM64, no se incorpora. El carril es opcional y no bloquea el uso de la
versión cuantitativa determinista. Si llega a promoverse después de validación, calibración cuando
aplique, explicación, shadow mode y rollback, seguirá siendo una señal o detección separada; una
recomendación futura sería un artefacto explícito, no personalizado y trazable, nunca un renombre
del diagnóstico ni una salida libre del LLM.

## Implementación básica de IA

### Función permitida

La primera IA cualitativa podrá:

- resumir cambios declarados en un filing o comunicado;
- extraer riesgos, eventos, cifras textuales y contradicciones aparentes;
- relacionar esas afirmaciones con métricas cuantitativas ya calculadas;
- comparar dos revisiones de un documento;
- proponer preguntas concretas para la revisión humana;
- redactar en español y citar cada afirmación al documento exacto.

En esta primera capa cualitativa no calificará una inversión, no predecirá precio, no activará reglas
ni generará una recomendación; su salida no sustituye la promoción validada y separada de capas
posteriores.

### Prerrequisito: corpus local

La primera fuente será SEC EDGAR porque ya existe identidad de emisor y es oficial. Cada documento
conservará:

- fuente, emisor, formulario, identificador externo, URL y licencia;
- `event_at`, `published_at`, `available_at` y `retrieved_at`;
- hash del contenido, idioma, revisión y relación con documentos anteriores;
- texto extraído y fragmentos direccionables para citas.

Después se podrán añadir comunicados SMV, BVL, bancos centrales y relaciones con inversores.
Descubridores como GDELT no se tratarán como confirmación del hecho.

### Puerto y adaptadores

Se definirá un puerto `QualitativeAnalysisProvider` independiente del proveedor. La primera entrega
puede usar un endpoint compatible configurado por variables de entorno, con modelo y URL
explícitos. Así se podrá probar un modelo económico sin acoplar el dominio a DeepSeek, OpenAI,
Gemini u otro proveedor.

Cada ejecución persistirá:

- hash del paquete de evidencia y corte point-in-time;
- proveedor, modelo, parámetros y versión de prompt;
- JSON estructurado validado, afirmaciones, citas y advertencias;
- tokens de entrada/salida, coste calculado y duración;
- error seguro, estado de caché y decisión de presupuesto.

La clave API nunca se persiste. Una respuesta sin citas válidas falla de forma visible. El texto de
los documentos se trata como datos no confiables y no como instrucciones.

Cuando la solicitud explique un modelo, el LLM recibirá un `ModelExplanationPacket`, no una serie
temporal: predicción o score, baseline, valores originales, principales SHAP, calibración o
percentil, incertidumbre, evidencia y versiones. SHAP atribuye el resultado al modelo; no prueba
causalidad. Para análisis documental se adjuntan fragmentos citables por separado.

### Control de coste y operación 24/7

- IA desactivada por defecto;
- ejecución solo por solicitud o candidato nuevo que cumpla una política explícita;
- caché por documento, corte, prompt y modelo;
- una solicitud concurrente como máximo;
- límites diarios de solicitudes, tokens y coste;
- contexto construido localmente con solo fragmentos relevantes;
- modelo económico para extracción y resumen; escalamiento manual a otro modelo cuando el caso lo
  justifique;
- ningún polling continuo al modelo.

El presupuesto inicial apunta a no más de 800 tokens de evidencia condensada y una salida breve,
pero se controlará por tokens facturados totales y moneda diaria. La llamada exige evidencia nueva,
deduplicación, cooldown y un candidato local elegible. `P > 0,85` no es un requisito universal: se
usará solo si existe un clasificador calibrado y el umbral fue elegido fuera de muestra. Un detector
no supervisado publica `anomaly_score` o percentil, nunca una probabilidad ficticia.

Con este diseño el scheduler cuantitativo puede operar 24/7 sin tokens y la IA consume únicamente
cuando aporta valor.

## Bloques de ejecución

### Bloque 0 — estabilizar la base publicada

1. observar y revisar CI, health y telemetría del `main` vigente antes de ampliar función;
2. corregir cualquier fallo reproducible sin ampliar alcance;
3. clasificar los fallos de proveedor y aplicar una política que no desperdicie reintentos;
4. validar reinicio, idempotencia y tres ciclos de scheduler en modo silencioso;
5. fusionar solo con checks aprobados y crear la siguiente rama desde `main`.

Salida: cero fallos críticos sin explicar y rama principal reproducible.

### Bloque 1 — generalización, eficiencia y operación recuperable

1. congelar nuevas APIs específicas de AAPL y definir los contratos genéricos compatibles;
2. migrar bootstrap, runner, estado y controlador por etapas, sin cambiar identidades persistidas;
3. crear overview compacto y detalle bajo demanda;
4. separar adaptadores web y caché por dominio;
5. convertir refresh manual en trabajo no bloqueante;
6. instrumentar latencia, volumen y presupuesto por proveedor;
7. conservar los contratos integrados de backup/restore y completar el rehearsal HUMAN exact-SHA
   de OPS-8 antes de considerar integrada la transición de readiness;
8. diseñar y migrar estados operativos solo después del smoke temporal.

Salida: AAPL usa el mismo núcleo que los demás emisores, la interfaz sigue respondiendo durante un
refresh, el reinicio recupera trabajos y la historia operativa no depende de reescrituras
crecientes.

### Bloque 2 — análisis y alertas básicas completas

1. ampliar la experiencia de watchlist/favoritos persistentes y sus plantillas;
2. añadir indicadores técnicos mínimos y extender valoración PIT v1 con historia y reglas
   posteriores compatibles;
3. comparaciones ampliadas sólo si justifican una muestra y benchmark distintos de MKT-3;
4. catálogo pequeño de reglas útiles por dominio;
5. outbox reanudable, notificación local y un canal externo opcional;
6. resumen diario de frescura, fallos y candidatos.

Salida: el sistema encuentra, explica y notifica candidatos sin IA ni intervención continua.

### Bloque 3 — corpus y Cazatiburones básico

1. importar y versionar filings SEC;
2. búsqueda y línea temporal local;
3. normalizar Forms 3/4/5 y Schedules 13D/13G;
4. incorporar 13F con correspondencias verificadas de CUSIP y clase;
5. calcular cambios descriptivos, features y anomalías locales con baseline robusto;
6. añadir panel, evidencia y reglas Cazatiburones independientes.

Salida: el analista puede revisar actividad declarada de participantes y recibir candidatos
trazables sin convertirlos en señal de compra o venta.

### Bloque 4 — IA cualitativa opcional

1. ejecutar el carril predictivo como experimento local, con baselines y validación temporal;
2. construir paquetes de evidencia deterministas desde documentos, eventos, métricas y, si fue
   aceptado, el modelo local;
3. implementar el puerto de IA y un adaptador económico;
4. validar citas, presupuesto, caché, prompt injection y ausencia de secretos;
5. añadir una acción de “resumir evidencia” y un panel separado en la interfaz.

Salida: un candidato o documento puede enriquecerse con un resumen citado, pero toda la herramienta
sigue funcionando con IA apagada.

### Bloque 5 — aceptación de la versión

1. congelar candidate PR/head/tree con `candidate-stage`/`candidate-update` sólo después de BUILD,
   AUDIT y CI exact-SHA;
2. ejecutar la aceptación HUMAN de mercado, recovery, accesibilidad y una captura finita explícita
   del observer;
3. reutilizar OPS-8 y la evidencia integrada de corpus sólo cuando el SHA/tree y la superficie no
   hayan cambiado; repetir únicamente la costura invalidada;
4. verificar benchmark p50/p95, PID/NRestarts, RSS/HWM/swap, gaps, 503, restart y SHA drift sin
   atribuir causalidad de memoria;
5. mantener `RELEASE-ACCEPTANCE` en `PLANNED` hasta el comentario HUMAN exact-SHA y el merge; sólo
   entonces proponer `RELEASE-ACCEPTANCE DONE`, `EQUITY-UNIVERSE NEXT` y `SEC-CORPUS PLANNED`;
6. crear tag sólo después del smoke post-merge con el tree idéntico.

Salida: versión básica funcional, reproducible y utilizable diariamente.

## Presupuestos iniciales

Se validarán mediante un script repetible, no con una sola medición:

- overview resumido: p95 menor a 100 ms y menos de 20 KiB sin compresión;
- catálogo: p95 menor a 50 ms;
- gráfico anual diario: p95 en caché menor a 100 ms y primera lectura menor a 1 s;
- fundamentales del período visible: p95 en caché menor a 100 ms y primera lectura menor a 1 s;
- UI interactiva durante un refresh de proveedor;
- cero consultas al proveedor cuando la cobertura ya está completa;
- cero llamadas de IA sin acción o evento elegible;
- cero series crudas o cálculos delegados al LLM;
- inferencia y SHAP locales por lote y solo sobre evidencia nueva;
- límite configurable de tokens de entrada, salida y coste diario, con objetivo inicial de 800
  tokens de evidencia por evento;
- memoria registrada y revisada mediante una captura finita read-only;
- estados y logs con retención acotada y compacción verificable;
- un fallo de canal o IA nunca pierde el candidato ni bloquea el scheduler.

Estos valores son objetivos iniciales para el entorno local actual. Antes de elegir un SBC se repetirán en
ARM64 y se fijarán límites de memoria, temperatura y almacenamiento.

## Validación estadística obligatoria

Todo modelo predictivo o detector supervisado que aspire a salir del workspace de investigación
debe aprobar:

1. particiones cronológicas purged walk-forward;
2. purge de cualquier muestra de entrenamiento cuyo intervalo de label solape validación/test;
3. embargo coherente con horizonte, frecuencia y dependencia serial;
4. transformaciones, imputación, selección y tuning ajustados dentro de cada fold;
5. holdout final cronológico que no participe en decisiones de modelo;
6. comparación con baseline y reporte por fold, régimen y activo;
7. calibración temporal para cualquier salida denominada probabilidad;
8. sensibilidad a costes, drift, universo y múltiples intentos de modelado;
9. reconstrucción exacta de features mediante `available_at`;
10. shadow mode y criterio de retirada.

Queda prohibido usar `KFold` aleatorio, shuffle o una partición que permita entrenar con datos
posteriores al test. Un `TimeSeriesSplit` simple con `gap` tampoco basta cuando los intervalos de
formación de labels se solapan: debe aplicarse purge explícito.

## Definición de terminado

La versión básica estará lista únicamente cuando:

- todos los activos configurados muestren su matriz real de capacidades y frescura;
- no existan fallos recurrentes sin clasificación o acción;
- cada resultado y candidato pueda reconstruirse desde su evidencia;
- mercado, fundamentales y valoración permanezcan separados;
- AAPL no conserve una ruta analítica privilegiada fuera de adaptadores compatibles documentados;
- Cazatiburones reconstruya filings, participantes, instrumentos y disponibilidad sin inferencias
  ocultas;
- notificaciones sean deduplicadas, reanudables y opcionales;
- la IA entregue salida estructurada y citada dentro de presupuesto, o pueda apagarse sin degradar
  el producto cuantitativo;
- cualquier modelo local habilitado conserve manifest, validación temporal, baseline, calibración,
  SHAP, shadow mode y rollback; si no existe un modelo aceptable, la versión sigue operativa sin él;
- backup, restauración, reinicio y observación operacional finita hayan sido probados;
- Ruff, formato, Pytest, cobertura, auditoría, CI y smokes reales pasen;
- la documentación describa exactamente el comportamiento y las limitaciones observadas.

## Decisiones explícitamente diferidas

- `EXTENDED-SOAK / DEDICATED-RUNTIME ALWAYS-ON ACCEPTANCE` requiere un Work Block independiente y un
  host persistente; no se autoriza infraestructura adicional en esta aceptación.

- No se compra una fuente BVL ni se automatiza un endpoint sin autorización.
- No se añade una base vectorial hasta que la búsqueda local del corpus demuestre que es necesaria.
- No se migra a PostgreSQL ni a microservicios para el volumen actual.
- No se ejecuta un LLM continuamente.
- La predicción de precios no se incorpora a esta versión operativa; cualquier investigación futura
  debe superar su matriz PIT, baseline, validación temporal y criterios de promoción propios.
- No se añaden XGBoost, LightGBM o SHAP como dependencias hasta completar el benchmark y autorizar
  explícitamente su impacto reproducible y ARM64.
- La historia desde 1950 se mantendrá en un workspace de investigación separado.
- Cazatiburones para BVL, análisis on-chain y despliegue SBC comienzan después de la aceptación
  básica; la primera vertical SEC de Cazatiburones sí forma parte de la ruta.
- Integración con brokers, ejecución controlada y posible automatización acotada pertenecen a un
  horizonte posterior: requieren decisión humana o de política, autorización explícita, contratos
  propios, trazabilidad y controles de riesgo, sin tocar el núcleo analítico ni reescribir historia.
