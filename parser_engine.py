import time
import json
import os
import re
import logging
from DrissionPage import ChromiumPage, ChromiumOptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ───────────────────── конфиг для слабого VPS ─────────────────────
PAGES_LIMIT        = 20   # максимум страниц на категорию
PRODUCTS_PER_PAGE  = 36   # уменьшили с 36 — меньше нагрузка на RAM
BATCH_SIZE         = 2    # было 4 — на 1GB RAM больше нельзя
SCROLL_STEPS       = 1    # было 2 — экономим время
SCROLL_PAUSE       = 0.2  # было 0.3
PAGE_LOAD_PAUSE    = 1.2  # было 1.5
BATCH_LOAD_PAUSE   = 1.0  # пауза после открытия пачки вкладок
# ──────────────────────────────────────────────────────────────────


def _make_options(load_images: bool = True) -> ChromiumOptions:
    co = ChromiumOptions()
    co.auto_port()

    co.set_argument('--headless')
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-gpu')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-extensions')
    co.set_argument('--disable-blink-features=AutomationControlled')
    co.set_argument('--page-load-strategy=eager')

    # ── критично для экономии RAM на слабом VPS ──
    co.set_argument('--disable-background-networking')
    co.set_argument('--disable-default-apps')
    co.set_argument('--disable-sync')
    co.set_argument('--no-first-run')
    co.set_argument('--disable-translate')
    co.set_argument('--hide-scrollbars')
    co.set_argument('--mute-audio')
    co.set_argument('--safebrowsing-disable-auto-update')
    co.set_argument('--js-flags=--max-old-space-size=128')  # лимит JS heap 128MB
    co.set_argument('--single-process')  # нет fork-процессов — важно для 1 vCPU

    co.mute(True)
    co.incognito(True)
    co.set_browser_path("/usr/bin/chromium-browser")

    # Картинки
    img_policy = 1 if load_images else 2
    co.set_pref("profile.managed_default_content_settings.images", img_policy)

    # Если картинки не нужны — блокируем всё лишнее
    if not load_images:
        co.set_pref("profile.managed_default_content_settings.plugins", 2)
        co.set_pref("profile.managed_default_content_settings.popups", 2)
        co.set_pref("profile.managed_default_content_settings.geolocation", 2)
        co.set_pref("profile.managed_default_content_settings.notifications", 2)
        co.set_pref("profile.managed_default_content_settings.media_stream", 2)

    return co


def _safe_quit(page: ChromiumPage) -> None:
    """Закрывает браузер не бросая исключений."""
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


# ─────────────────── сбор каталога ───────────────────────

def scrape_citilink(url: str, category_name: str) -> list[dict]:
    """
    Проходит по страницам каталога, собирает базовую инфу + фото,
    затем батчами открывает страницы товаров для характеристик и цены.
    """
    all_results: list[dict] = []

    # ── Фаза 1: каталог — нужны картинки, один браузер на все страницы ──
    log.info("[%s] Фаза 1: сбор каталога → %s", category_name, url)
    product_data = _collect_catalog(url, category_name)
    log.info("[%s] Каталог собран: %d товаров", category_name, len(product_data))

    if not product_data:
        return all_results

    # ── Фаза 2: детали товаров — без картинок, батчами ──
    log.info("[%s] Фаза 2: сбор характеристик...", category_name)
    for batch_start in range(0, len(product_data), BATCH_SIZE):
        batch = product_data[batch_start: batch_start + BATCH_SIZE]
        batch_results = _process_batch(batch, category_name)
        all_results.extend(batch_results)
        time.sleep(1.0)  # пауза между батчами — даём RAM восстановиться

    log.info("[%s] Готово. Итого: %d товаров", category_name, len(all_results))
    return all_results


def _collect_catalog(url: str, category_name: str) -> list[dict]:
    """
    Фаза 1: проходим страницы каталога с картинками.
    Один браузер живёт на все страницы категории.
    """
    product_data: list[dict] = []
    co = _make_options(load_images=True)
    page = ChromiumPage(co)

    try:
        for current_page in range(1, PAGES_LIMIT + 1):
            target_url = url if current_page == 1 else f"{url.rstrip('/')}/?p={current_page}"

            if not _is_alive(page):
                log.warning("[%s] Сессия разорвана, переподключение...", category_name)
                _safe_quit(page)
                co = _make_options(load_images=True)
                page = ChromiumPage(co)

            try:
                page.get(target_url)
                time.sleep(PAGE_LOAD_PAUSE)
            except Exception as e:
                log.error("[%s] Не удалось загрузить %s: %s", category_name, target_url, e)
                break

            if current_page == 1:
                try:
                    label = page.ele('css:label[for="Подробный режим каталога-list"]', timeout=4)
                    if label:
                        label.click()
                        time.sleep(1.5)
                except Exception:
                    pass

            log.info("[%s] Страница %d", category_name, current_page)

            for _ in range(SCROLL_STEPS):
                page.scroll.down(900)
                time.sleep(SCROLL_PAUSE)

            items = (
                page.eles('css:[data-meta-name="SnippetProductHorizontalLayout"]')
                or page.eles('css:[data-meta-product-id]')
            )

            if not items:
                log.info("[%s] Товары на стр.%d не найдены — конец каталога.", category_name, current_page)
                break

            page_products: list[dict] = []
            for item in items:
                try:
                    title_el = item.ele('css:[data-meta-name="Snippet__title"]', timeout=1)
                    if not title_el:
                        continue

                    href = title_el.attr("href") or ""
                    name = (title_el.text or "").strip()
                    if not href or not name or len(name) < 5:
                        continue

                    full_url = href if href.startswith("http") else f"https://www.citilink.ru{href}"
                    if not full_url.endswith('/properties/'):
                        full_url = full_url.rstrip('/') + '/properties/'

                    # Картинка — берём ЗДЕСЬ пока браузер с картинками открыт
                    image_url = ""
                    img_el = item.ele("css:img", timeout=0.3)
                    if img_el:
                        # Пробуем data-src (lazy-load) потом обычный src
                        src = img_el.attr("data-src") or img_el.attr("src") or ""
                        if src.startswith("//"):
                            image_url = "https:" + src
                        elif src.startswith("/"):
                            image_url = "https://www.citilink.ru" + src
                        else:
                            image_url = src

                    page_products.append({"name": name, "url": full_url, "image": image_url})
                except Exception:
                    pass

            if PRODUCTS_PER_PAGE:
                page_products = page_products[:PRODUCTS_PER_PAGE]

            product_data.extend(page_products)
            log.info("[%s] Стр.%d — %d товаров (итого: %d)",
                     category_name, current_page, len(page_products), len(product_data))

            time.sleep(1.0)

    except Exception as e:
        log.error("[%s] Критическая ошибка каталога: %s", category_name, e)
    finally:
        _safe_quit(page)

    return product_data


# ─────────────── обработка пачки вкладок ─────────────────

def _process_batch(product_data: list[dict], category_name: str) -> list[dict]:
    """
    Фаза 2: открывает BATCH_SIZE вкладок БЕЗ картинок,
    собирает цену и характеристики.
    """
    co = _make_options(load_images=False)  # картинки на стр. товара не нужны
    results: list[dict] = []
    page = ChromiumPage(co)

    tabs: list[dict] = []
    try:
        for p in product_data:
            try:
                tab = page.new_tab(p["url"])
                tabs.append({"tab": tab, "product": p})
            except Exception as e:
                log.debug("Не удалось открыть вкладку %s: %s", p["url"], e)

        time.sleep(BATCH_LOAD_PAUSE)

        for entry in tabs:
            tab = entry["tab"]
            product = entry["product"]
            try:
                specs = _collect_specs(tab)
                price_text = _collect_price(tab)
                extracted = _extract_logic(category_name, product["name"], specs)

                results.append({
                    "id":            abs(hash(product["name"] + category_name)) % (10 ** 9),
                    "name":          product["name"],
                    "category":      category_name,
                    "priceCitilink": price_text,
                    "priceDNS":      "---",
                    "imageUrl":      product["image"],  # пришло из фазы 1
                    "productUrl":    product["url"],
                    **extracted,
                    "specs":         specs,
                })
                log.info("  ✓ %s", product["name"][:55])
            except Exception as e:
                log.debug("Ошибка обработки %s: %s", product["name"][:40], e)
            finally:
                try:
                    tab.close()
                except Exception:
                    pass

    except Exception as e:
        log.error("Ошибка батча: %s", e)
    finally:
        _safe_quit(page)

    return results


def _collect_specs(tab) -> dict:
    specs = {}
    try:
        rows = tab.eles('css:[class*="PropertiesItem"]', timeout=3)
        for row in rows:
            try:
                n = row.ele('css:[class*="PropertiesName"]', timeout=0.1)
                v = row.ele('css:[class*="PropertiesValue"]', timeout=0.1)
                if n and v:
                    key = n.text.strip().rstrip(":")
                    specs[key] = v.text.strip()
            except Exception:
                pass
    except Exception:
        pass
    return specs


def _collect_price(tab) -> str:
    try:
        el = tab.ele('css:[data-meta-name="PriceBlock__price"]', timeout=1)
        if el:
            digits = "".join(filter(str.isdigit, el.text or ""))
            if digits:
                formatted = "{:,}".format(int(digits)).replace(",", " ")
                return f"{formatted} руб"
    except Exception:
        pass
    return "---"


# ─────────────────────────────────────────────────────────────────────────────
#  ИЗВЛЕЧЕНИЕ ХАРАКТЕРИСТИК
# ─────────────────────────────────────────────────────────────────────────────

def _empty_compat() -> dict:
    return {
        "socket":              "---",
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
    r = _empty_compat()
    c = {str(k).strip().lower(): str(v).strip() for k, v in specs.items()}
    cv = {k: v.lower() for k, v in c.items()}
    full = (name + " " + " ".join(cv.keys()) + " " + " ".join(cv.values())).lower()

    def val(key_fragment: str) -> str:
        for k, v in cv.items():
            if key_fragment in k:
                return v
        return ""

    def mm(text: str) -> int:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мм|mm)", text, re.I)
        return int(float(m.group(1).replace(",", "."))) if m else 0

    def watt(text: str) -> int:
        m = re.search(r"(\d{2,4})\s*(?:вт|w)\b", text, re.I)
        return int(m.group(1)) if m else 0

    def first_int(text: str, min_val: int = 0, max_val: int = 99999) -> int:
        for m in re.finditer(r"\d+", text):
            v = int(m.group())
            if min_val <= v <= max_val:
                return v
        return 0

    if category == "Процессоры":
        r["socket"]  = _find_socket(full)
        r["ramType"] = _find_ddr(full, r["socket"])
        r["tdp"]     = watt(val("тепловыделение")) or watt(val("tdp")) or first_int(val("tdp"), 10, 500)

    elif category == "Видеокарты":
        r["gpuChipset"] = c.get("видеочипсет", "").split(",")[0].strip() or name

        vram_str = val("объем видеопамяти") or val("память")
        m = re.search(r"(\d+)\s*гб", vram_str, re.I)
        r["vram"] = int(m.group(1)) if m else 0

        r["gpuTdp"]    = watt(val("максимальное энергопотребление")) or \
                          watt(val("энергопотребление")) or \
                          watt(val("tdp"))
        r["gpuReqPsu"] = watt(val("рекомендуемая мощность")) or \
                          watt(val("рекомендовано")) or \
                          watt(val("питание"))
        if r["gpuReqPsu"] == 0:
            r["gpuReqPsu"] = watt(full)

        pin_str = val("разъемы дополнительного питания") or val("питание")
        r["gpuPowerPin"] = _parse_gpu_pin(pin_str)

        r["gpuLength"] = mm(val("длина видеокарты")) or _find_gpu_length(full)
        r["gpuHeight"] = mm(val("высота видеокарты"))

        slots_str = val("конструкция системы охлаждения")
        if "трёхслот" in slots_str or "трехслот" in slots_str or "3-slot" in slots_str:
            r["gpuSlots"] = 3
        elif "двухслот" in slots_str or "2-slot" in slots_str:
            r["gpuSlots"] = 2
        elif "однослот" in slots_str or "1-slot" in slots_str:
            r["gpuSlots"] = 1

        r["gpuPciVersion"] = _find_pci_version(full)

    elif category == "Материнские платы":
        r["socket"]     = _find_socket(full)
        r["formFactor"] = _find_form_factor(full)
        r["ramType"]    = _find_ddr(full)
        r["pciVersion"] = _find_pci_version(full)

        slots_raw = val("количество слотов памяти") or val("слотов памяти") or val("слоты памяти")
        r["ramSlots"] = first_int(slots_raw, 1, 8)

        freq_raw = val("максимальная частота памяти") or val("частота памяти")
        r["ramMaxFreq"] = first_int(freq_raw, 800, 12000)

        r["cpuPowerPin"] = _parse_cpu_pin(val("разъем питания процессора") or val("питание процессора"))

        m2_raw = val("количество разъемов m.2") or val("разъемов m.2") or val("m.2")
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
        r["psuWattage"]  = watt(val("мощность")) or first_int(val("мощность"), 200, 3000)
        r["formFactor"]  = _find_psu_form_factor(full)
        r["psuLength"]   = mm(val("глубина")) or mm(val("длина"))

        r["cpuPowerPin"] = _parse_cpu_pin(
            val("разъем cpu") or val("разъемов cpu") or val("питания cpu") or val("разъем 8 pin")
        )
        r["gpuPowerPin"] = _parse_gpu_pin(
            val("разъем pcie") or val("разъемов pcie") or val("разъем 6+2") or
            val("12vhpwr") or val("разъем 16")
        )

    elif category == "Корпуса":
        case_ff_raw = (val("форм-фактор совместимых") or
                       val("типоразмер") or
                       val("форм-фактор"))
        r["formFactor"]         = _find_form_factor(case_ff_raw)

        r["maxGpuLength"]       = mm(val("длина видеокарты")) or mm(val("макс. длина видеокарты"))
        r["maxCpuCoolerHeight"] = (mm(val("высота кулера")) or
                                   mm(val("макс. высота кулера")) or
                                   mm(val("высота процессорного кулера")))
        r["maxPsuLength"]       = mm(val("длина блока питания")) or mm(val("глубина блока питания"))

        mb_fmt_raw = val("форм-фактор совместимых") or val("совместимые мп") or val("форм-фактор")
        r["supportedMbFormats"] = _find_supported_mb_formats(mb_fmt_raw)

    elif category == "Кулеры":
        compat_raw = val("совместимость") or val("сокет")
        r["socket"]       = _find_all_sockets(compat_raw) if compat_raw else _find_socket(full)
        r["maxTdp"]       = watt(val("рассеиваемая мощность")) or watt(val("tdp")) or \
                             first_int(val("tdp"), 30, 500)
        r["coolerHeight"] = mm(val("высота кулера")) or mm(val("высота"))

    elif category == "SSD":
        r["ssdInterface"]  = _find_ssd_interface(full)
        r["ssdFormFactor"] = _find_ssd_form_factor(full)

        cap_str = val("объем") or val("ёмкость") or val("емкость")
        r["ssdCapacityGb"] = _parse_capacity_gb(cap_str or full)

    return r


# ─────────────────────── вспомогательные парсеры ─────────────────────────────

def _normalize_lga(text: str) -> str:
    return re.sub(r'(?i)lga\s+(\d+)', r'LGA\1', text)


def _find_socket(text: str) -> str:
    norm = _normalize_lga(text).upper()
    for s in ["AM5", "AM4", "LGA1851", "LGA1700", "LGA1200", "LGA2066", "LGA2011", "LGA1366", "TR5", "SP3"]:
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
    if "ddr5" in text or socket == "AM5" or socket == "LGA1851":
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
        ("ATX",      [r"\batx\b"]),
        ("mATX",     [r"matx", r"micro-atx", r"m-atx"]),
        ("Mini-ITX", [r"mini-itx"]),
        ("Flex-ATX", [r"flex-atx"]),
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
        ("ATX",      r"\batx\b"),
        ("mATX",     r"matx|micro-atx|m-atx"),
        ("Mini-ITX", r"mini-itx"),
    ]
    for label, pattern in mapping:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(label)
    return found


def _parse_cpu_pin(text: str) -> str:
    text = text.lower().strip()
    if not text or text == "---":
        return "---"
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
    lengths = [int(x) for x in re.findall(r"(\d{3})\s*(?:мм|mm)", text) if 140 < int(x) < 500]
    return max(lengths) if lengths else 0


# ─────────────────── файловые утилиты ────────────────────

def load_from_file(filename: str = "components.json") -> dict | None:
    if not os.path.exists(filename):
        return None
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        for category, items in data.items():
            for item in items:
                if "id" not in item:
                    item["id"] = abs(hash(item.get("name", "") + category)) % (10 ** 9)
                if category == "Кулеры" and "specs" in item:
                    compat = item["specs"].get("Совместимость", "")
                    if compat:
                        item["socket"] = _find_all_sockets(compat)
        return data
    except Exception as e:
        log.error("Не удалось загрузить %s: %s", filename, e)
        return None


def save_to_file(data: dict, filename: str = "components.json") -> None:
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("Сохранено → %s", filename)
    except Exception as e:
        log.error("Ошибка сохранения %s: %s", filename, e)