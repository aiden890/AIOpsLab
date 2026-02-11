# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""OpenRCA problem implementation."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiopslab.paths import BASE_PARENT_DIR
from aiopslab.orchestrator.static_problems.base import BaseStaticProblem
from aiopslab.service.apps.static_replayer.openrca import OpenRCAStaticApp
from aiopslab.session import SessionItem
from .loader import OpenRCALoader
from .evaluator import OpenRCAEvaluator

logger = logging.getLogger(__name__)


class OpenRCAProblem(BaseStaticProblem):
    """
    Generic OpenRCA problem for any dataset and task.

    Handles multiple instances of the same task type.

    Usage:
        # First instance of task_1 in Bank dataset
        problem = OpenRCAProblem(dataset="Bank", task_index="task_1", instance=0)

        # Second instance of task_6
        problem = OpenRCAProblem(dataset="Bank", task_index="task_6", instance=1)

        # Auto-select random instance
        problem = OpenRCAProblem(dataset="Bank", task_index="task_1")
    """

    def __init__(
        self,
        dataset: str = "Bank",
        task_index: str = "task_1",
        instance: Optional[int] = None
    ):
        """
        Initialize OpenRCA problem.

        Args:
            dataset: Dataset name (Bank, Telecom, Market)
            task_index: Task type (task_1, task_6, etc.)
            instance: Which instance of this task (0, 1, 2...).
                     If None, randomly selects one.
        """
        super().__init__()

        self.dataset = dataset
        self.task_index = task_index
        self.instance = instance
        self.dataset_path = BASE_PARENT_DIR / "openrca_dataset" / dataset

        # Initialize loader
        self.loader = OpenRCALoader(self.dataset_path)

        # Load query.csv to determine instance
        self.loader.load_query_csv()
        task_rows = self.loader.query_df[self.loader.query_df['task_index'] == task_index]

        if len(task_rows) == 0:
            raise ValueError(f"No tasks found with index: {task_index}")

        # Select instance
        if instance is None:
            import random
            self.instance = random.randint(0, len(task_rows) - 1)
            logger.info(f"Auto-selected instance {self.instance} of {len(task_rows)} available")
        elif instance >= len(task_rows):
            raise ValueError(
                f"Instance {instance} not available. "
                f"Only {len(task_rows)} instances of {task_index} exist."
            )

        # Store the specific task row
        self.task_row = task_rows.iloc[self.instance]

        # Load record.csv to get fault_datetime for this task instance
        self.loader.load_record_csv()

        # Get the record row corresponding to this task instance
        # Assuming record rows match query rows by index
        if self.instance >= len(self.loader.record_df):
            raise ValueError(
                f"Record row {self.instance} not found. "
                f"Record.csv has only {len(self.loader.record_df)} rows."
            )

        record_row = self.loader.record_df.iloc[self.instance]
        self.fault_datetime = record_row['datetime']

        logger.info(f"Using fault_datetime from record.csv: {self.fault_datetime}")

        # Create task-specific config file with correct fault_datetime
        import yaml
        import tempfile
        base_config_file = f"aiopslab/service/apps/static_replayer/configs/openrca/{dataset.lower()}.yaml"

        # Load base config
        with open(base_config_file, 'r') as f:
            config = yaml.safe_load(f)

        # Update with task-specific fault_datetime
        if 'time_mapping' not in config:
            config['time_mapping'] = {}
        config['time_mapping']['historical_fault_time'] = self.fault_datetime

        # Create temporary config file
        self.temp_config_file = tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.yaml',
            prefix=f'replayer_config_{dataset}_{task_index}_{instance}_',
            delete=False
        )
        yaml.dump(config, self.temp_config_file)
        self.temp_config_file.close()

        logger.info(f"Created task-specific config: {self.temp_config_file.name}")

        # Initialize app with task-specific config
        self.app = OpenRCAStaticApp(self.temp_config_file.name)

        # Also set in Python config for get_time_mapper()
        self.app.config['dataset']['fault_datetime'] = self.fault_datetime

        logger.info(
            f"OpenRCAProblem initialized: {dataset} {task_index} "
            f"(instance {self.instance}/{len(task_rows)-1})"
        )

    def start_replayer(self, timeout: int = 300):
        """Start replayer and setup time mapper."""
        super().start_replayer(timeout)

        # Setup time mapper
        time_mapper = self.app.get_time_mapper()
        self.loader.set_time_mapper(time_mapper)

        # Load ground truth for this specific instance
        self.load_ground_truth()

    def load_ground_truth(self):
        """Load ground truth for this specific task instance."""
        # Parse time window from THIS instance's instruction
        instruction = self.task_row['instruction']
        start_time, end_time = self.loader.parse_time_window_from_instruction(instruction)

        logger.info("=" * 80)
        logger.info("DEBUG: PROBLEM SETUP")
        logger.info("=" * 80)
        logger.info(f"Query (task_row):")
        logger.info(f"  task_index: {self.task_row['task_index']}")
        logger.info(f"  instruction: {self.task_row['instruction']}")
        logger.info(f"  scoring_points: {self.task_row['scoring_points']}")
        logger.info(f"  time_window: {start_time} to {end_time}")
        logger.info(f"")

        # Get ground truth for this window
        self.ground_truth = self.loader.get_ground_truth(start_time, end_time)

        if self.ground_truth is None:
            logger.error(f"Ground Truth: NOT FOUND")
            logger.error(f"No record found in time window: {start_time} to {end_time}")
            logger.error(f"Available records in record.csv: {len(self.loader.record_df)}")
            if not self.loader.record_df.empty:
                logger.error(f"First record time: {self.loader.record_df.iloc[0]['datetime']}")
                logger.error(f"Last record time: {self.loader.record_df.iloc[-1]['datetime']}")
            raise ValueError(
                f"No ground truth found for task {self.task_index} instance {self.instance}. "
                f"Time window: {start_time} to {end_time}"
            )

        logger.info(f"Ground Truth (record):")
        logger.info(f"  component: {self.ground_truth.get('component')}")
        logger.info(f"  datetime: {self.ground_truth.get('datetime')}")
        logger.info(f"  reason: {self.ground_truth.get('reason')}")
        logger.info(f"  timestamp: {self.ground_truth.get('timestamp')}")
        logger.info("=" * 80)

    def get_task_description(self) -> str:
        """Return task description for agent."""
        instruction = self.task_row['instruction']
        start_time, end_time = self.loader.parse_time_window_from_instruction(instruction)

        task_type = self._get_task_type()

        desc = f"# OpenRCA {self.dataset} - {task_type}\n\n"
        desc += f"**Time Window:** {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        desc += "**Available Actions:**\n"
        desc += "- query_static_traces(start_time, end_time, cmdb_id=None)\n"
        desc += "- query_static_logs(start_time, end_time, cmdb_id=None, keyword=None)\n"
        desc += "- query_static_metrics(start_time, end_time, cmdb_id=None)\n\n"

        # Add solution format based on task type
        if self.task_index == "task_1":
            desc += '**Solution Format:** `{"root_cause_time": "YYYY-MM-DD HH:MM:SS"}`\n'
        elif self.task_index in ["task_6", "task_7"]:
            desc += '**Solution Format:** `{"root_cause_component": "...", "root_cause_reason": "..."}`\n'

        return desc

    def get_instructions(self) -> str:
        """Return detailed instructions with remapped timestamps."""
        # Parse time window to get remapped times
        start_time, end_time = self.loader.parse_time_window_from_instruction(
            self.task_row['instruction']
        )

        # Create simplified instruction with only remapped times
        task_type = self._get_task_type()

        if self.task_index == "task_1":
            instruction = f"A system failure was detected. Your task is to identify the exact time when the root cause occurred.\n\n"
            instruction += f"Analyze telemetry data in the time window: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            instruction += f"Submit your answer as: {{\"root_cause_time\": \"YYYY-MM-DD HH:MM:SS\"}}"
        elif self.task_index in ["task_6", "task_7"]:
            instruction = f"A system failure was detected. Your task is to identify the root cause component and reason.\n\n"
            instruction += f"Analyze telemetry data in the time window: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            instruction += f"Submit your answer as: {{\"root_cause_component\": \"...\", \"root_cause_reason\": \"...\"}}"
        else:
            instruction = f"Analyze the system failure in the time window: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}"

        return instruction

    def get_config(self) -> Dict[str, Any]:
        """Return problem configuration."""
        return {
            'dataset': 'openrca',
            'namespace': self.dataset.lower(),
            'task_index': self.task_index,
            'instance': self.instance
        }

    def eval(self, soln: Any, trace: List[SessionItem], duration: float) -> Dict[str, Any]:
        """Evaluate solution based on task type."""
        logger.info("=" * 80)
        logger.info(f"DEBUG: EVALUATION")
        logger.info("=" * 80)
        logger.info(f"Problem: {self.dataset} {self.task_index} (instance {self.instance})")
        logger.info(f"")
        logger.info(f"Submitted Solution:")
        logger.info(f"  {soln}")
        logger.info(f"")
        logger.info(f"Expected Ground Truth:")
        logger.info(f"  component: {self.ground_truth.get('component')}")
        logger.info(f"  datetime: {self.ground_truth.get('datetime')}")
        logger.info(f"  reason: {self.ground_truth.get('reason')}")
        logger.info("=" * 80)

        evaluator = OpenRCAEvaluator(self.ground_truth)

        try:
            # Determine what to evaluate based on solution fields
            has_time = 'root_cause_time' in soln
            has_component = 'root_cause_component' in soln
            has_reason = 'root_cause_reason' in soln

            result = {}

            # Evaluate based on what's provided
            if has_time:
                predicted_time = self._parse_time(soln['root_cause_time'])
                time_eval = evaluator.evaluate_time_detection(predicted_time)
                result['time'] = time_eval

            if has_component and has_reason:
                comp_reason_eval = evaluator.evaluate_component_and_reason(
                    soln['root_cause_component'],
                    soln['root_cause_reason']
                )
                result['component'] = comp_reason_eval['component']
                result['reason'] = comp_reason_eval['reason']

            elif has_component:
                comp_eval = evaluator.evaluate_component_detection(soln['root_cause_component'])
                result['component'] = comp_eval

            elif has_reason:
                reason_eval = evaluator.evaluate_reason_detection(soln['root_cause_reason'])
                result['reason'] = reason_eval

            # Determine overall success
            success = all(r.get('correct', False) for r in result.values())

            logger.info("")
            logger.info("=" * 80)
            logger.info("DEBUG: EVALUATION RESULTS")
            logger.info("=" * 80)
            logger.info(f"Evaluation Details:")
            for key, value in result.items():
                logger.info(f"  {key}: {value}")
            logger.info(f"")
            logger.info(f"Overall Success: {success}")
            logger.info(f"Duration: {duration:.2f}s")
            logger.info(f"Feedback: {self._generate_feedback(result)}")
            logger.info("=" * 80)

            return {
                'success': success,
                'evaluation': result,
                'metrics': {'duration_seconds': duration},
                'feedback': self._generate_feedback(result)
            }

        except Exception as e:
            logger.error(f"Evaluation error: {e}", exc_info=True)
            return {
                'success': False,
                'evaluation': {},
                'metrics': {'duration_seconds': duration},
                'feedback': f"Invalid solution: {e}"
            }

    def _get_task_type(self) -> str:
        """Determine task type name."""
        if self.task_index == "task_1":
            return "Time Detection"
        elif self.task_index == "task_2":
            return "Component Detection"
        elif self.task_index == "task_3":
            return "Reason Detection"
        elif self.task_index in ["task_5", "task_6", "task_7"]:
            return "Component and Reason Detection"
        else:
            return "RCA Task"

    def _parse_time(self, time_str: str) -> datetime:
        """Parse time string in various formats."""
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"]:
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue
        raise ValueError(f"Cannot parse time: {time_str}")

    def _generate_feedback(self, result: Dict) -> str:
        """Generate human-readable feedback."""
        parts = []

        if 'time' in result:
            t = result['time']
            parts.append(f"Time: {t['error_minutes']}min error")

        if 'component' in result:
            c = result['component']
            parts.append(f"Component: {c['accuracy']}")

        if 'reason' in result:
            r = result['reason']
            parts.append(f"Reason: {r['accuracy']}")

        return ", ".join(parts) if parts else "Evaluated"

    def cleanup(self):
        """Cleanup resources including temporary config file."""
        # Cleanup app (stop replayer)
        if hasattr(self, 'app') and self.app:
            self.app.cleanup()

        # Remove temporary config file
        if hasattr(self, 'temp_config_file'):
            import os
            try:
                if os.path.exists(self.temp_config_file.name):
                    os.unlink(self.temp_config_file.name)
                    logger.info(f"Removed temporary config: {self.temp_config_file.name}")
            except Exception as e:
                logger.warning(f"Failed to remove temp config: {e}")
