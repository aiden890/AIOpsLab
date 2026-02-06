# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Real-time simulation clock with speed control."""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional


class SimulationClock:
    """Real-time simulation clock with speed multiplier.

    The clock advances based on real wall-clock time multiplied by a speed factor.
    This allows the simulation to run faster or slower than real-time.

    Example:
        clock = SimulationClock(
            start_time=datetime(2021, 3, 4, 14, 0, 0),
            speed=10.0  # 10x speed: 1 real second = 10 simulation seconds
        )
        clock.start()
        # After 6 real seconds, clock.now() returns 2021-03-04 14:01:00
    """

    def __init__(self, start_time: datetime, speed: float = 1.0):
        """Initialize the simulation clock.

        Args:
            start_time: The simulation start time
            speed: Speed multiplier (1.0 = real-time, 10.0 = 10x faster)
        """
        self._start_time = start_time
        self._speed = speed
        self._real_start: Optional[float] = None
        self._paused = True
        self._pause_time = start_time
        self._lock = threading.Lock()

    @property
    def speed(self) -> float:
        """Get current speed multiplier."""
        return self._speed

    @property
    def is_running(self) -> bool:
        """Check if clock is running."""
        return not self._paused

    def start(self):
        """Start the clock."""
        with self._lock:
            if self._paused:
                self._real_start = time.time()
                self._start_time = self._pause_time
                self._paused = False

    def pause(self):
        """Pause the clock. Time stops advancing."""
        with self._lock:
            if not self._paused:
                self._pause_time = self._calculate_now()
                self._paused = True

    def resume(self):
        """Resume the clock from paused state."""
        self.start()

    def stop(self):
        """Stop and reset the clock."""
        with self._lock:
            self._paused = True
            self._pause_time = self._start_time

    def set_speed(self, speed: float):
        """Change the speed multiplier.

        Args:
            speed: New speed multiplier (must be > 0)
        """
        if speed <= 0:
            raise ValueError("Speed must be positive")

        with self._lock:
            # Capture current simulation time before changing speed
            if not self._paused:
                current = self._calculate_now()
                self._start_time = current
                self._real_start = time.time()
            self._speed = speed

    def set_time(self, new_time: datetime):
        """Jump to a specific simulation time.

        Args:
            new_time: The new simulation time
        """
        with self._lock:
            if self._paused:
                self._pause_time = new_time
            else:
                self._start_time = new_time
                self._real_start = time.time()

    def now(self) -> datetime:
        """Get current simulation time.

        Returns:
            Current simulation datetime
        """
        with self._lock:
            if self._paused:
                return self._pause_time
            return self._calculate_now()

    def timestamp(self) -> float:
        """Get current simulation time as Unix timestamp.

        Returns:
            Unix timestamp (seconds since epoch)
        """
        return self.now().timestamp()

    def _calculate_now(self) -> datetime:
        """Calculate current simulation time based on elapsed real time."""
        if self._real_start is None:
            return self._start_time

        real_elapsed = time.time() - self._real_start
        sim_elapsed = real_elapsed * self._speed
        return self._start_time + timedelta(seconds=sim_elapsed)

    def __str__(self) -> str:
        status = "running" if self.is_running else "paused"
        return f"SimulationClock({self.now()}, speed={self._speed}x, {status})"
