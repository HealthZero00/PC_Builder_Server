"""
parser_engine.py  —  оптимизирован под VPS 1 vCPU / 1 GB RAM
─────────────────────────────────────────────────────────────
Архитектура:
  • ОДИН браузер на категорию — никаких дополнительных процессов Chrome
  • ДВЕ фазы в одном браузере:
      1) Обход страниц каталога → собираем name, URL, imageUrl
      2) Последовательный обход страниц /properties/ → specs + price
  • Картинки в браузере ОТКЛЮЧЕНЫ (экономим RAM/трафик),
    но src-атрибут в DOM всё равно есть → imageUrl извлекается корректно
  • Чекпойнт: сохраняем в JSON каждые SAVE_EVERY товаров
"""

import time
import json
import os
import re
import logging
from DrissionPage import ChromiumPage, ChromiumOptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────── конфиг ───────────────────────────
PAGES_LIMIT          = 20    # макс. страниц каталога на категорию
PRODUCTS_PER_PAGE    = 36    # товаров со страницы (None = все)
SCROLL_STEPS         = 2     # прокруток для lazy-load
SCROLL_PAUSE         = 0.4   # сек между прокрутками
CATALOG_PAGE_PAUSE   = 1.5   # сек после загрузки страницы каталога
PRODUCT_PAGE_PAUSE   = 1.2   # сек после загрузки страницы товара
SAVE_EVERY           = 15    # сохранять промежуточный результат каждые N товаров
BROWSER_PATH         = "/usr/bin/chromium-browser"
# ──────────────────────────────────────────────────────────


def _make_options() -> ChromiumOptions:
    co = ChromiumOptions()
    co.auto_port()                        # уникальный порт — без конфликтов
    co.set_browser_path(BROWSER_PATH)
    co.mute(True)
    co.incognito(True)

    # обязательно для VPS/root
    co.set_argument("--headless")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-setuid-sandbox")

    # экономия памяти
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-extensions")
    co.set_argument("--disable-background-networking")
    co.set_argument("--disable-default-apps")
    co.set_argument("--disable-sync")
    co.set_argument("--no-first-run")
    co.set_argument("--disable-blink-features=AutomationControlled")

    # ограничиваем V8 heap (JS движок) — критично при 1 GB RAM
    co.set_argument("--js-flags=--max-old-space-size=256")

    # eager = не ждём полной загрузки, достаточно DOM
    co.set_argument("--page-load-strategy=eager")

    # КАРТИНКИ ОТКЛЮЧЕНЫ для экономии RAM и трафика.
    # Важно: src-атрибут в DOM всё равно проставляется JS,
    # поэтому imageUrl мы извлекаем корректно.
    co.set_pref("profile.managed_default_content_settings.images", 2)

    return co


def _safe_quit(page: ChromiumPage) -> None:
    try:
        page.quit()
    except Exception:
        pass


def _is_alive(page: ChromiumPage) -> bool:
    try:
        _ = page.title
        return True
    except Exception:
        return False


def _reconnect(page: ChromiumPage) -> ChromiumPage:
    """Закрывает старый браузер и возвращает новый."""
    log.warning("Переподключение браузера...")
    _safe_quit(page)
    time.sleep(1)
    return ChromiumPage(_make_options())


# ═══════════════════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ
# ═══════════════════════════════════════════════════════════

def scrape_citilink(url: str, category_name: str,
                    checkpoint_cb=None) -> list:
    """
    checkpoint_cb(results_so_far) — опциональный колбэк для промежуточного
    сохранения (вызывается каждые SAVE_EVERY товаров).
    """
    results = []
    page = ChromiumPage(_make_options())

    try:
        # ══ ФАЗА 1: обход каталога, сбор URL + imageUrl ══════════════════
        products = _collect_catalog(page, url, category_name)
        log.info("[%s] Всего товаров в очереди: %d", category_name, len(products))

        # ══ ФАЗА 2: последовательный обход страниц характеристик ══════════
        for i, prod in enumerate(products):
            if not _is_alive(page):
                page = _reconnect(page)

            try:
                page.get(prod["url"])
                time.sleep(PRODUCT_PAGE_PAUSE)

                specs = _collect_specs(page)
                price = _collect_price(page)
                extracted = _extract_logic(category_name, prod["name"], specs)

                results.append({
                    "id":            abs(hash(prod["name"] + category_name)) % (10 ** 9),
                    "name":          prod["name"],
                    "category":      category_name,
                    "priceCitilink": price,
                    "priceDNS":      "---",
                    "imageUrl":      prod["image"],
                    "productUrl":    prod["url"],
                    **extracted,
                    "specs":         specs,
                })
                log.info("  [%d/%d] %s", i + 1, len(products), prod["name"][:55])

            except Exception as e:
                log.debug("  [skip] %s: %s", prod["name"][:40], e)

            # чекпойнт — не теряем данные при краше
            if checkpoint_cb and (i + 1) % SAVE_EVERY == 0:
                checkpoint_cb(results)

    except Exception as e:
        log.error("[%s] Критическая ошибка: %s", category_name, e)
    finally:
        _safe_quit(page)

    log.info("[%s] Готово: %d товаров", category_name, len(results))
    return results


# ═══════════════════════════════════════════════════════════
#  ФАЗА 1 — каталог
# ═══════════════════════════════════════════════════════════

def _collect_catalog(page: ChromiumPage, base_url: str,
                     category_name: str) -> list:
    all_products = []

    for page_num in range(1, PAGES_LIMIT + 1):
        target = base_url if page_num == 1 else f"{base_url.rstrip('/')}/?p={page_num}"

        if not _is_alive(page):
            page = _reconnect(page)

        try:
            page.get(target)
            time.sleep(CATALOG_PAGE_PAUSE)
        except Exception as e:
            log.error("[%s] Не загрузить стр.%d: %s", category_name, page_num, e)
            break

        # переключаем в подробный режим на первой странице
        if page_num == 1:
            try:
                lbl = page.ele('css:label[for="Подробный режим каталога-list"]', timeout=3)
                if lbl:
                    lbl.click()
                    time.sleep(1.0)
            except Exception:
                pass

        # прокрутка — lazy-load изображений
        for _ in range(SCROLL_STEPS):
            page.scroll.down(900)
            time.sleep(SCROLL_PAUSE)

        items = (
            page.eles('css:[data-meta-name="SnippetProductHorizontalLayout"]')
            or page.eles('css:[data-meta-product-id]')
        )
        if not items:
            log.info("[%s] Конец каталога на стр.%d", category_name, page_num)
            break

        page_products = []
        for item in items:
            try:
                title_el = item.ele('css:[data-meta-name="Snippet__title"]', timeout=1)
                if not title_el:
                    continue
                href = title_el.attr("href") or ""
                name = (title_el.text or "").strip()
                if not href or len(name) < 5:
                    continue

                prod_url = href if href.startswith("http") else f"https://www.citilink.ru{href}"
                # сразу ведём на страницу характеристик — один переход вместо двух
                if not prod_url.endswith("/properties/"):
                    prod_url = prod_url.rstrip("/") + "/properties/"

                # imageUrl: src-атрибут есть в DOM даже при отключённой загрузке картинок
                image_url = ""
                img_el = item.ele("css:img", timeout=0.3)
                if img_el:
                    src = (img_el.attr("data-src")    # lazy-load атрибут
                           or img_el.attr("src") or "")
                    if src.startswith("//"):
                        image_url = "https:" + src
                    elif src.startswith("/"):
                        image_url = "https://www.citilink.ru" + src
                    else:
                        image_url = src

                page_products.append({"name": name, "url": prod_url, "image": image_url})
            except Exception:
                pass

        if PRODUCTS_PER_PAGE:
            page_products = page_products[:PRODUCTS_PER_PAGE]

        log.info("[%s] Стр.%d → %d товаров", category_name, page_num, len(page_products))
        all_products.extend(page_products)
        time.sleep(0.8)

    return all_products


# ═══════════════════════════════════════════════════════════
#  СБОР ДАННЫХ СО СТРАНИЦЫ ТОВАРА
# ═══════════════════════════════════════════════════════════

def _collect_specs(page: ChromiumPage) -> dict:
    specs = {}
    try:
        rows = page.eles('css:[class*="PropertiesItem"]', timeout=2)
        for row in rows:
            try:
                n = row.ele('css:[class*="PropertiesName"]', timeout=0.1)
                v = row.ele('css:[class*="PropertiesValue"]', timeout=0.1)
                if n and v:
                    specs[n.text.strip().rstrip(":")] = v.text.strip()
            except Exception:
                pass
    except Exception:
        pass
    return specs


def _collect_price(page: ChromiumPage) -> str:
    try:
        el = page.ele('css:[data-meta-name="PriceBlock__price"]', timeout=0.5)
        if el:
            digits = "".join(filter(str.isdigit, el.text or ""))
            if digits:
                return "{:,}".format(int(digits)).replace(",", " ") + " руб"
    except Exception:
        pass
    return "---"


# ═══════════════════════════════════════════════════════════
#  ИЗВЛЕЧЕНИЕ ПОЛЕЙ СОВМЕСТИМОСТИ
# ═══════════════════════════════════════════════════════════

def _empty_compat() -> dict:
    return {
        # CPU
        "socket":                "---",
        "tdp":                   0,
        "ramType":               "---",
        "cpuOfficialMaxRamFreq": 0,
        "hasIGPU":               False,
        "cpuCores":              0,
        # MB
        "formFactor":            "---",
        "ramSlots":              0,
        "ramMaxFreq":            0,
        "maxRamCapacityGb":      0,
        "xmpSupport":            False,
        "cpuPowerPin":           "---",
        "pciVersion":            "---",
        "m2Slots":               0,
        "m2Types":               [],
        "sataPortCount":         0,
        "sataDisabledWithM2":    False,
        "fanHeaders":            0,
        # RAM
        "ramCapacity":           0,
        "ramSticks":             1,
        "ramHeight":             0,
        # GPU
        "gpuChipset":            "---",
        "vram":                  0,
        "gpuTdp":                0,
        "gpuReqPsu":             0,
        "gpuPowerPin":           "---",
        "gpuPowerPinCount":      0,
        "gpuLength":             0,
        "gpuHeight":             0,
        "gpuSlots":              0.0,
        "gpuPciVersion":         "---",
        # PSU
        "psuWattage":            0,
        "psuFormFactor":         "---",
        "psuLength":             0,
        "cpuCableCpuCount":      0,
        "gpuCableCount":         0,
        "psuModular":            "---",
        "psuCertification":      "---",
        # Case
        "supportedMbFormats":    [],
        "maxGpuLength":          0,
        "maxGpuSlots":           0.0,
        "maxCpuCoolerHeight":    0,
        "maxPsuLength":          0,
        "maxRadiatorSizes":      [],
        "caseFanSlots":          0,
        "includedFans":          0,
        # Cooler
        "maxTdp":                0,
        "coolerHeight":          0,
        "coolerWidth":           0,
        "coolerType":            "---",
        "aioRadiatorSize":       0,
        "coolerFanSize":         0,
        "coolerFanCount":        0,
        # SSD
        "ssdInterface":          "---",
        "ssdFormFactor":         "---",
        "ssdCapacityGb":         0,
        "ssdKeyType":            "---",
        "ssdPciVersion":         "---",
    }


def _extract_logic(category: str, name: str, specs: dict) -> dict:
    r  = _empty_compat()
    c  = {str(k).strip().lower(): str(v).strip()  for k, v in specs.items()}
    cv = {k: v.lower()                             for k, v in c.items()}
    full = (name + " " + " ".join(cv.keys()) + " " + " ".join(cv.values())).lower()

    def val(*frags):
        for f in frags:
            for k, v in cv.items():
                if f in k: return v
        return ""

    def mm(t):
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мм|mm)", t, re.I)
        return int(float(m.group(1).replace(",", "."))) if m else 0

    def watt(t):
        m = re.search(r"(\d{2,4})\s*(?:вт|w)\b", t, re.I)
        return int(m.group(1)) if m else 0

    def rint(t, lo=0, hi=99999):
        for m in re.finditer(r"\d+", t):
            v = int(m.group())
            if lo <= v <= hi: return v
        return 0

    # ── Процессоры ────────────────────────────────────────
    if category == "Процессоры":
        r["socket"]  = _find_socket(full)
        r["ramType"] = _find_ddr(full, r["socket"])
        r["tdp"]     = watt(val("тепловыделение", "tdp")) or rint(val("тепловыделение", "tdp"), 10, 500)
        r["cpuOfficialMaxRamFreq"] = rint(val("частота памяти", "поддерживаемая частота"), 1600, 8000)
        r["hasIGPU"] = any(x in full for x in ("uhd graphics", "radeon graphics", "iris xe", "встроенное"))
        r["cpuCores"] = rint(val("количество ядер", "ядер"), 1, 256)

    # ── Видеокарты ────────────────────────────────────────
    elif category == "Видеокарты":
        r["gpuChipset"]  = c.get("видеочипсет", "").split(",")[0].strip() or name
        m_vram = re.search(r"(\d+)\s*гб", val("объем видеопамяти", "память"), re.I)
        r["vram"]        = int(m_vram.group(1)) if m_vram else 0
        r["gpuTdp"]      = watt(val("максимальное энергопотребление", "энергопотребление", "tdp"))
        r["gpuReqPsu"]   = watt(val("рекомендуемая мощность", "рекомендовано")) or watt(val("питание")) or watt(full)
        pin_raw          = val("разъемы дополнительного питания", "питание")
        r["gpuPowerPin"] = _parse_gpu_pin(pin_raw)
        r["gpuPowerPinCount"] = _count_gpu_pins(pin_raw)
        r["gpuLength"]   = mm(val("длина видеокарты")) or _find_gpu_length(full)
        r["gpuHeight"]   = mm(val("высота видеокарты"))
        r["gpuSlots"]    = _parse_gpu_slots(val("конструкция системы охлаждения"), full)
        r["gpuPciVersion"] = _find_pci_version(full)

    # ── Материнские платы ─────────────────────────────────
    elif category == "Материнские платы":
        r["socket"]     = _find_socket(full)
        r["formFactor"] = _find_form_factor(full)
        r["ramType"]    = _find_ddr(full)
        r["pciVersion"] = _find_pci_version(full)
        r["ramSlots"]   = rint(val("количество слотов памяти", "слотов памяти"), 1, 8)
        r["ramMaxFreq"] = rint(val("максимальная частота памяти", "частота памяти"), 800, 12000)
        r["maxRamCapacityGb"] = rint(val("максимальный объем памяти", "макс. объем"), 4, 2048)
        r["xmpSupport"] = any(x in full for x in ("xmp", "docp", "expo"))
        r["cpuPowerPin"]= _parse_cpu_pin(val("разъем питания процессора", "питание процессора"))
        r["m2Slots"]    = rint(val("количество разъемов m.2", "разъемов m.2", "m.2"), 0, 8)
        r["m2Types"]    = _find_m2_types(full)
        r["sataPortCount"] = rint(val("количество разъемов sata", "sata"), 0, 12)
        r["sataDisabledWithM2"] = "отключается" in val("m.2")
        fan_raw         = val("разъемов для вентиляторов", "fan header")
        r["fanHeaders"] = rint(fan_raw, 0, 16)

    # ── Оперативная память ────────────────────────────────
    elif category == "Оперативная память":
        r["ramType"]     = _find_ddr(full)
        r["ramCapacity"] = rint(val("объем", "память"), 1, 256)
        r["ramMaxFreq"]  = rint(val("частота", "тактовая частота"), 800, 12000)
        r["xmpSupport"]  = any(x in full for x in ("xmp", "docp", "expo"))
        h_raw = val("высота", "высота радиатора")
        r["ramHeight"]   = mm(h_raw) or rint(h_raw, 20, 80)
        kit_m = re.search(r"(\d+)\s*x\s*\d+|kit\s*of\s*(\d+)|(\d+)\s*шт", name, re.I)
        if kit_m:
            r["ramSticks"] = int(kit_m.group(1) or kit_m.group(2) or kit_m.group(3))

    # ── Блоки питания ─────────────────────────────────────
    elif category == "Блоки питания":
        r["psuWattage"]    = watt(val("мощность")) or rint(val("мощность"), 200, 3000)
        r["psuFormFactor"] = _find_psu_form_factor(full)
        r["psuLength"]     = mm(val("глубина", "длина")) or rint(val("глубина"), 50, 350)
        cpu_raw = val("разъем cpu", "разъемов cpu", "питания cpu", "разъем 8 pin")
        r["cpuPowerPin"]      = _parse_cpu_pin(cpu_raw)
        r["cpuCableCpuCount"] = rint(cpu_raw, 1, 4) or (1 if cpu_raw else 0)
        gpu_raw = val("разъем pcie", "разъемов pcie", "разъем 6+2", "12vhpwr", "разъем 16")
        r["gpuPowerPin"]  = _parse_gpu_pin(gpu_raw)
        r["gpuCableCount"]= rint(gpu_raw, 1, 12) or len(re.findall(r"6\+2|8.pin|16.pin", gpu_raw))
        mod_raw = val("модульность", "кабельная система")
        r["psuModular"]   = ("Full" if any(x in mod_raw for x in ("полностью", "full"))
                             else "Semi" if "semi" in mod_raw or "полу" in mod_raw
                             else "Non" if mod_raw else "---")
        r["psuCertification"] = _parse_80plus(full)

    # ── Корпуса ───────────────────────────────────────────
    elif category == "Корпуса":
        mb_fmt_raw = val("форм-фактор совместимых", "совместимые мп", "форм-фактор")
        r["formFactor"]        = _find_form_factor(mb_fmt_raw)
        r["supportedMbFormats"]= _find_supported_mb_formats(mb_fmt_raw)
        r["maxGpuLength"]      = mm(val("длина видеокарты", "макс. длина видеокарты"))
        r["maxGpuSlots"]       = _parse_gpu_slots(val("толщина видеокарты"), "")
        r["maxCpuCoolerHeight"]= mm(val("высота кулера", "макс. высота кулера", "высота процессорного кулера"))
        r["maxPsuLength"]      = mm(val("длина блока питания", "глубина блока питания"))
        r["maxRadiatorSizes"]  = _find_radiator_sizes(full)
        r["caseFanSlots"]      = rint(val("мест для вентиляторов", "мест под вентиляторы"), 0, 20)
        r["includedFans"]      = rint(val("вентиляторов в комплекте", "количество вентиляторов"), 0, 20)

    # ── Кулеры ────────────────────────────────────────────
    elif category == "Кулеры":
        r["socket"]      = _find_all_sockets(val("совместимость", "сокет") or full)
        r["maxTdp"]      = watt(val("рассеиваемая мощность", "tdp")) or rint(val("tdp"), 30, 600)
        r["coolerHeight"]= mm(val("высота", "высота кулера")) or rint(val("высота"), 50, 200)
        r["coolerWidth"] = mm(val("ширина")) or rint(val("ширина"), 40, 200)
        r["coolerType"]  = ("AIO" if any(x in full for x in ("жидкост", "aio", "water", "liquid"))
                            else "Air")
        if r["coolerType"] == "AIO":
            r["aioRadiatorSize"] = _find_radiator_size_single(full)
        r["coolerFanSize"]  = mm(val("размер вентилятора", "диаметр вентилятора"))
        r["coolerFanCount"] = rint(val("количество вентиляторов"), 0, 6)

    # ── SSD ───────────────────────────────────────────────
    elif category == "SSD":
        r["ssdInterface"]  = _find_ssd_interface(full)
        r["ssdFormFactor"] = _find_ssd_form_factor(full)
        r["ssdCapacityGb"] = _parse_capacity_gb(val("объем", "ёмкость", "емкость") or full)
        r["ssdKeyType"]    = _find_m2_key(full)
        r["ssdPciVersion"] = _find_pci_version(full) if r["ssdInterface"] == "NVMe" else "---"

    return r


# ═══════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════

def _normalize_lga(t): return re.sub(r"(?i)lga\s+(\d+)", r"LGA\1", t)

def _find_socket(text):
    n = _normalize_lga(text).upper()
    for s in ["AM5","AM4","LGA1851","LGA1700","LGA1200","LGA2066","LGA2011","LGA1366","TR5","SP3"]:
        if s in n: return s
    return "---"

def _find_all_sockets(text):
    n = _normalize_lga(text).upper()
    order = ["AM5","AM4","AM3+","AM3","AM2+","FM2+","FM2","FM1",
             "LGA1851","LGA1700","LGA1200","LGA2066","LGA2011","LGA1366",
             "LGA1156","LGA1155","LGA1151","LGA1150","TR5","SP3"]
    found = [s for s in order if s in n]
    return ",".join(found) if found else "---"

def _find_ddr(text, socket=""):
    if "ddr5" in text or socket in ("AM5","LGA1851"): return "DDR5"
    if "ddr4" in text or socket == "AM4":             return "DDR4"
    return "---"

def _find_pci_version(text):
    m = re.search(r"pci[\s\-e]*(?:express)?[\s\-]*(\d+(?:\.\d+)?)", text, re.I)
    if m:
        v = m.group(1)
        return v if "." in v else v + ".0"
    return "---"

def _find_form_factor(text):
    for label, pats in [("E-ATX",[r"e-atx"]),("ATX",[r"\batx\b"]),
                         ("mATX",[r"matx",r"micro-atx",r"m-atx"]),
                         ("Mini-ITX",[r"mini-itx"]),("Flex-ATX",[r"flex-atx"])]:
        for p in pats:
            if re.search(p, text, re.I): return label
    return "---"

def _find_psu_form_factor(text):
    if re.search(r"sfx[\s-]?l", text, re.I):  return "SFX-L"
    if re.search(r"\bsfx\b", text, re.I):      return "SFX"
    if re.search(r"\batx\b", text, re.I):      return "ATX"
    return "---"

def _find_supported_mb_formats(text):
    found = []
    for label, pat in [("E-ATX",r"e-atx"),("ATX",r"\batx\b"),
                        ("mATX",r"matx|micro-atx|m-atx"),("Mini-ITX",r"mini-itx")]:
        if re.search(pat, text, re.I): found.append(label)
    return found

def _parse_cpu_pin(t):
    t = t.lower().strip()
    if not t or t == "---": return "---"
    if re.search(r"8\s*\+\s*8", t): return "8+8 pin"
    if re.search(r"8\s*\+\s*4", t): return "8+4 pin"
    if re.search(r"4\s*\+\s*4", t): return "4+4 pin"
    if re.search(r"\b8\b", t):       return "8 pin"
    if re.search(r"\b4\b", t):       return "4 pin"
    return t[:30]

def _parse_gpu_pin(t):
    t = t.lower().strip()
    if not t or "без дополнительного питания" in t: return "без питания"
    if re.search(r"12vhpwr|12\s*v\s*hpwr|16\s*pin", t): return "12VHPWR (16 pin)"
    if re.search(r"8\s*\+\s*8\s*\+\s*8", t): return "8+8+8 pin"
    if re.search(r"8\s*\+\s*8", t):           return "8+8 pin"
    if re.search(r"8\s*\+\s*6|6\s*\+\s*8", t): return "8+6 pin"
    if re.search(r"6\s*\+\s*2", t):           return "6+2 pin"
    if re.search(r"\b8\b", t):                return "8 pin"
    if re.search(r"\b6\b", t):                return "6 pin"
    return t[:30]

def _count_gpu_pins(t):
    t = t.lower()
    if re.search(r"8\s*\+\s*8\s*\+\s*8", t): return 3
    if re.search(r"8\s*\+\s*8", t):           return 2
    if re.search(r"8\s*\+\s*6|6\s*\+\s*8", t): return 2
    if re.search(r"12vhpwr|16\s*pin", t):      return 1
    if re.search(r"\b8\b|\b6\b", t):           return 1
    return 0

def _parse_gpu_slots(slots_str, full_text=""):
    combined = (slots_str + " " + full_text).lower()
    m = re.search(r"(\d+[.,]\d+)\s*[-\s]?slot", combined, re.I)
    if m: return float(m.group(1).replace(",", "."))
    if re.search(r"3[,.]5", combined):                              return 3.5
    if re.search(r"2[,.]5", combined):                              return 2.5
    if re.search(r"трёхслот|трехслот|3\s*slot|triple", combined):  return 3.0
    if re.search(r"двухслот|2\s*slot|dual.slot", combined):        return 2.0
    if re.search(r"однослот|1\s*slot|single.slot", combined):      return 1.0
    return 0.0

def _find_m2_types(text):
    types = []
    if "nvme" in text: types.append("NVMe")
    if re.search(r"\bsata\b", text, re.I) and "m.2" in text: types.append("SATA")
    return types or ["NVMe"]

def _find_ssd_interface(text):
    if "nvme" in text:                          return "NVMe"
    if re.search(r"\bsata\b", text, re.I):     return "SATA"
    if "pcie" in text or "pci-e" in text:       return "NVMe"
    return "---"

def _find_ssd_form_factor(text):
    m = re.search(r"m\.2\s*(\d{4})", text, re.I)
    if m: return f"M.2 {m.group(1)}"
    if "m.2" in text:              return "M.2"
    if re.search(r"2\.5", text):   return '2.5"'
    if re.search(r"3\.5", text):   return '3.5"'
    return "---"

def _find_m2_key(text):
    if re.search(r"ключ\s*m\b|key\s*m\b|m-key", text, re.I): return "M"
    if re.search(r"b\s*\+\s*m|b&m", text, re.I):              return "B+M"
    if re.search(r"\bsata\b", text, re.I):                     return "B+M"
    return "M"

def _parse_capacity_gb(text):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(тб|tb)", text, re.I)
    if m: return int(float(m.group(1).replace(",", ".")) * 1024)
    m = re.search(r"(\d+)\s*(гб|gb)", text, re.I)
    if m: return int(m.group(1))
    return 0

def _find_gpu_length(text):
    lengths = [int(x) for x in re.findall(r"(\d{3})\s*(?:мм|mm)", text) if 140 < int(x) < 500]
    return max(lengths) if lengths else 0

def _find_radiator_sizes(text):
    return sorted({s for s in [120, 140, 240, 280, 360, 420] if str(s) in text})

def _find_radiator_size_single(text):
    for s in [420, 360, 280, 240, 140, 120]:
        if str(s) in text: return s
    return 0

def _parse_80plus(text):
    t = text.lower()
    for cert in ["titanium","platinum","gold","silver","bronze","white"]:
        if cert in t: return f"80+ {cert.capitalize()}"
    return "80+" if "80+" in t else "---"


# ═══════════════════════════════════════════════════════════
#  ФАЙЛОВЫЕ УТИЛИТЫ
# ═══════════════════════════════════════════════════════════

def load_from_file(filename: str = "components.json"):
    if not os.path.exists(filename):
        return None
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        # обратная совместимость: добавляем id если нет
        for category, items in data.items():
            for item in items:
                if "id" not in item:
                    item["id"] = abs(hash(item.get("name", "") + category)) % (10 ** 9)
                # кулеры: дополняем список сокетов из specs
                if category == "Кулеры" and "specs" in item:
                    compat = item["specs"].get("Совместимость", "")
                    if compat:
                        item["socket"] = _find_all_sockets(compat)
        return data
    except Exception as e:
        log.error("Не удалось загрузить %s: %s", filename, e)
        return None


def save_to_file(data: dict, filename: str = "components.json") -> None:
    tmp = filename + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filename)   # атомарная замена — не потеряем файл при краше
        log.info("Сохранено -> %s", filename)
    except Exception as e:
        log.error("Ошибка сохранения: %s", e)