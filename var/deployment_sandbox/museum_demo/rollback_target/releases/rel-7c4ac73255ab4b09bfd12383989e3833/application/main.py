"""Лабораторное приложение: реестр музейной коллекции, версия 1.0.0."""

import json

APP_NAME = "Museum Collection Registry Demo"
APP_VERSION = "1.0.0"
FEATURES = [
    "просмотр музейной коллекции",
    "просмотр карточек предметов",
]

SAMPLE_ITEMS = [
    {"inventory_number": "LAB-0001", "title": "Керамический сосуд", "category": "археология"},
    {"inventory_number": "LAB-0002", "title": "Графический лист", "category": "графика"},
    {"inventory_number": "LAB-0003", "title": "Историческая фотография", "category": "фотография"},
]

def get_status():
    return {
        "application": APP_NAME,
        "version": APP_VERSION,
        "status": "ok",
        "features": FEATURES,
        "items_count": len(SAMPLE_ITEMS),
    }

if __name__ == "__main__":
    print(json.dumps(get_status(), ensure_ascii=False, indent=2))
