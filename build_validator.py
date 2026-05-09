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
    # AMD AM5
    "AM5": ["X870E", "X870", "X670E", "X670", "B850", "B650E", "B650", "A620"],
    # AMD AM4 (все чипсеты)
    "AM4": ["X570", "B550", "A520", "X470", "B450", "A320", "X370", "B350", "A300"],
    # Intel LGA1700 (12–14 gen)
    "LGA1700": ["Z790", "Z690", "H770", "H670", "B760", "B660", "H610"],
    # Intel LGA1851 (15 gen Arrow Lake)
    "LGA1851": ["Z890", "B860", "H810"],
    # Intel LGA1200 (10–11 gen)
    "LGA1200": ["Z590", "Z490", "H570", "H510", "B560", "B460"],
}

# Чипсеты, требующие BIOS Flashback для новых CPU (AM5 + старый чипсет)
BIOS_FLASHBACK_REQUIRED: dict[str, list[str]] = {
    "AM5": ["A620"],           # A620 может не иметь BIOS для Ryzen 9xxx из коробки
    "AM4": ["A320", "B350"],   # очень старые платы могут не поддерживать Ryzen 5xxx
    "LGA1700": ["H510", "B460"],
}

# Форм-факторы MB: иерархия размеров (больше индекс = больший форм-фактор)
MB_SIZE_RANK: dict[str, int] = {
    "Mini-ITX": 0,
    "Flex-ATX": 1,
    "mATX":     2,
    "ATX":      3,
    "E-ATX":    4,
}

# Корпус: какие форм-факторы он вмещает (минимум → максимум)
CASE_COMPATIBLE_MB: dict[str, list[str]] = {
    "Mini-ITX": ["Mini-ITX"],
    "mATX":     ["Mini-ITX", "mATX"],
    "ATX":      ["Mini-ITX", "mATX", "ATX"],
    "E-ATX":    ["Mini-ITX", "mATX", "ATX", "E-ATX"],
}

# Нормализация разъёмов питания CPU (сколько 12V контактов «подаётся»)
CPU_PIN_AMPERAGE: dict[str, int] = {
    "4 pin":   1,   # самый слабый (старые платы)
    "4+4 pin": 2,   # = 8 pin (эффективно)
    "8 pin":   2,
    "8+4 pin": 3,
    "8+8 pin": 4,   # для разогнанных HEDT
}

# Линии PCIe у процессоров по сокету (упрощённая модель)
PCIE_LANES_BY_SOCKET: dict[str, int] = {
    "AM5":    28,   # Ryzen 7xxx/9xxx
    "AM4":    20,   # Ryzen 5xxx
    "LGA1700": 20,  # Core 12-14 gen
    "LGA1851": 24,  # Core Ultra 200
}

# Тепловыделение «балласта» системы (ОЗУ, накопители, вентиляторы и т.п.)
SYSTEM_OVERHEAD_W = 80

# Рекомендуемый запас по мощности БП (%)
PSU_HEADROOM_PCT = 0.20


# ═══════════════════════════════════════════════════════════════════════════
#  ТИПЫ РЕЗУЛЬТАТОВ
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    code:    str    # машинный код — используется на Android для цвета/иконки
    title:   str    # краткий заголовок
    detail:  str    # подробное объяснение + совет
    field:   str = ""  # имя поля/компонента («cpu», «psu», «gpu»)


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
    """Безопасный get с fallback для None / "---" / 0 / пустой строки."""
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

        # RAM может быть как списком, так и одним объектом
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
        self.check_socket_compatibility()
        self.check_ram_type()
        self.check_ram_slots()
        self.check_ram_cooler_clearance()    # ← физический конфликт RAM/кулер
        self.check_power_deep()              # ← энергоаудит по линиям
        self.check_pcie_lanes()              # ← линии PCIe
        self.check_gpu_physical()            # ← толщина / длина видеокарты
        self.check_cooler_vs_cpu()
        self.check_cooler_vs_case()
        self.check_case_form_factor()
        self.check_psu_form_factor()
        self.check_bios_flashback()          # ← совместимость BIOS
        self.check_ssd_slot_availability()
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
            # кулер может поддерживать несколько сокетов через запятую
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

        # CPU тоже ограничивает тип DDR
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

        # Двухканальный режим
        if len(self.ram_sticks) == 1 and mb_slots >= 2:
            self.result.advisory.append(Issue(
                code="SINGLE_CHANNEL",
                title="Включён одноканальный режим памяти",
                detail=f"Один модуль ОЗУ в двухслотовой плате даёт одноканальный режим. "
                       f"Производительность на 10–30% ниже двухканального. "
                       f"Рекомендуем добавить второй идентичный модуль.",
                field="ram"
            ))

        # Частота ОЗУ vs максимальная частота платы
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
        """
        Башенный кулер нависает над первым слотом ОЗУ.
        Если высота планки > 35 мм — возможен конфликт.
        """
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
        rec_psu   = int(total_tdp * (1 + PSU_HEADROOM_PCT))  # +20% запас

        # ── 5.0 Нет БП — но GPU есть ─────────────────────────────────────
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

        # ── 5.1 Общая мощность ───────────────────────────────────────────
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

        # Рекомендация GPU производителя
        if psu_w and gpu_req and psu_w < gpu_req:
            self.result.critical.append(Issue(
                code="PSU_BELOW_GPU_REQUIREMENT",
                title="БП ниже рекомендации производителя видеокарты",
                detail=f"Производитель GPU требует минимум {gpu_req} Вт, "
                       f"выбранный БП: {psu_w} Вт. "
                       f"Возможны артефакты, вылеты или несохранённые данные.",
                field="psu"
            ))

        # ── 5.2 Разъём питания CPU (линии 12V) ───────────────────────────
        mb_cpu_pin  = _g(self.mb,  "cpuPowerPin", "---")
        psu_cpu_pin = _g(self.psu, "cpuPowerPin", "---")

        mb_amps  = CPU_PIN_AMPERAGE.get(mb_cpu_pin, 0)
        psu_amps = CPU_PIN_AMPERAGE.get(psu_cpu_pin, 0)

        if mb_amps and psu_amps:
            if psu_amps < mb_amps:
                # БП даёт меньше пинов чем требует плата
                self.result.critical.append(Issue(
                    code="CPU_POWER_PIN_CRITICAL",
                    title="БП не имеет нужного разъёма питания CPU",
                    detail=f"Материнская плата требует {mb_cpu_pin}, "
                           f"а БП предоставляет только {psu_cpu_pin}. "
                           f"Запуск невозможен или разгон будет заблокирован.",
                    field="psu/mb"
                ))
            elif psu_amps == mb_amps and mb_amps >= 3:
                # 8+4 / 8+8 — если БП в притык, предупреждаем о разгоне
                self.result.advisory.append(Issue(
                    code="CPU_POWER_PIN_OC_LIMIT",
                    title="Разъём питания CPU ограничивает разгон",
                    detail=f"Плата имеет {mb_cpu_pin} (полный разгон), "
                           f"БП подаёт {psu_cpu_pin}. "
                           f"Разгон через второй разъём может быть ограничен.",
                    field="psu"
                ))

        # ── 5.3 Разъём питания GPU (12VHPWR / обычные 8-pin) ─────────────
        gpu_pin = _g(self.gpu, "gpuPowerPin", "")
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
        """
        Считаем расход линий PCIe:
          GPU    → 16 линий
          NVMe   → 4 линии каждый
          Если итог > лимита CPU → предупреждение о снижении скорости.
        """
        cpu_socket = _g(self.cpu, "socket")
        if not cpu_socket:
            return

        total_lanes = PCIE_LANES_BY_SOCKET.get(cpu_socket, 20)
        used_lanes  = 0
        issues_detail = []

        # GPU — x16 слот
        if self.gpu:
            gpu_pci = _g(self.gpu, "gpuPciVersion", "4.0")
            used_lanes += 16
            issues_detail.append(f"GPU: x16 ({gpu_pci})")

        # SSD (NVMe)
        if self.ssd:
            ssd_iface = _g(self.ssd, "ssdInterface", "")
            if ssd_iface == "NVMe":
                used_lanes += 4
                issues_detail.append("NVMe SSD #1: x4")

        # Если MB имеет M.2 слоты сверх одного — считаем доп. NVMe
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

        # ── PCIe версия GPU vs MB ─────────────────────────────────────────
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

        gpu_len      = _int(_g(self.gpu, "gpuLength"), 0)
        max_gpu_len  = _int(_g(self.case, "maxGpuLength"), 0)
        gpu_slots    = _int(_g(self.gpu, "gpuSlots"), 0)

        # Длина GPU vs корпус
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

        # Толщина (слоты расширения) GPU
        # Стандарт: одна видеокарта занимает 2 или 3 слота расширения
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
        cpu_tdp    = _int(_g(self.cpu, "tdp"), 0)
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
        case_max = _int(_g(self.case, "maxCpuCoolerHeight"), 0)

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
        mb_ff   = _g(self.mb,   "formFactor")
        case_ff = _g(self.case, "formFactor")
        supported = _g(self.case, "supportedMbFormats", [])

        if not mb_ff or not case_ff:
            return

        # Если есть явный список supportedMbFormats
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

        # Фоллбэк: иерархическая проверка
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

        # Mini-ITX корпуса обычно требуют SFX
        if case_ff == "Mini-ITX" and psu_ff == "ATX":
            self.result.critical.append(Issue(
                code="PSU_FF_MISMATCH",
                title="ATX блок питания не подходит к Mini-ITX корпусу",
                detail="Корпус Mini-ITX обычно требует БП формата SFX или SFX-L. "
                       "ATX БП физически не вставить — другой размер и крепёж.",
                field="psu/case"
            ))

        # Проверка длины БП
        if psu_len and case_psu_max and psu_len > case_psu_max:
            self.result.critical.append(Issue(
                code="PSU_TOO_LONG",
                title="Блок питания слишком длинный для корпуса",
                detail=f"Длина БП: {psu_len} мм, максимум для корпуса: {case_psu_max} мм. "
                       f"Возможен конфликт с кабелями или накопителями.",
                field="psu/case"
            ))

    # ─────────────────────────────────────────────────────────────────────
    #  12. BIOS FLASHBACK (совместимость чипсета и поколения CPU)
    # ─────────────────────────────────────────────────────────────────────

    def check_bios_flashback(self):
        """
        Если чипсет платы — из «старшего» поколения для данного сокета,
        BIOS может не поддерживать новый CPU без предварительного обновления.
        """
        if not self.cpu or not self.mb:
            return

        cpu_socket = _g(self.cpu, "socket", "")
        chipset    = _g(self.mb,  "chipset", "").upper()  # если есть в specs

        # Пытаемся определить чипсет из имени MB
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

        # Дополнительно: AM4 + Ryzen 5000 на A320/B350 — критично
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

        ssd_iface = _g(self.ssd, "ssdInterface", "")
        mb_m2_cnt = _int(_g(self.mb, "m2Slots"), 0)
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

    # ─────────────────────────────────────────────────────────────────────
    #  СВОДКА
    # ─────────────────────────────────────────────────────────────────────

    def _build_summary(self):
        cpu_tdp  = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp  = _int(_g(self.gpu, "gpuTdp"), 0)
        psu_w    = _int(_g(self.psu, "psuWattage"), 0)
        total    = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        rec      = int(total * (1 + PSU_HEADROOM_PCT))

        self.result.summary = {
            "cpuTdpW":           cpu_tdp,
            "gpuTdpW":           gpu_tdp,
            "systemOverheadW":   SYSTEM_OVERHEAD_W,
            "totalEstimatedW":   total,
            "recommendedPsuW":   rec,
            "selectedPsuW":      psu_w,
            "psuHeadroomPct":    round((psu_w - total) / total * 100, 1) if total else 0,
            "ramSticksCount":    len(self.ram_sticks),
            "criticalCount":     len(self.result.critical),
            "warningCount":      len(self.result.warning),
            "advisoryCount":     len(self.result.advisory),
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
    components = {
        "cpu": {...},
        "mb":  {...},
        ...
    }
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
            "advisory": [{"code": "VALIDATOR_ERROR",
                          "title": "Ошибка валидатора",
                          "detail": str(e),
                          "field":  ""}],
            "summary":  {},
        }