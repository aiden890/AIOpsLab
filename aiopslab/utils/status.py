# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Utility functions for handling status and errors."""

from enum import Enum
from colorama import Fore, Style

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
        self.enable_printing = config.get("print_session")
        self.step_count = 0

    def problem_init(self, problem_desc, instructions, apis=None):
        """Print initial problem setup."""
        if self.enable_printing:
            print(f"\n{Fore.MAGENTA}{'='*60}")
            print(f"🎯 Problem Initialization")
            print(f"{'='*60}{Style.RESET_ALL}\n")

            print(f"{Fore.CYAN}📋 Problem Description:{Style.RESET_ALL}")
            print(f"{problem_desc}\n")

            print(f"{Fore.CYAN}📝 Instructions:{Style.RESET_ALL}")
            print(f"{instructions}\n")

            if apis:
                print(f"{Fore.CYAN}🔧 Available APIs:{Style.RESET_ALL}")
                api_list = list(apis.keys())
                for i, api in enumerate(api_list, 1):
                    print(f"  {i}. {api}")
                print(f"\n{Fore.YELLOW}Total APIs available: {len(api_list)}{Style.RESET_ALL}")

            print(f"\n{Fore.MAGENTA}{'='*60}")
            print(f"Starting Agent Execution")
            print(f"{'='*60}{Style.RESET_ALL}\n")

    def agent(self, action):
        if self.enable_printing:
            self.step_count += 1
            print(f"\n{Fore.CYAN}{'='*60}")
            print(f"Step {self.step_count}")
            print(f"{'='*60}{Style.RESET_ALL}")

            # Parse Thought and Action from the response
            thought, action_text = self._parse_react_response(action)

            if thought:
                print(f"{Fore.YELLOW}💭 Thought:{Style.RESET_ALL}")
                print(f"   {thought}\n")

            if action_text:
                print(f"{Fore.GREEN}⚡ Action:{Style.RESET_ALL}")
                print(f"   {action_text}")

    def service(self, response):
        if self.enable_printing:
            print(f"\n{Fore.BLUE}📋 Observation:{Style.RESET_ALL}")
            # Convert to string if it's not already
            response_str = str(response) if not isinstance(response, str) else response
            # Truncate very long responses for readability
            if len(response_str) > 2000:
                print(f"   {response_str[:2000]}...")
                print(f"   {Fore.YELLOW}[Response truncated - {len(response_str)} total chars]{Style.RESET_ALL}")
            else:
                print(f"   {response_str}")
            print(f"{Fore.CYAN}{'-'*60}{Style.RESET_ALL}\n")

    def result(self, results):
        print(f"\n{Fore.MAGENTA}{'='*60}")
        print(f"📊 Results:")
        print(f"{'='*60}{Style.RESET_ALL}")
        print(f"{results}")

    def registry_info(self, registry):
        """Print static problem registry information."""
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"📚 Static Problem Registry")
        print(f"{'='*60}{Style.RESET_ALL}\n")

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

            print(f"{Fore.GREEN}📂 {dataset}:{Style.RESET_ALL}")
            print(f"   Total: {len(pids)} problems")
            print(f"   Task breakdown:")
            for task_type, count in sorted(task_counts.items()):
                print(f"      • {task_type}: {count} problems")
            print()

        print(f"{Fore.YELLOW}✅ Total problems in registry: {len(registry.PROBLEM_REGISTRY)}{Style.RESET_ALL}")
        print(f"{Fore.CYAN}{'='*60}{Style.RESET_ALL}\n")

    def _parse_react_response(self, text):
        """Parse ReAct format response into thought and action components."""
        import re

        thought = ""
        action = ""

        # Extract thought section
        thought_match = re.search(r'Thought:\s*(.*?)(?=\nAction:|$)', text, re.IGNORECASE | re.DOTALL)
        if thought_match:
            thought = thought_match.group(1).strip()

        # Extract action section - only content within code blocks
        action_match = re.search(r'Action:\s*(.*?)(?=$|\n(?:Thought|Action|Observation):)', text, re.IGNORECASE | re.DOTALL)
        if action_match:
            action_section = action_match.group(1).strip()

            # Extract only the code block content
            code_block_match = re.search(r'```(?:\w+)?\s*(.*?)\s*```', action_section, re.DOTALL)
            if code_block_match:
                action = code_block_match.group(1).strip()
            else:
                # Fallback: use the first line if no code block
                action = action_section.split('\n')[0].strip()

        return thought.strip(), action.strip()
