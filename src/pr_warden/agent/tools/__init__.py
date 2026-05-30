from pr_warden.agent.tools.base import Tool, tool_to_anthropic_schema
from pr_warden.agent.tools.done import DoneTool
from pr_warden.agent.tools.find_references import FindReferencesTool
from pr_warden.agent.tools.get_author_history import GetAuthorHistoryTool
from pr_warden.agent.tools.get_file import GetFileTool
from pr_warden.agent.tools.get_issue import GetIssueTool
from pr_warden.agent.tools.get_pr_diff import GetPRDiffTool
from pr_warden.agent.tools.get_repo_conventions import GetRepoConventionsTool

DONE_TOOL = "done"


def build_tools() -> list[Tool]:
    """The toolset offered to the agent. `done` must be present — it's how the
    loop terminates."""
    return [
        GetFileTool(),
        GetPRDiffTool(),
        GetIssueTool(),
        FindReferencesTool(),
        GetRepoConventionsTool(),
        GetAuthorHistoryTool(),
        DoneTool(),
    ]


__all__ = ["Tool", "tool_to_anthropic_schema", "build_tools", "DONE_TOOL"]
