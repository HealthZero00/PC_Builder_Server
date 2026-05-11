import os
import time
import threading
import uvicorn
import random
from fastapi import FastAPI
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import parser_engine
import build_validator
from database import load_all_from_db, save_to_db

# Загружаем настройки из .env
load_dotenv()
SHOULD_PARSE = os.getenv("ENABLE_PARSER", "false").lower() == "true"

cache = {}
cache_lock = threading.Lock()

URLS = {
    # "Видеокарты": "https://www.citilink.ru/catalog/videokarty/",
    # "Процессоры": "https://www.citilink.ru/catalog/processory/",
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
    print(f">>> [CONFIG] ПАРСЕР ВКЛЮЧЕН: {SHOULD_PARSE}")
    print("=" * 70)

    # 1. Загрузка данных из БД в кэш
    try:
        cache = load_all_from_db()
        if cache:
            total_items = sum(len(v) for v in cache.values())
            print(f">>> [✓] Кэш загружен из БД: {total_items} товаров")
        else:
            print(">>> [!] База данных пуста.")
            cache = {}
    except Exception as e:
        print(f">>> [!] Ошибка подключения к БД: {e}")
        cache = {}

    # 2. Логика фонового парсера
    def run_background_parsing():
        global cache
        for category, url in URLS.items():
            try:
                print(f"\n>>> [PARSING] Обновляю: {category}")
                data = parser_engine.scrape_citilink(url, category)

                if data:
                    save_to_db(category, data)
                    with cache_lock:
                        cache[category] = data
                    print(f"    [✓] {category}: {len(data)} товаров обновлено")
                else:
                    print(f"    [!] {category}: Нет данных")

            except Exception as e:
                print(f"    [ERROR] Ошибка парсинга {category}: {e}")

            time.sleep(random.uniform(3.0, 7.0))
        print("\n>>> [✓] ЦИКЛ ПАРСИНГА ЗАВЕРШЕН\n")

    # 3. Запуск парсера только если ENABLE_PARSER=true
    if SHOULD_PARSE:
        threading.Thread(target=run_background_parsing, daemon=True).start()

    yield
    print(">>> [SYSTEM] Сервер остановлен")


app = FastAPI(
    title="PC Builder API",
    description="API для сборки ПК с логикой на Python и базой PostgreSQL",
    version="1.2",
    lifespan=lifespan
)


def _find_component(component_id: int | None, category: str) -> dict | None:
    if component_id is None: return None
    with cache_lock:
        category_list = cache.get(category, [])
    for comp in category_list:
        if comp.get("id") == component_id:
            return comp
    return None


@app.get("/components")
async def get_components(category: str):
    with cache_lock:
        components = list(cache.get(category, []))
    return {"category": category, "count": len(components), "components": components}


@app.get("/compatibility-check")
async def check_compatibility(
        cpu_id: int = None, mb_id: int = None, gpu_id: int = None,
        ram_id: int = None, psu_id: int = None, case_id: int = None,
        cooler_id: int = None, ssd_id: int = None
):
    # Собираем объекты из кэша по ID, которые прислал Android
    components = {
        "cpu": _find_component(cpu_id, "Процессоры"),
        "mb": _find_component(mb_id, "Материнские платы"),
        "gpu": _find_component(gpu_id, "Видеокарты"),
        "ram": _find_component(ram_id, "Оперативная память"),
        "psu": _find_component(psu_id, "Блоки питания"),
        "case": _find_component(case_id, "Корпуса"),
        "cooler": _find_component(cooler_id, "Кулеры"),
        "ssd": _find_component(ssd_id, "SSD"),
    }

    # Проверка обязательного минимума
    if not components["cpu"] or not components["mb"]:
        return {
            "status": "CRITICAL",
            "compatible": False,
            "critical": [{"title": "Неполная сборка", "detail": "Выберите процессор и мат.плату"}]
        }

    # Запуск твоего тяжелого валидатора
    result = build_validator.check_compatibility(components)

    # Добавляем удобный флаг для Android
    result["compatible"] = result["status"] != "CRITICAL"
    return result


if __name__ == "__main__":
    # Запуск сервера
    uvicorn.run(app, host="0.0.0.0", port=8000)