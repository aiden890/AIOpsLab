"""Executor: generates and runs Python code in an IPython kernel.

Key difference from the original OpenRCA executor: uses the pre-injected
`telemetry` helper object instead of direct file paths.
"""

import re
import time
import traceback
from datetime import datetime

import tiktoken

from aiopslab.orchestrator.static_actions.executor.api_router import get_chat_completion
from aiopslab.orchestrator.static_actions.executor.prompts.executor_prompt import (
    rule,
    system_template,
    code_format,
    summary_template,
    conclusion_template,
)


def execute_act(instruction, background, history, kernel, configs, logger,
                max_retries=3):
    """Execute an instruction by generating and running Python code.

    Args:
        instruction: Natural language instruction from Controller.
        background: Domain knowledge (schema + candidates).
        history: Executor conversation history (list of dicts).
        kernel: IPython InteractiveShellEmbed instance (with telemetry injected).
        configs: LLM API config dict.
        logger: Logger instance.
        max_retries: Max attempts on execution error (default: 3).

    Returns:
        tuple: (code, result, success, updated_history)
    """
    logger.debug("Start execution")
    t1 = datetime.now()

    if not history:
        history = [
            {"role": "system", "content": system_template.format(
                rule=rule, background=background, format=code_format
            )},
        ]

    code_pattern = re.compile(r"```python\n(.*?)\n```", re.DOTALL)
    code = ""
    result = ""
    status = False

    history.append({"role": "user", "content": instruction})
    prompt = history.copy()
    note = [{"role": "user", "content": (
        f"Continue your code writing process following the rules:\n\n{rule}\n\n"
        f"Response format:\n\n{code_format}"
    )}]

    tokenizer = tiktoken.encoding_for_model("gpt-4")

    retry_flag = False
    for attempt in range(max_retries):
        try:
            if not retry_flag:
                response = get_chat_completion(prompt + note, configs)
            else:
                response = get_chat_completion(prompt, configs)
                retry_flag = False

            # Extract Python code block
            match = re.search(code_pattern, response)
            code = match.group(1).strip() if match else response.strip()

            logger.debug(f"Raw Code:\n{code}")

            # Block visualization libraries
            if "import matplotlib" in code or "import seaborn" in code:
                logger.warning("Visualization code detected, requesting rewrite.")
                prompt.append({"role": "assistant", "content": code})
                prompt.append({"role": "user", "content": (
                    "You are not permitted to generate visualizations. "
                    "Please provide text-based results instead."
                )})
                continue

            # Execute in IPython kernel, capturing stdout as fallback
            from IPython.utils.capture import capture_output
            with capture_output() as captured:
                exec_result = kernel.run_cell(code)
            status = exec_result.success

            if status:
                # Prefer expression result; fall back to captured stdout
                if exec_result.result is not None:
                    result = str(exec_result.result).strip()
                else:
                    result = captured.stdout.strip()
                if not result:
                    result = "(Code executed successfully with no output)"

                # Truncate overly long results before summarization
                tokens_len = len(tokenizer.encode(result))
                was_truncated = False
                if tokens_len > 16384:
                    logger.warning(f"Token length exceeds limit: {tokens_len}, truncating")
                    result = result[:8000] + "\n\n[... truncated ...]\n\n" + result[-2000:]
                    was_truncated = True

                t2 = datetime.now()

                # Warn about truncated DataFrames
                row_pattern = r"\[(\d+)\s+rows\s+x\s+\d+\s+columns\]"
                row_match = re.search(row_pattern, result)
                if row_match and int(row_match.group(1)) > 10:
                    result += (
                        "\n\n**Note**: The printed pandas DataFrame is truncated. "
                        "Only **10 rows** are displayed. Use `df.head(X)` to display more rows."
                    )

                logger.debug(f"Execution Result:\n{result}")
                logger.debug(f"Execution finished. Time cost: {t2 - t1}")

                # Summarize result with LLM
                history.append({"role": "assistant", "content": code})
                summary_input = summary_template.format(result=result)
                if was_truncated:
                    summary_input += (
                        "\n\nWARNING: The output was truncated due to excessive length. "
                        "Summarize based on the visible portion. Note any gaps in the data."
                    )
                history.append({"role": "user", "content": summary_input})

                answer = get_chat_completion(history, configs)
                logger.debug(f"Brief Answer:\n{answer}")

                history.append({"role": "assistant", "content": answer})
                result = conclusion_template.format(answer=answer, result=result)

                return code, result, status, history
            else:
                # Execution failed - format error and retry
                # SyntaxErrors go to error_before_exec; runtime errors to error_in_exec
                err_obj = exec_result.error_in_exec or exec_result.error_before_exec
                if err_obj is not None:
                    err_msg = "".join(traceback.format_exception(
                        type(err_obj), err_obj, err_obj.__traceback__,
                    ))
                else:
                    err_msg = captured.stderr.strip() if captured.stderr else "Unknown execution error"
                t2 = datetime.now()
                logger.warning(f"Execution failed. Error: {err_msg}")
                logger.debug(f"Time cost: {t2 - t1}")

                prompt.append({"role": "assistant", "content": code})
                prompt.append({"role": "user", "content": (
                    f"Execution failed:\n{err_msg}\nPlease revise your code and retry."
                )})
                retry_flag = True

        except Exception as e:
            logger.error(e)
            time.sleep(1)

    t2 = datetime.now()
    logger.error(f"Max retries reached. Time cost: {t2 - t1}")
    err = "The Executor failed to complete the instruction, please re-write a new instruction for Executor."
    history.append({"role": "assistant", "content": err})
    return err, err, True, history
