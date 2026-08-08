# Desarrollo

## Instalar la herramienta de entorno

El proyecto usa exactamente `uv 0.11.29`. Una instalación aislada que no modifica el Python del
sistema es:

```bash
python3 -m venv "$HOME/.local/share/investment-analyst/tooling/uv-0.11.29"
"$HOME/.local/share/investment-analyst/tooling/uv-0.11.29/bin/python" \
  -m pip install "uv==0.11.29"
export PATH="$HOME/.local/share/investment-analyst/tooling/uv-0.11.29/bin:$PATH"
uv --version
```

El último comando debe comenzar con `uv 0.11.29`. La exportación de `PATH` debe repetirse en una
terminal nueva o añadirse manualmente a la configuración local del shell.

## Sincronizar el entorno exacto

```bash
uv lock --check
uv sync --locked --extra dev
```

`uv lock --check` falla si `pyproject.toml` y `uv.lock` no representan el mismo proyecto.
`uv sync --locked --extra dev` crea o sincroniza `.venv`, instala el proyecto en modo editable y no
actualiza silenciosamente el lock.

## Comprobaciones individuales

```bash
uv run --locked --extra dev python -m ruff format --check .
uv run --locked --extra dev python -m ruff check .
uv run --locked --extra dev python -m pytest \
  --cov=investment_analyst \
  --cov-report=term-missing
uv run --locked --extra dev pip-audit \
  --local \
  --skip-editable \
  --progress-spinner off
```

La cobertura mide líneas y ramas, incluidos los procesos Python iniciados por las pruebas CLI. El
umbral mínimo es 82.00%, frente a una línea base observada de 82.54% con 894 pruebas.

## Verificación completa

```bash
bash scripts/check.sh
```

La verificación completa local continúa disponible para preparación offline, diagnóstico, cambios
de dependencias/toolchain/CI o cuando un Work Block la exija expresamente. En el flujo normal de un
PR, el builder ejecuta Ruff y Pytest focalizados, revisa diff/status y publica el candidato; GitHub
CI es la autoridad de la suite determinista completa para ese SHA. No se debe ejecutar Pytest
completo y después `scripts/check.sh` como dos gates equivalentes.

Los smokes con proveedores, persistencia, scheduler, interfaz o servicio son una evidencia separada
de CI y deben seguir el Issue en un workspace temporal o seguro. Consulta
[`development_protocol.md`](development_protocol.md).

Consulta `docs/dependency_management.md` antes de añadir o actualizar dependencias.
