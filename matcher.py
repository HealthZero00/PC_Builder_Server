"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

"""
matcher.py — алгоритм матчинга товаров между магазинами.
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

FUZZY_THRESHOLD = 0.85


def normalize(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"[^\w\s\-]", " ", name)
    tokens = [t for t in name.split() if t not in _STOP_WORDS and len(t) > 1]
    return " ".join(tokens)


_ARTICLE_PATTERNS = [
    r"\b(i[3579]-\d{4,5}[a-z]{0,3})\b",
    r"\b(ultra\s*[3579]\s*\d{3,4}[a-z]{0,3})\b",
    r"\b(ryzen\s*[3579]\s*\d{4,5}[a-z0-9]*)\b",
    r"\b(rtx\s*\d{3,4}(?:\s*(?:super|ti))?)\b",
    r"\b(rx\s*\d{3,4}(?:\s*xt)?)\b",
    r"\b(arc\s+[ab]\d{3,4})\b",
    r"\b([a-z0-9]{2,4}-_?[a-z0-9]{2,4}(?:[-_][a-z0-9]+)+)\b",
]


def extract_articles(name: str) -> set[str]:
    name_lower = name.lower()
    found: set[str] = set()
    for pattern in _ARTICLE_PATTERNS:
        for m in re.finditer(pattern, name_lower):
            article = re.sub(r"[\s\-_]+", "", m.group(1))
            found.add(article)
    return found


def _extract_gpu_model(text: str) -> Optional[str]:
    text = text.lower()

    patterns = [
        # RTX с суффиксами
        r"rtx\s*(\d{3,4})\s*(ti\s*super|super\s*ti|super|ti)",
        # RX с суффиксами
        r"rx\s*(\d{3,4})\s*(xtx|xt)",
        # RTX без суффикса
        r"rtx\s*(\d{3,4})(?!\s*(?:ti|super))",
        # RX без суффикса
        r"rx\s*(\d{3,4})(?!\s*(?:xt|xtx))",
        # Arc
        r"arc\s+([ab]\d{3,4})",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            groups = [g for g in m.groups() if g]
            if "rtx" in pattern:
                prefix = "rtx"
            elif "rx" in pattern:
                prefix = "rx"
            else:
                prefix = "arc"
            return prefix + "".join(g.strip() for g in groups).replace(" ", "")
    return None


def _extract_cpu_model(text: str) -> Optional[str]:
    text = text.lower()

    patterns = [
        # Intel Core Ultra
        r"ultra\s*([3579])\s*(\d{3,4}[a-z0-9]*)",
        # Intel i3/i5/i7/i9
        r"i([3579])[-\s](\d{4,5}[a-z0-9]*)",
        # AMD Ryzen (включая X3D, G, X и т.д.)
        r"ryzen\s*[3579]\s*(\d{4,5}[a-z0-9]*)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            groups = [g for g in m.groups() if g]
            return "".join(groups).replace(" ", "").replace("-", "")
    return None


def _gpu_models_compatible(model_a: str, model_b: str) -> bool:
    return model_a == model_b


def _cpu_models_compatible(model_a: str, model_b: str) -> bool:
    return model_a == model_b


def _box_oem_conflict(name_a: str, name_b: str) -> bool:
    a = name_a.lower()
    b = name_b.lower()
    a_box = any(w in a for w in ["box", "бокс"])
    a_oem = any(w in a for w in ["oem", "оем"])
    b_box = any(w in b for w in ["box", "бокс"])
    b_oem = any(w in b for w in ["oem", "оем"])
    if a_box and b_oem:
        return True
    if a_oem and b_box:
        return True
    return False


def find_match(
    regard_name: str,
    existing_names: list[str],
    threshold: float = FUZZY_THRESHOLD,
) -> Optional[str]:

    if not existing_names:
        return None

    rn_norm  = normalize(regard_name)
    rn_lower = regard_name.lower()

    rn_gpu_model = _extract_gpu_model(rn_lower)
    rn_cpu_model = _extract_cpu_model(rn_lower)

    if rn_cpu_model:
        for db_name in existing_names:
            if _box_oem_conflict(regard_name, db_name):
                continue
            db_cpu_model = _extract_cpu_model(db_name.lower())
            if db_cpu_model and _cpu_models_compatible(rn_cpu_model, db_cpu_model):
                log.info(
                    "[matcher] CPU точное совпадение: '%s' ← '%s'",
                    db_name, regard_name,
                )
                return db_name

    if rn_gpu_model:
        candidates = []
        for db_name in existing_names:
            if _box_oem_conflict(regard_name, db_name):
                continue
            db_gpu_model = _extract_gpu_model(db_name.lower())
            if db_gpu_model and _gpu_models_compatible(rn_gpu_model, db_gpu_model):
                candidates.append(db_name)

        if len(candidates) == 1:
            log.info(
                "[matcher] GPU точное совпадение: '%s' ← '%s'",
                candidates[0], regard_name,
            )
            return candidates[0]

        elif len(candidates) > 1:
            best_name, best_score = _fuzzy_best(rn_norm, candidates)
            if best_score >= threshold:
                log.info(
                    "[matcher] GPU fuzzy среди кандидатов (%.0f%%): '%s' ← '%s'",
                    best_score * 100, best_name, regard_name,
                )
                return best_name
            # Если fuzzy не уверен — не матчим, лучше создать новый
            log.debug(
                "[matcher] GPU: несколько кандидатов, fuzzy слабый (%.2f), пропускаем",
                best_score,
            )
            return None

    for db_name in existing_names:
        if _box_oem_conflict(regard_name, db_name):
            continue
        if rn_norm == normalize(db_name):
            log.debug("[matcher] Точное совпадение: '%s'", db_name)
            return db_name

    rn_articles = extract_articles(regard_name)
    best_name:  Optional[str] = None
    best_score: float         = 0.0

    for db_name in existing_names:
        if _box_oem_conflict(regard_name, db_name):
            continue

        if rn_gpu_model:
            db_gpu_model = _extract_gpu_model(db_name.lower())
            if db_gpu_model and not _gpu_models_compatible(rn_gpu_model, db_gpu_model):
                continue

        if rn_cpu_model:
            db_cpu_model = _extract_cpu_model(db_name.lower())
            if db_cpu_model and not _cpu_models_compatible(rn_cpu_model, db_cpu_model):
                continue

        db_norm     = normalize(db_name)
        db_articles = extract_articles(db_name)

        if rn_articles and db_articles:
            common      = rn_articles & db_articles
            significant = {a for a in common if len(a) >= 4}
            if significant:
                score = difflib.SequenceMatcher(None, rn_norm, db_norm).ratio()
                if score > 0.6 and score > best_score:
                    best_score = score
                    best_name  = db_name
                    continue

        score = difflib.SequenceMatcher(None, rn_norm, db_norm).ratio()
        if score > best_score:
            best_score = score
            best_name  = db_name

    if best_score >= threshold:
        log.info(
            "[matcher] СОВПАДЕНИЕ (%.0f%%): '%s' ← '%s'",
            best_score * 100, best_name, regard_name,
        )
        return best_name

    log.debug("[matcher] Нет совпадений для: '%s' (%.2f)", regard_name, best_score)
    return None


def _fuzzy_best(norm_query: str, candidates: list[str]) -> tuple[str, float]:
    best_name  = candidates[0]
    best_score = 0.0
    for name in candidates:
        score = difflib.SequenceMatcher(None, norm_query, normalize(name)).ratio()
        if score > best_score:
            best_score = score
            best_name  = name
    return best_name, best_score


def get_names_from_cache(cache: dict, category: str) -> list[str]:
    items = cache.get(category, [])
    return [item["name"] for item in items if item.get("name")]