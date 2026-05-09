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

# ───────────────────────── конфиг (ОПТИМИЗАЦИЯ под 1ГБ / 1 ядро) ─────────────────────────
PAGES_LIMIT        = 10     # максимум страниц на категорию
PRODUCTS_PER_PAGE  = 20     # сколько товаров брать со страницы
BATCH_SIZE         = 1      # ВАЖНО: не батчим вкладки — 1 за раз
SCROLL_STEPS       = 1
SCROLL_PAUSE       = 0.2
PAGE_LOAD_PAUSE    = 1.0
# ────────────────────────────────────────────────────────────────────────────────────────────


def _make_options(load_images: bool = True) -> ChromiumOptions:
    """
    Максимально лёгкие настройки Chromium для VPS 1GB / 1 ядро.
    Картинки НЕ отключаем (нужны).
    """
    co = ChromiumOptions()
    co.auto_port()

    # Headless новый режим
    co.set_argument('--headless=new')
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-gpu')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-extensions')
    co.set_argument('--disable-background-networking')
    co.set_argument('--disable-sync')
    co.set_argument('--disable-translate')
    co.set_argument('--disable-notifications')
    co.set_argument('--disable-infobars')
    co.set_argument('--disable-popup-blocking')

    # Картинки включены
    co.set_argument('--blink-settings=imagesEnabled=true')

    # Экономия памяти / процессов
    co.set_argument('--renderer-process-limit=1')
    co.set_argument('--single-process')   # 🔥 КРИТИЧНО для 1ГБ
    co.set_argument('--no-zygote')
    co.set_argument('--memory-pressure-off')

    co.set_browser_path("/usr/bin/chromium-browser")

    # Политика изображений (на всякий случай)
    img_policy = 1 if load_images else 2
    co.set_pref("profile.managed_default_content_settings.images", img_policy)

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


# ─────────────────── сбор каталога ───────────────────────

def scrape_citilink(url: str, category_name: str) -> list[dict]:
    """
    Проходит по страницам каталога, собирает карточки товаров.
    Затем для каждого товара открывает страницу свойств и собирает цену + характеристики.
    Работает в ОДНОМ ChromiumPage без вкладок.
    """
    all_results: list[dict] = []

    co = _make_options(load_images=True)
    page = ChromiumPage(co)

    try:
        log.info("[%s] Старт парсинга → %s", category_name, url)

        for current_page in range(1, PAGES_LIMIT + 1):
            target_url = url if current_page == 1 else f"{url.rstrip('/')}/?p={current_page}"

            # Переподключение если сессия умерла
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

            # На первой странице пробуем переключить в подробный режим каталога
            if current_page == 1:
                try:
                    label = page.ele('css:label[for="Подробный режим каталога-list"]', timeout=4)
                    if label:
                        label.click()
                        time.sleep(1.0)
                except Exception:
                    pass

            log.info("[%s] Страница %d", category_name, current_page)

            # Прокрутка для подгрузки lazy-load
            for _ in range(SCROLL_STEPS):
                page.scroll.down(900)
                time.sleep(SCROLL_PAUSE)

            # Сбор карточек товаров
            items = (
                page.eles('css:[data-meta-name="SnippetProductHorizontalLayout"]')
                or page.eles('css:[data-meta-product-id]')
            )

            if not items:
                log.info("[%s] Товары на стр.%d не найдены — конец каталога.", category_name, current_page)
                break

            product_data: list[dict] = []
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

                    # картинка
                    image_url = ""
                    img_el = item.ele("css:img", timeout=0.3)
                    if img_el:
                        src = img_el.attr("src") or ""
                        if src.startswith("//"):
                            image_url = "https:" + src
                        elif src.startswith("/"):
                            image_url = "https://www.citilink.ru" + src
                        else:
                            image_url = src

                    product_data.append({"name": name, "url": full_url, "image": image_url})
                except Exception:
                    pass

            if PRODUCTS_PER_PAGE:
                product_data = product_data[:PRODUCTS_PER_PAGE]

            log.info("[%s] Стр.%d — собрано %d товаров", category_name, current_page, len(product_data))

            # ── ОБРАБОТКА ТОВАРОВ ПО ОДНОМУ (без вкладок, без батчей) ──
            for product in product_data:
                try:
                    # Загружаем страницу товара
                    page.get(product["url"])
                    time.sleep(0.8)

                    # Собираем характеристики и цену
                    specs = _collect_specs(page)
                    price_text = _collect_price(page)

                    # Извлекаем структурированные поля совместимости
                    extracted = _extract_logic(category_name, product["name"], specs)

                    # Формируем запись
                    all_results.append({
                        "id": abs(hash(product["name"] + category_name)) % (10 ** 9),
                        "name": product["name"],
                        "category": category_name,
                        "priceCitilink": price_text,
                        "priceDNS": "---",
                        "imageUrl": product["image"],
                        "productUrl": product["url"],
                        **extracted,
                        "specs": specs,
                    })

                    log.info("  ✓ %s", product["name"][:50])

                except Exception as e:
                    log.debug("Ошибка товара %s: %s", product["name"][:40], e)

            time.sleep(0.5)

    except Exception as e:
        log.error("[%s] Критическая ошибка: %s", category_name, e)
    finally:
        _safe_quit(page)

    log.info("[%s] Готово. Итого: %d товаров", category_name, len(all_results))
    return all_results


# ─────────────── сбор данных со страницы товара ─────────────────

def _collect_specs(tab) -> dict:
    specs = {}
    try:
        rows = tab.eles('css:[class*="PropertiesItem"]', timeout=2)
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
        el = tab.ele('css:[data-meta-name="PriceBlock__price"]', timeout=0.5)
        if el:
            digits = "".join(filter(str.isdigit, el.text or ""))
            if digits:
                formatted = "{:,}".format(int(digits)).replace(",", " ")
                return f"{formatted} руб"
    except Exception:
        pass
    return "---"


# ─────────────────────────────────────────────────────────────────────────────
#  ИЗВЛЕЧЕНИЕ ХАРАКТЕРИСТИК (полная схема совместимости)
#  Все поля всегда присутствуют → фронт не падает по KeyError
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

    # ── утилиты парсинга ─────────────────────────────────────────────────────
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

    # ── логика по категориям ─────────────────────────────────────────────────
    if category == "Процессоры":
        r["socket"]  = _find_socket(full)
        r["ramType"] = _find_ddr(full, r["socket"])
        r["tdp"]     = watt(val("тепловыделение")) or watt(val("tdp")) or first_int(val("tdp"), 10, 500)

    elif category == "Видеокарты":
        r["gpuChipset"] = c.get("видеочипсет", "").split(",")[0].strip() or name

        vram_str = val("объем видеопамяти") or val("память")
        m = re.search(r"(\d+)\s*гб", vram_str, re.I)
        r["vram"] = int(m.group(1)) if m else 0

        r["gpuTdp"]   = watt(val("максимальное энергопотребление")) or \
                         watt(val("энергопотребление")) or watt(val("tdp"))
        r["gpuReqPsu"]= watt(val("рекомендуемая мощность")) or \
                         watt(val("рекомендовано")) or watt(val("питание")) or watt(full)

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
        r["psuWattage"]   = watt(val("мощность")) or first_int(val("мощность"), 200, 3000)
        r["psuFormFactor"] = _find_psu_form_factor(full)
        r["psuLength"]    = mm(val("глубина")) or mm(val("длина"))

        r["cpuPowerPin"]  = _parse_cpu_pin(
            val("разъем cpu") or val("разъемов cpu") or val("питания cpu") or val("разъем 8 pin")
        )
        r["gpuPowerPin"]  = _parse_gpu_pin(
            val("разъем pcie") or val("разъемов pcie") or val("разъем 6+2") or
            val("12vhpwr") or val("разъем 16")
        )

    elif category == "Корпуса":
        case_ff_raw = (val("форм-фактор совместимых") or val("типоразмер") or val("форм-фактор"))
        r["formFactor"]         = _find_form_factor(case_ff_raw)

        r["maxGpuLength"]        = mm(val("длина видеокарты")) or mm(val("макс. длина видеокарты"))
        r["maxCpuCoolerHeight"]  = (mm(val("высота кулера")) or
                                    mm(val("макс. высота кулера")) or
                                    mm(val("высота процессорного кулера")))
        r["maxPsuLength"]        = mm(val("длина блока питания")) or mm(val("глубина блока питания"))

        mb_fmt_raw = val("форм-фактор совместимых") or val("совместимые мп") or val("форм-фактор")
        r["supportedMbFormats"]  = _find_supported_mb_formats(mb_fmt_raw)

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
        # Обратная совместимость: добавляем id + корректируем socket для кулеров
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


if __name__ == "__main__":
    # Тестовый запуск одной категории (для отладки)
    test_urls = {
        "Процессоры": "https://www.citilink.ru/catalog/processory/",
        "Видеокарты": "https://www.citilink.ru/catalog/videokarty/",
        "Материнские платы": "https://www.citilink.ru/catalog/materinskie-platy/",
        "Оперативная память": "https://www.citilink.ru/catalog/moduli-pamyati/",
        "Блоки питания": "https://www.citilink.ru/catalog/bloki-pitaniya/",
        "Корпуса": "https://www.citilink.ru/catalog/korpusa/",
        "SSD": "https://www.citilink.ru/catalog/ssd-nakopiteli/",
        "Кулеры": "https://www.citilink.ru/catalog/sistemy-ohlazhdeniya-processora/"
    }
    # Запусти только нужную категорию:
    cat = "Процессоры"
    data = scrape_citilink(test_urls[cat], cat)
    save_to_file({cat: data})