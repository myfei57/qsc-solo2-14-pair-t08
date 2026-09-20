"""二次蒸汽与冷凝水余热回收：回收单元、水质门控与批次核算台账。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError, NotFoundError, SequenceError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .audit import AuditLog
from .models import (
    HeatRecoveryUnit,
    QualityStatus,
    RecoveryRoute,
    RecoveryRun,
    RecoveryStage,
    WaterQualityCheck,
)
from .thermo import (
    BOILER_EFFICIENCY,
    STEAM_ENERGY_GJ_PER_T,
    heat_balance,
    settle_recovery,
)

RECOVERY_UNITS = "recovery_units"
RECOVERY_LEDGER = "recovery_runs"
QUALITY_CHECKS = "water_quality_checks"

# 直接补入酿造热水的冷凝水水质限值（工艺水内控指标）。
QUALITY_LIMITS: dict[str, tuple[float, float]] = {
    # 字段: (下限, 上限)
    "conductivity_us_cm": (0.0, 100.0),
    "ph": (6.5, 8.5),
    "hardness_mg_l": (0.0, 60.0),
    "chloride_mg_l": (0.0, 100.0),
    "sulfate_mg_l": (0.0, 250.0),
    "nitrate_mg_l": (0.0, 25.0),
    "iron_mg_l": (0.0, 0.2),
    "tco_mg_l": (0.0, 2.0),
    "tcb_cfu_ml": (0.0, 100.0),
}
QUALITY_LABELS = {
    "conductivity_us_cm": "电导率",
    "ph": "pH",
    "hardness_mg_l": "总硬度",
    "chloride_mg_l": "氯化物",
    "sulfate_mg_l": "硫酸盐",
    "nitrate_mg_l": "硝酸盐",
    "iron_mg_l": "铁",
    "tco_mg_l": "总有机碳",
    "tcb_cfu_ml": "菌落总数",
}


class HeatRecoverySystem:
    """管理回收单元配置、水质化验与逐批次余热核算。"""

    def __init__(
        self,
        store: FileStore,
        clock: Clock,
        alarms: AlarmCenter,
        audit: AuditLog,
    ) -> None:
        self.store = store
        self.clock = clock
        self.alarms = alarms
        self.audit = audit
        self.units = store.collection(RECOVERY_UNITS)
        self.runs = store.collection(RECOVERY_LEDGER)
        self.checks = store.collection(QUALITY_CHECKS)

    # ---- 回收单元 -----------------------------------------------------------

    def register_unit(self, brewery_id: str, line_id: str, code: str) -> dict[str, Any]:
        """为一条糖化线登记余热回收单元。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_line = require_text(line_id, field="line_id", max_length=64)
        clean_code = require_text(code, field="code", max_length=32)
        existing = self.units.find(lambda item: item.get("code") == clean_code)
        if existing:
            raise ConflictError("回收单元编号已存在", code=clean_code)
        now = format_moment(self.clock.now())
        unit = HeatRecoveryUnit(
            id=new_id("hru"),
            code=clean_code,
            brewery_id=clean_brewery,
            line_id=clean_line,
            updated_at=now,
        )
        return self.units.put(unit.id, unit.to_doc())

    def configure(self, unit_id: str, **values: Any) -> dict[str, Any]:
        """调整回收单元的工艺与经济参数。"""

        ranges = {
            "supply_temp_c": (80.0, 180.0),
            "flash_temp_c": (60.0, 150.0),
            "output_temp_c": (30.0, 99.0),
            "cold_temp_c": (0.0, 40.0),
            "target_temp_c": (40.0, 99.0),
            "hx_efficiency": (0.3, 1.0),
            "vapor_capture_ratio": (0.0, 1.0),
            "hlt_capacity_l": (100.0, 1_000_000.0),
            "batches_per_year": (1, 366),
            "steam_price_per_t": (0.0, 5000.0),
            "water_price_per_t": (0.0, 200.0),
            "steam_to_tco2": (0.0, 1.0),
        }
        unit = self.units.require(unit_id, label="回收单元")

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            patch: list[tuple[str, Any]] = []
            for key, raw in values.items():
                if raw is None:
                    continue
                if key not in ranges:
                    raise ValidationError("不支持的配置项", field=key)
                minimum, maximum = ranges[key]
                if key == "batches_per_year":
                    value = require_number(raw, field=key, minimum=minimum, maximum=maximum)
                    value = int(value)
                else:
                    value = require_number(raw, field=key, minimum=minimum, maximum=maximum)
                patch.append((key, value))
            patch.append(("updated_at", format_moment(self.clock.now())))
            return merge_documents(document, patch)

        with self.store.locks.guard(f"hru:{unit_id}"):
            return self.units.update(unit_id, mutate)

    def get_unit(self, unit_id: str) -> dict[str, Any]:
        return self.units.require(unit_id, label="回收单元")

    # ---- 水质化验 -----------------------------------------------------------

    def record_quality(
        self,
        unit_id: str,
        actor: str,
        values: dict[str, Any],
        batch_id: str | None = None,
        lab_report: str = "",
    ) -> dict[str, Any]:
        """登记一次冷凝水水质化验，按内控限值判定合格/不合格。"""

        unit = self.units.require(unit_id, label="回收单元")
        clean_actor = require_text(actor, field="actor", max_length=60)
        measured: dict[str, float] = {}
        for key, raw in values.items():
            if key not in QUALITY_LIMITS or raw is None:
                continue
            low, high = QUALITY_LIMITS[key]
            measured[key] = require_number(raw, field=key, minimum=low - 1e-9, maximum=1e6)
        if not measured:
            raise ValidationError("至少提交一项水质指标")
        coliform = values.get("coliform")
        online_ok = values.get("online_ok")

        failed: list[str] = []
        for key, value in measured.items():
            low, high = QUALITY_LIMITS[key]
            if value < low or value > high:
                failed.append(key)
        if coliform is False:
            failed.append("coliform")
        if online_ok is False:
            failed.append("online_conductivity")

        status = QualityStatus.PASSED.value if not failed else QualityStatus.FAILED.value
        now = format_moment(self.clock.now())
        check = WaterQualityCheck(
            id=new_id("qw"),
            unit_id=unit_id,
            batch_id=batch_id,
            status=status,
            conductivity_us_cm=measured.get("conductivity_us_cm"),
            ph=measured.get("ph"),
            hardness_mg_l=measured.get("hardness_mg_l"),
            chloride_mg_l=measured.get("chloride_mg_l"),
            sulfate_mg_l=measured.get("sulfate_mg_l"),
            nitrate_mg_l=measured.get("nitrate_mg_l"),
            iron_mg_l=measured.get("iron_mg_l"),
            tco_mg_l=measured.get("tco_mg_l"),
            tcb_cfu_ml=measured.get("tcb_cfu_ml"),
            coliform=coliform if isinstance(coliform, bool) else None,
            online_ok=online_ok if isinstance(online_ok, bool) else None,
            failed_items=failed,
            lab_report=lab_report.strip() if isinstance(lab_report, str) else "",
            checked_by=clean_actor,
            checked_at=now,
        )
        stored = self.checks.put(check.id, check.to_doc())
        if failed:
            labels = "、".join(QUALITY_LABELS.get(key, key) for key in failed if key in QUALITY_LABELS)
            detail = "、".join(key for key in failed if key not in QUALITY_LABELS)
            message = f"回收单元 {unit['code']} 冷凝水水质不合格"
            if labels:
                message += f"：{labels} 超限"
            if detail:
                message += f"（{detail} 异常）"
            self.alarms.raise_alarm(
                brewery_id=str(unit["brewery_id"]),
                source=f"hru:{unit_id}",
                severity="critical",
                code="recovery_water_quality_failed",
                message=message,
                latching=True,
                context={
                    "unit_id": unit_id,
                    "batch_id": batch_id,
                    "failed_items": failed,
                    "check_id": check.id,
                },
            )
        self.audit.record(
            str(unit["brewery_id"]),
            batch_id,
            clean_actor,
            "recovery.quality_checked",
            {"unit_id": unit_id, "status": status, "failed_items": failed},
        )
        return stored

    def latest_quality(self, unit_id: str, batch_id: str | None = None) -> dict[str, Any] | None:
        """返回最新一次水质化验（优先指定批次，否则取单元最近一次）。"""

        items = self.checks.find(lambda item: item.get("unit_id") == unit_id)
        if batch_id:
            batch_items = [item for item in items if item.get("batch_id") == batch_id]
            if batch_items:
                items = batch_items
        if not items:
            return None
        items.sort(key=lambda item: str(item.get("checked_at", "")))
        return items[-1]

    # ---- 投建前估算 ---------------------------------------------------------

    def estimate(
        self,
        unit_id: str,
        vapor_kg: float,
        condensate_kg: float,
        batches_per_year: int | None = None,
    ) -> dict[str, Any]:
        """按单元配置计算单批与年化回收效果（水质合格/不合格两种情景）。"""

        unit = self.units.require(unit_id, label="回收单元")
        vapor = require_number(vapor_kg, field="vapor_kg", minimum=0.0, maximum=2_000_000.0)
        condensate = require_number(condensate_kg, field="condensate_kg", minimum=0.0, maximum=2_000_000.0)
        years = int(batches_per_year or unit["batches_per_year"])
        require_number(years, field="batches_per_year", minimum=1, maximum=366)
        balance = heat_balance(
            vapor_kg=vapor,
            vapor_capture_ratio=float(unit["vapor_capture_ratio"]),
            condensate_kg=condensate,
            supply_temp_c=float(unit["supply_temp_c"]),
            flash_temp_c=float(unit["flash_temp_c"]),
            output_temp_c=float(unit["output_temp_c"]),
            cold_temp_c=float(unit["cold_temp_c"]),
            hx_efficiency=float(unit["hx_efficiency"]),
            hlt_capacity_kg=float(unit["hlt_capacity_l"]),
        )
        common = {
            "vapor_kg": vapor,
            "condensate_kg": condensate,
            "batches_per_year": years,
            "steam_price_per_t": float(unit["steam_price_per_t"]),
            "water_price_per_t": float(unit["water_price_per_t"]),
            "steam_to_tco2": float(unit["steam_to_tco2"]),
        }
        passed = settle_recovery(balance, water_quality_passed=True, **common)
        failed = settle_recovery(balance, water_quality_passed=False, **common)
        return {
            "unit_id": unit_id,
            "inputs": {"vapor_kg": vapor, "condensate_kg": condensate},
            "balance": balance,
            "quality_passed": passed,
            "quality_failed": failed,
        }

    # ---- 批次台账 -----------------------------------------------------------

    def arm(
        self,
        unit_id: str,
        batch_id: str,
        planned_vapor_kg: float,
        planned_condensate_kg: float,
    ) -> dict[str, Any]:
        """为批次挂账：按计划蒸发量/冷凝水量预热计算理论热平衡。"""

        unit = self.units.require(unit_id, label="回收单元")
        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        vapor = require_number(planned_vapor_kg, field="planned_vapor_kg", minimum=0.0, maximum=2_000_000.0)
        condensate = require_number(
            planned_condensate_kg, field="planned_condensate_kg", minimum=0.0, maximum=2_000_000.0
        )
        if vapor <= 0 and condensate <= 0:
            raise ValidationError("计划回收量不能全部为零")
        existing = self.runs.find(
            lambda item: item.get("batch_id") == clean_batch
            and item.get("stage") != RecoveryStage.SETTLED.value
        )
        if existing:
            raise ConflictError("该批次已有进行中的回收台账", batch_id=clean_batch)

        balance = heat_balance(
            vapor_kg=vapor,
            vapor_capture_ratio=float(unit["vapor_capture_ratio"]),
            condensate_kg=condensate,
            supply_temp_c=float(unit["supply_temp_c"]),
            flash_temp_c=float(unit["flash_temp_c"]),
            output_temp_c=float(unit["output_temp_c"]),
            cold_temp_c=float(unit["cold_temp_c"]),
            hx_efficiency=float(unit["hx_efficiency"]),
            hlt_capacity_kg=float(unit["hlt_capacity_l"]),
        )
        now = format_moment(self.clock.now())
        run = RecoveryRun(
            id=new_id("rec"),
            unit_id=unit_id,
            batch_id=clean_batch,
            stage=RecoveryStage.ARMED.value,
            route=RecoveryRoute.INDIRECT.value,
            planned_vapor_kg=vapor,
            planned_condensate_kg=condensate,
            balance=balance,
            updated_at=now,
        )
        return self.runs.put(run.id, run.to_doc())

    def start_capture(self, batch_id: str) -> dict[str, Any]:
        """煮沸开始，切换到回收中。"""

        run = self._require_active_run(batch_id)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != RecoveryStage.ARMED.value:
                raise SequenceError(
                    "当前回收状态不允许开始捕获",
                    batch_id=batch_id,
                    stage=document.get("stage"),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("stage", RecoveryStage.CAPTURING.value), ("started_capture_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"recovery:{batch_id}"):
            return self.runs.update(str(run["id"]), mutate)

    def settle(
        self,
        batch_id: str,
        actor: str,
        actual: dict[str, Any],
        force_divert: bool = False,
    ) -> dict[str, Any]:
        """批次结束按表计量结算：水质合格才计回用水量，否则切地漏。

        :param actual: 表计读数，键包括
            vapor_kg / condensate_kg / water_to_hlt_kg / water_diverted_kg /
            hlt_temp_c / cold_temp_c
        :param force_divert: 操作员强制排放（如怀疑管路污染），即便化验合格
        """

        clean_actor = require_text(actor, field="actor", max_length=60)
        run = self._require_active_run(batch_id)
        unit = self.units.require(str(run["unit_id"]), label="回收单元")
        if run.get("stage") != RecoveryStage.CAPTURING.value:
            raise SequenceError("尚未开始捕获，不能结算", batch_id=batch_id, stage=run.get("stage"))

        meters = self._read_meters(actual)
        check = self.latest_quality(str(run["unit_id"]), batch_id)
        quality_passed = bool(check and check.get("status") == QualityStatus.PASSED.value)
        if force_divert:
            quality_passed = False
        route = RecoveryRoute.DIRECT.value if quality_passed else RecoveryRoute.DIVERTED.value
        diverted_reason = ""
        if not quality_passed:
            if force_divert:
                diverted_reason = "operator_force_divert"
            elif check is None:
                diverted_reason = "quality_not_tested"
            else:
                diverted_reason = "quality_failed"

        # 热量按热水罐侧表计核算（最保守、可对账）：m·cp·ΔT
        delivered_vapor = float(meters["vapor_kg"]) * float(unit["vapor_capture_ratio"])
        heat_utilized_mj = (
            meters["water_to_hlt_kg"]
            * 4.186
            * (meters["hlt_temp_c"] - meters["cold_temp_c"])
            / 1000.0
        )
        heat_utilized_mj = max(0.0, heat_utilized_mj)
        steam_saved_t = heat_utilized_mj / 1000.0 / BOILER_EFFICIENCY / STEAM_ENERGY_GJ_PER_T
        water_reused_t = meters["water_to_hlt_kg"] / 1000.0 if quality_passed else 0.0
        if meters["water_diverted_kg"] > 0 or "water_diverted_kg" in actual:
            water_diverted_t = meters["water_diverted_kg"] / 1000.0
        else:
            # 未装排放表时按质量平衡倒推
            water_diverted_t = (
                delivered_vapor + meters["condensate_kg"] - water_reused_t * 1000.0
            ) / 1000.0
        water_diverted_t = max(0.0, water_diverted_t)
        steam_cost = steam_saved_t * float(unit["steam_price_per_t"])
        water_cost = water_reused_t * float(unit["water_price_per_t"])
        co2_t = steam_saved_t * float(unit["steam_to_tco2"])
        batches_per_year = int(unit["batches_per_year"])
        settlement = {
            "route": route,
            "quality_check_id": check["id"] if check else None,
            "quality_passed": quality_passed,
            "diverted_reason": diverted_reason,
            "heat_utilized_mj": round(heat_utilized_mj, 2),
            "steam_saved_t": round(steam_saved_t, 3),
            "water_reused_t": round(water_reused_t, 3),
            "water_diverted_t": round(water_diverted_t, 3),
            "co2_saved_t": round(co2_t, 4),
            "steam_cost_saved": round(steam_cost, 2),
            "water_cost_saved": round(water_cost, 2),
            "total_saved": round(steam_cost + water_cost, 2),
            "annualized": {
                "batches_per_year": batches_per_year,
                "steam_saved_t": round(steam_saved_t * batches_per_year, 1),
                "water_reused_t": round(water_reused_t * batches_per_year, 1),
                "co2_saved_t": round(co2_t * batches_per_year, 1),
                "total_saved": round((steam_cost + water_cost) * batches_per_year, 2),
            },
        }
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", RecoveryStage.SETTLED.value),
                    ("route", route),
                    ("quality_check_id", check["id"] if check else None),
                    ("actual", meters),
                    ("settlement", settlement),
                    ("diverted_reason", diverted_reason),
                    ("settled_at", now),
                    ("settled_by", clean_actor),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"recovery:{batch_id}"):
            stored = self.runs.update(str(run["id"]), mutate)
        self.store.append_event(
            "recovery.settled",
            {
                "batch_id": batch_id,
                "unit_id": str(run["unit_id"]),
                "route": route,
                "steam_saved_t": settlement["steam_saved_t"],
                "water_reused_t": settlement["water_reused_t"],
            },
        )
        self.audit.record(
            str(unit["brewery_id"]),
            batch_id,
            clean_actor,
            "recovery.settled",
            {
                "unit_id": str(run["unit_id"]),
                "route": route,
                "steam_saved_t": settlement["steam_saved_t"],
                "water_reused_t": settlement["water_reused_t"],
                "water_diverted_t": settlement["water_diverted_t"],
                "heat_utilized_mj": settlement["heat_utilized_mj"],
            },
        )
        return stored

    def _read_meters(self, actual: dict[str, Any]) -> dict[str, float]:
        if not isinstance(actual, dict):
            raise ValidationError("表计读数必须是对象", field="actual")
        schema = {
            "vapor_kg": (0.0, 2_000_000.0),
            "condensate_kg": (0.0, 2_000_000.0),
            "water_to_hlt_kg": (0.0, 2_000_000.0),
            "water_diverted_kg": (0.0, 2_000_000.0),
        }
        meters: dict[str, float] = {}
        for key, (low, high) in schema.items():
            meters[key] = require_number(actual.get(key, 0.0), field=key, minimum=low, maximum=high)
        hlt_temp = require_number(
            actual.get("hlt_temp_c"), field="hlt_temp_c", minimum=0.0, maximum=120.0
        )
        cold_temp = require_number(
            actual.get("cold_temp_c", 15.0), field="cold_temp_c", minimum=0.0, maximum=60.0
        )
        if hlt_temp < cold_temp:
            raise ValidationError("热水罐供水温度不能低于补水温度")
        meters["hlt_temp_c"] = hlt_temp
        meters["cold_temp_c"] = cold_temp
        return meters

    def _require_active_run(self, batch_id: str) -> dict[str, Any]:
        clean = require_text(batch_id, field="batch_id", max_length=64)
        items = self.runs.find(
            lambda item: item.get("batch_id") == clean
            and item.get("stage") != RecoveryStage.SETTLED.value
        )
        if not items:
            raise NotFoundError("该批次没有进行中的回收台账", batch_id=clean)
        return items[0]

    def get_run(self, batch_id: str) -> dict[str, Any]:
        clean = require_text(batch_id, field="batch_id", max_length=64)
        items = self.runs.find(lambda item: item.get("batch_id") == clean)
        if not items:
            raise NotFoundError("该批次没有回收台账", batch_id=clean)
        items.sort(key=lambda item: str(item.get("updated_at", "")))
        return items[-1]

    def list_runs(self, unit_id: str | None = None, settled_only: bool = False) -> list[dict[str, Any]]:
        items = self.runs.all()
        if unit_id:
            items = [item for item in items if item.get("unit_id") == unit_id]
        if settled_only:
            items = [item for item in items if item.get("stage") == RecoveryStage.SETTLED.value]
        items.sort(key=lambda item: str(item.get("settled_at") or item.get("updated_at", "")))
        return items

    # ---- 核算报表 -----------------------------------------------------------

    def report(self, unit_id: str | None = None) -> dict[str, Any]:
        """汇总已结算批次的回收量、节约费用与年化估算。"""

        runs = self.list_runs(unit_id=unit_id, settled_only=True)
        steam = sum(float(item["settlement"].get("steam_saved_t", 0.0)) for item in runs)
        water = sum(float(item["settlement"].get("water_reused_t", 0.0)) for item in runs)
        diverted = sum(float(item["settlement"].get("water_diverted_t", 0.0)) for item in runs)
        heat = sum(float(item["settlement"].get("heat_utilized_mj", 0.0)) for item in runs)
        co2 = sum(float(item["settlement"].get("co2_saved_t", 0.0)) for item in runs)
        money = sum(float(item["settlement"].get("total_saved", 0.0)) for item in runs)
        direct = [item for item in runs if item.get("route") == RecoveryRoute.DIRECT.value]
        monthly: dict[str, dict[str, float]] = {}
        for item in runs:
            month = str(item.get("settled_at", ""))[:7]
            bucket = monthly.setdefault(
                month, {"batches": 0, "steam_saved_t": 0.0, "water_reused_t": 0.0, "total_saved": 0.0}
            )
            bucket["batches"] += 1
            bucket["steam_saved_t"] += float(item["settlement"].get("steam_saved_t", 0.0))
            bucket["water_reused_t"] += float(item["settlement"].get("water_reused_t", 0.0))
            bucket["total_saved"] += float(item["settlement"].get("total_saved", 0.0))
        for bucket in monthly.values():
            for key in ("steam_saved_t", "water_reused_t", "total_saved"):
                bucket[key] = round(bucket[key], 2)
        return {
            "unit_id": unit_id,
            "settled_batches": len(runs),
            "direct_reuse_batches": len(direct),
            "diverted_batches": len(runs) - len(direct),
            "totals": {
                "steam_saved_t": round(steam, 2),
                "water_reused_t": round(water, 2),
                "water_diverted_t": round(diverted, 2),
                "heat_utilized_mj": round(heat, 1),
                "co2_saved_t": round(co2, 2),
                "total_saved": round(money, 2),
            },
            "monthly": dict(sorted(monthly.items())),
        }

    def ledger(self, unit_id: str | None = None) -> list[dict[str, Any]]:
        """导出逐批次核算台账行。"""

        rows: list[dict[str, Any]] = []
        for item in self.list_runs(unit_id=unit_id, settled_only=True):
            settlement = item.get("settlement", {})
            rows.append(
                {
                    "batch_id": item.get("batch_id"),
                    "unit_id": item.get("unit_id"),
                    "settled_at": item.get("settled_at"),
                    "route": item.get("route"),
                    "quality_check_id": item.get("quality_check_id"),
                    "planned_vapor_kg": item.get("planned_vapor_kg"),
                    "planned_condensate_kg": item.get("planned_condensate_kg"),
                    **item.get("actual", {}),
                    **settlement,
                }
            )
        return rows

    def summary(self) -> dict[str, Any]:
        """回收系统整体状态。"""

        items = self.runs.all()
        by_stage: dict[str, int] = {}
        for item in items:
            key = str(item.get("stage"))
            by_stage[key] = by_stage.get(key, 0) + 1
        return {
            "units": self.units.count(),
            "runs": len(items),
            "settled": by_stage.get(RecoveryStage.SETTLED.value, 0),
            "by_stage": by_stage,
            "quality_checks": self.checks.count(),
        }
