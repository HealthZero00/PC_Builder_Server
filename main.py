import logging
import threading
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import parser_engine

# ─────────────────── логирование ──────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────── конфиг ───────────────────────────────
PARALLEL_CATEGORIES = 2   # сколько категорий парсить одновременно
                           # > 2 — Citilink может забанить по IP, осторожно

URLS: dict[str, str] = {
    "Видеокарты":        "https://www.citilink.ru/catalog/videokarty/",
    "Процессоры":        "https://www.citilink.ru/catalog/processory/",
    "Материнские платы": "https://www.citilink.ru/catalog/materinskie-platy/",
    "Оперативная память":"https://www.citilink.ru/catalog/moduli-pamyati/",
    "Блоки питания":     "https://www.citilink.ru/catalog/bloki-pitaniya/",
    "Корпуса":           "https://www.citilink.ru/catalog/korpusa/",
    "SSD":               "https://www.citilink.ru/catalog/ssd-nakopiteli/",
    "Кулеры":            "https://www.citilink.ru/catalog/sistemy-ohlazhdeniya-processora/",
}

# ─────────────────── потоко-безопасный кеш ────────────────
_cache: dict[str, list] = {}
_cache_lock = threading.Lock()

# флаг — парсинг ещё идёт
_parsing_in_progress = threading.Event()


def _set_category(category: str, data: list) -> None:
    with _cache_lock:
        _cache[category] = data


def _get_cache_snapshot() -> dict:
    with _cache_lock:
        return dict(_cache)


# ─────────────────── фоновый парсинг ──────────────────────

def _parse_category(category: str, url: str) -> tuple[str, list]:
    """Обёртка для ThreadPoolExecutor — возвращает (категория, данные)."""
    try:
        data = parser_engine.scrape_citilink(url, category)
        return category, data
    except Exception as e:
        log.error("Ошибка категории [%s]: %s", category, e)
        return category, []


def _run_parsing() -> None:
    """
    Запускает PARALLEL_CATEGORIES категорий одновременно.
    По завершении каждой — сразу пишет в кеш и сохраняет файл.
    """
    _parsing_in_progress.set()
    log.info("=== Парсинг запущен (параллельно: %d) ===", PARALLEL_CATEGORIES)
    start = time.time()

    with ThreadPoolExecutor(max_workers=PARALLEL_CATEGORIES) as pool:
        futures = {
            pool.submit(_parse_category, cat, url): cat
            for cat, url in URLS.items()
        }

        for future in as_completed(futures):
            category, data = future.result()
            if data:
                _set_category(category, data)
                # сохраняем снапшот после каждой готовой категории
                parser_engine.save_to_file(_get_cache_snapshot())
                log.info("[%s] ✓ %d товаров записано в кеш", category, len(data))
            else:
                log.warning("[%s] Данных нет, пропускаем", category)

    elapsed = time.time() - start
    log.info("=== Парсинг завершён за %.1f сек ===", elapsed)
    _parsing_in_progress.clear()


# ─────────────────── lifespan ─────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Грузим кеш с диска если есть
    saved = parser_engine.load_from_file()
    if saved:
        with _cache_lock:
            _cache.update(saved)
        log.info("Загружен кеш с диска (%d категорий)", len(saved))

    # Запускаем фоновый парсинг
    thread = threading.Thread(target=_run_parsing, daemon=True, name="parser-bg")
    thread.start()

    yield  # сервер работает

    # при остановке — дать потоку спокойно завершиться (или убить daemon)
    log.info("Сервер останавливается...")


# ─────────────────── FastAPI app ──────────────────────────

app = FastAPI(
    title="PC Components API",
    version="2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/components")
async def get_components(category: str):
    """
    Возвращает товары категории из кеша.
    Если категория ещё парсится — отдаёт то, что уже есть (может быть пусто).
    """
    with _cache_lock:
        data = _cache.get(category)

    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"Категория '{category}' не найдена. Доступные: {list(URLS.keys())}",
        )
    return data


@app.get("/components/all")
async def get_all_components():
    """Отдаёт весь кеш целиком — удобно для дебага."""
    return _get_cache_snapshot()


@app.get("/status")
async def get_status():
    """Показывает что уже готово и идёт ли парсинг."""
    with _cache_lock:
        ready = {cat: len(items) for cat, items in _cache.items()}
    return {
        "parsing_in_progress": _parsing_in_progress.is_set(),
        "ready_categories": ready,
        "total_products": sum(ready.values()),
    }


# ─────────────────── точка входа ──────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )