"""
parser_regard.py — парсер магазина Регард (regard.ru).

АРХИТЕКТУРА (аналогична parser_engine.py для Ситилинка):

  Фаза 1 — scrape_regard(url, category_name):
    Обходит страницы каталога, собирает:
      name, url, image, regard_id
    Один браузер живёт всё время → экономия на запуске Chrome.

  Фаза 2 — батчевая обработка страниц товаров:
    BATCH_SIZE вкладок одновременно без картинок.
    Собирает specs (характеристики) и цену.

КАК ОТЛИЧАЕТСЯ ОТ СИТИЛИНКА:
  • Другие CSS-классы (с хэш-суффиксами — used partial class matching)
  • Другая структура URL: /catalog/5162/... для каталога, /product/ID/slug для товара
  • Пагинация: ?page=2, ?page=3

СЕЛЕКТОРЫ определены на основе инспектора из скриншотов:
  Карточка каталога  : div[class*="Card_wrap"]
  Ссылка и название  : a[class*="CardText_link"]  → div[class*="CardText_title"]
  Цена               : span[class*="Price_price"]
  Характеристики     : div[class*="CharacteristicsItem_item"]
  Название хар-ки    : div[class*="CharacteristicsItem_name"]
  Значение хар-ки    : div[class*="CharacteristicsItem_value"]
"""

import re
import time
import random
import logging
from DrissionPage import ChromiumPage, ChromiumOptions

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  КОНФИГУРАЦИЯ — настройки производительности
# ─────────────────────────────────────────────────────────────────────────────

BATCH_SIZE        = 5    # вкладок одновременно (немного меньше чем Ситилинк,
                          # Регард чуть медленнее отвечает)
SCROLL_STEPS      = 2    # прокруток для ленивой загрузки картинок
SCROLL_PAUSE      = 0.3
PAGE_LOAD_PAUSE   = 1.5  # ждём после загрузки страницы каталога
BATCH_LOAD_PAUSE  = 2.0  # ждём после открытия всех вкладок батча

PAGE_DELAY_MIN = 1.5     # антибан: пауза между страницами каталога
PAGE_DELAY_MAX = 3.0

BASE_URL = "https://www.regard.ru"


def _make_options_regard(load_images: bool = True) -> ChromiumOptions:
    """
    Создаёт ChromiumOptions для браузера Регарда.
    Почти то же что в Ситилинке, но другое смещение окна —
    чтобы два браузера не перекрывали друг друга.

    Ситилинк открывается на ЛЕВОМ мониторе (offset_x=-1680),
    Регард открывается на ПРАВОМ (offset_x=0).
    """
    co = ChromiumOptions()
    co.auto_port()

    # Регард — правый монитор, чтобы видеть оба браузера одновременно
    co.set_argument("--window-size=1680,1050")
    co.set_argument("--window-position=0,0")   # правый (основной) монитор
    co.set_argument("--start-maximized")

    # Случайный User-Agent — антибан
    _USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    co.set_argument(f"--user-agent={random.choice(_USER_AGENTS)}")

    co.set_argument("--no-sandbox")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-extensions")
    co.set_argument("--page-load-strategy=eager")

    co.mute(True)
    co.incognito(True)
    co.set_browser_path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

    # Управление картинками
    img_policy = 1 if load_images else 2
    co.set_pref("profile.managed_default_content_settings.images", img_policy)

    if not load_images:
        co.set_pref("profile.managed_default_content_settings.plugins", 2)
        co.set_pref("profile.managed_default_content_settings.popups",  2)
        co.set_pref("profile.managed_default_content_settings.geolocation", 2)
        co.set_pref("profile.managed_default_content_settings.notifications", 2)

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


# ─────────────────────────────────────────────────────────────────────────────
#  ГЛАВНАЯ ТОЧКА ВХОДА
# ─────────────────────────────────────────────────────────────────────────────

def scrape_regard(url: str, category_name: str) -> list[dict]:
    """
    Парсит категорию Регарда. Возвращает список товаров в том же формате,
    что и scrape_citilink() из parser_engine.py — структура идентична,
    только добавляется поле "source": "regard".

    Вызывается из main.py в отдельном потоке, параллельно с Ситилинком.

    Аргументы:
        url           : URL категории, напр. "https://www.regard.ru/catalog/5162/kulery-dlya-processorov"
        category_name : русское название категории, напр. "Кулеры"

    Возвращает:
        list[dict] — список товаров (см. структуру ниже в _process_batch_regard)
    """
    log.info("[Регард/%s] Старт → %s", category_name, url)

    # Фаза 1: каталог — собираем name, url, image
    product_data = _collect_catalog_regard(url, category_name)
    if not product_data:
        log.warning("[Регард/%s] Каталог пуст или не удалось загрузить", category_name)
        return []

    log.info("[Регард/%s] Фаза 2: характеристики %d товаров...", category_name, len(product_data))

    # Фаза 2: страницы товаров — батчами
    all_results: list[dict] = []
    for batch_start in range(0, len(product_data), BATCH_SIZE):
        batch = product_data[batch_start: batch_start + BATCH_SIZE]
        batch_results = _process_batch_regard(batch, category_name)
        all_results.extend(batch_results)

        if batch_start + BATCH_SIZE < len(product_data):
            time.sleep(random.uniform(1.0, 2.5))

    log.info("[Регард/%s] Готово. Итого: %d товаров", category_name, len(all_results))
    return all_results


# ─────────────────────────────────────────────────────────────────────────────
#  ФАЗА 1: СБОР КАТАЛОГА
# ─────────────────────────────────────────────────────────────────────────────

def _collect_catalog_regard(url: str, category_name: str) -> list[dict]:
    """
    Обходит страницы каталога Регарда и собирает для каждой карточки:
      - name     : название товара
      - url      : ссылка на страницу товара (полный URL)
      - image    : URL картинки
      - regard_id: ID товара (виден в карточке как "ID: 452936")

    ПАГИНАЦИЯ Регарда:
      Страница 1: /catalog/5162/kulery-dlya-processorov
      Страница 2: /catalog/5162/kulery-dlya-processorov?page=2
      Страница 3: /catalog/5162/kulery-dlya-processorov?page=3

    ЗАЩИТА ОТ ЗАЦИКЛИВАНИЯ:
      Если набор имён товаров на странице N совпадает с N-1 — конец каталога.
    """
    product_data: list[dict] = []
    last_page_names: set[str] = set()

    co = _make_options_regard(load_images=True)
    page = ChromiumPage(co)

    try:
        current_page = 1

        while True:
            # Строим URL страницы
            if current_page == 1:
                target_url = url
            else:
                sep = "&" if "?" in url else "?"
                target_url = f"{url}{sep}page={current_page}"

            # Проверяем жив ли браузер
            if not _is_alive(page):
                log.warning("[Регард/%s] Сессия умерла, перезапуск...", category_name)
                _safe_quit(page)
                page = ChromiumPage(_make_options_regard(load_images=True))

            try:
                page.get(target_url)
                time.sleep(PAGE_LOAD_PAUSE)
            except Exception as e:
                log.error("[Регард/%s] Не удалось загрузить %s: %s", category_name, target_url, e)
                break

            log.info("[Регард/%s] Страница %d", category_name, current_page)

            # Прокрутка для lazy-load изображений
            for _ in range(SCROLL_STEPS):
                page.scroll.down(800)
                time.sleep(SCROLL_PAUSE)

            # ── КАРТОЧКИ ТОВАРОВ ───────────────────────────────────────────
            # Из скриншота 1: div.Card_wrap__hE544.Card_listing__nGjbk.ListingRenderer_listingCard__DqY3k
            # Классы содержат хэш-суффиксы — используем "contains" через *=
            # Вариант 1 — основной селектор карточки
            cards = page.eles('css:div[class*="Card_listing"]')
            # Вариант 2 — запасной, если структура изменилась
            if not cards:
                cards = page.eles('css:div[class*="ListingRenderer_listingCard"]')

            if not cards:
                log.info("[Регард/%s] Стр.%d — товаров не найдено, конец.", category_name, current_page)
                break

            page_products: list[dict] = []
            current_page_names: set[str] = set()

            for card in cards:
                try:
                    # ── ССЫЛКА И НАЗВАНИЕ ─────────────────────────────────
                    # Из скриншота 1:
                    #   <a class="CardText_link__C_fFZ link_black" href="/product/452936/...">
                    #     <div class="CardText_title__7b5bO..." title="Кулер ID-COOLING SE-224-XTS BLACK">
                    link_el = card.ele('css:a[class*="CardText_link"]', timeout=1)
                    if not link_el:
                        continue

                    href = link_el.attr("href") or ""
                    if not href:
                        continue

                    # Название берём из атрибута title вложенного div — он всегда полный
                    title_el = link_el.ele('css:div[class*="CardText_title"]', timeout=0.5)
                    if title_el:
                        name = title_el.attr("title") or title_el.text.strip()
                    else:
                        name = link_el.text.strip()

                    if not name or len(name) < 5:
                        continue

                    current_page_names.add(name)

                    # Полный URL товара
                    if href.startswith("http"):
                        product_url = href
                    else:
                        product_url = BASE_URL + href

                    # ── ID ТОВАРА ─────────────────────────────────────────
                    # Из URL: /product/452936/slug → extracting ID
                    regard_id_match = re.search(r"/product/(\d+)/", product_url)
                    regard_id = regard_id_match.group(1) if regard_id_match else ""

                    # ── КАРТИНКА ──────────────────────────────────────────
                    image_url = ""
                    img_el = card.ele("css:img", timeout=0.3)
                    if img_el:
                        src = (img_el.attr("data-src")
                               or img_el.attr("src")
                               or img_el.attr("data-lazy")
                               or "")
                        if src.startswith("//"):
                            image_url = "https:" + src
                        elif src.startswith("/"):
                            image_url = BASE_URL + src
                        else:
                            image_url = src

                    page_products.append({
                        "name":      name,
                        "url":       product_url,
                        "image":     image_url,
                        "regard_id": regard_id,
                    })

                except Exception as e:
                    log.debug("[Регард/%s] Ошибка карточки: %s", category_name, e)

            # Защита от зацикливания
            if current_page > 1 and current_page_names == last_page_names:
                log.info(
                    "[Регард/%s] Страница %d дублирует %d — конец каталога.",
                    category_name, current_page, current_page - 1
                )
                break

            last_page_names = current_page_names
            product_data.extend(page_products)

            log.info(
                "[Регард/%s] Стр.%d — %d товаров (итого: %d)",
                category_name, current_page, len(page_products), len(product_data)
            )

            # Нет товаров на странице — тоже конец
            if not page_products:
                break

            current_page += 1
            time.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    except Exception as e:
        log.error("[Регард/%s] Критическая ошибка каталога: %s", category_name, e)
    finally:
        _safe_quit(page)

    return product_data


# ─────────────────────────────────────────────────────────────────────────────
#  ФАЗА 2: БАТЧ СТРАНИЦ ТОВАРОВ
# ─────────────────────────────────────────────────────────────────────────────

def _process_batch_regard(product_data: list[dict], category_name: str) -> list[dict]:
    """
    Открывает несколько вкладок параллельно (без картинок),
    затем последовательно собирает характеристики и цену.
    """
    co      = _make_options_regard(load_images=False)
    results: list[dict] = []
    page    = ChromiumPage(co)
    tabs:   list[dict] = []

    try:
        # Открываем все вкладки разом — они грузятся параллельно
        for p in product_data:
            try:
                tab = page.new_tab(p["url"])
                tabs.append({"tab": tab, "product": p})
            except Exception as e:
                log.debug("[Регард] Вкладка не открылась %s: %s", p["url"], e)

        time.sleep(BATCH_LOAD_PAUSE)

        # Собираем данные последовательно
        for entry in tabs:
            tab     = entry["tab"]
            product = entry["product"]
            try:
                specs      = _collect_specs_regard(tab)
                price_text = _collect_price_regard(tab)

                # Используем ту же функцию _extract_logic из parser_engine,
                # поскольку характеристики одинаковы по смыслу для обоих магазинов.
                # Импортируем здесь, чтобы избежать циклических зависимостей.
                from parser_engine import _extract_logic
                extracted = _extract_logic(category_name, product["name"], specs)

                results.append({
                    # Идентификатор — используем regard_id если есть,
                    # иначе хэш как в Ситилинке
                    "id": (
                        int(product["regard_id"])
                        if product.get("regard_id", "").isdigit()
                        else abs(hash(product["name"] + category_name)) % (10 ** 9)
                    ),
                    "name":          product["name"],
                    "category":      category_name,

                    # Цена от Регарда (Ситилинк будет "---", т.к. не парсили)
                    "priceCitilink": "---",
                    "priceRegard":   price_text,   # новое поле для Регарда

                    "imageUrl":      product["image"],
                    "productUrl":    product["url"],

                    # Источник — важно для сохранения в БД
                    "source": "regard",

                    # Все поля совместимости из _extract_logic
                    **extracted,
                    "specs": specs,
                })
                log.info("  ✓ [Регард] %s", product["name"][:55])

            except Exception as e:
                log.debug("[Регард] Ошибка %s: %s", product["name"][:40], e)
            finally:
                try:
                    tab.close()
                except Exception:
                    pass

    except Exception as e:
        log.error("[Регард] Ошибка батча: %s", e)
    finally:
        _safe_quit(page)

    return results


# ─────────────────────────────────────────────────────────────────────────────
#  СБОР ХАРАКТЕРИСТИК СО СТРАНИЦЫ ТОВАРА
# ─────────────────────────────────────────────────────────────────────────────

def _collect_specs_regard(tab) -> dict:
    """
    Собирает характеристики со страницы товара Регарда.

    Из скриншота 4 видно структуру:
      section.CharacteristicsSection_section (секция типа "ОСНОВНЫЕ", "РАЗЪЁМЫ")
        div.CharacteristicsSection_content
          div.CharacteristicsItem_item (одна строка характеристики)
            div.CharacteristicsItem_left
              div.CharacteristicsItem_name    ← название хар-ки
            div.CharacteristicsItem_value     ← значение

    Важно: секций может быть несколько (ОСНОВНЫЕ, РАЗЪЁМЫ И ИНТЕРФЕЙСЫ, ...).
    Собираем все в единый словарь.
    """
    specs: dict = {}
    try:
        # Ждём появления характеристик на странице
        # Вариант 1 — прямой поиск всех строк характеристик
        rows = tab.eles('css:div[class*="CharacteristicsItem_item"]', timeout=5)

        if not rows:
            # Вариант 2 — если товар требует прокрутки до раздела "Характеристики"
            tab.scroll.down(500)
            time.sleep(0.5)
            rows = tab.eles('css:div[class*="CharacteristicsItem_item"]', timeout=3)

        for row in rows:
            try:
                # Название характеристики
                name_el = row.ele('css:div[class*="CharacteristicsItem_name"]', timeout=0.2)
                # Значение характеристики
                val_el  = row.ele('css:div[class*="CharacteristicsItem_value"]', timeout=0.2)

                if name_el and val_el:
                    key = name_el.text.strip().rstrip(":")
                    val = val_el.text.strip()
                    if key and val:
                        specs[key] = val
            except Exception:
                pass

    except Exception as e:
        log.debug("[Регард] _collect_specs ошибка: %s", e)

    return specs


def _collect_price_regard(tab) -> str:
    """
    Собирает цену со страницы товара Регарда.

    Из скриншота 2:
      span.CardPrice_price__YFA2m.Card_price__3VIdu
        span.Price_price_m2aSe  ← содержит: "1", "<!---->", " ", "990", " ₽"
                                   (цифры разбиты на части через комментарии)

    Стратегия: берём весь текст блока цены и выбираем из него цифры.
    """
    try:
        # Основной селектор — страница товара показывает крупную цену
        price_el = (
            tab.ele('css:span[class*="Price_price"]', timeout=2)
            or tab.ele('css:[class*="product-price"]', timeout=1)
            or tab.ele('css:[data-type="currency"]', timeout=1)
        )
        if price_el:
            # Из скриншота видно что текст содержит только цифры и "₽"
            raw = price_el.text or ""
            digits = "".join(filter(str.isdigit, raw))
            if digits:
                formatted = "{:,}".format(int(digits)).replace(",", " ")
                return f"{formatted} руб"

    except Exception as e:
        log.debug("[Регард] _collect_price ошибка: %s", e)

    return "---"
