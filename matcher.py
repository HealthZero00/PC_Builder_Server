"""
matcher.py — алгоритм матчинга (сопоставления) товаров между Ситилинком и Регардом.

ЗАЧЕМ ЭТО НУЖНО?
Один и тот же товар в двух магазинах называется немного по-разному:
  Citilink : "Кулер ID-COOLING SE-224-XTS BLACK"
  Regard   : "Кулер ID-COOLING SE-224-XTS BLACK"   ← иногда совпадает
  Citilink : "Процессор Intel Core i5-14400F OEM"
  Regard   : "Процессор Intel i5-14400F (Raptor Lake Refresh) LGA1700, OEM"

Если не сопоставлять — в БД будет два дублирующих товара с разными ценами.
Правильно: один товар в таблице `components`, а в `component_prices` — строки
для 'citilink' и 'regard' отдельно.

КАК РАБОТАЕТ МАТЧИНГ (три уровня):

Уровень 1 — ТОЧНОЕ СОВПАДЕНИЕ (самое быстрое):
  Нормализуем строки и сравниваем напрямую.

Уровень 2 — СОВПАДЕНИЕ ПО АРТИКУЛУ:
  Вытаскиваем из обоих названий модельные номера (регулярками).
  "i5-14400F", "RTX 4070", "SE-224-XTS" — если артикулы совпали, это один товар.

Уровень 3 — НЕЧЁТКОЕ СРАВНЕНИЕ (fuzzy):
  Используем difflib.SequenceMatcher. Если схожесть > 88%, считаем одним товаром.
"""

import re
import difflib
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  СТОП-СЛОВА — убираем из названий перед сравнением, чтобы не мешали
# ─────────────────────────────────────────────────────────────────────────────
_STOP_WORDS = {
    # Категории товаров
    "процессор", "видеокарта", "кулер", "сжо", "материнская", "плата",
    "оперативная", "память", "блок", "питания", "накопитель", "ssd",
    "корпус", "cooler",
    # Варианты исполнения
    "oem", "box", "rtail", "bulk",
    # Предлоги и общие слова
    "для", "и", "с",
}

# Порог нечёткого совпадения (0.0 — 1.0). 0.88 = 88% схожести.
# Поднимай если слишком много ложных совпадений, опускай если пропускает очевидные.
FUZZY_THRESHOLD = 0.88


def normalize(name: str) -> str:
    """
    Приводит название к «стандартному» виду для сравнения.
    Пример:
      "Процессор Intel Core i5-14400F, OEM" → "core i5-14400f intel"
      "Intel i5-14400F (Raptor Lake) OEM"   → "i5-14400f intel"
    """
    name = name.lower()

    # Удаляем скобки и их содержимое — "(Raptor Lake Refresh)", "(Box)"
    name = re.sub(r"\(.*?\)", "", name)

    # Удаляем все символы кроме букв, цифр, пробела и дефиса
    name = re.sub(r"[^\w\s\-]", " ", name)

    # Разбиваем на слова, убираем стоп-слова и сортируем
    tokens = [
        t for t in name.split()
        if t not in _STOP_WORDS and len(t) > 1
    ]
    tokens.sort()

    return " ".join(tokens)


# ─────────────────────────────────────────────────────────────────────────────
#  ИЗВЛЕЧЕНИЕ АРТИКУЛА (модельного номера)
#  Примеры артикулов:
#    Intel  : i3-12100F, i7-13700K, i9-14900KS
#    AMD    : Ryzen 5 5600X, Ryzen 9 7950X
#    NVIDIA : RTX 4070, RTX 3060 Ti, RX 7800 XT
#    Кулеры : SE-224-XTS, NH-D15, AK620
#    Память : DDR5-6000, CL36
#    SSD    : 970 EVO Plus, MZ-V8P1T0BW
# ─────────────────────────────────────────────────────────────────────────────

_ARTICLE_PATTERNS = [
    # Intel CPU: i3-12100F, i9-14900KS, Core Ultra 9 285K
    r"\b(i[3579]-\d{4,5}[a-z]{0,3})\b",
    r"\b(core\s+ultra\s+\d+\s+\d+[a-z]{0,3})\b",

    # AMD CPU: Ryzen 5 5600X, Ryzen 9 7950X3D
    r"\b(ryzen\s+[359]\s+\d{4}[a-z]{0,3})\b",

    # GPU: RTX 4070, RTX 4080 Super, RX 7800 XT
    r"\b(rtx\s+\d{3,4}(?:\s+(?:super|ti))?)\b",
    r"\b(rx\s+\d{3,4}(?:\s+xt)?)\b",
    r"\b(arc\s+[ab]\d{3,4})\b",

    # Кулеры и СЖО: SE-224-XTS, NH-D15, AIO-360
    r"\b([a-z]{1,4}[-_]\d{2,4}[-_]?[a-z0-9\-]*)\b",

    # Память: DDR5-6000, DDR4-3200
    r"\b(ddr[45][-\s]?\d{3,5})\b",

    # Любой буквенно-цифровой артикул длиной 5+: MZ-V8P1T0BW, LHR-D15S
    r"\b([a-z0-9]{2,}[-][a-z0-9]{2,}(?:[-][a-z0-9]+)*)\b",
]


def extract_articles(name: str) -> set[str]:
    """
    Вытаскивает все «артикулы» (модельные номера) из строки.
    Возвращает множество строк в нижнем регистре.
    """
    name_lower = name.lower()
    found: set[str] = set()
    for pattern in _ARTICLE_PATTERNS:
        for m in re.finditer(pattern, name_lower):
            article = re.sub(r"\s+", "", m.group(1))  # убираем пробелы внутри
            found.add(article)
    return found


# ─────────────────────────────────────────────────────────────────────────────
#  ГЛАВНАЯ ФУНКЦИЯ МАТЧИНГА
# ─────────────────────────────────────────────────────────────────────────────

def find_match(
    regard_name: str,
    existing_names: list[str],
    threshold: float = FUZZY_THRESHOLD
) -> Optional[str]:
    """
    Ищет в списке `existing_names` (уже есть в БД от Ситилинка) товар,
    совпадающий с `regard_name`.

    Возвращает:
        str  — оригинальное имя из базы, если нашли совпадение
        None — если не нашли (значит это новый товар)

    Параметры:
        regard_name    : название товара с Регарда
        existing_names : список имён, уже сохранённых в БД (от Ситилинка)
        threshold      : порог нечёткого совпадения (0.0–1.0)
    """
    if not existing_names:
        return None

    rn_norm     = normalize(regard_name)
    rn_articles = extract_articles(regard_name)

    best_name  : Optional[str] = None
    best_score : float = 0.0

    for db_name in existing_names:
        # ── Уровень 1: точное совпадение нормализованных строк ──────────────
        db_norm = normalize(db_name)
        if rn_norm == db_norm:
            log.debug("[matcher] Точное: '%s' == '%s'", regard_name, db_name)
            return db_name  # 100% совпадение — сразу возвращаем

        # ── Уровень 2: совпадение артикулов ─────────────────────────────────
        db_articles = extract_articles(db_name)
        if rn_articles and db_articles:
            # Достаточно пересечения хотя бы одного значимого артикула
            common = rn_articles & db_articles
            # Фильтруем слишком короткие совпадения (буквы типа "rx", "ti")
            significant = {a for a in common if len(a) >= 5}
            if significant:
                # Дополнительно проверяем нечётко, чтобы не смешать i5/i7
                score = difflib.SequenceMatcher(None, rn_norm, db_norm).ratio()
                if score > 0.70 and score > best_score:
                    best_score = score
                    best_name  = db_name
                    log.debug(
                        "[matcher] Артикул %s → '%s' (score=%.2f)",
                        significant, db_name, score
                    )
                    continue

        # ── Уровень 3: нечёткое сравнение ───────────────────────────────────
        score = difflib.SequenceMatcher(None, rn_norm, db_norm).ratio()
        if score > best_score:
            best_score = score
            best_name  = db_name

    if best_score >= threshold:
        log.info(
            "[matcher] СОВПАДЕНИЕ (%.0f%%): '%s' ← '%s'",
            best_score * 100, best_name, regard_name
        )
        return best_name

    log.debug("[matcher] Нет совпадений для: '%s' (лучший score=%.2f)", regard_name, best_score)
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  УТИЛИТА: получить список имён из кэша для одной категории
# ─────────────────────────────────────────────────────────────────────────────

def get_names_from_cache(cache: dict, category: str) -> list[str]:
    """
    Возвращает список имён товаров из кэша для нужной категории.
    Используется перед сохранением Регарда — проверяем уже ли есть от Ситилинка.

    Пример:
        names = get_names_from_cache(cache, "Процессоры")
        # → ["Intel Core i5-14400F OEM", "AMD Ryzen 5 5600X", ...]
    """
    items = cache.get(category, [])
    return [item["name"] for item in items if item.get("name")]
