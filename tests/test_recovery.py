"""余热回收：水质门限、阀门联锁与回收量核算。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import ConflictError, SequenceError, ValidationError
from breweryctl.core.heat import (
    SPECIFIC_HEAT_WATER_KJ_KG_K,
    reuse_ratio,
    steam_equivalent_kg,
    water_heat_kj,
)

from .helpers import create_batch, make_app


GOOD_SAMPLE = {"conductivity_us_cm": 200.0, "ph": 7.2, "hardness_mg_l": 5.0, "oil_mg_l": 0.1}


class RecoveryFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.recovery = self.app.registry.recovery
        self.batch_id = create_batch(self.app)
        self.recovery.start(self.batch_id, "tester")

    def test_starts_diverting_until_quality_passes(self) -> None:
        report = self.recovery.report(self.batch_id)
        self.assertEqual("divert", report["route"])
        self.assertEqual("unknown", report["quality_status"])
        report = self.recovery.sample_quality(self.batch_id, GOOD_SAMPLE, "tester")
        self.assertEqual("recover", report["route"])
        self.assertEqual("pass", report["quality_status"])

    def test_bad_quality_diverts_and_latches(self) -> None:
        self.recovery.sample_quality(self.batch_id, GOOD_SAMPLE, "tester")
        bad = dict(GOOD_SAMPLE, conductivity_us_cm=5000.0)
        report = self.recovery.sample_quality(self.batch_id, bad, "tester")
        self.assertEqual("divert", report["route"])
        self.assertTrue(report["quality_latched"])
        # 锁定后即使来一次合格水也不会自动恢复回收
        report = self.recovery.sample_quality(self.batch_id, GOOD_SAMPLE, "tester")
        self.assertEqual("divert", report["route"])
        self.assertTrue(report["awaiting_reset"])
        report = self.recovery.resume(self.batch_id, "tester")
        self.assertEqual("recover", report["route"])
        self.assertFalse(report["quality_latched"])

    def test_cannot_resume_while_still_failing(self) -> None:
        bad = dict(GOOD_SAMPLE, ph=11.0)
        self.recovery.sample_quality(self.batch_id, bad, "tester")
        with self.assertRaises(ConflictError):
            self.recovery.resume(self.batch_id, "tester")

    def test_condensate_is_split_by_route(self) -> None:
        # 前 300kg 在直排（水质未知）期间
        self.recovery.record_condensate(self.batch_id, 300.0)
        self.recovery.sample_quality(self.batch_id, GOOD_SAMPLE, "tester")
        # 合格后 700kg 回收进水罐
        self.recovery.record_condensate(self.batch_id, 1000.0)
        report = self.recovery.report(self.batch_id)
        self.assertEqual(1000.0, report["condensate_total_kg"])
        self.assertEqual(700.0, report["condensate_recovered_kg"])
        self.assertEqual(300.0, report["condensate_diverted_kg"])
        self.assertAlmostEqual(0.7, report["reuse_ratio"], places=6)

    def test_hotwater_meter_accumulates_recovered_heat(self) -> None:
        # 两段加热，验证增量积分
        self.recovery.record_hotwater(self.batch_id, 1000.0, 15.0, 60.0)
        self.recovery.record_hotwater(self.batch_id, 2000.0, 20.0, 85.0)
        report = self.recovery.report(self.batch_id)
        expected = water_heat_kj(1000.0, 60.0, 15.0) + water_heat_kj(1000.0, 85.0, 20.0)
        self.assertAlmostEqual(expected, report["energy_recovered_kj"], places=2)
        self.assertAlmostEqual(
            steam_equivalent_kg(expected), report["steam_equivalent_kg"], places=2
        )

    def test_totalizer_cannot_go_backwards(self) -> None:
        self.recovery.record_hotwater(self.batch_id, 1000.0, 15.0, 60.0)
        with self.assertRaises(ValidationError):
            self.recovery.record_hotwater(self.batch_id, 900.0, 15.0, 60.0)
        self.recovery.record_condensate(self.batch_id, 500.0)
        with self.assertRaises(ValidationError):
            self.recovery.record_condensate(self.batch_id, 499.0)

    def test_hot_out_below_in_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.recovery.record_hotwater(self.batch_id, 100.0, 80.0, 40.0)

    def test_close_freezes_run_and_diverts(self) -> None:
        self.recovery.sample_quality(self.batch_id, GOOD_SAMPLE, "tester")
        self.recovery.record_condensate(self.batch_id, 100.0)
        report = self.recovery.close(self.batch_id, "tester")
        self.assertEqual("closed", report["stage"])
        self.assertEqual("divert", report["route"])
        with self.assertRaises(SequenceError):
            self.recovery.record_condensate(self.batch_id, 200.0)

    def test_ledger_aggregates_across_batches(self) -> None:
        self.recovery.sample_quality(self.batch_id, GOOD_SAMPLE, "tester")
        self.recovery.record_condensate(self.batch_id, 500.0)
        self.recovery.record_hotwater(self.batch_id, 500.0, 20.0, 80.0)
        other = create_batch(self.app)
        self.recovery.start(other, "tester")
        self.recovery.record_condensate(other, 250.0)  # 直排
        ledger = self.recovery.ledger()
        self.assertEqual(2, ledger["runs"])
        self.assertEqual(500.0, ledger["condensate_recovered_kg"])
        self.assertEqual(250.0, ledger["condensate_diverted_kg"])
        self.assertAlmostEqual(
            500.0 / 750.0, ledger["reuse_ratio"], places=6
        )
        expected_energy = water_heat_kj(500.0, 80.0, 20.0)
        self.assertAlmostEqual(expected_energy, ledger["energy_recovered_kj"], places=2)

    def test_quality_failure_raises_latching_alarm(self) -> None:
        bad = dict(GOOD_SAMPLE, hardness_mg_l=999.0)
        self.recovery.sample_quality(self.batch_id, bad, "tester")
        alarms = self.app.registry.alarms.list_alarms(status="active")
        codes = [alarm["code"] for alarm in alarms]
        self.assertIn("recovery_water_quality", codes)
        self.assertTrue(all(alarm["latching"] for alarm in alarms if alarm["code"] == "recovery_water_quality"))


class HeatFormulaTest(unittest.TestCase):
    def test_water_heat(self) -> None:
        self.assertAlmostEqual(
            1000.0 * SPECIFIC_HEAT_WATER_KJ_KG_K * 45.0,
            water_heat_kj(1000.0, 60.0, 15.0),
            places=6,
        )

    def test_reuse_ratio(self) -> None:
        self.assertIsNone(reuse_ratio(0.0, 0.0))
        self.assertAlmostEqual(0.25, reuse_ratio(25.0, 100.0), places=6)


if __name__ == "__main__":
    unittest.main()
