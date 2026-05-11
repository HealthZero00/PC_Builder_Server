"""
build_validator.py
Глубокий аудит совместимости сборки ПК.

Принимает словарь вида:
    {
        "cpu":    { ...поля из parser_engine... },
        "mb":     { ... },
        "gpu":    { ... },
        "ram":    { ... },        # список или один объект
        "psu":    { ... },
        "case":   { ... },
        "cooler": { ... },
        "ssd":    { ... },        # опционально
    }

Возвращает:
    {
        "status": "OK" | "WARNING" | "CRITICAL",
        "critical": [ {...}, ... ],
        "warning":  [ {...}, ... ],
        "advisory": [ {...}, ... ],
        "summary":  { "totalTdp": int, "recommendedPsu": int, ... }
    }
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Any

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ — «База знаний» инженера ПК
# ═══════════════════════════════════════════════════════════════════════════

# Какой сокет появился в каком году / поколении AMD
SOCKET_CHIPSET_MAP: dict[str, list[str]] = {
    "AM5":    ["X870E", "X870", "X670E", "X670", "B850", "B650E", "B650", "A620"],
    "AM4":    ["X570", "B550", "A520", "X470", "B450", "A320", "X370", "B350", "A300"],
    "LGA1700":["Z790", "Z690", "H770", "H670", "B760", "B660", "H610"],
    "LGA1851":["Z890", "B860", "H810"],
    "LGA1200":["Z590", "Z490", "H570", "H510", "B560", "B460"],
}

# Чипсеты, требующие BIOS Flashback для новых CPU
BIOS_FLASHBACK_REQUIRED: dict[str, list[str]] = {
    "AM5":    ["A620"],
    "AM4":    ["A320", "B350"],
    "LGA1700":["H510", "B460"],
}

# Форм-факторы MB: иерархия размеров
MB_SIZE_RANK: dict[str, int] = {
    "Mini-ITX": 0,
    "Flex-ATX": 1,
    "mATX":     2,
    "ATX":      3,
    "E-ATX":    4,
}

# Корпус: какие форм-факторы он вмещает
CASE_COMPATIBLE_MB: dict[str, list[str]] = {
    "Mini-ITX": ["Mini-ITX"],
    "mATX":     ["Mini-ITX", "mATX"],
    "ATX":      ["Mini-ITX", "mATX", "ATX"],
    "E-ATX":    ["Mini-ITX", "mATX", "ATX", "E-ATX"],
}

# Нормализация разъёмов питания CPU
CPU_PIN_AMPERAGE: dict[str, int] = {
    "4 pin":   1,
    "4+4 pin": 2,
    "8 pin":   2,
    "8+4 pin": 3,
    "8+8 pin": 4,
}

# Линии PCIe у процессоров по сокету
PCIE_LANES_BY_SOCKET: dict[str, int] = {
    "AM5":     28,
    "AM4":     20,
    "LGA1700": 20,
    "LGA1851": 24,
}

# Тепловыделение «балласта» системы (ОЗУ, накопители, вентиляторы)
SYSTEM_OVERHEAD_W = 80

# Рекомендуемый запас по мощности БП (%)
PSU_HEADROOM_PCT = 0.20

# ── ДОПОЛНИТЕЛЬНЫЕ КОНСТАНТЫ (новые проверки 23-34) ─────────────────────────

# Сокеты Intel, для которых старые кулеры требуют Mounting Kit
LGA_NEED_MOUNTING_KIT = {"LGA1700", "LGA1851"}

# Intel Z-чипсеты (только они разрешают разгон K-CPU)
INTEL_Z_CHIPSETS = {
    "Z890", "Z790", "Z690",
    "Z590", "Z490", "Z390", "Z370", "Z270", "Z170",
}

# Infinity Fabric: оптимальный порог синхронной работы 1:1
IF_THRESHOLD_AM4 = 3600   # выше → плавный переход в режим 1:2
IF_THRESHOLD_AM5 = 6000   # выше → нестабильность без ручных тайментов

# TDP порог "мощного GPU" для предупреждения о двух кабелях
GPU_DUAL_CABLE_TDP_THRESHOLD = 250   # Вт


# ── НОВЫЕ КОНСТАНТЫ ────────────────────────────────────────────────────────

# CPU с интегрированной графикой:
# AMD — только G-серия (5600G, 7600G, 8600G...)
# Intel — всё кроме серии F (13400F, 14600KF...)
IGPU_AMD_PATTERN   = r'ryzen.+\d{4,5}g\b'
IGPU_INTEL_EXCLUDE = r'\d{4,6}[kf]*f\b'

# Грубая оценка «класса» компонента по TDP
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

# Нативные частоты JEDEC — выше требуют XMP/EXPO
XMP_THRESHOLD_DDR4 = 2400
XMP_THRESHOLD_DDR5 = 4800

# Зона максимальной эффективности БП (% от номинала)
PSU_SWEET_SPOT_MIN = 0.40   # < 40% — КПД падает
PSU_SWEET_SPOT_MAX = 0.80   # > 80% — нагрев, шум

# Минимальный VRAM по разрешению (ГБ)
VRAM_MIN_1080P = 8
VRAM_MIN_1440P = 10
VRAM_MIN_4K    = 12


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
    """Безопасный get с fallback для None / '---' / 0 / пустой строки."""
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


def _has(component: dict | None) -> bool:
    return bool(component)


# ═══════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ КЛАСС ВАЛИДАТОРА
# ═══════════════════════════════════════════════════════════════════════════

class BuildValidator:
    """
    Проверяет сборку ПК по нескольким группам критериев.
    Все публичные методы check_* добавляют Issue в self.result.
    """

    def __init__(self, components: dict[str, Any]):
        self.cpu    = components.get("cpu")
        self.mb     = components.get("mb")
        self.gpu    = components.get("gpu")
        self.psu    = components.get("psu")
        self.case   = components.get("case")
        self.cooler = components.get("cooler")
        self.ssd    = components.get("ssd")

        raw_ram = components.get("ram")
        if isinstance(raw_ram, list):
            self.ram_sticks = raw_ram
        elif raw_ram:
            self.ram_sticks = [raw_ram]
        else:
            self.ram_sticks = []

        self.result = ValidationResult()

    # ─────────────────────────────────────────────────────────────────────
    #  ЗАПУСК ВСЕХ ПРОВЕРОК
    # ─────────────────────────────────────────────────────────────────────

    def validate(self) -> ValidationResult:
        # ── Оригинальные проверки ─────────────────────────────────────────
        self.check_socket_compatibility()       # 1.  Сокет CPU/MB/Кулер
        self.check_ram_type()                   # 2.  Тип DDR
        self.check_ram_slots()                  # 3.  Кол-во слотов + частота
        self.check_ram_cooler_clearance()       # 4.  Высота ОЗУ vs кулер
        self.check_power_deep()                 # 5.  Энергоаудит
        self.check_pcie_lanes()                 # 6.  Линии PCIe
        self.check_gpu_physical()               # 7.  Габариты GPU
        self.check_cooler_vs_cpu()              # 8.  TDP кулера vs CPU
        self.check_cooler_vs_case()             # 9.  Высота кулера vs корпус
        self.check_case_form_factor()           # 10. Форм-фактор MB vs корпус
        self.check_psu_form_factor()            # 11. Форм-фактор БП vs корпус
        self.check_bios_flashback()             # 12. BIOS совместимость
        self.check_ssd_slot_availability()      # 13. M.2 слоты

        # ── Новые проверки ────────────────────────────────────────────────
        self.check_igpu()                       # 14. Нет GPU + нет iGPU = нет картинки
        self.check_ram_total_capacity()         # 15. Суммарный объём ОЗУ vs лимит платы
        self.check_xmp_expo()                   # 16. XMP/EXPO для быстрой памяти
        self.check_bottleneck()                 # 17. Бутылочное горлышко CPU/GPU
        self.check_wifi()                       # 18. Wi-Fi / Bluetooth на плате
        self.check_nvme_heatsink()              # 19. Радиатор для NVMe Gen4/Gen5
        self.check_sata_ssd_bay()               # 20. SATA 2.5" в Mini-ITX корпусе
        self.check_psu_efficiency_zone()        # 21. Зона КПД блока питания
        self.check_vram_adequacy()              # 22. Объём VRAM


        # ── Новые проверки 23-34 ─────────────────────────────────────────
        self.check_aio_radiator_vs_case()       # 23. AIO: размер радиатора vs корпус
        self.check_aio_radiator_vs_ram()        # 24. AIO сверху + высокая ОЗУ
        self.check_cooler_mounting_kit()        # 25. LGA1700/1851 Mounting Kit
        self.check_intel_pcb_bend()             # 26. Изгиб текстолита LGA1700
        self.check_gpu_dual_cable()             # 27. Два кабеля питания GPU
        self.check_cpu_cable_length_tower()     # 28. Длина кабеля CPU в Full Tower
        self.check_intel_k_chipset()            # 29. K-CPU + не-Z чипсет
        self.check_infinity_fabric()            # 30. AM4/AM5 Infinity Fabric ratio
        self.check_usb_c_front_panel()          # 31. USB-C на панели vs Type-E header
        self.check_argb_rgb_headers()           # 32. ARGB 5V vs RGB 12V конфликт
        self.check_ram_population_order()       # 33. Порядок установки планок
        self.check_ecc_ram_compatibility()      # 34. ECC на потребительской плате

        self._build_summary()
        return self.result

    # ─────────────────────────────────────────────────────────────────────
    #  1. СОКЕТ
    # ─────────────────────────────────────────────────────────────────────

    def check_socket_compatibility(self):
        cpu_socket    = _g(self.cpu,    "socket")
        mb_socket     = _g(self.mb,     "socket")
        cooler_socket = _g(self.cooler, "socket")

        if cpu_socket and mb_socket and cpu_socket != mb_socket:
            self.result.critical.append(Issue(
                code="SOCKET_MISMATCH",
                title="Несовместимые сокеты CPU и материнской платы",
                detail=f"Процессор имеет сокет {cpu_socket}, а материнская плата — {mb_socket}. "
                       f"Физически несовместимы. Замените один из компонентов.",
                field="cpu/mb"
            ))

        if cpu_socket and cooler_socket and cooler_socket != "Universal":
            supported = [s.strip().upper() for s in cooler_socket.split(",")]
            if cpu_socket not in supported:
                self.result.critical.append(Issue(
                    code="COOLER_SOCKET_MISMATCH",
                    title="Кулер не подходит к процессору",
                    detail=f"Кулер поддерживает сокеты {cooler_socket}, "
                           f"но процессор использует {cpu_socket}. "
                           f"Потребуется крепёж или другой кулер.",
                    field="cooler"
                ))

    # ─────────────────────────────────────────────────────────────────────
    #  2. ТИП ОЗУ
    # ─────────────────────────────────────────────────────────────────────

    def check_ram_type(self):
        mb_ddr = _g(self.mb, "ramType")
        if not mb_ddr:
            return

        for i, stick in enumerate(self.ram_sticks):
            stick_ddr = _g(stick, "ramType")
            if stick_ddr and stick_ddr != mb_ddr:
                self.result.critical.append(Issue(
                    code="RAM_TYPE_MISMATCH",
                    title=f"Тип памяти модуля #{i+1} не совместим с материнской платой",
                    detail=f"Материнская плата поддерживает {mb_ddr}, "
                           f"а выбранный модуль — {stick_ddr}. "
                           f"Физически несовместимо (разные контакты и напряжение).",
                    field="ram"
                ))

        cpu_ddr = _g(self.cpu, "ramType")
        if cpu_ddr and mb_ddr and cpu_ddr != mb_ddr:
            self.result.critical.append(Issue(
                code="CPU_RAM_TYPE_MISMATCH",
                title="Тип памяти CPU не соответствует плате",
                detail=f"Процессор нативно поддерживает {cpu_ddr}, "
                       f"плата рассчитана под {mb_ddr}.",
                field="cpu/mb"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  3. СЛОТЫ ОЗУ
    # ─────────────────────────────────────────────────────────────────────

    def check_ram_slots(self):
        mb_slots = _int(_g(self.mb, "ramSlots"), 0)
        if not mb_slots or not self.ram_sticks:
            return

        if len(self.ram_sticks) > mb_slots:
            self.result.critical.append(Issue(
                code="RAM_SLOTS_OVERFLOW",
                title="Не хватает слотов ОЗУ",
                detail=f"Выбрано {len(self.ram_sticks)} модулей памяти, "
                       f"но плата имеет только {mb_slots} слота. "
                       f"Уберите лишние модули или выберите плату с большим числом слотов.",
                field="mb"
            ))

        if len(self.ram_sticks) == 1 and mb_slots >= 2:
            self.result.advisory.append(Issue(
                code="SINGLE_CHANNEL",
                title="Включён одноканальный режим памяти",
                detail=f"Один модуль ОЗУ в двухслотовой плате даёт одноканальный режим. "
                       f"Производительность на 10–30% ниже двухканального. "
                       f"Рекомендуем добавить второй идентичный модуль.",
                field="ram"
            ))

        mb_max_freq = _int(_g(self.mb, "ramMaxFreq"), 0)
        for i, stick in enumerate(self.ram_sticks):
            stick_freq = _int(_g(stick, "ramMaxFreq"), 0)
            if stick_freq and mb_max_freq and stick_freq > mb_max_freq:
                self.result.warning.append(Issue(
                    code="RAM_FREQ_THROTTLE",
                    title=f"Модуль #{i+1} будет понижен по частоте",
                    detail=f"Модуль поддерживает {stick_freq} МГц, "
                           f"но плата ограничена {mb_max_freq} МГц. "
                           f"Система будет работать на более низкой частоте.",
                    field="ram"
                ))

    # ─────────────────────────────────────────────────────────────────────
    #  4. ФИЗИЧЕСКИЙ КОНФЛИКТ RAM / КУЛЕР
    # ─────────────────────────────────────────────────────────────────────

    def check_ram_cooler_clearance(self):
        if not self.cooler or not self.ram_sticks:
            return

        for i, stick in enumerate(self.ram_sticks):
            ram_h = _int(_g(stick, "ramHeight"), 0)
            if not ram_h:
                continue

            if ram_h > 35:
                self.result.warning.append(Issue(
                    code="RAM_COOLER_HEIGHT",
                    title=f"Высокий профиль ОЗУ (модуль #{i+1}) может конфликтовать с кулером",
                    detail=f"Высота модуля памяти — {ram_h} мм. "
                           f"Башенные кулеры перекрывают первый слот при высоте ОЗУ > 35 мм. "
                           f"Проверьте, поддерживает ли кулер 'Low-Profile RAM clearance', "
                           f"или используйте память без высоких радиаторов (≤ 33 мм).",
                    field="ram/cooler"
                ))

            if ram_h > 50:
                self.result.critical.append(Issue(
                    code="RAM_COOLER_HEIGHT_CRITICAL",
                    title=f"Планка ОЗУ #{i+1} физически не поместится рядом с кулером",
                    detail=f"Высота модуля {ram_h} мм — это почти наверняка конфликт "
                           f"с любым башенным кулером. Выберите память с профилем ≤ 33 мм.",
                    field="ram/cooler"
                ))

    # ─────────────────────────────────────────────────────────────────────
    #  5. ЭНЕРГЕТИЧЕСКИЙ АУДИТ (DEEP POWER CHECK)
    # ─────────────────────────────────────────────────────────────────────

    def check_power_deep(self):
        cpu_tdp  = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp  = _int(_g(self.gpu, "gpuTdp"), 0)
        psu_w    = _int(_g(self.psu, "psuWattage"), 0)
        gpu_req  = _int(_g(self.gpu, "gpuReqPsu"), 0)

        total_tdp = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        rec_psu   = int(total_tdp * (1 + PSU_HEADROOM_PCT))

        if not psu_w and self.gpu:
            req = gpu_req or (gpu_tdp + 150)
            self.result.warning.append(Issue(
                code="NO_PSU_SELECTED",
                title="Блок питания не выбран",
                detail=f"В сборке есть видеокарта (TDP {gpu_tdp} Вт, "
                       f"рекомендовано >= {req} Вт). "
                       f"Добавьте БП мощностью не менее {req} Вт.",
                field="psu"
            ))

        if psu_w and total_tdp:
            if psu_w < total_tdp:
                self.result.critical.append(Issue(
                    code="PSU_UNDERPOWERED",
                    title="Блок питания не обеспечивает нужную мощность",
                    detail=f"Расчётное потребление: {total_tdp} Вт "
                           f"(CPU {cpu_tdp} W + GPU {gpu_tdp} W + система {SYSTEM_OVERHEAD_W} W). "
                           f"БП: {psu_w} Вт. Система не запустится или будет нестабильна. "
                           f"Рекомендуется БП ≥ {rec_psu} Вт.",
                    field="psu"
                ))
            elif psu_w < rec_psu:
                self.result.warning.append(Issue(
                    code="PSU_LOW_HEADROOM",
                    title="Малый запас мощности БП",
                    detail=f"БП {psu_w} Вт покрывает пиковое потребление {total_tdp} Вт, "
                           f"но запас менее 20%. При разгоне или пиковых нагрузках "
                           f"возможны просадки напряжения. Рекомендуется {rec_psu} Вт.",
                    field="psu"
                ))

        if psu_w and gpu_req and psu_w < gpu_req:
            self.result.critical.append(Issue(
                code="PSU_BELOW_GPU_REQUIREMENT",
                title="БП ниже рекомендации производителя видеокарты",
                detail=f"Производитель GPU требует минимум {gpu_req} Вт, "
                       f"выбранный БП: {psu_w} Вт. "
                       f"Возможны артефакты, вылеты или несохранённые данные.",
                field="psu"
            ))

        mb_cpu_pin  = _g(self.mb,  "cpuPowerPin", "---")
        psu_cpu_pin = _g(self.psu, "cpuPowerPin", "---")
        mb_amps     = CPU_PIN_AMPERAGE.get(mb_cpu_pin, 0)
        psu_amps    = CPU_PIN_AMPERAGE.get(psu_cpu_pin, 0)

        if mb_amps and psu_amps:
            if psu_amps < mb_amps:
                self.result.critical.append(Issue(
                    code="CPU_POWER_PIN_CRITICAL",
                    title="БП не имеет нужного разъёма питания CPU",
                    detail=f"Материнская плата требует {mb_cpu_pin}, "
                           f"а БП предоставляет только {psu_cpu_pin}. "
                           f"Запуск невозможен или разгон будет заблокирован.",
                    field="psu/mb"
                ))
            elif psu_amps == mb_amps and mb_amps >= 3:
                self.result.advisory.append(Issue(
                    code="CPU_POWER_PIN_OC_LIMIT",
                    title="Разъём питания CPU ограничивает разгон",
                    detail=f"Плата имеет {mb_cpu_pin} (полный разгон), "
                           f"БП подаёт {psu_cpu_pin}. "
                           f"Разгон через второй разъём может быть ограничен.",
                    field="psu"
                ))

        gpu_pin     = _g(self.gpu, "gpuPowerPin", "")
        psu_gpu_pin = _g(self.psu, "gpuPowerPin", "")

        if gpu_pin == "12VHPWR (16 pin)":
            if psu_gpu_pin and "12VHPWR" not in psu_gpu_pin:
                self.result.warning.append(Issue(
                    code="GPU_12VHPWR_ADAPTER",
                    title="Видеокарта требует 12VHPWR, но БП не имеет нативного разъёма",
                    detail="GPU использует разъём 12VHPWR (16 pin). "
                           "БП не имеет нативного кабеля — потребуется переходник 4×8-pin→16-pin. "
                           "Переходники повышают риск перегрева контактов: "
                           "используйте только сертифицированные кабели (не 3rd party).",
                    field="gpu/psu"
                ))
            else:
                self.result.advisory.append(Issue(
                    code="GPU_12VHPWR_REMINDER",
                    title="Видеокарта с 12VHPWR — соблюдайте правила укладки кабеля",
                    detail="Разъём 12VHPWR чувствителен к изгибу кабеля у коннектора. "
                           "Не сгибайте кабель под углом > 90° ближе 35 мм от разъёма "
                           "(рекомендация NVIDIA после случаев оплавления на RTX 40xx).",
                    field="gpu"
                ))
        elif gpu_pin and gpu_pin != "без питания":
            if not psu_gpu_pin or psu_gpu_pin == "без питания":
                self.result.critical.append(Issue(
                    code="GPU_POWER_MISSING",
                    title="БП не имеет разъёмов питания для GPU",
                    detail=f"Видеокарте нужен {gpu_pin}, "
                           f"но у выбранного БП нет разъёмов питания GPU.",
                    field="psu"
                ))

    # ─────────────────────────────────────────────────────────────────────
    #  6. ЛИНИИ PCIe (BANDWIDTH AUDIT)
    # ─────────────────────────────────────────────────────────────────────

    def check_pcie_lanes(self):
        cpu_socket = _g(self.cpu, "socket")
        if not cpu_socket:
            return

        total_lanes   = PCIE_LANES_BY_SOCKET.get(cpu_socket, 20)
        used_lanes    = 0
        issues_detail = []

        if self.gpu:
            gpu_pci = _g(self.gpu, "gpuPciVersion", "4.0")
            used_lanes += 16
            issues_detail.append(f"GPU: x16 ({gpu_pci})")

        if self.ssd:
            ssd_iface = _g(self.ssd, "ssdInterface", "")
            if ssd_iface == "NVMe":
                used_lanes += 4
                issues_detail.append("NVMe SSD #1: x4")

        mb_m2_slots = _int(_g(self.mb, "m2Slots"), 0)
        if mb_m2_slots > 1:
            extra_nvme = mb_m2_slots - 1
            used_lanes += extra_nvme * 4
            issues_detail.append(f"Потенциальных доп. NVMe M.2: {extra_nvme} × x4")

        if used_lanes > total_lanes:
            lost = used_lanes - total_lanes
            self.result.warning.append(Issue(
                code="PCIE_LANES_EXCEEDED",
                title="Расход линий PCIe превышает возможности процессора",
                detail=f"CPU ({cpu_socket}) предоставляет {total_lanes} линий PCIe. "
                       f"Конфигурация требует ≈ {used_lanes} линий "
                       f"({', '.join(issues_detail)}). "
                       f"Недостаёт ~{lost} линий — плата переключит GPU на x8 "
                       f"или урежет скорость NVMe. "
                       f"Производительность GPU снизится на 1–5%, NVMe может упасть вдвое.",
                field="mb/cpu"
            ))
        elif used_lanes > total_lanes * 0.9:
            self.result.advisory.append(Issue(
                code="PCIE_LANES_NEAR_LIMIT",
                title="Линии PCIe заняты почти полностью",
                detail=f"Используется {used_lanes} из {total_lanes} линий PCIe. "
                       f"При добавлении дополнительных NVMe или карт расширения "
                       f"возможно снижение скорости.",
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
                        title=f"GPU PCIe {gv} работает в слоте PCIe {mv} (пониженная версия)",
                        detail=f"Видеокарта поддерживает PCIe {gv}, но материнская плата "
                               f"предоставляет только PCIe {mv} x16. "
                               f"Карта работоспособна (обратная совместимость), "
                               f"{'потери производительности минимальны (~' + str(loss_pct) + '%)' if loss_pct else 'без заметных потерь'}. "
                               f"Для полной скорости нужна плата с PCIe {gv}.",
                        field="gpu/mb"
                    ))
            except (ValueError, TypeError):
                pass

    # ─────────────────────────────────────────────────────────────────────
    #  7. ФИЗИЧЕСКИЕ ГАБАРИТЫ GPU
    # ─────────────────────────────────────────────────────────────────────

    def check_gpu_physical(self):
        if not self.gpu or not self.case:
            return

        gpu_len     = _int(_g(self.gpu,  "gpuLength"), 0)
        max_gpu_len = _int(_g(self.case, "maxGpuLength"), 0)
        gpu_slots   = _int(_g(self.gpu,  "gpuSlots"), 0)

        if gpu_len and max_gpu_len:
            if gpu_len > max_gpu_len:
                self.result.critical.append(Issue(
                    code="GPU_TOO_LONG",
                    title="Видеокарта не помещается в корпус по длине",
                    detail=f"GPU: {gpu_len} мм, максимум для корпуса: {max_gpu_len} мм. "
                           f"Разница: {gpu_len - max_gpu_len} мм. "
                           f"Карту физически не вставить. "
                           f"Выберите компактную версию (например, ITX-edition) "
                           f"или другой корпус.",
                    field="gpu/case"
                ))
            elif gpu_len > max_gpu_len * 0.92:
                self.result.warning.append(Issue(
                    code="GPU_TIGHT_FIT",
                    title="Видеокарта почти не умещается в корпус",
                    detail=f"GPU: {gpu_len} мм, допустимо: {max_gpu_len} мм. "
                           f"Запас всего {max_gpu_len - gpu_len} мм. "
                           f"Кабели питания в нижней части могут мешать установке. "
                           f"Используйте кабели с угловым коннектором.",
                    field="gpu/case"
                ))

        if gpu_slots == 3:
            self.result.advisory.append(Issue(
                code="GPU_TRIPLE_SLOT",
                title="Трёхслотовая видеокарта — проверьте наличие свободных слотов",
                detail=f"GPU занимает 3 слота расширения. "
                       f"Убедитесь, что в корпусе есть 3 смежных свободных заглушки, "
                       f"и что другие карты расширения (Wi-Fi, звуковая) не мешают.",
                field="gpu/case"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  8. КУЛЕР vs CPU (TDP)
    # ─────────────────────────────────────────────────────────────────────

    def check_cooler_vs_cpu(self):
        cpu_tdp    = _int(_g(self.cpu,    "tdp"), 0)
        cooler_tdp = _int(_g(self.cooler, "maxTdp"), 0)

        if not cpu_tdp or not cooler_tdp:
            return

        if cooler_tdp < cpu_tdp:
            self.result.critical.append(Issue(
                code="COOLER_TDP_INSUFFICIENT",
                title="Кулер не справится с тепловыделением процессора",
                detail=f"TDP процессора: {cpu_tdp} Вт, кулер рассчитан на {cooler_tdp} Вт. "
                       f"Система будет троттлить и перегреваться. "
                       f"Выберите кулер с maxTDP ≥ {int(cpu_tdp * 1.15)} Вт (запас 15%).",
                field="cooler"
            ))
        elif cooler_tdp < cpu_tdp * 1.15:
            self.result.advisory.append(Issue(
                code="COOLER_TDP_MARGINAL",
                title="Кулер работает почти на пределе TDP",
                detail=f"Кулер рассчитан на {cooler_tdp} Вт, CPU потребляет {cpu_tdp} Вт. "
                       f"Запас < 15%. При нагрузке температура будет высокой. "
                       f"Обеспечьте хорошую вентиляцию в корпусе.",
                field="cooler"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  9. КУЛЕР vs КОРПУС (высота)
    # ─────────────────────────────────────────────────────────────────────

    def check_cooler_vs_case(self):
        cooler_h = _int(_g(self.cooler, "coolerHeight"), 0)
        case_max = _int(_g(self.case,   "maxCpuCoolerHeight"), 0)

        if not cooler_h or not case_max:
            return

        if cooler_h > case_max:
            self.result.critical.append(Issue(
                code="COOLER_HEIGHT_OVERFLOW",
                title="Кулер не помещается в корпус",
                detail=f"Высота кулера: {cooler_h} мм, "
                       f"максимум для корпуса: {case_max} мм. "
                       f"Разница: {cooler_h - case_max} мм. Крышка корпуса не закроется.",
                field="cooler/case"
            ))
        elif cooler_h > case_max - 5:
            self.result.warning.append(Issue(
                code="COOLER_HEIGHT_TIGHT",
                title="Кулер в притык по высоте",
                detail=f"Кулер {cooler_h} мм, лимит корпуса {case_max} мм. "
                       f"Запас всего {case_max - cooler_h} мм — "
                       f"термопаста сожмётся, но могут быть вибрации боковой панели.",
                field="cooler/case"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  10. ФОРМ-ФАКТОР КОРПУСА vs МАТЕРИНСКАЯ ПЛАТА
    # ─────────────────────────────────────────────────────────────────────

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
                    detail=f"Корпус поддерживает: {', '.join(supported)}. "
                           f"Выбрана плата формата {mb_ff}. "
                           f"Крепёжные отверстия не совпадут.",
                    field="mb/case"
                ))
            return

        mb_rank   = MB_SIZE_RANK.get(mb_ff, -1)
        case_rank = MB_SIZE_RANK.get(case_ff, -1)

        if mb_rank > case_rank and mb_rank != -1 and case_rank != -1:
            self.result.critical.append(Issue(
                code="MB_TOO_LARGE_FOR_CASE",
                title="Материнская плата слишком большая для корпуса",
                detail=f"Плата {mb_ff} не вместится в корпус {case_ff}. "
                       f"Выберите корпус формата {mb_ff} или плату меньшего размера.",
                field="mb/case"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  11. ФОРМ-ФАКТОР БП vs КОРПУС
    # ─────────────────────────────────────────────────────────────────────

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
                detail="Корпус Mini-ITX обычно требует БП формата SFX или SFX-L. "
                       "ATX БП физически не вставить — другой размер и крепёж.",
                field="psu/case"
            ))

        if psu_len and case_psu_max and psu_len > case_psu_max:
            self.result.critical.append(Issue(
                code="PSU_TOO_LONG",
                title="Блок питания слишком длинный для корпуса",
                detail=f"Длина БП: {psu_len} мм, максимум для корпуса: {case_psu_max} мм. "
                       f"Возможен конфликт с кабелями или накопителями.",
                field="psu/case"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  12. BIOS FLASHBACK
    # ─────────────────────────────────────────────────────────────────────

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
                detail=f"Плата на чипсете {chipset} может быть выпущена "
                       f"до появления вашего процессора. "
                       f"Перед установкой проверьте список совместимости на сайте производителя. "
                       f"Если BIOS устарел — воспользуйтесь функцией BIOS Flashback "
                       f"(обновление без CPU/RAM) или прошейте через старый поддерживаемый CPU.",
                field="mb"
            ))

        if cpu_socket == "AM4" and chipset in ("A320", "B350"):
            cpu_gen = _detect_amd_gen(self.cpu.get("name", ""))
            if cpu_gen == 5:
                self.result.critical.append(Issue(
                    code="BIOS_AM4_GEN5_UNSUPPORTED",
                    title="Плата на A320/B350 не поддерживает Ryzen 5000",
                    detail=f"Большинство плат на чипсете {chipset} официально "
                           f"не получили поддержку Ryzen 5000 (Zen 3). "
                           f"Система может не запуститься. Рекомендуется плата B550 или X570.",
                    field="mb"
                ))

    # ─────────────────────────────────────────────────────────────────────
    #  13. SSD vs СЛОТЫ M.2
    # ─────────────────────────────────────────────────────────────────────

    def check_ssd_slot_availability(self):
        if not self.ssd or not self.mb:
            return

        ssd_iface   = _g(self.ssd, "ssdInterface", "")
        mb_m2_cnt   = _int(_g(self.mb, "m2Slots"), 0)
        mb_m2_types = _g(self.mb, "m2Types", [])

        if ssd_iface == "NVMe" and mb_m2_cnt == 0:
            self.result.critical.append(Issue(
                code="NO_M2_SLOT",
                title="Плата не имеет слотов M.2 для NVMe SSD",
                detail="Выбранный SSD — NVMe (M.2), но плата не поддерживает M.2 слоты. "
                       "Используйте SATA SSD или выберите другую плату.",
                field="ssd/mb"
            ))

        if ssd_iface == "NVMe" and mb_m2_types and "NVMe" not in mb_m2_types:
            self.result.critical.append(Issue(
                code="M2_NVME_UNSUPPORTED",
                title="Слот M.2 на плате не поддерживает NVMe",
                detail="Плата имеет M.2 слот только для SATA SSD. "
                       "NVMe SSD в нём не заработает (нет PCIe сигнала). "
                       "Выберите плату с NVMe-совместимым M.2 слотом.",
                field="ssd/mb"
            ))

    # ═══════════════════════════════════════════════════════════════════════
    #  14–22. НОВЫЕ ПРОВЕРКИ
    # ═══════════════════════════════════════════════════════════════════════

    # ─────────────────────────────────────────────────────────────────────
    #  14. ИНТЕГРИРОВАННАЯ ГРАФИКА (iGPU)
    #      Если нет дискретной GPU — нужна iGPU в CPU, иначе нет картинки
    # ─────────────────────────────────────────────────────────────────────

    def check_igpu(self):
        if self.gpu:
            return  # дискретная есть — iGPU не критична

        if not self.cpu:
            return

        cpu_name = (self.cpu.get("name") or "").lower()
        has_igpu = False

        # Intel: iGPU есть у всех Core, КРОМЕ серии F
        if any(x in cpu_name for x in ("intel", "core i", "core ultra", "pentium", "celeron")):
            if not re.search(IGPU_INTEL_EXCLUDE, cpu_name, re.I):
                has_igpu = True

        # AMD: iGPU только у G-серии Ryzen
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
                    "Система загрузится, но на мониторе ничего не будет. "
                    "Решения: добавить дискретную GPU, "
                    "или заменить CPU на модель с iGPU "
                    "(Intel без суффикса F, или AMD Ryzen G-серии: 5600G, 7700G, 8600G...)."
                ),
                field="gpu/cpu"
            ))
        else:
            self.result.advisory.append(Issue(
                code="IGPU_ONLY_MODE",
                title="Работа на встроенной графике CPU — производительность ограничена",
                detail=(
                    "Дискретная видеокарта не выбрана. "
                    "Система будет использовать iGPU процессора. "
                    "Для игр и рендеринга производительность значительно ниже дискретной GPU. "
                    "Убедитесь, что в BIOS включён видеовыход (Display Output = iGPU / Auto)."
                ),
                field="cpu"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  15. ОБЩИЙ ОБЪЁМ ОЗУ vs МАКСИМУМ ПЛАТЫ
    # ─────────────────────────────────────────────────────────────────────

    def check_ram_total_capacity(self):
        if not self.ram_sticks or not self.mb:
            return

        total_gb = sum(_int(_g(s, "ramCapacity"), 0) for s in self.ram_sticks)
        if not total_gb:
            return

        mb_ff    = _g(self.mb, "formFactor", "")
        ram_type = _g(self.mb, "ramType", "")

        # Типовые лимиты по форм-фактору и типу памяти
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

        if total_gb > mb_max_gb:
            self.result.critical.append(Issue(
                code="RAM_CAPACITY_OVERFLOW",
                title="Суммарный объём ОЗУ превышает лимит платы",
                detail=(
                    f"Установлено {total_gb} ГБ ОЗУ, "
                    f"но плата формата {mb_ff} ({ram_type}) "
                    f"типично поддерживает максимум {mb_max_gb} ГБ. "
                    f"Система не запустится или не определит лишнюю память. "
                    f"Уточните максимум в спецификации конкретной модели платы."
                ),
                field="ram/mb"
            ))

        if total_gb >= 64:
            self.result.advisory.append(Issue(
                code="RAM_LARGE_CAPACITY",
                title=f"Установлено {total_gb} ГБ ОЗУ — убедитесь, что ОС 64-битная",
                detail=(
                    "Для использования более 4 ГБ ОЗУ необходима 64-битная ОС. "
                    "Windows 10/11 Home поддерживает до 128 ГБ, Pro — до 2 ТБ. "
                    "При большом объёме убедитесь, что все планки видны в BIOS "
                    "(иногда нужно включить XMP/EXPO для стабильности)."
                ),
                field="ram"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  16. XMP / EXPO — быстрая память без включения профиля работает
    #      на базовой частоте JEDEC (DDR4: 2133, DDR5: 4800 МГц)
    # ─────────────────────────────────────────────────────────────────────

    def check_xmp_expo(self):
        if not self.ram_sticks:
            return

        for i, stick in enumerate(self.ram_sticks):
            ram_type  = _g(stick, "ramType", "")
            freq      = _int(_g(stick, "ramMaxFreq"), 0)
            threshold = XMP_THRESHOLD_DDR5 if ram_type == "DDR5" else XMP_THRESHOLD_DDR4

            if freq and freq > threshold:
                profile = "EXPO" if ram_type == "DDR5" else "XMP"
                self.result.advisory.append(Issue(
                    code=f"XMP_EXPO_REQUIRED",
                    title=f"ОЗУ {freq} МГц: для достижения заявленной скорости включите {profile} в BIOS",
                    detail=(
                        f"Планка {ram_type} {freq} МГц без активации профиля {profile} "
                        f"будет работать на базовой частоте JEDEC ({threshold} МГц). "
                        f"Чтобы получить заявленную скорость: "
                        f"войдите в BIOS → Memory / OC → включите {profile} Profile 1. "
                        f"Для AMD Ryzen это называется EXPO, для Intel — XMP 3.0."
                    ),
                    field="ram"
                ))
                break  # один совет на всю сборку достаточно

    # ─────────────────────────────────────────────────────────────────────
    #  17. БУТЫЛОЧНОЕ ГОРЛЫШКО (BOTTLENECK)
    #      Грубая оценка по TDP: CPU vs GPU дисбаланс
    # ─────────────────────────────────────────────────────────────────────

    def check_bottleneck(self):
        if not self.cpu or not self.gpu:
            return

        cpu_tdp = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp = _int(_g(self.gpu, "gpuTdp"), 0)

        if not cpu_tdp or not gpu_tdp:
            return

        def _classify(tdp: int, table: dict) -> str:
            for threshold in sorted(table):
                if tdp <= threshold:
                    return table[threshold]
            return "flagship"

        cpu_class = _classify(cpu_tdp, CPU_CLASS_BY_TDP)
        gpu_class = _classify(gpu_tdp, GPU_CLASS_BY_TDP)

        classes = ["low-end", "mid-low", "mid", "mid-high", "high-end", "flagship"]
        ci = classes.index(cpu_class)
        gi = classes.index(gpu_class)
        diff = gi - ci  # >0: GPU мощнее CPU

        if diff >= 3:
            self.result.warning.append(Issue(
                code="BOTTLENECK_CPU",
                title="Процессор может стать узким местом для видеокарты",
                detail=(
                    f"CPU класса «{cpu_class}» (TDP {cpu_tdp} Вт) "
                    f"и GPU класса «{gpu_class}» (TDP {gpu_tdp} Вт) — значительный дисбаланс. "
                    f"В процессорозависимых играх (CS2, RPG, стратегии) "
                    f"CPU будет ограничивать FPS. "
                    f"Рекомендуется CPU уровня «{classes[max(0, gi - 1)]}» или выше."
                ),
                field="cpu/gpu"
            ))
        elif diff <= -3:
            self.result.advisory.append(Issue(
                code="BOTTLENECK_GPU",
                title="Видеокарта слабее процессора — возможен дисбаланс",
                detail=(
                    f"CPU класса «{cpu_class}» (TDP {cpu_tdp} Вт) "
                    f"значительно мощнее GPU класса «{gpu_class}» (TDP {gpu_tdp} Вт). "
                    f"В играх FPS будет ограничен GPU, а CPU будет простаивать. "
                    f"Рассмотрите GPU помощнее или выберите CPU попроще и сэкономьте бюджет."
                ),
                field="cpu/gpu"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  18. WiFi / Bluetooth
    # ─────────────────────────────────────────────────────────────────────

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
                    "Выбранная плата не имеет встроенного беспроводного модуля. "
                    "Если нужен Wi-Fi — докупите PCIe Wi-Fi карту (≈ 1 000–3 000 руб.) "
                    "или USB Wi-Fi адаптер. "
                    "Для Bluetooth — USB-донгл (≈ 300–600 руб.). "
                    "Платы с Wi-Fi обычно имеют суффикс 'Wi-Fi' или 'AX' в названии."
                ),
                field="mb"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  19. NVMe Gen4 / Gen5 — НАГРЕВ SSD
    # ─────────────────────────────────────────────────────────────────────

    def check_nvme_heatsink(self):
        if not self.ssd or not self.mb:
            return

        ssd_iface = _g(self.ssd, "ssdInterface", "")
        if ssd_iface != "NVMe":
            return

        ssd_name = (self.ssd.get("name") or "").lower()
        mb_specs = self.mb.get("specs") or {}

        is_gen4_or_gen5 = any(x in ssd_name for x in (
            "gen4", "gen 4", "gen5", "gen 5",
            "pcie 4", "pcie 5", "nvme 2"
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
                        "SSD поколения Gen4/Gen5 греется до 80–90°C под нагрузкой. "
                        "Встроенный радиатор M.2 платы поможет удержать температуру. "
                        "Убедитесь, что термопрокладка между SSD и радиатором не повреждена."
                    ),
                    field="ssd"
                ))
            else:
                self.result.warning.append(Issue(
                    code="NVME_GEN4_NO_HEATSINK",
                    title="NVMe Gen4/Gen5 SSD перегреется без радиатора",
                    detail=(
                        "Скоростные NVMe Gen4/Gen5 накопители греются до 80–90°C. "
                        "Без охлаждения контроллер уйдёт в троттлинг — скорость падает в 2–5 раз. "
                        "Решения: "
                        "1) Плата с радиатором M.2 (M.2 Heatsink / M.2 Shield). "
                        "2) Отдельный радиатор для M.2 (200–600 руб.). "
                        "3) SSD с пассивным охлаждением от производителя."
                    ),
                    field="ssd/mb"
                ))
        else:
            self.result.advisory.append(Issue(
                code="NVME_GEN3_SLOT_CHECK",
                title="NVMe SSD: проверьте, что слот M.2 поддерживает PCIe (не только SATA)",
                detail=(
                    "Некоторые платы имеют M.2 слоты только для SATA SSD — "
                    "в них NVMe работать не будет. "
                    "В спецификации платы слот должен быть помечен как 'PCIe + SATA' или 'NVMe'."
                ),
                field="ssd/mb"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  20. SATA SSD 2.5" В Mini-ITX КОРПУСЕ
    # ─────────────────────────────────────────────────────────────────────

    def check_sata_ssd_bay(self):
        if not self.ssd or not self.case:
            return

        ssd_ff    = _g(self.ssd,  "ssdFormFactor", "")
        ssd_iface = _g(self.ssd,  "ssdInterface", "")
        case_ff   = _g(self.case, "formFactor", "")

        is_sata_25 = (ssd_ff == '2.5"' or ssd_iface == "SATA")

        if is_sata_25 and case_ff == "Mini-ITX":
            self.result.warning.append(Issue(
                code="SATA_SSD_NO_BAY_MINIITX",
                title="SATA SSD 2.5\" может не поместиться в Mini-ITX корпус",
                detail=(
                    "Многие компактные Mini-ITX корпуса не имеют "
                    "стандартных отсеков для 2.5\" накопителей. "
                    "Перед покупкой проверьте спецификацию корпуса: наличие 2.5\" Bay. "
                    "Альтернатива — M.2 NVMe SSD, который крепится прямо на материнскую плату."
                ),
                field="ssd/case"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  21. КПД БП: ЗОНА ЭФФЕКТИВНОСТИ
    # ─────────────────────────────────────────────────────────────────────

    def check_psu_efficiency_zone(self):
        psu_w   = _int(_g(self.psu, "psuWattage"), 0)
        cpu_tdp = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp = _int(_g(self.gpu, "gpuTdp"), 0)

        if not psu_w or not (cpu_tdp or gpu_tdp):
            return

        total_w  = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        load_pct = total_w / psu_w

        if load_pct < PSU_SWEET_SPOT_MIN:
            self.result.advisory.append(Issue(
                code="PSU_OVERSIZED",
                title=f"БП избыточен: реальная нагрузка ≈ {int(load_pct * 100)}% от номинала",
                detail=(
                    f"При типичном потреблении {total_w} Вт и БП {psu_w} Вт "
                    f"нагрузка составит ≈ {int(load_pct * 100)}%. "
                    f"Оптимальная зона КПД — 40–80%. "
                    f"При нагрузке < 40% КПД снижается и вы переплачиваете за электричество. "
                    f"Это не критично — БП просто работает не в пике эффективности."
                ),
                field="psu"
            ))
        elif load_pct > PSU_SWEET_SPOT_MAX:
            self.result.advisory.append(Issue(
                code="PSU_NEAR_MAX_LOAD",
                title=f"БП работает при нагрузке ≈ {int(load_pct * 100)}% — повышенный нагрев",
                detail=(
                    f"При потреблении {total_w} Вт и БП {psu_w} Вт "
                    f"нагрузка ≈ {int(load_pct * 100)}%. "
                    f"При такой нагрузке БП шумит и греется сильнее, "
                    f"ресурс конденсаторов сокращается. "
                    f"Рекомендуется БП на 20–25% мощнее: ≈ {int(total_w / 0.65)} Вт."
                ),
                field="psu"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  22. ОБЪЁМ VRAM — ХВАТИТ ЛИ ДЛЯ ЗАДАЧИ
    # ─────────────────────────────────────────────────────────────────────

    def check_vram_adequacy(self):
        if not self.gpu:
            return

        vram_gb = _int(_g(self.gpu, "vram"), 0)
        if not vram_gb:
            return

        if vram_gb < VRAM_MIN_1080P:
            self.result.warning.append(Issue(
                code="VRAM_LOW_1080P",
                title=f"Объём VRAM ({vram_gb} ГБ) мал для современных игр",
                detail=(
                    f"В современных играх при 1080p рекомендуется минимум {VRAM_MIN_1080P} ГБ VRAM. "
                    f"При {vram_gb} ГБ возможны подгрузки текстур и stuttering "
                    f"в требовательных играх (Alan Wake 2, Cyberpunk 2077 с RT). "
                    f"На Medium-High настройках проблем, скорее всего, не будет."
                ),
                field="gpu"
            ))
        elif vram_gb >= VRAM_MIN_4K:
            self.result.advisory.append(Issue(
                code="VRAM_4K_READY",
                title=f"VRAM {vram_gb} ГБ — видеокарта готова к 4K и AI-задачам",
                detail=(
                    f"{vram_gb} ГБ VRAM достаточно для 4K-гейминга, "
                    f"локального запуска LLM (до 7B параметров), "
                    f"и профессиональной работы в DaVinci Resolve / Blender. "
                    f"Для AI/3D в полную силу также рекомендуется 32+ ГБ системной RAM."
                ),
                field="gpu"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  СВОДКА
    # ─────────────────────────────────────────────────────────────────────


    # ═══════════════════════════════════════════════════════════════════════
    #  23–34. НОВЫЕ ПРОВЕРКИ (физика, питание, платформенные нюансы)
    # ═══════════════════════════════════════════════════════════════════════

    # ─────────────────────────────────────────────────────────────────────
    #  23. AIO: РАЗМЕР РАДИАТОРА vs КОРПУС
    #  Корпус может быть ATX, но не поддерживать 360 мм сверху
    #  (мешают радиаторы VRM материнской платы)
    # ─────────────────────────────────────────────────────────────────────

    def check_aio_radiator_vs_case(self):
        if not self.cooler or not self.case:
            return
        if _g(self.cooler, "coolerType", "") != "AIO":
            return

        rad_size = _int(_g(self.cooler, "aioRadiatorSize"), 0)
        if not rad_size:
            return

        supported = _g(self.case, "maxRadiatorSizes", []) or []

        if supported and rad_size not in supported:
            self.result.critical.append(Issue(
                code="AIO_RADIATOR_NOT_SUPPORTED",
                title=f"Корпус не поддерживает радиатор СЖО {rad_size} мм",
                detail=(
                    f"Выбранная СЖО имеет радиатор {rad_size} мм, "
                    f"но корпус поддерживает только: {sorted(supported)} мм. "
                    f"Радиатор физически не встанет ни на одну панель. "
                    f"Выберите СЖО с меньшим радиатором или другой корпус."
                ),
                field="cooler/case"
            ))
        elif rad_size == 360:
            # 360 мм при верхней установке часто упирается в VRM-радиатор MB
            self.result.advisory.append(Issue(
                code="AIO_360_VRM_CLEARANCE",
                title="СЖО 360 мм: проверьте совместимость с VRM-радиатором платы",
                detail=(
                    "Радиатор 360 мм при верхней установке может упереться в "
                    "радиаторы цепей питания (VRM heatsink) материнской платы. "
                    "Актуально для плат с высокими VRM-радиаторами (ASUS ROG, MSI MEG). "
                    "Перед покупкой проверьте раздел 'Compatible CPU Coolers' "
                    "на сайте производителя корпуса или форум с замерами."
                ),
                field="cooler/mb"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  24. AIO СВЕРХУ + ВЫСОКАЯ ОЗУ
    #  Вентиляторы радиатора нависают над DIMM-слотами
    # ─────────────────────────────────────────────────────────────────────

    def check_aio_radiator_vs_ram(self):
        if not self.cooler or not self.ram_sticks:
            return
        if _g(self.cooler, "coolerType", "") != "AIO":
            return

        rad_size = _int(_g(self.cooler, "aioRadiatorSize"), 0)
        if rad_size < 240:          # 120 мм AIO сверху обычно не мешает
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
                        f"Модули памяти высотой {ram_h} мм (> 40 мм) "
                        f"могут физически касаться нижнего вентилятора. "
                        f"Решения: "
                        f"1) Низкопрофильная ОЗУ ≤ 35 мм (G.Skill Ripjaws V, Kingston без радиатора). "
                        f"2) Монтаж радиатора на переднюю панель (если позволяет корпус). "
                        f"3) Кулер с отступом вентиляторов от первого DIMM-слота."
                    ),
                    field="cooler/ram"
                ))
                break   # одного предупреждения достаточно

    # ─────────────────────────────────────────────────────────────────────
    #  25. LGA1700 / LGA1851 — MOUNTING KIT ДЛЯ СТАРЫХ КУЛЕРОВ
    #  Кулеры под LGA1200 и старше не имеют нативного крепления
    # ─────────────────────────────────────────────────────────────────────

    def check_cooler_mounting_kit(self):
        if not self.cooler or not self.cpu:
            return

        cpu_socket = _g(self.cpu, "socket", "")
        if cpu_socket not in LGA_NEED_MOUNTING_KIT:
            return

        cooler_sockets = (_g(self.cooler, "socket", "") or "").upper()

        # Если кулер уже включает нужный сокет — всё нормально
        if cpu_socket in cooler_sockets:
            return

        # Если поддерживает только старые Intel-сокеты — нужен kit
        old_intel = {"LGA1200", "LGA1151", "LGA1150", "LGA1155", "LGA1156"}
        has_only_old = any(s in cooler_sockets for s in old_intel)

        if has_only_old or cooler_sockets == "---":
            self.result.advisory.append(Issue(
                code="COOLER_MOUNTING_KIT_NEEDED",
                title=f"Кулер может потребовать Mounting Kit для {cpu_socket}",
                detail=(
                    f"Кулеры для LGA1200 и старше не имеют нативного крепления под {cpu_socket}. "
                    f"Большинство топовых производителей предоставляют бесплатный Upgrade Kit "
                    f"(Noctua, be quiet!, Thermalright, DeepCool). "
                    f"Запросите его на сайте производителя кулера до покупки. "
                    f"Без правильного крепления давление на крышку CPU будет неравномерным "
                    f"и температуры вырастут на 5–15°C."
                ),
                field="cooler"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  26. ИЗГИБ ТЕКСТОЛИТА INTEL LGA1700
    #  12–14 поколение i7/i9: стандартный mounting frame гнёт PCB
    # ─────────────────────────────────────────────────────────────────────

    def check_intel_pcb_bend(self):
        if not self.cpu:
            return

        cpu_socket = _g(self.cpu, "socket", "")
        if cpu_socket != "LGA1700":
            return

        cpu_name   = (self.cpu.get("name") or "").lower()
        cpu_tdp    = _int(_g(self.cpu, "tdp"), 0)
        is_highend = cpu_tdp >= 125 or any(x in cpu_name for x in ("i9-", "i7-", "core i9", "core i7"))

        if is_highend:
            self.result.advisory.append(Issue(
                code="INTEL_LGA1700_PCB_BEND",
                title="LGA1700 i7/i9: рекомендуется Contact Frame против изгиба PCB",
                detail=(
                    "Процессоры Intel 12–14 поколения (LGA1700) имеют задокументированную проблему: "
                    "стандартный механизм крепления материнской платы прогибает крышку IHS. "
                    "Это приводит к неравномерному контакту с кулером и температурам "
                    "на 5–15°C выше нормы. "
                    "Решение: Contact Frame (Thermalright LGA1700 — ~500 руб., или оригинальный Intel). "
                    "Особенно актуально для i9-13900K/14900K и i7-13700K/14700K. "
                    "Установка простая — заменяет штатный пластиковый frame платы."
                ),
                field="cpu"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  27. МОЩНЫЙ GPU — ДВА ОТДЕЛЬНЫХ КАБЕЛЯ (не сплиттер «поросячий хвост»)
    #  RTX 3080+, RX 6900XT+: один кабель с Y-разветвителем = риск перегрева
    # ─────────────────────────────────────────────────────────────────────

    def check_gpu_dual_cable(self):
        if not self.gpu or not self.psu:
            return

        gpu_tdp    = _int(_g(self.gpu, "gpuTdp"), 0)
        pin_count  = _int(_g(self.gpu, "gpuPowerPinCount"), 0)
        gpu_pin    = _g(self.gpu, "gpuPowerPin", "")
        psu_cables = _int(_g(self.psu, "gpuCableCount"), 0)

        if gpu_tdp < GPU_DUAL_CABLE_TDP_THRESHOLD or pin_count < 2:
            return

        if "12VHPWR" in (gpu_pin or ""):
            return  # 12VHPWR — один разъём уже несёт весь ток, другая история

        if psu_cables == 1 or psu_cables == 0:
            self.result.warning.append(Issue(
                code="GPU_SINGLE_CABLE_SPLITTER_RISK",
                title=f"GPU {gpu_tdp} Вт: не используйте один кабель с разветвителем",
                detail=(
                    f"Видеокарта ({gpu_tdp} Вт, {gpu_pin}) требует {pin_count} разъёма питания. "
                    f"Один кабель-«поросячий хвост» с двумя головками "
                    f"пропускает весь ток через один провод — это нагрев, нестабильность "
                    f"и в редких случаях пожар (реальные случаи с RTX 3080). "
                    f"Решение: БП с двумя отдельными PCIe кабелями, "
                    f"или замена БП на модульный с достаточным числом линий."
                ),
                field="gpu/psu"
            ))
        else:
            self.result.advisory.append(Issue(
                code="GPU_DUAL_CABLE_REMINDER",
                title=f"Мощный GPU ({gpu_tdp} Вт): каждый разъём — отдельным кабелем от БП",
                detail=(
                    f"Для {gpu_pin} ({pin_count} разъёма) "
                    f"подключайте каждый разъём отдельным кабелем от блока питания, "
                    f"а не Y-разветвителем с одного кабеля. "
                    f"Это снижает ток через каждый провод и разъём, "
                    f"уменьшает нагрев и падение напряжения."
                ),
                field="gpu/psu"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  28. FULL TOWER + КАБЕЛЬ ПИТАНИЯ CPU
    #  Стандартный EPS 4+4 / 8-pin кабель (60 см) часто не хватает
    #  при прокладке за задней стенкой Full Tower
    # ─────────────────────────────────────────────────────────────────────

    def check_cpu_cable_length_tower(self):
        if not self.psu or not self.case:
            return

        case_name = (self.case.get("name") or "").lower()
        case_ff   = _g(self.case, "formFactor", "")

        is_full_tower = "full" in case_name or case_ff == "Full Tower"
        if not is_full_tower:
            return

        cpu_pin   = _g(self.psu, "cpuPowerPin", "")
        psu_mod   = _g(self.psu, "psuModular", "---")

        if cpu_pin and cpu_pin != "---":
            self.result.advisory.append(Issue(
                code="FULL_TOWER_CPU_CABLE_TOO_SHORT",
                title="Full Tower: стандартный CPU-кабель БП может быть коротким",
                detail=(
                    f"В корпусах Full Tower при скрытой прокладке кабеля {cpu_pin} "
                    f"(EPS 12V, CPU power) за задней стенкой "
                    f"стандартного кабеля 55–65 см часто не хватает. "
                    f"Рекомендуется кабель длиной ≥ 75–80 см. "
                    f"{'Модульный БП (' + psu_mod + ') позволяет докупить удлинённый кабель.' if psu_mod in ('Full', 'Semi') else 'Немодульный БП: проверьте длину комплектного CPU-кабеля в обзорах.'}"
                ),
                field="psu/case"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  29. INTEL K-CPU + НЕ Z-ЧИПСЕТ
    #  Разгон через множитель доступен только на Z-платах
    # ─────────────────────────────────────────────────────────────────────

    def check_intel_k_chipset(self):
        if not self.cpu or not self.mb:
            return

        cpu_name = (self.cpu.get("name") or "").upper()
        mb_name  = (self.mb.get("name") or "").upper()

        # K / KS / KF — разблокированный множитель
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
                f"Процессор с индексом K (разблокированный множитель, TDP {cpu_tdp} Вт) "
                f"требует чипсет серии Z (Z890/Z790/Z690...) для разгона. "
                f"На чипсетах B/H разгон через множитель недоступен. "
                f"Вы платите за разблокированный CPU, но не можете его разогнать. "
                f"Рекомендации: "
                f"1) Замените плату на Z-чипсет. "
                f"2) Или возьмите CPU без K (идентичная производительность на B-плате). "
                f"3) KF-версия дешевле K (без iGPU) — если GPU есть, KF выгоднее."
            ),
            field="cpu/mb"
        ))

    # ─────────────────────────────────────────────────────────────────────
    #  30. AMD INFINITY FABRIC — ЧАСТОТА ОЗУ vs FCLK
    #  AM4 > 3600 МГц → режим 1:2 (падение latency)
    #  AM5 > 6000 МГц → нестабильность без ручной настройки
    # ─────────────────────────────────────────────────────────────────────

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
                    title=f"AM4 + ОЗУ {ram_freq} МГц: Infinity Fabric перейдёт в асинхронный режим 1:2",
                    detail=(
                        f"На AMD AM4 (Zen 2/3) шина Infinity Fabric (FCLK) синхронизируется с ОЗУ 1:1 "
                        f"до {IF_THRESHOLD_AM4} МГц. "
                        f"При {ram_freq} МГц FCLK отстаёт вдвое (режим 1:2) — "
                        f"это увеличивает задержку памяти и нивелирует преимущество быстрой ОЗУ. "
                        f"Оптимальная зона AM4: 3600–3800 МГц (FCLK 1800–1900 МГц, 1:1). "
                        f"Реальный прирост от 3600→4400 МГц в режиме 1:2 обычно < 2%."
                    ),
                    field="ram/cpu"
                ))
                break

            elif cpu_socket == "AM5" and ram_freq > IF_THRESHOLD_AM5:
                self.result.advisory.append(Issue(
                    code="AM5_IF_ASYNC_MODE",
                    title=f"AM5 + ОЗУ {ram_freq} МГц: выход из зоны синхронной FCLK",
                    detail=(
                        f"На AM5 (Zen 4/5) оптимальная точка синхронной работы — {IF_THRESHOLD_AM5} МГц "
                        f"при FCLK 3000 МГц (1:1). "
                        f"При {ram_freq} МГц система работает в асинхронном режиме — "
                        f"более высокая задержка и возможная нестабильность. "
                        f"Стабильность свыше 6000 МГц зависит от 'silicon lottery' конкретного чипа. "
                        f"Если память нестабильна — снизьте до {IF_THRESHOLD_AM5} МГц "
                        f"с включённым EXPO/XMP Profile 1."
                    ),
                    field="ram/cpu"
                ))
                break

    # ─────────────────────────────────────────────────────────────────────
    #  31. USB TYPE-C НА ПЕРЕДНЕЙ ПАНЕЛИ КОРПУСА vs Type-E header на плате
    #  Бюджетные платы часто не имеют Internal USB 3.2 Gen 2 Type-E (19-pin)
    # ─────────────────────────────────────────────────────────────────────

    def check_usb_c_front_panel(self):
        if not self.case or not self.mb:
            return

        # Ищем признаки USB-C на передней панели корпуса
        case_info = (
            str(self.case.get("specs") or {}) + " " +
            (self.case.get("name") or "")
        ).lower()
        has_front_usbc = any(x in case_info for x in (
            "type-c", "usb-c", "usb 3.2 gen 2",
            "usb4", "usb 3.2 type-c", "front type-c"
        ))
        if not has_front_usbc:
            return

        # Ищем Internal Type-E на плате
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
                title="USB-C на передней панели корпуса: проверьте наличие Type-E header на плате",
                detail=(
                    "Корпус имеет USB Type-C на передней панели. "
                    "Для подключения нужен внутренний разъём USB 3.2 Gen 2 Type-E (19-pin) на плате. "
                    "Бюджетные платы (особенно B-series и H-series) часто не имеют этого разъёма. "
                    "Последствие: порт USB-C на корпусе будет нефункциональным. "
                    "Проверьте спецификацию платы: раздел 'Internal I/O' — 'USB 3.2 Gen 2 × 1 (Type-E)'."
                ),
                field="case/mb"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  32. ARGB (5V 3-pin) vs RGB (12V 4-pin) — КОНФЛИКТ НАПРЯЖЕНИЙ
    #  Подключить 5V ARGB-вентилятор к 12V RGB-разъёму = сжечь ленту
    # ─────────────────────────────────────────────────────────────────────

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

        mb_has_argb  = any(x in mb_info for x in ("argb", "addressable", "5v d-rgb", "5v rgb"))
        mb_has_rgb12 = bool(re.search(r'12v\s*rgb|d_led\b|rgb_header', mb_info))
        case_has_argb= any(x in case_info for x in ("argb", "addressable", "5v"))
        case_has_rgb = any(x in case_info for x in ("rgb", "подсветк"))

        # Корпус с подсветкой, но плата без ARGB (только 12V RGB или без ничего)
        if case_has_argb and not mb_has_argb:
            self.result.warning.append(Issue(
                code="ARGB_HEADER_INCOMPATIBLE",
                title="Подсветка ARGB (5V) корпуса: на плате может не быть нужного разъёма",
                detail=(
                    "Корпус использует ARGB подсветку (5V, 3-pin Addressable). "
                    "Если на плате только 12V RGB разъёмы (4-pin) — подключать НЕЛЬЗЯ: "
                    "это сожжёт вентиляторы/ленты. "
                    "Проверьте спецификацию платы: "
                    "нужен разъём 'ARGB' / '5V D-RGB' / 'ADD_HEADER'. "
                    "Если разъёма нет — используйте отдельный ARGB-контроллер (300–600 руб.) "
                    "с питанием от SATA и управлением кнопкой или пультом."
                ),
                field="case/mb"
            ))

        # Плата только с ARGB, а корпус с 12V RGB — тоже не подключить
        elif case_has_rgb and mb_has_argb and not mb_has_rgb12:
            self.result.advisory.append(Issue(
                code="RGB12V_HEADER_MISSING",
                title="Корпус с 12V RGB: на плате может не быть 12V разъёма",
                detail=(
                    "Если вентиляторы/лента корпуса работают на 12V RGB (4-pin non-addressable), "
                    "а плата имеет только ARGB (5V) разъёмы — "
                    "прямое подключение также недопустимо. "
                    "Уточните тип подсветки в корпусе (5V или 12V) перед покупкой."
                ),
                field="case/mb"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  33. ПОРЯДОК УСТАНОВКИ ОЗУ В СЛОТЫ (DUAL CHANNEL)
    #  2 планки в 4-слотовой плате — нужны слоты A2+B2, не A1+B1
    # ─────────────────────────────────────────────────────────────────────

    def check_ram_population_order(self):
        if not self.ram_sticks or not self.mb:
            return

        mb_slots = _int(_g(self.mb, "ramSlots"), 0)
        n_sticks = len(self.ram_sticks)

        if mb_slots == 4 and n_sticks == 2:
            self.result.advisory.append(Issue(
                code="RAM_SLOT_POPULATION_ORDER",
                title="2 планки ОЗУ в 4-слотовой плате: соблюдайте порядок установки",
                detail=(
                    "Для активации двухканального режима (2 × выше пропускная способность) "
                    "устанавливайте планки в слоты A2+B2 "
                    "(обычно 2-й и 4-й от процессора, выделены цветом или маркировкой). "
                    "Установка в A1+A2 или B1+B2 активирует одноканальный режим — "
                    "производительность ниже на 10–30%. "
                    "Точное расположение слотов — в мануале к плате (Memory Installation Guide)."
                ),
                field="ram/mb"
            ))

        elif mb_slots == 2 and n_sticks == 1:
            self.result.advisory.append(Issue(
                code="RAM_SINGLE_STICK_TWO_SLOT",
                title="1 планка ОЗУ в 2-слотовой плате: установите в слот A2 (или DIMM_B1)",
                detail=(
                    "При одном модуле в 2-слотовой плате установите его в "
                    "рекомендованный слот (часто помечен как DIMM_A2 или DIMM_B1 — "
                    "дальний от процессора). "
                    "Часть плат требует именно этот слот для первоначальной загрузки. "
                    "Проверьте схему в мануале."
                ),
                field="ram/mb"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  34. ECC ПАМЯТЬ НА ПОТРЕБИТЕЛЬСКОЙ ПЛАТЕ
    #  ECC работает только на серверных/рабочих чипсетах
    # ─────────────────────────────────────────────────────────────────────

    def check_ecc_ram_compatibility(self):
        if not self.ram_sticks or not self.mb:
            return

        for stick in self.ram_sticks:
            stick_name  = (stick.get("name")  or "").lower()
            stick_specs = str(stick.get("specs") or {}).lower()

            if "ecc" not in stick_name and "ecc" not in stick_specs:
                continue

            mb_name = (self.mb.get("name") or "").lower()
            # Серверные/рабочие чипсеты где ECC реально работает
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
                        "ECC (Error-Correcting Code) работает только с чипсетами "
                        "серверного / рабочего класса (Intel W790, AMD TRX50, EPYC SP3). "
                        "На потребительских Z/B/H платах ECC-модуль определится "
                        "и будет работать как обычная не-ECC память — "
                        "без исправления одиночных битовых ошибок. "
                        "Переплата за ECC теряет смысл. "
                        "Если ECC критична (медицина, финансы, научные расчёты) — "
                        "выбирайте платформу W790 или AMD PRO/EPYC."
                    ),
                    field="ram/mb"
                ))
            break  # достаточно одного предупреждения

    def _build_summary(self):
        cpu_tdp  = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp  = _int(_g(self.gpu, "gpuTdp"), 0)
        psu_w    = _int(_g(self.psu, "psuWattage"), 0)
        total    = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        rec      = int(total * (1 + PSU_HEADROOM_PCT))
        vram_gb  = _int(_g(self.gpu, "vram"), 0)
        total_ram_gb = sum(_int(_g(s, "ramCapacity"), 0) for s in self.ram_sticks)
        load_pct = round(total / psu_w * 100, 1) if psu_w else 0

        self.result.summary = {
            # ── Оригинальные поля ──────────────────────────────────
            "cpuTdpW":          cpu_tdp,
            "gpuTdpW":          gpu_tdp,
            "systemOverheadW":  SYSTEM_OVERHEAD_W,
            "totalEstimatedW":  total,
            "recommendedPsuW":  rec,
            "selectedPsuW":     psu_w,
            "psuHeadroomPct":   round((psu_w - total) / total * 100, 1) if total else 0,
            "ramSticksCount":   len(self.ram_sticks),
            "criticalCount":    len(self.result.critical),
            "warningCount":     len(self.result.warning),
            "advisoryCount":    len(self.result.advisory),
            # ── Новые поля ─────────────────────────────────────────
            "totalRamGb":       total_ram_gb,   # суммарно установлено ГБ ОЗУ
            "vramGb":           vram_gb,         # VRAM видеокарты
            "psuLoadPct":       load_pct,        # реальная нагрузка на БП (%)
            "hasDiscreteGpu":   bool(self.gpu),  # есть ли дискретная GPU
        }


# ═══════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════

def _detect_amd_gen(cpu_name: str) -> int:
    """Определяет поколение AMD Ryzen по имени (1000–9000 серии)."""
    m = re.search(r"ryzen\s*\d\s+(\d)(\d{3})", cpu_name, re.I)
    if m:
        return int(m.group(1))
    return 0


# ═══════════════════════════════════════════════════════════════════════════
#  ПУБЛИЧНЫЙ API
# ═══════════════════════════════════════════════════════════════════════════

def check_compatibility(components: dict) -> dict:
    """
    Точка входа.
    components = { "cpu": {...}, "mb": {...}, ... }
    Возвращает dict, готовый к сериализации в JSON для Android-клиента.
    """
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