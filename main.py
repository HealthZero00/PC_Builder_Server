"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

import os
import asyncio
import random
import logging
import uvicorn
import threading

from fastapi import FastAPI, Query
from typing import Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv

import parser_engine
import parser_regard
import parser_dns
import build_validator
import matcher
from database import (
    load_all_from_db,
    save_to_db,
    save_regard_price_to_db,
    save_store_specs_and_compat_to_db,
)

load_dotenv()

log = logging.getLogger(__name__)

SHOULD_PARSE = os.getenv("ENABLE_PARSER", "false").lower() == "true"

cache: dict = {}
cache_lock = asyncio.Lock()

cache_lock_sync = threading.Lock()

COMPAT_KEYS = (
    "socket", "chipset", "ramType", "ramSlots", "ramMaxFreq", "ramHeight",
    "ramCapacity", "tdp", "cpuPowerPin", "maxTdp", "coolerHeight",
    "psuWattage", "psuFormFactor", "psuLength", "psuEfficiency",
    "gpuPowerPin", "formFactor", "pciVersion", "m2Slots", "m2Types",
    "gpuChipset", "vram", "gpuLength", "gpuHeight", "gpuSlots", "gpuTdp",
    "gpuReqPsu", "gpuPciVersion", "maxGpuLength", "maxCpuCoolerHeight",
    "maxPsuLength", "supportedMbFormats", "ssdInterface", "ssdFormFactor",
    "ssdCapacityGb",
)

URLS_CITILINK = {
    "Видеокарты":         "https://www.citilink.ru/catalog/videokarty/",
    "Процессоры":         "https://www.citilink.ru/catalog/processory/",
    "Материнские платы":  "https://www.citilink.ru/catalog/materinskie-platy/",
    "Оперативная память": "https://www.citilink.ru/catalog/moduli-pamyati/",
    "Блоки питания":      "https://www.citilink.ru/catalog/bloki-pitaniya/",
    "Корпуса":            "https://www.citilink.ru/catalog/korpusa/",
    "SSD":                "https://www.citilink.ru/catalog/ssd-nakopiteli/",
    "Кулеры":             "https://www.citilink.ru/catalog/sistemy-ohlazhdeniya-processora/",
}

URLS_REGARD = {
    "Кулеры":            "https://www.regard.ru/catalog/5162/kulery-dlya-processorov",
    "СЖО":               "https://www.regard.ru/catalog/1008/zidkostnoe-oxlazdenie-szo",
    "Материнские платы": "https://www.regard.ru/catalog/1000/materinskie-platy",
    "Блоки питания":     "https://www.regard.ru/catalog/1225/bloki-pitaniya",
    "Оперативная память":"https://www.regard.ru/catalog/1010/operativnaya-pamyat",
    "Процессоры":        "https://www.regard.ru/catalog/1001/processory",
    "Видеокарты":        "https://www.regard.ru/catalog/1013/videokarty",
    "SSD":               "https://www.regard.ru/catalog/1015/nakopiteli-ssd",
    "Корпуса":           "https://www.regard.ru/catalog/1032/korpusa",
}

URLS_DNS = {
    "Процессоры":         "https://www.dns-shop.ru/catalog/17a899cd16404e77/processory/",
    "Материнские платы":  "https://www.dns-shop.ru/catalog/17a89a0416404e77/materinskie-platy/",
    "Видеокарты":         "https://www.dns-shop.ru/catalog/17a89aab16404e77/videokarty/",
    "Оперативная память": "https://www.dns-shop.ru/catalog/17a89a3916404e77/operativnaa-pamat-dimm/",
    "Блоки питания":      "https://www.dns-shop.ru/catalog/17a89c2216404e77/bloki-pitania/",
    "Корпуса":            "https://www.dns-shop.ru/catalog/17a89c5616404e77/korpusa/",
    "Кулеры":             "https://www.dns-shop.ru/catalog/17a9cc2d16404e77/kulery-dla-processorov/",
    "СЖО":                "https://www.dns-shop.ru/catalog/17a9cc9816404e77/sistemy-zidkostnogo-ohlazdenia/",
    "SSD":                "https://www.dns-shop.ru/catalog/8a9ddfba20724e77/ssd-nakopiteli/",
    "SSD M.2":            "https://www.dns-shop.ru/catalog/dd58148920724e77/ssd-m2-nakopiteli/",
}



@asynccontextmanager
async def lifespan(app: FastAPI):
    global cache

    print("\n" + "=" * 70)
    print(">>> [SYSTEM] PC BUILDER API ЗАПУЩЕН")
    print(f">>> [CONFIG] ПАРСЕР ВКЛЮЧЕН: {SHOULD_PARSE}")
    print("=" * 70)

    try:
        loop = asyncio.get_event_loop()
        loaded = await loop.run_in_executor(None, load_all_from_db)
        if loaded:
            cache = loaded
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

    if SHOULD_PARSE:
        asyncio.create_task(
            _parse_all_stores_async(),
            name="Parser-All-Stores"
        )
        print(">>> [►] Последовательная задача парсинга Citilink → Регард → DNS запущена")

    yield

    print(">>> [SYSTEM] Сервер остановлен")

def _has_parsed_value(value) -> bool:
    return value not in (None, "", "---", 0, [], {})


def _merge_parsed_item_into_cache(cached_item: dict, parsed_item: dict) -> None:
    """Доклеивает specs и совместимость магазина к уже существующему товару."""
    parsed_specs = parsed_item.get("specs") or {}
    if parsed_specs:
        cached_specs = cached_item.get("specs") or {}
        merged_specs = dict(cached_specs)
        merged_specs.update(parsed_specs)
        cached_item["specs"] = merged_specs

    for key in COMPAT_KEYS:
        value = parsed_item.get(key)
        if _has_parsed_value(value):
            cached_item[key] = value


def _merge_parsed_items_with_existing_cache(category: str, parsed_items: list[dict]) -> list[dict]:
    existing_items = cache.get(category, [])
    existing_names = [item.get("name", "") for item in existing_items if item.get("name")]
    existing_by_name = {item["name"]: item for item in existing_items if item.get("name")}

    merged: list[dict] = []
    for parsed_item in parsed_items:
        item = dict(parsed_item)
        item_name = item.get("name", "")
        matched_name = item_name if item_name in existing_by_name else matcher.find_match(item_name, existing_names)
        existing = existing_by_name.get(matched_name or "")

        if existing:
            item["id"] = existing.get("id", item.get("id"))
            if not item.get("imageUrl") and existing.get("imageUrl"):
                item["imageUrl"] = existing["imageUrl"]

            for key in ("priceRegard", "productUrlRegard", "priceDNS", "productUrlDNS"):
                if _has_parsed_value(existing.get(key)) and not _has_parsed_value(item.get(key)):
                    item[key] = existing[key]

            specs = dict(existing.get("specs") or {})
            specs.update(item.get("specs") or {})
            if specs:
                item["specs"] = specs

            for key in COMPAT_KEYS:
                if not _has_parsed_value(item.get(key)) and _has_parsed_value(existing.get(key)):
                    item[key] = existing[key]

        merged.append(item)

    return merged


async def _parse_all_stores_async() -> None:
    await _parse_store_async(URLS_CITILINK, "citilink")
    await asyncio.sleep(random.uniform(5.0, 9.0))
    await _parse_store_async(URLS_REGARD, "regard")
    await asyncio.sleep(random.uniform(5.0, 9.0))
    await _parse_store_async(URLS_DNS, "dns")


async def _parse_store_async(urls: dict, store_name: str) -> None:
    global cache

    if not urls:
        print(f">>> [PARSER:{store_name.upper()}] URL-список пуст, пропускаем.")
        return

    print(f"\n>>> [PARSER:{store_name.upper()}] Начинаю цикл парсинга...")

    for category, url in urls.items():
        try:
            print(f"\n>>> [PARSING:{store_name}] Обновляю: {category}")

            if store_name == "citilink":
                # scrape_citilink — async функция
                data = await parser_engine.scrape_citilink(url, category)

            elif store_name == "regard":
                # scrape_regard теперь тоже async — прямой await
                data = await parser_regard.scrape_regard(url, category)

            elif store_name == "dns":
                data = await parser_dns.scrape_dns(url, category)

            else:
                print(f"    [!] Неизвестный магазин: {store_name}")
                continue

            if not data:
                print(f"    [!] {category}: Нет данных")
                continue

            if store_name == "citilink":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, save_to_db, category, data, "citilink"
                )
                async with cache_lock:
                    cache[category] = _merge_parsed_items_with_existing_cache(category, data)
                print(f"    [✓] {category} (Ситилинк): {len(data)} товаров")

            elif store_name == "regard":
                await _save_regard_with_matching_async(category, data)
                print(f"    [✓] {category} (Регард): {len(data)} товаров обработано")

            elif store_name == "dns":
                await _save_dns_with_matching_async(category, data)
                print(f"    [✓] {category} (DNS): {len(data)} товаров обработано")

        except Exception as e:
            print(f"    [ERROR] Ошибка парсинга {category} ({store_name}): {e}")
            log.exception(
                "Детали ошибки парсинга %s / %s", store_name, category
            )

        await asyncio.sleep(random.uniform(3.0, 7.0))

    print(f"\n>>> [✓] ЦИКЛ ПАРСИНГА {store_name.upper()} ЗАВЕРШЁН\n")


async def _save_regard_with_matching_async(category: str, regard_items: list[dict]) -> None:
    global cache

    loop = asyncio.get_event_loop()

    matched_count = 0
    new_count     = 0

    async with cache_lock:
        existing_names = matcher.get_names_from_cache(cache, category)

    for item in regard_items:
        regard_name  = item.get("name", "")
        regard_price = item.get("priceRegard", "---")
        regard_url   = item.get("productUrl", "")

        matched_db_name = matcher.find_match(regard_name, existing_names)

        if matched_db_name:
            # Обновляем цену и характеристики
            await loop.run_in_executor(
                None, save_regard_price_to_db, matched_db_name, regard_price, regard_url
            )
            await loop.run_in_executor(
                None, save_store_specs_and_compat_to_db,
                matched_db_name, category, item, "regard"
            )

            # Обновляем кэш
            async with cache_lock:
                for cached_item in cache.get(category, []):
                    if cached_item["name"] == matched_db_name:
                        cached_item["priceRegard"]      = regard_price
                        cached_item["productUrlRegard"] = regard_url
                        _merge_parsed_item_into_cache(cached_item, item)
                        break

            matched_count += 1
        else:
            # Новый товар
            await loop.run_in_executor(
                None, save_to_db, category, [item], "regard"
            )
            async with cache_lock:
                cache.setdefault(category, []).append(item)
                existing_names.append(regard_name)

            new_count += 1

    print(
        f"    [Матчинг:{category}] "
        f"Сопоставлено: {matched_count}, "
        f"Новых: {new_count}"
    )


async def _save_dns_with_matching_async(category: str, dns_items: list[dict]) -> None:
    global cache

    loop = asyncio.get_event_loop()

    matched_count = 0
    new_count     = 0
    public_category = dns_items[0].get("category", category) if dns_items else category

    async with cache_lock:
        existing_names = matcher.get_names_from_cache(cache, public_category)

    for item in dns_items:
        item_category = item.get("category") or public_category
        dns_name      = item.get("name", "")
        dns_price     = item.get("priceDNS") or item.get("price") or "---"
        dns_url       = item.get("productUrlDNS") or item.get("productUrl", "")

        matched_db_name = matcher.find_match(dns_name, existing_names)

        if matched_db_name:
            await loop.run_in_executor(
                None, save_regard_price_to_db, matched_db_name, dns_price, dns_url, "dns"
            )
            await loop.run_in_executor(
                None, save_store_specs_and_compat_to_db,
                matched_db_name, item_category, item, "dns"
            )

            async with cache_lock:
                for cached_item in cache.get(item_category, []):
                    if cached_item["name"] == matched_db_name:
                        cached_item["priceDNS"]      = dns_price
                        cached_item["productUrlDNS"] = dns_url
                        _merge_parsed_item_into_cache(cached_item, item)
                        break

            matched_count += 1
        else:
            await loop.run_in_executor(
                None, save_to_db, item_category, [item], "dns"
            )
            async with cache_lock:
                cache.setdefault(item_category, []).append(item)
                existing_names.append(dns_name)

            new_count += 1

    print(
        f"    [Матчинг DNS:{public_category}] "
        f"Сопоставлено: {matched_count}, "
        f"Новых: {new_count}"
    )


app = FastAPI(
    title="PC Builder API",
    description="API для сборки ПК — Ситилинк + Регард + DNS, PostgreSQL",
    version="3.0",
    lifespan=lifespan
)

async def _find_component_by_id(component_id: int) -> dict | None:
    if component_id is None:
        return None
    async with cache_lock:
        for category_items in cache.values():
            for comp in category_items:
                if comp.get("id") == component_id:
                    return comp
    return None


async def _find_components_by_ids(ids: list[int]) -> list[dict]:
    result = []
    for cid in ids:
        item = await _find_component_by_id(cid)
        if item is not None:
            result.append(item)
        else:
            log.warning("Компонент с ID=%s не найден в кэше", cid)
    return result


@app.get("/components")
async def get_components(category: str):
    async with cache_lock:
        components = list(cache.get(category, []))
        if category == "Кулеры":
            components.extend(cache.get("СЖО", []))
        elif category == "SSD":
            components.extend(cache.get("SSD M.2", []))
    return {
        "category":   category,
        "count":      len(components),
        "components": components,
    }


@app.get("/compatibility-check")
async def check_compatibility(
    cpu_id:    Optional[int]       = Query(None),
    mb_id:     Optional[int]       = Query(None),
    gpu_id:    Optional[list[int]] = Query(None),
    ram_id:    Optional[list[int]] = Query(None),
    psu_id:    Optional[int]       = Query(None),
    case_id:   Optional[int]       = Query(None),
    cooler_id: Optional[int]       = Query(None),
    ssd_id:    Optional[list[int]] = Query(None),
):
    cpu    = await _find_component_by_id(cpu_id)
    mb     = await _find_component_by_id(mb_id)
    psu    = await _find_component_by_id(psu_id)
    case_  = await _find_component_by_id(case_id)
    cooler = await _find_component_by_id(cooler_id)
    gpus   = await _find_components_by_ids(gpu_id or [])
    rams   = await _find_components_by_ids(ram_id or [])
    ssds   = await _find_components_by_ids(ssd_id or [])

    components = {
        "cpu": cpu, "mb": mb, "gpu": gpus, "ram": rams,
        "psu": psu, "case": case_, "cooler": cooler, "ssd": ssds,
    }
    result = build_validator.check_compatibility(components)
    result["compatible"] = result["status"] != "CRITICAL"
    return result


@app.get("/health")
async def health():
    async with cache_lock:
        cats  = {k: len(v) for k, v in cache.items()}
        total = sum(cats.values())

        regard_prices = sum(
            1
            for items in cache.values()
            for item in items
            if item.get("priceRegard") and item["priceRegard"] != "---"
        )
        dns_prices = sum(
            1
            for items in cache.values()
            for item in items
            if item.get("priceDNS") and item["priceDNS"] != "---"
        )

    return {
        "status":            "ok",
        "components_cached": total,
        "regard_prices":     regard_prices,
        "dns_prices":        dns_prices,
        "categories":        cats,
        "parser_enabled":    SHOULD_PARSE,
        "stores":            ["citilink", "regard", "dns"],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
