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
import csv
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tiktoken
from IPython.terminal.embed import InteractiveShellEmbed
from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from clients.utils.llm import GPTClient
from clients.utils.templates import DOCS_WITH_POSSIBLE_ROOT_CAUSES
from clients.openrca_rca.telemetry_helper import TelemetryHelper

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

    def get_model_name(self):
        """Return the model name used by this agent."""
        return self.llm.get_model_name()

    def init_context(self, problem_desc: str, instructions: str, apis: str, possible_rca: dict = None):
        self.shell_api = self._filter_dict(apis, lambda k, _: "exec_shell" in k)
        self.submit_api = self._filter_dict(apis, lambda k, _: "submit" in k)
        self.telemetry_apis = self._filter_dict(
            apis, lambda k, _: "exec_shell" not in k and "submit" not in k
        )

        stringify_apis = lambda apis: "\n\n".join(
            [f"{k}{v}" for k, v in apis.items()]
        )

        self.system_message = DOCS_WITH_POSSIBLE_ROOT_CAUSES.format(
            prob_desc=problem_desc,
            telemetry_apis=stringify_apis(self.telemetry_apis),
            shell_api=stringify_apis(self.shell_api),
            submit_api=stringify_apis(self.submit_api),
            possible_root_causes=self._format_possible_rca(possible_rca),
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

    def _format_possible_rca(self, prc: dict) -> str:
        if not prc:
            return ""
        lines = []
        levels = prc.get("component_levels", {})
        components = prc.get("components", [])
        reasons = prc.get("reasons", [])

        lines.append("## POSSIBLE ROOT CAUSE COMPONENTS:")
        if levels:
            if levels.get("node"):
                lines.append("\n(node level)")
                lines.extend(f"- {c}" for c in levels["node"])
            if levels.get("pod"):
                lines.append("\n(pod level)")
                lines.extend(f"- {c}" for c in levels["pod"])
            if levels.get("service"):
                lines.append("\n(service level)")
                lines.extend(f"- {c}" for c in levels["service"])
        else:
            lines.extend(f"- {c}" for c in components)

        lines.append("\n## POSSIBLE ROOT CAUSE REASONS:")
        lines.extend(f"- {r}" for r in reasons)

        return "\n".join(lines)

    def _add_instr(self, input):
        return input + "\n\n" + RESP_INSTR


def setup_executor(actions_obj, namespace):
    """Create an IPython kernel and inject a Python code executor into the actions object.

    The react agent writes Python code directly, so we run it in IPython and
    return the result — no LLM code-generation step needed.
    """
    kernel = InteractiveShellEmbed()
    helper = TelemetryHelper(actions_obj, namespace)
    kernel.push({"telemetry": helper})
    kernel.run_cell(
        "import pandas as pd\n"
        "pd.set_option('display.width', 427)\n"
        "pd.set_option('display.max_columns', 10)\n"
    )

    def _run_code(code: str) -> str:
        exec_result = kernel.run_cell(code)
        if exec_result.success:
            out = str(exec_result.result).strip() if exec_result.result is not None else ""
            return out if out else "(code executed successfully, no output)"
        else:
            import traceback as tb
            err = "".join(tb.format_exception(
                type(exec_result.error_in_exec),
                exec_result.error_in_exec,
                exec_result.error_in_exec.__traceback__,
            ))
            return f"Execution error:\n{err}"

    actions_obj.set_executor(_run_code)
    return kernel


SCORE_FIELDS = [
    "timestamp", "eval_id", "model", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
]


def append_score(scores_path: Path, eval_id: str, model: str, pid: str, results: dict):
    """Append one row to the scores CSV for this eval run."""
    is_new = not scores_path.exists()
    with open(scores_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eval_id": eval_id,
            "model": model,
            "problem_id": pid,
            "task_type": results.get("task_type", ""),
            "difficulty": results.get("difficulty", ""),
            "score": results.get("score", ""),
            "success": results.get("success", ""),
            "steps": results.get("steps", ""),
            "TTA": round(results.get("TTA", 0), 2),
            "in_tokens": results.get("in_tokens", ""),
            "out_tokens": results.get("out_tokens", ""),
        })


def parse_args():
    parser = argparse.ArgumentParser(description="ReAct agent for OpenRCA static problems")
    parser.add_argument("--problem", type=str, help="Single problem ID to run")
    parser.add_argument("--dataset", type=str, help="Filter by dataset (e.g. openrca_bank)")
    parser.add_argument("--task-type", type=str, help="Filter by task type (e.g. task_1, task_7)")
    parser.add_argument("--max-steps", type=int, default=30, help="Max agent steps (default: 30)")
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    parser.add_argument("--eval-id", type=str, default=None, help="Eval run identifier (default: auto UUID)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]
    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)

    # Select problems to run
    if args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = orchestrator.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    scores_path = results_dir / f"{eval_id}_scores.csv"

    print(f"Running {len(problem_ids)} problems")
    print(f"Results dir: {results_dir}")
    print(f"Eval ID: {eval_id}")
    print(f"Scores file: {scores_path}\n")

    for pid in problem_ids:
        print(f"\n{'='*60}")
        print(f"Problem: {pid}")
        print(f"{'='*60}\n")

        agent = Agent()
        orchestrator.register_agent(agent, name="react-static")

        kernel = None
        try:
            problem_desc, instructs, apis = orchestrator.init_problem(pid)
            problem = orchestrator.session.problem
            possible_rca = problem.app.dataset_config.get("possible_root_causes")
            agent.init_context(problem_desc, instructs, apis, possible_rca=possible_rca)
            orchestrator._system_message = agent.system_message

            # Inject IPython executor so the execute() action works
            kernel = setup_executor(problem._actions, problem.namespace)

            # Print initial problem setup
            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            full_output = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
            results = full_output.get("results", {})

            append_score(scores_path, eval_id, agent.get_model_name(), pid, results)
            print(f"\nScore: {results.get('score', 'N/A')}  Success: {results.get('success', 'N/A')}")

        except Exception as e:
            print(f"\nError running {pid}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if kernel is not None:
                kernel.reset()

    print(f"\n{'='*60}")
    print(f"Done! {len(problem_ids)} problems completed.")
    print(f"{'='*60}")
