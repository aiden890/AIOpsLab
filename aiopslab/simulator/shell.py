# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Interactive shell for RCA Simulator."""

import cmd
import asyncio
import sys
from pathlib import Path

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from aiopslab.simulator.orchestrator import SimulationOrchestrator
from clients.run_openrca import OpenRCAAgent


class SimulatorShell(cmd.Cmd):
    """Interactive shell for RCA simulation."""

    intro = """
=====================================
  RCA Simulator - Interactive Mode
=====================================
Type 'help' for available commands.
"""
    prompt = ">>> "

    def __init__(self):
        super().__init__()
        self.orch = SimulationOrchestrator()
        self.agent = None
        self.problem_id = "openrca-bank-task_3-0"  # Default problem
        self._initialized = False

    def _ensure_initialized(self):
        """Initialize problem and agent if not already done."""
        if self._initialized:
            return True

        if self.orch.current_time is None:
            print("Error: Set simulation time first with 'time' command")
            return False

        # Create and register agent FIRST (before init_problem)
        self.agent = OpenRCAAgent()
        self.orch.register_agent(self.agent, name="react")

        # Initialize problem
        try:
            task_desc, instructions, apis = self.orch.init_problem(self.problem_id)
        except Exception as e:
            print(f"Error initializing problem: {e}")
            return False

        # Initialize agent context
        self.agent.init_context(task_desc, instructions, apis)

        self._initialized = True
        print(f"Initialized: {self.problem_id}")
        return True

    # === Time Commands ===

    def do_time(self, arg):
        """time [YYYY-MM-DD HH:MM:SS] - Get or set simulation time

        Examples:
            time                        - Show current time and status
            time 2021-03-04 14:50:00   - Set time (clock paused)
        """
        arg = arg.strip()
        if arg:
            if self.orch.set_time(arg):
                print(f"Time set: {self.orch.get_clock_status()}")
                # Reset initialization when time changes
                self._initialized = False
            else:
                print(f"Invalid time format: {arg}")
                print("Use: YYYY-MM-DD HH:MM:SS")
        else:
            print(f"Clock: {self.orch.get_clock_status()}")

    # === Clock Control Commands ===

    def do_start(self, arg):
        """start - Start the simulation clock

        The clock will advance in real-time (multiplied by speed).
        """
        if self.orch.clock is None:
            print("Error: Set time first with 'time' command")
            return
        self.orch.start_clock()
        print(f"Clock started: {self.orch.get_clock_status()}")

    def do_pause(self, arg):
        """pause - Pause the simulation clock

        Time stops advancing until resumed.
        """
        if self.orch.clock is None:
            print("Error: Clock not initialized")
            return
        self.orch.pause_clock()
        print(f"Clock paused: {self.orch.get_clock_status()}")

    def do_resume(self, arg):
        """resume - Resume the simulation clock"""
        if self.orch.clock is None:
            print("Error: Clock not initialized")
            return
        self.orch.resume_clock()
        print(f"Clock resumed: {self.orch.get_clock_status()}")

    def do_speed(self, arg):
        """speed [N] - Get or set clock speed multiplier

        Examples:
            speed       - Show current speed
            speed 1     - Real-time (1 real sec = 1 sim sec)
            speed 60    - Fast (1 real sec = 1 sim min)
            speed 3600  - Very fast (1 real sec = 1 sim hour)
        """
        arg = arg.strip()
        if arg:
            try:
                speed = float(arg)
                if self.orch.set_speed(speed):
                    print(f"Speed set: {speed}x")
                else:
                    print("Error: Initialize clock first with 'time' command")
            except ValueError:
                print(f"Invalid speed: {arg}")
        else:
            if self.orch.clock:
                print(f"Current speed: {self.orch.clock.speed}x")
            else:
                print("Clock not initialized")

    # === Run Commands ===

    def do_run(self, arg):
        """run [max_steps] - Run the agent

        Examples:
            run      - Run with default 30 steps
            run 10   - Run with max 10 steps

        Note: Clock continues running during agent execution.
        """
        if not self._ensure_initialized():
            return

        max_steps = 30
        if arg.strip():
            try:
                max_steps = int(arg.strip())
            except ValueError:
                print("Invalid step count. Using default: 30")

        print(f"\nRunning agent...")
        print(f"Clock: {self.orch.get_clock_status()}")
        print(f"Data access limited to before current simulation time")
        print("-" * 40)

        try:
            result = asyncio.run(self.orch.start_problem(max_steps))
            print("-" * 40)
            print(f"Final time: {self.orch.get_clock_status()}")
            print(f"Result: {result.get('results', {})}")
        except Exception as e:
            print(f"Error: {e}")

        # Reset for next run
        self._initialized = False

    # === Status Commands ===

    def do_status(self, arg):
        """status - Show current simulation status"""
        print(f"Clock:   {self.orch.get_clock_status()}")
        print(f"Problem: {self.problem_id}")
        print(f"Agent:   {'react' if self.agent else 'Not deployed'}")
        print(f"Ready:   {'Yes' if self._initialized else 'No'}")

    def do_problem(self, arg):
        """problem [id] - Get or set problem ID

        Examples:
            problem                          - Show current problem
            problem openrca-bank-task_3-5   - Set problem
        """
        arg = arg.strip()
        if arg:
            self.problem_id = arg
            self._initialized = False
            print(f"Problem set: {self.problem_id}")
        else:
            print(f"Current problem: {self.problem_id}")

    # === Exit Commands ===

    def do_quit(self, arg):
        """quit - Exit the simulator"""
        print("Goodbye!")
        return True

    def do_exit(self, arg):
        """exit - Exit the simulator"""
        return self.do_quit(arg)

    def do_q(self, arg):
        """q - Exit the simulator"""
        return self.do_quit(arg)

    # === Help ===

    def do_help(self, arg):
        """Show help for commands"""
        if arg:
            super().do_help(arg)
        else:
            print("""
Available commands:

  Time & Clock:
    time [TIME]      - Get/set simulation time (YYYY-MM-DD HH:MM:SS)
    start            - Start the clock (time advances)
    pause            - Pause the clock
    resume           - Resume the clock
    speed [N]        - Get/set speed multiplier (60 = 1 real sec = 1 sim min)

  Execution:
    run [STEPS]      - Run agent (default: 30 steps)
    status           - Show current status
    problem [ID]     - Get/set problem ID

  Other:
    quit/exit/q      - Exit simulator

Quick start:
  >>> time 2021-03-04 14:00:00
  >>> speed 60
  >>> start
  >>> run
""")

    def emptyline(self):
        """Do nothing on empty line."""
        pass


def main():
    """Entry point for the simulator."""
    shell = SimulatorShell()
    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
