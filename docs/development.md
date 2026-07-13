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
