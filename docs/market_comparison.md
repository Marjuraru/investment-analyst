# Comparación diaria de mercado

`market-multi-asset-comparison-v1` es una consulta local, efímera y de solo lectura. Compara entre dos y cinco activos visibles del catálogo que tengan barras diarias y la misma moneda de cotización.

La solicitud usa `asset_id` repetido, `benchmark_id`, `start`, `end` inclusivos y `known_at` con zona horaria. El resultado ordena primero la referencia y después los pares por `asset_id`. Reconstruye cada serie mediante `HistoricalMarketDataService` en un único corte PIT y conserva los UUID de observación de cada cierre.

Solo se usan fechas calendario UTC presentes en todos los activos; no hay interpolación, *forward fill* ni inferencia de calendario bursátil. La muestra común debe contener de 21 a 1300 cierres. Los valores normalizados usan `100 * close_t / close_0`; retorno total, drawdown máximo, volatilidad diaria muestral, correlación y beta se calculan con `Decimal` de precisión 34. Correlación y beta se marcan no disponibles cuando la varianza necesaria es cero.

El artefacto no se persiste como `MetricResult`, no mezcla análisis fundamental, no produce puntajes, ranking, señales ni recomendaciones, y no abre proveedores, planificadores o escritores.

La interfaz local expone `GET /api/v1/market-comparison`; `POST` y `HEAD` no crean ninguna ruta nueva. El gráfico se carga bajo demanda y visualiza únicamente series normalizadas sobre la muestra común.
