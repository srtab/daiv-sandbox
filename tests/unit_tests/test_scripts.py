def test_init_meta_script_creates_root_and_seed_commits():
    """CMD_INIT_META_SCRIPT is exported and looks well-formed."""
    from daiv_sandbox.scripts import CMD_INIT_META_SCRIPT

    assert 'git -C "$META" init' in CMD_INIT_META_SCRIPT
    # Empty root commit so HEAD~1 always exists.
    assert '--allow-empty -m "root"' in CMD_INIT_META_SCRIPT
    # Seed commit captures the freshly-extracted workspace.
    assert '--allow-empty -m "seed"' in CMD_INIT_META_SCRIPT
    # Excludes file is present so .git inside NEW is ignored.
    assert "core.excludesFile" in CMD_INIT_META_SCRIPT


def test_turn_diff_script_advances_head_and_emits_diff():
    """CMD_TURN_DIFF_SCRIPT is exported and emits HEAD~1..HEAD."""
    from daiv_sandbox.scripts import CMD_TURN_DIFF_SCRIPT

    assert 'git -C "$META" --work-tree="$NEW" add -A' in CMD_TURN_DIFF_SCRIPT
    assert '--allow-empty -m "turn"' in CMD_TURN_DIFF_SCRIPT
    assert "HEAD~1..HEAD" in CMD_TURN_DIFF_SCRIPT
    assert "diff -M --binary" in CMD_TURN_DIFF_SCRIPT


def test_old_extractor_script_removed():
    """CMD_GIT_DIFF_EXTRACTOR_SCRIPT no longer exists."""
    from daiv_sandbox import scripts

    assert not hasattr(scripts, "CMD_GIT_DIFF_EXTRACTOR_SCRIPT")
