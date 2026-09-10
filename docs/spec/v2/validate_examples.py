"""Valida fixtures sintéticas y referencias del paquete. No conecta con Faustus.

Uso: python -m pip install 'jsonschema[format]>=4,<5'  # [format] trae rfc3339-validator; sin él el caso 'Fecha no válida' se acepta
     python validate_examples.py
La validación de schemas NO prueba permisos, precondiciones ni seguridad del runtime.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from graphlib import TopologicalSorter, CycleError
try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:
    raise SystemExit("Falta jsonschema. Instala: python -m pip install 'jsonschema[format]>=4,<5'")

ROOT = Path(__file__).resolve().parent

def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def main() -> int:
    failures: list[str] = []
    validators = {}
    for path in sorted((ROOT / 'schemas').glob('*.schema.json')):
        schema = load(path)
        Draft202012Validator.check_schema(schema)
        name = path.name.removesuffix('.schema.json')
        validators[name] = Draft202012Validator(schema, format_checker=FormatChecker())
    positives = 0
    for path in sorted((ROOT / 'examples').glob('*.valid.json')):
        name = path.name.removesuffix('.valid.json')
        errors = list(validators[name].iter_errors(load(path)))
        failures.extend(f'{path.name}: {e.message}' for e in errors)
        positives += 1
    negatives = load(ROOT / 'examples' / 'negative_cases.json')
    for case in negatives:
        if not list(validators[case['schema']].iter_errors(case['instance'])):
            failures.append('Negativo aceptado: ' + case['reason'])
    # Apply a registered tool's argument schema after the envelope schema.
    descriptor = load(ROOT/'examples'/'tool_descriptor.valid.json')
    invocation = load(ROOT/'examples'/'tool_invocation.valid.json')
    arg_validator = Draft202012Validator(descriptor['input_schema'])
    failures.extend(e.message for e in arg_validator.iter_errors(invocation['arguments']))
    malformed_args = dict(invocation['arguments'], path=42)
    if not list(arg_validator.iter_errors(malformed_args)):
        failures.append('El schema de argumentos aceptó path no textual.')
    backlog = load(ROOT/'backlog.json')['requirements']
    ids = {r['id'] for r in backlog}
    if len(ids) != len(backlog):
        failures.append('IDs de requisitos duplicados.')
    graph = {r['id']: set(r['depends_on']) for r in backlog}
    for req, deps in graph.items():
        failures.extend(f'{req}: dependencia inexistente {d}' for d in deps - ids)
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        failures.append('Dependencias cíclicas: ' + str(exc))
    tools = load(ROOT/'tool_catalog.json')['tools']
    if len({t['name'] for t in tools}) != len(tools):
        failures.append('Nombres de herramientas duplicados.')
    for scenario in load(ROOT/'acceptance_scenarios.json')['scenarios']:
        failures.extend(f"{scenario['id']}: requisito inexistente {r}" for r in scenario['requirements'] if r not in ids)
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print(f'OK: {len(validators)} schemas; {positives} fixtures válidas; '
          f'{len(negatives)} fixtures inválidas rechazadas; argumentos en segunda pasada.')
    print(f'OK: {len(backlog)} requisitos, dependencias acíclicas y {len(tools)} nombres únicos.')
    print('Esto valida el paquete de diseño, no la implementación ni los tests de Faustus.')
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f'No se pudo validar el paquete: {exc}')
