"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

"""
parser_engine.py — парсер Citilink.
p.
"""

import asyncio
import json
import os
import re
import random
import logging
from camoufox.async_api import AsyncCamoufox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

PAGES_LIMIT       = None
PRODUCTS_PER_PAGE = None
BATCH_SIZE        = 6
SCROLL_STEPS      = 2
SCROLL_PAUSE      = 0.25
PAGE_LOAD_PAUSE   = 1.2
BATCH_LOAD_PAUSE  = 1.5
PAGE_DELAY_MIN    = 1.0
PAGE_DELAY_MAX    = 2.5

def _make_browser(block_images: bool = False, headless: bool = True) -> AsyncCamoufox:
    return AsyncCamoufox(
        headless=headless,
        os="windows",
        humanize=True,
        block_images=block_images,

    )


async def _close_browser_safe(browser, category_name: str) -> None:
    try:
        await browser.close()
    except Exception as e:
        err = str(e)
        if "Connection closed" in err or "Browser.close" in err:
            log.debug(
                "[%s] Браузер закрылся с ожидаемой ошибкой Node.js 24: %s",
                category_name, err
            )
        else:
            log.warning(
                "[%s] Неожиданная ошибка закрытия браузера: %s",
                category_name, e
            )


async def _scroll_page(page) -> None:
    try:
        await page.mouse.wheel(0, 900)
    except Exception:
        pass


async def _safe_close(obj) -> None:
    try:
        await obj.close()
    except Exception:
        pass


async def _ele(container, selector: str, timeout: float = 3.0):
    try:
        if timeout > 0.5:
            return await container.wait_for_selector(
                selector, timeout=int(timeout * 1000), state="attached"
            )
        return await container.query_selector(selector)
    except Exception:
        return None


async def _eles(container, selector: str) -> list:
    try:
        return await container.query_selector_all(selector)
    except Exception:
        return []


async def _attr(el, attr_name: str) -> str:
    try:
        return await el.get_attribute(attr_name) or ""
    except Exception:
        return ""


async def _text(el) -> str:
    try:
        return (await el.text_content() or "").strip()
    except Exception:
        return ""


def _setup_page_handlers(page) -> None:
    page.on("pageerror", lambda _: None)
    page.on("crash",     lambda _: None)


# ═════════════════════════ главная точка входа ═══════════════════════════════

async def scrape_citilink(url: str, category_name: str) -> list[dict]:
    log.info("[%s] Старт → %s", category_name, url)

    product_data = await _collect_catalog(url, category_name)
    if not product_data:
        log.warning("[%s] Каталог пуст", category_name)
        return []

    log.info("[%s] Фаза 2: характеристики %d товаров...", category_name, len(product_data))

    all_results: list[dict] = []
    for batch_start in range(0, len(product_data), BATCH_SIZE):
        batch = product_data[batch_start: batch_start + BATCH_SIZE]
        batch_results = await _process_batch(batch, category_name)
        all_results.extend(batch_results)

        if batch_start + BATCH_SIZE < len(product_data):
            await asyncio.sleep(random.uniform(0.8, 2.0))

    log.info("[%s] Готово. Итого: %d товаров", category_name, len(all_results))
    return all_results


async def _collect_catalog(url: str, category_name: str) -> list[dict]:
    product_data: list[dict] = []
    last_page_product_names: set[str] = set()

    browser_cm = _make_browser(block_images=False)

    try:
        browser = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[%s] Не удалось запустить браузер: %s", category_name, e)
        return product_data

    ctx  = None
    page = None

    try:
        ctx  = await browser.new_context()
        page = await ctx.new_page()
        _setup_page_handlers(page)

        current_page = 1
        while True:
            if PAGES_LIMIT is not None and current_page > PAGES_LIMIT:
                break

            target_url = (
                url if current_page == 1
                else f"{url.rstrip('/')}/?p={current_page}"
            )

            try:
                if page.is_closed():
                    log.warning("[%s] Страница закрыта, пересоздаём...", category_name)
                    page = await ctx.new_page()
                    _setup_page_handlers(page)
            except Exception:
                pass

            try:
                await page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(PAGE_LOAD_PAUSE)
            except Exception as e:
                log.error("[%s] Не удалось загрузить %s: %s", category_name, target_url, e)
                break

            # Подробный режим на первой странице
            if current_page == 1:
                try:
                    label = await page.wait_for_selector(
                        'label[for="Подробный режим каталога-list"]',
                        timeout=4000,
                        state="attached"
                    )
                    if label:
                        await label.click()
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

            log.info("[%s] Страница %d", category_name, current_page)

            for _ in range(SCROLL_STEPS):
                await _scroll_page(page)
                await asyncio.sleep(SCROLL_PAUSE)

            try:
                items = await page.query_selector_all(
                    '[data-meta-name="SnippetProductHorizontalLayout"]'
                )
                if not items:
                    items = await page.query_selector_all('[data-meta-product-id]')
            except Exception:
                items = []

            if not items:
                log.info(
                    "[%s] Стр.%d — товаров нет, конец каталога.",
                    category_name, current_page
                )
                break

            page_products: list[dict] = []
            current_page_names: set[str] = set()

            for item in items:
                try:
                    title_el = await item.query_selector(
                        '[data-meta-name="Snippet__title"]'
                    )
                    if not title_el:
                        continue

                    href = await title_el.get_attribute("href") or ""
                    name = (await title_el.text_content() or "").strip()

                    if not href or not name or len(name) < 5:
                        continue

                    current_page_names.add(name)

                    full_url = (
                        href if href.startswith("http")
                        else f"https://www.citilink.ru{href}"
                    )
                    if not full_url.endswith('/properties/'):
                        full_url = full_url.rstrip('/') + '/properties/'

                    # Изображение
                    image_url = ""
                    img_el = await item.query_selector("img")
                    if img_el:
                        src = (
                            await img_el.get_attribute("data-src") or
                            await img_el.get_attribute("src") or ""
                        )
                        if src.startswith("//"):
                            image_url = "https:" + src
                        elif src.startswith("/"):
                            image_url = "https://www.citilink.ru" + src
                        else:
                            image_url = src

                    page_products.append({
                        "name":  name,
                        "url":   full_url,
                        "image": image_url,
                    })
                except Exception:
                    pass

            if current_page > 1 and current_page_names == last_page_product_names:
                log.info(
                    "[%s] Стр.%d совпадает с предыдущей — конец списка.",
                    category_name, current_page
                )
                break

            last_page_product_names = current_page_names

            if PRODUCTS_PER_PAGE:
                page_products = page_products[:PRODUCTS_PER_PAGE]

            product_data.extend(page_products)
            log.info(
                "[%s] Стр.%d — %d товаров (итого: %d)",
                category_name, current_page, len(page_products), len(product_data)
            )

            current_page += 1
            await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    except Exception as e:
        log.error("[%s] Критическая ошибка каталога: %s", category_name, e)
    finally:
        if page:
            await _safe_close(page)
        if ctx:
            await _safe_close(ctx)
        await _close_browser_safe(browser, category_name)

    return product_data

async def _process_batch(product_data: list[dict], category_name: str) -> list[dict]:
    results: list[dict] = []

    browser_cm = _make_browser(block_images=True)

    try:
        browser = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[%s] Не удалось запустить браузер (батч): %s", category_name, e)
        return results

    ctx = None

    try:
        ctx = await browser.new_context()
        pages: list[dict] = []

        for p in product_data:
            try:
                tab = await ctx.new_page()
                _setup_page_handlers(tab)
                await tab.goto(p["url"], wait_until="commit")
                pages.append({"tab": tab, "product": p})
            except Exception as e:
                log.debug("Страница не открылась %s: %s", p.get("url", ""), e)

        await asyncio.sleep(BATCH_LOAD_PAUSE)

        for entry in pages:
            tab     = entry["tab"]
            product = entry["product"]
            try:
                try:
                    await tab.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass

                specs      = await _collect_specs(tab)
                price_text = await _collect_price(tab)
                extracted  = _extract_logic(category_name, product["name"], specs)

                results.append({
                    "id":            abs(hash(product["name"] + category_name)) % (10 ** 9),
                    "name":          product["name"],
                    "category":      category_name,
                    "priceCitilink": price_text,
                    "priceDNS":      "---",
                    "imageUrl":      product["image"],
                    "productUrl":    product["url"],
                    **extracted,
                    "specs":         specs,
                })
                log.info("  ✓ %s", product["name"][:55])
            except Exception as e:
                log.debug("Ошибка %s: %s", product["name"][:40], e)
            finally:
                await _safe_close(tab)

    except Exception as e:
        log.error("[%s] Критическая ошибка батча: %s", category_name, e)
    finally:
        if ctx:
            await _safe_close(ctx)
        await _close_browser_safe(browser, category_name)

    return results

async def _collect_specs(page) -> dict:
    specs = {}
    try:
        try:
            await page.wait_for_selector(
                '[class*="PropertiesItem"]', timeout=3000, state="attached"
            )
        except Exception:
            return specs

        rows = await page.query_selector_all('[class*="PropertiesItem"]')
        for row in rows:
            try:
                n = await row.query_selector('[class*="PropertiesName"]')
                v = await row.query_selector('[class*="PropertiesValue"]')
                if n and v:
                    key = (await n.text_content() or "").strip().rstrip(":")
                    val = (await v.text_content() or "").strip()
                    specs[key] = val
            except Exception:
                pass
    except Exception:
        pass
    return specs


async def _collect_price(page) -> str:
    try:
        el = await page.query_selector('[data-meta-name="PriceBlock__price"]')
        if el:
            text = (await el.text_content() or "").strip()
            digits = "".join(filter(str.isdigit, text))
            if digits:
                formatted = "{:,}".format(int(digits)).replace(",", " ")
                return f"{formatted} руб"
    except Exception:
        pass
    return "---"

def _empty_compat() -> dict:
    return {
        "socket":              "---",
        "chipset":             "---",
        "ramType":             "---",
        "ramSlots":            0,
        "ramMaxFreq":          0,
        "ramHeight":           0,
        "ramCapacity":         0,
        "tdp":                 0,
        "maxTdp":              0,
        "coolerHeight":        0,
        "psuWattage":          0,
        "psuFormFactor":       "---",
        "psuLength":           0,
        "psuEfficiency":       "---",
        "cpuPowerPin":         "---",
        "gpuPowerPin":         "---",
        "formFactor":          "---",
        "pciVersion":          "---",
        "m2Slots":             0,
        "m2Types":             [],
        "gpuLength":           0,
        "gpuHeight":           0,
        "gpuSlots":            0,
        "gpuTdp":              0,
        "gpuReqPsu":           0,
        "gpuPciVersion":       "---",
        "vram":                0,
        "gpuChipset":          "---",
        "maxGpuLength":        0,
        "maxCpuCoolerHeight":  0,
        "maxPsuLength":        0,
        "supportedMbFormats":  [],
        "ssdInterface":        "---",
        "ssdFormFactor":       "---",
        "ssdCapacityGb":       0,
    }


def _extract_logic(category: str, name: str, specs: dict) -> dict:
    if category == "СЖО":
        category = "Кулеры"
    elif category == "SSD M.2":
        category = "SSD"

    r  = _empty_compat()
    c  = {str(k).strip().lower(): str(v).strip() for k, v in specs.items()}
    cv = {k: v.lower() for k, v in c.items()}

    def val(key_fragment: str) -> str:
        for k, v in cv.items():
            if key_fragment in k:
                return v
        return ""

    def val_any(*key_fragments: str) -> str:
        for fragment in key_fragments:
            found = val(fragment)
            if found:
                return found
        return ""

    def val_all(*key_fragments: str) -> str:
        for k, v in cv.items():
            if all(fragment in k for fragment in key_fragments):
                return v
        return ""

    def mm(text: str) -> int:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мм|mm)", text, re.I)
        return int(float(m.group(1).replace(",", "."))) if m else 0

    def dimensions_depth_mm(text: str) -> int:
        numbers = re.findall(r"\d+(?:[.,]\d+)?", text or "")
        if len(numbers) < 3:
            return 0
        return int(float(numbers[2].replace(",", ".")))

    def val_dimensions(*key_fragments: str) -> str:
        key_fragments = tuple(fragment.replace("ё", "е") for fragment in key_fragments)
        for k, v in cv.items():
            norm_key = k.replace("ё", "е")
            if "упаков" in norm_key:
                continue
            if all(fragment in norm_key for fragment in key_fragments):
                return v
        return ""

    def watt(text: str) -> int:
        m = re.search(r"(\d{2,4})\s*(?:вт|w)\b", text, re.I)
        return int(m.group(1)) if m else 0

    def first_int(text: str, min_val: int = 0, max_val: int = 99999) -> int:
        for m in re.finditer(r"\d+", text):
            v = int(m.group())
            if min_val <= v <= max_val:
                return v
        return 0

    full = (name + " " + " ".join(cv.keys()) + " " + " ".join(cv.values())).lower()

    if category == "Процессоры":
        r["socket"]  = _find_socket(full)
        r["ramType"] = _find_ddr(full, r["socket"])
        r["tdp"]     = (
            watt(val_any("тепловыделение", "энергопотребление")) or
            watt(val("tdp")) or
            first_int(val("tdp"), 10, 500)
        )

    elif category == "Видеокарты":
        gpu_chipset = val_any(
            "видеочипсет",
            "графический процессор",
            "gpu",
            "модель графического процессора",
        )
        r["gpuChipset"] = gpu_chipset.split(",")[0].strip() or _find_gpu_chipset(name) or name[:80]

        vram_str = val_any("объем видеопамяти", "объем памяти", "видеопамять", "память")
        m = re.search(r"(\d+)\s*гб", vram_str, re.I)
        r["vram"] = int(m.group(1)) if m else 0

        r["gpuTdp"] = (
            watt(val_any("максимальное энергопотребление", "потребление", "энергопотребление")) or
            watt(val("tdp"))
        )
        r["gpuReqPsu"] = (
            watt(val_any("рекомендуемая мощность", "рекомендованная мощность")) or
            watt(val("рекомендовано")) or
            watt(val("питание"))
        )
        if r["gpuReqPsu"] == 0:
            r["gpuReqPsu"] = watt(full)

        pin_str = val_any("разъемы дополнительного питания", "дополнительное питание", "питание")
        r["gpuPowerPin"] = _parse_gpu_pin(pin_str)

        r["gpuLength"] = (
            mm(val_all("длина", "видеокарт")) or
            mm(val_all("длина", "граф")) or
            mm(val("длина")) or
            _find_gpu_length(full)
        )
        r["gpuHeight"] = mm(val_all("высота", "видеокарт")) or mm(val("высота"))

        slots_str = val_any("конструкция системы охлаждения", "занимаемых слотов", "слотов расширения")
        if "трёхслот" in slots_str or "трехслот" in slots_str or "3-slot" in slots_str:
            r["gpuSlots"] = 3
        elif "двухслот" in slots_str or "2-slot" in slots_str:
            r["gpuSlots"] = 2
        elif "однослот" in slots_str or "1-slot" in slots_str:
            r["gpuSlots"] = 1
        else:
            r["gpuSlots"] = first_int(slots_str, 1, 5)

        r["gpuPciVersion"] = _find_pci_version(full)

    elif category == "Материнские платы":
        r["socket"]     = _find_socket(full)
        r["formFactor"] = _find_form_factor(full)
        r["ramType"]    = _find_ddr(full)
        r["pciVersion"] = _find_pci_version(full)
        r["chipset"]    = val_any("чипсет", "набор системной логики").upper() or _find_chipset(full)

        slots_raw = (
            val("количество слотов памяти") or
            val("слотов оперативной памяти") or
            val("слотов памяти") or
            val("слоты памяти")
        )
        r["ramSlots"] = first_int(slots_raw, 1, 8)

        freq_raw = val_any("максимальная частота памяти", "частота памяти", "частота оперативной памяти")
        r["ramMaxFreq"] = first_int(freq_raw, 800, 12000)

        r["cpuPowerPin"] = _parse_cpu_pin(
            val_any("разъем питания процессора", "питание процессора", "cpu power")
        )

        m2_raw = (
            val("количество разъемов m.2") or
            val("разъемов m.2") or
            val("m.2")
        )
        r["m2Slots"] = first_int(m2_raw, 0, 8)
        r["m2Types"] = _find_m2_types(full)

    elif category == "Оперативная память":
        r["ramType"]     = _find_ddr(full)
        r["ramCapacity"] = first_int(val("объем") or val("память"), 1, 256)

        freq_raw = val("частота") or val("тактовая частота")
        r["ramMaxFreq"] = first_int(freq_raw, 800, 12000)

        height_raw = val("высота") or val("высота радиатора")
        r["ramHeight"] = mm(height_raw) or first_int(height_raw, 20, 80)

    elif category == "Блоки питания":
        r["psuWattage"] = watt(val("мощность")) or first_int(val("мощность"), 200, 3000)
        r["psuFormFactor"] = _find_psu_form_factor(full)
        r["formFactor"] = r["psuFormFactor"]
        r["psuLength"]  = (
            mm(val_all("глубина", "блока питания")) or
            mm(val_all("длина", "блока питания")) or
            dimensions_depth_mm(val_dimensions("размеры", "шхвхг")) or
            dimensions_depth_mm(val_dimensions("габариты", "шхвхг")) or
            mm(val("глубина"))
        )
        r["psuEfficiency"] = _find_psu_efficiency(full)

        r["cpuPowerPin"] = _parse_cpu_pin(
            val("разъем cpu") or
            val("разъемов cpu") or
            val("питания cpu") or
            val("разъем 8 pin")
        )
        gpu_pin_raw = (
            val("питание видеокарты") or
            val("разъем pcie") or
            val("разъемов pcie") or
            val("разъем 6+2") or
            val("12vhpwr") or
            val("разъем 16") or
            val("разъемы")
        )
        r["gpuPowerPin"] = _parse_gpu_pin(gpu_pin_raw)

    elif category == "Корпуса":
        case_ff_raw = (
            val_all("форм", "материн") or
            val("форм-фактор совместимых") or
            val("типоразмер") or
            val("форм-фактор")
        )
        r["formFactor"]         = _find_form_factor(case_ff_raw)
        r["maxGpuLength"]       = (
            mm(val_all("длина", "видеокарт")) or
            mm(val_all("длина", "граф")) or
            mm(val("макс. длина видеокарты"))
        )
        r["maxCpuCoolerHeight"] = (
            mm(val_all("высота", "кулер")) or
            mm(val("макс. высота кулера")) or
            mm(val("высота процессорного кулера"))
        )
        r["maxPsuLength"] = (
            mm(val_all("длина", "блока питания")) or
            mm(val_all("глубина", "блока питания"))
        )

        mb_fmt_raw = (
            val_all("форм", "материн") or
            val("форм-фактор совместимых") or
            val("совместимые мп") or
            val("форм-фактор")
        )
        r["supportedMbFormats"] = _find_supported_mb_formats(mb_fmt_raw)

    elif category == "Кулеры":
        compat_raw = val_any("совместимость", "сокет", "поддерживаемые платформы")
        r["socket"]       = _find_all_sockets(compat_raw) if compat_raw else _find_socket(full)
        r["maxTdp"]       = (
            watt(val("рассеиваемая мощность")) or
            watt(val("tdp")) or
            first_int(val("tdp"), 30, 500)
        )
        r["coolerHeight"] = mm(val_all("высота", "кулер")) or mm(val("высота"))

    elif category == "SSD":
        r["ssdInterface"]  = _find_ssd_interface(full)
        r["ssdFormFactor"] = _find_ssd_form_factor(full)

        cap_str = val_any("объем", "ёмкость", "емкость", "объём")
        r["ssdCapacityGb"] = _parse_capacity_gb(cap_str or full)

    return r

def _normalize_lga(text: str) -> str:
    return re.sub(r'(?i)lga\s+(\d+)', r'LGA\1', text)


def _find_socket(text: str) -> str:
    norm = _normalize_lga(text).upper()
    for s in [
        "AM5", "AM4", "LGA1851", "LGA1700", "LGA1200",
        "LGA2066", "LGA2011", "LGA1366", "TR5", "SP3"
    ]:
        if s in norm:
            return s
    return "---"


def _find_all_sockets(text: str) -> str:
    norm = _normalize_lga(text).upper()
    candidates = [
        "AM5", "AM4", "AM3+", "AM3", "AM2+", "AM2",
        "FM2+", "FM2", "FM1",
        "LGA1851", "LGA1700", "LGA1200", "LGA2066",
        "LGA2011", "LGA1366", "LGA1156", "LGA1155", "LGA1151", "LGA1150",
        "TR5", "SP3",
    ]
    found = [s for s in candidates if s in norm]
    return ",".join(found) if found else "---"


def _find_ddr(text: str, socket: str = "") -> str:
    if "ddr5" in text or socket in ("AM5", "LGA1851"):
        return "DDR5"
    if "ddr4" in text or socket == "AM4":
        return "DDR4"
    return "---"


def _find_pci_version(text: str) -> str:
    m = re.search(r"pci[\s\-e]*(?:express)?[\s\-]*(\d+(?:\.\d+)?)", text, re.I)
    if m:
        ver = m.group(1)
        if "." not in ver:
            ver += ".0"
        return ver
    return "---"


def _find_form_factor(text: str) -> str:
    for label, patterns in [
        ("E-ATX",    [r"e-atx"]),
        ("Mini-ITX", [r"mini-itx"]),
        ("mATX",     [r"\bmatx\b", r"micro[-\s]?atx", r"\bm-atx\b"]),
        ("Flex-ATX", [r"flex-atx"]),
        ("ATX",      [r"(^|[,\s;/])atx($|[,\s;/])"]),
    ]:
        for p in patterns:
            if re.search(p, text, re.IGNORECASE):
                return label
    return "---"


def _find_psu_form_factor(text: str) -> str:
    if re.search(r"sfx[\s-]?l", text, re.I):
        return "SFX-L"
    if re.search(r"\bsfx\b", text, re.I):
        return "SFX"
    if re.search(r"\batx\b", text, re.I):
        return "ATX"
    return "---"


def _find_supported_mb_formats(text: str) -> list[str]:
    found = []
    mapping = [
        ("E-ATX",    r"e-atx"),
        ("Mini-ITX", r"mini-itx"),
        ("mATX",     r"\bmatx\b|micro[-\s]?atx|\bm-atx\b"),
        ("ATX",      r"(^|[,\s;/])atx($|[,\s;/])"),
    ]
    for label, pattern in mapping:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(label)
    return found


def _find_chipset(text: str) -> str:
    norm = text.upper()
    chipsets = [
        "X870E", "X870", "X670E", "X670", "B850", "B650E", "B650", "A620",
        "X570", "B550", "A520", "X470", "B450", "A320", "X370", "B350",
        "Z890", "B860", "H810", "Z790", "Z690", "H770", "H670", "B760",
        "B660", "H610", "Z590", "Z490", "H570", "H510", "B560", "B460",
        "W790", "W680", "TRX50", "TRX40",
    ]
    for chipset in chipsets:
        if re.search(rf"\b{re.escape(chipset)}\b", norm):
            return chipset
    return "---"


def _find_gpu_chipset(text: str) -> str:
    patterns = [
        r"\b(?:geforce\s+)?rtx\s*\d{3,4}(?:\s*(?:super|ti))?\b",
        r"\b(?:geforce\s+)?gtx\s*\d{3,4}(?:\s*ti)?\b",
        r"\bradeon\s+rx\s*\d{3,4}(?:\s*xt)?\b",
        r"\brx\s*\d{3,4}(?:\s*xt)?\b",
        r"\barc\s+[ab]\d{3,4}\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    return "---"


def _find_psu_efficiency(text: str) -> str:
    m = re.search(r"80\s*\+?\s*(white|bronze|silver|gold|platinum|titanium)", text, re.I)
    if m:
        return m.group(1).capitalize()
    for label in ("Bronze", "Silver", "Gold", "Platinum", "Titanium"):
        if re.search(rf"\b{label}\b", text, re.I):
            return label
    return "---"


def _parse_cpu_pin(text: str) -> str:
    text = text.lower().strip()
    if not text or text == "---":
        return "---"
    m = re.search(r"(\d+)\s*[x×]\s*(4\s*\+\s*4|8)", text)
    if m:
        count = int(m.group(1))
        unit = "8" if "8" in m.group(2) else "4+4"
        if unit in ("8", "4+4"):
            return "+".join(["8"] * count) + " pin"
    if re.search(r"8\s*\+\s*8", text):
        return "8+8 pin"
    if re.search(r"8\s*\+\s*4", text):
        return "8+4 pin"
    if re.search(r"4\s*\+\s*4", text):
        return "4+4 pin"
    if re.search(r"\b8\b", text):
        return "8 pin"
    if re.search(r"\b4\b", text):
        return "4 pin"
    return text[:30]


def _parse_gpu_pin(text: str) -> str:
    text = text.lower().strip()
    if not text or text in ("---", "без дополнительного питания"):
        return "без питания"
    if re.search(r"12vhpwr|12\s*v\s*hpwr|16\s*pin", text):
        return "12VHPWR (16 pin)"
    m = re.search(r"(\d+)\s*[x×]\s*\(?\s*(6\s*\+\s*2|8|6)\s*\)?", text)
    if m:
        count = int(m.group(1))
        unit_raw = m.group(2).replace(" ", "")
        unit = "8" if unit_raw in ("6+2", "8") else "6"
        return "+".join([unit] * count) + " pin"
    if re.search(r"8\s*\+\s*8\s*\+\s*8", text):
        return "8+8+8 pin"
    if re.search(r"8\s*\+\s*8", text):
        return "8+8 pin"
    if re.search(r"8\s*\+\s*6", text):
        return "8+6 pin"
    if re.search(r"\b8\b", text):
        return "8 pin"
    if re.search(r"6\s*\+\s*2", text):
        return "6+2 pin"
    if re.search(r"\b6\b", text):
        return "6 pin"
    return text[:30]


def _find_m2_types(text: str) -> list[str]:
    types = []
    if "nvme" in text:
        types.append("NVMe")
    if re.search(r"\bsata\b", text, re.I) and "m.2" in text:
        types.append("SATA")
    return types or ["NVMe"]


def _find_ssd_interface(text: str) -> str:
    if "nvme" in text:
        return "NVMe"
    if re.search(r"\bsata\b", text, re.I):
        return "SATA"
    if "pcie" in text or "pci-e" in text:
        return "NVMe"
    return "---"


def _find_ssd_form_factor(text: str) -> str:
    m = re.search(r"m\.2\s*(\d{4})", text, re.I)
    if m:
        return f"M.2 {m.group(1)}"
    if "m.2" in text:
        return "M.2"
    if re.search(r"2\.5", text):
        return '2.5"'
    if re.search(r"3\.5", text):
        return '3.5"'
    return "---"


def _parse_capacity_gb(text: str) -> int:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(тб|tb)", text, re.I)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1024)
    m = re.search(r"(\d+)\s*(гб|gb)", text, re.I)
    if m:
        return int(m.group(1))
    return 0


def _find_gpu_length(text: str) -> int:
    lengths = [
        int(x) for x in re.findall(r"(\d{3})\s*(?:мм|mm)", text)
        if 140 < int(x) < 500
    ]
    return max(lengths) if lengths else 0
