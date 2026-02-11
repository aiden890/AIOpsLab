# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Results writer for static RCA experiments."""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List
import pandas as pd

logger = logging.getLogger(__name__)


class ResultsWriter:
    """Write experiment results in structured format."""

    def __init__(self, session_dir: Path, summary_file: Path):
        """
        Initialize results writer.

        Args:
            session_dir: Directory for this session's results
            summary_file: Path to experiments.csv summary file
        """
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.summary_file = Path(summary_file)

    def create_session_directory(self, problem_id: str, session_id: str) -> Path:
        """
        Create timestamped session directory.

        Args:
            problem_id: Problem identifier (e.g., 'openrca_bank_task1')
            session_id: Unique session ID

        Returns:
            Path to session directory
        """
        # Parse problem_id: openrca_bank_task1 -> openrca_bank/task1/
        parts = problem_id.split('_')
        if len(parts) >= 2:
            # Try to find task part
            task_idx = None
            for i, part in enumerate(parts):
                if part.startswith('task'):
                    task_idx = i
                    break

            if task_idx is not None:
                dataset_namespace = '_'.join(parts[:task_idx])
                task = '_'.join(parts[task_idx:])
            else:
                # Fallback: all before last part is dataset/namespace
                dataset_namespace = '_'.join(parts[:-1])
                task = parts[-1]
        else:
            dataset_namespace = problem_id
            task = "default"

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = (
            self.results_dir
            / dataset_namespace
            / task
            / f"{timestamp}_{session_id}"
        )

        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "logs").mkdir(exist_ok=True)

        logger.info(f"Session directory: {session_dir}")
        return session_dir

    def write_metadata(self, session_dir: Path, metadata: Dict[str, Any]):
        """Write metadata.json"""
        with open(session_dir / "metadata.json", 'w') as f:
            json.dump({"metadata": metadata}, f, indent=2, default=str)

        logger.debug("Wrote metadata.json")

    def write_results(self, session_dir: Path, results: Dict[str, Any]):
        """Write results.json"""
        with open(session_dir / "results.json", 'w') as f:
            json.dump({"results": results}, f, indent=2, default=str)

        logger.info("Wrote results.json")

    def write_trace(self, session_dir: Path, trace: List[Any]):
        """Write conversation trace"""
        with open(session_dir / "trace.json", 'w') as f:
            json.dump({"trace": trace}, f, indent=2, default=str)

        logger.debug("Wrote trace.json")

    def save_results(
        self,
        session_id: str,
        problem_id: str,
        solution: Dict[str, Any],
        eval_results: Dict[str, Any],
        trace: List[Any],
        duration: float
    ):
        """
        Save complete experiment results.

        Writes metadata.json, results.json, trace.json to session directory
        and appends summary row to experiments.csv.

        Args:
            session_id: Unique session identifier
            problem_id: Problem identifier (e.g., 'openrca_bank_task1')
            solution: Agent's proposed solution
            eval_results: Evaluation results from problem.eval()
            trace: Conversation trace
            duration: Session duration in seconds
        """
        timestamp_start = datetime.now().isoformat()

        # Construct metadata
        metadata = {
            'session_id': session_id,
            'problem_id': problem_id,
            'timestamp_start': timestamp_start,
            'duration_seconds': duration,
            'problem': {
                'id': problem_id,
                'dataset': problem_id.split('_')[0] if '_' in problem_id else problem_id,
                'namespace': '_'.join(problem_id.split('_')[:-1]) if '_' in problem_id else '',
                'type': 'rca'
            },
            'agent': {
                'type': 'unknown'  # Will be updated when agent info is available
            }
        }

        # Construct results
        results = {
            'success': eval_results.get('success', False),
            'solution': solution,
            'evaluation': eval_results.get('evaluation', {}),
            'metrics': eval_results.get('metrics', {}),
            'feedback': eval_results.get('feedback', '')
        }

        # Write all result files
        self.write_metadata(self.session_dir, metadata)
        self.write_results(self.session_dir, results)
        self.write_trace(self.session_dir, trace)

        # Append to summary CSV
        self.append_to_summary(metadata, results)

        logger.info(f"✓ All results saved to: {self.session_dir}")

    def append_to_summary(self, metadata: Dict[str, Any], results: Dict[str, Any]):
        """Append experiment to summary CSV"""
        evaluation = results.get('evaluation', {})
        metrics = results.get('metrics', {})

        row = {
            'session_id': metadata.get('session_id', ''),
            'timestamp': metadata.get('timestamp_start', ''),
            'dataset': metadata.get('problem', {}).get('dataset', ''),
            'namespace': metadata.get('problem', {}).get('namespace', ''),
            'task_type': metadata.get('problem', {}).get('type', ''),
            'agent_type': metadata.get('agent', {}).get('type', ''),
            'success': results.get('success', False),
            'duration_sec': metadata.get('duration_seconds', 0),
            'time_error_min': evaluation.get('time_error_minutes'),
            'component_correct': evaluation.get('component_accuracy') == 'Correct' if 'component_accuracy' in evaluation else None,
            'reason_correct': evaluation.get('reason_accuracy') == 'Correct' if 'reason_accuracy' in evaluation else None,
            'num_steps': metrics.get('num_agent_steps'),
            'total_tokens': metrics.get('tokens', {}).get('total') if isinstance(metrics.get('tokens'), dict) else None,
            'notes': ''
        }

        # Append to CSV
        df = pd.DataFrame([row])
        df.to_csv(
            self.summary_file,
            mode='a',
            header=not self.summary_file.exists(),
            index=False
        )

        logger.info(f"Updated summary: {self.summary_file}")
