from typing import Literal

from pydantic import BaseModel


class PRSummary(BaseModel):
    summary: str
    risk: Literal["low", "medium", "high"]
    key_changes: list[str]
    reviewer_focus: list[str]
    cost_usd: float
