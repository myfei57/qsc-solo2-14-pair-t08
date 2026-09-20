"""余热回收应用服务：联锁告警、审计与回收量核算报表。"""

from __future__ import annotations

from typing import Any

from ..core.heat import (
    STANDARD_COAL_KJ_KG,
    standard_coal_kg,
    steam_equivalent_kg,
    reuse_ratio,
)
from ..core.validators import require_text
from ..domain.alarms import AlarmCenter
from ..domain.audit import AuditLog
from ..domain.models import CondensateRoute, WaterQualityStatus
from ..domain.recovery import RecoveryController
from ..persistence.store import FileStore

BATCHES = "batches"


class RecoveryService:
    """对外编排余热回收：启停、水质门限、计量与核算。"""

    def __init__(
        self,
        recovery: RecoveryController,
        alarms: AlarmCenter,
        audit: AuditLog,
        store: FileStore,
    ) -> None:
        self.recovery = recovery
        self.alarms = alarms
        self.audit = audit
        self.store = store

    def start(self, batch_id: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        brewery_id = self._brewery_of(batch_id)
        run = self.recovery.start(batch_id, brewery_id)
        self.audit.record(brewery_id, batch_id, clean_actor, "recovery.started", {"run_id": run["id"]})
        return self.report(batch_id)

    def close(self, batch_id: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        run = self.recovery.close(batch_id)
        self.audit.record(
            str(run.get("brewery_id")), batch_id, clean_actor, "recovery.closed",
            {"run_id": run["id"]},
        )
        return self.report(batch_id)

    def sample_quality(self, batch_id: str, sample: dict[str, float], actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        before = self.recovery.get(batch_id)
        result = self.recovery.record_quality_sample(batch_id, sample)
        violations = result["sample"]["violations"]
        brewery_id = str(before.get("brewery_id"))
        if violations:
            self.alarms.raise_alarm(
                brewery_id=brewery_id,
                source=f"recovery:{batch_id}",
                severity="critical",
                code="recovery_water_quality",
                message="冷凝水水质越界，已故障安全切为直排，待人工复位",
                latching=True,
                context={"batch_id": batch_id, "violations": violations, "metrics": result["sample"]["metrics"]},
            )
            action = "recovery.quality_divert"
        else:
            action = "recovery.quality_pass"
        self.audit.record(
            brewery_id, batch_id, clean_actor, action,
            {"violations": violations, "route": result["run"].get("route")},
        )
        return self.report(batch_id)

    def resume(self, batch_id: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        run = self.recovery.resume(batch_id)
        self.audit.record(
            str(run.get("brewery_id")), batch_id, clean_actor, "recovery.resumed",
            {"run_id": run["id"]},
        )
        return self.report(batch_id)

    def record_condensate(self, batch_id: str, totalizer_kg: float) -> dict[str, Any]:
        self.recovery.record_condensate(batch_id, totalizer_kg)
        return self.report(batch_id)

    def record_hotwater(
        self, batch_id: str, totalizer_kg: float, temp_in_c: float, temp_out_c: float
    ) -> dict[str, Any]:
        self.recovery.record_hotwater(batch_id, totalizer_kg, temp_in_c, temp_out_c)
        return self.report(batch_id)

    def record_vapor_condensate(self, batch_id: str, totalizer_kg: float) -> dict[str, Any]:
        self.recovery.record_vapor_condensate(batch_id, totalizer_kg)
        return self.report(batch_id)

    def report(self, batch_id: str) -> dict[str, Any]:
        return self._accounting(self.recovery.get(batch_id))

    def ledger(self, brewery_id: str | None = None) -> dict[str, Any]:
        """跨批次汇总核算，可按工厂过滤。"""

        runs = self.recovery.all_runs()
        if brewery_id:
            runs = [run for run in runs if run.get("brewery_id") == brewery_id]
        rows = [self._accounting(run) for run in runs]

        energy_kj = sum(float(row["energy_recovered_kj"]) for row in rows)
        condensate_recovered = sum(float(row["condensate_recovered_kg"]) for row in rows)
        condensate_diverted = sum(float(row["condensate_diverted_kg"]) for row in rows)
        condensate_total = condensate_recovered + condensate_diverted
        hotwater_mass = sum(float(row["hotwater_mass_kg"]) for row in rows)
        return {
            "runs": len(rows),
            "energy_recovered_kj": round(energy_kj, 3),
            "steam_equivalent_kg": round(steam_equivalent_kg(energy_kj), 3),
            "standard_coal_kgce": round(standard_coal_kg(energy_kj), 3),
            "hotwater_heated_kg": round(hotwater_mass, 3),
            "condensate_total_kg": round(condensate_total, 3),
            "condensate_recovered_kg": round(condensate_recovered, 3),
            "condensate_diverted_kg": round(condensate_diverted, 3),
            "reuse_ratio": reuse_ratio(condensate_recovered, condensate_total),
            "coal_factor_kj_per_kgce": STANDARD_COAL_KJ_KG,
            "rows": rows,
        }

    # ---------------------------------------------------------------- 内部

    def _accounting(self, run: dict[str, Any]) -> dict[str, Any]:
        energy = float(run.get("energy_recovered_kj", 0.0))
        recovered = float(run.get("condensate_recovered_kg", 0.0))
        diverted = float(run.get("condensate_diverted_kg", 0.0))
        total_cond = recovered + diverted
        return {
            "batch_id": run.get("batch_id"),
            "run_id": run.get("id"),
            "stage": run.get("stage"),
            "route": run.get("route"),
            "route_label": "回收进水罐" if run.get("route") == CondensateRoute.RECOVER.value else "直排地漏",
            "quality_status": run.get("quality_status"),
            "quality_latched": bool(run.get("quality_latched")),
            "awaiting_reset": bool(
                run.get("quality_latched")
                and run.get("quality_status") == WaterQualityStatus.PASS.value
            ),
            "hotwater_mass_kg": run.get("hotwater_mass_kg", 0.0),
            "energy_recovered_kj": run.get("energy_recovered_kj", 0.0),
            "steam_equivalent_kg": round(steam_equivalent_kg(energy), 3),
            "standard_coal_kgce": round(standard_coal_kg(energy), 3),
            "condensate_total_kg": run.get("condensate_total_kg", 0.0),
            "condensate_recovered_kg": run.get("condensate_recovered_kg", 0.0),
            "condensate_diverted_kg": run.get("condensate_diverted_kg", 0.0),
            "condensate_bucket_total_kg": round(total_cond, 3),
            "reuse_ratio": reuse_ratio(recovered, total_cond),
            "vapor_condensate_kg": run.get("vapor_condensate_kg", 0.0),
            "started_at": run.get("started_at"),
            "closed_at": run.get("closed_at"),
        }

    def _brewery_of(self, batch_id: str) -> str:
        batch = self.store.collection(BATCHES).get(batch_id)
        if batch is None:
            from ..core.errors import NotFoundError

            raise NotFoundError("批次不存在", batch_id=batch_id)
        return str(batch.get("brewery_id"))
