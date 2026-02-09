"""ReAct client for Static Problems (OpenRCA datasets).

Similar to react.py but for static dataset problems instead of live microservices.
"""

import asyncio
import json
import sys
from pathlib import Path

# Add aiopslab to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator import Orchestrator
from clients.utils.llm import GPTClient
from clients.utils.templates import DOCS


RESP_INSTR = """DO NOT REPEAT ACTIONS! Respond with:
Thought: <your thought on the previous output>
Action: <your action towards detecting/localizing the issue>
"""


def count_message_tokens(message, enc):
    import tiktoken
    tokens = 4
    tokens += len(enc.encode(message.get("content", "")))
    return tokens


def trim_history_to_token_limit(history, max_tokens=120000, model="gpt-4"):
    import tiktoken
    enc = tiktoken.encoding_for_model(model)

    trimmed = []
    total_tokens = 0

    last_msg = history[-1]
    last_msg_tokens = count_message_tokens(last_msg, enc)

    if last_msg_tokens > max_tokens:
        truncated_content = enc.decode(enc.encode(last_msg["content"])[:max_tokens - 4])
        return [{"role": last_msg["role"], "content": truncated_content}]

    trimmed.insert(0, last_msg)
    total_tokens += last_msg_tokens

    for message in reversed(history[:-1]):
        message_tokens = count_message_tokens(message, enc)
        if total_tokens + message_tokens > max_tokens:
            break
        trimmed.insert(0, message)
        total_tokens += message_tokens

    return trimmed


class Agent:
    def __init__(self):
        self.history = []
        self.llm = GPTClient(auth_type="azure_key")

    def init_context(self, problem_desc: str, instructions: str, apis: str):
        """Initialize the context for the agent."""

        self.shell_api = self._filter_dict(apis, lambda k, _: "exec_shell" in k)
        self.submit_api = self._filter_dict(apis, lambda k, _: "submit" in k)
        self.telemetry_apis = self._filter_dict(
            apis, lambda k, _: "exec_shell" not in k and "submit" not in k
        )

        stringify_apis = lambda apis: "\n\n".join(
            [f"{k}\n{v}" for k, v in apis.items()]
        )

        self.system_message = DOCS.format(
            prob_desc=problem_desc,
            telemetry_apis=stringify_apis(self.telemetry_apis),
            shell_api=stringify_apis(self.shell_api),
            submit_api=stringify_apis(self.submit_api),
        )

        self.task_message = instructions

        self.history.append({"role": "system", "content": self.system_message})
        self.history.append({"role": "user", "content": self.task_message})

    async def get_action(self, input) -> str:
        """Wrapper to interface the agent with OpsBench."""
        self.history.append({"role": "user", "content": self._add_instr(input)})
        trimmed_history = trim_history_to_token_limit(self.history)
        response = self.llm.run(trimmed_history)
        self.history.append({"role": "assistant", "content": response[0]})
        return response[0]

    def _filter_dict(self, dictionary, filter_func):
        return {k: v for k, v in dictionary.items() if filter_func(k, v)}

    def _add_instr(self, input):
        return input + "\n\n" + RESP_INSTR


if __name__ == "__main__":
    # Static problem IDs
    static_problems = {
        "openrca-bank-detection-0": "aiopslab.orchestrator.static_problems.openrca_detection:OpenRCABankDetection",
        "openrca-telecom-detection-0": "aiopslab.orchestrator.static_problems.openrca_detection:OpenRCATelecomDetection",
        "openrca-market-cb1-detection-0": "aiopslab.orchestrator.static_problems.openrca_detection:OpenRCAMarketCloudbed1Detection",
        "openrca-market-cb2-detection-0": "aiopslab.orchestrator.static_problems.openrca_detection:OpenRCAMarketCloudbed2Detection",
    }

    results_dir = Path("results/static_problems")
    results_dir.mkdir(parents=True, exist_ok=True)

    for problem_id, problem_class_path in static_problems.items():
        print(f"\n{'='*60}")
        print(f"Running Problem: {problem_id}")
        print(f"{'='*60}\n")

        agent = Agent()
        orchestrator = Orchestrator()
        orchestrator.register_agent(agent, name="react")

        try:
            # Import problem class dynamically
            module_path, class_name = problem_class_path.split(":")
            module = __import__(module_path, fromlist=[class_name])
            problem_class = getattr(module, class_name)

            # Initialize problem directly
            problem = problem_class()

            # Manually init problem in orchestrator
            orchestrator.problem = problem
            orchestrator.session.create_problem_dir(problem_id)

            # Get problem description and APIs
            problem_desc = problem.app.get_app_summary()
            instructions = problem.get_task_instruction()
            apis = orchestrator._get_actions_dict()

            agent.init_context(problem_desc, instructions, apis)

            # Start problem
            full_output = asyncio.run(orchestrator.start_problem(max_steps=30))
            results = full_output.get("results", {})

            # Save results
            filename = results_dir / f"react_{problem_id}.json"
            with open(filename, "w") as f:
                json.dump(results, f, indent=2)

            print(f"\n✓ Results saved to: {filename}")

        except Exception as e:
            print(f"\n✗ Error while running problem {problem_id}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"All static problems completed!")
    print(f"Results saved to: {results_dir}")
    print(f"{'='*60}\n")
