# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""OpenRCA evaluator for RCA solutions."""

import logging
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class OpenRCAEvaluator:
    """
    Evaluator for OpenRCA RCA solutions.

    Provides methods to evaluate different aspects of RCA:
    - Time detection accuracy
    - Component detection accuracy
    - Reason detection accuracy

    All timestamps are in CURRENT timeline (already remapped by loader).
    """

    def __init__(self, ground_truth: Dict[str, Any]):
        """
        Initialize evaluator with ground truth.

        Args:
            ground_truth: Ground truth dictionary with:
                - component: Root cause component
                - timestamp: Unix timestamp (CURRENT timeline)
                - datetime: Datetime string (CURRENT timeline)
                - reason: Root cause reason
        """
        self.ground_truth = ground_truth

        # Parse datetime from ground truth
        self.gt_datetime = datetime.fromisoformat(
            ground_truth['datetime'].replace(' ', 'T')
        )

        logger.debug(f"Evaluator initialized with ground truth: {ground_truth['component']} at {self.gt_datetime}")

    def evaluate_time_detection(
        self,
        predicted_time: datetime,
        tolerance_minutes: float = 1.0
    ) -> Dict[str, Any]:
        """
        Evaluate time detection accuracy.

        Both predicted_time and ground truth are in CURRENT timeline.
        Simple comparison!

        Args:
            predicted_time: Agent's predicted root cause time (current timeline)
            tolerance_minutes: Acceptable error in minutes (default: 1.0)

        Returns:
            Dictionary with:
                - correct: Whether prediction is within tolerance
                - error_minutes: Absolute error in minutes
                - ground_truth: Ground truth datetime
                - predicted: Predicted datetime
        """
        # Calculate time difference in minutes
        time_diff = abs((predicted_time - self.gt_datetime).total_seconds() / 60.0)

        correct = time_diff <= tolerance_minutes

        logger.info(
            f"Time evaluation: error={time_diff:.2f}min, "
            f"correct={correct} (tolerance={tolerance_minutes}min)"
        )
        logger.info(f"  Ground truth: {self.gt_datetime}")
        logger.info(f"  Predicted: {predicted_time}")

        return {
            'correct': correct,
            'error_minutes': round(time_diff, 2),
            'ground_truth': self.gt_datetime.isoformat(),
            'predicted': predicted_time.isoformat(),
            'tolerance_minutes': tolerance_minutes
        }

    def evaluate_component_detection(
        self,
        predicted_component: str
    ) -> Dict[str, Any]:
        """
        Evaluate component detection accuracy.

        Args:
            predicted_component: Agent's predicted root cause component

        Returns:
            Dictionary with:
                - correct: Whether prediction matches ground truth
                - accuracy: 'Correct' or 'Incorrect'
                - ground_truth: Ground truth component
                - predicted: Predicted component
        """
        gt_component = self.ground_truth['component']

        # Case-insensitive comparison
        correct = predicted_component.lower() == gt_component.lower()

        logger.info(
            f"Component evaluation: predicted={predicted_component}, "
            f"ground_truth={gt_component}, correct={correct}"
        )

        return {
            'correct': correct,
            'accuracy': 'Correct' if correct else 'Incorrect',
            'ground_truth': gt_component,
            'predicted': predicted_component
        }

    def evaluate_reason_detection(
        self,
        predicted_reason: str
    ) -> Dict[str, Any]:
        """
        Evaluate reason detection accuracy.

        Args:
            predicted_reason: Agent's predicted root cause reason

        Returns:
            Dictionary with:
                - correct: Whether prediction matches ground truth
                - accuracy: 'Correct' or 'Incorrect'
                - ground_truth: Ground truth reason
                - predicted: Predicted reason
        """
        gt_reason = self.ground_truth['reason']

        # Case-insensitive comparison
        correct = predicted_reason.lower() == gt_reason.lower()

        logger.info(
            f"Reason evaluation: predicted={predicted_reason}, "
            f"ground_truth={gt_reason}, correct={correct}"
        )

        return {
            'correct': correct,
            'accuracy': 'Correct' if correct else 'Incorrect',
            'ground_truth': gt_reason,
            'predicted': predicted_reason
        }

    def evaluate_component_and_reason(
        self,
        predicted_component: str,
        predicted_reason: str
    ) -> Dict[str, Any]:
        """
        Evaluate both component and reason detection.

        Args:
            predicted_component: Agent's predicted component
            predicted_reason: Agent's predicted reason

        Returns:
            Dictionary with component and reason evaluation results
        """
        component_eval = self.evaluate_component_detection(predicted_component)
        reason_eval = self.evaluate_reason_detection(predicted_reason)

        success = component_eval['correct'] and reason_eval['correct']

        logger.info(
            f"Component+Reason evaluation: component={component_eval['correct']}, "
            f"reason={reason_eval['correct']}, overall={success}"
        )

        return {
            'success': success,
            'component': component_eval,
            'reason': reason_eval
        }

    def evaluate_full(
        self,
        predicted_time: Optional[datetime] = None,
        predicted_component: Optional[str] = None,
        predicted_reason: Optional[str] = None,
        time_tolerance_minutes: float = 1.0
    ) -> Dict[str, Any]:
        """
        Evaluate all provided predictions.

        Args:
            predicted_time: Optional predicted root cause time
            predicted_component: Optional predicted component
            predicted_reason: Optional predicted reason
            time_tolerance_minutes: Time tolerance in minutes

        Returns:
            Comprehensive evaluation dictionary
        """
        results = {}

        if predicted_time is not None:
            results['time'] = self.evaluate_time_detection(
                predicted_time,
                time_tolerance_minutes
            )

        if predicted_component is not None:
            results['component'] = self.evaluate_component_detection(
                predicted_component
            )

        if predicted_reason is not None:
            results['reason'] = self.evaluate_reason_detection(
                predicted_reason
            )

        # Determine overall success
        all_correct = all(
            eval_result.get('correct', False)
            for eval_result in results.values()
        )

        results['success'] = all_correct

        return results
