"""
main.py  —  оптимизирован под VPS 1 vCPU / 1 GB RAM
─────────────────────────────────────────────────────
• PARALLEL_CATEGORIES = 1  — на 1 vCPU параллельность только мешает
• Чекпойнт каждые N товаров — не теряем прогресс при краше
• Thread-safe cache через Lock
• /status — видим прогресс в реальном времени
"""

import logging
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import parser_engine
import build_validator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
#  КОНФИГ
# ─────────────────────────────────────────────────────────

# На 1 vCPU / 1 GB RAM — только 1.
# Два браузера одновременно = 700-900 MB RAM → OOM killer.
# Если сервер >= 2 GB — можно поднять до 2.
PARALLEL_CATEGORIES = 1

URLS: dict[str, str] = {
    "Видеокарты":         "https://www.citilink.ru/catalog/videokarty/",
    "Процессоры":         "https://www.citilink.ru/catalog/processory/",
    "Материнские платы":  "https://www.citilink.ru/catalog/materinskie-platy/",
    "Оперативная память": "https://www.citilink.ru/catalog/moduli-pamyati/",
    "Блоки питания":      "https://www.citilink.ru/catalog/bloki-pitaniya/",
    "Корпуса":            "https://www.citilink.ru/catalog/korpusa/",
    "SSD":                "https://www.citilink.ru/catalog/ssd-nakopiteli/",
    "Кулеры":             "https://www.citilink.ru/catalog/sistemy-ohlazhdeniya-processora/",
}

# ─────────────────────────────────────────────────────────
#  THREAD-SAFE КЕश
# ─────────────────────────────────────────────────────────

_cache: dict[str, list] = {}
_cache_lock = threading.Lock()

# прогресс парсинга для /status
_progress: dict[str, str] = {}   # category -> "pending"|"running"|"done"|"error"
_parsing_active = threading.Event()


def _set(category: str, data: list) -> None:
    with _cache_lock:
        _cache[category] = data


def _snapshot() -> dict:
    with _cache_lock:
        return dict(_cache)


# ─────────────────────────────────────────────────────────
#  ФОНОВЫЙ ПАРСИНГ
# ─────────────────────────────────────────────────────────

def _parse_one(category: str, url: str) -> None:
    """Парсит одну категорию с чекпойнтами."""
    _progress[category] = "running"
    log.info("[PARSER] Старт: %s", category)

    # колбэк: сохраняем промежуточный результат
    def checkpoint(partial: list) -> None:
        _set(category, partial)
        snap = _snapshot()
        parser_engine.save_to_file(snap)
        log.info("[CHECKPOINT] %s: %d товаров сохранено", category, len(partial))

    try:
        data = parser_engine.scrape_citilink(url, category, checkpoint_cb=checkpoint)
        if data:
            _set(category, data)
            parser_engine.save_to_file(_snapshot())
            _progress[category] = "done"
            log.info("[PARSER] Готово: %s — %d товаров", category, len(data))
        else:
            _progress[category] = "error"
            log.warning("[PARSER] Пусто: %s", category)
    except Exception as e:
        _progress[category] = "error"
        log.error("[PARSER] Ошибка %s: %s", category, e)


def _run_parsing() -> None:
    """
    Запускает парсинг категорий.
    PARALLEL_CATEGORIES=1 → строго последовательно (рекомендуется для 1 vCPU).
    """
    _parsing_active.set()
    log.info("=== Парсинг запущен (параллельность: %d) ===", PARALLEL_CATEGORIES)
    t0 = time.time()

    if PARALLEL_CATEGORIES <= 1:
        # Последовательно — один браузер, минимум RAM
        for category, url in URLS.items():
            _parse_one(category, url)
    else:
        # Параллельно — только если сервер позволяет
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=PARALLEL_CATEGORIES) as pool:
            futures = {pool.submit(_parse_one, cat, url): cat
                       for cat, url in URLS.items()}
            for f in as_completed(futures):
                pass  # результаты уже пишутся внутри _parse_one

    elapsed = time.time() - t0
    log.info("=== Парсинг завершён за %.0f сек (%.1f мин) ===",
             elapsed, elapsed / 60)
    _parsing_active.clear()


# ─────────────────────────────────────────────────────────
#  LIFESPAN
# ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=== PC Builder API стартует ===")

    saved = parser_engine.load_from_file()
    if saved:
        with _cache_lock:
            _cache.update(saved)
        for cat in URLS:
            _progress[cat] = "done" if cat in _cache else "pending"
        log.info("Кеш загружен: %d категорий, %d товаров",
                 len(saved), sum(len(v) for v in saved.values()))
    else:
        for cat in URLS:
            _progress[cat] = "pending"
        log.info("Кеш не найден — начинаем парсинг с нуля")

    thread = threading.Thread(target=_run_parsing, daemon=True, name="parser")
    thread.start()

    yield

    log.info("Сервер завершает работу...")


# ─────────────────────────────────────────────────────────
#  FastAPI
# ─────────────────────────────────────────────────────────

app = FastAPI(
    title="PC Builder API",
    version="2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _find_component(component_id: int | None, category: str) -> dict | None:
    if component_id is None:
        return None
    with _cache_lock:
        items = _cache.get(category, [])
    for comp in items:
        if comp.get("id") == component_id:
            return comp
    return None


# ── эндпоинты ────────────────────────────────────────────

@app.get("/components")
async def get_components(category: str):
    """Товары категории. Категории: Процессоры, Видеокарты, ..."""
    with _cache_lock:
        data = _cache.get(category)
    if data is None:
        raise HTTPException(404, f"Категория '{category}' не найдена. "
                                 f"Доступные: {list(URLS.keys())}")
    return {"category": category, "count": len(data), "components": data}


@app.get("/components/all")
async def get_all():
    """Весь кеш. Для отладки."""
    return _snapshot()


@app.get("/compatibility-check")
async def check_compatibility(
    cpu_id:          int | None = None,
    motherboard_id:  int | None = None,
    gpu_id:          int | None = None,
    ram_id:          int | None = None,
    psu_id:          int | None = None,
    case_id:         int | None = None,
    cooler_id:       int | None = None,
    ssd_id:          int | None = None,
):
    """Полный аудит совместимости сборки."""
    components = {
        "cpu":    _find_component(cpu_id,         "Процессоры"),
        "mb":     _find_component(motherboard_id,  "Материнские платы"),
        "gpu":    _find_component(gpu_id,          "Видеокарты"),
        "ram":    _find_component(ram_id,           "Оперативная память"),
        "psu":    _find_component(psu_id,           "Блоки питания"),
        "case":   _find_component(case_id,          "Корпуса"),
        "cooler": _find_component(cooler_id,        "Кулеры"),
        "ssd":    _find_component(ssd_id,           "SSD"),
    }
    if not components["cpu"] or not components["mb"]:
        return {
            "compatible": False, "status": "CRITICAL",
            "critical": [{"code": "MISSING_REQUIRED",
                          "title": "Выберите процессор и материнскую плату",
                          "detail": "CPU и MB обязательны для проверки совместимости.",
                          "field": "cpu/mb"}],
            "warning": [], "advisory": [], "summary": {},
        }
    result = build_validator.check_compatibility(components)
    result["compatible"] = result["status"] != "CRITICAL"
    return result


@app.get("/search")
async def search(query: str, category: str | None = None):
    """Поиск компонента по имени."""
    q = query.lower()
    cats = [category] if category else list(URLS.keys())
    results = []
    with _cache_lock:
        for cat in cats:
            for comp in _cache.get(cat, []):
                if q in comp.get("name", "").lower():
                    results.append({"category": cat, "component": comp})
    return {"query": query, "found": len(results), "results": results[:20]}


@app.get("/status")
async def status():
    """Прогресс парсинга и состояние кеша."""
    with _cache_lock:
        ready = {cat: len(items) for cat, items in _cache.items()}
    return {
        "parsing_active":  _parsing_active.is_set(),
        "parallel":        PARALLEL_CATEGORIES,
        "categories":      {
            cat: {
                "status":  _progress.get(cat, "pending"),
                "count":   ready.get(cat, 0),
            }
            for cat in URLS
        },
        "total_products":  sum(ready.values()),
    }


# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )