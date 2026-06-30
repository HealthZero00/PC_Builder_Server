"""
© Жиляков Д.Э., 2026. Все права защищены.
"""

"""
database.py — работа с PostgreSQL для Вольтажа
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

def _nn(val, max_len: int | None = None) -> Optional[str]:
    if not val or val == "---":
        return None
    text = str(val)
    return text[:max_len] if max_len else text


def _ni(val) -> Optional[int]:
    try:
        v = int(float(str(val)))
        return v if v > 0 else None
    except Exception:
        return None


def _na(val) -> Optional[list]:
    return list(val) if val and isinstance(val, (list, tuple)) else None


def _has_value(value) -> bool:
    return value not in (None, "", "---", 0, [], {})


def _decode_specs(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _extract_price_rub(item: dict) -> int:
    candidates = [
        item.get("priceCitilink"),
        item.get("priceRegard"),
        item.get("priceDNS"),
        item.get("price"),
    ]
    raw_price = next(
        (v for v in candidates if v and str(v).strip() not in ("---", "0", "")),
        "0"
    )
    digits = "".join(filter(str.isdigit, str(raw_price)))
    return int(digits) if digits else 0


def _extract_compat_fields(item: dict) -> dict:
    return {
        "socket":            item.get("socket"),
        "chipset":           item.get("chipset"),
        "ramType":           item.get("ramType"),
        "ramSlots":          item.get("ramSlots"),
        "ramMaxFreq":        item.get("ramMaxFreq"),
        "ramHeight":         item.get("ramHeight"),
        "ramCapacity":       item.get("ramCapacity"),
        "tdp":               item.get("tdp"),
        "cpuPowerPin":       item.get("cpuPowerPin"),
        "maxTdp":            item.get("maxTdp"),
        "coolerHeight":      item.get("coolerHeight"),
        "psuWattage":        item.get("psuWattage"),
        "psuFormFactor":     item.get("psuFormFactor"),
        "psuLength":         item.get("psuLength"),
        "psuEfficiency":     item.get("psuEfficiency") or item.get("psuCertification"),
        "gpuPowerPin":       item.get("gpuPowerPin"),
        "formFactor":        item.get("formFactor"),
        "pciVersion":        item.get("pciVersion"),
        "m2Slots":           item.get("m2Slots"),
        "m2Types":           item.get("m2Types"),
        "gpuChipset":        item.get("gpuChipset"),
        "vram":              item.get("vram"),
        "gpuLength":         item.get("gpuLength"),
        "gpuHeight":         item.get("gpuHeight"),
        "gpuSlots":          item.get("gpuSlots"),
        "gpuTdp":            item.get("gpuTdp"),
        "gpuReqPsu":         item.get("gpuReqPsu"),
        "gpuPciVersion":     item.get("gpuPciVersion"),
        "maxGpuLength":      item.get("maxGpuLength"),
        "maxCpuCoolerHeight": item.get("maxCpuCoolerHeight"),
        "maxPsuLength":      item.get("maxPsuLength"),
        "supportedMbFormats": item.get("supportedMbFormats"),
        "ssdInterface":      item.get("ssdInterface"),
        "ssdFormFactor":     item.get("ssdFormFactor"),
        "ssdCapacityGb":     item.get("ssdCapacityGb"),
    }


def _upsert_component_compat(
    conn: pg8000.native.Connection,
    component_id: int,
    compat: dict,
) -> None:
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
            socket                   = COALESCE(EXCLUDED.socket,                   component_compat.socket),
            chipset                  = COALESCE(EXCLUDED.chipset,                  component_compat.chipset),
            ram_type                 = COALESCE(EXCLUDED.ram_type,                 component_compat.ram_type),
            ram_slots                = COALESCE(EXCLUDED.ram_slots,                component_compat.ram_slots),
            ram_max_freq_mhz         = COALESCE(EXCLUDED.ram_max_freq_mhz,         component_compat.ram_max_freq_mhz),
            ram_height_mm            = COALESCE(EXCLUDED.ram_height_mm,            component_compat.ram_height_mm),
            ram_capacity_gb          = COALESCE(EXCLUDED.ram_capacity_gb,          component_compat.ram_capacity_gb),
            tdp_w                    = COALESCE(EXCLUDED.tdp_w,                    component_compat.tdp_w),
            cpu_power_pin            = COALESCE(EXCLUDED.cpu_power_pin,            component_compat.cpu_power_pin),
            max_tdp_w                = COALESCE(EXCLUDED.max_tdp_w,                component_compat.max_tdp_w),
            cooler_height_mm         = COALESCE(EXCLUDED.cooler_height_mm,         component_compat.cooler_height_mm),
            psu_wattage_w            = COALESCE(EXCLUDED.psu_wattage_w,            component_compat.psu_wattage_w),
            psu_form_factor          = COALESCE(EXCLUDED.psu_form_factor,          component_compat.psu_form_factor),
            psu_length_mm            = COALESCE(EXCLUDED.psu_length_mm,            component_compat.psu_length_mm),
            psu_efficiency           = COALESCE(EXCLUDED.psu_efficiency,           component_compat.psu_efficiency),
            gpu_power_pin            = COALESCE(EXCLUDED.gpu_power_pin,            component_compat.gpu_power_pin),
            form_factor              = COALESCE(EXCLUDED.form_factor,              component_compat.form_factor),
            pci_version              = COALESCE(EXCLUDED.pci_version,              component_compat.pci_version),
            m2_slots                 = COALESCE(EXCLUDED.m2_slots,                 component_compat.m2_slots),
            m2_types                 = COALESCE(EXCLUDED.m2_types,                 component_compat.m2_types),
            gpu_chipset              = COALESCE(EXCLUDED.gpu_chipset,              component_compat.gpu_chipset),
            vram_gb                  = COALESCE(EXCLUDED.vram_gb,                  component_compat.vram_gb),
            gpu_length_mm            = COALESCE(EXCLUDED.gpu_length_mm,            component_compat.gpu_length_mm),
            gpu_height_mm            = COALESCE(EXCLUDED.gpu_height_mm,            component_compat.gpu_height_mm),
            gpu_slots                = COALESCE(EXCLUDED.gpu_slots,                component_compat.gpu_slots),
            gpu_tdp_w                = COALESCE(EXCLUDED.gpu_tdp_w,                component_compat.gpu_tdp_w),
            gpu_req_psu_w            = COALESCE(EXCLUDED.gpu_req_psu_w,            component_compat.gpu_req_psu_w),
            gpu_pci_version          = COALESCE(EXCLUDED.gpu_pci_version,          component_compat.gpu_pci_version),
            max_gpu_length_mm        = COALESCE(EXCLUDED.max_gpu_length_mm,        component_compat.max_gpu_length_mm),
            max_cpu_cooler_height_mm = COALESCE(EXCLUDED.max_cpu_cooler_height_mm, component_compat.max_cpu_cooler_height_mm),
            max_psu_length_mm        = COALESCE(EXCLUDED.max_psu_length_mm,        component_compat.max_psu_length_mm),
            supported_mb_formats     = COALESCE(EXCLUDED.supported_mb_formats,     component_compat.supported_mb_formats),
            ssd_interface            = COALESCE(EXCLUDED.ssd_interface,            component_compat.ssd_interface),
            ssd_form_factor          = COALESCE(EXCLUDED.ssd_form_factor,          component_compat.ssd_form_factor),
            ssd_capacity_gb          = COALESCE(EXCLUDED.ssd_capacity_gb,          component_compat.ssd_capacity_gb),
            updated_at               = now()
        """,
        cid=component_id,
        socket=_nn(compat.get("socket")),
        chipset=_nn(compat.get("chipset"), 30),
        ram_type=_nn(compat.get("ramType"), 10),
        ram_slots=_ni(compat.get("ramSlots")),
        ram_max_freq=_ni(compat.get("ramMaxFreq")),
        ram_height=_ni(compat.get("ramHeight")),
        ram_cap=_ni(compat.get("ramCapacity")),
        tdp=_ni(compat.get("tdp")),
        cpu_pin=_nn(compat.get("cpuPowerPin"), 20),
        max_tdp=_ni(compat.get("maxTdp")),
        cooler_h=_ni(compat.get("coolerHeight")),
        psu_w=_ni(compat.get("psuWattage")),
        psu_ff=_nn(compat.get("psuFormFactor"), 10),
        psu_len=_ni(compat.get("psuLength")),
        psu_eff=_nn(compat.get("psuEfficiency"), 10),
        gpu_pin=_nn(compat.get("gpuPowerPin"), 30),
        ff=_nn(compat.get("formFactor"), 15),
        pci_ver=_nn(compat.get("pciVersion"), 5),
        m2_slots=_ni(compat.get("m2Slots")),
        m2_types=_na(compat.get("m2Types")),
        gpu_chip=_nn(compat.get("gpuChipset"), 80),
        vram=_ni(compat.get("vram")),
        gpu_len=_ni(compat.get("gpuLength")),
        gpu_h=_ni(compat.get("gpuHeight")),
        gpu_slots=_ni(compat.get("gpuSlots")),
        gpu_tdp=_ni(compat.get("gpuTdp")),
        gpu_req=_ni(compat.get("gpuReqPsu")),
        gpu_pci=_nn(compat.get("gpuPciVersion"), 5),
        max_gpu=_ni(compat.get("maxGpuLength")),
        max_cool=_ni(compat.get("maxCpuCoolerHeight")),
        max_psu=_ni(compat.get("maxPsuLength")),
        mb_formats=_na(compat.get("supportedMbFormats")),
        ssd_iface=_nn(compat.get("ssdInterface"), 10),
        ssd_ff=_nn(compat.get("ssdFormFactor"), 20),
        ssd_gb=_ni(compat.get("ssdCapacityGb")),
    )

def _is_valid_image_url(url: str) -> bool:
    if not url or url == "---":
        return False
    url = url.strip()
    if not url.startswith("http"):
        return False
    if url.startswith("data:"):
        return False
    return True


def save_to_db(category_name: str, items_list: list, store: str = "citilink") -> int:
    conns = get_connections()
    if not conns:
        log.error("save_to_db: Нет доступных подключений!")
        return 0

    import matcher

    saved_count = 0
    try:
        for item in items_list:
            raw_name = (item.get("name") or "").strip()
            if not raw_name:
                continue

            price_rub = _extract_price_rub(item)
            specs_json = json.dumps(item.get("specs") or {}, ensure_ascii=False)
            compat = _extract_compat_fields(item)

            new_image_url = item.get("imageUrl") or ""
            image_is_valid = _is_valid_image_url(new_image_url)

            for conn in conns:
                try:
                    final_name = raw_name
                    comp_id = None

                    if store != "citilink":
                        existing_rows = conn.run(
                            "SELECT name FROM components WHERE category = :cat",
                            cat=category_name
                        )
                        existing_names = [r[0] for r in existing_rows]
                        matched_name = matcher.find_match(raw_name, existing_names)
                        if matched_name:
                            final_name = matched_name

                    res = conn.run(
                        """
                        INSERT INTO components (name, category, image_url)
                        VALUES (:name, :cat, :img)
                        ON CONFLICT (name) DO UPDATE
                            SET category   = EXCLUDED.category,
                                image_url  = CASE
                                    WHEN EXCLUDED.image_url = '' OR EXCLUDED.image_url IS NULL
                                        THEN components.image_url
                                    WHEN components.image_url IS NULL OR components.image_url = ''
                                        THEN EXCLUDED.image_url
                                    WHEN components.image_url NOT LIKE 'http%'
                                        THEN EXCLUDED.image_url
                                    ELSE components.image_url
                                END,
                                updated_at = now()
                        RETURNING id, image_url
                        """,
                        name=final_name,
                        cat=category_name,
                        img=new_image_url if image_is_valid else "",
                    )
                    comp_id = res[0][0]
                    stored_image = res[0][1] or ""

                    if image_is_valid and not _is_valid_image_url(stored_image):
                        conn.run(
                            """
                            UPDATE components
                               SET image_url  = :img,
                                   updated_at = now()
                             WHERE id = :cid
                               AND (image_url IS NULL OR image_url = '' OR image_url NOT LIKE 'http%')
                            """,
                            img=new_image_url,
                            cid=comp_id,
                        )
                        log.debug(
                            "[БД] image_url принудительно обновлён для '%s': %s",
                            final_name[:50], new_image_url[:80],
                        )

                    conn.run(
                        """
                        INSERT INTO component_prices (component_id, store, price_rub, product_url)
                        VALUES (:cid, :store, :price, :url)
                        ON CONFLICT (component_id, store) DO UPDATE
                            SET price_rub   = EXCLUDED.price_rub,
                                product_url = EXCLUDED.product_url,
                                updated_at  = now()
                        """,
                        cid=comp_id,
                        store=store,
                        price=price_rub,
                        url=item.get("productUrl", ""),
                    )

                    conn.run(
                        """
                        INSERT INTO component_specs (component_id, store, specs)
                        VALUES (:cid, :store, :specs)
                        ON CONFLICT (component_id, store) DO UPDATE
                            SET specs      = EXCLUDED.specs,
                                updated_at = now()
                        """,
                        cid=comp_id,
                        store=store,
                        specs=specs_json,
                    )

                    _upsert_component_compat(conn, comp_id, compat)

                    log.debug(
                        "[БД] %s '%s' image='%s'",
                        store, final_name[:50], stored_image[:60] if stored_image else "(пусто)",
                    )

                except Exception as e:
                    log.error(
                        "Ошибка записи товара '%s' в БД: %s",
                        raw_name, e, exc_info=True,
                    )

            saved_count += 1

        log.info("[БД] %s: сохранено %d товаров (store=%s)", category_name, saved_count, store)

    finally:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

    return saved_count

def load_all_from_db() -> dict:
    conns = get_connections()
    if not conns:
        log.error("load_all_from_db: Нет подключений к БД")
        return {}

    conn = conns[0]
    try:
        rows = conn.run("""
            SELECT
                c.id, c.name, c.category, c.image_url,
                cp_cl.price_rub,  cp_cl.product_url,
                cp_rg.price_rub,  cp_rg.product_url,
                cp_dns.price_rub, cp_dns.product_url,
                cs_cl.specs  AS specs_citilink,
                cs_rg.specs  AS specs_regard,
                cs_dns.specs AS specs_dns,
                cc.socket, cc.chipset, cc.ram_type, cc.ram_slots,
                cc.ram_max_freq_mhz, cc.ram_height_mm, cc.ram_capacity_gb,
                cc.tdp_w, cc.cpu_power_pin, cc.max_tdp_w, cc.cooler_height_mm,
                cc.psu_wattage_w, cc.psu_form_factor, cc.psu_length_mm,
                cc.psu_efficiency, cc.gpu_power_pin, cc.form_factor, cc.pci_version,
                cc.m2_slots, cc.m2_types, cc.gpu_chipset, cc.vram_gb,
                cc.gpu_length_mm, cc.gpu_height_mm, cc.gpu_slots, cc.gpu_tdp_w,
                cc.gpu_req_psu_w, cc.gpu_pci_version, cc.max_gpu_length_mm,
                cc.max_cpu_cooler_height_mm, cc.max_psu_length_mm,
                cc.supported_mb_formats, cc.ssd_interface, cc.ssd_form_factor,
                cc.ssd_capacity_gb
            FROM components c
            LEFT JOIN component_prices cp_cl
                ON c.id = cp_cl.component_id AND cp_cl.store = 'citilink'
            LEFT JOIN component_prices cp_rg
                ON c.id = cp_rg.component_id AND cp_rg.store = 'regard'
            LEFT JOIN component_prices cp_dns
                ON c.id = cp_dns.component_id AND cp_dns.store = 'dns'
            LEFT JOIN component_specs cs_cl
                ON c.id = cs_cl.component_id AND cs_cl.store = 'citilink'
            LEFT JOIN component_specs cs_rg
                ON c.id = cs_rg.component_id AND cs_rg.store = 'regard'
            LEFT JOIN component_specs cs_dns
                ON c.id = cs_dns.component_id AND cs_dns.store = 'dns'
            LEFT JOIN component_compat cc
                ON c.id = cc.component_id
            ORDER BY c.category
        """)

        cache: dict = {}
        for row in rows:
            (
                cid, name, category, image_url,
                price_cl,  url_cl,
                price_rg,  url_rg,
                price_dns, url_dns,
                specs_cl_raw, specs_rg_raw, specs_dns_raw,
                socket, chipset, ram_type, ram_slots, ram_max_freq,
                ram_height, ram_cap, tdp, cpu_pin, max_tdp, cooler_h,
                psu_w, psu_ff, psu_len, psu_eff, gpu_pin, ff, pci_ver,
                m2_slots, m2_types, gpu_chip, vram, gpu_len, gpu_h,
                gpu_slots, gpu_tdp, gpu_req, gpu_pci,
                max_gpu, max_cool, max_psu, mb_formats,
                ssd_iface, ssd_ff, ssd_gb,
            ) = row

            price_cl_str  = "{:,}".format(price_cl).replace(",", " ")  + " руб" if price_cl  else "---"
            price_rg_str  = "{:,}".format(price_rg).replace(",", " ")  + " руб" if price_rg  else "---"
            price_dns_str = "{:,}".format(price_dns).replace(",", " ") + " руб" if price_dns else "---"

            specs_cl  = _decode_specs(specs_cl_raw)
            specs_rg  = _decode_specs(specs_rg_raw)
            specs_dns = _decode_specs(specs_dns_raw)

            specs_dict: dict = {}
            for source_specs in (specs_cl, specs_rg, specs_dns):
                specs_dict.update(source_specs)

            try:
                from parser_engine import _extract_logic
                derived_compat = _extract_logic(category or "", name or "", specs_dict)
            except Exception:
                derived_compat = {}

            item = {
                "id":       cid,
                "name":     name     or "",
                "category": category or "",
                "imageUrl": image_url or "",

                "priceCitilink":    price_cl_str,
                "productUrl":       url_cl  or "",
                "priceRegard":      price_rg_str,
                "productUrlRegard": url_rg  or "",
                "priceDNS":         price_dns_str,
                "productUrlDNS":    url_dns or "",

                "specs":         specs_dict,
                "specsCitilink": specs_cl,
                "specsRegard":   specs_rg,
                "specsDNS":      specs_dns,

                "socket":      socket    or "---",
                "chipset":     chipset   or "---",
                "ramType":     ram_type  or "---",
                "ramSlots":    ram_slots or 0,
                "ramMaxFreq":  ram_max_freq or 0,
                "ramHeight":   ram_height   or 0,
                "ramCapacity": ram_cap      or 0,
                "tdp":         tdp          or 0,
                "cpuPowerPin": cpu_pin or "---",
                "maxTdp":      max_tdp or 0,
                "coolerHeight": cooler_h or 0,
                "psuWattage":   psu_w    or 0,
                "psuFormFactor": psu_ff  or "---",
                "psuLength":     psu_len or 0,
                "psuEfficiency": psu_eff or "---",
                "gpuPowerPin":   gpu_pin or "---",
                "formFactor":    ff      or "---",
                "pciVersion":    pci_ver or "---",
                "m2Slots":  m2_slots or 0,
                "m2Types":  list(m2_types) if m2_types else [],
                "gpuChipset":   gpu_chip or "---",
                "vram":         vram     or 0,
                "gpuLength":    gpu_len  or 0,
                "gpuHeight":    gpu_h    or 0,
                "gpuSlots":     gpu_slots or 0,
                "gpuTdp":       gpu_tdp  or 0,
                "gpuReqPsu":    gpu_req  or 0,
                "gpuPciVersion": gpu_pci or "---",
                "maxGpuLength":        max_gpu  or 0,
                "maxCpuCoolerHeight":  max_cool or 0,
                "maxPsuLength":        max_psu  or 0,
                "supportedMbFormats":  list(mb_formats) if mb_formats else [],
                "ssdInterface":   ssd_iface or "---",
                "ssdFormFactor":  ssd_ff    or "---",
                "ssdCapacityGb":  ssd_gb    or 0,
            }

            for key, value in derived_compat.items():
                if _has_value(value) and not _has_value(item.get(key)):
                    item[key] = value

            cache.setdefault(category, []).append(item)

        total = sum(len(v) for v in cache.values())
        log.info(
            "load_all_from_db: загружено %d товаров в %d категориях",
            total, len(cache),
        )
        return cache

    except Exception as e:
        log.error("Ошибка load_all_from_db: %s", e)
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def save_regard_price_to_db(
    component_name: str,
    price_str: str,
    product_url: str = "",
    store: str = "regard",
) -> bool:
    digits = "".join(filter(str.isdigit, str(price_str)))
    price_rub = int(digits) if digits else 0

    conns = get_connections()
    if not conns:
        log.error("save_regard_price_to_db: Нет подключений!")
        return False

    success = False
    for conn in conns:
        try:
            res = conn.run(
                "SELECT id FROM components WHERE name = :name LIMIT 1",
                name=component_name,
            )
            if not res:
                log.warning("Компонент '%s' не найден", component_name)
                continue

            component_id = res[0][0]
            conn.run(
                """
                INSERT INTO component_prices (component_id, store, price_rub, product_url)
                VALUES (:cid, :store, :price, :url)
                ON CONFLICT (component_id, store) DO UPDATE
                    SET price_rub   = EXCLUDED.price_rub,
                        product_url = EXCLUDED.product_url,
                        updated_at  = now()
                """,
                cid=component_id,
                store=store,
                price=price_rub,
                url=product_url,
            )
            log.info("[БД] Цена %s для '%s': %d руб.", store, component_name, price_rub)
            success = True

        except Exception as e:
            log.error("save_regard_price_to_db: Ошибка для '%s': %s", component_name, e)

    for c in conns:
        try:
            c.close()
        except Exception:
            pass

    return success

def save_regard_specs_to_db(
    component_name: str,
    specs: dict,
    store: str = "regard",
) -> bool:
    if not specs:
        return False

    specs_json = json.dumps(specs, ensure_ascii=False)
    conns = get_connections()
    if not conns:
        return False

    success = False
    for conn in conns:
        try:
            res = conn.run(
                "SELECT id FROM components WHERE name = :name LIMIT 1",
                name=component_name,
            )
            if not res:
                log.warning("Компонент '%s' не найден", component_name)
                continue

            component_id = res[0][0]
            conn.run(
                """
                INSERT INTO component_specs (component_id, store, specs)
                VALUES (:cid, :store, :specs)
                ON CONFLICT (component_id, store) DO UPDATE
                    SET specs      = EXCLUDED.specs,
                        updated_at = now()
                """,
                cid=component_id,
                store=store,
                specs=specs_json,
            )
            log.info("[БД] Specs %s для '%s': %d полей", store, component_name, len(specs))
            success = True

        except Exception as e:
            log.error("save_regard_specs_to_db: Ошибка для '%s': %s", component_name, e)

    for c in conns:
        try:
            c.close()
        except Exception:
            pass

    return success


def save_store_specs_and_compat_to_db(
    component_name: str,
    category_name: str,
    item: dict,
    store: str,
) -> bool:
    specs = item.get("specs") or {}
    compat = _extract_compat_fields(item)
    specs_json = json.dumps(specs, ensure_ascii=False)

    new_image_url = item.get("imageUrl") or ""

    conns = get_connections()
    if not conns:
        return False

    success = False
    for conn in conns:
        try:
            res = conn.run(
                "SELECT id, image_url FROM components WHERE name = :name LIMIT 1",
                name=component_name,
            )
            if not res:
                log.warning("Компонент '%s' не найден", component_name)
                continue

            component_id = res[0][0]
            stored_image = res[0][1] or ""

            if _is_valid_image_url(new_image_url) and not _is_valid_image_url(stored_image):
                conn.run(
                    """
                    UPDATE components
                       SET image_url  = :img,
                           updated_at = now()
                     WHERE id = :cid
                    """,
                    img=new_image_url,
                    cid=component_id,
                )
                log.debug(
                    "[БД] image_url обновлён для '%s' (%s): %s",
                    component_name[:50], store, new_image_url[:80],
                )

            if specs:
                conn.run(
                    """
                    INSERT INTO component_specs (component_id, store, specs)
                    VALUES (:cid, :store, :specs)
                    ON CONFLICT (component_id, store) DO UPDATE
                        SET specs      = EXCLUDED.specs,
                            updated_at = now()
                    """,
                    cid=component_id,
                    store=store,
                    specs=specs_json,
                )

            _upsert_component_compat(conn, component_id, compat)
            log.info(
                "[БД] Specs + compat %s для '%s' (%s): %d полей",
                store, component_name, category_name, len(specs),
            )
            success = True

        except Exception as e:
            log.error(
                "save_store_specs_and_compat_to_db: Ошибка для '%s': %s",
                component_name, e, exc_info=True,
            )

    for c in conns:
        try:
            c.close()
        except Exception:
            pass

    return success