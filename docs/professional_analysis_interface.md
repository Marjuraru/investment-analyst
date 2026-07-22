# Referencias para una interfaz profesional de análisis

## Patrones adoptados

La interfaz toma referencias funcionales sin copiar código, identidad visual ni componentes
propietarios.

### TradingView

La documentación de [TradingView Advanced Charts](https://www.tradingview.com/charting-library-docs/latest/ui_elements/Time-Scale/)
distingue el rango visible de la resolución de cada barra. Al ampliar el rango reduce la resolución;
por ejemplo, cinco años pueden usar semanas y la vista completa usa meses. También advierte que
forzar demasiadas barras en pantalla aumenta cálculos, bloquea el navegador y consume memoria.

Aplicación al proyecto:

- historial local completo como universo de consulta permanente;
- tipo de gráfico e intervalo de cada barra separados del zoom visible;
- zoom local centrado en el cursor, con alternativa por teclado y restablecimiento explícito;
- desplazamiento horizontal mediante arrastre cuando existe una vista ampliada;
- cobertura local y número de días con datos siempre visibles;
- tabla exacta cargada solo cuando el analista la abre;
- agregación diaria, semanal o mensual antes de dibujar el historial completo.

No se integra la librería de TradingView: requiere su propio acuerdo de uso y no proporciona los
datos de mercado. El gráfico local conserva su contrato, fuente y evidencia propios.

### Koyfin

El [Historical Graph de Koyfin](https://www.koyfin.com/help/charts-and-graphs/) permite combinar
precio, medias, valoración y fundamentales; ajustar ventanas, colores y ejes; mostrar la tabla
subyacente y guardar plantillas reutilizables.

Aplicación al proyecto:

- mantener gráfico, estadísticas rápidas y tabla auditable en una misma herramienta;
- conservar ventana, color y visibilidad de las SMA como configuración explícita y local;
- permitir más adelante graficar métricas fundamentales históricas sin mezclarlas con el precio en
  una puntuación;
- usar escala lineal o logarítmica sin modificar valores exactos, y estudiar plantillas locales por
  tipo de análisis y comparación normalizada.

## Estadísticas del rango

El gráfico publica retorno total, CAGR cuando los extremos abarcan al menos 365 días y máximo
drawdown basado en cierres. El drawdown conserva los UUID y timestamps exactos del pico y valle. No
es una estimación intradía ni una recomendación de riesgo.

## Historial completo e intervalos implementados

El contrato `aapl-market-chart-v5` permite pedir explícitamente 1 día, 1 semana o 1 mes y tres
ventanas SMA. En modo
automático conserva su política compatible por rango, pero la interfaz solicita siempre el historial
completo y por ello usa meses calendario UTC. Los rangos anteriores permanecen disponibles solo en
el endpoint por compatibilidad. Nunca reescribe ni persiste nuevas barras: agrega en memoria la
selección point-in-time y mantiene el último cierre diario aparte del gráfico agregado.

OHLC, volumen, operaciones, VWAP, calidad y SMA tienen reglas versionadas. Las tres SMA aceptan
ventanas configurables y acotadas, conservan sus inputs exactos y cargan contexto anterior al rango
consultado; sus colores y visibilidad son preferencias locales del navegador. Las estadísticas del
rango se calculan sobre los cierres consultados, por lo que el máximo drawdown declara explícitamente
su resolución. El trabajo de estadísticas rápidas diarias se limita a los últimos 21 días con datos.
La escala logarítmica se aplica solo a la posición visual de precios positivos; detalle, tabla,
estadísticas y exportaciones conservan los mismos decimales exactos. Alternar la escala no consulta
de nuevo el workspace.
La misma respuesta puede mostrarse como línea de cierre o velas OHLC sin perder medias, volumen ni
evidencia. Las velas se dibujan en rutas SVG agrupadas por dirección para evitar miles de nodos DOM.
El zoom recorta únicamente el viewport del navegador: no altera el corte, los valores ni las
estadísticas del rango consultado, y la tabla y el CSV de mercado siguen el tramo visible.
Cambiar a semana o mes sí solicita una agregación nueva y cada vela conserva las barras diarias que
la componen. La vela del calendario vigente se marca como «En curso» y con contorno discontinuo;
las anteriores nunca se recortan artificialmente para encajar en el rango.

## Orden de expansión

1. Rangos largos, estadísticas exactas y carga diferida de evidencia. Completado.
2. Agregación OHLCV semanal y mensual con contratos e identidades deterministas. Completado.
3. Ventanas, colores y visibilidad configurables para las tres SMA. Completado.
4. Escala lineal/logarítmica, línea/velas e intervalos diario/semanal/mensual. Completado.
5. Ingestión intradía base de 1 minuto y agregaciones 5/15/30/45 min y 1/2/4/5 h.
6. Comparación normalizada y plantillas locales.
7. Gráficas históricas de márgenes, caja, crecimiento, valoración y datos por acción.
8. Herramientas de anotación y eventos corporativos, sin ejecución de órdenes.

El backfill de cinco años o de toda la vida de AAPL debe ocurrir después del punto 2 para mantener
una interfaz rápida y respuestas acotadas.
