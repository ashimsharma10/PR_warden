from types import SimpleNamespace

import pytest

from pr_warden.agent.context import PRContext
from pr_warden.agent.loop import MODEL, run_agent
from pr_warden.agent.schemas import DoneInput, ToolResult
from pr_warden.agent.tools import build_tools, tool_to_anthropic_schema
from pr_warden.agent.tools.find_references import FindReferencesTool, find_symbol_refs
from pr_warden.agent.tools.get_file import GetFileTool
from pr_warden.agent.tools.get_issue import GetIssueTool
from pr_warden.agent.tools.get_pr_diff import GetPRDiffTool
from pr_warden.github.schemas import GitHubUser, PullRequest, Ref


def _pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=7,
        title="fix: stop double-charging on retry",
        body="Fixes #42 — payments were charged twice on webhook retry.",
        state="open",
        draft=False,
        head=Ref(ref="fix/double-charge", sha="head123"),
        base=Ref(ref="main", sha="base000"),
        user=GitHubUser(login="dev", id=1),
        changed_files=1,
        additions=10,
        deletions=2,
    )
    defaults.update(kwargs)
    return PullRequest(**defaults)


def _ctx(files=None, **pr_kwargs) -> PRContext:
    return PRContext(
        token="tok",
        repo="acme/widgets",
        pr=_pr(**pr_kwargs),
        files=files if files is not None else [
            {"filename": "payments.py", "status": "modified",
             "additions": 10, "deletions": 2, "patch": "@@\n+if not already_charged:"},
        ],
    )


def _tool_use(name, input, id="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def _text(text="thinking"):
    return SimpleNamespace(type="text", text=text)


def _resp(content, in_tokens=100, out_tokens=20):
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


def _scripted_send(responses):
    """A fake `send` that yields canned responses in order."""
    it = iter(responses)

    async def send(*, model, system, tools, messages):
        return next(it)

    return send


_DONE_ARGS = {
    "summary": "Guards the charge behind an already_charged check.",
    "files_touched": ["payments"],
    "intent_matches_diff": True,
    "intent_mismatch_reason": "",
    "notable": ["No test added for the retry path"],
    "open_questions": [],
    "confidence": 0.8,
}


# ── schema ──────────────────────────────────────────────────────────────────

def test_done_input_confidence_bounds():
    with pytest.raises(Exception):
        DoneInput(summary="x", intent_matches_diff=True, confidence=1.5)


def test_tool_to_anthropic_schema_shape():
    schema = tool_to_anthropic_schema(GetFileTool())
    assert schema["name"] == "get_file"
    assert schema["description"]
    assert "properties" in schema["input_schema"]


def test_build_tools_includes_done():
    names = {t.name for t in build_tools()}
    assert "done" in names
    assert {"get_file", "get_pr_diff", "get_issue", "find_references"} <= names


# ── loop ────────────────────────────────────────────────────────────────────

async def test_loop_immediate_done():
    send = _scripted_send([_resp([_tool_use("done", _DONE_ARGS)])])
    res = await run_agent(_ctx(), api_key="k", send=send)
    assert res.stopped_for == "done"
    assert res.assessment.confidence == 0.8
    assert res.tool_call_count == 0
    assert res.cost_usd > 0


async def test_loop_runs_tool_then_done():
    send = _scripted_send([
        _resp([_tool_use("get_pr_diff", {}, id="a")]),
        _resp([_tool_use("done", _DONE_ARGS, id="b")]),
    ])
    res = await run_agent(_ctx(), api_key="k", send=send)
    assert res.stopped_for == "done"
    assert res.tool_call_count == 1
    assert res.trace[0]["tool"] == "get_pr_diff"
    assert res.trace[0]["ok"] is True


async def test_loop_invalid_done_retries():
    bad = {**_DONE_ARGS, "confidence": 5.0}  # out of range
    send = _scripted_send([
        _resp([_tool_use("done", bad, id="a")]),
        _resp([_tool_use("done", _DONE_ARGS, id="b")]),
    ])
    res = await run_agent(_ctx(), api_key="k", send=send)
    assert res.stopped_for == "done"
    assert res.assessment.confidence == 0.8


async def test_loop_no_tool_use_force_finalizes_with_done():
    send = _scripted_send([
        _resp([_text("I'll just talk")]),
        _resp([_tool_use("done", _DONE_ARGS)]),  # forced finalize asks again
    ])
    res = await run_agent(_ctx(), api_key="k", send=send)
    assert res.stopped_for == "no_tool_call"
    assert res.assessment.confidence == 0.8


async def test_loop_force_finalize_fallback_when_still_no_done():
    send = _scripted_send([
        _resp([_text("no tools")]),
        _resp([_text("still no done")]),  # forced finalize also fails
    ])
    res = await run_agent(_ctx(), api_key="k", send=send)
    assert res.stopped_for == "no_tool_call"
    assert res.assessment.confidence == 0.0
    assert res.assessment.open_questions  # fallback notes incompleteness


async def test_loop_tool_budget_exhausted():
    # Always ask for a tool; loop must stop at MAX_TOOL_CALLS, then finalize.
    many = [_resp([_tool_use("get_pr_diff", {}, id=str(i))]) for i in range(20)]
    many.append(_resp([_tool_use("done", _DONE_ARGS)]))  # forced finalize
    res = await run_agent(_ctx(), api_key="k", send=_scripted_send(many))
    assert res.stopped_for == "tool_call_budget"
    assert res.tool_call_count >= 12


async def test_loop_parallel_tool_calls_counted():
    send = _scripted_send([
        _resp([
            _tool_use("get_pr_diff", {}, id="a"),
            _tool_use("get_pr_diff", {"file_path": "payments.py"}, id="b"),
        ]),
        _resp([_tool_use("done", _DONE_ARGS)]),
    ])
    res = await run_agent(_ctx(), api_key="k", send=send)
    assert res.tool_call_count == 2
    assert len(res.trace) == 2


# ── get_file ────────────────────────────────────────────────────────────────

async def test_get_file_region(monkeypatch):
    from pr_warden.github import client

    async def fake(token, repo, path, ref=None):
        return "\n".join(f"line{i}" for i in range(1, 11))

    monkeypatch.setattr(client, "get_repo_file", fake)
    res = await GetFileTool().run(_ctx(), GetFileTool.input_schema(path="a.py", start_line=2, end_line=4))
    assert res.ok
    assert "line2" in res.content and "line4" in res.content
    assert "line5" not in res.content


async def test_get_file_not_found(monkeypatch):
    from pr_warden.github import client

    async def fake(token, repo, path, ref=None):
        return None

    monkeypatch.setattr(client, "get_repo_file", fake)
    res = await GetFileTool().run(_ctx(), GetFileTool.input_schema(path="missing.py"))
    assert not res.ok
    assert res.error == "not_found"


# ── get_pr_diff ─────────────────────────────────────────────────────────────

async def test_get_pr_diff_full():
    res = await GetPRDiffTool().run(_ctx(), GetPRDiffTool.input_schema())
    assert res.ok and "payments.py" in res.content


async def test_get_pr_diff_specific_file():
    res = await GetPRDiffTool().run(_ctx(), GetPRDiffTool.input_schema(file_path="payments.py"))
    assert res.ok and "already_charged" in res.content


async def test_get_pr_diff_missing_file():
    res = await GetPRDiffTool().run(_ctx(), GetPRDiffTool.input_schema(file_path="nope.py"))
    assert not res.ok and res.error == "not_found"


# ── get_issue ───────────────────────────────────────────────────────────────

async def test_get_issue(monkeypatch):
    from pr_warden.github import client

    async def fake_issue(token, repo, number):
        return {"number": number, "title": "Double charge", "state": "open",
                "labels": [{"name": "bug"}], "body": "Charged twice."}

    async def fake_comments(token, repo, number, limit=10):
        return [{"user": {"login": "alice"}, "body": "still seeing it"}]

    monkeypatch.setattr(client, "get_issue", fake_issue)
    monkeypatch.setattr(client, "list_issue_comments", fake_comments)
    res = await GetIssueTool().run(_ctx(), GetIssueTool.input_schema(number=42))
    assert res.ok
    assert "Double charge" in res.content and "@alice" in res.content


async def test_get_issue_not_found(monkeypatch):
    from pr_warden.github import client

    async def fake_issue(token, repo, number):
        return None

    monkeypatch.setattr(client, "get_issue", fake_issue)
    res = await GetIssueTool().run(_ctx(), GetIssueTool.input_schema(number=999))
    assert not res.ok and res.error == "not_found"


# ── find_references (AST) ───────────────────────────────────────────────────

def test_find_symbol_refs_name_and_attribute():
    src = "x = login()\nobj.login()\n# login in a comment\ns = 'login in a string'\n"
    lines = find_symbol_refs(src, "login")
    assert lines == [1, 2]  # comment and string are ignored


def test_find_symbol_refs_syntax_error_returns_empty():
    assert find_symbol_refs("def (:\n", "login") == []


async def test_find_references_tool(monkeypatch):
    from pr_warden.github import client

    async def fake_tree(token, repo, branch="HEAD"):
        return ["a.py", "b.py", "README.md"]

    files = {"a.py": "login()\n", "b.py": "# login\nx = 1\n"}

    async def fake_file(token, repo, path, ref=None):
        return files.get(path)

    monkeypatch.setattr(client, "list_repo_tree", fake_tree)
    monkeypatch.setattr(client, "get_repo_file", fake_file)
    res = await FindReferencesTool().run(
        _ctx(), FindReferencesTool.input_schema(symbol="login", file_path="a.py")
    )
    assert res.ok
    assert "a.py:1" in res.content
    assert "b.py" not in res.content  # only a comment mention there


async def test_find_references_none_found(monkeypatch):
    from pr_warden.github import client

    async def fake_tree(token, repo, branch="HEAD"):
        return ["a.py"]

    async def fake_file(token, repo, path, ref=None):
        return "x = 1\n"

    monkeypatch.setattr(client, "list_repo_tree", fake_tree)
    monkeypatch.setattr(client, "get_repo_file", fake_file)
    res = await FindReferencesTool().run(
        _ctx(), FindReferencesTool.input_schema(symbol="login", file_path="a.py")
    )
    assert res.ok and "No references" in res.content


# ── diff section (prioritized inclusion) ─────────────────────────────────────

def test_render_diff_small_files_all_inline():
    from pr_warden.agent.prompts import render_diff_section

    files = [
        {"filename": "a.py", "additions": 2, "deletions": 0, "patch": "@@\n+a"},
        {"filename": "b.py", "additions": 2, "deletions": 0, "patch": "@@\n+b"},
    ]
    out = render_diff_section(files)
    assert "+a" in out and "+b" in out
    assert "withheld" not in out


def test_render_diff_oversized_file_is_stubbed():
    from pr_warden.agent.prompts import render_diff_section

    big = "@@\n" + "\n".join(f"+line{i}" for i in range(5000))  # > 8k chars
    files = [
        {"filename": "small.py", "additions": 1, "deletions": 0, "patch": "@@\n+x"},
        {"filename": "huge.py", "additions": 5000, "deletions": 0, "patch": big},
    ]
    out = render_diff_section(files)
    assert "+x" in out                       # small file inlined
    assert "line4999" not in out             # huge file not inlined
    assert 'get_pr_diff(file_path="huge.py")' in out
    assert "withheld" in out


def test_render_diff_total_budget_stubs_overflow():
    from pr_warden.agent.prompts import render_diff_section

    # Each ~5k chars; 10 of them exceed the 30k total budget → some withheld.
    files = [
        {"filename": f"f{i}.py", "additions": 1, "deletions": 0,
         "patch": "@@\n" + "x" * 5000}
        for i in range(10)
    ]
    out = render_diff_section(files)
    assert "withheld" in out
    inlined = sum(1 for i in range(10) if f"--- f{i}.py ---" in out)
    assert 0 < inlined < 10


def test_render_diff_binary_file_no_patch():
    from pr_warden.agent.prompts import render_diff_section

    out = render_diff_section([{"filename": "logo.png", "additions": 0, "deletions": 0}])
    assert "no inline diff" in out
    assert "withheld" not in out


async def test_withheld_file_still_readable_via_get_pr_diff():
    # The escape hatch the stub points at must actually work.
    files = [{"filename": "huge.py", "additions": 1, "deletions": 0,
              "patch": "@@\n+secret_logic()"}]
    ctx = _ctx(files=files)
    res = await GetPRDiffTool().run(ctx, GetPRDiffTool.input_schema(file_path="huge.py"))
    assert res.ok and "secret_logic" in res.content


# ── default model wiring ─────────────────────────────────────────────────────

def test_default_model_is_sonnet():
    assert MODEL.startswith("claude-sonnet-4")
