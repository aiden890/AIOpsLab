"""Unit tests for static dataset v2.

Tests core components: StaticApp, dynamic registry, evaluator, actions, orchestrator.
No LLM or Docker dependency required.
"""

import sys
import os
import tempfile
import shutil

import pytest
import pandas as pd

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestStaticApp:
    """Test service/static_app.py - the telemetry service client."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        ns_dir = os.path.join(self.tmpdir, "static-bank")
        os.makedirs(os.path.join(ns_dir, "logs"))
        os.makedirs(os.path.join(ns_dir, "metrics"))
        os.makedirs(os.path.join(ns_dir, "traces"))

        log_df = pd.DataFrame({
            "timestamp": [1000.0, 1001.0, 1002.0],
            "cmdb_id": ["Mysql02", "WebServer01", "Mysql02"],
            "log_level": ["ERROR", "INFO", "WARN"],
            "message": ["Connection failed", "Request ok", "Slow query"],
        })
        log_df.to_csv(os.path.join(ns_dir, "logs", "log_service.csv"), index=False)

        metric_df = pd.DataFrame({
            "timestamp": [1000.0, 1001.0, 1002.0],
            "metric_name": ["cpu_usage", "mem_usage", "cpu_usage"],
            "value": [0.85, 0.72, 0.91],
        })
        metric_df.to_csv(os.path.join(ns_dir, "metrics", "metric_app.csv"), index=False)

        trace_df = pd.DataFrame({
            "timestamp": [1000.0, 1001.0],
            "trace_id": ["t1", "t2"],
            "span_id": ["s1", "s2"],
            "service": ["frontend", "backend"],
            "duration": [150.0, 200.0],
        })
        trace_df.to_csv(os.path.join(ns_dir, "traces", "trace_span.csv"), index=False)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_get_logs(self):
        from aiopslab.service.static_app import StaticApp
        app = StaticApp(self.tmpdir)
        result = app.get_logs("static-bank")
        assert "Mysql02" in result
        assert "Connection failed" in result

    def test_get_logs_filter_service(self):
        from aiopslab.service.static_app import StaticApp
        app = StaticApp(self.tmpdir)
        result = app.get_logs("static-bank", service="Mysql02")
        assert "Mysql02" in result
        assert "WebServer01" not in result

    def test_get_logs_missing_namespace(self):
        from aiopslab.service.static_app import StaticApp
        app = StaticApp(self.tmpdir)
        result = app.get_logs("nonexistent")
        assert "Error" in result

    def test_get_metrics(self):
        from aiopslab.service.static_app import StaticApp
        app = StaticApp(self.tmpdir)
        result = app.get_metrics("static-bank", duration_minutes=999999)
        assert "cpu_usage" in result

    def test_get_traces(self):
        from aiopslab.service.static_app import StaticApp
        app = StaticApp(self.tmpdir)
        result = app.get_traces("static-bank", duration_minutes=999999)
        assert "frontend" in result

    def test_store_telemetry(self):
        from aiopslab.service.static_app import StaticApp
        app = StaticApp(self.tmpdir)
        df = pd.DataFrame({
            "timestamp": [2000.0, 2001.0],
            "metric_name": ["new_metric", "new_metric"],
            "value": [1.0, 2.0],
        })
        count = app.store_telemetry("static-bank", "metrics", df)
        assert count == 2

    def test_clear_telemetry(self):
        from aiopslab.service.static_app import StaticApp
        app = StaticApp(self.tmpdir)
        app.clear_telemetry("static-bank")
        assert not os.path.exists(os.path.join(self.tmpdir, "static-bank"))


class TestStaticProblemRegistry:
    """Test dynamic problem registry loaded from query.csv."""

    def test_registry_loads(self):
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        assert len(registry.PROBLEM_REGISTRY) > 0

    def test_registry_has_openrca_bank_problems(self):
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        bank_ids = registry.get_problem_ids(dataset="openrca_bank")
        assert len(bank_ids) > 0
        # Bank has ~120 queries
        assert len(bank_ids) >= 100

    def test_registry_has_openrca_telecom_problems(self):
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        telecom_ids = registry.get_problem_ids(dataset="openrca_telecom")
        assert len(telecom_ids) > 0

    def test_registry_total_problem_count(self):
        """All 4 datasets together should have ~330 problems."""
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        total = registry.get_problem_count()
        assert total > 200  # Should be ~330

    def test_registry_filter_by_task_type(self):
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        task1_ids = registry.get_problem_ids(task_type="task_1")
        assert len(task1_ids) > 0
        assert all("task_1" in pid for pid in task1_ids)

    def test_registry_filter_by_dataset(self):
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        bank_ids = registry.get_problem_ids(dataset="openrca_bank")
        assert all(pid.startswith("openrca_bank") for pid in bank_ids)

    def test_registry_problem_id_format(self):
        """Problem IDs should be: {dataset}-{task_type}-{index}."""
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        ids = registry.get_problem_ids()
        for pid in ids:
            parts = pid.split("-")
            assert len(parts) >= 3
            assert "task_" in pid

    def test_all_docker_deployment(self):
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        registry = StaticProblemRegistry()
        for pid in registry.get_problem_ids()[:5]:  # Sample first 5
            assert registry.get_problem_deployment(pid) == "docker"


class TestOpenRCAEvaluator:
    """Test the OpenRCA evaluation logic."""

    def test_perfect_score_task1(self):
        """task_1: datetime only."""
        from aiopslab.orchestrator.evaluators.openrca_eval import openrca_evaluate
        prediction = '{"1": {"root cause occurrence datetime": "2021-03-04 14:57:00", "root cause component": "", "root cause reason": ""}}'
        scoring = "The only root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-04 14:57:00"
        passing, failing, score = openrca_evaluate(prediction, scoring)
        assert score == 1.0
        assert len(failing) == 0

    def test_perfect_score_task3(self):
        """task_3: component only."""
        from aiopslab.orchestrator.evaluators.openrca_eval import openrca_evaluate
        prediction = '{"1": {"root cause occurrence datetime": "", "root cause component": "Mysql02", "root cause reason": ""}}'
        scoring = "The only predicted root cause component is Mysql02"
        passing, failing, score = openrca_evaluate(prediction, scoring)
        assert score == 1.0

    def test_perfect_score_task7(self):
        """task_7: all three fields."""
        from aiopslab.orchestrator.evaluators.openrca_eval import openrca_evaluate
        prediction = '{"1": {"root cause occurrence datetime": "2021-03-04 14:57:00", "root cause component": "Mysql02", "root cause reason": "high memory usage"}}'
        scoring = (
            "The only root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-04 14:57:00\n"
            "The only predicted root cause component is Mysql02\n"
            "The only predicted root cause reason is high memory usage"
        )
        passing, failing, score = openrca_evaluate(prediction, scoring)
        assert score == 1.0
        assert len(failing) == 0

    def test_wrong_component(self):
        from aiopslab.orchestrator.evaluators.openrca_eval import openrca_evaluate
        prediction = '{"1": {"root cause occurrence datetime": "", "root cause component": "WrongService", "root cause reason": ""}}'
        scoring = "The only predicted root cause component is Mysql02"
        passing, failing, score = openrca_evaluate(prediction, scoring)
        assert score == 0.0
        assert len(failing) > 0

    def test_time_within_1min(self):
        from aiopslab.orchestrator.evaluators.openrca_eval import _time_within_1min
        assert _time_within_1min("2021-03-04 14:57:00", "2021-03-04 14:57:30")
        assert _time_within_1min("2021-03-04 14:57:00", "2021-03-04 14:56:30")
        assert not _time_within_1min("2021-03-04 14:57:00", "2021-03-04 14:55:00")

    def test_empty_prediction(self):
        from aiopslab.orchestrator.evaluators.openrca_eval import openrca_evaluate
        prediction = "{}"
        scoring = "The only predicted root cause component is Mysql02"
        passing, failing, score = openrca_evaluate(prediction, scoring)
        assert score == 0.0

    def test_task_difficulty(self):
        from aiopslab.orchestrator.evaluators.openrca_eval import get_task_difficulty
        assert get_task_difficulty("task_1") == "easy"
        assert get_task_difficulty("task_3") == "easy"
        assert get_task_difficulty("task_4") == "middle"
        assert get_task_difficulty("task_6") == "middle"
        assert get_task_difficulty("task_7") == "hard"

    def test_partial_score_task7(self):
        """task_7 with only 2/3 correct should get partial score."""
        from aiopslab.orchestrator.evaluators.openrca_eval import openrca_evaluate
        prediction = '{"1": {"root cause occurrence datetime": "2021-03-04 14:57:00", "root cause component": "Mysql02", "root cause reason": "wrong reason"}}'
        scoring = (
            "The only root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-04 14:57:00\n"
            "The only predicted root cause component is Mysql02\n"
            "The only predicted root cause reason is high memory usage"
        )
        passing, failing, score = openrca_evaluate(prediction, scoring)
        assert 0.0 < score < 1.0
        assert round(score, 2) == 0.67


class TestBaseOrchestrator:
    """Test orchestrator base class extraction."""

    def test_base_orchestrator_imports(self):
        from aiopslab.orchestrator.base import BaseOrchestrator
        assert BaseOrchestrator is not None

    def test_orchestrator_inherits_base(self):
        from aiopslab.orchestrator.base import BaseOrchestrator
        from aiopslab.orchestrator.orchestrator import Orchestrator
        assert issubclass(Orchestrator, BaseOrchestrator)

    def test_static_orchestrator_inherits_base(self):
        from aiopslab.orchestrator.base import BaseOrchestrator
        from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
        assert issubclass(StaticOrchestrator, BaseOrchestrator)

    def test_static_orchestrator_uses_static_registry(self):
        from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
        from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
        orch = StaticOrchestrator()
        assert isinstance(orch.probs, StaticProblemRegistry)


class TestStaticActions:
    """Test static actions."""

    def test_static_rca_actions_import(self):
        from aiopslab.orchestrator.static_actions.rca import StaticRCAActions
        assert StaticRCAActions is not None

    def test_static_rca_actions_has_submit(self):
        from aiopslab.orchestrator.static_actions.rca import StaticRCAActions
        assert hasattr(StaticRCAActions, "submit")
        assert getattr(StaticRCAActions.submit, "is_action", False)

    def test_static_task_actions_has_methods(self):
        from aiopslab.orchestrator.static_actions.base import StaticTaskActions
        actions = StaticTaskActions.__dict__
        assert "get_logs" in actions
        assert "get_metrics" in actions
        assert "get_traces" in actions
        assert "read_logs" in actions
        assert "read_metrics" in actions
        assert "read_traces" in actions
        assert "exec_shell" in actions

    def test_static_detection_actions_backward_compat(self):
        """detection.py still exists for backward compatibility."""
        from aiopslab.orchestrator.static_actions.detection import StaticDetectionActions
        assert StaticDetectionActions is not None


class TestOpenRCATask:
    """Test the unified OpenRCA task class."""

    def test_openrca_task_import(self):
        from aiopslab.orchestrator.tasks.openrca_task import OpenRCATask
        assert OpenRCATask is not None

    def test_openrca_task_inherits_task(self):
        from aiopslab.orchestrator.tasks.openrca_task import OpenRCATask
        from aiopslab.orchestrator.tasks.base import Task
        assert issubclass(OpenRCATask, Task)


class TestOpenRCAProblemClasses:
    """Test the problem classes (without instantiating — requires dataset)."""

    def test_bank_problem_import(self):
        from aiopslab.orchestrator.static_problems.openrca.bank import OpenRCABankProblem
        assert OpenRCABankProblem is not None

    def test_telecom_problem_import(self):
        from aiopslab.orchestrator.static_problems.openrca.telecom import OpenRCATelecomProblem
        assert OpenRCATelecomProblem is not None

    def test_market_cb1_problem_import(self):
        from aiopslab.orchestrator.static_problems.openrca.market_cloudbed1 import OpenRCAMarketCB1Problem
        assert OpenRCAMarketCB1Problem is not None

    def test_market_cb2_problem_import(self):
        from aiopslab.orchestrator.static_problems.openrca.market_cloudbed2 import OpenRCAMarketCB2Problem
        assert OpenRCAMarketCB2Problem is not None

    def test_bank_problem_inherits_correctly(self):
        from aiopslab.orchestrator.static_problems.openrca.bank import OpenRCABankProblem
        from aiopslab.orchestrator.static_problems.openrca.base_task import OpenRCABaseTask
        from aiopslab.orchestrator.tasks.openrca_task import OpenRCATask
        assert issubclass(OpenRCABankProblem, OpenRCABaseTask)
        assert issubclass(OpenRCABankProblem, OpenRCATask)


class TestTimeRemapperReplay:
    """Test TimeRemapper with replay (pre/post buffer) settings."""

    def _make_query_info(self, start_ts, duration):
        from aiopslab.service.apps.static_dataset.time_mapping.base_query_parser import QueryResult
        return QueryResult(
            task_id="task_1",
            time_range={
                "start": start_ts,
                "end": start_ts + duration,
                "start_str": "2021-03-04 14:30:00",
                "end_str": "2021-03-04 15:00:00",
                "duration": duration,
            },
            faults=[],
            metadata={},
        )

    def test_time_offset_is_zero(self):
        """time_offset should always be 0 (no timestamp conversion)."""
        from aiopslab.service.apps.static_dataset.time_mapping.time_remapper import TimeRemapper

        fault_start = 1614868200  # 2021-03-04 14:30:00 UTC
        query_info = self._make_query_info(fault_start, 1800)

        config = {
            "time_mapping": {"anchor_strategy": "fault_start"},
            "replay": {
                "pre_buffer_minutes": 30,
                "post_buffer_minutes": 30,
            },
        }

        remapper = TimeRemapper(config, query_info)
        assert remapper.mapping["time_offset"] == 0

    def test_init_window_bounds(self):
        """init_start/end should reflect pre/post buffer around anchor."""
        from aiopslab.service.apps.static_dataset.time_mapping.time_remapper import TimeRemapper

        fault_start = 1614868200
        query_info = self._make_query_info(fault_start, 1800)

        config = {
            "time_mapping": {"anchor_strategy": "fault_start"},
            "replay": {
                "pre_buffer_minutes": 30,
                "post_buffer_minutes": 30,
            },
        }

        remapper = TimeRemapper(config, query_info)
        m = remapper.mapping

        assert m["init_start_original"] == fault_start - 1800
        assert m["init_end_original"] == fault_start + 1800

    def test_fault_window(self):
        """Fault window should match original time range."""
        from aiopslab.service.apps.static_dataset.time_mapping.time_remapper import TimeRemapper

        fault_start = 1614868200
        duration = 1800
        query_info = self._make_query_info(fault_start, duration)

        config = {
            "time_mapping": {"anchor_strategy": "fault_start"},
            "replay": {
                "pre_buffer_minutes": 30,
                "post_buffer_minutes": 30,
            },
        }

        remapper = TimeRemapper(config, query_info)
        assert remapper.mapping["fault_start"] == fault_start
        assert remapper.mapping["fault_end"] == fault_start + duration
        assert remapper.is_in_fault_window(fault_start + 100)
        assert not remapper.is_in_fault_window(fault_start - 100)

    def test_replay_config_in_dataset_config(self):
        """Dataset configs should have replay section with buffer settings."""
        import json
        from pathlib import Path

        config_dir = Path(__file__).parent.parent / "aiopslab" / "service" / "apps" / "static_dataset" / "config"
        for config_file in config_dir.glob("openrca_*.json"):
            with open(config_file) as f:
                config = json.load(f)
            assert "replay" in config, f"Missing replay in {config_file.name}"
            replay = config["replay"]
            assert "pre_buffer_minutes" in replay
            assert "post_buffer_minutes" in replay
            assert "streaming_interval_seconds" in replay


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
