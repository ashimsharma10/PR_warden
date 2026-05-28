from pr_warden.checks.registry import CheckResult
from pr_warden.composer import LABEL_CLEAN, LABEL_NEEDS_ATTENTION, build_comment, pick_label


def test_comment_contains_header():
    results = [CheckResult("title_quality", True, "")]
    assert "## PRwarden Review" in build_comment(results)


def test_comment_shows_pass_icon():
    results = [CheckResult("title_quality", True, "")]
    assert "✅" in build_comment(results)


def test_comment_shows_fail_icon_and_reason():
    results = [CheckResult("title_quality", False, "Title is a single word")]
    comment = build_comment(results)
    assert "❌" in comment
    assert "Title is a single word" in comment


def test_comment_all_passed_no_reasons():
    results = [
        CheckResult("title_quality", True, ""),
        CheckResult("description", True, ""),
        CheckResult("pr_size", True, "4 files, 100 lines"),
    ]
    comment = build_comment(results)
    assert comment.count("✅") == 3
    assert "❌" not in comment


def test_comment_mixed():
    results = [
        CheckResult("title_quality", False, "Generic single-word title"),
        CheckResult("description", True, ""),
    ]
    comment = build_comment(results)
    assert "✅" in comment
    assert "❌" in comment
    assert "Generic single-word title" in comment


def test_comment_includes_summary():
    results = [CheckResult("title_quality", True, "")]
    comment = build_comment(results, summary="This PR looks good overall.")
    assert "This PR looks good overall." in comment
    assert "### Summary" in comment


def test_comment_footer():
    comment = build_comment([CheckResult("a", True, "")])
    assert "/prwarden recheck" in comment


def test_pick_label_all_pass():
    results = [CheckResult("a", True, ""), CheckResult("b", True, "")]
    assert pick_label(results) == LABEL_CLEAN


def test_pick_label_any_fail():
    results = [CheckResult("a", True, ""), CheckResult("b", False, "reason")]
    assert pick_label(results) == LABEL_NEEDS_ATTENTION


def test_pick_label_all_fail():
    results = [CheckResult("a", False, "x"), CheckResult("b", False, "y")]
    assert pick_label(results) == LABEL_NEEDS_ATTENTION
