from typing import Literal

from pydantic import BaseModel, ConfigDict


class PRSummary(BaseModel):
    # The model returns summary/risk/key_changes/reviewer_focus; cost_usd is
    # filled in by the runner after the call. extra="ignore" keeps validation
    # forgiving if the model emits an unexpected key.
    model_config = ConfigDict(extra="ignore")

    summary: str
    risk: Literal["low", "medium", "high"]
    key_changes: list[str] = []
    reviewer_focus: list[str] = []
    cost_usd: float = 0.0
