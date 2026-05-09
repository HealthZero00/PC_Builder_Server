import time
import threading
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import parser_engine
import build_validator
import json

cache = {}

URLS = {
    "Видеокарты": "https://www.citilink.ru/catalog/videokarty/",
    "Процессоры": "https://www.citilink.ru/catalog/processory/",
    "Материнские платы": "https://www.citilink.ru/catalog/materinskie-platy/",
    "Оперативная память": "https://www.citilink.ru/catalog/moduli-pamyati/",
    "Блоки питания": "https://www.citilink.ru/catalog/bloki-pitaniya/",
    "Корпуса": "https://www.citilink.ru/catalog/korpusa/",
    "SSD": "https://www.citilink.ru/catalog/ssd-nakopiteli/",
    "Кулеры": "https://www.citilink.ru/catalog/sistemy-ohlazhdeniya-processora/"
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация сервера и запуск парсинга в фоне"""
    global cache

    print("\n" + "=" * 70)
    print(">>> [SYSTEM] PC BUILDER API ЗАПУЩЕН")
    print("=" * 70)

    # Загружаем кэш если существует
    cache = parser_engine.load_from_file() or {}
    if cache:
        print(f">>> [✓] Загружен кэш с {len(cache)} категориями")
    else:
        print(">>> [!] Кэш не найден — начинаем парсинг...")

    def run_background_parsing():
        """Фоновый парсинг данных"""
        for category, url in URLS.items():
            try:
                print(f"\n>>> [PARSING] Категория: {category}")
                data = parser_engine.scrape_citilink(url, category)
                if data:
                    cache[category] = data
                    parser_engine.save_to_file(cache)
                    print(f"    [✓] {category}: {len(data)} товаров")
            except Exception as e:
                print(f"    [ERROR] {category}: {e}")
            time.sleep(2)  # Вежливая задержка между запросами

        print("\n" + "=" * 70)
        print(">>> [✓] ПАРСИНГ ЗАВЕРШЕН")
        print("=" * 70 + "\n")

    # Запускаем парсинг в отдельном потоке
    parsing_thread = threading.Thread(target=run_background_parsing, daemon=True)
    parsing_thread.start()

    yield  # API работает пока сервер живой

    print(">>> [SYSTEM] Сервер завершает работу...")


app = FastAPI(
    title="PC Builder API",
    description="API для сборки и проверки совместимости ПК",
    version="1.0",
    lifespan=lifespan
)


def _find_component(component_id: int | None, category: str) -> dict | None:
    """Поиск компонента по ID в кэше."""
    if component_id is None:
        return None
    for comp in cache.get(category, []):
        if comp.get("id") == component_id:
            return comp
    return None


@app.get("/components")
async def get_components(category: str):
    """
    Получить компоненты по категории.

    Категории: Процессоры, Материнские платы, Видеокарты,
              Оперативная память, Блоки питания, Корпуса, SSD, Кулеры
    """
    components = cache.get(category, [])
    return {
        "category": category,
        "count": len(components),
        "components": components
    }


@app.get("/compatibility-check")
async def check_compatibility(
        cpu_id: int = None,
        motherboard_id: int = None,
        gpu_id: int = None,
        ram_id: int = None,
        psu_id: int = None,
        case_id: int = None,
        cooler_id: int = None,
        ssd_id: int = None,
):
    """
    Проверить совместимость сборки ПК.

    Принимает ID компонентов (можно получить из /components)
    и возвращает подробный отчёт о совместимости.
    """
    # Собираем компоненты из кэша по ID
    components = {
        "cpu":    _find_component(cpu_id,         "Процессоры"),
        "mb":     _find_component(motherboard_id,  "Материнские платы"),
        "gpu":    _find_component(gpu_id,          "Видеокарты"),
        "ram":    _find_component(ram_id,          "Оперативная память"),
        "psu":    _find_component(psu_id,          "Блоки питания"),
        "case":   _find_component(case_id,         "Корпуса"),
        "cooler": _find_component(cooler_id,       "Кулеры"),
        "ssd":    _find_component(ssd_id,          "SSD"),
    }

    # CPU и MB — обязательные компоненты
    if not components["cpu"] or not components["mb"]:
        return {
            "compatible": False,
            "status": "CRITICAL",
            "critical": [{"code": "MISSING_REQUIRED",
                          "title": "Выберите процессор и материнскую плату",
                          "detail": "CPU и материнская плата обязательны для проверки совместимости.",
                          "field": "cpu/mb"}],
            "warning":  [],
            "advisory": [],
            "summary":  {}
        }

    # Запускаем полный аудит через build_validator
    result = build_validator.check_compatibility(components)

    # Адаптируем ответ: добавляем поле compatible для удобства клиента
    result["compatible"] = result["status"] != "CRITICAL"
    return result


@app.get("/search")
async def search_component(query: str, category: str = None):
    """
    Поиск компонента по названию.

    Опционально фильтрует по категории.
    """
    results = []
    query_lower = query.lower()
    search_categories = [category] if category else URLS.keys()

    for cat in search_categories:
        for comp in cache.get(cat, []):
            if query_lower in comp.get("name", "").lower():
                results.append({"category": cat, "component": comp})

    return {
        "query":   query,
        "found":   len(results),
        "results": results[:10]  # Лимит 10 результатов
    }


@app.get("/cache-status")
async def get_cache_status():
    """Получить статус кэша."""
    return {
        "status":           "loaded" if cache else "empty",
        "categories":       {cat: len(comps) for cat, comps in cache.items()},
        "total_components": sum(len(comps) for comps in cache.values())
    }


if __name__ == "__main__":
    print("\n>>> [API] Стартуем на http://0.0.0.0:8000")
    print(">>> [API] Документация: http://localhost:8000/docs\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")