from pr_warden.commands import parse_command


def test_parses_known_commands():
    assert parse_command("/warden mute") == "mute"
    assert parse_command("/warden unmute") == "unmute"


def test_case_insensitive_prefix_and_command():
    assert parse_command("/WARDEN Mute") == "mute"
    assert parse_command("/Warden UNMUTE") == "unmute"


def test_tolerates_surrounding_whitespace():
    assert parse_command("   /warden mute   ") == "mute"


def test_only_first_line_counts():
    # Mentioning the command mid-comment (not the first line) does not trigger it.
    assert parse_command("nice work!\n/warden mute") is None
    # ...but a command on the first line with trailing prose-lines still parses.
    assert parse_command("/warden mute\nthanks") == "mute"


def test_unknown_subcommand_is_ignored():
    assert parse_command("/warden frobnicate") is None
    assert parse_command("/warden") is None


def test_extra_tokens_rejected():
    # Avoid accidental matches like "/warden mute please" — must be exactly two.
    assert parse_command("/warden mute please") is None


def test_wrong_prefix_ignored():
    assert parse_command("/mute") is None
    assert parse_command("/prwarden mute") is None


def test_non_command_text_ignored():
    assert parse_command("looks good to me") is None


def test_empty_and_none():
    assert parse_command("") is None
    assert parse_command("   ") is None
    assert parse_command(None) is None
