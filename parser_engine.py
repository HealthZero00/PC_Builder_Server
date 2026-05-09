import time
import json
import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from DrissionPage import ChromiumPage, ChromiumOptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ───────────────────────── конфиг ─────────────────────────
PAGES_LIMIT        = 20   # максимум страниц на категорию
PRODUCTS_PER_PAGE  = 36  # сколько товаров брать со страницы (None = все)
BATCH_SIZE         = 4    # вкладок одновременно
SCROLL_STEPS       = 2    # прокруток вниз перед сбором
SCROLL_PAUSE       = 0.3  # пауза между прокрутками (сек)
PAGE_LOAD_PAUSE    = 1.5  # пауза после загрузки страницы
BATCH_LOAD_PAUSE   = 1.0  # пауза после открытия пачки вкладок
# ──────────────────────────────────────────────────────────


def _make_options(load_images: bool = True) -> ChromiumOptions:
    co = ChromiumOptions()
    co.auto_port()

    # 1. РЕЖИМ БЕЗ ОКНА (Обязательно для VPS)
    # co.set_argument('--headless')
    #
    # # 2. РАБОТА ПОД ROOT (Обязательно для VPS)
    # co.set_argument('--no-sandbox')

    # Остальные полезные настройки
    co.mute(True)
    co.incognito(True)
    # co.set_browser_path("/usr/bin/chromium-browser")
    co.set_browser_path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-extensions")
    co.set_argument("--page-load-strategy=eager")

    # Настройка картинок (на сервере лучше ставить False, чтобы парсило быстрее)
    img_policy = 1 if load_images else 2
    co.set_pref("profile.managed_default_content_settings.images", img_policy)

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
    co = _make_options(load_images=True)
    page = ChromiumPage(co)

    try:
        log.info("[%s] Старт парсинга → %s", category_name, url)

        for current_page in range(1, PAGES_LIMIT + 1):
            target_url = url if current_page == 1 else f"{url.rstrip('/')}/?p={current_page}"

            # ── переподключение если сессия умерла ──
            if not _is_alive(page):
                log.warning("[%s] Сессия разорвана, переподключение...", category_name)
                _safe_quit(page)
                co = _make_options(load_images=True)  # новые опции = новый порт
                page = ChromiumPage(co)

            try:
                page.get(target_url)
                time.sleep(PAGE_LOAD_PAUSE)
            except Exception as e:
                log.error("[%s] Не удалось загрузить %s: %s", category_name, target_url, e)
                break

            # на первой странице переключаем в подробный режим
            if current_page == 1:
                try:
                    label = page.ele('css:label[for="Подробный режим каталога-list"]', timeout=4)
                    if label:
                        label.click()
                        time.sleep(1.5)
                except Exception:
                    pass

            log.info("[%s] Страница %d", category_name, current_page)

            # прокрутка для подгрузки lazy-load товаров
            for _ in range(SCROLL_STEPS):
                page.scroll.down(900)
                time.sleep(SCROLL_PAUSE)

            # ── сбор карточек ──
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

            # ── батчевая обработка страниц товаров ──
            for batch_start in range(0, len(product_data), BATCH_SIZE):
                batch = product_data[batch_start : batch_start + BATCH_SIZE]
                batch_results = _process_batch(batch, category_name)
                all_results.extend(batch_results)

            time.sleep(1.5)

    except Exception as e:
        log.error("[%s] Критическая ошибка: %s", category_name, e)
    finally:
        _safe_quit(page)

    log.info("[%s] Готово. Итого: %d товаров", category_name, len(all_results))
    return all_results


# ─────────────── обработка пачки вкладок ─────────────────

def _process_batch(product_data: list[dict], category_name: str) -> list[dict]:
    """
    Открывает BATCH_SIZE вкладок параллельно, собирает цену и характеристики.
    """
    co = _make_options(load_images=False)   # картинки на стр. товара не нужны
    results: list[dict] = []
    page = ChromiumPage(co)

    tabs: list[dict] = []
    try:
        # открываем все вкладки разом
        for p in product_data:
            try:
                tab = page.new_tab(p["url"])
                tabs.append({"tab": tab, "product": p})
            except Exception as e:
                log.debug("Не удалось открыть вкладку %s: %s", p["url"], e)

        time.sleep(1.5)

        # раскрываем «Все характеристики» во всех вкладках сразу
        # for entry in tabs:
        #     try:
        #         entry["tab"].run_js(
        #             """
        #             document.querySelectorAll('button').forEach(btn => {
        #                 const t = btn.innerText || '';
        #                 if (t.includes('Все характеристики') || t.includes('больше')) {
        #                     btn.click();
        #                 }
        #             });
        #             """
        #         )
        #     except Exception:
        #         pass
        #
        # time.sleep(1.2)

        # собираем данные
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
                    "imageUrl":      product["image"],
                    "productUrl":    product["url"],
                    **extracted,         # socket, ramType, power, formFactor, gpuLength
                    "specs":         specs,
                })
                log.info("  ✓ %s", product["name"][:50])
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
                # обычный пробел — без \u00a0, чтобы не было кракозябр в приложении
                formatted = "{:,}".format(int(digits)).replace(",", " ")
                return f"{formatted} руб"
    except Exception:
        pass
    return "---"


# ─────────────────────────────────────────────────────────────────────────────
#  ИЗВЛЕЧЕНИЕ ХАРАКТЕРИСТИК
#  Каждое поле — конкретная причина несовместимости в реальной сборке.
#
#  Схема полей:
#  socket             — CPU / MB / Cooler          (совпадение обязательно)
#  ramType            — CPU / MB / RAM              (DDR4 / DDR5)
#  ramSlots           — MB                          (нельзя поставить 4 планки в 2 слота)
#  ramMaxFreq         — MB                          (МГц, не ставь быструю RAM в медленную плату)
#  ramHeight          — RAM                         (мм, может упереться в кулер)
#  ramCapacity        — RAM                         (ГБ одной планки)
#  tdp                — CPU                         (Вт, нужен кулер с maxTdp ≥ этого)
#  maxTdp             — Cooler                      (Вт, должен быть ≥ tdp процессора)
#  coolerHeight       — Cooler                      (мм, должен влезть в корпус)
#  psuWattage         — PSU                         (Вт суммарно)
#  psuFormFactor      — PSU                         (ATX / SFX / SFX-L)
#  psuLength          — PSU                         (мм, в mini-корпусах важно)
#  cpuPowerPin        — PSU / MB                    (4+4 / 8 / 8+4 / 8+8 pin)
#  gpuPowerPin        — PSU / GPU                   (8pin / 12VHPWR / без питания)
#  formFactor         — MB / Case / PSU             (ATX / mATX / Mini-ITX / E-ATX)
#  pciVersion         — MB / GPU                    (3.0 / 4.0 / 5.0)
#  m2Slots            — MB                          (кол-во слотов)
#  m2Types            — MB                          ([NVMe, SATA] — что поддерживают слоты)
#  gpuLength          — GPU                         (мм)
#  gpuHeight          — GPU                         (мм)
#  gpuSlots           — GPU                         (занято слотов расширения: 2 / 3)
#  gpuTdp             — GPU                         (Вт реального потребления)
#  gpuReqPsu          — GPU                         (Вт рекомендованного БП)
#  gpuPciVersion      — GPU                         (версия PCI-E карты)
#  vram               — GPU                         (ГБ)
#  gpuChipset         — GPU                         (NVIDIA GeForce RTX 5070 и т.п.)
#  maxGpuLength       — Case                        (мм)
#  maxCpuCoolerHeight — Case                        (мм)
#  maxPsuLength       — Case                        (мм, 0 = нет ограничения)
#  supportedMbFormats — Case                        ([ATX, mATX, Mini-ITX, ...])
#  ssdInterface       — SSD                         (NVMe / SATA)
#  ssdFormFactor      — SSD                         (M.2 2280 / 2.5" / и т.д.)
#  ssdCapacityGb      — SSD                         (ГБ числом)
# ─────────────────────────────────────────────────────────────────────────────

def _empty_compat() -> dict:
    """Пустой шаблон — все поля присутствуют всегда, нет KeyError на фронте."""
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
    c = {str(k).strip().lower(): str(v).strip() for k, v in specs.items()}  # clean keys
    cv = {k: v.lower() for k, v in c.items()}                               # lower values
    full = (name + " " + " ".join(cv.keys()) + " " + " ".join(cv.values())).lower()

    # ── универсальные хелперы ──────────────────────────────────────────────
    def val(key_fragment: str) -> str:
        """Первое значение у ключа, содержащего key_fragment."""
        for k, v in cv.items():
            if key_fragment in k:
                return v
        return ""

    def mm(text: str) -> int:
        """Первое число перед мм/mm."""
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мм|mm)", text, re.I)
        return int(float(m.group(1).replace(",", "."))) if m else 0

    def watt(text: str) -> int:
        """Первое число перед вт/w."""
        m = re.search(r"(\d{2,4})\s*(?:вт|w)\b", text, re.I)
        return int(m.group(1)) if m else 0

    def first_int(text: str, min_val: int = 0, max_val: int = 99999) -> int:
        for m in re.finditer(r"\d+", text):
            v = int(m.group())
            if min_val <= v <= max_val:
                return v
        return 0

    # ──────────────────────────────────────────────────────────────────────
    if category == "Процессоры":
        r["socket"]  = _find_socket(full)
        r["ramType"] = _find_ddr(full, r["socket"])
        r["tdp"]     = watt(val("тепловыделение")) or watt(val("tdp")) or first_int(val("tdp"), 10, 500)

    # ──────────────────────────────────────────────────────────────────────
    elif category == "Видеокарты":
        # чипсет
        r["gpuChipset"] = c.get("видеочипсет", "").split(",")[0].strip() or name

        # видеопамять
        vram_str = val("объем видеопамяти") or val("память")
        m = re.search(r"(\d+)\s*гб", vram_str, re.I)
        r["vram"] = int(m.group(1)) if m else 0

        # TDP и рекомендуемый БП — берём из нескольких возможных ключей
        r["gpuTdp"]   = watt(val("максимальное энергопотребление")) or \
                         watt(val("энергопотребление")) or \
                         watt(val("tdp"))
        r["gpuReqPsu"]= watt(val("рекомендуемая мощность")) or \
                         watt(val("рекомендовано")) or \
                         watt(val("питание"))   # fallback из строки «8 pin, рекомендовано 750 Вт»
        if r["gpuReqPsu"] == 0:
            # ищем в свободном тексте строки питания
            r["gpuReqPsu"] = watt(full)

        # разъём питания GPU
        pin_str = val("разъемы дополнительного питания") or val("питание")
        r["gpuPowerPin"] = _parse_gpu_pin(pin_str)

        # габариты
        r["gpuLength"] = mm(val("длина видеокарты")) or _find_gpu_length(full)
        r["gpuHeight"] = mm(val("высота видеокарты"))

        # слоты
        slots_str = val("конструкция системы охлаждения")
        if "трёхслот" in slots_str or "трехслот" in slots_str or "3-slot" in slots_str:
            r["gpuSlots"] = 3
        elif "двухслот" in slots_str or "2-slot" in slots_str:
            r["gpuSlots"] = 2
        elif "однослот" in slots_str or "1-slot" in slots_str:
            r["gpuSlots"] = 1

        # версия PCI-E
        r["gpuPciVersion"] = _find_pci_version(full)

    # ──────────────────────────────────────────────────────────────────────
    elif category == "Материнские платы":
        r["socket"]     = _find_socket(full)
        r["formFactor"] = _find_form_factor(full)
        r["ramType"]    = _find_ddr(full)
        r["pciVersion"] = _find_pci_version(full)

        # слоты ОЗУ
        slots_raw = val("количество слотов памяти") or val("слотов памяти") or val("слоты памяти")
        r["ramSlots"] = first_int(slots_raw, 1, 8)

        # максимальная частота ОЗУ
        freq_raw = val("максимальная частота памяти") or val("частота памяти")
        r["ramMaxFreq"] = first_int(freq_raw, 800, 12000)

        # разъём питания CPU на плате (что нужно от БП)
        r["cpuPowerPin"] = _parse_cpu_pin(val("разъем питания процессора") or val("питание процессора"))

        # M.2 слоты
        m2_raw = val("количество разъемов m.2") or val("разъемов m.2") or val("m.2")
        r["m2Slots"] = first_int(m2_raw, 0, 8)
        r["m2Types"] = _find_m2_types(full)

    # ──────────────────────────────────────────────────────────────────────
    elif category == "Оперативная память":
        r["ramType"]     = _find_ddr(full)
        r["ramCapacity"] = first_int(val("объем") or val("память"), 1, 256)

        freq_raw = val("частота") or val("тактовая частота")
        r["ramMaxFreq"] = first_int(freq_raw, 800, 12000)

        # высота планки — критично для совместимости с кулером
        height_raw = val("высота") or val("высота радиатора")
        r["ramHeight"] = mm(height_raw) or first_int(height_raw, 20, 80)

    # ──────────────────────────────────────────────────────────────────────
    elif category == "Блоки питания":
        r["psuWattage"]   = watt(val("мощность")) or first_int(val("мощность"), 200, 3000)
        r["formFactor"]   = _find_psu_form_factor(full)
        r["psuLength"]    = mm(val("глубина")) or mm(val("длина"))

        # разъёмы для CPU — сколько 8-pin / 4+4 есть
        r["cpuPowerPin"]  = _parse_cpu_pin(
            val("разъем cpu") or val("разъемов cpu") or val("питания cpu") or val("разъем 8 pin")
        )
        # разъёмы для GPU
        r["gpuPowerPin"]  = _parse_gpu_pin(
            val("разъем pcie") or val("разъемов pcie") or val("разъем 6+2") or
            val("12vhpwr") or val("разъем 16")
        )

    # ──────────────────────────────────────────────────────────────────────
    elif category == "Корпуса":
        # formFactor корпуса — ищем ТОЛЬКО в ключе про МП/типоразмер,
        # иначе "Форм-фактор БП: ATX" ложно делает корпус ATX-совместимым.
        case_ff_raw = (val("форм-фактор совместимых") or
                       val("типоразмер") or
                       val("форм-фактор"))
        r["formFactor"]         = _find_form_factor(case_ff_raw)

        r["maxGpuLength"]        = mm(val("длина видеокарты")) or mm(val("макс. длина видеокарты"))
        r["maxCpuCoolerHeight"]  = (mm(val("высота кулера")) or
                                    mm(val("макс. высота кулера")) or
                                    mm(val("высота процессорного кулера")))
        r["maxPsuLength"]        = mm(val("длина блока питания")) or mm(val("глубина блока питания"))

        # supportedMbFormats — из ключей про МП, не из full (там есть "ATX" от БП)
        mb_fmt_raw = val("форм-фактор совместимых") or val("совместимые мп") or val("форм-фактор")
        r["supportedMbFormats"]  = _find_supported_mb_formats(mb_fmt_raw)

    # ──────────────────────────────────────────────────────────────────────
    elif category == "Кулеры":
        # Кулеры поддерживают несколько сокетов — берём весь список из поля "Совместимость"
        compat_raw = val("совместимость") or val("сокет")
        r["socket"]       = _find_all_sockets(compat_raw) if compat_raw else _find_socket(full)
        r["maxTdp"]       = watt(val("рассеиваемая мощность")) or watt(val("tdp")) or \
                             first_int(val("tdp"), 30, 500)
        r["coolerHeight"] = mm(val("высота кулера")) or mm(val("высота"))

    # ──────────────────────────────────────────────────────────────────────
    elif category == "SSD":
        r["ssdInterface"]  = _find_ssd_interface(full)
        r["ssdFormFactor"] = _find_ssd_form_factor(full)

        cap_str = val("объем") or val("ёмкость") or val("емкость")
        r["ssdCapacityGb"] = _parse_capacity_gb(cap_str or full)

    return r


# ─────────────────────── вспомогательные парсеры ─────────────────────────────

def _normalize_lga(text: str) -> str:
    """'LGA 1700' -> 'LGA1700' (убираем пробел внутри LGA-обозначения)."""
    return re.sub(r'(?i)lga\s+(\d+)', r'LGA\1', text)


def _find_socket(text: str) -> str:
    norm = _normalize_lga(text).upper()
    for s in ["AM5", "AM4", "LGA1851", "LGA1700", "LGA1200", "LGA2066", "LGA2011", "LGA1366", "TR5", "SP3"]:
        if s in norm:
            return s
    return "---"


def _find_all_sockets(text: str) -> str:
    """
    Возвращает ВСЕ поддерживаемые сокеты через запятую (для кулеров).
    Пример: "AM4,AM5,LGA1200,LGA1700,LGA1851"
    Валидатор делает split(",") и проверяет cpu_socket in supported.
    """
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
    """Возвращает строку версии PCI-E: 3.0 / 4.0 / 5.0."""
    # ищем «pci-e 5.0», «pci express 4.0», «pcie4», и т.п.
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
    """Список форм-факторов материнских плат, поддерживаемых корпусом."""
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
    """
    Нормализует строку питания CPU.
    '8+4 pin', '8+8 pin', '8 pin', '4+4 pin' → возвращает строку как есть,
    но стандартизированную.
    """
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
    return text[:30]  # fallback — первые 30 символов


def _parse_gpu_pin(text: str) -> str:
    """Нормализует разъём питания GPU."""
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
    return types or ["NVMe"]  # по умолчанию NVMe если M.2 вообще упоминается


def _find_ssd_interface(text: str) -> str:
    if "nvme" in text:
        return "NVMe"
    if re.search(r"\bsata\b", text, re.I):
        return "SATA"
    if "pcie" in text or "pci-e" in text:
        return "NVMe"
    return "---"


def _find_ssd_form_factor(text: str) -> str:
    # M.2 — ищем конкретный типоразмер
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
    # «2 ТБ» → 2048, «512 ГБ» → 512
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
        # Обратная совместимость: добавляем id если его нет (старый кэш без fix)
        for category, items in data.items():
            for item in items:
                if "id" not in item:
                    item["id"] = abs(hash(item.get("name", "") + category)) % (10 ** 9)
                # Кулеры: старый парсер писал только первый сокет → перезаписываем
                # из specs["Совместимость"] если там несколько сокетов.
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