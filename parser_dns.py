"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

"""
parser_dns.py - parser for dns-shop.ru.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from camoufox.async_api import AsyncCamoufox

from parser_engine import _extract_logic

log = logging.getLogger(__name__)

BASE_URL = "https://www.dns-shop.ru"

URLS_DNS = {
    "Процессоры": "https://www.dns-shop.ru/catalog/17a899cd16404e77/processory/",
    "Материнские платы": "https://www.dns-shop.ru/catalog/17a89a0416404e77/materinskie-platy/",
    "Видеокарты": "https://www.dns-shop.ru/catalog/17a89aab16404e77/videokarty/",
    "Оперативная память": "https://www.dns-shop.ru/catalog/17a89a3916404e77/operativnaa-pamat-dimm/",
    "Блоки питания": "https://www.dns-shop.ru/catalog/17a89c2216404e77/bloki-pitania/",
    "Корпуса": "https://www.dns-shop.ru/catalog/17a89c5616404e77/korpusa/",
    "Кулеры": "https://www.dns-shop.ru/catalog/17a9cc2d16404e77/kulery-dla-processorov/",
    "СЖО": "https://www.dns-shop.ru/catalog/17a9cc9816404e77/sistemy-zidkostnogo-ohlazdenia/",
    "SSD": "https://www.dns-shop.ru/catalog/8a9ddfba20724e77/ssd-nakopiteli/",
    "SSD M.2": "https://www.dns-shop.ru/catalog/dd58148920724e77/ssd-m2-nakopiteli/",
}

PUBLIC_CATEGORY_ALIASES = {
    "SSD M.2": "SSD",
}

LOGIC_CATEGORY_ALIASES = {
    "СЖО": "Кулеры",
    "SSD M.2": "SSD",
}


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


PAGES_LIMIT = _env_int("DNS_PAGES_LIMIT", None)
PRODUCTS_PER_PAGE = _env_int("DNS_PRODUCTS_PER_PAGE", None)
BATCH_SIZE = _env_int("DNS_BATCH_SIZE", 4) or 2
MAX_RETRIES = _env_int("DNS_MAX_RETRIES", 3) or 3

SCROLL_STEPS = _env_int("DNS_SCROLL_STEPS", 6) or 6
SCROLL_PAUSE = _env_float("DNS_SCROLL_PAUSE", 0.45)
PAGE_LOAD_PAUSE = _env_float("DNS_PAGE_LOAD_PAUSE", 2.2)
PAGE_DELAY_MIN = _env_float("DNS_PAGE_DELAY_MIN", 2.5)
PAGE_DELAY_MAX = _env_float("DNS_PAGE_DELAY_MAX", 6.0)
BATCH_LOAD_PAUSE = _env_float("DNS_BATCH_LOAD_PAUSE", 2.8)

HEADLESS = _env_bool("DNS_HEADLESS", True)
HUMANIZE: bool | float = _env_float("DNS_HUMANIZE", 1.6)
USE_PERSISTENT_PROFILE = _env_bool("DNS_PERSISTENT_PROFILE", True)
WAIT_ON_BLOCK = _env_bool("DNS_WAIT_ON_BLOCK", True)
BLOCK_WAIT_SECONDS = _env_int("DNS_BLOCK_WAIT_SECONDS", 120) or 120

PROFILE_DIR = Path(os.getenv("DNS_PROFILE_DIR", ".camoufox_dns_profile")).resolve()
DEBUG_DIR = Path(os.getenv("DNS_DEBUG_DIR", "dns_debug")).resolve()

PAGE_TIMEOUT_MS = (_env_int("DNS_PAGE_TIMEOUT_SECONDS", 40) or 40) * 1000
CATALOG_WAIT_MS = (_env_int("DNS_CATALOG_WAIT_SECONDS", 20) or 20) * 1000
SPECS_WAIT_MS = (_env_int("DNS_SPECS_WAIT_SECONDS", 18) or 18) * 1000

CATALOG_CARD_SELECTOR = (
    "div.catalog-product[data-id='product'], "
    "div.catalog-product, "
    "[data-id='product'][data-code], "
    "[data-product][data-code]"
)

PRODUCT_NAME_SELECTORS = [
    "a.catalog-product__name",
    ".catalog-product__name a",
    ".catalog-product__name",
    "a[href*='/product/']",
]

PRODUCT_PRICE_SELECTORS = [
    ".product-buy__price",
    "[class*='product-buy__price']",
    "[data-role='price']",
    "[itemprop='price']",
]

IMAGE_SELECTORS = [
    ".catalog-product__image img",
    "img[data-src]",
    "img[src]",
    "source[srcset]",
]

SPEC_ROW_SELECTORS = [
    ".product-characteristics__spec",
    "li[class*='product-characteristics__spec']",
    "[class*='product-characteristics__spec']",
]

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

TRACKER_HOST_PARTS = (
    "adfox",
    "analytics",
    "doubleclick",
    "facebook",
    "google-analytics",
    "googletagmanager",
    "mc.yandex",
    "metrika",
    "rambler",
    "top-fwz1.mail.ru",
    "vk.com/rtrg",
)


def _parse_proxy(value: str | None = None) -> dict[str, str] | None:
    raw = (value or os.getenv("DNS_PROXY") or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return {"server": raw}

    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def _make_browser(block_images: bool = False) -> AsyncCamoufox:
    proxy = _parse_proxy()
    options: dict[str, Any] = {
        "headless": HEADLESS,
        "os": "windows",
        "locale": "ru-RU",
        "humanize": HUMANIZE,
        "block_images": block_images,
        "block_webrtc": True,
        "enable_cache": True,
        "i_know_what_im_doing": True,
    }
    if proxy:
        options["proxy"] = proxy
        if _env_bool("DNS_GEOIP", True):
            options["geoip"] = True
    elif _env_bool("DNS_GEOIP", False):
        options["geoip"] = True

    if USE_PERSISTENT_PROFILE:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        options["persistent_context"] = True
        options["user_data_dir"] = str(PROFILE_DIR)

    return AsyncCamoufox(**options)


async def _safe_close(obj: Any) -> None:
    try:
        await obj.close()
    except Exception:
        pass


async def _close_camoufox(manager: AsyncCamoufox, label: str) -> None:
    try:
        await manager.__aexit__(None, None, None)
    except Exception as e:
        err = str(e)
        if "Connection closed" in err or "Browser.close" in err:
            log.debug("[%s] Browser closed with expected Playwright noise: %s", label, err)
        else:
            log.debug("[%s] Camoufox close warning: %s", label, e)


def _setup_page_handlers(page: Any) -> None:
    page.on("pageerror", lambda _: None)
    page.on("crash", lambda _: None)
    page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))


async def _apply_dns_preferences(context: Any) -> None:
    cookies = []
    city_id = os.getenv("DNS_CITY_ID", "").strip()
    city_path = os.getenv("DNS_CITY_PATH", "").strip()

    if city_id:
        cookies.append({
            "name": "city_id",
            "value": city_id,
            "domain": ".dns-shop.ru",
            "path": "/",
        })
    if city_path:
        cookies.append({
            "name": "city_path",
            "value": city_path,
            "domain": ".dns-shop.ru",
            "path": "/",
        })

    if cookies:
        try:
            await context.add_cookies(cookies)
        except Exception as e:
            log.debug("[DNS] Could not apply region cookies: %s", e)


async def _new_context(browser_or_context: Any) -> tuple[Any, bool]:
    if hasattr(browser_or_context, "new_context"):
        context = await browser_or_context.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.6,en;q=0.5",
                "DNT": "1",
            },
        )
        owns_context = True
    else:
        context = browser_or_context
        owns_context = False

    await _apply_dns_preferences(context)
    return context, owns_context


async def _install_routes(page: Any, block_images: bool = False) -> None:
    async def _route(route: Any) -> None:
        request = route.request
        url = request.url.lower()
        resource_type = request.resource_type
        try:
            if resource_type in {"font", "media"}:
                await route.abort()
                return
            if block_images and resource_type == "image":
                await route.abort()
                return
            if any(part in url for part in TRACKER_HOST_PARTS):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await page.route("**/*", _route)
    except Exception:
        pass


async def _new_page(context: Any, block_images: bool = False) -> Any:
    page = await context.new_page()
    _setup_page_handlers(page)
    await _install_routes(page, block_images=block_images)
    return page


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\u2009", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


async def _text(el: Any) -> str:
    try:
        return _clean_text(await el.text_content())
    except Exception:
        return ""


async def _attr(el: Any, name: str) -> str:
    try:
        return await el.get_attribute(name) or ""
    except Exception:
        return ""


async def _ele(container: Any, selector: str, timeout: float = 1.0) -> Any | None:
    try:
        if timeout > 0.5:
            return await container.wait_for_selector(
                selector,
                timeout=int(timeout * 1000),
                state="attached",
            )
        return await container.query_selector(selector)
    except Exception:
        return None


async def _eles(container: Any, selector: str) -> list[Any]:
    try:
        return await container.query_selector_all(selector)
    except Exception:
        return []


async def _first(container: Any, selectors: list[str], timeout: float = 0.8) -> Any | None:
    for selector in selectors:
        el = await _ele(container, selector, timeout=timeout)
        if el:
            return el
    return None


def _format_price_rub(digits: str) -> str:
    digits = "".join(filter(str.isdigit, digits))
    if not digits:
        return "---"
    value = int(digits)
    if value <= 100 or value >= 10_000_000:
        return "---"
    return f"{value:,}".replace(",", " ") + " руб"


def _extract_price_from_text(text: str) -> str:
    raw = _clean_text(text)
    if not raw:
        return "---"

    price_matches = re.findall(r"(\d[\d\s]{2,10})\s*(?:₽|руб|р\b)", raw, re.I)
    for match in price_matches:
        price = _format_price_rub(match)
        if price != "---":
            return price

    digits = "".join(filter(str.isdigit, raw))
    return _format_price_rub(digits)


async def _extract_price(container: Any) -> str:
    for selector in PRODUCT_PRICE_SELECTORS:
        el = await _ele(container, selector, timeout=0.5)
        if not el:
            continue
        text = await _text(el)
        price = _extract_price_from_text(text)
        if price != "---":
            return price

        content = await _attr(el, "content")
        price = _extract_price_from_text(content)
        if price != "---":
            return price

    try:
        html = await container.inner_html()
        return _extract_price_from_text(html)
    except Exception:
        return "---"


def _absolute_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    return urljoin(BASE_URL, href)


def _image_from_src(src: str) -> str:
    if not src:
        return ""
    # srcset: "url1 1x, url2 2x" → берём url1
    src = src.split(" ")[0].split(",")[0].strip()
    if not src or src.startswith("data:"):
        return ""
    return _absolute_url(src)


def _upscale_dns_thumb(url: str, size: int = 600) -> str:
    return re.sub(r"/fit/\d+/\d+/", f"/fit/{size}/{size}/", url)


async def _extract_detail_page_image(tab: Any) -> str:
    try:
        strip_items = await _eles(tab, "picture.product-images-slider__item")
        for pic in strip_items:
            # Сначала source[srcset] — webp лучше качеством
            source_el = await _ele(pic, "source[srcset]", timeout=0.2)
            if source_el:
                srcset = await _attr(source_el, "srcset")
                url = _upscale_dns_thumb(_image_from_src(srcset))
                if url:
                    log.debug("[DNS] Image from strip source[srcset]: %s", url[:80])
                    return url

            # Потом img
            img_el = await _ele(pic, "img", timeout=0.2)
            if img_el:
                src = (
                    await _attr(img_el, "data-src")
                    or await _attr(img_el, "src")
                )
                url = _upscale_dns_thumb(_image_from_src(src))
                if url:
                    log.debug("[DNS] Image from strip img: %s", url[:80])
                    return url
    except Exception:
        pass

    try:
        source_el = await _ele(
            tab,
            "picture.product-images-slider__main source[srcset]",
            timeout=2.0,
        )
        if source_el:
            srcset = await _attr(source_el, "srcset")
            url = _image_from_src(srcset)
            if url:
                log.debug("[DNS] Image from main slider source[srcset]: %s", url[:80])
                return url
    except Exception:
        pass

    try:
        img_el = await _ele(
            tab,
            "picture.product-images-slider__main img",
            timeout=1.0,
        )
        if img_el:
            src = (
                await _attr(img_el, "data-src")
                or await _attr(img_el, "srcset")
                or await _attr(img_el, "src")
            )
            url = _image_from_src(src)
            if url:
                log.debug("[DNS] Image from main slider img: %s", url[:80])
                return url
    except Exception:
        pass

    try:
        for attempt in range(8):  # 8 × 0.5с = до 4 секунд
            src3d_source = await _ele(
                tab,
                ".slider3d-image__image-wrap source[srcset]",
                timeout=0.3,
            )
            if src3d_source:
                srcset = await _attr(src3d_source, "srcset")
                url = _image_from_src(srcset)
                if url:
                    log.debug(
                        "[DNS] Image from 3D slider source[srcset] (attempt %d): %s",
                        attempt, url[:80],
                    )
                    return url

            img3d = await _ele(tab, ".slider3d-image__img", timeout=0.3)
            if img3d:
                src = (
                    await _attr(img3d, "data-src")
                    or await _attr(img3d, "srcset")
                    or await _attr(img3d, "src")
                )
                url = _image_from_src(src)
                if url:
                    log.debug(
                        "[DNS] Image from 3D slider img (attempt %d): %s",
                        attempt, url[:80],
                    )
                    return url

            await asyncio.sleep(0.5)
    except Exception:
        pass

    try:
        meta_el = await _ele(tab, 'meta[property="og:image"]', timeout=1.5)
        if meta_el:
            content = await _attr(meta_el, "content")
            if content:
                url = _absolute_url(content)
                log.debug("[DNS] Image from og:image: %s", url[:80])
                return url
    except Exception:
        pass

    try:
        page_url = tab.url
    except Exception:
        page_url = "unknown"
    log.warning("[DNS] Could not extract any image from product page: %s", page_url)
    return ""


def _product_to_characteristics_url(product_url: str) -> str:
    parsed = urlparse(product_url)
    match = re.match(r"^/product/([^/]+)/(.+)$", parsed.path)
    if not match:
        return product_url
    product_id, slug = match.groups()
    return f"{parsed.scheme}://{parsed.netloc}/product/characteristics/{product_id}/{slug}"


def _stable_id(name: str, category: str, code: str = "") -> int:
    if code and code.isdigit():
        return int(code)
    digest = hashlib.sha1(f"dns:{category}:{name}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % (10 ** 9)


def _public_category(category: str) -> str:
    return PUBLIC_CATEGORY_ALIASES.get(category, category)


def _logic_category(category: str) -> str:
    return LOGIC_CATEGORY_ALIASES.get(category, _public_category(category))


async def _human_pause(min_s: float = 0.3, max_s: float = 1.2) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _human_warmup(page: Any) -> None:
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 900}
        width = int(viewport.get("width") or 1280)
        height = int(viewport.get("height") or 900)
        await page.mouse.move(
            random.randint(80, max(120, width - 120)),
            random.randint(80, max(120, height - 120)),
            steps=random.randint(8, 20),
        )
    except Exception:
        pass
    await _human_pause(0.2, 0.8)


async def _scroll_page(page: Any, steps: int = SCROLL_STEPS) -> None:
    for _ in range(steps):
        try:
            await page.mouse.wheel(0, random.randint(500, 1100))
        except Exception:
            pass
        await asyncio.sleep(random.uniform(SCROLL_PAUSE * 0.7, SCROLL_PAUSE * 1.6))


async def _body_probe(page: Any, limit: int = 3500) -> str:
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        body = ""
    return _clean_text(f"{title} {body}")[:limit]


async def _block_reason(page: Any, response: Any | None = None) -> str:
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


async def _dump_debug_page(page: Any, label: str, reason: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label)[:80]
        stamp = int(asyncio.get_running_loop().time() * 1000)
        base = DEBUG_DIR / f"{stamp}_{safe}"
        html = await page.content()
        (base.with_suffix(".html")).write_text(html, encoding="utf-8")
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        log.warning("[DNS] Debug dump saved for block '%s': %s", reason, base)
    except Exception as e:
        log.debug("[DNS] Could not save debug dump: %s", e)


async def _handle_block(page: Any, label: str, response: Any | None = None) -> bool:
    reason = await _block_reason(page, response)
    if not reason:
        return False

    log.warning("[DNS/%s] Block-like page detected: %s", label, reason)
    await _dump_debug_page(page, label, reason)

    if WAIT_ON_BLOCK and not HEADLESS:
        log.warning(
            "[DNS/%s] Waiting %s sec for manual browser challenge/captcha resolution...",
            label,
            BLOCK_WAIT_SECONDS,
        )
        try:
            await page.wait_for_timeout(BLOCK_WAIT_SECONDS * 1000)
            if not await _block_reason(page):
                log.info("[DNS/%s] Challenge looks resolved, continuing", label)
                return False
        except Exception:
            pass

    return True


async def _goto_with_retries(page: Any, url: str, label: str) -> Any | None:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )
            await asyncio.sleep(PAGE_LOAD_PAUSE + random.uniform(0.2, 1.4))
            if await _handle_block(page, label, response):
                await asyncio.sleep(random.uniform(8, 16) * attempt)
                continue
            return response
        except Exception as e:
            last_error = e
            log.warning("[DNS/%s] Load attempt %d failed: %s", label, attempt, e)
            await asyncio.sleep(random.uniform(4, 9) * attempt)

    log.error("[DNS/%s] Could not load %s: %s", label, url, last_error)
    return None


async def _wait_for_catalog(page: Any, category_name: str) -> bool:
    try:
        await page.wait_for_selector(
            CATALOG_CARD_SELECTOR,
            timeout=CATALOG_WAIT_MS,
            state="attached",
        )
        return True
    except Exception:
        if await _handle_block(page, f"{category_name}_catalog_wait"):
            return False
        return False


async def _extract_product_card(card: Any, category_name: str) -> dict[str, Any] | None:
    try:
        data = await card.evaluate(
            """
            (card) => {
                const nameSelectors = [
                    'a.catalog-product__name',
                    '.catalog-product__name a',
                    '.catalog-product__name',
                    'a[href*="/product/"]'
                ];
                let linkEl = null;
                for (const sel of nameSelectors) {
                    linkEl = card.querySelector(sel);
                    if (linkEl) break;
                }
                if (!linkEl) return null;

                let href = linkEl.getAttribute('href') || '';
                if (!href) {
                    const nested = linkEl.querySelector('a[href*="/product/"]');
                    href = nested ? (nested.getAttribute('href') || '') : '';
                }

                const name = linkEl.getAttribute('title')
                    || linkEl.getAttribute('aria-label')
                    || (linkEl.textContent || '').trim();

                const imgSelectors = [
                    '.catalog-product__image img',
                    'img[data-src]',
                    'img[src]',
                    'source[srcset]'
                ];
                let imageUrl = '';
                for (const sel of imgSelectors) {
                    const img = card.querySelector(sel);
                    if (!img) continue;
                    const src = img.getAttribute('data-src')
                        || img.getAttribute('srcset')
                        || img.getAttribute('src');
                    if (src) { imageUrl = src; break; }
                }

                const priceSelectors = [
                    '.product-buy__price',
                    '[class*="product-buy__price"]',
                    '[data-role="price"]',
                    '[itemprop="price"]'
                ];
                let priceText = '';
                for (const sel of priceSelectors) {
                    const el = card.querySelector(sel);
                    if (!el) continue;
                    priceText = (el.getAttribute('content') || el.textContent || '').trim();
                    if (priceText) break;
                }
                if (!priceText) {
                    priceText = card.innerHTML;
                }

                const code = card.getAttribute('data-code') || '';
                const dnsUuid = card.getAttribute('data-product')
                    || card.getAttribute('data-entity') || '';

                return { href, name, imageUrl, priceText, code, dnsUuid };
            }
            """
        )

        if not data or not data.get("href") or not data.get("name"):
            return None

        product_url = _absolute_url(data["href"])
        if "/product/" not in product_url:
            return None

        name = _clean_text(data["name"])
        if not name or len(name) < 5:
            return None

        image_url = _image_from_src(data.get("imageUrl") or "")
        price = _extract_price_from_text(data.get("priceText") or "")
        code = data.get("code") or ""
        dns_uuid = data.get("dnsUuid") or ""

        return {
            "id": _stable_id(name, _public_category(category_name), code),
            "name": name,
            "url": product_url,
            "image": image_url,
            "price_from_catalog": price,
            "dns_code": code,
            "dns_uuid": dns_uuid,
        }
    except Exception as e:
        log.debug("[DNS/%s] Card parse error: %s", category_name, e)
        return None


def _catalog_page_url(base_url: str, page_num: int) -> str:
    if page_num <= 1:
        return base_url
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["p"] = str(page_num)
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return urlunparse(parsed._replace(path=path, query=urlencode(query)))


async def _collect_catalog_dns(url: str, category_name: str) -> list[dict[str, Any]]:
    product_data: list[dict[str, Any]] = []
    last_page_names: set[str] = set()

    browser_cm = _make_browser(block_images=False)
    try:
        browser_or_context = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[DNS/%s] Could not start Camoufox: %s", category_name, e)
        return product_data

    context = None
    owns_context = False
    page = None

    try:
        context, owns_context = await _new_context(browser_or_context)
        page = await _new_page(context, block_images=False)

        current_page = 1
        while PAGES_LIMIT is None or current_page <= PAGES_LIMIT:
            target_url = _catalog_page_url(url, current_page)
            log.info("[DNS/%s] Catalog page %d -> %s", category_name, current_page, target_url)

            response = await _goto_with_retries(page, target_url, f"{category_name}_p{current_page}")
            if response is None:
                break

            await _human_warmup(page)
            await _scroll_page(page)

            if not await _wait_for_catalog(page, category_name):
                log.info("[DNS/%s] No product cards on page %d", category_name, current_page)
                break

            cards = await _eles(page, CATALOG_CARD_SELECTOR)
            page_products: list[dict[str, Any]] = []
            current_names: set[str] = set()

            for card in cards:
                if PRODUCTS_PER_PAGE is not None and len(page_products) >= PRODUCTS_PER_PAGE:
                    break
                product = await _extract_product_card(card, category_name)
                if not product:
                    continue
                if product["name"] in current_names:
                    continue
                current_names.add(product["name"])
                page_products.append(product)

            if current_page > 1 and current_names and current_names == last_page_names:
                log.info("[DNS/%s] Page %d duplicates previous page, stopping", category_name, current_page)
                break

            last_page_names = current_names
            product_data.extend(page_products)
            log.info(
                "[DNS/%s] Page %d: %d products, total %d",
                category_name,
                current_page,
                len(page_products),
                len(product_data),
            )

            if not page_products:
                break

            current_page += 1
            await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    except Exception as e:
        log.error("[DNS/%s] Catalog critical error: %s", category_name, e)
    finally:
        if page:
            await _safe_close(page)
        if context and owns_context:
            await _safe_close(context)
        await _close_camoufox(browser_cm, f"DNS/{category_name}")

    return product_data


async def _click_expand_specs(page: Any) -> None:
    click_script = """
        () => {
            const candidates = [
                '.product-characteristics__expand',
                'button[class*="product-characteristics__expand"]',
                'button[class*="characteristics"][class*="expand"]',
                'button'
            ];
            let clicked = 0;
            for (const selector of candidates) {
                for (const el of document.querySelectorAll(selector)) {
                    const text = (el.innerText || el.textContent || '').toLowerCase();
                    if (text.includes('развернуть') || text.includes('показать все')) {
                        el.click();
                        clicked++;
                    }
                }
            }
            return clicked;
        }
    """
    try:
        clicked = await page.evaluate(click_script)
        if clicked:
            await asyncio.sleep(1.2)
    except Exception:
        pass


async def _wait_for_specs(page: Any) -> bool:
    selectors = ", ".join(SPEC_ROW_SELECTORS)
    try:
        await page.wait_for_selector(selectors, timeout=SPECS_WAIT_MS, state="attached")
        return True
    except Exception:
        return False


async def _collect_specs_by_dom(page: Any) -> dict[str, str]:
    script = """
        () => {
            const clean = (value) => (value || '')
                .replace(/[\\u00a0\\u2009]+/g, ' ')
                .replace(/\\s+/g, ' ')
                .replace(/[.:\\s]+$/g, '')
                .trim();
            const specs = {};
            const rows = document.querySelectorAll(
                '.product-characteristics__spec, li[class*="product-characteristics__spec"], [class*="product-characteristics__spec"]'
            );
            for (const row of rows) {
                const title = row.querySelector(
                    '.product-characteristics__spec-title, [class*="spec-title"]'
                );
                const value = row.querySelector(
                    '.product-characteristics__spec-value, [class*="spec-value"]'
                );
                if (!title || !value) continue;
                const key = clean(title.innerText || title.textContent);
                const val = clean(value.innerText || value.textContent);
                if (key && val && key.length > 1) specs[key] = val;
            }
            return specs;
        }
    """
    try:
        raw = await page.evaluate(script)
        if isinstance(raw, dict):
            return {str(k): _clean_text(str(v)) for k, v in raw.items() if k and v}
    except Exception:
        pass
    return {}


async def _collect_specs_by_selectors(page: Any) -> dict[str, str]:
    specs: dict[str, str] = {}
    rows: list[Any] = []
    for selector in SPEC_ROW_SELECTORS:
        rows = await _eles(page, selector)
        if rows:
            break

    for row in rows:
        try:
            name_el = (
                await _ele(row, ".product-characteristics__spec-title", timeout=0.2)
                or await _ele(row, "[class*='spec-title']", timeout=0.2)
            )
            value_el = (
                await _ele(row, ".product-characteristics__spec-value", timeout=0.2)
                or await _ele(row, "[class*='spec-value']", timeout=0.2)
            )
            if not name_el or not value_el:
                continue

            key = _clean_text(await _text(name_el)).rstrip(".:")
            value = _clean_text(await _text(value_el))
            if key and value:
                specs[key] = value
        except Exception:
            continue

    return specs


async def _collect_specs_dns(page: Any) -> dict[str, str]:
    await _click_expand_specs(page)
    if not await _wait_for_specs(page):
        return {}

    specs = await _collect_specs_by_dom(page)
    if specs:
        return specs
    return await _collect_specs_by_selectors(page)


async def _collect_price_from_product_page(page: Any) -> str:
    price = await _extract_price(page)
    if price != "---":
        return price

    try:
        price_js = await page.evaluate("""
            () => {
                const meta = document.querySelector('meta[itemprop="price"], [itemprop="price"]');
                if (meta) return meta.getAttribute('content') || meta.textContent;
                const scripts = [...document.querySelectorAll('script')].map(s => s.textContent || '').join('\\n');
                const m = scripts.match(/"price"\\s*:\\s*"?([0-9]{3,9})"?/);
                return m ? m[1] : '';
            }
        """)
        return _extract_price_from_text(str(price_js or ""))
    except Exception:
        return "---"


async def _extract_canonical_name(tab: Any) -> str:
    try:
        h1 = await _ele(tab, "[data-product-title]", timeout=2.0)
        if h1:
            text = _clean_text(await _text(h1))
            if text and len(text) >= 5:
                return text
    except Exception:
        pass
    return ""


async def _process_batch_dns(
        product_data: list[dict[str, Any]],
        category_name: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    browser_cm = _make_browser(block_images=True)

    try:
        browser_or_context = await browser_cm.__aenter__()
    except Exception as e:
        log.error("[DNS/%s] Could not start Camoufox for batch: %s", category_name, e)
        return results

    context = None
    owns_context = False

    try:
        context, owns_context = await _new_context(browser_or_context)

        for product in product_data:
            tab = None
            try:
                tab = await asyncio.wait_for(_new_page(context, block_images=True), timeout=10.0)
                target = _product_to_characteristics_url(product["url"])

                response = await _goto_with_retries(tab, target, product["name"][:45])
                if response is None:
                    await _safe_close(tab)
                    continue

                try:
                    await tab.wait_for_load_state("domcontentloaded", timeout=10000)
                    await _human_warmup(tab)
                    await _scroll_page(tab, steps=3)

                    specs = await _collect_specs_dns(tab)
                    if not specs and target != product["url"]:
                        await _goto_with_retries(tab, product["url"], "retry_specs")
                        specs = await _collect_specs_dns(tab)

                    canonical_name = await _extract_canonical_name(tab)
                    final_name = canonical_name or product["name"]
                    if canonical_name and canonical_name != product["name"]:
                        log.warning(
                            "[DNS] Имя с каталога не совпало со страницей товара (%s) — "
                            "беру название со страницы: '%s' -> '%s'",
                            product.get("url", "")[:60],
                            product["name"][:60],
                            canonical_name[:60],
                        )

                    price_text = product.get("price_from_catalog", "---")
                    if price_text == "---":
                        price_text = await _collect_price_from_product_page(tab)

                    image_url = product.get("image", "")
                    if not image_url:
                        image_url = await _extract_detail_page_image(tab)

                    public_category = _public_category(category_name)
                    extracted = _extract_logic(_logic_category(category_name), final_name, specs)

                    results.append({
                        "id": product.get("id") or _stable_id(final_name, public_category),
                        "name": final_name,
                        "category": public_category,
                        "priceDNS": price_text,
                        "price": price_text,
                        "imageUrl": image_url,
                        "productUrl": product.get("url", ""),
                        "source": "dns",
                        **extracted,
                        "specs": specs,
                    })
                    log.info("  [DNS] Успешно: %s", product["name"][:50])

                except Exception as e:
                    log.error("[DNS] Ошибка парсинга характеристик для %s: %s", product["name"][:30], e)

            except Exception as e:
                log.error("[DNS] Ошибка при обработке вкладки %s: %s", product.get("name", "")[:30], e)
            finally:
                if tab:
                    await _safe_close(tab)

    except Exception as e:
        log.error("[DNS/%s] Batch critical error: %s", category_name, e)
    finally:
        if context and owns_context:
            await _safe_close(context)
        await _close_camoufox(browser_cm, f"DNS/{category_name}/batch")

    return results


async def scrape_dns(url: str, category_name: str) -> list[dict[str, Any]]:
    log.info("[DNS] Начинаю парсинг категории '%s' -> %s", category_name, url)

    catalog_products = await _collect_catalog_dns(url, category_name)
    if not catalog_products:
        log.warning("[DNS/%s] Не найдено товаров в каталоге или сработал блок.", category_name)
        return []

    log.info("[DNS/%s] Фаза 2: сбор характеристик %d товаров...", category_name, len(catalog_products))

    final_results = []
    for i in range(0, len(catalog_products), BATCH_SIZE):
        batch = catalog_products[i: i + BATCH_SIZE]
        batch_res = await _process_batch_dns(batch, category_name)
        final_results.extend(batch_res)
        if i + BATCH_SIZE < len(catalog_products):
            await asyncio.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    log.info("[DNS/%s] Готово. Итого: %d товаров успешно обработано.", category_name, len(final_results))
    return final_results


async def scrape_dns_catalogs() -> dict[str, list[dict[str, Any]]]:
    all_data = {}
    for cat_name, cat_url in URLS_DNS.items():
        all_data[cat_name] = await scrape_dns(cat_url, cat_name)
        await asyncio.sleep(random.uniform(5.0, 12.0))
    return all_data