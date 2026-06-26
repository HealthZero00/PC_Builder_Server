"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

"""
parser_regard.py — парсер магазина Регард (regard.ru).
"""

import asyncio
import re
import random
import logging
from camoufox.async_api import AsyncCamoufox

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# КОНФИГ
# ────────────────────────────────────────────────────────────────────────────
PAGES_LIMIT       = 1
PRODUCTS_PER_PAGE = 1
BATCH_SIZE        = 4
SCROLL_STEPS      = 2
SCROLL_PAUSE      = 0.25
PAGE_LOAD_PAUSE   = 1.5
BATCH_LOAD_PAUSE  = 2.0
PAGE_DELAY_MIN    = 1.0
PAGE_DELAY_MAX    = 2.5

BASE_URL = "https://www.regard.ru"


# ─────────────────────────────────────────────────────────────────────────────
# УТИЛИТЫ
# ─────────────────────────────────────────────────────────────────────────────

def _make_browser(block_images: bool = False) -> AsyncCamoufox:
    """Создаёт контекст AsyncCamoufox."""
    return AsyncCamoufox(
        headless=True,
        os="windows",
        humanize=True,
        block_images=block_images,
        i_know_what_im_doing=True,  # Убирает LeakWarning
    )


async def _close_browser_safe(browser, label: str = "Регард") -> None:
    """Безопасное закрытие браузера."""
    try:
        await browser.close()
    except Exception as e:
        err = str(e)
        if "Connection closed" in err or "Browser.close" in err:
            log.debug("[%s] Браузер закрылся с ожидаемой ошибкой: %s", label, err)
        else:
            log.warning("[%s] Неожиданная ошибка закрытия браузера: %s", label, e)


async def _safe_close(obj) -> None:
    """Безопасно закрывает страницу / контекст."""
    try:
        await obj.close()
    except Exception:
        pass


def _setup_page_handlers(page) -> None:
    """Подавляем JS-ошибки страницы."""
    page.on("pageerror", lambda _: None)
    page.on("crash",     lambda _: None)


async def _scroll_page(page) -> None:
    """Прокрутка через mouse.wheel."""
    try:
        await page.mouse.wheel(0, 800)
    except Exception:
        pass


async def _ele(container, selector: str, timeout: float = 3.0):
    """Найти один элемент."""
    try:
        if timeout > 0.5:
            return await container.wait_for_selector(
                selector,
                timeout=int(timeout * 1000),
                state="attached"
            )
        return await container.query_selector(selector)
    except Exception:
        return None


async def _eles(container, selector: str) -> list:
    """Найти все элементы по CSS-селектору."""
    try:
        return await container.query_selector_all(selector)
    except Exception:
        return []


async def _attr(el, attr_name: str) -> str:
    """Получить атрибут элемента."""
    try:
        return await el.get_attribute(attr_name) or ""
    except Exception:
        return ""


async def _text(el) -> str:
    """Получить текст элемента."""
    try:
        return (await el.text_content() or "").strip()
    except Exception:
        return ""


# ────────────────────────────────────────────────────────────────────────────
# СНЯТИЕ ГАЛОЧКИ «МОДИФИКАЦИИ» (JS-клик вместо Playwright .click())
# ─────────────────────────────────────────────────────────────────────────────

async def _uncheck_modifications(page, category_name: str) -> None:
    """
    Снимает галочку 'Модификации'.
    Используем JS-клик — не зависает в отличие от Playwright .click().
    """
    try:
        # Ждём появления чекбокса (не более 4 сек)
        label = await asyncio.wait_for(
            page.wait_for_selector(
                'label[for*="Модификации"], label[for*="odifikac"]',
                state="attached"
            ),
            timeout=4.0
        )

        if label:
            # JS-клик — не зависает на scroll/visibility проверках
            await page.evaluate(
                "(el) => el.click()",
                label
            )
            await asyncio.sleep(1.5)
            log.info("[Регард/%s] Галочка 'Модификации' снята ✓", category_name)

    except asyncio.TimeoutError:
        log.debug(
            "[Регард/%s] Чекбокс 'Модификации' не найден (возможно, нет на странице)",
            category_name
        )
    except Exception as e:
        log.warning(
            "[Регард/%s] Не удалось снять 'Модификации': %s",
            category_name, e
        )


# ─────────────────────────────────────────────────────────────────────────────
# ПОЛУЧЕНИЕ ЦЕНЫ ИЗ КАРТОЧКИ КАТАЛОГА
# ─────────────────────────────────────────────────────────────────────────────

async def _get_price_from_card(card) -> str:
    """Извлекает цену из карточки каталога Регарда."""
    try:
        # Способ 1: visually-hidden span
        hidden = await _ele(card, 'span.visually-hidden', timeout=0.3)
        if hidden:
            text = await _text(hidden)
            digits = "".join(filter(str.isdigit, text))
            if digits and int(digits) > 100:
                return "{:,}".format(int(digits)).replace(",", " ") + " руб"

        # Способ 2: data-атрибуты на карточке
        for attr in ("data-price", "data-product-price", "price"):
            val = await _attr(card, attr)
            if val and str(val).isdigit() and int(val) > 100:
                return "{:,}".format(int(val)).replace(",", " ") + " руб"

        # Способ 3: innerHTML + регулярка
        try:
            html = await card.inner_html() or ""

            m = re.search(r'visually-hidden[^>]*>([^<]+)<', html)
            if m:
                text = m.group(1).strip()
                digits = "".join(filter(str.isdigit, text))
                if digits and int(digits) > 100:
                    return "{:,}".format(int(digits)).replace(",", " ") + " руб"

            price_matches = re.findall(r'(\d[\d\s]{1,8}\d)\s*(?:₽|руб)', html)
            for m_str in price_matches:
                digits = "".join(filter(str.isdigit, m_str))
                if digits and 100 < int(digits) < 10_000_000:
                    return "{:,}".format(int(digits)).replace(",", " ") + " руб"
        except Exception:
            pass

        # Способ 4: data-атрибуты на ссылке товара
        try:
            link_el = await _ele(card, 'a[href*="/product/"]', timeout=0.3)
            if link_el:
                for attr in ("data-price", "data-gtm-price", "data-ecom-price"):
                    val = await _attr(link_el, attr)
                    if val and str(val).replace(".", "").isdigit():
                        price_val = int(float(val))
                        if price_val > 100:
                            return "{:,}".format(price_val).replace(",", " ") + " руб"
        except Exception:
            pass

    except Exception as e:
        log.debug("[Регард] Ошибка парсинга цены из карточки: %s", e)

    return "---"


# ─────────────────────────────────────────────────────────────────────────────
# ПОЛУЧЕНИЕ ЦЕНЫ СО СТРАНИЦЫ ТОВАРА (fallback)
# ─────────────────────────────────────────────────────────────────────────────

async def _get_price_from_product_page(page) -> str:
    """Парсит цену непосредственно со страницы товара."""
    try:
        # Способ 1: visually-hidden
        hidden = await _ele(page, 'span.visually-hidden', timeout=2.0)
        if hidden:
            text = await _text(hidden)
            digits = "".join(filter(str.isdigit, text))
            if digits and 100 < int(digits) < 10_000_000:
                return "{:,}".format(int(digits)).replace(",", " ") + " руб"

        # Способ 2: span[class*="Price_price"]
        price_span = await _ele(page, 'span[class*="Price_price"]', timeout=2.0)
        if price_span:
            raw = await _text(price_span)
            digits = "".join(filter(str.isdigit, raw))
            if digits and 100 < int(digits) < 10_000_000:
                return "{:,}".format(int(digits)).replace(",", " ") + " руб"

        # Способ 3: div[class*="PriceBlock"]
        price_block = await _ele(page, 'div[class*="PriceBlock"]', timeout=2.0)
        if price_block:
            raw = await _text(price_block)
            m = re.search(r'([\d\s]{3,})\s*₽', raw)
            if m:
                digits = "".join(filter(str.isdigit, m.group(1)))
                if digits and 100 < int(digits) < 10_000_000:
                    return "{:,}".format(int(digits)).replace(",", " ") + " руб"

        # Способ 4: JS evaluate
        try:
            price_js = await page.evaluate("""
                () => {
                    const spans = document.querySelectorAll('span.visually-hidden');
                    for (const s of spans) {
                        const t = s.textContent.trim();
                        const digits = t.replace(/\\D/g, '');
                        if (digits && parseInt(digits) > 100) return digits;
                    }
                    const offer = document.querySelector('[itemprop="price"]');
                    if (offer) return offer.getAttribute('content') || offer.textContent;
                    return null;
                }
            """)
            if price_js:
                digits = "".join(filter(str.isdigit, str(price_js)))
                if digits and 100 < int(digits) < 10_000_000:
                    return "{:,}".format(int(digits)).replace(",", " ") + " руб"
        except Exception:
            pass

    except Exception as e:
        log.debug("[Регард] Ошибка парсинга цены со страницы товара: %s", e)

    return "---"


# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНАЯ ТОЧКА ВХОДА
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_regard(url: str, category_name: str) -> list[dict]:
    """
    Две фазы:
    1. Каталог (с картинками) — собирает name + url + image + price_from_catalog
    2. Страницы товаров (без картинок) — батчами по BATCH_SIZE вкладок
    """
    log.info("[Регард/%s] Старт → %s", category_name, url)

    product_data = await _collect_catalog_regard(url, category_name)
    if not product_data:
        log.warning("[Регард/%s] Каталог пуст", category_name)
        return []

    log.info(
        "[Регард/%s] Фаза 2: характеристики %d товаров...",
        category_name, len(product_data)
    )

    all_results: list[dict] = []
    for batch_start in range(0, len(product_data), BATCH_SIZE):
        batch = product_data[batch_start: batch_start + BATCH_SIZE]
        batch_results = await _process_batch_regard(batch, category_name)
        all_results.extend(batch_results)

        if batch_start + BATCH_SIZE < len(product_data):
            await asyncio.sleep(random.uniform(2.0, 4.5))

    log.info("[Регард/%s] Готово. Итого: %d товаров", category_name, len(all_results))
    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# ФАЗА 1: СБОР КАТАЛОГА
# ─────────────────────────────────────────────────────────────────────────────

async def _collect_catalog_regard(url: str, category_name: str) -> list[dict]:
    """Обходит страницы каталога regard.ru."""
    product_data: list[dict]  = []
    last_page_names: set[str] = set()
    modifications_unchecked   = False

    browser_cm = _make_browser(block_images=False)

    try:
        browser = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[Регард/%s] Не удалось запустить браузер: %s", category_name, e)
        return product_data

    ctx  = None
    page = None

    try:
        ctx  = await browser.new_context()
        page = await ctx.new_page()
        _setup_page_handlers(page)

        current_page = 1

        while PAGES_LIMIT is None or current_page <= PAGES_LIMIT:

            target_url = (
                url if current_page == 1
                else f"{url}{'&' if '?' in url else '?'}page={current_page}"
            )

            try:
                if page.is_closed():
                    log.warning("[Регард/%s] Страница закрыта, пересоздаём...", category_name)
                    page = await ctx.new_page()
                    _setup_page_handlers(page)
            except Exception:
                pass

            try:
                await page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=30000  # Явный таймаут 30 сек
                )
                await asyncio.sleep(PAGE_LOAD_PAUSE)
            except Exception as e:
                log.error(
                    "[Регард/%s] Не удалось загрузить %s: %s",
                    category_name, target_url, e
                )
                break

            if not modifications_unchecked:
                await _uncheck_modifications(page, category_name)
                modifications_unchecked = True
                await asyncio.sleep(0.5)

            log.info("[Регард/%s] Страница %d", category_name, current_page)

            for _ in range(SCROLL_STEPS):
                await _scroll_page(page)
                await asyncio.sleep(SCROLL_PAUSE)

            await asyncio.sleep(0.5)

            cards = await _eles(page, 'div[class*="Card_listing"]')
            if not cards:
                cards = await _eles(page, 'div[class*="ListingRenderer_listingCard"]')
            if not cards:
                cards = await _eles(page, 'div[class*="Card_card"]')

            if not cards:
                log.info(
                    "[Регард/%s] Стр.%d — товаров не найдено",
                    category_name, current_page
                )
                break

            page_products: list[dict] = []
            current_page_names: set[str] = set()
            collected_on_page = 0

            for card in cards:
                if PRODUCTS_PER_PAGE is not None and collected_on_page >= PRODUCTS_PER_PAGE:
                    break

                try:
                    link_el = await _ele(card, 'a[class*="CardText_link"]', timeout=1.0)
                    if not link_el:
                        link_el = await _ele(card, 'a[href*="/product/"]', timeout=1.0)
                    if not link_el:
                        continue

                    href = await _attr(link_el, "href")
                    if not href:
                        continue

                    title_el = await _ele(
                        link_el, 'div[class*="CardText_title"]', timeout=0.5
                    )
                    if title_el:
                        name = (
                            await _attr(title_el, "title")
                            or await _text(title_el)
                        )
                    else:
                        name = (
                            await _attr(link_el, "title")
                            or await _text(link_el)
                        )

                    if not name or len(name) < 5:
                        continue

                    current_page_names.add(name)
                    product_url = (
                        href if href.startswith("http") else BASE_URL + href
                    )

                    regard_id_match = re.search(r"/product/(\d+)/", product_url)
                    regard_id = regard_id_match.group(1) if regard_id_match else ""

                    image_url = ""
                    img_el = await _ele(card, "img", timeout=0.3)
                    if img_el:
                        src = (
                            await _attr(img_el, "data-src")
                            or await _attr(img_el, "src")
                            or await _attr(img_el, "data-lazy")
                        )
                        if src.startswith("//"):
                            image_url = "https:" + src
                        elif src.startswith("/"):
                            image_url = BASE_URL + src
                        else:
                            image_url = src

                    price_from_catalog = await _get_price_from_card(card)
                    log.debug(
                        "[Регард/%s] Цена из каталога для '%s': %s",
                        category_name, name[:40], price_from_catalog
                    )

                    page_products.append({
                        "name":               name,
                        "url":                product_url,
                        "image":              image_url,
                        "regard_id":          regard_id,
                        "price_from_catalog": price_from_catalog,
                    })
                    collected_on_page += 1

                except Exception as e:
                    log.debug("[Регард/%s] Ошибка карточки: %s", category_name, e)

            if current_page > 1 and current_page_names == last_page_names:
                log.info(
                    "[Регард/%s] Страница %d дублирует предыдущую — конец.",
                    category_name, current_page
                )
                break

            last_page_names = current_page_names
            product_data.extend(page_products)

            log.info(
                "[Регард/%s] Стр.%d — %d товаров (всего: %d)",
                category_name, current_page,
                len(page_products), len(product_data)
            )

            if not page_products:
                break

            current_page += 1
            await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    except Exception as e:
        log.error("[Регард/%s] Критическая ошибка каталога: %s", category_name, e)
    finally:
        if page:
            await _safe_close(page)
        if ctx:
            await _safe_close(ctx)
        await _close_browser_safe(browser, category_name)

    return product_data


# ────────────────────────────────────────────────────────────────────────────
# ФАЗА 2: БАТЧ СТРАНИЦ ТОВАРОВ
# ─────────────────────────────────────────────────────────────────────────────

async def _process_batch_regard(
    product_data: list[dict],
    category_name: str
) -> list[dict]:
    """Открывает до BATCH_SIZE вкладок без картинок."""
    results: list[dict] = []

    browser_cm = _make_browser(block_images=True)

    try:
        browser = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[Регард/%s] Не удалось запустить браузер (батч): %s", category_name, e)
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
                await asyncio.sleep(random.uniform(0.3, 0.7))
            except Exception as e:
                log.debug(
                    "[Регард] Вкладка не открылась %s: %s",
                    p.get("url", ""), e
                )

        await asyncio.sleep(BATCH_LOAD_PAUSE)

        for entry in pages:
            tab     = entry["tab"]
            product = entry["product"]
            try:
                try:
                    await tab.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass

                await asyncio.sleep(2.5)

                specs = await _collect_specs_regard(tab)

                price_text = product.get("price_from_catalog", "---")
                if price_text == "---":
                    price_text = await _get_price_from_product_page(tab)
                    if price_text != "---":
                        log.info(
                            "  [Регард] Цена со страницы товара: %s → %s",
                            product["name"][:35], price_text
                        )

                from parser_engine import _extract_logic
                extracted = _extract_logic(category_name, product["name"], specs)

                results.append({
                    "id": (
                        int(product["regard_id"])
                        if product.get("regard_id", "").isdigit()
                        else abs(hash(product["name"] + category_name)) % (10 ** 9)
                    ),
                    "name":          product["name"],
                    "category":      category_name,
                    "priceCitilink": "---",
                    "priceRegard":   price_text,
                    "imageUrl":      product["image"],
                    "productUrl":    product["url"],
                    "source":        "regard",
                    **extracted,
                    "specs":         specs,
                })
                log.info(
                    "  ✓ [Регард] %s — %s | specs: %d полей",
                    product["name"][:40], price_text, len(specs)
                )

            except Exception as e:
                log.debug(
                    "[Регард] Ошибка %s: %s",
                    product["name"][:40], e
                )
            finally:
                await _safe_close(tab)

    except Exception as e:
        log.error("[Регард] Ошибка батча: %s", e)
    finally:
        if ctx:
            await _safe_close(ctx)
        await _close_browser_safe(browser, category_name)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# СБОР ХАРАКТЕРИСТИК СО СТРАНИЦЫ ТОВАРА
# ─────────────────────────────────────────────────────────────────────────────

async def _expand_all_sections(page) -> None:
    """
    Раскрывает все закрытые секции аккордеона характеристик.
    Используем только JS-клики — никаких Playwright .click()
    """
    try:
        result = await asyncio.wait_for(
            page.evaluate("""
                () => {
                    const sections = document.querySelectorAll(
                        '[class*="CharacteristicsSection_section"]'
                    );
                    let clicked = 0;
                    sections.forEach(section => {
                        const hiddenContent = section.querySelector('[aria-hidden="true"]');
                        if (hiddenContent) {
                            const header = section.querySelector(
                                'h3, [class*="CharacteristicsSection_title"]'
                            );
                            if (header) {
                                header.click();
                                clicked++;
                            }
                        }
                    });
                    return clicked;
                }
            """),
            timeout=5.0
        )

        if result and int(result) > 0:
            log.debug("[Регард] Раскрыто секций: %d", result)
            await asyncio.sleep(0.5)

    except asyncio.TimeoutError:
        log.debug("[Регард] Таймаут раскрытия секций")
    except Exception as e:
        log.debug("[Регард] Ошибка раскрытия секций: %s", e)


async def _collect_specs_regard(page) -> dict:
    """Собирает характеристики со страницы товара Регарда."""
    specs = {}

    try:
        await asyncio.sleep(1.5)

        await _expand_all_sections(page)
        await asyncio.sleep(0.8)

        items = []
        for _ in range(10):
            items = await _eles(page, 'div[class*="CharacteristicsItem_item"]')
            if items:
                break
            await asyncio.sleep(1.0)
        else:
            log.warning("[Регард] Характеристики не появились за 10 сек")
            return specs

        log.debug("[Регард] Найдено строк характеристик: %d", len(items))

        for item in items:
            try:
                name_el = await _ele(
                    item,
                    'div[class*="CharacteristicsItem_name"]',
                    timeout=0.5
                )
                val_el = await _ele(
                    item,
                    'div[class*="CharacteristicsItem_value"]',
                    timeout=0.5
                )

                if not name_el or not val_el:
                    continue

                name_txt = await _text(name_el)
                name_txt = re.sub(r'[\s\.\xa0\:·]+$', '', name_txt).strip()
                name_txt = re.sub(r'\.{2,}', '', name_txt).strip()

                if not name_txt or len(name_txt) < 2:
                    continue

                val_txt = await _text(val_el)

                if not val_txt:
                    link_el = await _ele(val_el, 'a', timeout=0.3)
                    if link_el:
                        val_txt = await _text(link_el)

                if not val_txt:
                    try:
                        inner = await val_el.inner_html() or ""
                        val_txt = re.sub(r'<[^>]+>', ' ', inner).strip()
                        val_txt = re.sub(r'\s+', ' ', val_txt).strip()
                    except Exception:
                        pass

                if not val_txt:
                    try:
                        val_txt = await val_el.evaluate(
                            "el => el.textContent.trim()"
                        ) or ""
                    except Exception:
                        pass

                val_txt = val_txt.strip()

                if name_txt and val_txt:
                    specs[name_txt] = val_txt

            except Exception as e:
                log.debug("[Регард] Ошибка строки характеристики: %s", e)
                continue

    except Exception as e:
        log.debug("[Регард] Ошибка сбора характеристик: %s", e)

    return specs