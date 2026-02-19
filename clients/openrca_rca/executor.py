"""Executor: generates and runs Python code in IPython kernel.

Adapted from OpenRCA executor.py.
Key difference: uses `telemetry` helper (pre-injected in kernel) instead of direct file paths.
"""

import re
import time
import traceback
from datetime import datetime

import tiktoken

from clients.openrca_rca.api_router import get_chat_completion
from clients.openrca_rca.prompts.executor_prompt import (
    rule,
    system_template,
    code_format,
    summary_template,
    conclusion_template,
)


def execute_act(instruction, background, history, kernel, configs, logger):
    """Execute an instruction by generating and running Python code.

    Args:
        instruction: Natural language instruction from Controller.
        background: Domain knowledge (schema + candidates).
        history: Executor conversation history (list of dicts).
        kernel: IPython InteractiveShellEmbed instance (with telemetry injected).
        configs: LLM API config dict.
        logger: Logger instance.

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

    # Up to 2 attempts (initial + 1 retry on error)
    retry_flag = False
    for attempt in range(2):
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

            # Execute in IPython kernel
            exec_result = kernel.run_cell(code)
            status = exec_result.success

            if status:
                result = str(exec_result.result).strip()

                # Check token length
                tokens_len = len(tokenizer.encode(result))
                if tokens_len > 16384:
                    logger.warning(f"Token length exceeds limit: {tokens_len}")
                    continue

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
                history.append({"role": "user", "content": summary_template.format(result=result)})

                answer = get_chat_completion(history, configs)
                logger.debug(f"Brief Answer:\n{answer}")

                history.append({"role": "assistant", "content": answer})
                result = conclusion_template.format(answer=answer, result=result)

                return code, result, status, history
            else:
                # Execution failed - format error and retry
                err_msg = "".join(traceback.format_exception(
                    type(exec_result.error_in_exec),
                    exec_result.error_in_exec,
                    exec_result.error_in_exec.__traceback__,
                ))
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
