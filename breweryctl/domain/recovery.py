"""余热回收：二次蒸汽取热 + 蒸汽冷凝水分质回用。

- 二次蒸汽（煮沸/糖化外排蒸汽）经间接冷凝器把热交给酿造热水，
  其凝结水（二次水）可能夹带酒花树脂/挥发性物质，默认不进水罐，
  只在热水侧用热量表计量回收的热。
- 蒸汽夹套冷凝水是热源、也是合格补给水源，经在线水质门限决定
  「回收进热水罐」或「直排地漏」；任一指标越界即故障安全切排放
  并锁定，待水质恢复后由人工复位。
- 热水侧累计热量表为回收热量的权威计量；冷凝水侧流量表按回收 /
  排放两路累计，可核算回用率。
"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, NotFoundError, SequenceError, ValidationError
from ..core.heat import water_heat_kj
from ..core.ids import new_id
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .models import CondensateRoute, RecoveryRun, RecoveryStage, WaterQualityStatus

RECOVERY_RUNS = "recovery_runs"

# 可由在线仪表给出的水质指标及取值范围
_QUALITY_FIELDS = {
    "conductivity_us_cm": (0.0, 200000.0),
    "ph": (0.0, 14.0),
    "hardness_mg_l": (0.0, 5000.0),
    "oil_mg_l": (0.0, 500.0),
}


class RecoveryController:
    """管理按批次建立的余热回收台账与阀门联锁。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.runs = store.collection(RECOVERY_RUNS)

    # ------------------------------------------------------------------ 启停

    def start(self, batch_id: str, brewery_id: str) -> dict[str, Any]:
        """为批次建立回收运行；未投运前默认直排、不进水罐。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        existing = self.runs.get(clean_batch)
        if existing and existing.get("stage") == RecoveryStage.ACTIVE.value:
            raise ConflictError("该批次已有进行中的余热回收运行", batch_id=clean_batch)
        now = format_moment(self.clock.now())
        run = RecoveryRun(
            id=new_id("rec"),
            batch_id=clean_batch,
            brewery_id=clean_brewery,
            stage=RecoveryStage.ACTIVE.value,
            route=CondensateRoute.DIVERT.value,
            quality_status=WaterQualityStatus.UNKNOWN.value,
            started_at=now,
            updated_at=now,
        )
        doc = self.runs.put(clean_batch, run.to_doc())
        self._ledger(
            "recovery.started",
            clean_batch,
            {"run_id": run.id, "brewery_id": clean_brewery, "route": run.route},
        )
        return doc

    def close(self, batch_id: str) -> dict[str, Any]:
        """结束回收运行并冻结累计值。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != RecoveryStage.ACTIVE.value:
                raise SequenceError(
                    "回收运行未在进行中，无法结束",
                    batch_id=batch_id,
                    stage=document.get("stage"),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", RecoveryStage.CLOSED.value),
                    ("route", CondensateRoute.DIVERT.value),
                    ("closed_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"recovery:{batch_id}"):
            updated = self.runs.update(batch_id, mutate)
        self._ledger("recovery.closed", batch_id, {"run_id": updated["id"]})
        return updated

    # ------------------------------------------------------------- 水质门限

    def evaluate_quality(self, sample: dict[str, float]) -> tuple[bool, list[dict[str, Any]]]:
        """按配置门限判定水质是否合格，返回（是否合格、越界项）。"""

        violations: list[dict[str, Any]] = []
        conductivity = sample.get("conductivity_us_cm")
        if conductivity is not None and conductivity > self.settings.conductivity_max_us_cm:
            violations.append(
                {"metric": "conductivity_us_cm", "value": conductivity, "limit": self.settings.conductivity_max_us_cm}
            )
        ph = sample.get("ph")
        if ph is not None and not (self.settings.ph_min <= ph <= self.settings.ph_max):
            violations.append(
                {"metric": "ph", "value": ph, "limit": [self.settings.ph_min, self.settings.ph_max]}
            )
        hardness = sample.get("hardness_mg_l")
        if hardness is not None and hardness > self.settings.hardness_max_mg_l:
            violations.append(
                {"metric": "hardness_mg_l", "value": hardness, "limit": self.settings.hardness_max_mg_l}
            )
        oil = sample.get("oil_mg_l")
        if oil is not None and oil > self.settings.oil_max_mg_l:
            violations.append({"metric": "oil_mg_l", "value": oil, "limit": self.settings.oil_max_mg_l})
        return (not violations, violations)

    def record_quality_sample(
        self, batch_id: str, sample: dict[str, float]
    ) -> dict[str, Any]:
        """录入一次在线水质采样，并据此切换回收/排放阀门。

        故障安全：越界或数据缺失时一律排放；越界即锁定，必须在水质
        恢复合格后由人工 :meth:`resume` 才能重新回收。
        """

        clean_sample = self._clean_sample(sample)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            self._require_active(document, batch_id)
            passed, _violations = self.evaluate_quality(clean_sample)
            now = format_moment(self.clock.now())
            sample_id = new_id("qs")
            status = (
                WaterQualityStatus.PASS.value if passed else WaterQualityStatus.FAIL.value
            )
            route = document.get("route")
            latched = bool(document.get("quality_latched"))

            if passed:
                if not latched:
                    route = CondensateRoute.RECOVER.value
            else:
                # 越界即切排放并锁定
                route = CondensateRoute.DIVERT.value
                latched = True

            return merge_documents(
                document,
                [
                    ("quality_status", status),
                    ("quality_latched", latched),
                    ("route", route),
                    ("last_sample_id", sample_id),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"recovery:{batch_id}"):
            self.runs.update(batch_id, mutate)
            run = self.runs.require(batch_id)
        _, violations = self.evaluate_quality(clean_sample)
        context = {
            "sample_id": run.get("last_sample_id"),
            "passed": not violations,
            "violations": violations,
            "metrics": clean_sample,
        }
        self._ledger("recovery.quality_sample", batch_id, context)
        return {"run": run, "sample": context}

    def resume(self, batch_id: str) -> dict[str, Any]:
        """水质恢复合格后人工解除排放锁定，恢复回收进水罐。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            self._require_active(document, batch_id)
            if not document.get("quality_latched"):
                raise ConflictError("回收未被水质锁定，无需复位", batch_id=batch_id)
            if document.get("quality_status") != WaterQualityStatus.PASS.value:
                raise ConflictError(
                    "水质尚未恢复合格，禁止解除排放锁定",
                    batch_id=batch_id,
                    quality_status=document.get("quality_status"),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("quality_latched", False),
                    ("route", CondensateRoute.RECOVER.value),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"recovery:{batch_id}"):
            updated = self.runs.update(batch_id, mutate)
        self._ledger("recovery.resumed", batch_id, {"run_id": updated["id"]})
        return updated

    # ---------------------------------------------------------------- 计量

    def record_condensate(self, batch_id: str, totalizer_kg: float) -> dict[str, Any]:
        """录入蒸汽夹套冷凝水累计流量表读数（单调累计 kg）。

        按读数增量与当前阀门去向（回收/排放）分桶累计。
        """

        total = require_number(totalizer_kg, field="totalizer_kg", minimum=0.0, maximum=1.0e10)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            self._require_active(document, batch_id)
            previous = float(document.get("condensate_total_kg", 0.0))
            if total + 1e-6 < previous:
                raise ValidationError(
                    "冷凝水累计表读数不能倒退",
                    previous_kg=previous,
                    reading_kg=total,
                )
            delta = max(0.0, total - previous)
            route = str(document.get("route"))
            recovered = float(document.get("condensate_recovered_kg", 0.0))
            diverted = float(document.get("condensate_diverted_kg", 0.0))
            if route == CondensateRoute.RECOVER.value:
                recovered += delta
            else:
                diverted += delta
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("condensate_total_kg", round(total, 3)),
                    ("condensate_recovered_kg", round(recovered, 3)),
                    ("condensate_diverted_kg", round(diverted, 3)),
                    ("updated_at", now),
                ],
            ), delta

        with self.store.locks.guard(f"recovery:{batch_id}"):
            captured: dict[str, float] = {}

            def wrap(doc: dict[str, Any]) -> dict[str, Any]:
                out, delta = mutate(doc)
                captured["delta"] = delta
                captured["route"] = out.get("route")
                return out

            updated = self.runs.update(batch_id, wrap)
            delta = captured.get("delta", 0.0)
            route = captured.get("route")
        self._ledger(
            "recovery.condensate_meter",
            batch_id,
            {"totalizer_kg": round(total, 3), "delta_kg": round(delta, 3), "route": route},
        )
        return updated

    def record_hotwater(
        self,
        batch_id: str,
        totalizer_kg: float,
        temp_in_c: float,
        temp_out_c: float,
    ) -> dict[str, Any]:
        """录入热水侧累计流量与进/出口温度，增量积分回收热量（权威计量）。"""

        total = require_number(totalizer_kg, field="totalizer_kg", minimum=0.0, maximum=1.0e10)
        t_in = require_number(temp_in_c, field="temp_in_c", minimum=0.0, maximum=150.0)
        t_out = require_number(temp_out_c, field="temp_out_c", minimum=0.0, maximum=150.0)
        if t_out + 1e-9 < t_in:
            raise ValidationError("热水出口温度不能低于入口温度", temp_in_c=t_in, temp_out_c=t_out)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            self._require_active(document, batch_id)
            previous = float(document.get("hotwater_mass_kg", 0.0))
            if total + 1e-6 < previous:
                raise ValidationError(
                    "热水累计表读数不能倒退", previous_kg=previous, reading_kg=total
                )
            delta_mass = max(0.0, total - previous)
            delta_heat = water_heat_kj(delta_mass, t_out, t_in)
            energy = float(document.get("energy_recovered_kj", 0.0)) + delta_heat
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("hotwater_mass_kg", round(total, 3)),
                    ("energy_recovered_kj", round(energy, 3)),
                    ("last_hot_in_c", t_in),
                    ("last_hot_out_c", t_out),
                    ("updated_at", now),
                ],
            ), {"delta_mass_kg": delta_mass, "delta_heat_kj": delta_heat}

        with self.store.locks.guard(f"recovery:{batch_id}"):
            captured: dict[str, float] = {}

            def wrap(doc: dict[str, Any]) -> dict[str, Any]:
                out, info = mutate(doc)
                captured.update(info)
                return out

            updated = self.runs.update(batch_id, wrap)
        self._ledger(
            "recovery.hotwater_meter",
            batch_id,
            {
                "totalizer_kg": round(total, 3),
                "temp_in_c": t_in,
                "temp_out_c": t_out,
                "delta_mass_kg": round(captured.get("delta_mass_kg", 0.0), 3),
                "delta_heat_kj": round(captured.get("delta_heat_kj", 0.0), 3),
                "energy_recovered_kj": updated["energy_recovered_kj"],
            },
        )
        return updated

    def record_vapor_condensate(self, batch_id: str, totalizer_kg: float) -> dict[str, Any]:
        """录入二次蒸汽凝结水累计量（二次水，仅作热源记录，默认不进水罐）。"""

        total = require_number(totalizer_kg, field="totalizer_kg", minimum=0.0, maximum=1.0e10)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            self._require_active(document, batch_id)
            previous = float(document.get("vapor_condensate_kg", 0.0))
            if total + 1e-6 < previous:
                raise ValidationError(
                    "二次水累计表读数不能倒退", previous_kg=previous, reading_kg=total
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document, [("vapor_condensate_kg", round(total, 3)), ("updated_at", now)]
            )

        with self.store.locks.guard(f"recovery:{batch_id}"):
            updated = self.runs.update(batch_id, mutate)
        self._ledger(
            "recovery.vapor_meter", batch_id, {"totalizer_kg": round(total, 3)}
        )
        return updated

    # ---------------------------------------------------------------- 查询

    def get(self, batch_id: str) -> dict[str, Any]:
        document = self.runs.get(batch_id)
        if document is None:
            raise NotFoundError("余热回收运行不存在", batch_id=batch_id)
        return document

    def all_runs(self) -> list[dict[str, Any]]:
        return self.runs.all()

    # ---------------------------------------------------------------- 内部

    def _require_active(self, document: dict[str, Any], batch_id: str) -> None:
        if document.get("stage") != RecoveryStage.ACTIVE.value:
            raise SequenceError(
                "余热回收运行未在进行中", batch_id=batch_id, stage=document.get("stage")
            )

    def _clean_sample(self, sample: dict[str, Any]) -> dict[str, float]:
        if not isinstance(sample, dict) or not sample:
            raise ValidationError("水质采样不能为空", fields=list(_QUALITY_FIELDS))
        clean: dict[str, float] = {}
        for key, (lo, hi) in _QUALITY_FIELDS.items():
            if key in sample and sample[key] is not None:
                clean[key] = require_number(sample[key], field=key, minimum=lo, maximum=hi)
        unknown = set(sample) - set(_QUALITY_FIELDS)
        if unknown:
            raise ValidationError("存在不支持的水质指标", unknown=sorted(unknown))
        return clean

    def _ledger(self, kind: str, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"batch_id": batch_id, "run_id": None}
        run = self.runs.get(batch_id)
        if run is not None:
            body["run_id"] = run.get("id")
        body.update(payload)
        return self.store.append_event(kind, body)
