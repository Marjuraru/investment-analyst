# Estrategia para datos históricos de investigación

## Decisión

Los datos desde 1920 o anteriores son valiosos para estudiar regímenes, primas de riesgo,
valoración agregada y relaciones macrofinancieras. No se incorporan todavía al workspace operativo
de Apple. Primero se implementarán en un workspace de investigación separado, con contratos de
fuente, licencia, revisión y disponibilidad histórica propios.

Una serie larga no convierte por sí sola un modelo en predictivo. Para una acción individual puede
introducir cambios estructurales, sesgo de supervivencia, revisiones macro conocidas solo después
del evento y universos formados con información futura. Apple, además, cotiza desde 1980; los datos
anteriores describen mercados y regímenes, no la historia de AAPL.

## Fuentes candidatas

### Accesibles para prototipos de investigación

- [Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html):
  factores, carteras e industrias de Estados Unidos desde julio de 1926. Es apropiada para
  benchmarks y primas de riesgo, no sustituye precios y fundamentales por empresa. Antes de
  redistribuir sus archivos debe verificarse su licencia y copyright.
- [Robert Shiller Online Data](https://www.econ.yale.edu/~shiller/data.htm): mercado agregado de
  Estados Unidos desde 1871, con precio, dividendos, beneficios, CPI y CAPE. Parte de los datos
  anteriores a 1926 está empalmada o interpolada; no representa valores individuales.
- [FRED y ALFRED](https://fred.stlouisfed.org/docs/api/fred/overview.html): variables macroeconómicas
  y sus vintages. ALFRED es preferible para backtesting point-in-time porque permite reconstruir lo
  que se conocía en una fecha, aunque la profundidad y disponibilidad de revisiones dependen de cada
  serie.
- [Jordà-Schularick-Taylor Macrohistory Database](https://www.macrohistory.net/database/): 48
  variables anuales para 18 economías avanzadas desde 1870. Su licencia es abierta para
  investigación, pero contiene restricciones específicas contra la integración y reventa por
  proveedores comerciales; debe revisarse antes de distribuir un producto.
- [NBER Macrohistory Database](https://www.nber.org/research/data/nber-macrohistory-database):
  series históricas muy detalladas, especialmente de Estados Unidos, útiles para completar períodos
  prebélicos e interbélicos. Muchas no continúan hasta el presente y requieren empalmes explícitos.

### Cobertura institucional por valor individual

- [CRSP US Stock](https://indexes.morningstar.com/research-data-products): más de cien años de
  mercado estadounidense desde 1925, identificador permanente `PERMNO` y continuidad a través de
  eventos corporativos. Es la referencia preferida para backtesting por empresa y universos sin
  sesgo de supervivencia, pero requiere licencia o acceso institucional.
- [Finaeon / Global Financial Data](https://finaeon.com/): cobertura histórica multiactivo y
  acciones estadounidenses, incluidas deslistadas, desde antes de 1920. Es comercial y requiere
  validar precio, licencia, API, frecuencia, ajustes y derecho de almacenamiento local.

## Contrato mínimo antes de ingerir

Cada dataset deberá declarar y probar:

- `dataset_id`, proveedor, URL oficial, licencia y versión o fecha de corte;
- identificador estable del instrumento y vigencia de símbolos, nombres y pertenencia al universo;
- `observed_at`, `available_at`, `retrieved_at` y vintage cuando exista;
- frecuencia, moneda, zona horaria, calendario y convención de períodos;
- tratamiento de splits, dividendos, deslistados, fusiones y retornos totales;
- series originales frente a series empalmadas, interpoladas o reconstruidas;
- checksum del archivo crudo, transformación versionada y evidencia reproducible.

No se mezclarán series de investigación con las observaciones operativas de AAPL ni se usarán para
alterar sus diagnósticos fundamentales o de mercado.

## Ruta de implementación

1. Prototipo local separado con French y FRED/ALFRED, sin UI ni predicción productiva.
2. Catálogo de datasets, licencias, vintages y pruebas contra look-ahead.
3. Motor de features reproducible y evaluación walk-forward con particiones temporales.
4. Baselines simples, costos, delistings, drift, intervalos de incertidumbre y comparación fuera de
   muestra.
5. Decidir entre CRSP y Finaeon para valores individuales antes de integrar un proveedor estable.
6. Exponer únicamente relaciones y modelos que conserven trazabilidad; nunca convertir una
   predicción en recomendación u orden automática.

Hasta completar los puntos 1 y 2, entrenar un predictor dentro de la herramienta principal no es una
base suficientemente sólida.
