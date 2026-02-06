# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Simulation Orchestrator with real-time clock control."""

from datetime import datetime
from typing import Optional

from aiopslab.orchestrator.static_orchestrator import StaticDatasetOrchestrator
from aiopslab.simulator.clock import SimulationClock


class SimulationOrchestrator(StaticDatasetOrchestrator):
    """Orchestrator with real-time simulation clock.

    Extends StaticDatasetOrchestrator to add:
    - Real-time simulation clock with speed control
    - Time-restricted data access based on clock.now()
    """

    def __init__(self, results_dir=None):
        super().__init__(results_dir)
        self.clock: Optional[SimulationClock] = None

    def init_clock(self, start_time: str, speed: float = 1.0) -> bool:
        """Initialize simulation clock.

        Args:
            start_time: Start time in format "YYYY-MM-DD HH:MM:SS"
            speed: Speed multiplier (1.0 = real-time, 60.0 = 1 real sec = 1 sim min)

        Returns:
            bool: True if successful
        """
        try:
            dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            self.clock = SimulationClock(dt, speed)
            return True
        except ValueError:
            return False

    def set_time(self, time_str: str) -> bool:
        """Set simulation time (initializes clock if needed).

        Args:
            time_str: Time string in format "YYYY-MM-DD HH:MM:SS"

        Returns:
            bool: True if successful
        """
        try:
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            if self.clock is None:
                self.clock = SimulationClock(dt)
            else:
                self.clock.set_time(dt)
            return True
        except ValueError:
            return False

    def set_speed(self, speed: float) -> bool:
        """Set clock speed multiplier.

        Args:
            speed: Speed multiplier (must be > 0)

        Returns:
            bool: True if successful
        """
        if self.clock is None:
            return False
        try:
            self.clock.set_speed(speed)
            return True
        except ValueError:
            return False

    def start_clock(self):
        """Start the simulation clock."""
        if self.clock:
            self.clock.start()

    def pause_clock(self):
        """Pause the simulation clock."""
        if self.clock:
            self.clock.pause()

    def resume_clock(self):
        """Resume the simulation clock."""
        if self.clock:
            self.clock.resume()

    @property
    def current_time(self) -> Optional[datetime]:
        """Get current simulation time as datetime.

        Returns:
            datetime: Current simulation time or None
        """
        if self.clock is None:
            return None
        return self.clock.now()

    def get_time(self) -> str:
        """Get current simulation time as string.

        Returns:
            str: Current time string or "Not set"
        """
        if self.clock is None:
            return "Not set"
        return self.clock.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_timestamp(self) -> Optional[float]:
        """Get current simulation time as Unix timestamp.

        Returns:
            float: Unix timestamp or None
        """
        if self.clock is None:
            return None
        return self.clock.timestamp()

    def get_clock_status(self) -> str:
        """Get clock status string.

        Returns:
            str: Status description
        """
        if self.clock is None:
            return "Not initialized"

        status = "running" if self.clock.is_running else "paused"
        return f"{self.get_time()} ({status}, {self.clock.speed}x)"

    async def ask_env(self, input):
        """Ask environment with time restriction applied.

        Overrides parent to apply time restriction on data access APIs.
        """
        modified_input = input

        # Parse the action to check if it's a time-sensitive API
        try:
            resp = self.parser.parse(input)
            api_name = resp["api_name"]
            kwargs = resp["kwargs"]

            # Apply time restriction for telemetry APIs
            if api_name in ["get_metric_container", "get_metric_app", "get_traces", "get_logs"]:
                original_end_time = kwargs.get("end_time")
                kwargs = self._apply_time_restriction(kwargs)

                # Rebuild action string if end_time was modified
                if original_end_time != kwargs.get("end_time"):
                    modified_input = self._rebuild_action_string(api_name, kwargs)

        except Exception:
            pass  # Let parent handle parsing errors

        # Call parent implementation with (possibly modified) input
        return await super().ask_env(modified_input)

    def _rebuild_action_string(self, api_name: str, kwargs: dict) -> str:
        """Rebuild action string from API name and kwargs.

        Args:
            api_name: Name of the API
            kwargs: Keyword arguments

        Returns:
            str: Rebuilt action string with code block formatting
        """
        # Format kwargs as key=value pairs
        args_str = ", ".join(f'{k}="{v}"' for k, v in kwargs.items())
        # Wrap in code block for parser compatibility
        return f"```\n{api_name}({args_str})\n```"

    def _apply_time_restriction(self, kwargs: dict) -> dict:
        """Apply time restriction to API kwargs.

        If end_time exceeds current simulation time, restrict it.

        Args:
            kwargs: API keyword arguments

        Returns:
            dict: Modified kwargs with time restriction
        """
        current = self.current_time
        if current is None:
            return kwargs

        if "end_time" in kwargs:
            try:
                end_time = datetime.strptime(kwargs["end_time"], "%Y-%m-%d %H:%M:%S")
                if end_time > current:
                    kwargs["end_time"] = current.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass

        return kwargs
