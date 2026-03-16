# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import importlib


def action(method):
    """
    Decorator to mark a method as an action.

    Args:
        method (function): The method to mark as an action.

    Returns:
        function: The decorated method.
    """
    method.is_action = True
    return method


def read(method):
    """
    Decorator to mark a method as a read action.

    Args:
        method (function): The method to mark as a read action.

    Returns:
        function: The decorated method.
    """
    method.is_action = True
    method.action_type = "read"
    return method


def write(method):
    """
    Decorator to mark a method as a write action.

    Args:
        method (function): The method to mark as a write action.

    Returns:
        function: The decorated method.
    """
    method.is_action = True
    method.action_type = "write"
    return method


def log_action(method):
    """Mark a method as a log telemetry action."""
    method.is_action = True
    method.action_type = "read"
    method.telemetry_type = "log"
    return method


def metric_action(method):
    """Mark a method as a metric telemetry action."""
    method.is_action = True
    method.action_type = "read"
    method.telemetry_type = "metric"
    return method


def trace_action(method):
    """Mark a method as a trace telemetry action."""
    method.is_action = True
    method.action_type = "read"
    method.telemetry_type = "trace"
    return method


def visualization(method):
    """Stack on top of metric_action/trace_action to mark as a visualization tool."""
    method.is_visualization = True
    return method


def executor_action(method):
    """Mark a method as an executor action (requires use_executor=True)."""
    method.is_action = True
    method.action_type = "action"
    method.telemetry_type = "executor"
    return method


def hypothesis_action(method):
    """Mark a method as a hypothesis action (requires use_hypothesis=True)."""
    method.is_action = True
    method.action_type = "action"
    method.telemetry_type = "hypothesis"
    return method


def hypothesis_executor_action(method):
    """Mark a method that requires BOTH hypothesis and executor to be enabled."""
    method.is_action = True
    method.action_type = "action"
    method.telemetry_type = "hypothesis"
    method.required_telemetry_types = frozenset({"hypothesis", "executor"})
    return method


def hypothesis_trace_action(method):
    """Mark a method that requires BOTH hypothesis and trace to be enabled."""
    method.is_action = True
    method.action_type = "action"
    method.telemetry_type = "hypothesis"
    method.required_telemetry_types = frozenset({"hypothesis", "trace"})
    return method


def get_actions(task: str, subtype: str | None = None) -> dict:
    """
    Get all actions for the given task.
        key: action name
        value: docstring of the action

    Args:
        task (str): The name of the task.
        subtype (str): The subtype of the action (optional) (default: None).

    Returns:
        dict: A dictionary of actions for the given task.
    """
    class_name = task.title() + "Actions"
    module = importlib.import_module("aiopslab.orchestrator.actions." + task)
    class_obj = getattr(module, class_name)

    actions = {
        method: getattr(class_obj, method).__doc__.strip()
        for method in dir(class_obj)
        if callable(getattr(class_obj, method))
        and getattr(getattr(class_obj, method), "is_action", False)
    }

    if subtype:
        actions = {
            method: doc
            for method, doc in actions.items()
            if getattr(getattr(class_obj, method), "action_type", None) == subtype
        }

    return actions
