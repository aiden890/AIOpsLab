"""ReAct client for Static Dataset Problems (OpenRCA).

Same pattern as react.py but uses StaticOrchestrator for static dataset problems.

Usage:
    # Run all static problems
    python clients/react_static.py

    # Run specific dataset
    python clients/react_static.py --dataset openrca_bank

    # Run specific task type
    python clients/react_static.py --task-type task_7

    # Run single problem
    python clients/react_static.py --problem openrca_bank-task_1-0
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tiktoken
from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from clients.utils.llm import GPTClient
from clients.utils.templates import DOCS

RESP_INSTR = """Please avoid repeating previous actions. Respond with:
Thought: <your analysis of the previous output>
Action: <your next diagnostic step>
"""


def count_message_tokens(message, enc):
    tokens = 4
    tokens += len(enc.encode(message.get("content", "")))
    return tokens


def trim_history_to_token_limit(history, max_tokens=120000, model="gpt-4"):
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
        self.history.append({"role": "user", "content": self._add_instr(input)})
        trimmed_history = trim_history_to_token_limit(self.history)
        response = self.llm.run(trimmed_history)
        self.history.append({"role": "assistant", "content": response[0]})
        return response[0]

    def _filter_dict(self, dictionary, filter_func):
        return {k: v for k, v in dictionary.items() if filter_func(k, v)}

    def _add_instr(self, input):
        return input + "\n\n" + RESP_INSTR


def parse_args():
    parser = argparse.ArgumentParser(description="ReAct agent for OpenRCA static problems")
    parser.add_argument("--problem", type=str, help="Single problem ID to run")
    parser.add_argument("--dataset", type=str, help="Filter by dataset (e.g. openrca_bank)")
    parser.add_argument("--task-type", type=str, help="Filter by task type (e.g. task_1, task_7)")
    parser.add_argument("--max-steps", type=int, default=30, help="Max agent steps (default: 30)")
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    orchestrator = StaticOrchestrator(results_dir=str(results_dir))

    # Select problems to run
    if args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = orchestrator.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    print(f"Running {len(problem_ids)} problems")
    print(f"Results dir: {results_dir}\n")

    for pid in problem_ids:
        print(f"\n{'='*60}")
        print(f"Problem: {pid}")
        print(f"{'='*60}\n")

        agent = Agent()
        orchestrator.register_agent(agent, name="react-static")

        try:
            problem_desc, instructs, apis = orchestrator.init_problem(pid)
            agent.init_context(problem_desc, instructs, apis)

            # Print initial problem setup
            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            full_output = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
            results = full_output.get("results", {})

            # Session already saved with descriptive name
            print(f"\nSession saved by orchestrator")
            print(f"Score: {results.get('score', 'N/A')}")
            print(f"Success: {results.get('success', 'N/A')}")

        except Exception as e:
            print(f"\nError running {pid}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Done! {len(problem_ids)} problems completed.")
    print(f"{'='*60}")
