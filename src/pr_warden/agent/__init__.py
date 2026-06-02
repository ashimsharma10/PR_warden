"""Stage 3: the tool-using review agent.

The agent is the LLM; the loop is our code. `run_agent` owns the loop, the
budget, and termination — the model only decides which tool to call next.
"""

from pr_warden.agent.context import PRContext
from pr_warden.agent.loop import run_agent
from pr_warden.agent.review import REVIEW_AGENT
from pr_warden.agent.schemas import AgentResult, DoneInput
from pr_warden.agent.spec import AgentSpec

__all__ = ["PRContext", "run_agent", "AgentResult", "DoneInput", "AgentSpec", "REVIEW_AGENT"]
