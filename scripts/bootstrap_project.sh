#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# bootstrap_project.sh
# Crea únicamente la estructura base del proyecto investment-analyst.
# Seguro e idempotente: no borra ni sobrescribe archivos existentes.
# Ejecutar desde la raíz del repositorio investment-analyst.
# ------------------------------------------------------------------

ok()   { printf '✅ %s\n' "$*"; }
skip() { printf '⚠️  %s\n' "$*"; }

# Crea un archivo con el contenido recibido por stdin SOLO si no existe.
create_file() {
  local path="$1"
  mkdir -p "$(dirname "$path")"
  if [[ -e "$path" ]]; then
    skip "Ya existe, se conserva: $path"
    cat > /dev/null   # descarta el heredoc entrante
    return 0
  fi
  cat > "$path"
  ok "Creado: $path"
}

# ---- Directorios de paquetes Python (llevan __init__.py) ----
PACKAGE_DIRS=(
  "src/investment_analyst"
  "src/investment_analyst/api"
  "src/investment_analyst/frontend"
  "src/investment_analyst/core"
  "src/investment_analyst/core/models"
  "src/investment_analyst/core/configuration"
  "src/investment_analyst/core/interfaces"
  "src/investment_analyst/providers"
  "src/investment_analyst/providers/market"
  "src/investment_analyst/providers/fundamentals"
  "src/investment_analyst/providers/crypto"
  "src/investment_analyst/providers/macro"
  "src/investment_analyst/analytics"
  "src/investment_analyst/analytics/market"
  "src/investment_analyst/analytics/fundamentals"
  "src/investment_analyst/analytics/crypto"
  "src/investment_analyst/analytics/unified"
  "src/investment_analyst/analytics/cazatiburones"
  "src/investment_analyst/diagnostics"
  "src/investment_analyst/validation"
  "src/investment_analyst/alerts"
  "src/investment_analyst/audit"
  "src/investment_analyst/knowledge"
  "src/investment_analyst/knowledge/glossary"
  "src/investment_analyst/storage"
)

# ---- Directorios sin paquete Python ----
PLAIN_DIRS=(
  "tests/unit"
  "tests/integration"
  "scripts"
  "config"
  "docs"
  "data/raw"
  "data/normalized"
  "data/processed"
  "logs"
)

echo "==> Creando directorios..."
for d in "${PACKAGE_DIRS[@]}" "${PLAIN_DIRS[@]}"; do
  mkdir -p "$d"
done
ok "Directorios listos."

echo "==> Creando __init__.py de paquetes..."
for d in "${PACKAGE_DIRS[@]}"; do
  # El __init__.py raíz se crea aparte (contiene __version__).
  [[ "$d" == "src/investment_analyst" ]] && continue
  init="$d/__init__.py"
  if [[ -e "$init" ]]; then
    skip "Ya existe, se conserva: $init"
  else
    : > "$init"
    ok "Creado: $init"
  fi
done

echo "==> Creando .gitkeep en data/ y logs/..."
for d in "data/raw" "data/normalized" "data/processed" "logs"; do
  keep="$d/.gitkeep"
  if [[ -e "$keep" ]]; then
    skip "Ya existe, se conserva: $keep"
  else
    : > "$keep"
    ok "Creado: $keep"
  fi
done

echo "==> Creando archivos base..."

# -------------------- Paquete raíz --------------------
create_file "src/investment_analyst/__init__.py" <<'EOF'
"""Investment Analyst.

Herramienta personal de análisis de inversiones.
Solo recolecta, procesa, relaciona, visualiza y explica información
para que un analista humano tome sus propias decisiones.

No ejecuta órdenes ni constituye asesoramiento financiero.
"""

__version__ = "0.1.0"
EOF

# -------------------- pyproject.toml --------------------
create_file "pyproject.toml" <<'EOF'
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "investment-analyst"
version = "0.1.0"
description = "Herramienta personal de análisis de inversiones (solo análisis, sin ejecución de órdenes)."
readme = "README.md"
requires-python = ">=3.12"
license = { text = "Proprietary" }
dependencies = []

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "ruff>=0.6",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
addopts = "-ra"

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.ruff.format]
quote-style = "double"
EOF

# -------------------- .gitignore --------------------
create_file ".gitignore" <<'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
build/
dist/
.eggs/

# Entorno virtual
.venv/
venv/
env/

# Entorno / secretos
.env
.env.*
!.env.example
*.key
*.pem
secrets/

# Cachés
.cache/
.mypy_cache/
.ruff_cache/
.pytest_cache/

# Cobertura
.coverage
.coverage.*
htmlcov/

# VS Code
.vscode/

# Bases de datos locales
*.sqlite
*.sqlite3
*.db
*.duckdb

# Parquet local
*.parquet

# Datos descargados
data/raw/*
data/normalized/*
data/processed/*
!data/**/.gitkeep

# Logs
logs/*
!logs/.gitkeep
*.log
EOF

# -------------------- .env.example --------------------
create_file ".env.example" <<'EOF'
# Solo nombres de variables futuras. Sin valores reales.

# Precios de acciones
ALPACA_API_KEY=
ALPACA_API_SECRET=
ALPACA_BASE_URL=

# Fundamentales de acciones
FMP_API_KEY=
SEC_EDGAR_USER_AGENT=

# Macroeconomía
FRED_API_KEY=

# Cripto (precio / operaciones)
COINBASE_API_KEY=
COINBASE_API_SECRET=

# Cripto (fundamentales)
COINGECKO_API_KEY=
COINMETRICS_API_KEY=
# DefiLlama no requiere clave actualmente.

# Aplicación
APP_ENV=
LOG_LEVEL=
EOF

# -------------------- README.md --------------------
create_file "README.md" <<'EOF'
# Investment Analyst

## Propósito
Herramienta personal para recolectar, procesar, relacionar, visualizar y
explicar información útil para el análisis de inversiones. Ayuda a un analista
humano a tomar sus propias decisiones.

## Alcance
- No compra, vende ni ejecuta órdenes.
- Sin trading automático, integración de brokers ni gestión de cuentas.
- Sin recomendaciones directas de compra o venta.

## Principios
- **Transparencia:** cada dato conserva fuente, timestamp, periodo y fecha de
  disponibilidad; cada métrica muestra fórmula, parámetros, unidades y versión;
  cada diagnóstico es reconstruible y auditable.
- **Separación de análisis:** mercado, fundamental y unificado, consultables de
  forma independiente.
- **Independencia de proveedores:** la arquitectura no depende de un proveedor
  concreto.
- **Determinismo:** cálculos reproducibles, sin LLM activo ni consumo de tokens
  en el MVP.

## Estado actual
Solo estructura base del proyecto. Sin indicadores, modelos, conexiones a API,
bases de datos, alertas ni interfaz.

## Entorno mínimo
- Python 3.12
- Entorno virtual `.venv`
- Instalación editable con dependencias de desarrollo (ver `docs/development.md`)

## Advertencia
Esta herramienta **no ejecuta operaciones** y **no constituye asesoramiento
financiero**.
EOF

# -------------------- docs/architecture.md --------------------
create_file "docs/architecture.md" <<'EOF'
# Arquitectura

## Separación por capas
- `providers`: obtención de datos crudos por dominio (market, fundamentals,
  crypto, macro).
- `core`: modelos, configuración e interfaces compartidas.
- `analytics`: cálculos deterministas por dominio y unificados, incluido
  `cazatiburones`.
- `diagnostics`: composición de diagnósticos de mercado, fundamental y unificado.
- `validation`, `alerts`, `audit`, `knowledge`, `storage`: soporte transversal.
- `api` y `frontend`: capas de exposición, aisladas del núcleo.

## Independencia de proveedores
El acceso a datos se define mediante interfaces en `core/interfaces`. Los
proveedores concretos son intercambiables; ningún módulo de análisis depende
directamente de un proveedor específico.

## Trazabilidad
Cada dato conserva fuente, timestamp, periodo y fecha real de disponibilidad.
Cada métrica documenta fórmula, parámetros, unidades, entradas y versión. Cada
diagnóstico debe poder reconstruirse y auditarse; no existe lógica financiera
oculta.

## Diagnósticos separados y unificados
Los diagnósticos de mercado y fundamental se calculan de forma independiente y
pueden consultarse por separado. El diagnóstico unificado los combina sin
mezclar sus fuentes ni ocultar su origen.

## Escalabilidad futura
La modularidad y el desacoplamiento permiten incorporar más adelante Docker,
PostgreSQL, un VPS y una versión de escritorio (PWA o Tauri) sin reescribir el
núcleo. Nada de esa infraestructura se incluye en el MVP.

## Prohibición actual de ejecución de órdenes
El sistema no ejecuta operaciones ni se integra con brokers. Solo produce
información y diagnósticos para decisión humana.
EOF

# -------------------- docs/development.md --------------------
create_file "docs/development.md" <<'EOF'
# Desarrollo

## Activar el entorno virtual
```bash
source .venv/bin/activate
```

## Instalar en modo editable con dependencias de desarrollo
```bash
pip install -e ".[dev]"
```

## Ejecutar Ruff
```bash
ruff format --check .
ruff check .
```

## Ejecutar pytest
```bash
pytest
```

## Verificación completa
```bash
bash scripts/check.sh
```
EOF

# -------------------- Test mínimo --------------------
create_file "tests/unit/test_package.py" <<'EOF'
import investment_analyst


def test_package_importable():
    assert investment_analyst is not None


def test_version():
    assert investment_analyst.__version__ == "0.1.0"
EOF

# -------------------- scripts/check.sh --------------------
create_file "scripts/check.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ruff format --check .
ruff check .
pytest
EOF

# Permisos de ejecución para el verificador.
if [[ -f "scripts/check.sh" ]]; then
  chmod +x scripts/check.sh
  ok "Permisos de ejecución: scripts/check.sh"
fi

echo
ok "Estructura base creada. Ningún archivo existente fue modificado."