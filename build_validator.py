"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

"""
build_validator.py
Аудит совместимости сборки ПК.
"""

import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

log = logging.getLogger(__name__)



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

IGPU_AMD_PATTERN    = r'ryzen.+\d{4,5}g\b'
IGPU_INTEL_EXCLUDE  = r'\d{4,6}[kf]*f\b'
IGPU_AMD_AM5_SOCKET = "AM5"

IGPU_PRESENT_MARKERS: set[str] = {
    "есть", "да", "yes", "true", "1",
    "amd radeon graphics", "radeon graphics",
    "intel uhd graphics", "intel iris xe",
    "intel hd graphics",
}
IGPU_ABSENT_MARKERS: set[str] = {
    "нет", "no", "false", "0", "отсутствует", "none", "-", "---",
}

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

NO_POWER_MARKERS = {"без питания", "no power", "нет", "none", "---", ""}

MULTI_PCIE_SLOT_CHIPSETS = {
    "X870E", "X670E", "X870", "X670",
    "Z890", "Z790", "Z690",
    "X570", "X470", "X370",
    "Z590", "Z490", "Z390", "Z370",
}

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


def _lookup_text(value: object) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = re.sub(r"\bpci\s*-\s*e\b", "pcie", text)
    text = re.sub(r"\bpci\s+e\b", "pcie", text)
    return re.sub(r"\s+", " ", text)


def _iter_component_specs(component: dict | None):
    if not component:
        return
    for key in ("specs", "specsCitilink", "specsRegard", "specsDNS"):
        specs = component.get(key)
        if isinstance(specs, dict):
            yield from specs.items()


def _derive_gpu_pin_from_specs(component: dict | None) -> str:
    for raw_key, raw_value in _iter_component_specs(component) or ():
        key = _lookup_text(raw_key)
        value = str(raw_value or "").strip()
        value_lookup = _lookup_text(value)

        count = _int(value, 0)
        if count > 0 and all(part in key for part in ("разъемов", "6+2", "pci")):
            return f"{count}x(6+2) pin"
        if count > 0 and all(part in key for part in ("разъемов", "8", "pin", "pci")):
            return f"{count}x8 pin"
        if count > 0 and all(part in key for part in ("разъемов", "6", "pin", "pci")):
            return f"{count}x6 pin"

        if "питание видеокарты" in key and value_lookup not in NO_POWER_MARKERS:
            return value
        if key == "разъемы" and "питание видеокарты" in value_lookup:
            return value

    return ""

def _derive_psu_length_from_specs(component: dict | None) -> int:
    for raw_key, raw_value in _iter_component_specs(component) or ():
        key = _lookup_text(raw_key)
        value = str(raw_value or "")

        if "упаков" in key or "кабел" in key or "линий" in key:
            continue

        numbers = re.findall(r"\d+(?:[.,]\d+)?", value)
        if not numbers:
            continue

        if ("размеры" in key or "габариты" in key) and len(numbers) >= 3:
            return int(float(numbers[2].replace(",", ".")))

        if "глубина" in key or ("длина" in key and "блока питания" in key):
            return int(float(numbers[0].replace(",", ".")))

    return 0

def _normalize_gpu_pin(pin_str: str) -> str:
    if not pin_str:
        return ""

    s = pin_str.lower().strip()

    for noise in (
        "питание видеокарты", "питание", "видеокарты",
        "рекомендовано", "разъём", "разъем", "коннектор", "connector",
    ):
        s = s.replace(noise, " ")
    s = re.sub(r'\s+', ' ', s).strip()

    if s in NO_POWER_MARKERS or not s:
        return s

    if "12vhpwr" in s or "16-pin" in s or re.search(r'16\s*pin', s):
        return "12vhpwr (16 pin)"

    m = re.match(r'(\d+)\s*[xх×*]\s*\((\d+)\+(\d+)\)', s)
    if m:
        count     = int(m.group(1))
        total_per = int(m.group(2)) + int(m.group(3))
        return "+".join([str(total_per)] * count) + " pin"

    m = re.match(r'(\d+)\s*[xх×*]\s*\((\d+)\)', s)
    if m:
        count = int(m.group(1))
        a     = int(m.group(2))
        return "+".join([str(a)] * count) + " pin"

    m = re.match(r'(\d+)\s*[xх×*]\s*(\d+)', s)
    if m:
        count = int(m.group(1))
        a     = int(m.group(2))
        return "+".join([str(a)] * count) + " pin"

    m = re.match(r'^(\d+)\+(\d+)$', s.replace(" pin", "").replace("pin", "").strip())
    if m:
        total = int(m.group(1)) + int(m.group(2))
        return f"{total} pin"

    m = re.match(r'^(\d+)$', s.replace(" pin", "").replace("pin", "").strip())
    if m:
        return f"{m.group(1)} pin"

    result = s.replace("pin", "").strip()
    return result if result else s


def _gpu_pin_units(pin_str: str) -> int:
    if not pin_str:
        return 0

    normalized = _normalize_gpu_pin(pin_str)
    low = normalized.lower()

    if not low or low in NO_POWER_MARKERS:
        return 0

    if "12vhpwr" in low:
        return 8

    numbers = re.findall(r'\d+', low.replace("pin", ""))
    if numbers:
        return len(numbers)

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


def _get_mb_pcie_x16_slots(mb: dict | None) -> int:
    if not mb:
        return 1

    direct = _int(_g(mb, "pcieX16Slots"), 0)
    if direct > 0:
        return direct

    PCIE_KEY_FRAGMENTS = (
        "слоты pci",
        "слот pci",
        "pcie слот",
        "слоты расширения",
        "expansion slot",
        "pci express slot",
        "разъемы pci",
        "слоты",
    )

    max_found = 0

    for raw_key, raw_value in _iter_component_specs(mb):
        key_norm   = _lookup_text(raw_key)
        value_norm = _lookup_text(str(raw_value or ""))

        key_relevant = any(frag in key_norm for frag in PCIE_KEY_FRAGMENTS)
        if not key_relevant:
            continue

        matches1 = re.findall(r'x16\s*[xх×]\s*(\d+)', value_norm)
        count1 = sum(int(m) for m in matches1 if m)

        matches2 = re.findall(r'(\d+)\s*[xх×]\s*(?:pcie?|pci-e)[^\n,]*?x16', value_norm)
        count2 = sum(int(m) for m in matches2 if m)

        total = count1 + count2

        if total > max_found:
            max_found = total

        if max_found == 0 and "x16" in value_norm:
            mentions = len(re.findall(r'\bx16\b', value_norm))
            if mentions > max_found:
                max_found = mentions

    if max_found > 0:
        log.debug("_get_mb_pcie_x16_slots: найдено %d слотов x16 из specs", max_found)
        return max_found

    chipset = _g(mb, "chipset", "").upper()
    if chipset in MULTI_PCIE_SLOT_CHIPSETS:
        log.debug(
            "_get_mb_pcie_x16_slots: chipset %s → assume 2 слота x16",
            chipset,
        )
        return 2
    return 1


def _count_ram_modules(stick: dict | None) -> int:
    if not stick:
        return 1

    for raw_key, raw_value in _iter_component_specs(stick):
        key = _lookup_text(raw_key)
        if any(kw in key for kw in (
            "количество модул",
            "кол-во модул",
            "modules in kit",
            "number of modules",
            "модулей в комплект",
        )):
            n = _int(raw_value, 0)
            if n > 0:
                return n

    name = _lookup_text(stick.get("name") or "")

    m = re.search(r'\b(\d+)\s*[xх×*]\s*\d+\s*(?:gb|гб)\b', name)
    if m:
        n = int(m.group(1))
        if 1 < n <= 8:
            return n

    m = re.search(r'\b\d+\s*(?:gb|гб)\s*[xх×*]\s*(\d+)\b', name)
    if m:
        n = int(m.group(1))
        if 1 < n <= 8:
            return n

    m = re.search(r'\b(\d+)\s*(?:шт|pcs|pieces|pack)\b', name)
    if m:
        n = int(m.group(1))
        if 1 < n <= 8:
            return n

    return 1


def _mb_supports_nvme(mb: dict | None) -> bool:
    if not mb:
        return False

    m2_types = _g(mb, "m2Types", [])
    if m2_types and "NVMe" in m2_types:
        return True

    NVME_VALUE_MARKERS = (
        "nvme",
        "nvm express",
        "pci-e",
        "pcie",
        "pci express",
    )

    M2_KEY_FRAGMENTS = (
        "тип слот",
        "слот m.2",
        "m.2",
        "поддержка nvme",
        "поддерживаемые технологии",
        "nvme",
        "nvm",
        "интерфейс m.2",
        "interface m.2",
        "supported technologies",
        "накопител",
    )

    for raw_key, raw_value in _iter_component_specs(mb):
        key_norm   = _lookup_text(raw_key)
        value_norm = _lookup_text(str(raw_value or ""))

        key_relevant   = any(frag in key_norm   for frag in M2_KEY_FRAGMENTS)
        value_has_nvme = any(marker in value_norm for marker in NVME_VALUE_MARKERS)

        if key_relevant and value_has_nvme:
            log.debug(
                "_mb_supports_nvme: '%s'='%s' → NVMe поддерживается",
                raw_key, raw_value,
            )
            return True

        if "nvme" in key_norm and value_norm not in IGPU_ABSENT_MARKERS:
            log.debug(
                "_mb_supports_nvme: ключ '%s' содержит nvme, значение '%s' → True",
                raw_key, raw_value,
            )
            return True

    chipset = _g(mb, "chipset", "").upper()
    if chipset and chipset in M2_SLOTS_BY_CHIPSET:
        slots_count = M2_SLOTS_BY_CHIPSET.get(chipset, 0)
        if slots_count > 0:
            log.debug(
                "_mb_supports_nvme: chipset %s (%d M.2 слотов) → NVMe assumed",
                chipset, slots_count,
            )
            return True

    mb_name = _lookup_text(mb.get("name") or "")
    if any(x in mb_name for x in ("nvme", "m.2", "pcie")):
        return True

    return False

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

        self.check_ram_mixing()
        self.check_multi_gpu()
        self.check_multi_ssd()

        self._build_summary()
        return self.result


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

    def check_ram_slots(self):
        if not self.ram_sticks:
            return

        mb_slots = _get_mb_ram_slots(self.mb)
        n = len(self.ram_sticks)
        total_physical = sum(_count_ram_modules(s) for s in self.ram_sticks)

        if mb_slots > 0 and total_physical > mb_slots:
            self.result.critical.append(Issue(
                code="RAM_SLOTS_OVERFLOW",
                title=f"Не хватает слотов ОЗУ: {total_physical} модулей, на плате {mb_slots}",
                detail=(
                    f"Суммарно в сборке {total_physical} физических модулей памяти "
                    f"({n} {'позиция' if n == 1 else 'позиции' if n < 5 else 'позиций'} в сборке), "
                    f"но материнская плата имеет только {mb_slots} слота. "
                    f"Уберите лишние или выберите плату с большим числом слотов."
                ),
                field="mb/ram"
            ))

        if total_physical == 1 and mb_slots >= 2:
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
        elif total_physical == 3 and mb_slots == 4:
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
                    title=f"Модуль #{i + 1} будет понижен по частоте",
                    detail=(
                        f"Модуль поддерживает {stick_freq} МГц, "
                        f"но плата ограничена {mb_max_freq} МГц."
                    ),
                    field="ram"
                ))

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

    def check_power_deep(self):
        cpu_tdp = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp = sum(_int(_g(g, "gpuTdp"), 0) for g in self.gpus)
        psu_w   = _int(_g(self.psu, "psuWattage"), 0)
        gpu_req = max((_int(_g(g, "gpuReqPsu"), 0) for g in self.gpus), default=0)

        total_tdp = cpu_tdp + gpu_tdp + SYSTEM_OVERHEAD_W
        rec_psu   = int(total_tdp * (1 + PSU_HEADROOM_PCT))

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

    def check_gpu_physical(self):
        if not self.gpus or not self.case:
            return

        max_gpu_len = _int(_g(self.case, "maxGpuLength"), 0)
        triple_slot_reported = False

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

            if gpu_slots == 3 and not triple_slot_reported:
                triple_slot_reported = True
                triple_count = sum(
                    1 for g in self.gpus if _int(_g(g, "gpuSlots"), 0) == 3
                )
                slots_needed = triple_count * 3
                self.result.advisory.append(Issue(
                    code="GPU_TRIPLE_SLOT",
                    title=(
                        f"Трёхслотовая GPU — проверьте свободные слоты в корпусе"
                        if triple_count == 1 else
                        f"{triple_count} трёхслотовые GPU — проверьте свободные слоты"
                    ),
                    detail=(
                        f"{triple_count} × 3-слотовая GPU занимает суммарно "
                        f"{slots_needed} слота расширения. "
                        f"Убедитесь в наличии достаточного числа свободных заглушек в корпусе."
                    ),
                    field="gpu/case"
                ))

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

    def check_psu_form_factor(self):
        psu_ff  = _g(self.psu,  "psuFormFactor")
        case_ff = _g(self.case, "formFactor")

        if not psu_ff or not case_ff:
            return

        psu_len         = _int(_g(self.psu,  "psuLength"), 0)
        case_psu_max    = _int(_g(self.case, "maxPsuLength"), 0)
        derived_psu_len = _derive_psu_length_from_specs(self.psu)
        if derived_psu_len and (
            not psu_len
            or psu_len > 250
            or (case_psu_max and psu_len > case_psu_max and derived_psu_len <= case_psu_max)
        ):
            psu_len = derived_psu_len

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

    def check_ssd_slot_availability(self):
        if not self.ssds or not self.mb:
            return

        mb_m2_cnt        = _get_mb_m2_slots(self.mb)
        mb_nvme_supported = _mb_supports_nvme(self.mb)

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

            if ssd_iface == "NVMe" and mb_m2_cnt > 0 and not mb_nvme_supported:
                self.result.critical.append(Issue(
                    code="M2_NVME_UNSUPPORTED",
                    title="Слот M.2 на плате не поддерживает NVMe",
                    detail=(
                        "Плата имеет M.2 слот только для SATA SSD. "
                        "NVMe SSD в нём не заработает."
                    ),
                    field="ssd/mb"
                ))

    def check_igpu(self):
        if self.gpus:
            return
        if not self.cpu:
            return

        cpu_name   = (self.cpu.get("name") or "").strip()
        cpu_name_l = cpu_name.lower()
        cpu_socket = _g(self.cpu, "socket", "")

        igpu_from_specs = self._detect_igpu_from_specs()
        if igpu_from_specs is True:
            self._add_igpu_advisory(cpu_name)
            return
        if igpu_from_specs is False:
            self._add_no_igpu_critical(cpu_name)
            return

        has_igpu_field = self.cpu.get("hasIgpu")
        if has_igpu_field is True:
            self._add_igpu_advisory(cpu_name)
            return
        if has_igpu_field is False:
            self._add_no_igpu_critical(cpu_name)
            return

        if cpu_socket == IGPU_AMD_AM5_SOCKET:
            is_ryzen_am5 = any(
                x in cpu_name_l
                for x in ("ryzen", "ryzen 3", "ryzen 5", "ryzen 7", "ryzen 9")
            )
            if is_ryzen_am5:
                log.debug(
                    "check_igpu: AM5 CPU '%s' → iGPU есть (Radeon Graphics 2CU)",
                    cpu_name
                )
                self._add_igpu_advisory(cpu_name)
                return

        has_igpu = self._detect_igpu_by_name(cpu_name_l)

        if has_igpu is None:
            log.warning(
                "check_igpu: не удалось определить iGPU для '%s' (socket=%s)",
                cpu_name, cpu_socket
            )
            self.result.advisory.append(Issue(
                code="IGPU_UNKNOWN",
                title="Не удалось определить наличие встроенной графики",
                detail=(
                    f"Для CPU «{cpu_name}» не удалось автоматически определить "
                    f"наличие iGPU. Если дискретной GPU нет — проверьте "
                    f"спецификацию процессора вручную перед сборкой."
                ),
                field="cpu"
            ))
            return

        if has_igpu:
            self._add_igpu_advisory(cpu_name)
        else:
            self._add_no_igpu_critical(cpu_name)

    def _detect_igpu_from_specs(self) -> bool | None:
        IGPU_SPEC_KEY_FRAGMENTS = (
            "интегрированное графическое ядро",
            "встроенная графика",
            "графическое ядро",
            "видеопроцессор",
            "графический процессор",
            "видеоядро",
            "integrated graphics",
            "integrated gpu",
            "igpu",
            "graphics",
        )

        for raw_key, raw_value in _iter_component_specs(self.cpu):
            key_norm   = _lookup_text(raw_key)
            value_norm = _lookup_text(str(raw_value or ""))

            key_is_igpu_related = any(
                fragment in key_norm
                for fragment in IGPU_SPEC_KEY_FRAGMENTS
            )
            if not key_is_igpu_related:
                continue

            if value_norm in IGPU_PRESENT_MARKERS:
                return True

            if value_norm in IGPU_ABSENT_MARKERS:
                return False

            if any(
                brand in value_norm
                for brand in (
                    "radeon", "amd radeon",
                    "intel uhd", "intel iris", "intel hd",
                    "uhd graphics", "iris xe",
                )
            ):
                return True

        return None

    def _detect_igpu_by_name(self, cpu_name_l: str) -> bool | None:
        is_intel = any(
            x in cpu_name_l
            for x in ("intel", "core i", "core ultra", "pentium", "celeron", "xeon e")
        )
        if is_intel:
            has_f_suffix = bool(re.search(IGPU_INTEL_EXCLUDE, cpu_name_l, re.I))
            return not has_f_suffix

        is_amd = any(x in cpu_name_l for x in ("ryzen", "amd athlon", "athlon"))
        if is_amd:
            if re.search(IGPU_AMD_PATTERN, cpu_name_l, re.I):
                return True
            m = re.search(r'ryzen\s*\d+\s+(\d{4})', cpu_name_l)
            if m:
                model_num = int(m.group(1))
                return model_num >= 7000

        return None

    def _add_igpu_advisory(self, cpu_name: str) -> None:
        self.result.advisory.append(Issue(
            code="IGPU_ONLY_MODE",
            title="Работа на встроенной графике CPU — производительность ограничена",
            detail=(
                f"Дискретная видеокарта не выбрана. "
                f"CPU «{cpu_name}» имеет встроенную графику. "
                f"Система будет использовать iGPU. "
                f"Убедитесь, что в BIOS включён видеовыход: "
                f"BIOS → Advanced → Integrated Graphics → Enabled / Auto."
            ),
            field="cpu"
        ))

    def _add_no_igpu_critical(self, cpu_name: str) -> None:
        self.result.critical.append(Issue(
            code="NO_GPU_NO_IGPU",
            title="Система не выдаст изображение — нет GPU и нет iGPU в CPU",
            detail=(
                f"В сборке нет дискретной видеокарты, а CPU «{cpu_name}» "
                f"не имеет встроенной графики. "
                f"Добавьте дискретную GPU или замените CPU на модель с iGPU "
                f"(Intel без суффикса F, AMD Ryzen G-серии, "
                f"или любой Ryzen на сокете AM5)."
            ),
            field="gpu/cpu"
        ))

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
        n = len(self.ram_sticks)

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

    def check_psu_efficiency_zone(self):
        psu_w   = _int(_g(self.psu, "psuWattage"), 0)
        cpu_tdp = _int(_g(self.cpu, "tdp"), 0)
        gpu_tdp = sum(_int(_g(g, "gpuTdp"), 0) for g in self.gpus)

        if self.gpus and gpu_tdp == 0:
            gpu_req = max((_int(_g(g, "gpuReqPsu"), 0) for g in self.gpus), default=0)
            if gpu_req > 0:
                gpu_tdp = int(gpu_req * 0.65)
            else:
                return

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

    def check_ram_population_order(self):
        if not self.ram_sticks or not self.mb:
            return

        mb_slots       = _get_mb_ram_slots(self.mb)
        total_physical = sum(_count_ram_modules(s) for s in self.ram_sticks)

        if mb_slots == 4 and total_physical == 2:
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
        elif mb_slots == 2 and total_physical == 1:
            self.result.advisory.append(Issue(
                code="RAM_SINGLE_STICK_TWO_SLOT",
                title="1 планка ОЗУ в 2-слотовой плате: установите в рекомендованный слот",
                detail=(
                    "Часть плат требует планку в слот DIMM_A2 или DIMM_B1 "
                    "(дальний от процессора) для первоначальной загрузки."
                ),
                field="ram/mb"
            ))

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

    def check_multi_gpu(self):
        if len(self.gpus) < 2:
            return

        mb_pcie_slots = _get_mb_pcie_x16_slots(self.mb)

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
                "rx 7",   "rx7",   "rx 6",   "rx6",
                "rtx 50", "rtx50",
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
                    title=f"Недостаточно M.2 слотов: выбрано {nvme_count}, на плате {mb_m2_slots}",
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
                    title=f"Недостаточно SATA портов: выбрано {sata_count}, на плате {mb_sata_ports}",
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
