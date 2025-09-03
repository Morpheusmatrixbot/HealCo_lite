import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.json_ld import _extract_from_json_ld


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

