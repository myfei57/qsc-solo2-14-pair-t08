"""二次蒸汽与冷凝水余热回收的热力学与水平衡计算。

全部为纯函数，便于单元测试与参数化核算。焓值采用 IAPWS 邻域内的
经验拟合（0–180 ℃），精度对工程估算足够，不依赖第三方库。
"""

from __future__ import annotations

from typing import Any

# ---- 物理常数 ---------------------------------------------------------------

WATER_CP_KJ_PER_KG_K = 4.186  # 水的比热，kJ/(kg·K)
LATENT_100C_KJ_PER_KG = 2257.0  # 100 ℃ 汽化潜热，kJ/kg

# ---- 经济/排放缺省因子 -------------------------------------------------------

STEAM_TO_TCO2 = 0.20  # 每吨蒸汽约 0.20 t CO2（天然气锅炉，含效率折损）
STEAM_PRICE_PER_T = 260.0  # 蒸汽单价，元/t
WATER_PRICE_PER_T = 4.5  # 酿造水综合水价，元/t
BOILER_EFFICIENCY = 0.90  # 锅炉热效率：回收同等热量少烧的蒸汽要除以效率
STEAM_ENERGY_GJ_PER_T = 2.75  # 3 bar(g) 饱和蒸汽每吨可释放焓差，GJ/t


# ---- 饱和水/蒸汽焓 -----------------------------------------------------------

def h_f(temp_c: float) -> float:
    """饱和水焓 kJ/kg，0–180 ℃ 二次拟合。"""

    return 4.211 * temp_c - 0.00057 * temp_c * temp_c


def h_fg(temp_c: float) -> float:
    """汽化潜热 kJ/kg，0–180 ℃ 线性近似（0 ℃≈2501，100 ℃≈2257）。"""

    return 2501.0 - 2.44 * temp_c


def flash_fraction(supply_temp_c: float, flash_temp_c: float) -> float:
    """饱和冷凝水节流到闪蒸压力后的闪蒸比例（质量分数）。"""

    numerator = h_f(supply_temp_c) - h_f(flash_temp_c)
    fraction = numerator / h_fg(flash_temp_c)
    return max(0.0, min(1.0, fraction))


# ---- 热/水平衡 ---------------------------------------------------------------

def heat_balance(
    *,
    vapor_kg: float,
    vapor_capture_ratio: float,
    condensate_kg: float,
    supply_temp_c: float,
    flash_temp_c: float,
    output_temp_c: float,
    cold_temp_c: float = 15.0,
    hx_efficiency: float = 0.90,
    hlt_capacity_kg: float | None = None,
) -> dict[str, Any]:
    """计算一个批次的可回收热量、产热水量与闪蒸份额。

    :param vapor_kg: 煮沸二次蒸汽可捕获总量（蒸发量 × 收集率），kg
    :param vapor_capture_ratio: 二次蒸汽实际进入冷凝器的比例 0–1
    :param condensate_kg: 糖化蒸汽冷凝水总量，kg
    :param supply_temp_c: 冷凝水在锅炉侧的饱和温度（如 3 bar(g)≈143.6 ℃）
    :param flash_temp_c: 闪蒸罐工作压力对应饱和温度（常压≈100 ℃）
    :param output_temp_c: 冷凝水冷却后/换热器出口温度，℃
    :param cold_temp_c: 冷酿造水补水温度，℃
    :param hx_efficiency: 换热器/管路综合热效率 0–1
    :param hlt_capacity_kg: 热水罐有效容积，kg；给出后对可利用热量封顶
    """

    vapor_captured_kg = vapor_kg * vapor_capture_ratio

    # 二次蒸汽：冷凝 + 冷凝水过冷到出口温度
    vapor_heat_kj = vapor_captured_kg * (
        h_fg(100.0) + WATER_CP_KJ_PER_KG_K * (100.0 - output_temp_c)
    )

    # 冷凝水闪蒸：闪蒸汽潜热 + 残余饱和水过冷
    fraction = flash_fraction(supply_temp_c, flash_temp_c)
    flash_steam_kg = condensate_kg * fraction
    residual_kg = condensate_kg - flash_steam_kg
    flash_heat_kj = flash_steam_kg * h_fg(flash_temp_c)
    residual_heat_kj = residual_kg * WATER_CP_KJ_PER_KG_K * (
        flash_temp_c - output_temp_c
    )
    condensate_heat_kj = flash_heat_kj + residual_heat_kj

    available_kj = vapor_heat_kj + condensate_heat_kj
    delivered_kj = available_kj * hx_efficiency

    # 单位冷酿造水从补水温度升到目标温度所需热量
    heat_per_kg = WATER_CP_KJ_PER_KG_K * (output_temp_c - cold_temp_c)
    hot_water_kg = delivered_kj / heat_per_kg if heat_per_kg > 0 else 0.0

    capped = False
    if hlt_capacity_kg is not None and hot_water_kg > hlt_capacity_kg:
        hot_water_kg = hlt_capacity_kg
        capped = True
    utilized_kj = hot_water_kg * heat_per_kg
    surplus_kj = max(0.0, delivered_kj - utilized_kj)

    return {
        "vapor_captured_kg": round(vapor_captured_kg, 1),
        "flash_fraction": round(fraction, 4),
        "flash_steam_kg": round(flash_steam_kg, 1),
        "residual_condensate_kg": round(residual_kg, 1),
        "vapor_heat_mj": round(vapor_heat_kj / 1000.0, 2),
        "flash_heat_mj": round(flash_heat_kj / 1000.0, 2),
        "residual_heat_mj": round(residual_heat_kj / 1000.0, 2),
        "available_heat_mj": round(available_kj / 1000.0, 2),
        "delivered_heat_mj": round(delivered_kj / 1000.0, 2),
        "utilized_heat_mj": round(utilized_kj / 1000.0, 2),
        "surplus_heat_mj": round(surplus_kj / 1000.0, 2),
        "hot_water_kg": round(hot_water_kg, 1),
        "hlt_capped": capped,
        "heat_per_kg_kj": round(heat_per_kg, 2),
        "hx_efficiency": hx_efficiency,
    }


def settle_recovery(
    balance: dict[str, Any],
    *,
    vapor_kg: float,
    condensate_kg: float,
    water_quality_passed: bool,
    batches_per_year: int = 300,
    steam_price_per_t: float = STEAM_PRICE_PER_T,
    water_price_per_t: float = WATER_PRICE_PER_T,
    boiler_efficiency: float = BOILER_EFFICIENCY,
    steam_to_tco2: float = STEAM_TO_TCO2,
) -> dict[str, Any]:
    """把热平衡折算为蒸汽/水/CO₂/费用节约，并按年批次数外推。

    热量无论水质是否合格都通过间壁换热器回收；
    水只有水质合格时才作为酿造水直接回用，不合格则进地漏（只计热量）。
    """

    delivered_mj = float(balance["delivered_heat_mj"])
    utilized_mj = float(balance["utilized_heat_mj"])
    # 锅炉少产蒸汽量（GJ 热量 / 效率 → 等效蒸汽吨数）
    steam_saved_t = utilized_mj / 1000.0 / boiler_efficiency / STEAM_ENERGY_GJ_PER_T
    # 实际进入管路的水 = 捕获的二次蒸汽凝结水 + 闪蒸后的残余冷凝水
    plumbed_t = (float(balance["vapor_captured_kg"]) + float(balance["residual_condensate_kg"])) / 1000.0
    water_reused_t = plumbed_t if water_quality_passed else 0.0
    water_diverted_t = plumbed_t - water_reused_t

    steam_cost = steam_saved_t * steam_price_per_t
    water_cost = water_reused_t * water_price_per_t
    co2_t = steam_saved_t * steam_to_tco2

    batch = {
        "steam_saved_t": round(steam_saved_t, 3),
        "water_reused_t": round(water_reused_t, 3),
        "water_diverted_t": round(max(0.0, water_diverted_t), 3),
        "heat_delivered_mj": round(delivered_mj, 2),
        "heat_utilized_mj": round(utilized_mj, 2),
        "co2_saved_t": round(co2_t, 4),
        "steam_cost_saved": round(steam_cost, 2),
        "water_cost_saved": round(water_cost, 2),
        "total_saved": round(steam_cost + water_cost, 2),
        "water_quality_passed": water_quality_passed,
    }
    annual = {
        "batches_per_year": batches_per_year,
        "steam_saved_t": round(steam_saved_t * batches_per_year, 1),
        "water_reused_t": round(water_reused_t * batches_per_year, 1),
        "co2_saved_t": round(co2_t * batches_per_year, 1),
        "total_saved": round((steam_cost + water_cost) * batches_per_year, 2),
    }
    return {"batch": batch, "annual": annual}
