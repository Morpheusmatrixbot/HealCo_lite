import ast
import inspect
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

# Load the _extract_from_json_ld function from main.py without importing the whole module
source = Path("main.py").read_text(encoding="utf-8")
module = ast.parse(source)

_extract_from_json_ld = None
for node in module.body:
    if isinstance(node, ast.FunctionDef) and node.name == "_extract_from_json_ld":
        func_code = ast.get_source_segment(source, node)
        exec(func_code, globals())
        break

assert _extract_from_json_ld is not None, "_extract_from_json_ld not found in main.py"


def test_extract_from_json_ld_missing_nutrition_returns_none():
    data = {"name": "Product"}
    assert _extract_from_json_ld(data) is None


def test_extract_from_json_ld_invalid_numeric_types():
    data = {
        "nutrition": {
            "calories": "abc",
            "proteinContent": "10",
        }
    }
    result = _extract_from_json_ld(data)
    assert result is not None
    assert result["kcal_100g"] is None
    assert result["protein_100g"] == 10.0


def test_extract_from_json_ld_empty_nutrition_returns_none():
    data = {"nutrition": {}}
    assert _extract_from_json_ld(data) is None


def test_extract_from_json_ld_nested_objects_handled():
    data = {
        "brand": {"name": "BrandX"},
        "nutrition": {"calories": {"@value": "123"}},
    }
    result = _extract_from_json_ld(data)
    assert result == {
        "name": "Продукт",
        "brand": "BrandX",
        "kcal_100g": 123.0,
        "protein_100g": None,
        "fat_100g": None,
        "carbs_100g": None,
    }
