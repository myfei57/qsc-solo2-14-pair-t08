"""水侧余热核算的纯函数。

全部以「被加热的酿造水」侧为权威计量，避免直接测二次蒸汽（带气液夹带、
计量误差大）。蒸汽冷凝水若直接回收进热水罐，按其显热单独计一份。
"""

from __future__ import annotations

from .errors import ValidationError

# kJ/(kg·K)，水在 0~100°C 的工程近似比热
SPECIFIC_HEAT_WATER_KJ_KG_K = 4.187
# 0.7 MPa(g) 饱和蒸汽汽化潜热工程近似 kJ/kg，用于折算「等价补蒸汽量」
DEFAULT_LATENT_KJ_KG = 2048.0
# 标煤折算 kJ/kgce（29307 kJ/kgce），用于节能量上报
STANDARD_COAL_KJ_KG = 29307.0


def water_heat_kj(mass_kg: float, temp_out_c: float, temp_in_c: float) -> float:
    """被加热水获得的显热（kJ）。"""

    if mass_kg < 0:
        raise ValidationError("水量不能为负", mass_kg=mass_kg)
    if temp_out_c + 1e-9 < temp_in_c:
        raise ValidationError(
            "热水出口温度不能低于入口温度",
            temp_in_c=temp_in_c,
            temp_out_c=temp_out_c,
        )
    return mass_kg * SPECIFIC_HEAT_WATER_KJ_KG_K * (temp_out_c - temp_in_c)


def condensate_sensible_kj(mass_kg: float, temp_c: float, ref_c: float = 20.0) -> float:
    """冷凝水回收相对补水基准温度携带的显热（kJ）。"""

    if temp_c + 1e-9 < ref_c:
        return 0.0
    return water_heat_kj(mass_kg, temp_c, ref_c)


def steam_equivalent_kg(energy_kj: float, latent_kj_kg: float = DEFAULT_LATENT_KJ_KG) -> float:
    """把回收热量折算为少烧的饱和蒸汽质量（kg）。"""

    if latent_kj_kg <= 0:
        raise ValidationError("汽化潜热必须为正", latent_kj_kg=latent_kj_kg)
    if energy_kj < 0:
        raise ValidationError("回收热量不能为负", energy_kj=energy_kj)
    return energy_kj / latent_kj_kg


def standard_coal_kg(energy_kj: float) -> float:
    """回收热量折算标煤（kgce）。"""

    if energy_kj < 0:
        raise ValidationError("回收热量不能为负", energy_kj=energy_kj)
    return energy_kj / STANDARD_COAL_KJ_KG


def reuse_ratio(recovered_kg: float, total_kg: float) -> float | None:
    """冷凝水回用率（0~1）；总量为零时返回 None。"""

    if total_kg <= 0:
        return None
    if recovered_kg < 0 or recovered_kg > total_kg + 1e-9:
        raise ValidationError(
            "回收量不能为负或超过总量", recovered_kg=recovered_kg, total_kg=total_kg
        )
    return recovered_kg / total_kg
