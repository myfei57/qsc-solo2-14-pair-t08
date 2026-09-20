"""余热回收热力学与水质门控、批次核算。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import ConflictError, SequenceError, ValidationError
from breweryctl.domain.thermo import (
    flash_fraction,
    h_f,
    h_fg,
    heat_balance,
    settle_recovery,
)

from .helpers import StepClock, create_batch, make_app


class ThermoTest(unittest.TestCase):
    def test_saturated_enthalpy_at_100c(self) -> None:
        self.assertAlmostEqual(h_f(100.0), 415.4, delta=2.0)
        self.assertAlmostEqual(h_fg(100.0), 2257.0, delta=2.0)

    def test_flash_fraction_monotonic_and_bounded(self) -> None:
        self.assertEqual(flash_fraction(100.0, 100.0), 0.0)
        fraction = flash_fraction(143.6, 100.0)
        self.assertGreater(fraction, 0.0)
        self.assertLess(fraction, 0.2)
        self.assertGreater(fraction, flash_fraction(120.0, 100.0))

    def test_heat_balance_recovers_more_water_at_higher_capture(self) -> None:
        common = dict(
            condensate_kg=1000.0,
            supply_temp_c=143.6,
            flash_temp_c=100.0,
            output_temp_c=80.0,
        )
        low = heat_balance(vapor_kg=800.0, vapor_capture_ratio=0.5, **common)
        high = heat_balance(vapor_kg=800.0, vapor_capture_ratio=1.0, **common)
        self.assertGreater(high["hot_water_kg"], low["hot_water_kg"])
        self.assertGreater(high["available_heat_mj"], 0.0)

    def test_hlt_capacity_caps_utilized_heat(self) -> None:
        balance = heat_balance(
            vapor_kg=800.0,
            vapor_capture_ratio=0.95,
            condensate_kg=1000.0,
            supply_temp_c=143.6,
            flash_temp_c=100.0,
            output_temp_c=80.0,
            hlt_capacity_kg=1000.0,
        )
        self.assertTrue(balance["hlt_capped"])
        self.assertEqual(balance["hot_water_kg"], 1000.0)
        self.assertGreater(balance["surplus_heat_mj"], 0.0)

    def test_settlement_water_credit_only_when_quality_passes(self) -> None:
        balance = heat_balance(
            vapor_kg=800.0,
            vapor_capture_ratio=0.95,
            condensate_kg=1000.0,
            supply_temp_c=143.6,
            flash_temp_c=100.0,
            output_temp_c=80.0,
        )
        passed = settle_recovery(
            balance,
            vapor_kg=800.0,
            condensate_kg=1000.0,
            water_quality_passed=True,
            batches_per_year=300,
        )
        failed = settle_recovery(
            balance,
            vapor_kg=800.0,
            condensate_kg=1000.0,
            water_quality_passed=False,
            batches_per_year=300,
        )
        # 热量收益与水质无关（间壁换热照常）
        self.assertEqual(
            passed["batch"]["steam_saved_t"], failed["batch"]["steam_saved_t"]
        )
        self.assertGreater(passed["batch"]["water_reused_t"], 0.0)
        self.assertEqual(failed["batch"]["water_reused_t"], 0.0)
        self.assertGreater(failed["batch"]["water_diverted_t"], 0.0)
        self.assertGreater(passed["annual"]["total_saved"], failed["annual"]["total_saved"])


class RecoveryFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.recovery = self.app.registry.recovery
        self.unit_id = str(self.recovery.units.all()[0]["id"])
        self.batch_id = create_batch(self.app)

    def _arm(self) -> dict:
        return self.recovery.arm(self.unit_id, self.batch_id, 800.0, 1000.0)

    def test_unit_seeded_on_bootstrap(self) -> None:
        self.assertEqual(1, self.recovery.units.count())

    def test_arm_computes_theoretical_balance(self) -> None:
        run = self._arm()
        self.assertEqual("armed", run["stage"])
        self.assertGreater(run["balance"]["available_heat_mj"], 0.0)
        self.assertIn("flash_fraction", run["balance"])

    def test_cannot_settle_before_capture(self) -> None:
        self._arm()
        with self.assertRaises(SequenceError):
            self.recovery.settle(
                self.batch_id,
                "tester",
                {
                    "vapor_kg": 700.0,
                    "condensate_kg": 950.0,
                    "water_to_hlt_kg": 3000.0,
                    "hlt_temp_c": 78.0,
                },
            )

    def test_duplicate_active_arm_rejected(self) -> None:
        self._arm()
        with self.assertRaises(ConflictError):
            self._arm()

    def test_failed_quality_diverts_water_but_keeps_heat(self) -> None:
        self._arm()
        self.recovery.start_capture(self.batch_id)
        self.recovery.record_quality(
            self.unit_id,
            "tester",
            {"conductivity_us_cm": 320.0, "ph": 7.1, "hardness_mg_l": 45.0},
            batch_id=self.batch_id,
        )
        run = self.recovery.settle(
            self.batch_id,
            "tester",
            {
                "vapor_kg": 700.0,
                "condensate_kg": 950.0,
                "water_to_hlt_kg": 3000.0,
                "water_diverted_kg": 1600.0,
                "hlt_temp_c": 78.0,
                "cold_temp_c": 15.0,
            },
        )
        self.assertEqual("settled", run["stage"])
        self.assertEqual("diverted", run["route"])
        self.assertEqual("quality_failed", run["diverted_reason"])
        # 热量照算（间壁换热），水回用为零
        self.assertGreater(run["settlement"]["steam_saved_t"], 0.0)
        self.assertEqual(0.0, run["settlement"]["water_reused_t"])
        self.assertGreater(run["settlement"]["water_diverted_t"], 0.0)
        # 闩锁告警已产生
        alarms = self.app.registry.alarms.list_alarms(status="active")
        self.assertTrue(any(a["code"] == "recovery_water_quality_failed" for a in alarms))

    def test_passed_quality_allows_direct_reuse(self) -> None:
        self._arm()
        self.recovery.start_capture(self.batch_id)
        check = self.recovery.record_quality(
            self.unit_id,
            "tester",
            {"conductivity_us_cm": 40.0, "ph": 7.2, "hardness_mg_l": 30.0},
            batch_id=self.batch_id,
        )
        self.assertEqual("passed", check["status"])
        run = self.recovery.settle(
            self.batch_id,
            "tester",
            {
                "vapor_kg": 700.0,
                "condensate_kg": 950.0,
                "water_to_hlt_kg": 1600.0,
                "water_diverted_kg": 0.0,
                "hlt_temp_c": 78.0,
                "cold_temp_c": 15.0,
            },
        )
        self.assertEqual("direct", run["route"])
        self.assertEqual(1.6, run["settlement"]["water_reused_t"])
        self.assertEqual(0.0, run["settlement"]["water_diverted_t"])

    def test_force_divert_overrides_passing_quality(self) -> None:
        self._arm()
        self.recovery.start_capture(self.batch_id)
        self.recovery.record_quality(
            self.unit_id,
            "tester",
            {"conductivity_us_cm": 40.0},
            batch_id=self.batch_id,
        )
        run = self.recovery.settle(
            self.batch_id,
            "tester",
            {
                "vapor_kg": 700.0,
                "condensate_kg": 950.0,
                "water_to_hlt_kg": 1600.0,
                "hlt_temp_c": 78.0,
            },
            force_divert=True,
        )
        self.assertEqual("diverted", run["route"])
        self.assertEqual("operator_force_divert", run["diverted_reason"])

    def test_missing_quality_test_diverts(self) -> None:
        self._arm()
        self.recovery.start_capture(self.batch_id)
        run = self.recovery.settle(
            self.batch_id,
            "tester",
            {
                "vapor_kg": 700.0,
                "condensate_kg": 950.0,
                "water_to_hlt_kg": 1600.0,
                "hlt_temp_c": 78.0,
            },
        )
        self.assertEqual("diverted", run["route"])
        self.assertEqual("quality_not_tested", run["diverted_reason"])

    def test_report_and_ledger_accumulate_settled_batches(self) -> None:
        self._arm()
        self.recovery.start_capture(self.batch_id)
        self.recovery.record_quality(
            self.unit_id, "tester", {"conductivity_us_cm": 40.0}, batch_id=self.batch_id
        )
        self.recovery.settle(
            self.batch_id,
            "tester",
            {
                "vapor_kg": 700.0,
                "condensate_kg": 950.0,
                "water_to_hlt_kg": 1600.0,
                "hlt_temp_c": 78.0,
            },
        )
        report = self.recovery.report(self.unit_id)
        self.assertEqual(1, report["settled_batches"])
        self.assertEqual(1, report["direct_reuse_batches"])
        self.assertGreater(report["totals"]["water_reused_t"], 0.0)
        self.assertIn("2026-01", report["monthly"])
        self.assertEqual(1, report["monthly"]["2026-01"]["batches"])
        ledger = self.recovery.ledger(self.unit_id)
        self.assertEqual(1, len(ledger))
        self.assertEqual("direct", ledger[0]["route"])

    def test_estimate_returns_both_quality_scenarios(self) -> None:
        estimate = self.recovery.estimate(self.unit_id, 800.0, 1000.0)
        self.assertGreater(
            estimate["quality_passed"]["annual"]["total_saved"],
            estimate["quality_failed"]["annual"]["total_saved"],
        )

    def test_settle_rejects_inverted_temperatures(self) -> None:
        self._arm()
        self.recovery.start_capture(self.batch_id)
        with self.assertRaises(ValidationError):
            self.recovery.settle(
                self.batch_id,
                "tester",
                {
                    "vapor_kg": 700.0,
                    "condensate_kg": 950.0,
                    "water_to_hlt_kg": 100.0,
                    "hlt_temp_c": 10.0,
                    "cold_temp_c": 20.0,
                },
            )

    def test_quality_requires_at_least_one_metric(self) -> None:
        with self.assertRaises(ValidationError):
            self.recovery.record_quality(self.unit_id, "tester", {})


if __name__ == "__main__":
    unittest.main()
