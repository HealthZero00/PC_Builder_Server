import os
import time
import threading
import uvicorn
import random
from fastapi import FastAPI, Query
from typing import Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import parser_engine
import build_validator
from database import load_all_from_db, save_to_db

load_dotenv()
SHOULD_PARSE = os.getenv("ENABLE_PARSER", "false").lower() == "true"

cache: dict = {}
cache_lock = threading.Lock()

URLS = {
    # "Видеокарты": "https://www.citilink.ru/catalog/videokarty/",
    # "Процессоры": "https://www.citilink.ru/catalog/processory/",
    # "Материнские платы": "https://www.citilink.ru/catalog/materinskie-platy/",
    # "Оперативная память": "https://www.citilink.ru/catalog/moduli-pamyati/",
    "Блоки питания": "https://www.citilink.ru/catalog/bloki-pitaniya/",
    # "Корпуса": "https://www.citilink.ru/catalog/korpusa/",
    # "SSD": "https://www.citilink.ru/catalog/ssd-nakopiteli/",
    # "Кулеры": "https://www.citilink.ru/catalog/sistemy-ohlazhdeniya-processora/"
}

# Маппинг категорий Android → категории в кэше
CATEGORY_MAP = {
    "Процессоры":        "Процессоры",
    "Материнские платы": "Материнские платы",
    "Видеокарты":        "Видеокарты",
    "Оперативная память":"Оперативная память",
    "Блоки питания":     "Блоки питания",
    "Корпуса":           "Корпуса",
    "Кулеры":            "Кулеры",
    "SSD":               "SSD",
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
            for cat, items in cache.items():
                print(f"        {cat}: {len(items)} шт.")
        else:
            print(">>> [!] База данных пуста.")
            cache = {}
    except Exception as e:
        print(f">>> [!] Ошибка подключения к БД: {e}")
        cache = {}

    # 2. Фоновый парсер
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
    version="2.0",
    lifespan=lifespan
)


# ═══════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════

def _find_component_by_id(component_id: int) -> dict | None:
    """
    Ищет компонент по ID во ВСЁМ кэше (по всем категориям).
    Не привязан к категории — Android передаёт только ID.
    """
    if component_id is None:
        return None
    with cache_lock:
        for category_items in cache.values():
            for comp in category_items:
                if comp.get("id") == component_id:
                    return comp
    return None


def _find_components_by_ids(ids: list[int]) -> list[dict]:
    """
    Находит компоненты по списку ID.
    ВАЖНО: один ID может встречаться несколько раз —
    пользователь добавил N одинаковых планок ОЗУ.
    Возвращаем по одному объекту на каждый ID, включая дубли.
    """
    result = []
    for cid in ids:
        item = _find_component_by_id(cid)
        if item is not None:
            result.append(item)
        else:
            print(f"    [WARN] Компонент с ID={cid} не найден в кэше")
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  ЭНДПОИНТЫ
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/components")
async def get_components(category: str):
    """Возвращает список компонентов по категории."""
    with cache_lock:
        components = list(cache.get(category, []))
    return {
        "category":   category,
        "count":      len(components),
        "components": components,
    }


@app.get("/compatibility-check")
async def check_compatibility(
    cpu_id:    Optional[int]       = Query(None),
    mb_id:     Optional[int]       = Query(None),
    # Множественные параметры: ?gpu_id=1&gpu_id=2
    # Retrofit на Android автоматически разворачивает List в такой формат
    gpu_id:    Optional[list[int]] = Query(None),
    ram_id:    Optional[list[int]] = Query(None),
    psu_id:    Optional[int]       = Query(None),
    case_id:   Optional[int]       = Query(None),
    cooler_id: Optional[int]       = Query(None),
    ssd_id:    Optional[list[int]] = Query(None),
):
    """
    Проверка совместимости сборки.

    Одиночные компоненты (CPU, MB, PSU, Case, Cooler):
        ?cpu_id=42&mb_id=17

    Множественные компоненты (GPU, RAM, SSD):
        ?ram_id=5&ram_id=5&ram_id=5   ← 3 одинаковых планки ОЗУ
        ?gpu_id=100&gpu_id=101         ← 2 разных видеокарты
        ?ssd_id=200&ssd_id=201         ← 2 SSD
    """

    # Одиночные компоненты
    cpu    = _find_component_by_id(cpu_id)
    mb     = _find_component_by_id(mb_id)
    psu    = _find_component_by_id(psu_id)
    case_  = _find_component_by_id(case_id)
    cooler = _find_component_by_id(cooler_id)

    # Множественные — с учётом дублей
    gpus = _find_components_by_ids(gpu_id or [])
    rams = _find_components_by_ids(ram_id or [])
    ssds = _find_components_by_ids(ssd_id or [])

    # Логируем что получили
    print(
        f"\n>>> [VALIDATE] "
        f"cpu={cpu_id}({'OK' if cpu else 'NOT FOUND'}) "
        f"mb={mb_id}({'OK' if mb else 'NOT FOUND'}) "
        f"gpu={gpu_id}({len(gpus)} шт.) "
        f"ram={ram_id}({len(rams)} шт.) "
        f"ssd={ssd_id}({len(ssds)} шт.) "
        f"psu={psu_id}({'OK' if psu else 'NOT FOUND'}) "
        f"case={case_id}({'OK' if case_ else 'NOT FOUND'}) "
        f"cooler={cooler_id}({'OK' if cooler else 'NOT FOUND'})"
    )

    # Собираем компоненты для валидатора
    # GPU/RAM/SSD передаём как списки — валидатор умеет работать с ними
    components = {
        "cpu":    cpu,
        "mb":     mb,
        "gpu":    gpus,   # список (может быть пустым)
        "ram":    rams,   # список
        "psu":    psu,
        "case":   case_,
        "cooler": cooler,
        "ssd":    ssds,   # список
    }

    # Запускаем валидатор
    result = build_validator.check_compatibility(components)

    # Добавляем удобный флаг для Android
    result["compatible"] = result["status"] != "CRITICAL"

    print(
        f">>> [RESULT] status={result['status']} "
        f"critical={len(result.get('critical', []))} "
        f"warning={len(result.get('warning', []))} "
        f"advisory={len(result.get('advisory', []))}"
    )

    return result


@app.get("/health")
async def health():
    """Проверка состояния сервера и кэша."""
    with cache_lock:
        cats  = {k: len(v) for k, v in cache.items()}
        total = sum(cats.values())
    return {
        "status":             "ok",
        "components_cached":  total,
        "categories":         cats,
        "parser_enabled":     SHOULD_PARSE,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)