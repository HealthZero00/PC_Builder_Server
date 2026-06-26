"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

"""
matcher.py — алгоритм матчинга (сопоставления) товаров между Ситилинком и Регардом.
"""

import re
import difflib
import logging
from typing import Optional

log = logging.getLogger(__name__)

_STOP_WORDS = {
    "процессор", "видеокарта", "кулер", "сжо", "материнская", "плата",
    "оперативная", "память", "блок", "питания", "накопитель", "ssd",
    "корпус", "cooler", "для", "и", "с", "в"
}

FUZZY_THRESHOLD = 0.85  # Чуть снижен, так как убрана хаотичная сортировка токенов


def normalize(name: str) -> str:
    """ Приводит название к стандартному сжатому виду без мусора. """
    name = name.lower()
    name = re.sub(r"\(.*?\)", "", name)  # Удаляем всё в скобках
    name = re.sub(r"[^\w\s\-]", " ", name)  # Заменяем спецсимволы на пробелы

    # Распрямляем популярные модификации для точного совпадения слов
    name = name.replace("super", " super ").replace("ti", " ti ").replace("x3d", " x3d ")

    tokens = [t for t in name.split() if t not in _STOP_WORDS and len(t) > 1]
    # ВНИМАНИЕ: Сортировка .sort() убрана! Порядок слов важен для difflib.SequenceMatcher.
    return " ".join(tokens)


# Улучшенные и расширенные паттерны под все поколения комплектующих (включая Ryzen 7 и Ultra)
_ARTICLE_PATTERNS = [
    # Intel CPU: i3/i5/i7/i9-14900KS, а также Core Ultra 5/7/9 245K
    r"\b(i[3579]-\d{4,5}[a-z]{0,3})\b",
    r"\b(ultra\s*[3579]\s*\d{3,4}[a-z]{0,3})\b",

    # AMD CPU: Добавлен захват Ryzen 7 и архитектур X3D /G /X
    r"\b(ryzen\s*[3579]\s*\d{4,5}[a-z0-9]*)\b",

    # GPU: RTX 4070, RTX 4080 super, RX 7800 xt, Arc A770
    r"\b(rtx\s*\d{3,4}(?:\s*(?:super|ti))?)\b",
    r"\b(rx\s*\d{3,4}(?:\s*xt)?)\b",
    r"\b(arc\s+[ab]\d{3,4})\b",

    # Универсальный паттерн для кулеров, БП и памяти (сложные буквенно-цифровые группы с дефисами)
    # Ищет конструкции типа SE-224-XTS, contents-360, pq850g, b760m-ds3h
    r"\b([a-z0-9]{2,4}-_?[a-z0-9]{2,4}(?:[-_][a-z0-9]+)+)\b",
]


def extract_articles(name: str) -> set[str]:
    name_lower = name.lower()
    found: set[str] = set()
    for pattern in _ARTICLE_PATTERNS:
        for m in re.finditer(pattern, name_lower):
            # Нормализуем пробелы и дефисы внутри артикула для стандартизации сравнения
            article = re.sub(r"[\s\-_]+", "", m.group(1))
            found.add(article)
    return found


def find_match(
    regard_name: str,
    existing_names: list[str],
    threshold: float = FUZZY_THRESHOLD
) -> Optional[str]:
    """ Ищет совпадение regard_name среди существующих имен из БД Ситилинка """
    if not existing_names:
        return None

    rn_norm = normalize(regard_name)
    rn_articles = extract_articles(regard_name)

    # Быстрая проверка на критичные маркеры модификаций
    is_ti = "ti" in rn_norm.split()
    is_super = "super" in rn_norm.split()
    is_x3d = "x3d" in rn_norm.split()
    is_box = any(w in regard_name.lower() for w in ["box", "бокс"])
    is_oem = any(w in regard_name.lower() for w in ["oem", "оем"])

    best_name: Optional[str] = None
    best_score: float = 0.0

    for db_name in existing_names:
        db_norm = normalize(db_name)

        # ── Уровень 1: Точное совпадение ───────────────────────────────────
        if rn_norm == db_norm:
            # Но даже при точном совпадении строк проверяем BOX/OEM (на всякий случай)
            if is_box and any(w in db_name.lower() for w in ["oem", "оем"]): continue
            if is_oem and any(w in db_name.lower() for w in ["box", "бокс"]): continue
            log.debug("[matcher] Точное совпадение: '%s' == '%s'", regard_name, db_name)
            return db_name

        # Жесткий фильтр модификаций: если один Super/Ti/X3D, а второй нет — запрещаем матчинг
        db_words = db_norm.split()
        if is_ti != ("ti" in db_words): continue
        if is_super != ("super" in db_words): continue
        if is_x3d != ("x3d" in db_words): continue
        if is_box and any(w in db_name.lower() for w in ["oem", "оем"]): continue
        if is_oem and any(w in db_name.lower() for w in ["box", "бокс"]): continue

        # ── Уровень 2: Совпадение артикулов ─────────────────────────────────
        db_articles = extract_articles(db_name)
        if rn_articles and db_articles:
            common = rn_articles & db_articles
            significant = {a for a in common if len(a) >= 4}  # Снизили до 4 для коротких плат/памяти
            if significant:
                score = difflib.SequenceMatcher(None, rn_norm, db_norm).ratio()
                if score > 0.65 and score > best_score:
                    best_score = score
                    best_name = db_name
                    log.debug("[matcher] Артикул %s → '%s' (score=%.2f)", significant, db_name, score)
                    continue

        # ── Уровень 3: Нечёткое сравнение (Fuzzy) ───────────────────────────
        score = difflib.SequenceMatcher(None, rn_norm, db_norm).ratio()
        if score > best_score:
            best_score = score
            best_name = db_name

    if best_score >= threshold:
        log.info("[matcher] СОВПАДЕНИЕ (%.0f%%): '%s' ← '%s'", best_score * 100, best_name, regard_name)
        return best_name

    log.debug("[matcher] Нет совпадений для: '%s' (лучший score=%.2f)", regard_name, best_score)
    return None


def get_names_from_cache(cache: dict, category: str) -> list[str]:
    items = cache.get(category, [])
    return [item["name"] for item in items if item.get("name")]