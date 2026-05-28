from pr_warden.repo_config import DEFAULT_CONFIG, RepoConfig, parse_config


def test_parse_none_returns_defaults():
    cfg = parse_config(None)
    assert cfg == DEFAULT_CONFIG


def test_parse_empty_string_returns_defaults():
    assert parse_config("") == DEFAULT_CONFIG


def test_parse_empty_yaml_doc_returns_defaults():
    assert parse_config("---\n") == DEFAULT_CONFIG


def test_invalid_yaml_falls_back_to_defaults():
    cfg = parse_config("key: : : not valid")
    assert isinstance(cfg, RepoConfig)
    assert cfg.checks.pr_size.file_limit == 25  # default


def test_yaml_not_a_mapping_falls_back():
    assert parse_config("- just\n- a\n- list") == DEFAULT_CONFIG


def test_partial_config_keeps_other_defaults():
    yaml_text = """
checks:
  pr_size:
    file_limit: 100
"""
    cfg = parse_config(yaml_text)
    assert cfg.checks.pr_size.file_limit == 100
    assert cfg.checks.pr_size.line_limit == 500  # default preserved
    assert cfg.checks.title_quality.enabled is True  # default preserved


def test_disable_a_check():
    cfg = parse_config("checks:\n  branch_naming:\n    enabled: false\n")
    assert cfg.checks.branch_naming.enabled is False
    assert cfg.checks.title_quality.enabled is True


def test_unknown_keys_ignored():
    yaml_text = """
checks:
  pr_size:
    file_limit: 40
    nonsense_key: hello
unknown_top_level: 1
"""
    cfg = parse_config(yaml_text)
    assert cfg.checks.pr_size.file_limit == 40


def test_branch_naming_custom_prefixes():
    cfg = parse_config(
        "checks:\n  branch_naming:\n    allowed_prefixes: [feature, bugfix]\n"
    )
    assert cfg.checks.branch_naming.allowed_prefixes == ["feature", "bugfix"]


def test_ai_branch_custom_tools():
    cfg = parse_config("checks:\n  ai_branch:\n    tools: [bard, gemini]\n")
    assert cfg.checks.ai_branch.tools == ["bard", "gemini"]


def test_no_tests_exempt_paths():
    cfg = parse_config(
        "checks:\n  no_tests:\n    exempt_paths:\n      - 'docs/**'\n      - 'scripts/*'\n"
    )
    assert cfg.checks.no_tests.exempt_paths == ["docs/**", "scripts/*"]
