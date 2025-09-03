import ast
import pathlib
from typing import Any, Dict, Optional
import pytest

# Load _extract_from_json_ld function from main.py without executing the whole module
MAIN_PATH = pathlib.Path(__file__).resolve().parent.parent / "main.py"
with MAIN_PATH.open("r", encoding="utf-8") as f:
    module_ast = ast.parse(f.read(), filename="main.py")

_extract_from_json_ld = None
for node in module_ast.body:
    if isinstance(node, ast.FunctionDef) and node.name == "_extract_from_json_ld":
        func_module = ast.Module(body=[node], type_ignores=[])
        code = compile(func_module, filename="main.py", mode="exec")
        namespace = {"Dict": Dict, "Any": Any, "Optional": Optional}
        exec(code, namespace)
        _extract_from_json_ld = namespace["_extract_from_json_ld"]
        break


def test_extract_from_json_ld_missing_nutrition_returns_none():
    data = {"name": "Sample product"}
    assert _extract_from_json_ld(data) is None


def test_extract_from_json_ld_invalid_input_type_returns_none():
    assert _extract_from_json_ld("not a dict") is None
    assert _extract_from_json_ld(123) is None


def test_extract_from_json_ld_empty_nutrition_returns_none():
    data = {"name": "Empty", "nutrition": {}}
    assert _extract_from_json_ld(data) is None


def test_extract_from_json_ld_nested_objects_parsed():
    data = {
        "name": "Nested",
        "brand": {"name": "Brand"},
        "nutrition": {
            "calories": {"@value": "100"},
            "proteinContent": {"value": "10"},
            "fatContent": "3",
            "carbohydrateContent": 2,
        },
    }
    expected = {
        "name": "Nested",
        "brand": "Brand",
        "kcal_100g": 100.0,
        "protein_100g": 10.0,
        "fat_100g": 3.0,
        "carbs_100g": 2.0,
    }
    assert _extract_from_json_ld(data) == expected
