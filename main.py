import time
import threading
import uvicorn
from fastapi import FastAPI
from contextlib import asynccontextmanager
import parser_engine
import build_validator

cache = {}
cache_lock = threading.Lock()  # защита от race condition при параллельном чтении/записи

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
    global cache

    print("\n" + "=" * 70)
    print(">>> [SYSTEM] PC BUILDER API ЗАПУЩЕН")
    print("=" * 70)

    cache = parser_engine.load_from_file() or {}
    if cache:
        print(f">>> [✓] Загружен кэш с {len(cache)} категориями")
    else:
        print(">>> [!] Кэш не найден — начинаем парсинг...")

    def run_background_parsing():
        for category, url in URLS.items():
            try:
                print(f"\n>>> [PARSING] Категория: {category}")
                data = parser_engine.scrape_citilink(url, category)
                if data:
                    with cache_lock:
                        cache[category] = data
                    # Сохраняем вне лока — IO не блокирует кэш
                    parser_engine.save_to_file(cache)
                    print(f"    [✓] {category}: {len(data)} товаров")
            except Exception as e:
                print(f"    [ERROR] {category}: {e}")

            # Увеличили паузу — даём браузеру умереть и освободить RAM
            time.sleep(4)

        print("\n" + "=" * 70)
        print(">>> [✓] ПАРСИНГ ЗАВЕРШЕН")
        print("=" * 70 + "\n")

    parsing_thread = threading.Thread(target=run_background_parsing, daemon=True)
    parsing_thread.start()

    yield

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
    with cache_lock:
        components = list(cache.get(category, []))  # копия — не держим лок пока FastAPI сериализует

    return {
        "category":  category,
        "count":     len(components),
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
    components = {
        "cpu":    _find_component(cpu_id,          "Процессоры"),
        "mb":     _find_component(motherboard_id,  "Материнские платы"),
        "gpu":    _find_component(gpu_id,          "Видеокарты"),
        "ram":    _find_component(ram_id,          "Оперативная память"),
        "psu":    _find_component(psu_id,          "Блоки питания"),
        "case":   _find_component(case_id,         "Корпуса"),
        "cooler": _find_component(cooler_id,       "Кулеры"),
        "ssd":    _find_component(ssd_id,          "SSD"),
    }

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

    result = build_validator.check_compatibility(components)
    result["compatible"] = result["status"] != "CRITICAL"
    return result


@app.get("/search")
async def search_component(query: str, category: str = None):
    """
    Поиск компонента по названию.

    Опционально фильтрует по категории.
    """
    query_lower = query.lower()
    search_categories = [category] if category else list(URLS.keys())
    results = []

    with cache_lock:
        for cat in search_categories:
            for comp in cache.get(cat, []):
                if query_lower in comp.get("name", "").lower():
                    results.append({"category": cat, "component": comp})
                    if len(results) >= 10:  # останавливаемся сразу как набрали 10
                        break
            if len(results) >= 10:
                break

    return {
        "query":   query,
        "found":   len(results),
        "results": results
    }


@app.get("/cache-status")
async def get_cache_status():
    """Получить статус кэша."""
    with cache_lock:
        categories = {cat: len(comps) for cat, comps in cache.items()}
        total = sum(categories.values())

    return {
        "status":           "loaded" if cache else "empty",
        "categories":       categories,
        "total_components": total
    }


if __name__ == "__main__":
    print("\n>>> [API] Стартуем на http://0.0.0.0:8000")
    print(">>> [API] Документация: http://localhost:8000/docs\n")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",   # было info — меньше IO на диск
        workers=1,             # явно 1 воркер — на 1 vCPU больше не нужно
        loop="asyncio"
    )