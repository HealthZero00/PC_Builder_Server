"""
build_validator.py
Глубокий аудит совместимости сборки ПК.
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ
# ═══════════════════════════════════════════════════════════════════════════

SOCKET_CHIPSET_MAP: dict[str, list[str]] = {
    "AM5":     ["X870E", "X870", "X670E", "X670", "B850", "B650E", "B650", "A620"],
    "AM4":     ["X570", "B550", "A520", "X470", "B450", "A320", "X370", "B350", "A300"],
    "LGA1700": ["Z790", "Z690", "H770", "H670", "B760", "B660", "H610"],
    "LGA1851": ["Z890", "B860", "H810"],
    "LGA1200": ["Z590", "Z490", "H570", "H510", "B560", "B460"],
}

BIOS_FLASHBACK_REQUIRED: dict[str, list[str]] = {
    "AM5":     ["A620"],
    "AM4":     ["A320", "B350"],
    "LGA1700": ["H510", "B460"],
}

MB_SIZE_RANK: dict[str, int] = {
    "Mini-ITX": 0,
    "Flex-ATX": 1,
    "mATX":     2,
    "ATX":      3,
    "E-ATX":    4,
}

CPU_PIN_AMPERAGE: dict[str, int] = {
    "4 pin":   1,
    "4+4 pin": 2,
    "8 pin":   2,
    "8+4 pin": 3,
    "8+8 pin": 4,
}

PCIE_LANES_BY_SOCKET: dict[str, int] = {
    "AM5":     28,
    "AM4":     20,
    "LGA1700": 20,
    "LGA1851": 24,
}

M2_SLOTS_BY_CHIPSET: dict[str, int] = {
    "X870E": 4, "X870": 4, "X670E": 4, "X670": 3,
    "B850":  3, "B650E": 3, "B650": 2, "A620": 1,
    "X570":  3, "B550": 2,  "A520": 1,
    "Z890":  5, "Z790": 5,  "Z690": 4,
    "H770":  3, "H670": 2,  "B760": 2, "B660": 2, "H610": 1,
    "B860":  2, "H810": 1,
    "Z590":  3, "Z490": 3,  "H570": 2, "B560": 2, "B460": 1,
}

SATA_PORTS_BY_FF: dict[str, int] = {
    "Mini-ITX": 4,
    "mATX":     4,
    "ATX":      6,
    "E-ATX":    8,
}

RAM_SLOTS_BY_FF: dict[str, int] = {
    "Mini-ITX": 2,
    "mATX":     4,
    "ATX":      4,
    "E-ATX":    8,
}

SYSTEM_OVERHEAD_W  = 80
PSU_HEADROOM_PCT   = 0.20
PSU_SWEET_SPOT_MIN = 0.40
PSU_SWEET_SPOT_MAX = 0.80

LGA_NEED_MOUNTING_KIT = {"LGA1700", "LGA1851"}

INTEL_Z_CHIPSETS = {
    "Z890", "Z790", "Z690",
    "Z590", "Z490", "Z390", "Z370", "Z270", "Z170",
}

IF_THRESHOLD_AM4 = 3600
IF_THRESHOLD_AM5 = 6000

GPU_DUAL_CABLE_TDP_THRESHOLD = 250

IGPU_AMD_PATTERN   = r'ryzen.+\d{4,5}g\b'
IGPU_INTEL_EXCLUDE = r'\d{4,6}[kf]*f\b'

GPU_CLASS_BY_TDP: dict[int, str] = {
    50:  "low-end",
    100: "mid-low",
    150: "mid",
    200: "mid-high",
    250: "high-end",
    350: "flagship",
}
CPU_CLASS_BY_TDP: dict[int, str] = {
    35:  "low-end",
    65:  "mid",
    95:  "mid-high",
    125: "high-end",
    170: "flagship",
}

XMP_THRESHOLD_DDR4 = 2400
XMP_THRESHOLD_DDR5 = 4800

VRAM_MIN_1080P = 8
VRAM_MIN_4K    = 12

# Маркеры «GPU не требует доп. питания»
NO_POWER_MARKERS = {"без питания", "no power", "нет", "none", "---", ""}


# ═══════════════════════════════════════════════════════════════════════════
#  ТИПЫ РЕЗУЛЬТАТОВ
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    code:   str
    title:  str
    detail: str
    field:  str = ""


@dataclass
class ValidationResult:
    critical: list[Issue] = field(default_factory=list)
    warning:  list[Issue] = field(default_factory=list)
    advisory: list[Issue] = field(default_factory=list)
    summary:  dict        = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.critical:
            return "CRITICAL"
        if self.warning:
            return "WARNING"
        return "OK"

    def to_dict(self) -> dict:
        return {
            "status":   self.status,
            "critical": [asdict(i) for i in self.critical],
            "warning":  [asdict(i) for i in self.warning],
            "advisory": [asdict(i) for i in self.advisory],
            "summary":  self.summary,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════

def _g(component: dict | None, key: str, default=None):
    if not component:
        return default
    v = component.get(key, default)
    if v is None or v == "---" or v == "" or v == []:
        return default
    if isinstance(v, int) and v == 0 and default != 0:
        return default
    return v


def _int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _normalize_gpu_pin(pin_str: str) -> str:
    """
    Нормализует строку разъёма питания GPU к единому формату.

    Реальные строки из парсера Ситилинк:
        "2x(6+2) pin"                    → "8+8 pin"
        "1x(6+2) pin"                    → "8 pin"
        "питание видеокарты 2x(6+2) pin" → "8+8 pin"
        "2x8 pin"                        → "8+8 pin"
        "3x8 pin"                        → "8+8+8 pin"
        "8 pin"                          → "8 pin"
        "6+2 pin"                        → "8 pin"
        "8+8 pin"                        → "8+8 pin"
        "12VHPWR (16 pin)"               → "12vhpwr (16 pin)"
        "без питания"                    → "без питания"
    """
    if not pin_str:
        return ""

    s = pin_str.lower().strip()

    # Убираем мусорные слова
    for noise in (
        "питание видеокарты", "питание", "видеокарты",
        "рекомендовано", "разъём", "разъем", "коннектор", "connector",
    ):
        s = s.replace(noise, " ")
    s = re.sub(r'\s+', ' ', s).strip()

    # Маркеры "без питания" — возвращаем как есть
    if s in NO_POWER_MARKERS or not s:
        return s

    # 12VHPWR / 16-pin
    if "12vhpwr" in s or "16-pin" in s or re.search(r'16\s*pin', s):
        return "12vhpwr (16 pin)"

    # Формат "Nx(A+B)" → суммируем пины, разворачиваем по количеству
    # Пример: "2x(6+2)" → count=2, a=6, b=2 → total_per=8 → "8+8 pin"
    m = re.match(r'(\d+)\s*[x×*]\s*\((\d+)\+(\d+)\)', s)
    if m:
        count     = int(m.group(1))
        total_per = int(m.group(2)) + int(m.group(3))
        return "+".join([str(total_per)] * count) + " pin"

    # Формат "Nx(A)" → "A+A+... pin"
    m = re.match(r'(\d+)\s*[x×*]\s*\((\d+)\)', s)
    if m:
        count = int(m.group(1))
        a     = int(m.group(2))
        return "+".join([str(a)] * count) + " pin"

    # Формат "NxA" без скобок → "A+A+... pin"
    # Пример: "2x8" → "8+8 pin", "3x6" → "6+6+6 pin"
    m = re.match(r'(\d+)\s*[x×*]\s*(\d+)', s)
    if m:
        count = int(m.group(1))
        a     = int(m.group(2))
        return "+".join([str(a)] * count) + " pin"

    # Формат "A+B" где результат — сумма (пр: "6+2" = 8-pin разъём)
    # Но только если это ОДИН разъём (нет умножителя)
    m = re.match(r'^(\d+)\+(\d+)$', s.replace(" pin", "").replace("pin", "").strip())
    if m:
        total = int(m.group(1)) + int(m.group(2))
        return f"{total} pin"

    # Просто число
    m = re.match(r'^(\d+)$', s.replace(" pin", "").replace("pin", "").strip())
    if m:
        return f"{m.group(1)} pin"

    # Убираем слово "pin" для финального возврата
    result = s.replace("pin", "").strip()
    return result if result else s


def _gpu_pin_units(pin_str: str) -> int:
    """
    Возвращает суммарное количество «единиц мощности» разъёма GPU.
    Сначала нормализует строку, потом суммирует все пины.
    Единица ≈ 75 Вт (один 6-pin эквивалент).

    Примеры:
        "8 pin"      → 1 (≈75 Вт от разъёма, но это 8-pin)
        "8+8 pin"    → 2
        "8+8+8 pin"  → 3
        "12vhpwr"    → 8 (600 Вт)
    """
    if not pin_str:
        return 0

    normalized = _normalize_gpu_pin(pin_str)
    low = normalized.lower()

    if not low or low in NO_POWER_MARKERS:
        return 0

    if "12vhpwr" in low:
        return 8

    # Суммируем все числа в строке
    # "8+8 pin" → [8, 8] → sum=16 → units = 16//8 = 2
    numbers = re.findall(r'\d+', low.replace("pin", ""))
    if numbers:
        total_pins = sum(int(n) for n in numbers)
        # Каждые 8 пинов = 1 единица (8-pin = ~150 Вт, 6-pin = ~75 Вт)
        # Используем 6 как делитель для гибкости (6-pin и 8-pin оба считаются)
        return max(1, total_pins // 6)

    return 0


def _common_prefix_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    common = 0
    for ca, cb in zip(a, b):
        if ca == cb:
            common += 1
        else:
            break
    return common / max(len(a), len(b))


def _detect_amd_gen(cpu_name: str) -> int:
    m = re.search(r"ryzen\s*\d\s+(\d)(\d{3})", cpu_name, re.I)
    if m:
        return int(m.group(1))
    return 0


def _get_mb_ram_slots(mb: dict | None) -> int:
    if not mb:
        return 0
    slots = _int(_g(mb, "ramSlots"), 0)
    if slots > 0:
        return slots
    ff = _g(mb, "formFactor", "")
    return RAM_SLOTS_BY_FF.get(ff, 4)


def _get_mb_m2_slots(mb: dict | None) -> int:
    if not mb:
        return 0
    slots = _int(_g(mb, "m2Slots"), 0)
    if slots > 0:
        return slots
    chipset = _g(mb, "chipset", "").upper()
    if chipset in M2_SLOTS_BY_CHIPSET:
        return M2_SLOTS_BY_CHIPSET[chipset]
    ff = _g(mb, "formFactor", "")
    return {"Mini-ITX": 1, "mATX": 2, "ATX": 2, "E-ATX": 3}.get(ff, 1)


def _get_mb_sata_ports(mb: dict | None) -> int:
    if not mb:
        return 0
    ports = _int(_g(mb, "sataPorts"), 0)
    if ports > 0:
        return ports
    ff = _g(mb, "formFactor", "")
    return SATA_PORTS_BY_FF.get(ff, 4)


# ═══════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ КЛАСС ВАЛИДАТОРА
# ═══════════════════════════════════════════════════════════════════════════

class BuildValidator:

    def __init__(self, components: dict[str, Any]):
        self.cpu    = components.get("cpu")
        self.mb     = components.get("mb")
        self.psu    = components.get("psu")
        self.case   = components.get("case")
        self.cooler = components.get("cooler")

        raw_gpu = components.get("gpu")
        if isinstance(raw_gpu, list):
            self.gpus = [g for g in raw_gpu if g]
        elif raw_gpu:
            self.gpus = [raw_gpu]
        else:
            self.gpus = []
        self.gpu = self.gpus[0] if self.gpus else None

        raw_ram = components.get("ram")
        if isinstance(raw_ram, list):
            self.ram_sticks = [r for r in raw_ram if r]
        elif raw_ram:
            self.ram_sticks = [raw_ram]
        else:
            self.ram_sticks = []

        raw_ssd = components.get("ssd")
        if isinstance(raw_ssd, list):
            self.ssds = [s for s in raw_ssd if s]
        elif raw_ssd:
            self.ssds = [raw_ssd]
        else:
            self.ssds = []
        self.ssd = self.ssds[0] if self.ssds else None

        self.result = ValidationResult()

    # ─────────────────────────────────────────────────────────────────────
    #  ЗАПУСК ВСЕХ ПРОВЕРОК
    # ─────────────────────────────────────────────────────────────────────

    def validate(self) -> ValidationResult:
        self.check_socket_compatibility()
        self.check_ram_type()
        self.check_ram_slots()
        self.check_ram_cooler_clearance()
        self.check_power_deep()
        self.check_pcie_lanes()
        self.check_gpu_physical()
        self.check_cooler_vs_cpu()
        self.check_cooler_vs_case()
        self.check_case_form_factor()
        self.check_psu_form_factor()
        self.check_bios_flashback()
        self.check_ssd_slot_availability()

        self.check_igpu()
        self.check_ram_total_capacity()
        self.check_xmp_expo()
        self.check_bottleneck()
        self.check_wifi()
        self.check_nvme_heatsink()
        self.check_sata_ssd_bay()
        self.check_psu_efficiency_zone()
        self.check_vram_adequacy()

        self.check_aio_radiator_vs_case()
        self.check_aio_radiator_vs_ram()
        self.check_cooler_mounting_kit()
        self.check_intel_pcb_bend()
        self.check_gpu_dual_cable()
        self.check_cpu_cable_length_tower()
        self.check_intel_k_chipset()
        self.check_infinity_fabric()
        self.check_usb_c_front_panel()
        self.check_argb_rgb_headers()
        self.check_ram_population_order()
        self.check_ecc_ram_compatibility()

        self.check_ram_mixing()
        self.check_multi_gpu()
        self.check_multi_ssd()

        self._build_summary()
        return self.result

    # ═══════════════════════════════════════════════════════════════════════
    #  1. СОКЕТ
    # ═══════════════════════════════════════════════════════════════════════

    def check_socket_compatibility(self):
        cpu_socket    = _g(self.cpu,    "socket")
        mb_socket     = _g(self.mb,     "socket")
        cooler_socket = _g(self.cooler, "socket")

        if cpu_socket and mb_socket and cpu_socket != mb_socket:
            self.result.critical.append(Issue(
                code="SOCKET_MISMATCH",
                title="Несовместимые сокеты CPU и материнской платы",
                detail=(
                    f"Процессор имеет сокет {cpu_socket}, "
                    f"а материнская плата — {mb_socket}. "
                    f"Физически несовместимы. Замените один из компонентов."
                ),
                field="cpu/mb"
            ))

        if cpu_socket and cooler_socket and cooler_socket != "Universal":
            supported = [s.strip().upper() for s in cooler_socket.split(",")]
            if cpu_socket not in supported:
                self.result.critical.append(Issue(
                    code="COOLER_SOCKET_MISMATCH",
                    title="Кулер не подходит к процессору",
                    detail=(
                        f"Кулер поддерживает сокеты {cooler_socket}, "
                        f"но процессор использует {cpu_socket}. "
                        f"Потребуется крепёж или другой кулер."
                    ),
                    field="cooler"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  2. ТИП ОЗУ
    # ═══════════════════════════════════════════════════════════════════════

    def check_ram_type(self):
        mb_ddr = _g(self.mb, "ramType")
        if not mb_ddr:
            return

        for i, stick in enumerate(self.ram_sticks):
            stick_ddr = _g(stick, "ramType")
            if stick_ddr and stick_ddr != mb_ddr:
                self.result.critical.append(Issue(
                    code="RAM_TYPE_MISMATCH",
                    title=f"Тип памяти модуля #{i+1} не совместим с платой",
                    detail=(
                        f"Материнская плата поддерживает {mb_ddr}, "
                        f"а модуль #{i+1} — {stick_ddr}. "
                        f"Физически несовместимо."
                    ),
                    field="ram"
                ))

        cpu_ddr = _g(self.cpu, "ramType")
        if cpu_ddr and mb_ddr and cpu_ddr != mb_ddr:
            self.result.critical.append(Issue(
                code="CPU_RAM_TYPE_MISMATCH",
                title="Тип памяти CPU не соответствует плате",
                detail=(
                    f"Процессор нативно поддерживает {cpu_ddr}, "
                    f"плата рассчитана под {mb_ddr}."
                ),
                field="cpu/mb"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  3. СЛОТЫ ОЗУ
    # ═══════════════════════════════════════════════════════════════════════

    def check_ram_slots(self):
        if not self.ram_sticks:
            return

        mb_slots = _get_mb_ram_slots(self.mb)
        n        = len(self.ram_sticks)

        if mb_slots > 0 and n > mb_slots:
            self.result.critical.append(Issue(
                code="RAM_SLOTS_OVERFLOW",
                title=f"Не хватает слотов ОЗУ: выбрано {n}, на плате {mb_slots}",
                detail=(
                    f"Вы добавили {n} модулей памяти, "
                    f"но материнская плата имеет только {mb_slots} слота. "
                    f"Физически невозможно установить все планки. "
                    f"Уберите лишние или выберите плату с большим числом слотов."
                ),
                field="mb/ram"
            ))

        if n == 1 and mb_slots >= 2:
            self.result.advisory.append(Issue(
                code="SINGLE_CHANNEL",
                title="Включён одноканальный режим памяти",
                detail=(
                    "Один модуль ОЗУ даёт одноканальный режим. "
                    "Производительность на 10–30% ниже двухканального. "
                    "Рекомендуем добавить второй идентичный модуль."
                ),
                field="ram"
            ))
        elif n == 3 and mb_slots == 4:
            self.result.advisory.append(Issue(
                code="RAM_ODD_COUNT",
                title="3 планки ОЗУ в 4-слотовой плате — нестандартная конфигурация",
                detail=(
                    "Три модуля нарушают симметрию двухканального режима. "
                    "Одна планка будет работать в одноканальном режиме. "
                    "Оптимально: 2 или 4 планки."
                ),
                field="ram"
            ))

        mb_max_freq = _int(_g(self.mb, "ramMaxFreq"), 0)
        for i, stick in enumerate(self.ram_sticks):
            stick_freq = _int(_g(stick, "ramMaxFreq"), 0)
            if stick_freq and mb_max_freq and stick_freq > mb_max_freq:
                self.result.warning.append(Issue(
                    code="RAM_FREQ_THROTTLE",
                    title=f"Модуль #{i+1} будет понижен по частоте",
                    detail=(
                        f"Модуль поддерживает {stick_freq} МГц, "
                        f"но плата ограничена {mb_max_freq} МГц."
                    ),
                    field="ram"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  4. ФИЗИЧЕСКИЙ КОНФЛИКТ RAM / КУЛЕР
    # ═══════════════════════════════════════════════════════════════════════

    def check_ram_cooler_clearance(self):
        if not self.cooler or not self.ram_sticks:
            return

        for i, stick in enumerate(self.ram_sticks):
            ram_h = _int(_g(stick, "ramHeight"), 0)
            if not ram_h:
                continue
            if ram_h > 50:
                self.result.critical.append(Issue(
                    code="RAM_COOLER_HEIGHT_CRITICAL",
                    title=f"Планка ОЗУ #{i+1} физически не поместится рядом с кулером",
                    detail=(
                        f"Высота модуля {ram_h} мм — конфликт "
                        f"с любым башенным кулером. Выберите память ≤ 33 мм."
                    ),
                    field="ram/cooler"
                ))
            elif ram_h > 35:
                self.result.warning.append(Issue(
                    code="RAM_COOLER_HEIGHT",
                    title=f"Высокий профиль ОЗУ (модуль #{i+1}) может конфликтовать с кулером",
                    detail=(
                        f"Высота модуля {ram_h} мм. "
                        f"Башенные кулеры перекрывают первый слот при высоте ОЗУ > 35 мм."
                    ),
                    field="ram/cooler"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  5. ЭНЕРГЕТИЧЕСКИЙ АУДИТ — ИСПРАВЛЕН (нормализация разъёмов GPU)
    # ═══════════════════════════════════════════════════════════════════════

    def check_power_deep(self):
        cpu_tdp = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp = sum(_int(_g(g, "gpuTdp"), 0) for g in self.gpus)
        psu_w   = _int(_g(self.psu, "psuWattage"), 0)
        gpu_req = max((_int(_g(g, "gpuReqPsu"), 0) for g in self.gpus), default=0)

        total_tdp = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        rec_psu   = int(total_tdp * (1 + PSU_HEADROOM_PCT))

        # ── Нет БП ────────────────────────────────────────────────────────
        if not psu_w and self.gpus:
            req = gpu_req or (gpu_tdp + 150)
            self.result.warning.append(Issue(
                code="NO_PSU_SELECTED",
                title="Блок питания не выбран",
                detail=(
                    f"В сборке есть видеокарта (TDP {gpu_tdp} Вт, "
                    f"рекомендовано >= {req} Вт). Добавьте БП."
                ),
                field="psu"
            ))

        # ── Мощность ──────────────────────────────────────────────────────
        if psu_w and total_tdp:
            if psu_w < total_tdp:
                self.result.critical.append(Issue(
                    code="PSU_UNDERPOWERED",
                    title="Блок питания не обеспечивает нужную мощность",
                    detail=(
                        f"Расчётное потребление: {total_tdp} Вт "
                        f"(CPU {cpu_tdp} W + GPU {gpu_tdp} W + "
                        f"система {SYSTEM_OVERHEAD_W} W). "
                        f"БП: {psu_w} Вт. Рекомендуется БП ≥ {rec_psu} Вт."
                    ),
                    field="psu"
                ))
            elif psu_w < rec_psu:
                self.result.warning.append(Issue(
                    code="PSU_LOW_HEADROOM",
                    title="Малый запас мощности БП",
                    detail=(
                        f"БП {psu_w} Вт, потребление {total_tdp} Вт. "
                        f"Запас < 20%. Рекомендуется {rec_psu} Вт."
                    ),
                    field="psu"
                ))

        if psu_w and gpu_req and psu_w < gpu_req:
            self.result.critical.append(Issue(
                code="PSU_BELOW_GPU_REQUIREMENT",
                title="БП ниже рекомендации производителя видеокарты",
                detail=(
                    f"Производитель GPU требует минимум {gpu_req} Вт, "
                    f"выбранный БП: {psu_w} Вт."
                ),
                field="psu"
            ))

        # ── Питание CPU (EPS) ──────────────────────────────────────────────
        mb_cpu_pin  = _g(self.mb,  "cpuPowerPin", "---")
        psu_cpu_pin = _g(self.psu, "cpuPowerPin", "---")
        mb_amps     = CPU_PIN_AMPERAGE.get(mb_cpu_pin, 0)
        psu_amps    = CPU_PIN_AMPERAGE.get(psu_cpu_pin, 0)

        if mb_amps and psu_amps:
            if psu_amps < mb_amps:
                self.result.critical.append(Issue(
                    code="CPU_POWER_PIN_CRITICAL",
                    title="БП не имеет нужного разъёма питания CPU",
                    detail=(
                        f"Плата требует {mb_cpu_pin}, "
                        f"БП предоставляет только {psu_cpu_pin}. "
                        f"Запуск невозможен."
                    ),
                    field="psu/mb"
                ))
            elif psu_amps == mb_amps and mb_amps >= 3:
                self.result.advisory.append(Issue(
                    code="CPU_POWER_PIN_OC_LIMIT",
                    title="Разъём питания CPU ограничивает разгон",
                    detail=(
                        f"Плата имеет {mb_cpu_pin}, БП подаёт {psu_cpu_pin}. "
                        f"Разгон через второй разъём может быть ограничен."
                    ),
                    field="psu"
                ))

        # ── Питание GPU — ИСПРАВЛЕНО: нормализация форматов ──────────────
        for gpu in self.gpus:
            gpu_pin     = _g(gpu,      "gpuPowerPin", "") or ""
            psu_gpu_pin = _g(self.psu, "gpuPowerPin", "") or ""

            # Нормализуем строки (2x(6+2) → 8+8 pin и т.д.)
            gpu_pin_norm = _normalize_gpu_pin(gpu_pin)
            psu_pin_norm = _normalize_gpu_pin(psu_gpu_pin)

            gpu_pin_low = gpu_pin_norm.lower().strip()
            psu_pin_low = psu_pin_norm.lower().strip()

            # GPU требует доп. питание?
            gpu_needs_power = (
                gpu_pin.lower().strip() not in NO_POWER_MARKERS
                and gpu_pin_low not in NO_POWER_MARKERS
                and bool(gpu_pin.strip())
            )

            # У БП есть разъёмы для GPU?
            psu_has_gpu_pin = (
                psu_gpu_pin.lower().strip() not in NO_POWER_MARKERS
                and psu_pin_low not in NO_POWER_MARKERS
                and _gpu_pin_units(psu_pin_norm) > 0
            )

            log.debug(
                "GPU pin: '%s' → '%s' | PSU pin: '%s' → '%s' | "
                "needs=%s has=%s",
                gpu_pin, gpu_pin_norm,
                psu_gpu_pin, psu_pin_norm,
                gpu_needs_power, psu_has_gpu_pin
            )

            if gpu_needs_power:
                if "12vhpwr" in gpu_pin_low:
                    # 12VHPWR — специальный случай
                    if not psu_has_gpu_pin or "12vhpwr" not in psu_pin_low:
                        self.result.warning.append(Issue(
                            code="GPU_12VHPWR_ADAPTER",
                            title="Видеокарта требует 12VHPWR, БП не имеет нативного разъёма",
                            detail=(
                                "GPU использует разъём 12VHPWR (16 pin). "
                                "Потребуется переходник 4×8-pin→16-pin. "
                                "Используйте только сертифицированные кабели."
                            ),
                            field="gpu/psu"
                        ))
                    else:
                        self.result.advisory.append(Issue(
                            code="GPU_12VHPWR_REMINDER",
                            title="12VHPWR: соблюдайте правила укладки кабеля",
                            detail=(
                                "Не сгибайте кабель под углом > 90° "
                                "ближе 35 мм от разъёма (риск оплавления на RTX 40xx)."
                            ),
                            field="gpu"
                        ))
                else:
                    if not psu_has_gpu_pin:
                        # БП вообще не имеет разъёмов GPU
                        self.result.critical.append(Issue(
                            code="GPU_POWER_MISSING",
                            title="БП не имеет разъёмов питания для GPU",
                            detail=(
                                f"Видеокарте нужен {gpu_pin}, "
                                f"но у выбранного БП нет разъёмов питания GPU "
                                f"(указано: '{psu_gpu_pin}'). "
                                f"Замените БП на модель с PCIe 6-pin / 8-pin кабелями."
                            ),
                            field="psu"
                        ))
                    else:
                        # Проверяем достаточность мощности разъёмов
                        gpu_units = _gpu_pin_units(gpu_pin_norm)
                        psu_units = _gpu_pin_units(psu_pin_norm)

                        if gpu_units > 0 and psu_units > 0 and psu_units < gpu_units:
                            self.result.critical.append(Issue(
                                code="GPU_POWER_PIN_INSUFFICIENT",
                                title="БП не имеет достаточного числа разъёмов для GPU",
                                detail=(
                                    f"GPU требует {gpu_pin} "
                                    f"(≈{gpu_units * 75} Вт через разъёмы), "
                                    f"БП предоставляет {psu_gpu_pin} "
                                    f"(≈{psu_units * 75} Вт). "
                                    f"Выберите БП с нужными PCIe кабелями."
                                ),
                                field="gpu/psu"
                            ))
                        # Если units совпадают или psu_units > gpu_units — всё ок
            else:
                # GPU питается от слота PCIe (≤75 Вт, без доп. разъёма)
                gpu_tdp_single = _int(_g(gpu, "gpuTdp"), 0)
                if gpu_tdp_single > 0:
                    self.result.advisory.append(Issue(
                        code="GPU_SLOT_POWERED",
                        title=f"GPU питается от слота PCIe (TDP {gpu_tdp_single} Вт)",
                        detail=(
                            "Видеокарта получает питание через слот PCIe x16 (лимит 75 Вт). "
                            "Дополнительные кабели питания не требуются."
                        ),
                        field="gpu"
                    ))

    # ═══════════════════════════════════════════════════════════════════════
    #  6. ЛИНИИ PCIe
    # ═══════════════════════════════════════════════════════════════════════

    def check_pcie_lanes(self):
        cpu_socket = _g(self.cpu, "socket")
        if not cpu_socket:
            return

        total_lanes   = PCIE_LANES_BY_SOCKET.get(cpu_socket, 20)
        used_lanes    = 0
        issues_detail = []

        for gpu in self.gpus:
            gpu_pci = _g(gpu, "gpuPciVersion", "4.0")
            used_lanes += 16
            issues_detail.append(f"GPU: x16 (PCIe {gpu_pci})")

        for ssd in self.ssds:
            if _g(ssd, "ssdInterface", "") == "NVMe":
                used_lanes += 4
                issues_detail.append("NVMe SSD: x4")

        mb_m2_slots = _get_mb_m2_slots(self.mb)
        extra_nvme  = max(0, mb_m2_slots - len(self.ssds))
        if extra_nvme > 0:
            used_lanes += extra_nvme * 4
            issues_detail.append(f"Потенциальных доп. NVMe M.2: {extra_nvme} × x4")

        if used_lanes > total_lanes:
            lost = used_lanes - total_lanes
            self.result.warning.append(Issue(
                code="PCIE_LANES_EXCEEDED",
                title="Расход линий PCIe превышает возможности процессора",
                detail=(
                    f"CPU ({cpu_socket}) предоставляет {total_lanes} линий PCIe. "
                    f"Конфигурация требует ≈ {used_lanes} линий "
                    f"({', '.join(issues_detail)}). "
                    f"Недостаёт ~{lost} линий — GPU переключится на x8 "
                    f"или NVMe потеряет скорость."
                ),
                field="mb/cpu"
            ))
        elif used_lanes > total_lanes * 0.9:
            self.result.advisory.append(Issue(
                code="PCIE_LANES_NEAR_LIMIT",
                title="Линии PCIe заняты почти полностью",
                detail=(
                    f"Используется {used_lanes} из {total_lanes} линий PCIe. "
                    f"При добавлении карт расширения скорость может снизиться."
                ),
                field="mb"
            ))

        if self.gpu and self.mb:
            gpu_pci_ver = _g(self.gpu, "gpuPciVersion", "")
            mb_pci_ver  = _g(self.mb,  "pciVersion", "")
            try:
                gv = float(gpu_pci_ver)
                mv = float(mb_pci_ver)
                if gv > mv:
                    loss_pct = 0 if mv >= 4.0 else (5 if gv - mv <= 1 else 15)
                    self.result.advisory.append(Issue(
                        code="PCIE_VERSION_DOWNGRADE",
                        title=f"GPU PCIe {gv} работает в слоте PCIe {mv}",
                        detail=(
                            f"Карта работоспособна (обратная совместимость). "
                            f"{'Потери ~' + str(loss_pct) + '%' if loss_pct else 'Без заметных потерь'}."
                        ),
                        field="gpu/mb"
                    ))
            except (ValueError, TypeError):
                pass

    # ═══════════════════════════════════════════════════════════════════════
    #  7. ФИЗИЧЕСКИЕ ГАБАРИТЫ GPU
    # ═══════════════════════════════════════════════════════════════════════

    def check_gpu_physical(self):
        if not self.gpus or not self.case:
            return

        max_gpu_len = _int(_g(self.case, "maxGpuLength"), 0)

        for gpu in self.gpus:
            gpu_len   = _int(_g(gpu, "gpuLength"), 0)
            gpu_slots = _int(_g(gpu, "gpuSlots"), 0)
            gpu_name  = _g(gpu, "name", "GPU")

            if gpu_len and max_gpu_len:
                if gpu_len > max_gpu_len:
                    self.result.critical.append(Issue(
                        code="GPU_TOO_LONG",
                        title=f"Видеокарта '{gpu_name}' не помещается в корпус по длине",
                        detail=(
                            f"GPU: {gpu_len} мм, максимум: {max_gpu_len} мм. "
                            f"Разница: {gpu_len - max_gpu_len} мм. "
                            f"Выберите компактную версию или другой корпус."
                        ),
                        field="gpu/case"
                    ))
                elif gpu_len > max_gpu_len * 0.92:
                    self.result.warning.append(Issue(
                        code="GPU_TIGHT_FIT",
                        title=f"Видеокарта '{gpu_name}' почти не умещается в корпус",
                        detail=(
                            f"GPU: {gpu_len} мм, допустимо: {max_gpu_len} мм. "
                            f"Запас {max_gpu_len - gpu_len} мм. "
                            f"Используйте кабели с угловым коннектором."
                        ),
                        field="gpu/case"
                    ))

            if gpu_slots == 3:
                self.result.advisory.append(Issue(
                    code="GPU_TRIPLE_SLOT",
                    title=f"Трёхслотовая GPU '{gpu_name}' — проверьте свободные слоты",
                    detail=(
                        "GPU занимает 3 слота расширения. "
                        "Убедитесь в наличии 3 смежных свободных заглушек в корпусе."
                    ),
                    field="gpu/case"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  8. КУЛЕР vs CPU (TDP)
    # ═══════════════════════════════════════════════════════════════════════

    def check_cooler_vs_cpu(self):
        cpu_tdp    = _int(_g(self.cpu,    "tdp"), 0)
        cooler_tdp = _int(_g(self.cooler, "maxTdp"), 0)

        if not cpu_tdp or not cooler_tdp:
            return

        if cooler_tdp < cpu_tdp:
            self.result.critical.append(Issue(
                code="COOLER_TDP_INSUFFICIENT",
                title="Кулер не справится с тепловыделением процессора",
                detail=(
                    f"TDP процессора: {cpu_tdp} Вт, кулер рассчитан на {cooler_tdp} Вт. "
                    f"Система будет троттлить. "
                    f"Выберите кулер с maxTDP ≥ {int(cpu_tdp * 1.15)} Вт."
                ),
                field="cooler"
            ))
        elif cooler_tdp < cpu_tdp * 1.15:
            self.result.advisory.append(Issue(
                code="COOLER_TDP_MARGINAL",
                title="Кулер работает почти на пределе TDP",
                detail=(
                    f"Кулер {cooler_tdp} Вт, CPU {cpu_tdp} Вт. "
                    f"Запас < 15%. Обеспечьте хорошую вентиляцию."
                ),
                field="cooler"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  9. КУЛЕР vs КОРПУС (высота)
    # ═══════════════════════════════════════════════════════════════════════

    def check_cooler_vs_case(self):
        cooler_h = _int(_g(self.cooler, "coolerHeight"), 0)
        case_max = _int(_g(self.case,   "maxCpuCoolerHeight"), 0)

        if not cooler_h or not case_max:
            return

        if cooler_h > case_max:
            self.result.critical.append(Issue(
                code="COOLER_HEIGHT_OVERFLOW",
                title="Кулер не помещается в корпус",
                detail=(
                    f"Высота кулера: {cooler_h} мм, "
                    f"максимум: {case_max} мм. "
                    f"Разница: {cooler_h - case_max} мм."
                ),
                field="cooler/case"
            ))
        elif cooler_h > case_max - 5:
            self.result.warning.append(Issue(
                code="COOLER_HEIGHT_TIGHT",
                title="Кулер в притык по высоте",
                detail=(
                    f"Кулер {cooler_h} мм, лимит {case_max} мм. "
                    f"Запас {case_max - cooler_h} мм."
                ),
                field="cooler/case"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  10. ФОРМ-ФАКТОР КОРПУСА vs MB
    # ═══════════════════════════════════════════════════════════════════════

    def check_case_form_factor(self):
        mb_ff     = _g(self.mb,   "formFactor")
        case_ff   = _g(self.case, "formFactor")
        supported = _g(self.case, "supportedMbFormats", [])

        if not mb_ff or not case_ff:
            return

        if supported:
            if mb_ff not in supported:
                self.result.critical.append(Issue(
                    code="MB_FORM_CASE_MISMATCH",
                    title="Материнская плата не подходит к корпусу",
                    detail=(
                        f"Корпус поддерживает: {', '.join(supported)}. "
                        f"Выбрана плата формата {mb_ff}."
                    ),
                    field="mb/case"
                ))
            return

        mb_rank   = MB_SIZE_RANK.get(mb_ff, -1)
        case_rank = MB_SIZE_RANK.get(case_ff, -1)

        if mb_rank > case_rank and mb_rank != -1 and case_rank != -1:
            self.result.critical.append(Issue(
                code="MB_TOO_LARGE_FOR_CASE",
                title="Материнская плата слишком большая для корпуса",
                detail=f"Плата {mb_ff} не вместится в корпус {case_ff}.",
                field="mb/case"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  11. ФОРМ-ФАКТОР БП vs КОРПУС
    # ═══════════════════════════════════════════════════════════════════════

    def check_psu_form_factor(self):
        psu_ff  = _g(self.psu,  "psuFormFactor")
        case_ff = _g(self.case, "formFactor")

        if not psu_ff or not case_ff:
            return

        psu_len      = _int(_g(self.psu,  "psuLength"), 0)
        case_psu_max = _int(_g(self.case, "maxPsuLength"), 0)

        if case_ff == "Mini-ITX" and psu_ff == "ATX":
            self.result.critical.append(Issue(
                code="PSU_FF_MISMATCH",
                title="ATX блок питания не подходит к Mini-ITX корпусу",
                detail=(
                    "Корпус Mini-ITX требует БП формата SFX или SFX-L. "
                    "ATX БП физически не вставить."
                ),
                field="psu/case"
            ))

        if psu_len and case_psu_max and psu_len > case_psu_max:
            self.result.critical.append(Issue(
                code="PSU_TOO_LONG",
                title="Блок питания слишком длинный для корпуса",
                detail=f"Длина БП: {psu_len} мм, максимум: {case_psu_max} мм.",
                field="psu/case"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  12. BIOS FLASHBACK
    # ═══════════════════════════════════════════════════════════════════════

    def check_bios_flashback(self):
        if not self.cpu or not self.mb:
            return

        cpu_socket = _g(self.cpu, "socket", "")
        chipset    = _g(self.mb,  "chipset", "").upper()

        mb_name = (self.mb.get("name") or "").upper()
        if not chipset:
            for s, chips in SOCKET_CHIPSET_MAP.items():
                for chip in chips:
                    if chip in mb_name:
                        chipset = chip
                        break

        if not chipset or not cpu_socket:
            return

        risky = BIOS_FLASHBACK_REQUIRED.get(cpu_socket, [])
        if chipset in risky:
            self.result.warning.append(Issue(
                code="BIOS_FLASHBACK_NEEDED",
                title="Возможно требуется обновление BIOS перед установкой CPU",
                detail=(
                    f"Плата на чипсете {chipset} может быть выпущена "
                    f"до появления вашего процессора. "
                    f"Проверьте список совместимости и используйте BIOS Flashback."
                ),
                field="mb"
            ))

        if cpu_socket == "AM4" and chipset in ("A320", "B350"):
            cpu_gen = _detect_amd_gen(self.cpu.get("name", ""))
            if cpu_gen == 5:
                self.result.critical.append(Issue(
                    code="BIOS_AM4_GEN5_UNSUPPORTED",
                    title="Плата на A320/B350 не поддерживает Ryzen 5000",
                    detail=(
                        f"Большинство плат на чипсете {chipset} не получили "
                        f"поддержку Ryzen 5000. Рекомендуется B550 или X570."
                    ),
                    field="mb"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  13. SSD vs СЛОТЫ M.2
    # ═══════════════════════════════════════════════════════════════════════

    def check_ssd_slot_availability(self):
        if not self.ssds or not self.mb:
            return

        mb_m2_cnt   = _get_mb_m2_slots(self.mb)
        mb_m2_types = _g(self.mb, "m2Types", [])

        for ssd in self.ssds:
            ssd_iface = _g(ssd, "ssdInterface", "")

            if ssd_iface == "NVMe" and mb_m2_cnt == 0:
                self.result.critical.append(Issue(
                    code="NO_M2_SLOT",
                    title="Плата не имеет слотов M.2 для NVMe SSD",
                    detail=(
                        "Выбранный SSD — NVMe (M.2), но плата не поддерживает M.2 слоты. "
                        "Используйте SATA SSD или выберите другую плату."
                    ),
                    field="ssd/mb"
                ))

            if ssd_iface == "NVMe" and mb_m2_types and "NVMe" not in mb_m2_types:
                self.result.critical.append(Issue(
                    code="M2_NVME_UNSUPPORTED",
                    title="Слот M.2 на плате не поддерживает NVMe",
                    detail=(
                        "Плата имеет M.2 слот только для SATA SSD. "
                        "NVMe SSD в нём не заработает."
                    ),
                    field="ssd/mb"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  14. ИНТЕГРИРОВАННАЯ ГРАФИКА
    # ═══════════════════════════════════════════════════════════════════════

    def check_igpu(self):
        if self.gpus:
            return
        if not self.cpu:
            return

        cpu_name = (self.cpu.get("name") or "").lower()
        has_igpu = False

        if any(x in cpu_name for x in ("intel", "core i", "core ultra", "pentium", "celeron")):
            if not re.search(IGPU_INTEL_EXCLUDE, cpu_name, re.I):
                has_igpu = True
        elif any(x in cpu_name for x in ("ryzen", "amd")):
            if re.search(IGPU_AMD_PATTERN, cpu_name, re.I):
                has_igpu = True

        if not has_igpu:
            self.result.critical.append(Issue(
                code="NO_GPU_NO_IGPU",
                title="Система не выдаст изображение — нет GPU и нет iGPU в CPU",
                detail=(
                    "В сборке нет дискретной видеокарты, а выбранный CPU "
                    "не имеет встроенной графики. "
                    "Добавьте дискретную GPU или замените CPU на модель с iGPU."
                ),
                field="gpu/cpu"
            ))
        else:
            self.result.advisory.append(Issue(
                code="IGPU_ONLY_MODE",
                title="Работа на встроенной графике CPU — производительность ограничена",
                detail=(
                    "Дискретная видеокарта не выбрана. "
                    "Система будет использовать iGPU. "
                    "Убедитесь, что в BIOS включён видеовыход (iGPU / Auto)."
                ),
                field="cpu"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  15. ОБЩИЙ ОБЪЁМ ОЗУ
    # ═══════════════════════════════════════════════════════════════════════

    def check_ram_total_capacity(self):
        if not self.ram_sticks or not self.mb:
            return

        total_gb = sum(_int(_g(s, "ramCapacity"), 0) for s in self.ram_sticks)
        if not total_gb:
            return

        mb_ff    = _g(self.mb, "formFactor", "")
        ram_type = _g(self.mb, "ramType", "")

        default_max = {
            ("Mini-ITX", "DDR5"): 96,
            ("Mini-ITX", "DDR4"): 64,
            ("mATX",     "DDR5"): 192,
            ("mATX",     "DDR4"): 128,
            ("ATX",      "DDR5"): 256,
            ("ATX",      "DDR4"): 128,
            ("E-ATX",    "DDR5"): 256,
            ("E-ATX",    "DDR4"): 128,
        }
        mb_max_gb = default_max.get((mb_ff, ram_type), 128)

        stick_gb = _int(_g(self.ram_sticks[0], "ramCapacity"), 0)
        n        = len(self.ram_sticks)

        if total_gb > mb_max_gb:
            self.result.critical.append(Issue(
                code="RAM_CAPACITY_OVERFLOW",
                title=f"Суммарный объём ОЗУ {total_gb} ГБ превышает лимит платы {mb_max_gb} ГБ",
                detail=(
                    f"Установлено {n} планок"
                    f"{' × ' + str(stick_gb) + ' ГБ' if stick_gb else ''} = {total_gb} ГБ. "
                    f"Плата формата {mb_ff} ({ram_type}) "
                    f"поддерживает максимум {mb_max_gb} ГБ."
                ),
                field="ram/mb"
            ))

        if total_gb >= 64:
            self.result.advisory.append(Issue(
                code="RAM_LARGE_CAPACITY",
                title=f"Установлено {total_gb} ГБ ОЗУ",
                detail=(
                    "Windows 10/11 Home поддерживает до 128 ГБ, Pro — до 2 ТБ. "
                    "Убедитесь, что все планки видны в BIOS."
                ),
                field="ram"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  16. XMP / EXPO
    # ═══════════════════════════════════════════════════════════════════════

    def check_xmp_expo(self):
        if not self.ram_sticks:
            return

        for stick in self.ram_sticks:
            ram_type  = _g(stick, "ramType", "")
            freq      = _int(_g(stick, "ramMaxFreq"), 0)
            threshold = XMP_THRESHOLD_DDR5 if ram_type == "DDR5" else XMP_THRESHOLD_DDR4

            if freq and freq > threshold:
                profile = "EXPO" if ram_type == "DDR5" else "XMP"
                self.result.advisory.append(Issue(
                    code="XMP_EXPO_REQUIRED",
                    title=f"ОЗУ {freq} МГц: включите {profile} в BIOS для достижения заявленной скорости",
                    detail=(
                        f"Без активации {profile} память будет работать "
                        f"на базовой частоте JEDEC ({threshold} МГц). "
                        f"BIOS → Memory / OC → {profile} Profile 1."
                    ),
                    field="ram"
                ))
                break

    # ═══════════════════════════════════════════════════════════════════════
    #  17. БУТЫЛОЧНОЕ ГОРЛЫШКО
    # ═══════════════════════════════════════════════════════════════════════

    def check_bottleneck(self):
        if not self.cpu or not self.gpus:
            return

        cpu_tdp = _int(_g(self.cpu,     "tdp"),    0)
        gpu_tdp = _int(_g(self.gpus[0], "gpuTdp"), 0)

        if not cpu_tdp or not gpu_tdp:
            return

        def _classify(tdp: int, table: dict) -> str:
            for threshold in sorted(table):
                if tdp <= threshold:
                    return table[threshold]
            return "flagship"

        cpu_class = _classify(cpu_tdp, CPU_CLASS_BY_TDP)
        gpu_class = _classify(gpu_tdp, GPU_CLASS_BY_TDP)
        classes   = ["low-end", "mid-low", "mid", "mid-high", "high-end", "flagship"]
        ci   = classes.index(cpu_class)
        gi   = classes.index(gpu_class)
        diff = gi - ci

        if diff >= 3:
            self.result.warning.append(Issue(
                code="BOTTLENECK_CPU",
                title="Процессор может стать узким местом для видеокарты",
                detail=(
                    f"CPU класса «{cpu_class}» (TDP {cpu_tdp} Вт) "
                    f"и GPU класса «{gpu_class}» (TDP {gpu_tdp} Вт) — значительный дисбаланс. "
                    f"В процессорозависимых играх CPU будет ограничивать FPS."
                ),
                field="cpu/gpu"
            ))
        elif diff <= -3:
            self.result.advisory.append(Issue(
                code="BOTTLENECK_GPU",
                title="Видеокарта слабее процессора — возможен дисбаланс",
                detail=(
                    f"CPU класса «{cpu_class}» значительно мощнее GPU класса «{gpu_class}». "
                    f"FPS будет ограничен GPU. Рассмотрите GPU помощнее."
                ),
                field="cpu/gpu"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  18. WiFi / Bluetooth
    # ═══════════════════════════════════════════════════════════════════════

    def check_wifi(self):
        if not self.mb:
            return

        mb_name = (self.mb.get("name") or "").lower()
        specs   = self.mb.get("specs") or {}

        has_wifi = (
            "wi-fi" in mb_name or "wifi" in mb_name or
            " ax" in mb_name or "bluetooth" in mb_name or
            any(
                "wi-fi" in str(v).lower() or "bluetooth" in str(v).lower()
                for v in specs.values()
            )
        )

        if not has_wifi:
            self.result.advisory.append(Issue(
                code="NO_WIFI_ON_MB",
                title="Материнская плата без Wi-Fi / Bluetooth",
                detail=(
                    "Если нужен Wi-Fi — докупите PCIe Wi-Fi карту или USB адаптер. "
                    "Платы с Wi-Fi обычно имеют суффикс 'Wi-Fi' или 'AX' в названии."
                ),
                field="mb"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  19. NVMe Gen4 / Gen5 — НАГРЕВ SSD
    # ═══════════════════════════════════════════════════════════════════════

    def check_nvme_heatsink(self):
        if not self.ssds or not self.mb:
            return

        for ssd in self.ssds:
            ssd_iface = _g(ssd, "ssdInterface", "")
            if ssd_iface != "NVMe":
                continue

            ssd_name = (ssd.get("name") or "").lower()
            mb_specs = self.mb.get("specs") or {}

            is_gen4_or_gen5 = any(x in ssd_name for x in (
                "gen4", "gen 4", "gen5", "gen 5", "pcie 4", "pcie 5", "nvme 2"
            ))

            mb_has_heatsink = (
                "heatsink" in (self.mb.get("name") or "").lower() or
                "m.2 shield" in (self.mb.get("name") or "").lower() or
                any(
                    "радиатор" in str(v).lower() or "heatsink" in str(v).lower()
                    for v in mb_specs.values()
                )
            )

            if is_gen4_or_gen5:
                if mb_has_heatsink:
                    self.result.advisory.append(Issue(
                        code="NVME_GEN4_HEATSINK_OK",
                        title="NVMe Gen4/5: радиатор M.2 на плате есть — хорошо",
                        detail=(
                            "SSD Gen4/Gen5 греется до 80–90°C. "
                            "Встроенный радиатор поможет удержать температуру."
                        ),
                        field="ssd"
                    ))
                else:
                    self.result.warning.append(Issue(
                        code="NVME_GEN4_NO_HEATSINK",
                        title="NVMe Gen4/Gen5 SSD перегреется без радиатора",
                        detail=(
                            "Без охлаждения контроллер уйдёт в троттлинг — "
                            "скорость падает в 2–5 раз. "
                            "Используйте плату с M.2 Heatsink или отдельный радиатор."
                        ),
                        field="ssd/mb"
                    ))
            else:
                self.result.advisory.append(Issue(
                    code="NVME_GEN3_SLOT_CHECK",
                    title="NVMe SSD: проверьте, что слот M.2 поддерживает PCIe",
                    detail=(
                        "Некоторые платы имеют M.2 слоты только для SATA. "
                        "Слот должен быть помечен как 'PCIe + SATA' или 'NVMe'."
                    ),
                    field="ssd/mb"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  20. SATA SSD 2.5" В Mini-ITX КОРПУСЕ
    # ═══════════════════════════════════════════════════════════════════════

    def check_sata_ssd_bay(self):
        if not self.ssds or not self.case:
            return

        case_ff = _g(self.case, "formFactor", "")

        for ssd in self.ssds:
            ssd_ff     = _g(ssd, "ssdFormFactor", "")
            ssd_iface  = _g(ssd, "ssdInterface", "")
            is_sata_25 = (ssd_ff == '2.5"' or ssd_iface == "SATA")

            if is_sata_25 and case_ff == "Mini-ITX":
                self.result.warning.append(Issue(
                    code="SATA_SSD_NO_BAY_MINIITX",
                    title="SATA SSD 2.5\" может не поместиться в Mini-ITX корпус",
                    detail=(
                        "Многие Mini-ITX корпуса не имеют отсеков для 2.5\" накопителей. "
                        "Проверьте наличие 2.5\" Bay в спецификации корпуса. "
                        "Альтернатива — M.2 NVMe SSD."
                    ),
                    field="ssd/case"
                ))
                break

    # ═══════════════════════════════════════════════════════════════════════
    #  21. КПД БП: ЗОНА ЭФФЕКТИВНОСТИ
    # ═══════════════════════════════════════════════════════════════════════

    def check_psu_efficiency_zone(self):
        psu_w   = _int(_g(self.psu, "psuWattage"), 0)
        cpu_tdp = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp = sum(_int(_g(g, "gpuTdp"), 0) for g in self.gpus)

        if not psu_w or not (cpu_tdp or gpu_tdp):
            return

        total_w  = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        load_pct = total_w / psu_w

        if load_pct < PSU_SWEET_SPOT_MIN:
            self.result.advisory.append(Issue(
                code="PSU_OVERSIZED",
                title=f"БП избыточен: реальная нагрузка ≈ {int(load_pct * 100)}% от номинала",
                detail=(
                    f"При потреблении {total_w} Вт и БП {psu_w} Вт "
                    f"нагрузка ≈ {int(load_pct * 100)}%. "
                    f"Оптимальная зона КПД — 40–80%."
                ),
                field="psu"
            ))
        elif load_pct > PSU_SWEET_SPOT_MAX:
            self.result.advisory.append(Issue(
                code="PSU_NEAR_MAX_LOAD",
                title=f"БП работает при нагрузке ≈ {int(load_pct * 100)}% — повышенный нагрев",
                detail=(
                    f"При нагрузке {int(load_pct * 100)}% БП шумит и греется сильнее. "
                    f"Рекомендуется БП ≈ {int(total_w / 0.65)} Вт."
                ),
                field="psu"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  22. ОБЪЁМ VRAM
    # ═══════════════════════════════════════════════════════════════════════

    def check_vram_adequacy(self):
        if not self.gpus:
            return

        for gpu in self.gpus:
            vram_gb = _int(_g(gpu, "vram"), 0)
            if not vram_gb:
                continue

            if vram_gb < VRAM_MIN_1080P:
                self.result.warning.append(Issue(
                    code="VRAM_LOW_1080P",
                    title=f"Объём VRAM ({vram_gb} ГБ) мал для современных игр",
                    detail=(
                        f"При 1080p рекомендуется минимум {VRAM_MIN_1080P} ГБ VRAM. "
                        f"Возможны подгрузки текстур в требовательных играх."
                    ),
                    field="gpu"
                ))
            elif vram_gb >= VRAM_MIN_4K:
                self.result.advisory.append(Issue(
                    code="VRAM_4K_READY",
                    title=f"VRAM {vram_gb} ГБ — видеокарта готова к 4K и AI-задачам",
                    detail=(
                        f"{vram_gb} ГБ VRAM достаточно для 4K-гейминга "
                        f"и локального запуска LLM (до 7B параметров)."
                    ),
                    field="gpu"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  23. AIO: РАЗМЕР РАДИАТОРА vs КОРПУС
    # ═══════════════════════════════════════════════════════════════════════

    def check_aio_radiator_vs_case(self):
        if not self.cooler or not self.case:
            return
        if _g(self.cooler, "coolerType", "") != "AIO":
            return

        rad_size  = _int(_g(self.cooler, "aioRadiatorSize"), 0)
        if not rad_size:
            return

        supported = _g(self.case, "maxRadiatorSizes", []) or []

        if supported and rad_size not in supported:
            self.result.critical.append(Issue(
                code="AIO_RADIATOR_NOT_SUPPORTED",
                title=f"Корпус не поддерживает радиатор СЖО {rad_size} мм",
                detail=(
                    f"СЖО имеет радиатор {rad_size} мм, "
                    f"корпус поддерживает только: {sorted(supported)} мм."
                ),
                field="cooler/case"
            ))
        elif rad_size == 360:
            self.result.advisory.append(Issue(
                code="AIO_360_VRM_CLEARANCE",
                title="СЖО 360 мм: проверьте совместимость с VRM-радиатором платы",
                detail=(
                    "Радиатор 360 мм при верхней установке может упереться в "
                    "VRM-радиатор материнской платы (актуально для ASUS ROG, MSI MEG)."
                ),
                field="cooler/mb"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  24. AIO СВЕРХУ + ВЫСОКАЯ ОЗУ
    # ═══════════════════════════════════════════════════════════════════════

    def check_aio_radiator_vs_ram(self):
        if not self.cooler or not self.ram_sticks:
            return
        if _g(self.cooler, "coolerType", "") != "AIO":
            return

        rad_size = _int(_g(self.cooler, "aioRadiatorSize"), 0)
        if rad_size < 240:
            return

        for i, stick in enumerate(self.ram_sticks):
            ram_h = _int(_g(stick, "ramHeight"), 0)
            if ram_h > 40:
                self.result.warning.append(Issue(
                    code="AIO_TOP_RAM_HEIGHT_CONFLICT",
                    title=f"СЖО {rad_size} мм сверху: ОЗУ {ram_h} мм может упереться в вентилятор",
                    detail=(
                        f"При установке радиатора {rad_size} мм на верхнюю панель "
                        f"вентиляторы нависают над DIMM-слотами. "
                        f"Модули {ram_h} мм > 40 мм могут касаться вентилятора. "
                        f"Решение: низкопрофильная ОЗУ ≤ 35 мм или фронтальный монтаж."
                    ),
                    field="cooler/ram"
                ))
                break

    # ═══════════════════════════════════════════════════════════════════════
    #  25. LGA1700 / LGA1851 — MOUNTING KIT
    # ═══════════════════════════════════════════════════════════════════════

    def check_cooler_mounting_kit(self):
        if not self.cooler or not self.cpu:
            return

        cpu_socket = _g(self.cpu, "socket", "")
        if cpu_socket not in LGA_NEED_MOUNTING_KIT:
            return

        cooler_sockets = (_g(self.cooler, "socket", "") or "").upper()

        if cpu_socket in cooler_sockets:
            return

        old_intel = {"LGA1200", "LGA1151", "LGA1150", "LGA1155", "LGA1156"}
        has_only_old = any(s in cooler_sockets for s in old_intel)

        if has_only_old or cooler_sockets == "---":
            self.result.advisory.append(Issue(
                code="COOLER_MOUNTING_KIT_NEEDED",
                title=f"Кулер может потребовать Mounting Kit для {cpu_socket}",
                detail=(
                    f"Кулеры для LGA1200 и старше не имеют нативного крепления под {cpu_socket}. "
                    f"Большинство производителей предоставляют бесплатный Upgrade Kit "
                    f"(Noctua, be quiet!, Thermalright, DeepCool)."
                ),
                field="cooler"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  26. ИЗГИБ ТЕКСТОЛИТА INTEL LGA1700
    # ═══════════════════════════════════════════════════════════════════════

    def check_intel_pcb_bend(self):
        if not self.cpu:
            return

        cpu_socket = _g(self.cpu, "socket", "")
        if cpu_socket != "LGA1700":
            return

        cpu_name   = (self.cpu.get("name") or "").lower()
        cpu_tdp    = _int(_g(self.cpu, "tdp"), 0)
        is_highend = cpu_tdp >= 125 or any(
            x in cpu_name for x in ("i9-", "i7-", "core i9", "core i7")
        )

        if is_highend:
            self.result.advisory.append(Issue(
                code="INTEL_LGA1700_PCB_BEND",
                title="LGA1700 i7/i9: рекомендуется Contact Frame против изгиба PCB",
                detail=(
                    "Стандартный механизм крепления LGA1700 прогибает крышку IHS. "
                    "Это приводит к неравномерному контакту с кулером (+5–15°C). "
                    "Решение: Thermalright LGA1700 Contact Frame (~500 руб.)."
                ),
                field="cpu"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  27. МОЩНЫЙ GPU — ДВА ОТДЕЛЬНЫХ КАБЕЛЯ
    # ═══════════════════════════════════════════════════════════════════════

    def check_gpu_dual_cable(self):
        if not self.gpus or not self.psu:
            return

        for gpu in self.gpus:
            gpu_tdp   = _int(_g(gpu, "gpuTdp"), 0)
            pin_count = _int(_g(gpu, "gpuPowerPinCount"), 0)
            gpu_pin   = _g(gpu, "gpuPowerPin", "")

            if gpu_tdp < GPU_DUAL_CABLE_TDP_THRESHOLD or pin_count < 2:
                continue
            if "12VHPWR" in (gpu_pin or ""):
                continue

            psu_cables = _int(_g(self.psu, "gpuCableCount"), 0)

            if psu_cables == 1 or psu_cables == 0:
                self.result.warning.append(Issue(
                    code="GPU_SINGLE_CABLE_SPLITTER_RISK",
                    title=f"GPU {gpu_tdp} Вт: не используйте один кабель с разветвителем",
                    detail=(
                        f"Видеокарта ({gpu_tdp} Вт, {gpu_pin}) требует {pin_count} разъёма. "
                        f"Один кабель-«поросячий хвост» перегружает провод. "
                        f"Используйте два отдельных PCIe кабеля от БП."
                    ),
                    field="gpu/psu"
                ))
            else:
                self.result.advisory.append(Issue(
                    code="GPU_DUAL_CABLE_REMINDER",
                    title=f"Мощный GPU ({gpu_tdp} Вт): каждый разъём — отдельным кабелем от БП",
                    detail=(
                        f"Для {gpu_pin} подключайте каждый разъём отдельным кабелем, "
                        f"а не Y-разветвителем."
                    ),
                    field="gpu/psu"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  28. FULL TOWER + КАБЕЛЬ ПИТАНИЯ CPU
    # ═══════════════════════════════════════════════════════════════════════

    def check_cpu_cable_length_tower(self):
        if not self.psu or not self.case:
            return

        case_name     = (self.case.get("name") or "").lower()
        case_ff       = _g(self.case, "formFactor", "")
        is_full_tower = "full" in case_name or case_ff == "Full Tower"
        if not is_full_tower:
            return

        cpu_pin = _g(self.psu, "cpuPowerPin", "")
        psu_mod = _g(self.psu, "psuModular", "---")

        if cpu_pin and cpu_pin != "---":
            self.result.advisory.append(Issue(
                code="FULL_TOWER_CPU_CABLE_TOO_SHORT",
                title="Full Tower: стандартный CPU-кабель БП может быть коротким",
                detail=(
                    f"В Full Tower при скрытой прокладке кабеля {cpu_pin} "
                    f"стандартного 55–65 см часто не хватает. "
                    f"Рекомендуется кабель ≥ 75–80 см. "
                    f"{'Модульный БП (' + psu_mod + ') позволяет докупить удлинённый кабель.' if psu_mod in ('Full', 'Semi') else 'Проверьте длину комплектного CPU-кабеля.'}"
                ),
                field="psu/case"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  29. INTEL K-CPU + НЕ Z-ЧИПСЕТ
    # ═══════════════════════════════════════════════════════════════════════

    def check_intel_k_chipset(self):
        if not self.cpu or not self.mb:
            return

        cpu_name = (self.cpu.get("name") or "").upper()
        mb_name  = (self.mb.get("name")  or "").upper()

        is_k_cpu = bool(re.search(r'\b\d{4,5}K[SF]?\b', cpu_name))
        if not is_k_cpu:
            return

        has_z_chipset = any(z in mb_name for z in INTEL_Z_CHIPSETS)
        if has_z_chipset:
            return

        cpu_tdp = _int(_g(self.cpu, "tdp"), 0)
        self.result.advisory.append(Issue(
            code="K_CPU_NO_Z_CHIPSET",
            title="Intel K-CPU + не-Z чипсет: разгон множителем заблокирован",
            detail=(
                f"CPU с индексом K (TDP {cpu_tdp} Вт) требует Z-чипсет для разгона. "
                f"На B/H чипсетах разгон через множитель недоступен. "
                f"Либо замените плату на Z-чипсет, либо возьмите CPU без K."
            ),
            field="cpu/mb"
        ))

    # ═══════════════════════════════════════════════════════════════════════
    #  30. AMD INFINITY FABRIC
    # ═══════════════════════════════════════════════════════════════════════

    def check_infinity_fabric(self):
        if not self.cpu or not self.ram_sticks:
            return

        cpu_socket = _g(self.cpu, "socket", "")
        if cpu_socket not in ("AM4", "AM5"):
            return

        for stick in self.ram_sticks:
            ram_freq = _int(_g(stick, "ramMaxFreq"), 0)
            if not ram_freq:
                continue

            if cpu_socket == "AM4" and ram_freq > IF_THRESHOLD_AM4:
                self.result.advisory.append(Issue(
                    code="AM4_IF_ASYNC_MODE",
                    title=f"AM4 + ОЗУ {ram_freq} МГц: Infinity Fabric в асинхронном режиме 1:2",
                    detail=(
                        f"AM4 синхронизирует FCLK 1:1 до {IF_THRESHOLD_AM4} МГц. "
                        f"При {ram_freq} МГц задержка памяти растёт. "
                        f"Оптимальная зона AM4: 3600–3800 МГц."
                    ),
                    field="ram/cpu"
                ))
                break

            elif cpu_socket == "AM5" and ram_freq > IF_THRESHOLD_AM5:
                self.result.advisory.append(Issue(
                    code="AM5_IF_ASYNC_MODE",
                    title=f"AM5 + ОЗУ {ram_freq} МГц: выход из зоны синхронной FCLK",
                    detail=(
                        f"Оптимальная точка AM5 — {IF_THRESHOLD_AM5} МГц. "
                        f"Свыше — асинхронный режим и возможная нестабильность."
                    ),
                    field="ram/cpu"
                ))
                break

    # ═══════════════════════════════════════════════════════════════════════
    #  31. USB TYPE-C ПЕРЕДНЯЯ ПАНЕЛЬ vs Type-E header
    # ═══════════════════════════════════════════════════════════════════════

    def check_usb_c_front_panel(self):
        if not self.case or not self.mb:
            return

        case_info = (
            str(self.case.get("specs") or {}) + " " +
            (self.case.get("name") or "")
        ).lower()
        has_front_usbc = any(x in case_info for x in (
            "type-c", "usb-c", "usb 3.2 gen 2", "usb4", "front type-c"
        ))
        if not has_front_usbc:
            return

        mb_info = (
            str(self.mb.get("specs") or {}) + " " +
            (self.mb.get("name") or "")
        ).lower()
        has_typee = any(x in mb_info for x in (
            "type-e", "type e", "usb 3.2 gen 2 type-e",
            "front usb-c header", "internal usb-c"
        ))

        if not has_typee:
            self.result.warning.append(Issue(
                code="USB_C_FRONT_HEADER_MISSING",
                title="USB-C на передней панели: проверьте наличие Type-E header на плате",
                detail=(
                    "Для USB-C на передней панели нужен разъём "
                    "USB 3.2 Gen 2 Type-E (19-pin) на плате. "
                    "Бюджетные B/H платы часто его не имеют."
                ),
                field="case/mb"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  32. ARGB (5V) vs RGB (12V)
    # ═══════════════════════════════════════════════════════════════════════

    def check_argb_rgb_headers(self):
        if not self.mb:
            return

        mb_info = (
            str(self.mb.get("specs") or {}) + " " +
            (self.mb.get("name") or "")
        ).lower()
        case_info = (
            str((self.case.get("specs") or {}) if self.case else {}) + " " +
            ((self.case.get("name") or "") if self.case else "")
        ).lower()

        mb_has_argb   = any(x in mb_info  for x in ("argb", "addressable", "5v d-rgb", "5v rgb"))
        mb_has_rgb12  = bool(re.search(r'12v\s*rgb|d_led\b|rgb_header', mb_info))
        case_has_argb = any(x in case_info for x in ("argb", "addressable", "5v"))
        case_has_rgb  = any(x in case_info for x in ("rgb", "подсветк"))

        if case_has_argb and not mb_has_argb:
            self.result.warning.append(Issue(
                code="ARGB_HEADER_INCOMPATIBLE",
                title="Подсветка ARGB (5V) корпуса: на плате может не быть нужного разъёма",
                detail=(
                    "Корпус использует ARGB (5V, 3-pin). "
                    "Если плата имеет только 12V RGB (4-pin) — подключать НЕЛЬЗЯ: "
                    "это сожжёт подсветку. "
                    "Нужен разъём 'ARGB' / '5V D-RGB' на плате."
                ),
                field="case/mb"
            ))
        elif case_has_rgb and mb_has_argb and not mb_has_rgb12:
            self.result.advisory.append(Issue(
                code="RGB12V_HEADER_MISSING",
                title="Корпус с 12V RGB: на плате может не быть 12V разъёма",
                detail=(
                    "Если вентиляторы корпуса работают на 12V RGB (4-pin), "
                    "а плата имеет только ARGB (5V) — прямое подключение недопустимо."
                ),
                field="case/mb"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  33. ПОРЯДОК УСТАНОВКИ ОЗУ В СЛОТЫ
    # ═══════════════════════════════════════════════════════════════════════

    def check_ram_population_order(self):
        if not self.ram_sticks or not self.mb:
            return

        mb_slots = _get_mb_ram_slots(self.mb)
        n_sticks = len(self.ram_sticks)

        if mb_slots == 4 and n_sticks == 2:
            self.result.advisory.append(Issue(
                code="RAM_SLOT_POPULATION_ORDER",
                title="2 планки ОЗУ в 4-слотовой плате: соблюдайте порядок установки",
                detail=(
                    "Для двухканального режима устанавливайте планки в A2+B2 "
                    "(обычно 2-й и 4-й слот от процессора, выделены цветом). "
                    "Установка в A1+A2 даст одноканальный режим (-10–30% производительности)."
                ),
                field="ram/mb"
            ))
        elif mb_slots == 2 and n_sticks == 1:
            self.result.advisory.append(Issue(
                code="RAM_SINGLE_STICK_TWO_SLOT",
                title="1 планка ОЗУ в 2-слотовой плате: установите в рекомендованный слот",
                detail=(
                    "Часть плат требует планку в слот DIMM_A2 или DIMM_B1 "
                    "(дальний от процессора) для первоначальной загрузки."
                ),
                field="ram/mb"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  34. ECC ПАМЯТЬ НА ПОТРЕБИТЕЛЬСКОЙ ПЛАТЕ
    # ═══════════════════════════════════════════════════════════════════════

    def check_ecc_ram_compatibility(self):
        if not self.ram_sticks or not self.mb:
            return

        for stick in self.ram_sticks:
            stick_name  = (stick.get("name")  or "").lower()
            stick_specs = str(stick.get("specs") or {}).lower()

            if "ecc" not in stick_name and "ecc" not in stick_specs:
                continue

            mb_name = (self.mb.get("name") or "").lower()
            workstation_markers = (
                "w790", "w680", "w680e", "trx50", "trx40", "sp3",
                "pro ws", "workstation", "ws x570", "creator"
            )
            is_workstation = any(m in mb_name for m in workstation_markers)

            if not is_workstation:
                self.result.warning.append(Issue(
                    code="ECC_RAM_ON_CONSUMER_MB",
                    title="ECC-память на потребительской плате: коррекция ошибок не активна",
                    detail=(
                        "ECC работает только с чипсетами серверного класса "
                        "(Intel W790, AMD TRX50, EPYC SP3). "
                        "На потребительских Z/B/H платах ECC-модуль работает "
                        "как обычная память без коррекции ошибок."
                    ),
                    field="ram/mb"
                ))
            break

    # ═══════════════════════════════════════════════════════════════════════
    #  35. СМЕШИВАНИЕ МОДУЛЕЙ ОЗУ
    # ═══════════════════════════════════════════════════════════════════════

    def check_ram_mixing(self):
        if len(self.ram_sticks) < 2:
            return

        types    = [_g(s, "ramType",    "") for s in self.ram_sticks]
        freqs    = [_int(_g(s, "ramMaxFreq"), 0) for s in self.ram_sticks]
        caps     = [_int(_g(s, "ramCapacity"), 0) for s in self.ram_sticks]
        timings  = [_g(s, "ramTimings", "") for s in self.ram_sticks]
        voltages = [_g(s, "ramVoltage", "") for s in self.ram_sticks]
        names    = [_g(s, "name", "") for s in self.ram_sticks]

        unique_types = set(t for t in types if t)
        if len(unique_types) > 1:
            self.result.critical.append(Issue(
                code="RAM_MIXED_DDR_TYPES",
                title="Смешаны модули разных поколений DDR",
                detail=(
                    f"Установлены модули: {', '.join(unique_types)}. "
                    f"Разные поколения DDR физически несовместимы."
                ),
                field="ram"
            ))
            return

        unique_freqs = set(f for f in freqs if f)
        if len(unique_freqs) > 1:
            min_freq = min(unique_freqs)
            max_freq = max(unique_freqs)
            self.result.warning.append(Issue(
                code="RAM_MIXED_FREQUENCIES",
                title="Модули ОЗУ имеют разные частоты",
                detail=(
                    f"Модули: {min_freq} МГц и {max_freq} МГц. "
                    f"Система снизит все до {min_freq} МГц. "
                    f"Рекомендуется использовать идентичные планки."
                ),
                field="ram"
            ))

        unique_timings = set(t for t in timings if t and t != "---")
        if len(unique_timings) > 1:
            self.result.advisory.append(Issue(
                code="RAM_MIXED_TIMINGS",
                title="Модули ОЗУ имеют разные тайминги",
                detail=(
                    f"Тайминги: {', '.join(unique_timings)}. "
                    f"BIOS выставит наиболее медленные. "
                    f"Рекомендуется Kit от одного производителя."
                ),
                field="ram"
            ))

        unique_caps = set(c for c in caps if c)
        if len(unique_caps) > 1:
            self.result.advisory.append(Issue(
                code="RAM_ASYMMETRIC_CAPACITY",
                title="Асимметричные модули ОЗУ (разный объём)",
                detail=(
                    f"Объёмы: {' + '.join(str(c) + ' ГБ' for c in caps)}. "
                    f"Часть памяти будет в одноканальном режиме (Flex Mode)."
                ),
                field="ram"
            ))

        unique_voltages = set(v for v in voltages if v and v != "---")
        if len(unique_voltages) > 1:
            self.result.warning.append(Issue(
                code="RAM_MIXED_VOLTAGE",
                title="Модули ОЗУ рассчитаны на разное напряжение",
                detail=(
                    f"Напряжения: {', '.join(unique_voltages)} В. "
                    f"BIOS выставит одно напряжение — возможен перегрев или нестабильность."
                ),
                field="ram"
            ))

        if len(names) >= 2 and all(names):
            name0 = (names[0] or "").lower()
            name1 = (names[1] or "").lower()
            if _common_prefix_ratio(name0, name1) < 0.7:
                self.result.advisory.append(Issue(
                    code="RAM_NOT_KIT",
                    title="Модули ОЗУ не являются комплектом (Kit) — возможна нестабильность",
                    detail=(
                        "Планки от разных моделей могут не пройти XMP/EXPO тест совместно. "
                        "Рекомендуется готовый двухканальный Kit (2×…)."
                    ),
                    field="ram"
                ))

    # ═══════════════════════════════════════════════════════════════════════
    #  36. НЕСКОЛЬКО GPU
    # ═══════════════════════════════════════════════════════════════════════

    def check_multi_gpu(self):
        if len(self.gpus) < 2:
            return

        mb_pcie_slots = _int(_g(self.mb, "pcieX16Slots"), 1)
        if len(self.gpus) > mb_pcie_slots:
            self.result.critical.append(Issue(
                code="MULTI_GPU_NO_SLOTS",
                title=f"Недостаточно слотов PCIe x16 для {len(self.gpus)} видеокарт",
                detail=(
                    f"Выбрано {len(self.gpus)} GPU, "
                    f"плата имеет {mb_pcie_slots} слот(а) PCIe x16."
                ),
                field="gpu/mb"
            ))

        cpu_socket   = _g(self.cpu, "socket", "")
        total_lanes  = PCIE_LANES_BY_SOCKET.get(cpu_socket, 20)
        needed_lanes = len(self.gpus) * 16

        if needed_lanes > total_lanes:
            self.result.warning.append(Issue(
                code="MULTI_GPU_LANES_SPLIT",
                title=f"При {len(self.gpus)} GPU каждая получит меньше x16 линий",
                detail=(
                    f"CPU ({cpu_socket}) даёт {total_lanes} линий PCIe. "
                    f"{len(self.gpus)} GPU × 16 = {needed_lanes} — слоты переключатся в x8/x4."
                ),
                field="cpu/mb/gpu"
            ))

        for gpu in self.gpus:
            gpu_name = (gpu.get("name") or "").lower()
            if any(x in gpu_name for x in (
                "rtx 40", "rtx40", "rtx 30", "rtx30",
                "rx 7",   "rx7",   "rx 6",   "rx6"
            )):
                self.result.advisory.append(Issue(
                    code="MULTI_GPU_NO_SLI_NVLINK",
                    title="Современные GPU не поддерживают SLI/NVLink/CrossFire",
                    detail=(
                        "NVIDIA убрала SLI начиная с RTX 30xx. "
                        "AMD CrossFire не поддерживается с RX 6xxx. "
                        "Несколько GPU работают независимо — для игр прироста FPS нет."
                    ),
                    field="gpu"
                ))
                break

        total_gpu_tdp = sum(_int(_g(g, "gpuTdp"), 0) for g in self.gpus)
        psu_w         = _int(_g(self.psu, "psuWattage"), 0)
        cpu_tdp       = _int(_g(self.cpu, "tdp"), 0)
        total_system  = cpu_tdp + total_gpu_tdp + SYSTEM_OVERHEAD_W
        rec_psu       = int(total_system * (1 + PSU_HEADROOM_PCT))

        if psu_w and psu_w < rec_psu:
            self.result.critical.append(Issue(
                code="MULTI_GPU_PSU_INSUFFICIENT",
                title=f"БП не хватает мощности для {len(self.gpus)} GPU",
                detail=(
                    f"CPU {cpu_tdp} Вт + GPU × {len(self.gpus)} = {total_gpu_tdp} Вт + "
                    f"система {SYSTEM_OVERHEAD_W} Вт = {total_system} Вт. "
                    f"Рекомендуется БП ≥ {rec_psu} Вт, выбрано {psu_w} Вт."
                ),
                field="psu"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  37. НЕСКОЛЬКО SSD
    # ═══════════════════════════════════════════════════════════════════════

    def check_multi_ssd(self):
        if not self.ssds:
            return

        mb_m2_slots   = _get_mb_m2_slots(self.mb)
        mb_sata_ports = _get_mb_sata_ports(self.mb)

        nvme_ssds = [s for s in self.ssds if _g(s, "ssdInterface", "") == "NVMe"]
        sata_ssds = [s for s in self.ssds
                     if _g(s, "ssdInterface", "") in ("SATA", "SATA III")]

        nvme_count = len(nvme_ssds)
        sata_count = len(sata_ssds)

        if nvme_count > 0 and self.mb:
            if nvme_count > mb_m2_slots:
                self.result.critical.append(Issue(
                    code="MULTI_SSD_NO_M2_SLOTS",
                    title=f"Недостаточно M.2 слотов: нужно {nvme_count}, на плате {mb_m2_slots}",
                    detail=(
                        f"Выбрано {nvme_count} NVMe SSD, "
                        f"плата имеет {mb_m2_slots} слот(а) M.2. "
                        f"Решения: плата с большим числом M.2, "
                        f"или PCIe → M.2 карта расширения."
                    ),
                    field="ssd/mb"
                ))
            elif nvme_count > 1:
                self.result.advisory.append(Issue(
                    code="MULTI_NVME_SATA_SHARING",
                    title=f"{nvme_count} NVMe SSD: возможен конфликт M.2 и SATA портов",
                    detail=(
                        "На многих платах при заполнении нескольких M.2 слотов "
                        "часть SATA портов автоматически отключается. "
                        "Проверьте таблицу 'M.2 and SATA Configuration' в мануале платы."
                    ),
                    field="ssd/mb"
                ))

        if sata_count > 0 and self.mb:
            if sata_count > mb_sata_ports:
                self.result.critical.append(Issue(
                    code="MULTI_SSD_NO_SATA_PORTS",
                    title=f"Недостаточно SATA портов: нужно {sata_count}, на плате {mb_sata_ports}",
                    detail=(
                        f"Выбрано {sata_count} SATA накопителя, "
                        f"плата имеет {mb_sata_ports} SATA порта. "
                        f"Решение: SATA-контроллер (PCIe карта) или замена на NVMe."
                    ),
                    field="ssd/mb"
                ))

        if len(self.ssds) > 1:
            total_gb = sum(_int(_g(s, "ssdCapacityGb"), 0) for s in self.ssds)
            total_tb = total_gb / 1024
            self.result.advisory.append(Issue(
                code="MULTI_SSD_INFO",
                title=(
                    f"Несколько накопителей: {len(self.ssds)} шт."
                    f"{', суммарно ' + str(round(total_tb, 1)) + ' ТБ' if total_tb > 0 else ''}"
                ),
                detail=(
                    f"В сборке {len(self.ssds)} накопителей "
                    f"({nvme_count} NVMe + {sata_count} SATA). "
                    f"Рекомендуется настроить резервное копирование (правило 3-2-1)."
                ),
                field="ssd"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  СВОДКА
    # ═══════════════════════════════════════════════════════════════════════

    def _build_summary(self):
        cpu_tdp      = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp      = sum(_int(_g(g, "gpuTdp"), 0) for g in self.gpus)
        psu_w        = _int(_g(self.psu, "psuWattage"), 0)
        total        = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        rec          = int(total * (1 + PSU_HEADROOM_PCT))
        vram_gb      = sum(_int(_g(g, "vram"), 0) for g in self.gpus)
        total_ram_gb = sum(_int(_g(s, "ramCapacity"), 0) for s in self.ram_sticks)
        load_pct     = round(total / psu_w * 100, 1) if psu_w else 0

        self.result.summary = {
            "cpuTdpW":         cpu_tdp,
            "gpuTdpW":         gpu_tdp,
            "systemOverheadW": SYSTEM_OVERHEAD_W,
            "totalEstimatedW": total,
            "recommendedPsuW": rec,
            "selectedPsuW":    psu_w,
            "psuHeadroomPct":  round((psu_w - total) / total * 100, 1) if total else 0,
            "ramSticksCount":  len(self.ram_sticks),
            "totalRamGb":      total_ram_gb,
            "gpuCount":        len(self.gpus),
            "ssdCount":        len(self.ssds),
            "vramGb":          vram_gb,
            "psuLoadPct":      load_pct,
            "hasDiscreteGpu":  bool(self.gpus),
            "criticalCount":   len(self.result.critical),
            "warningCount":    len(self.result.warning),
            "advisoryCount":   len(self.result.advisory),
        }


# ═══════════════════════════════════════════════════════════════════════════
#  ПУБЛИЧНЫЙ API
# ═══════════════════════════════════════════════════════════════════════════

def check_compatibility(components: dict) -> dict:
    try:
        validator = BuildValidator(components)
        result    = validator.validate()
        return result.to_dict()
    except Exception as e:
        log.error("Ошибка валидации: %s", e, exc_info=True)
        return {
            "status":   "ERROR",
            "critical": [],
            "warning":  [],
            "advisory": [{"code":   "VALIDATOR_ERROR",
                          "title":  "Ошибка валидатора",
                          "detail": str(e),
                          "field":  ""}],
            "summary":  {},
        }