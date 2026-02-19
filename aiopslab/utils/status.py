# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Utility functions for handling status and errors."""

from enum import Enum
from colorama import Fore, Style
from datetime import datetime
from pathlib import Path
import re

from aiopslab.config import Config
from aiopslab.paths import BASE_DIR

config = Config(BASE_DIR / "config.yml")


class SubmissionStatus(Enum):
    VALID_SUBMISSION = 1
    INVALID_SUBMISSION = 2


class InvalidActionError(Exception):
    def __init__(self, action_name):
        super().__init__(f"Invalid action: {action_name}")
        self.action_name = action_name


class ResponseParsingError(Exception):
    def __init__(self, message):
        super().__init__(f"Error parsing response: {message}")
        self.message = message


class SessionPrint:
    def __init__(self):
        self.enable_terminal = config.get("print_session_terminal", True)
        self.enable_file = config.get("print_session_file", True)
        self.step_count = 0
        self.log_file = None
        self.log_filepath = None

    def init_log_file(self, filepath):
        """Initialize log file for session output."""
        if self.enable_file:
            self.log_filepath = filepath
            # Create parent directories if needed
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            self.log_file = open(filepath, 'w', encoding='utf-8')

            # Always print log file path to terminal
            print(f"{Fore.CYAN}📝 Session log:{Style.RESET_ALL} {filepath}")

            self._log(f"Session log started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    def close_log_file(self):
        """Close the log file."""
        if self.log_file:
            self._log(f"\nSession log ended at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            self.log_file.close()

            # Always print completion message to terminal
            print(f"{Fore.CYAN}📝 Session log saved:{Style.RESET_ALL} {self.log_filepath}")

            self.log_file = None

    def _log(self, text, colored_text=None):
        """Write to terminal and/or file."""
        # Write to terminal with colors
        if self.enable_terminal:
            print(colored_text if colored_text else text)

        # Write to file without colors (strip ANSI codes)
        if self.enable_file and self.log_file:
            # Remove ANSI color codes for file output
            
            clean_text = re.sub(r'\x1b\[[0-9;]*m', '', str(colored_text if colored_text else text))
            self.log_file.write(clean_text + '\n')
            self.log_file.flush()

    def system_prompt(self, text):
        """Print the full system prompt sent to the agent."""
        if self.enable_terminal or self.enable_file:
            self._log("=" * 60, f"\n{Fore.MAGENTA}{'='*60}")
            self._log("📨 System Prompt (First Prompt)", "📨 System Prompt (First Prompt)")
            self._log("=" * 60, f"{'='*60}{Style.RESET_ALL}\n")
            self._log(text)
            self._log("=" * 60 + "\n", f"\n{Fore.MAGENTA}{'='*60}{Style.RESET_ALL}\n")

    def problem_init(self, problem_desc, instructions, apis=None):
        """Print initial problem setup."""
        if self.enable_terminal or self.enable_file:
            self._log("=" * 60, f"\n{Fore.MAGENTA}{'='*60}")
            self._log("🎯 Problem Initialization", f"🎯 Problem Initialization")
            self._log("=" * 60, f"{'='*60}{Style.RESET_ALL}\n")

            self._log("\n📋 Problem Description:", f"{Fore.CYAN}📋 Problem Description:{Style.RESET_ALL}")
            self._log(problem_desc + "\n")

            self._log("📝 Instructions:", f"{Fore.CYAN}📝 Instructions:{Style.RESET_ALL}")
            self._log(instructions + "\n")

            if apis:
                self._log("🔧 Available APIs:", f"{Fore.CYAN}🔧 Available APIs:{Style.RESET_ALL}")
                api_list = list(apis.keys())
                for i, api in enumerate(api_list, 1):
                    self._log(f"  {i}. {api}")
                self._log(f"\nTotal APIs available: {len(api_list)}", f"\n{Fore.YELLOW}Total APIs available: {len(api_list)}{Style.RESET_ALL}")

            self._log("\n" + "=" * 60, f"\n{Fore.MAGENTA}{'='*60}")
            self._log("Starting Agent Execution", f"Starting Agent Execution")
            self._log("=" * 60 + "\n", f"{'='*60}{Style.RESET_ALL}\n")

    def agent(self, action):
        self.step_count += 1

        # Always print step progress to terminal
        print(f"{Fore.CYAN}[Step {self.step_count}]{Style.RESET_ALL}", end=" ", flush=True)

        if self.enable_terminal or self.enable_file:
            self._log("\n" + "=" * 60, f"\n{Fore.CYAN}{'='*60}")
            self._log(f"Step {self.step_count}", f"Step {self.step_count}")
            self._log("=" * 60, f"{'='*60}{Style.RESET_ALL}")

            # Try to parse as ReAct format
            thought, action_text = self._parse_react_response(action)

            # If ReAct format detected (has thought or action)
            if thought or action_text:
                if thought:
                    self._log("💭 Thought:", f"{Fore.YELLOW}💭 Thought:{Style.RESET_ALL}")
                    self._log(f"   {thought}\n")

                if action_text:
                    self._log("⚡ Action:", f"{Fore.GREEN}⚡ Action:{Style.RESET_ALL}")
                    self._log(f"   {action_text}")
            else:
                # Non-ReAct agent: log raw response
                self._log("🤖 Agent Response:", f"{Fore.GREEN}🤖 Agent Response:{Style.RESET_ALL}")
                self._log(f"   {action}")

    def service(self, response):
        # Always print step completion to terminal
        print(f"{Fore.GREEN}✓{Style.RESET_ALL}")

        if self.enable_terminal or self.enable_file:
            self._log("\n📋 Observation:", f"\n{Fore.BLUE}📋 Observation:{Style.RESET_ALL}")
            # Convert to string if it's not already
            response_str = str(response) if not isinstance(response, str) else response
            # Truncate very long responses for readability
            if len(response_str) > 2000:
                self._log(f"   {response_str[:2000]}...")
                self._log(f"   [Response truncated - {len(response_str)} total chars]",
                         f"   {Fore.YELLOW}[Response truncated - {len(response_str)} total chars]{Style.RESET_ALL}")
            else:
                self._log(f"   {response_str}")
            self._log("-" * 60 + "\n", f"{Fore.CYAN}{'-'*60}{Style.RESET_ALL}\n")

    def result(self, results):
        # Always print final result to terminal
        success = results.get('success', False)
        score = results.get('score', 'N/A')

        if success:
            print(f"\n{Fore.GREEN}✅ SUCCESS{Style.RESET_ALL} - Score: {score}")
        else:
            print(f"\n{Fore.RED}❌ FAILED{Style.RESET_ALL} - Score: {score}")

        # Detailed results to log file or terminal if enabled
        if self.enable_terminal or self.enable_file:
            self._log("\n" + "=" * 60, f"\n{Fore.MAGENTA}{'='*60}")
            self._log("📊 Results:", f"📊 Results:")
            self._log("=" * 60, f"{'='*60}{Style.RESET_ALL}")

            # Print full record (faults from record.csv)
            record = results.get("record")
            if record:
                self._log("📋 Record (ground truth faults):", f"{Fore.YELLOW}📋 Record (ground truth faults):{Style.RESET_ALL}")
                header = f"   {'datetime':<22} {'level':<10} {'component':<15} {'reason'}"
                self._log(header)
                self._log("   " + "-" * 65)
                for fault in record:
                    row = (
                        f"   {fault.get('datetime', ''):<22}"
                        f" {fault.get('level', ''):<10}"
                        f" {fault.get('component', ''):<15}"
                        f" {fault.get('reason', '')}"
                    )
                    self._log(row)
                self._log("")

            # Print scoring criteria
            ground_truth = results.get("ground_truth")
            if ground_truth:
                self._log("📌 Scoring Criteria:", f"{Fore.YELLOW}📌 Scoring Criteria:{Style.RESET_ALL}")
                self._log(f"   {ground_truth}\n")

            # Print remaining metrics (exclude record/ground_truth from the dict dump)
            metrics = {k: v for k, v in results.items() if k not in ("ground_truth", "record")}
            self._log(f"{metrics}")

    def registry_info(self, registry):
        """Print static problem registry information."""
        self._log("\n" + "=" * 60, f"\n{Fore.CYAN}{'='*60}")
        self._log("📚 Static Problem Registry", f"📚 Static Problem Registry")
        self._log("=" * 60 + "\n", f"{'='*60}{Style.RESET_ALL}\n")

        # Group problems by dataset
        datasets = {}
        for pid in registry.PROBLEM_REGISTRY.keys():
            dataset = pid.split('-')[0] + '-' + pid.split('-')[1]  # e.g., "openrca_bank"
            if dataset not in datasets:
                datasets[dataset] = []
            datasets[dataset].append(pid)

        for dataset, pids in sorted(datasets.items()):
            # Count by task type
            task_counts = {}
            for pid in pids:
                parts = pid.split('-')
                if len(parts) >= 3:
                    task_type = parts[2]  # e.g., "task_1"
                    task_counts[task_type] = task_counts.get(task_type, 0) + 1

            self._log(f"📂 {dataset}:", f"{Fore.GREEN}📂 {dataset}:{Style.RESET_ALL}")
            self._log(f"   Total: {len(pids)} problems")
            self._log(f"   Task breakdown:")
            for task_type, count in sorted(task_counts.items()):
                self._log(f"      • {task_type}: {count} problems")
            self._log("")

        self._log(f"✅ Total problems in registry: {len(registry.PROBLEM_REGISTRY)}",
                 f"{Fore.YELLOW}✅ Total problems in registry: {len(registry.PROBLEM_REGISTRY)}{Style.RESET_ALL}")
        self._log("=" * 60 + "\n", f"{Fore.CYAN}{'='*60}{Style.RESET_ALL}\n")

    def _parse_react_response(self, text):
        """Parse ReAct format response into thought and action components.
        Returns empty strings if not in ReAct format."""

        thought = ""
        action = ""

        # Check if text contains ReAct markers (case-insensitive)
        if 'thought:' not in text.lower() and 'action:' not in text.lower():
            return "", ""

        # Extract thought section
        thought_match = re.search(r'Thought:\s*(.*?)(?=\nAction:|$)', text, re.IGNORECASE | re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()

        # Extract action section - only content within code blocks
        action_match = re.search(r'Action:\s*(.*?)(?=$|\n(?:Thought|Action|Observation):)', text, re.IGNORECASE | re.DOTALL)
        if action_match:
            action = action_match.group(1).strip()

        return thought.strip(), action.strip()
