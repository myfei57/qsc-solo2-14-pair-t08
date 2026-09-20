"""余热回收应用服务：面向 API 的批次挂账、水质登记与核算查询。"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..core.validators import require_number, require_text
from ..domain.heatrecovery import HeatRecoverySystem


class RecoveryService:
    """封装回收领域组件，按批次组织操作。"""

    def __init__(self, recovery: HeatRecoverySystem) -> None:
        self.recovery = recovery

    def list_units(self) -> list[dict[str, Any]]:
        return self.recovery.units.all()

    def register_unit(
        self, brewery_id: str, line_id: str, code: str
    ) -> dict[str, Any]:
        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_line = require_text(line_id, field="line_id", max_length=64)
        clean_code = require_text(code, field="code", max_length=32)
        return self.recovery.register_unit(clean_brewery, clean_line, clean_code)

    def configure(self, unit_id: str, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "supply_temp_c",
            "flash_temp_c",
            "output_temp_c",
            "cold_temp_c",
            "target_temp_c",
            "hx_efficiency",
            "vapor_capture_ratio",
            "hlt_capacity_l",
            "batches_per_year",
            "steam_price_per_t",
            "water_price_per_t",
            "steam_to_tco2",
        }
        values = {key: body.get(key) for key in allowed if key in body}
        if not values:
            raise ValidationError("没有可更新的配置项", allowed=sorted(allowed))
        return self.recovery.configure(unit_id, **values)

    def estimate(self, unit_id: str, body: dict[str, Any]) -> dict[str, Any]:
        vapor = require_number(body.get("vapor_kg"), field="vapor_kg", minimum=0.0, maximum=2_000_000.0)
        condensate = require_number(
            body.get("condensate_kg"), field="condensate_kg", minimum=0.0, maximum=2_000_000.0
        )
        years = body.get("batches_per_year")
        return self.recovery.estimate(unit_id, vapor, condensate, years)

    def arm(
        self,
        unit_id: str,
        batch_id: str,
        planned_vapor_kg: float,
        planned_condensate_kg: float,
    ) -> dict[str, Any]:
        return self.recovery.arm(
            unit_id, batch_id, planned_vapor_kg, planned_condensate_kg
        )

    def start_capture(self, batch_id: str) -> dict[str, Any]:
        return self.recovery.start_capture(batch_id)

    def record_quality(
        self,
        batch_id: str,
        actor: str,
        values: dict[str, Any],
        lab_report: str = "",
    ) -> dict[str, Any]:
        """对批次对应的回收单元登记一次水质化验。"""

        run = self.recovery.get_run(batch_id)
        return self.recovery.record_quality(
            str(run["unit_id"]), actor, values, batch_id=batch_id, lab_report=lab_report
        )

    def settle(
        self,
        batch_id: str,
        actor: str,
        actual: dict[str, Any],
        force_divert: bool = False,
    ) -> dict[str, Any]:
        return self.recovery.settle(batch_id, actor, actual, force_divert=force_divert)

    def run_view(self, batch_id: str) -> dict[str, Any]:
        run = self.recovery.get_run(batch_id)
        check = None
        if run.get("quality_check_id"):
            check = self.recovery.checks.get(str(run["quality_check_id"]))
        return {"run": run, "quality_check": check}

    def list_runs(self, unit_id: str | None = None) -> list[dict[str, Any]]:
        return self.recovery.list_runs(unit_id=unit_id)

    def report(self, unit_id: str | None = None) -> dict[str, Any]:
        return self.recovery.report(unit_id=unit_id)

    def ledger(self, unit_id: str | None = None) -> dict[str, Any]:
        rows = self.recovery.ledger(unit_id=unit_id)
        return {"rows": rows, "count": len(rows)}

    def summary(self) -> dict[str, Any]:
        return self.recovery.summary()
