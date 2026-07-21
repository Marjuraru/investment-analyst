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

- controles compactos de rango separados de las series SMA y volumen;
- rangos 1M, 3M, 6M, 1A, 2A, 5A y Máx.;
- cobertura local y número de sesiones siempre visibles;
- tabla exacta cargada solo cuando el analista la abre;
- agregación semanal para 5A y mensual para Máx. antes de ingerir décadas completas.

No se integra la librería de TradingView: requiere su propio acuerdo de uso y no proporciona los
datos de mercado. El gráfico local conserva su contrato, fuente y evidencia propios.

### Koyfin

El [Historical Graph de Koyfin](https://www.koyfin.com/help/charts-and-graphs/) permite combinar
precio, medias, valoración y fundamentales; ajustar ventanas, colores y ejes; mostrar la tabla
subyacente y guardar plantillas reutilizables.

Aplicación al proyecto:

- mantener gráfico, estadísticas rápidas y tabla auditable en una misma herramienta;
- conservar personalización futura de SMA, color y visibilidad como configuración explícita;
- permitir más adelante graficar métricas fundamentales históricas sin mezclarlas con el precio en
  una puntuación;
- estudiar plantillas locales por tipo de análisis, comparación normalizada y escala logarítmica.

## Estadísticas del rango

El gráfico publica retorno total, CAGR cuando los extremos abarcan al menos 365 días y máximo
drawdown basado en cierres. El drawdown conserva los UUID y timestamps exactos del pico y valle. No
es una estimación intradía ni una recomendación de riesgo.

## Resolución automática implementada

El contrato `aapl-market-chart-v2` usa puntos diarios hasta 2A, semanas ISO completas para 5A y
meses calendario UTC para Máx. Nunca reescribe ni persiste nuevas barras: agrega en memoria la
selección point-in-time. Conserva todas las identidades fuente, evita intervalos iniciales truncados
cuando se aplica un límite y mantiene la última sesión diaria aparte del gráfico agregado.

OHLC, volumen, operaciones, VWAP, calidad y SMA tienen reglas versionadas. Las estadísticas del
rango se calculan sobre los cierres visibles, por lo que el máximo drawdown declara explícitamente
su resolución. El trabajo de estadísticas rápidas diarias se limita a las últimas 21 sesiones.

## Orden de expansión

1. Rangos largos, estadísticas exactas y carga diferida de evidencia. Completado.
2. Agregación OHLCV semanal y mensual con contratos e identidades deterministas. Completado.
3. Ventanas y colores configurables para medias e indicadores.
4. Escala lineal/logarítmica, comparación normalizada y plantillas locales.
5. Gráficas históricas de márgenes, caja, crecimiento, valoración y datos por acción.
6. Herramientas de anotación y eventos corporativos, sin ejecución de órdenes.

El backfill de cinco años o de toda la vida de AAPL debe ocurrir después del punto 2 para mantener
una interfaz rápida y respuestas acotadas.
