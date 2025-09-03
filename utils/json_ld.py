from typing import Any, Dict, Optional


def _extract_from_json_ld(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Извлекает данные из JSON-LD структуры"""
    if not isinstance(data, dict):
        return None

    name = (data.get("name") or data.get("headline") or "").strip()
    brand = data.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")

    nutrition = data.get("nutrition") or {}

    def get_nutrition_value(key):
        value = nutrition.get(key)
        if isinstance(value, dict):
            value = value.get("@value") or value.get("value")
        try:
            return float(str(value).replace(',', '.')) if value is not None else None
        except:
            return None

    kcal = (
        get_nutrition_value("calories")
        or get_nutrition_value("energy")
        or get_nutrition_value("caloriesContent")
    )
    protein = get_nutrition_value("proteinContent")
    fat = get_nutrition_value("fatContent")
    carbs = get_nutrition_value("carbohydrateContent")

    if any(v is not None for v in (kcal, protein, fat, carbs)):
        return {
            "name": name or "Продукт",
            "brand": brand,
            "kcal_100g": kcal,
            "protein_100g": protein,
            "fat_100g": fat,
            "carbs_100g": carbs,
        }
    return None

