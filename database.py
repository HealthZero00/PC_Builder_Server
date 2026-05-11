"""
database.py — работа с PostgreSQL для PC Builder
Схема: components + component_prices + component_specs + component_compat
Поддержка двойной записи: Windows (Local) + VPS (Remote)
"""
import os
import json
import logging
import pg8000.native
from typing import Optional, List
from dotenv import load_dotenv

log = logging.getLogger(__name__)
load_dotenv()

DB_CONFIG = [
    {
        "host":     os.getenv("DB_LOCAL_HOST"),
        "database": os.getenv("DB_LOCAL_NAME"),
        "user":     os.getenv("DB_LOCAL_USER"),
        "password": os.getenv("DB_LOCAL_PASS"),
    },
    {
        "host":     os.getenv("DB_REMOTE_HOST"),
        "database": os.getenv("DB_REMOTE_NAME"),
        "user":     os.getenv("DB_REMOTE_USER"),
        "password": os.getenv("DB_REMOTE_PASS"),
    },
]


def get_connections() -> List[pg8000.native.Connection]:
    active = []
    for cfg in DB_CONFIG:
        if not cfg["host"]:
            continue
        try:
            active.append(pg8000.native.Connection(**cfg))
        except Exception as e:
            log.error("Ошибка подключения к БД %s: %s", cfg["host"], e)
    return active


# ══════════════════════════════════════════════════════════════
#  СОХРАНЕНИЕ (двойная запись)
# ══════════════════════════════════════════════════════════════

def save_to_db(category_name: str, items_list: list, store: str = "citilink") -> int:
    conns = get_connections()
    if not conns:
        log.error("save_to_db: Нет доступных подключений!")
        return 0

    saved_count = 0
    try:
        for item in items_list:
            name = (item.get("name") or "").strip()
            if not name:
                continue

            raw_price  = item.get("priceCitilink") or item.get("price") or "0"
            digits     = "".join(filter(str.isdigit, str(raw_price)))
            price_rub  = int(digits) if digits else 0
            specs_json = json.dumps(item.get("specs") or {}, ensure_ascii=False)
            compat     = _extract_compat_fields(item)

            for conn in conns:
                try:
                    # 1. components
                    res = conn.run(
                        """
                        INSERT INTO components (name, category, image_url)
                        VALUES (:name, :cat, :img)
                        ON CONFLICT (name) DO UPDATE
                            SET category  = EXCLUDED.category,
                                image_url = EXCLUDED.image_url,
                                updated_at = now()
                        RETURNING id
                        """,
                        name=name, cat=category_name, img=item.get("imageUrl", "")
                    )
                    comp_id = res[0][0]

                    # 2. component_prices
                    conn.run(
                        """
                        INSERT INTO component_prices (component_id, store, price_rub, product_url)
                        VALUES (:cid, :store, :price, :url)
                        ON CONFLICT (component_id, store) DO UPDATE
                            SET price_rub   = EXCLUDED.price_rub,
                                product_url = EXCLUDED.product_url,
                                updated_at  = now()
                        """,
                        cid=comp_id, store=store, price=price_rub, url=item.get("productUrl", "")
                    )

                    # 3. component_specs
                    conn.run(
                        """
                        INSERT INTO component_specs (component_id, store, specs)
                        VALUES (:cid, :store, :specs)
                        ON CONFLICT (component_id, store) DO UPDATE
                            SET specs = EXCLUDED.specs, updated_at = now()
                        """,
                        cid=comp_id, store=store, specs=specs_json
                    )

                    # 4. component_compat — все поля совместимости
                    conn.run(
                        """
                        INSERT INTO component_compat (
                            component_id, socket, chipset, ram_type, ram_slots,
                            ram_max_freq_mhz, ram_height_mm, ram_capacity_gb, tdp_w,
                            cpu_power_pin, max_tdp_w, cooler_height_mm, psu_wattage_w,
                            psu_form_factor, psu_length_mm, psu_efficiency, gpu_power_pin,
                            form_factor, pci_version, m2_slots, m2_types, gpu_chipset,
                            vram_gb, gpu_length_mm, gpu_height_mm, gpu_slots, gpu_tdp_w,
                            gpu_req_psu_w, gpu_pci_version, max_gpu_length_mm,
                            max_cpu_cooler_height_mm, max_psu_length_mm, supported_mb_formats,
                            ssd_interface, ssd_form_factor, ssd_capacity_gb
                        ) VALUES (
                            :cid, :socket, :chipset, :ram_type, :ram_slots,
                            :ram_max_freq, :ram_height, :ram_cap, :tdp,
                            :cpu_pin, :max_tdp, :cooler_h, :psu_w,
                            :psu_ff, :psu_len, :psu_eff, :gpu_pin,
                            :ff, :pci_ver, :m2_slots, :m2_types, :gpu_chip,
                            :vram, :gpu_len, :gpu_h, :gpu_slots, :gpu_tdp,
                            :gpu_req, :gpu_pci, :max_gpu,
                            :max_cool, :max_psu, :mb_formats,
                            :ssd_iface, :ssd_ff, :ssd_gb
                        )
                        ON CONFLICT (component_id) DO UPDATE SET
                            socket    = EXCLUDED.socket,
                            chipset   = EXCLUDED.chipset,
                            ram_type  = EXCLUDED.ram_type,
                            ram_slots = EXCLUDED.ram_slots,
                            tdp_w     = EXCLUDED.tdp_w,
                            gpu_tdp_w = EXCLUDED.gpu_tdp_w,
                            vram_gb   = EXCLUDED.vram_gb,
                            gpu_length_mm = EXCLUDED.gpu_length_mm,
                            max_gpu_length_mm = EXCLUDED.max_gpu_length_mm,
                            max_cpu_cooler_height_mm = EXCLUDED.max_cpu_cooler_height_mm,
                            updated_at = now()
                        """,
                        cid=comp_id,
                        socket=_nn(compat.get("socket")),
                        chipset=_nn(compat.get("chipset")),
                        ram_type=_nn(compat.get("ramType")),
                        ram_slots=_ni(compat.get("ramSlots")),
                        ram_max_freq=_ni(compat.get("ramMaxFreq")),
                        ram_height=_ni(compat.get("ramHeight")),
                        ram_cap=_ni(compat.get("ramCapacity")),
                        tdp=_ni(compat.get("tdp")),
                        cpu_pin=_nn(compat.get("cpuPowerPin")),
                        max_tdp=_ni(compat.get("maxTdp")),
                        cooler_h=_ni(compat.get("coolerHeight")),
                        psu_w=_ni(compat.get("psuWattage")),
                        psu_ff=_nn(compat.get("psuFormFactor")),
                        psu_len=_ni(compat.get("psuLength")),
                        psu_eff=_nn(compat.get("psuEfficiency")),
                        gpu_pin=_nn(compat.get("gpuPowerPin")),
                        ff=_nn(compat.get("formFactor")),
                        pci_ver=_nn(compat.get("pciVersion")),
                        m2_slots=_ni(compat.get("m2Slots")),
                        m2_types=_na(compat.get("m2Types")),
                        gpu_chip=_nn(compat.get("gpuChipset")),
                        vram=_ni(compat.get("vram")),
                        gpu_len=_ni(compat.get("gpuLength")),
                        gpu_h=_ni(compat.get("gpuHeight")),
                        gpu_slots=_ni(compat.get("gpuSlots")),
                        gpu_tdp=_ni(compat.get("gpuTdp")),
                        gpu_req=_ni(compat.get("gpuReqPsu")),
                        gpu_pci=_nn(compat.get("gpuPciVersion")),
                        max_gpu=_ni(compat.get("maxGpuLength")),
                        max_cool=_ni(compat.get("maxCpuCoolerHeight")),
                        max_psu=_ni(compat.get("maxPsuLength")),
                        mb_formats=_na(compat.get("supportedMbFormats")),
                        ssd_iface=_nn(compat.get("ssdInterface")),
                        ssd_ff=_nn(compat.get("ssdFormFactor")),
                        ssd_gb=_ni(compat.get("ssdCapacityGb")),
                    )
                except Exception as e:
                    log.error("Ошибка записи '%s' → %s: %s", name, conn.host, e)

            saved_count += 1

        log.info("[БД] %s: сохранено %d товаров", category_name, saved_count)

    finally:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

    return saved_count


# ══════════════════════════════════════════════════════════════
#  ЗАГРУЗКА В КЭШ FastAPI
#  ИСПРАВЛЕНО: возвращает specs + все поля совместимости,
#  чтобы Android получал полные данные и валидатор работал.
# ══════════════════════════════════════════════════════════════

def load_all_from_db() -> dict:
    """
    Загружает все компоненты из БД в формате, идентичном тому,
    что возвращал бы парсер напрямую.
    Поле 'specs' (характеристики) теперь включено.
    """
    conns = get_connections()
    if not conns:
        log.error("load_all_from_db: Нет подключений к БД")
        return {}

    conn = conns[0]   # локальная БД
    try:
        rows = conn.run("""
            SELECT
                c.id,
                c.name,
                c.category,
                c.image_url,

                -- цена и ссылка
                cp.price_rub,
                cp.product_url,

                -- характеристики (JSON из component_specs)
                cs.specs,

                -- поля совместимости
                cc.socket,
                cc.chipset,
                cc.ram_type,
                cc.ram_slots,
                cc.ram_max_freq_mhz,
                cc.ram_height_mm,
                cc.ram_capacity_gb,
                cc.tdp_w,
                cc.cpu_power_pin,
                cc.max_tdp_w,
                cc.cooler_height_mm,
                cc.psu_wattage_w,
                cc.psu_form_factor,
                cc.psu_length_mm,
                cc.gpu_power_pin,
                cc.form_factor,
                cc.pci_version,
                cc.m2_slots,
                cc.m2_types,
                cc.gpu_chipset,
                cc.vram_gb,
                cc.gpu_length_mm,
                cc.gpu_height_mm,
                cc.gpu_slots,
                cc.gpu_tdp_w,
                cc.gpu_req_psu_w,
                cc.gpu_pci_version,
                cc.max_gpu_length_mm,
                cc.max_cpu_cooler_height_mm,
                cc.max_psu_length_mm,
                cc.supported_mb_formats,
                cc.ssd_interface,
                cc.ssd_form_factor,
                cc.ssd_capacity_gb

            FROM components c
            LEFT JOIN component_prices cp
                ON c.id = cp.component_id AND cp.store = 'citilink'
            LEFT JOIN component_specs cs
                ON c.id = cs.component_id AND cs.store = 'citilink'
            LEFT JOIN component_compat cc
                ON c.id = cc.component_id
            ORDER BY c.category, cp.price_rub NULLS LAST
        """)

        cache: dict = {}

        for row in rows:
            (
                cid, name, category, image_url,
                price_rub, product_url,
                specs_raw,
                socket, chipset, ram_type, ram_slots, ram_max_freq,
                ram_height, ram_cap, tdp, cpu_pin, max_tdp, cooler_h,
                psu_w, psu_ff, psu_len, gpu_pin, ff, pci_ver,
                m2_slots, m2_types, gpu_chip, vram, gpu_len, gpu_h,
                gpu_slots, gpu_tdp, gpu_req, gpu_pci,
                max_gpu, max_cool, max_psu, mb_formats,
                ssd_iface, ssd_ff, ssd_gb,
            ) = row

            # Форматирование цены — то же, что делает парсер
            if price_rub:
                price_str = "{:,}".format(price_rub).replace(",", " ") + " руб"
            else:
                price_str = "---"

            # specs хранится в БД как JSON-строка
            specs_dict = {}
            if specs_raw:
                try:
                    specs_dict = json.loads(specs_raw) if isinstance(specs_raw, str) else specs_raw
                except Exception:
                    specs_dict = {}

            item = {
                "id":            cid,
                "name":          name or "",
                "category":      category or "",

                # Имена полей ТОЧНО совпадают с @SerializedName в ApiService.kt
                "imageUrl":      image_url or "",
                "priceCitilink": price_str,
                "productUrl":    product_url or "",

                # Характеристики — Android отображает их в диалоге
                "specs":         specs_dict,

                # Поля совместимости — нужны build_validator.py
                "socket":        socket or "---",
                "chipset":       chipset or "---",
                "ramType":       ram_type or "---",
                "ramSlots":      ram_slots or 0,
                "ramMaxFreq":    ram_max_freq or 0,
                "ramHeight":     ram_height or 0,
                "ramCapacity":   ram_cap or 0,
                "tdp":           tdp or 0,
                "cpuPowerPin":   cpu_pin or "---",
                "maxTdp":        max_tdp or 0,
                "coolerHeight":  cooler_h or 0,
                "psuWattage":    psu_w or 0,
                "psuFormFactor": psu_ff or "---",
                "psuLength":     psu_len or 0,
                "gpuPowerPin":   gpu_pin or "---",
                "formFactor":    ff or "---",
                "pciVersion":    pci_ver or "---",
                "m2Slots":       m2_slots or 0,
                "m2Types":       list(m2_types) if m2_types else [],
                "gpuChipset":    gpu_chip or "---",
                "vram":          vram or 0,
                "gpuLength":     gpu_len or 0,
                "gpuHeight":     gpu_h or 0,
                "gpuSlots":      gpu_slots or 0,
                "gpuTdp":        gpu_tdp or 0,
                "gpuReqPsu":     gpu_req or 0,
                "gpuPciVersion": gpu_pci or "---",
                "maxGpuLength":  max_gpu or 0,
                "maxCpuCoolerHeight": max_cool or 0,
                "maxPsuLength":  max_psu or 0,
                "supportedMbFormats": list(mb_formats) if mb_formats else [],
                "ssdInterface":  ssd_iface or "---",
                "ssdFormFactor": ssd_ff or "---",
                "ssdCapacityGb": ssd_gb or 0,
            }

            cache.setdefault(category, []).append(item)

        total = sum(len(v) for v in cache.values())
        log.info("load_all_from_db: загружено %d товаров в %d категориях", total, len(cache))
        return cache

    except Exception as e:
        log.error("Ошибка load_all_from_db: %s", e)
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════════════════════

def _nn(val) -> Optional[str]:
    return str(val) if val and val != "---" else None

def _ni(val) -> Optional[int]:
    try:
        v = int(float(str(val)))
        return v if v > 0 else None
    except Exception:
        return None

def _na(val) -> Optional[list]:
    return list(val) if val and isinstance(val, (list, tuple)) else None

def _extract_compat_fields(item: dict) -> dict:
    """Маппинг полей из парсера в поля БД."""
    return {
        "socket":             item.get("socket"),
        "chipset":            item.get("chipset"),
        "ramType":            item.get("ramType"),
        "ramSlots":           item.get("ramSlots"),
        "ramMaxFreq":         item.get("ramMaxFreq"),
        "ramHeight":          item.get("ramHeight"),
        "ramCapacity":        item.get("ramCapacity"),
        "tdp":                item.get("tdp"),
        "cpuPowerPin":        item.get("cpuPowerPin"),
        "maxTdp":             item.get("maxTdp"),
        "coolerHeight":       item.get("coolerHeight"),
        "psuWattage":         item.get("psuWattage"),
        "psuFormFactor":      item.get("psuFormFactor"),
        "psuLength":          item.get("psuLength"),
        "psuEfficiency":      item.get("psuCertification"),   # маппинг переименованного поля
        "gpuPowerPin":        item.get("gpuPowerPin"),
        "formFactor":         item.get("formFactor"),
        "pciVersion":         item.get("pciVersion"),
        "m2Slots":            item.get("m2Slots"),
        "m2Types":            item.get("m2Types"),
        "gpuChipset":         item.get("gpuChipset"),
        "vram":               item.get("vram"),
        "gpuLength":          item.get("gpuLength"),
        "gpuHeight":          item.get("gpuHeight"),
        "gpuSlots":           item.get("gpuSlots"),
        "gpuTdp":             item.get("gpuTdp"),
        "gpuReqPsu":          item.get("gpuReqPsu"),
        "gpuPciVersion":      item.get("gpuPciVersion"),
        "maxGpuLength":       item.get("maxGpuLength"),
        "maxCpuCoolerHeight": item.get("maxCpuCoolerHeight"),
        "maxPsuLength":       item.get("maxPsuLength"),
        "supportedMbFormats": item.get("supportedMbFormats"),
        "ssdInterface":       item.get("ssdInterface"),
        "ssdFormFactor":      item.get("ssdFormFactor"),
        "ssdCapacityGb":      item.get("ssdCapacityGb"),
    }