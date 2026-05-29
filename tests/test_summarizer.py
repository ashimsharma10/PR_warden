import pytest

from pr_warden.checks import CheckContext
from pr_warden.github.schemas import GitHubUser, PullRequest, Ref
from pr_warden.summarizer.client import LLMResult, estimate_cost
from pr_warden.summarizer.diff import MAX_DIFF_CHARS, build_diff_text
from pr_warden.summarizer.runner import (
    format_summary,
    parse_summary,
    summarize_pr,
)
from pr_warden.summarizer.schemas import PRSummary


def _pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=1,
        title="feat: add OAuth login",
        body="Adds OAuth login via GitHub.",
        state="open",
        draft=False,
        head=Ref(ref="feat/oauth-login", sha="abc123"),
        base=Ref(ref="main", sha="000000"),
        user=GitHubUser(login="dev", id=1),
        changed_files=2,
        additions=40,
        deletions=5,
    )
    defaults.update(kwargs)
    return PullRequest(**defaults)


def _ctx(files=None, **pr_kwargs) -> CheckContext:
    return CheckContext(pr=_pr(**pr_kwargs), files=files or [])


_VALID_JSON = """
{
  "summary": "Adds OAuth login.",
  "risk": "medium",
  "key_changes": ["new auth module", "removes password flow"],
  "reviewer_focus": ["token storage"]
}
"""


# ── estimate_cost ──────────────────────────────────────────────────────────────

def test_estimate_cost_haiku_35():
    # 1M input + 1M output at (0.80, 4.00)
    assert estimate_cost("claude-3-5-haiku-latest", 1_000_000, 1_000_000) == pytest.approx(4.80)


def test_estimate_cost_unknown_model_falls_back():
    assert estimate_cost("some-unknown-model", 1_000_000, 0) == pytest.approx(0.80)


# ── build_diff_text ──────────────────────────────────────────────────────────────

def test_build_diff_text_includes_patches():
    files = [
        {"filename": "a.py", "patch": "@@\n+print(1)"},
        {"filename": "b.py", "patch": "@@\n+print(2)"},
    ]
    out = build_diff_text(files)
    assert "a.py" in out and "b.py" in out and "print(1)" in out


def test_build_diff_text_handles_no_patch():
    out = build_diff_text([{"filename": "image.png"}])
    assert "no inline diff" in out


def test_build_diff_text_truncates():
    big = "x" * (MAX_DIFF_CHARS + 5000)
    out = build_diff_text([{"filename": "big.py", "patch": big}])
    assert len(out) <= MAX_DIFF_CHARS + 100
    assert "truncated" in out


# ── parse_summary ──────────────────────────────────────────────────────────────

def test_parse_summary_valid():
    s = parse_summary(_VALID_JSON, cost_usd=0.01)
    assert s is not None
    assert s.risk == "medium"
    assert s.cost_usd == 0.01
    assert s.key_changes == ["new auth module", "removes password flow"]


def test_parse_summary_strips_code_fences():
    fenced = "```json\n" + _VALID_JSON.strip() + "\n```"
    s = parse_summary(fenced, cost_usd=0.0)
    assert s is not None and s.summary == "Adds OAuth login."


def test_parse_summary_invalid_json_returns_none():
    assert parse_summary("not json at all", cost_usd=0.0) is None


def test_parse_summary_missing_required_field_returns_none():
    # no 'risk' → schema validation fails
    assert parse_summary('{"summary": "x"}', cost_usd=0.0) is None


def test_parse_summary_bad_risk_value_returns_none():
    assert parse_summary('{"summary": "x", "risk": "catastrophic"}', cost_usd=0.0) is None


# ── summarize_pr (injected fake model) ──────────────────────────────────────────

async def test_summarize_pr_happy_path():
    async def fake_call(**kwargs):
        return LLMResult(text=_VALID_JSON, input_tokens=100, output_tokens=50,
                         cost_usd=0.0005, model="claude-3-5-haiku-latest")

    ctx = _ctx(files=[{"filename": "auth.py", "patch": "@@\n+def login(): ..."}])
    s = await summarize_pr(ctx, api_key="sk-test", model="claude-3-5-haiku-latest", call=fake_call)
    assert s is not None
    assert s.cost_usd == 0.0005
    assert s.risk == "medium"


async def test_summarize_pr_no_api_key():
    ctx = _ctx(files=[{"filename": "a.py", "patch": "@@\n+x"}])
    s = await summarize_pr(ctx, api_key="", model="m")
    assert s is None


async def test_summarize_pr_empty_diff():
    s = await summarize_pr(_ctx(files=[]), api_key="sk-test", model="m")
    assert s is None


async def test_summarize_pr_model_error_returns_none():
    async def boom(**kwargs):
        raise RuntimeError("api down")

    ctx = _ctx(files=[{"filename": "a.py", "patch": "@@\n+x"}])
    s = await summarize_pr(ctx, api_key="sk-test", model="m", call=boom)
    assert s is None


async def test_summarize_pr_unparseable_output_returns_none():
    async def fake_call(**kwargs):
        return LLMResult(text="sorry I can't", input_tokens=1, output_tokens=1,
                         cost_usd=0.0, model="m")

    ctx = _ctx(files=[{"filename": "a.py", "patch": "@@\n+x"}])
    s = await summarize_pr(ctx, api_key="sk-test", model="m", call=fake_call)
    assert s is None


# ── format_summary ──────────────────────────────────────────────────────────────

def test_format_summary_renders_sections():
    s = PRSummary(summary="Does a thing.", risk="high",
                  key_changes=["a", "b"], reviewer_focus=["c"], cost_usd=0.0)
    out = format_summary(s)
    assert "Does a thing." in out
    assert "high" in out
    assert "- a" in out and "- b" in out
    assert "- c" in out
