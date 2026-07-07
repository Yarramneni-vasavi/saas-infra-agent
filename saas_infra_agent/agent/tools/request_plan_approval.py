"""Human-in-the-loop gate: pause the build until a human approves the plan.

Calling `interrupt()` inside the tool suspends the whole graph run at this
point; the pending state is persisted by the shared checkpointer. The
orchestrator surfaces the interrupt payload's `prompt` to the user and resumes
the same thread with `Command(resume=<user reply>)`, which becomes this tool's
return value.
"""

from langchain.tools import tool
from langgraph.types import interrupt

from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


@tool
def request_plan_approval(plan: str) -> str:
    """
    Ask the human to approve the build plan before any artifacts are written.

    Call this after write_todos, passing a concise human-readable summary of
    the plan: deployment target, files to be generated, and any assumptions.
    Execution pauses until the human replies; their reply is returned verbatim.
    Proceed only on an explicit approval — otherwise revise the plan and call
    this tool again with the updated plan.
    """
    logger.info("Tool called: request_plan_approval — pausing for human approval")
    reply = interrupt(
        {
            "type": "build_plan_approval",
            "prompt": (
                "The BUILD agent proposes the following plan:\n\n"
                f"{plan}\n\n"
                "Reply 'approve' to start the build, or describe what to change."
            ),
        }
    )
    return str(reply)
