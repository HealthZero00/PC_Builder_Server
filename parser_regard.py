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
import os
from pathlib import Path
from camoufox.async_api import AsyncCamoufox

log = logging.getLogger(__name__)

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int | None) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    if value.strip().lower() in {"none", "null", "all", "0"}:
        return None
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


PAGES_LIMIT        = _env_int("RG_PAGES_LIMIT",         None)
PRODUCTS_PER_PAGE  = _env_int("RG_PRODUCTS_PER_PAGE",   None)
BATCH_SIZE         = _env_int("RG_BATCH_SIZE",           4) or 4
MAX_RETRIES        = _env_int("RG_MAX_RETRIES",          3) or 3

SCROLL_STEPS       = _env_int("RG_SCROLL_STEPS",         2) or 2
SCROLL_PAUSE       = _env_float("RG_SCROLL_PAUSE",       0.25)
PAGE_LOAD_PAUSE    = _env_float("RG_PAGE_LOAD_PAUSE",    1.5)
BATCH_LOAD_PAUSE   = _env_float("RG_BATCH_LOAD_PAUSE",   2.0)
PAGE_DELAY_MIN     = _env_float("RG_PAGE_DELAY_MIN",     1.0)
PAGE_DELAY_MAX     = _env_float("RG_PAGE_DELAY_MAX",     2.5)

WAIT_ON_BLOCK      = _env_bool("RG_WAIT_ON_BLOCK",       True)
BLOCK_WAIT_SECONDS = _env_int("RG_BLOCK_WAIT_SECONDS",   120) or 120

PAGE_TIMEOUT_MS    = (_env_int("RG_PAGE_TIMEOUT_SECONDS", 30) or 30) * 1000

DEBUG_DIR = Path(os.getenv("RG_DEBUG_DIR", "regard_debug")).resolve()

BASE_URL = "https://www.regard.ru"

BLOCK_STATUS_CODES = {401, 403, 407, 408, 409, 423, 429, 451, 500, 502, 503}

BLOCK_TEXT_MARKERS = (
    "access denied",
    "attention required",
    "blocked",
    "captcha",
    "cloudflare",
    "ddos-guard",
    "forbidden",
    "rate limit",
    "too many requests",
    "verify you are human",
    "доступ временно ограничен",
    "доступ запрещен",
    "доступ ограничен",
    "защита от роботов",
    "капча",
    "подозрительная активность",
    "подтвердите, что вы не робот",
    "проверка браузера",
    "слишком много запросов",
)

def _make_browser(block_images: bool = False) -> AsyncCamoufox:
    """Создаёт контекст AsyncCamoufox."""
    return AsyncCamoufox(
        headless=True,
        os="windows",
        humanize=True,
        block_images=block_images,
        i_know_what_im_doing=True,
    )


async def _close_browser_safe(browser, label: str = "Regard") -> None:
    """Безопасное закрытие браузера."""
    try:
        await browser.close()
    except Exception as e:
        err = str(e)
        if "Connection closed" in err or "Browser.close" in err:
            log.debug("[Regard/%s] Browser closed with expected Node.js 24 error: %s", label, err)
        else:
            log.warning("[Regard/%s] Unexpected browser close error: %s", label, e)


async def _safe_close(obj) -> None:
    try:
        await obj.close()
    except Exception:
        pass


def _setup_page_handlers(page) -> None:
    page.on("pageerror", lambda _: None)
    page.on("crash",     lambda _: None)


async def _scroll_page(page) -> None:
    try:
        await page.mouse.wheel(0, 800)
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


async def _body_probe(page, limit: int = 3500) -> str:
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        body = ""
    text = f"{title} {body}"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


async def _block_reason(page, response=None) -> str:
    if response is not None:
        try:
            status = response.status
            if status in BLOCK_STATUS_CODES:
                return f"HTTP {status}"
        except Exception:
            pass
    probe = (await _body_probe(page)).lower()
    for marker in BLOCK_TEXT_MARKERS:
        if marker in probe:
            return marker
    return ""


async def _dump_debug_page(page, label: str, reason: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        safe  = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label)[:80]
        stamp = int(asyncio.get_running_loop().time() * 1000)
        base  = DEBUG_DIR / f"{stamp}_{safe}"
        html  = await page.content()
        base.with_suffix(".html").write_text(html, encoding="utf-8")
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        log.warning("[Regard] Debug dump saved for block '%s': %s", reason, base)
    except Exception as e:
        log.debug("[Regard] Could not save debug dump: %s", e)


async def _handle_block(page, label: str, response=None) -> bool:
    reason = await _block_reason(page, response)
    if not reason:
        return False

    log.warning("[Regard/%s] Block-like page detected: %s", label, reason)
    await _dump_debug_page(page, label, reason)

    if WAIT_ON_BLOCK:
        log.warning(
            "[Regard/%s] Waiting %ds for manual captcha/challenge resolution...",
            label, BLOCK_WAIT_SECONDS,
        )
        try:
            await page.wait_for_timeout(BLOCK_WAIT_SECONDS * 1000)
            if not await _block_reason(page):
                log.info("[Regard/%s] Challenge resolved, continuing", label)
                return False
        except Exception:
            pass

    return True


async def _goto_with_retries(page, url: str, label: str, wait_until: str = "domcontentloaded"):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(PAGE_LOAD_PAUSE + random.uniform(0.1, 0.8))
            if await _handle_block(page, label, response):
                await asyncio.sleep(random.uniform(8, 16) * attempt)
                continue
            return response
        except Exception as e:
            last_error = e
            log.warning("[Regard/%s] Load attempt %d failed: %s", label, attempt, e)
            await asyncio.sleep(random.uniform(4, 9) * attempt)

    log.error("[Regard/%s] Could not load %s after %d attempts: %s", label, url, MAX_RETRIES, last_error)
    return None


async def _uncheck_modifications(page, category_name: str) -> None:
    try:
        label = await asyncio.wait_for(
            page.wait_for_selector(
                'label[for*="Модификации"], label[for*="odifikac"]',
                state="attached"
            ),
            timeout=4.0
        )

        if label:
            await page.evaluate(
                "(el) => el.click()",
                label
            )
            await asyncio.sleep(1.5)
            log.info("[Regard/%s] Checkbox 'Modifications' unchecked ✓", category_name)

    except asyncio.TimeoutError:
        log.debug("[Regard/%s] Modifications checkbox not found (may not exist on this page)", category_name)
    except Exception as e:
        log.warning("[Regard/%s] Could not uncheck 'Modifications': %s", category_name, e)

async def _get_price_from_card(card) -> str:
    try:
        hidden = await _ele(card, 'span.visually-hidden', timeout=0.3)
        if hidden:
            text = await _text(hidden)
            digits = "".join(filter(str.isdigit, text))
            if digits and int(digits) > 100:
                return "{:,}".format(int(digits)).replace(",", " ") + " руб"

        for attr in ("data-price", "data-product-price", "price"):
            val = await _attr(card, attr)
            if val and str(val).isdigit() and int(val) > 100:
                return "{:,}".format(int(val)).replace(",", " ") + " руб"

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
        log.debug("[Regard] Card price parse error: %s", e)

    return "---"

async def _get_price_from_product_page(page) -> str:
    try:
        # Способ 1: visually-hidden
        hidden = await _ele(page, 'span.visually-hidden', timeout=2.0)
        if hidden:
            text = await _text(hidden)
            digits = "".join(filter(str.isdigit, text))
            if digits and 100 < int(digits) < 10_000_000:
                return "{:,}".format(int(digits)).replace(",", " ") + " руб"

        price_span = await _ele(page, 'span[class*="Price_price"]', timeout=2.0)
        if price_span:
            raw = await _text(price_span)
            digits = "".join(filter(str.isdigit, raw))
            if digits and 100 < int(digits) < 10_000_000:
                return "{:,}".format(int(digits)).replace(",", " ") + " руб"

        price_block = await _ele(page, 'div[class*="PriceBlock"]', timeout=2.0)
        if price_block:
            raw = await _text(price_block)
            m = re.search(r'([\d\s]{3,})\s*₽', raw)
            if m:
                digits = "".join(filter(str.isdigit, m.group(1)))
                if digits and 100 < int(digits) < 10_000_000:
                    return "{:,}".format(int(digits)).replace(",", " ") + " руб"

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
        log.debug("[Regard] Product page price parse error: %s", e)

    return "---"


async def scrape_regard(url: str, category_name: str) -> list[dict]:
    log.info("[Regard] Начинаю парсинг категории '%s' -> %s", category_name, url)

    product_data = await _collect_catalog_regard(url, category_name)
    if not product_data:
        log.warning("[Regard/%s] Не найдено товаров в каталоге или сработал блок.", category_name)
        return []

    log.info(
        "[Regard/%s] Фаза 2: сбор характеристик %d товаров...",
        category_name, len(product_data)
    )

    all_results: list[dict] = []
    for batch_start in range(0, len(product_data), BATCH_SIZE):
        batch = product_data[batch_start: batch_start + BATCH_SIZE]
        batch_results = await _process_batch_regard(batch, category_name)
        all_results.extend(batch_results)

        if batch_start + BATCH_SIZE < len(product_data):
            await asyncio.sleep(random.uniform(2.0, 4.5))

    log.info("[Regard/%s] Готово. Итого: %d товаров успешно обработано.", category_name, len(all_results))
    return all_results

async def _collect_catalog_regard(url: str, category_name: str) -> list[dict]:
    product_data: list[dict]  = []
    last_page_names: set[str] = set()
    modifications_unchecked   = False

    browser_cm = _make_browser(block_images=False)

    try:
        browser = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[Regard/%s] Could not start browser: %s", category_name, e)
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
                    log.warning("[Regard/%s] Page closed, recreating...", category_name)
                    page = await ctx.new_page()
                    _setup_page_handlers(page)
            except Exception:
                pass

            log.info("[Regard/%s] Catalog page %d -> %s", category_name, current_page, target_url)

            response = await _goto_with_retries(page, target_url, f"{category_name}_p{current_page}")
            if response is None:
                break

            if not modifications_unchecked:
                await _uncheck_modifications(page, category_name)
                modifications_unchecked = True
                await asyncio.sleep(0.5)

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
                log.info("[Regard/%s] No product cards on page %d", category_name, current_page)
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
                        "[Regard/%s] Catalog price for '%s': %s",
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
                    log.debug("[Regard/%s] Card parse error: %s", category_name, e)

            if current_page > 1 and current_page_names == last_page_names:
                log.info("[Regard/%s] Page %d duplicates previous page, stopping", category_name, current_page)
                break

            last_page_names = current_page_names
            product_data.extend(page_products)

            log.info(
                "[Regard/%s] Page %d: %d products, total %d",
                category_name, current_page, len(page_products), len(product_data),
            )

            if not page_products:
                break

            current_page += 1
            await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    except Exception as e:
        log.error("[Regard/%s] Catalog critical error: %s", category_name, e)
    finally:
        if page:
            await _safe_close(page)
        if ctx:
            await _safe_close(ctx)
        await _close_browser_safe(browser, category_name)

    return product_data


async def _process_batch_regard(
    product_data: list[dict],
    category_name: str
) -> list[dict]:
    results: list[dict] = []

    browser_cm = _make_browser(block_images=True)

    try:
        browser = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[Regard/%s] Could not start browser for batch: %s", category_name, e)
        return results

    ctx = None

    try:
        ctx = await browser.new_context()
        pages: list[dict] = []

        for p in product_data:
            try:
                tab = await ctx.new_page()
                _setup_page_handlers(tab)
                await tab.goto(p["url"], wait_until="commit", timeout=PAGE_TIMEOUT_MS)
                pages.append({"tab": tab, "product": p})
                await asyncio.sleep(random.uniform(0.3, 0.7))
            except Exception as e:
                log.debug("[Regard/%s] Tab failed to open %s: %s", category_name, p.get("url", ""), e)

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
                            "  [Regard] Price from product page: %s -> %s",
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
                    "  [Regard] Успешно: %s — %s | specs: %d fields",
                    product["name"][:40], price_text, len(specs)
                )

            except Exception as e:
                log.debug("[Regard/%s] Specs error for %s: %s", category_name, product["name"][:40], e)
            finally:
                await _safe_close(tab)

    except Exception as e:
        log.error("[Regard/%s] Batch critical error: %s", category_name, e)
    finally:
        if ctx:
            await _safe_close(ctx)
        await _close_browser_safe(browser, category_name)

    return results

async def _expand_all_sections(page) -> None:
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
            log.debug("[Regard] Expanded spec sections: %d", result)
            await asyncio.sleep(0.5)

    except asyncio.TimeoutError:
        log.debug("[Regard] Timeout expanding spec sections")
    except Exception as e:
        log.debug("[Regard] Error expanding spec sections: %s", e)


async def _collect_specs_regard(page) -> dict:
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
            log.warning("[Regard] Spec rows did not appear within 10s")
            return specs

        log.debug("[Regard] Spec rows found: %d", len(items))

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
                log.debug("[Regard] Spec row parse error: %s", e)
                continue

    except Exception as e:
        log.debug("[Regard] Specs collection error: %s", e)

    return specs