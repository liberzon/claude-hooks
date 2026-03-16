"""Comprehensive tests for smart_approve.py."""

import json
import os
import subprocess
import sys
import tempfile

import pytest

from smart_approve import (
    _skip_shell_value,
    command_matches_pattern,
    decide,
    decompose_command,
    extract_subshells,
    is_shell_structural,
    is_standalone_assignment,
    load_merged_settings,
    normalize_command,
    parse_bash_patterns,
    split_on_operators,
    strip_env_vars,
    strip_heredocs,
    strip_keyword_prefix,
    strip_redirections,
)


# ---------------------------------------------------------------------------
# Pattern parsing
# ---------------------------------------------------------------------------

class TestParseBashPatterns:
    def test_git_status(self):
        patterns = parse_bash_patterns(["Bash(git status:*)"])
        assert len(patterns) == 1
        assert patterns[0] == ("git status", "git status *")

    def test_rm(self):
        patterns = parse_bash_patterns(["Bash(rm:*)"])
        assert len(patterns) == 1
        assert patterns[0] == ("rm", "rm *")

    def test_skip_non_bash(self):
        patterns = parse_bash_patterns(["Read", "Write", "Skill(*)"])
        assert patterns == []

    def test_mixed(self):
        patterns = parse_bash_patterns([
            "Read",
            "Bash(git log:*)",
            "Write",
            "Bash(echo:*)",
            "Skill(*)",
        ])
        assert len(patterns) == 2
        assert patterns[0] == ("git log", "git log *")
        assert patterns[1] == ("echo", "echo *")

    def test_path_pattern(self):
        patterns = parse_bash_patterns(["Bash(/Users/yair/Library/Android/sdk/platform-tools/adb*)"])
        assert len(patterns) == 1
        # No colon, so prefix == glob
        assert patterns[0][0] == "/Users/yair/Library/Android/sdk/platform-tools/adb*"

    def test_comment_pattern(self):
        patterns = parse_bash_patterns(["Bash(#:*)"])
        assert len(patterns) == 1
        assert patterns[0] == ("#", "# *")

    def test_find_delete_pattern(self):
        patterns = parse_bash_patterns(["Bash(find*-delete:*)"])
        assert len(patterns) == 1
        assert patterns[0] == ("find*-delete", "find*-delete *")


# ---------------------------------------------------------------------------
# Command matching
# ---------------------------------------------------------------------------

class TestCommandMatchesPattern:
    def test_bare_command(self):
        patterns = parse_bash_patterns(["Bash(git status:*)"])
        assert command_matches_pattern("git status", patterns)

    def test_command_with_args(self):
        patterns = parse_bash_patterns(["Bash(git status:*)"])
        assert command_matches_pattern("git status --short", patterns)

    def test_no_match(self):
        patterns = parse_bash_patterns(["Bash(git status:*)"])
        assert not command_matches_pattern("git push origin main", patterns)

    def test_rm_match(self):
        patterns = parse_bash_patterns(["Bash(rm:*)"])
        assert command_matches_pattern("rm -rf /tmp/foo", patterns)
        assert command_matches_pattern("rm", patterns)

    def test_multiple_patterns(self):
        patterns = parse_bash_patterns([
            "Bash(git status:*)",
            "Bash(git diff:*)",
            "Bash(echo:*)",
        ])
        assert command_matches_pattern("git status", patterns)
        assert command_matches_pattern("git diff HEAD~1", patterns)
        assert command_matches_pattern("echo hello", patterns)
        assert not command_matches_pattern("curl http://example.com", patterns)


# ---------------------------------------------------------------------------
# Command decomposition: split_on_operators
# ---------------------------------------------------------------------------

class TestSplitOnOperators:
    def test_simple(self):
        assert split_on_operators("git status") == ["git status"]

    def test_and(self):
        assert split_on_operators("git status && git diff") == ["git status", "git diff"]

    def test_or(self):
        assert split_on_operators("git status || echo fail") == ["git status", "echo fail"]

    def test_semicolon(self):
        assert split_on_operators("git status; git diff") == ["git status", "git diff"]

    def test_pipe(self):
        assert split_on_operators("git log | head -5") == ["git log", "head -5"]

    def test_newlines(self):
        assert split_on_operators("git status\ngit diff") == ["git status", "git diff"]

    def test_mixed(self):
        result = split_on_operators("git status && git log | head -5; echo done")
        assert result == ["git status", "git log", "head -5", "echo done"]

    def test_trailing_operator(self):
        result = split_on_operators("git status && ")
        assert result == ["git status"]

    def test_preserves_subshell(self):
        # $() content should NOT be split at top level
        result = split_on_operators("echo $(git branch --show-current)")
        assert len(result) == 1
        assert "$(git branch --show-current)" in result[0]

    def test_quoted_operators_not_split(self):
        result = split_on_operators('echo "hello && world"')
        assert len(result) == 1

    def test_single_quoted_operators_not_split(self):
        result = split_on_operators("echo 'hello && world'")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Subshell extraction
# ---------------------------------------------------------------------------

class TestExtractSubshells:
    def test_dollar_paren(self):
        subs = extract_subshells("echo $(git branch --show-current)")
        assert "git branch --show-current" in subs

    def test_nested_dollar_paren(self):
        subs = extract_subshells("echo $(cat $(git rev-parse --show-toplevel)/file)")
        assert "cat $(git rev-parse --show-toplevel)/file" in subs
        assert "git rev-parse --show-toplevel" in subs

    def test_backtick(self):
        subs = extract_subshells("echo `git status`")
        assert "git status" in subs

    def test_no_subshells(self):
        subs = extract_subshells("git status --short")
        assert subs == []

    def test_multiple_subshells(self):
        subs = extract_subshells("echo $(git branch) and $(git status)")
        assert "git branch" in subs
        assert "git status" in subs


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_strip_env_vars(self):
        assert strip_env_vars("FOO=bar git status") == "git status"

    def test_strip_multiple_env_vars(self):
        assert strip_env_vars('FOO=bar BAZ="qux" git status') == "git status"

    def test_strip_redirections_output(self):
        assert strip_redirections("echo hello > file.txt") == "echo hello"

    def test_strip_redirections_append(self):
        assert strip_redirections("echo hello >> file.txt") == "echo hello"

    def test_strip_redirections_input(self):
        assert strip_redirections("sort < input.txt") == "sort"

    def test_normalize_full(self):
        assert normalize_command("  FOO=bar git status  ") == "git status"

    def test_normalize_collapse_spaces(self):
        assert normalize_command("git   status   --short") == "git status --short"

    def test_normalize_empty(self):
        assert normalize_command("") == ""
        assert normalize_command("   ") == ""


# ---------------------------------------------------------------------------
# Full decomposition
# ---------------------------------------------------------------------------

class TestDecomposeCommand:
    def test_simple(self):
        cmds = decompose_command("git status")
        assert "git status" in cmds

    def test_chain(self):
        cmds = decompose_command("git status && git diff")
        assert "git status" in cmds
        assert "git diff" in cmds

    def test_with_subshell(self):
        cmds = decompose_command("echo $(git branch --show-current)")
        assert "git branch --show-current" in cmds
        # The outer command includes the $() — it will be normalized
        assert any("echo" in c for c in cmds)

    def test_nested_subshell(self):
        cmds = decompose_command("echo $(cat $(git rev-parse --show-toplevel)/file)")
        assert "git rev-parse --show-toplevel" in cmds
        assert any("cat" in c for c in cmds)

    def test_env_var_stripped(self):
        cmds = decompose_command("FOO=bar git status")
        assert "git status" in cmds

    def test_pipe_chain(self):
        cmds = decompose_command("git log --oneline | head -5")
        assert "git log --oneline" in cmds
        assert "head -5" in cmds

    def test_complex(self):
        cmds = decompose_command("git status && git log | head -5; echo done")
        assert "git status" in cmds
        assert "git log" in cmds
        assert "head -5" in cmds
        assert "echo done" in cmds

    def test_empty(self):
        assert decompose_command("") == []
        assert decompose_command("   ") == []


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

MOCK_SETTINGS = {
    "permissions": {
        "allow": [
            "Read",
            "Bash(git status:*)",
            "Bash(git diff:*)",
            "Bash(git log:*)",
            "Bash(git branch:*)",
            "Bash(git rev-parse:*)",
            "Bash(echo:*)",
            "Bash(head:*)",
            "Bash(cat:*)",
            "Bash(ls:*)",
            "Bash(#:*)",
            "Bash(sort:*)",
        ],
        "deny": [
            "Bash(rm:*)",
            "Bash(rmdir:*)",
            "Bash(sudo:*)",
            "Bash(kill:*)",
        ],
    }
}


class TestDecide:
    def test_single_allowed(self):
        decision, reason = decide("git status", MOCK_SETTINGS)
        assert decision == "allow"

    def test_all_subcommands_allowed(self):
        decision, _ = decide("git status && git diff", MOCK_SETTINGS)
        assert decision == "allow"

    def test_one_not_in_allow(self):
        decision, _ = decide("git status && curl http://example.com", MOCK_SETTINGS)
        assert decision is None  # fall through

    def test_subshell_all_allowed(self):
        decision, _ = decide("echo $(git branch --show-current)", MOCK_SETTINGS)
        assert decision == "allow"

    def test_single_denied(self):
        decision, reason = decide("rm -rf /tmp/foo", MOCK_SETTINGS)
        assert decision == "deny"
        assert "rm" in reason

    def test_denied_in_chain(self):
        decision, reason = decide("git status && rm -rf /tmp", MOCK_SETTINGS)
        assert decision == "deny"

    def test_denied_in_subshell(self):
        decision, reason = decide("echo $(rm -rf /tmp/foo)", MOCK_SETTINGS)
        assert decision == "deny"

    def test_deny_precedence_over_allow(self):
        # rm is both matchable by deny; deny should win
        decision, _ = decide("rm foo.txt", MOCK_SETTINGS)
        assert decision == "deny"

    def test_empty_command(self):
        decision, _ = decide("", MOCK_SETTINGS)
        assert decision is None

    def test_pipe_all_allowed(self):
        decision, _ = decide("git log --oneline | head -5", MOCK_SETTINGS)
        assert decision == "allow"

    def test_comment_allowed(self):
        decision, _ = decide("# this is a comment", MOCK_SETTINGS)
        assert decision == "allow"

    def test_env_var_prefix_allowed(self):
        decision, _ = decide("FOO=bar git status", MOCK_SETTINGS)
        assert decision == "allow"

    def test_redirection_allowed(self):
        decision, _ = decide("echo hello > file.txt", MOCK_SETTINGS)
        assert decision == "allow"

    def test_complex_allowed(self):
        decision, _ = decide("git status && git log | head -5; echo done", MOCK_SETTINGS)
        assert decision == "allow"

    def test_no_settings(self):
        decision, _ = decide("git status", {})
        assert decision is None

    def test_sudo_denied(self):
        decision, _ = decide("sudo apt-get update", MOCK_SETTINGS)
        assert decision == "deny"

    def test_nested_subshell_allowed(self):
        decision, _ = decide("echo $(cat $(git rev-parse --show-toplevel)/file)", MOCK_SETTINGS)
        assert decision == "allow"

    def test_newline_separated_allowed(self):
        decision, _ = decide("git status\ngit diff", MOCK_SETTINGS)
        assert decision == "allow"

    def test_newline_with_denied(self):
        decision, _ = decide("git status\nrm -rf /tmp", MOCK_SETTINGS)
        assert decision == "deny"


# ---------------------------------------------------------------------------
# Integration: stdin → stdout via subprocess
# ---------------------------------------------------------------------------

class TestIntegration:
    @pytest.fixture
    def settings_file(self, tmp_path):
        """Create a temporary settings file mimicking real user settings."""
        settings = {
            "permissions": {
                "allow": [
                    "Read",
                    "Bash(ls:*)",
                    "Bash(git status:*)",
                    "Bash(git diff:*)",
                    "Bash(git log:*)",
                    "Bash(git branch:*)",
                    "Bash(git rev-parse:*)",
                    "Bash(echo:*)",
                    "Bash(head:*)",
                    "Bash(cat:*)",
                    "Bash(#:*)",
                ],
                "deny": [
                    "Bash(rm:*)",
                    "Bash(sudo:*)",
                ],
            }
        }
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(settings))
        return str(path)

    def _run(self, input_data, settings_file):
        """Run smart_approve.py as a subprocess."""
        script = os.path.join(os.path.dirname(__file__), "smart_approve.py")
        env = os.environ.copy()
        env["CLAUDE_SETTINGS_PATH"] = settings_file
        result = subprocess.run(
            [sys.executable, script],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_allowed_compound(self, settings_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git status && git diff"}},
            settings_file,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_denied_compound(self, settings_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git status && rm -rf /tmp"}},
            settings_file,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_output_format_allow(self, settings_file):
        """Verify the full hookSpecificOutput structure for allow decisions."""
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
            settings_file,
        )
        output = json.loads(result.stdout)
        hook = output["hookSpecificOutput"]
        assert hook["hookEventName"] == "PreToolUse"
        assert hook["permissionDecision"] == "allow"
        assert "permissionDecisionReason" in hook

    def test_output_format_deny(self, settings_file):
        """Verify the full hookSpecificOutput structure for deny decisions."""
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp"}},
            settings_file,
        )
        output = json.loads(result.stdout)
        hook = output["hookSpecificOutput"]
        assert hook["hookEventName"] == "PreToolUse"
        assert hook["permissionDecision"] == "deny"
        assert "permissionDecisionReason" in hook
        assert "rm" in hook["permissionDecisionReason"]

    def test_output_has_no_legacy_fields(self, settings_file):
        """Ensure we don't emit the old deprecated top-level decision/reason."""
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
            settings_file,
        )
        output = json.loads(result.stdout)
        assert "decision" not in output
        assert "reason" not in output

    def test_fallthrough(self, settings_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git status && curl http://example.com"}},
            settings_file,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_non_bash_tool(self, settings_file):
        result = self._run(
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/foo"}},
            settings_file,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_simple_allowed(self, settings_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
            settings_file,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_subshell_allowed(self, settings_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "echo $(git branch --show-current)"}},
            settings_file,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_pipe_allowed(self, settings_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "git log --oneline | head -5"}},
            settings_file,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_heredoc_reasonable(self, settings_file):
        """Heredoc commands should be handled reasonably."""
        cmd = "git commit -m \"$(cat <<'EOF'\nmessage\nEOF\n)\""
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            settings_file,
        )
        # Should not crash — either allow, deny, or fall through
        assert result.returncode == 0

    def test_empty_command(self, settings_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": ""}},
            settings_file,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_invalid_json_input(self, settings_file):
        """Invalid JSON input should not crash."""
        script = os.path.join(os.path.dirname(__file__), "smart_approve.py")
        env = os.environ.copy()
        env["CLAUDE_SETTINGS_PATH"] = settings_file
        result = subprocess.run(
            [sys.executable, script],
            input="not json",
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Regression tests for bug fixes
# ---------------------------------------------------------------------------

class TestBug1SubshellParenDepth:
    """Bug 1: split_on_operators double-counted $( paren depth."""

    def test_subshell_then_newline_splits(self):
        """$() followed by newline + another command must split correctly."""
        result = split_on_operators("echo $(git status)\nkubectl run foo")
        assert len(result) == 2
        assert "echo $(git status)" in result[0]
        assert "kubectl run foo" in result[1]

    def test_subshell_then_and_splits(self):
        """$() followed by && must split."""
        result = split_on_operators("VAR=$(cmd) && echo done")
        assert len(result) == 2

    def test_nested_subshell_depth(self):
        """Nested $() should track depth correctly."""
        result = split_on_operators("echo $(cat $(git rev-parse --show-toplevel)/f)\ngit status")
        assert len(result) == 2
        assert "git status" in result[1]

    def test_multiple_subshells_in_sequence(self):
        """Multiple $() in one segment should not confuse depth."""
        result = split_on_operators("echo $(a) $(b)\ngit status")
        assert len(result) == 2


class TestBug2BackslashContinuation:
    """Bug 2: backslash-newline line continuation was treated as split."""

    def test_continuation_stays_as_one_segment(self):
        result = split_on_operators("kubectl run foo \\\n  --context bar")
        assert len(result) == 1
        assert "--context bar" in result[0]

    def test_continuation_with_multiple_lines(self):
        result = split_on_operators("cmd \\\n  --flag1 \\\n  --flag2")
        assert len(result) == 1
        assert "--flag1" in result[0]
        assert "--flag2" in result[0]

    def test_real_newline_still_splits(self):
        """Bare newline (no backslash) should still split."""
        result = split_on_operators("git status\ngit diff")
        assert len(result) == 2


class TestBug3StripEnvVarsSubshell:
    """Bug 3: strip_env_vars didn't handle $(...) values."""

    def test_env_var_with_subshell_value(self):
        result = strip_env_vars("MASTER_PASS=$(aws secretsmanager get-secret) kubectl run foo")
        assert result == "kubectl run foo"

    def test_env_var_with_piped_subshell(self):
        result = strip_env_vars("PASS=$(aws s3 ls | python3 -c 'print(1)') kubectl run foo")
        assert result == "kubectl run foo"

    def test_standalone_assignment_kept(self):
        """If assignment is the whole command, keep it (nothing follows)."""
        result = strip_env_vars("MASTER_PASS=$(aws secretsmanager get-secret)")
        assert result == "MASTER_PASS=$(aws secretsmanager get-secret)"

    def test_simple_env_var_still_works(self):
        assert strip_env_vars("FOO=bar git status") == "git status"

    def test_quoted_env_var_still_works(self):
        assert strip_env_vars('FOO="hello world" git status') == "git status"

    def test_single_quoted_env_var(self):
        assert strip_env_vars("FOO='hello world' git status") == "git status"

    def test_skip_shell_value_subshell(self):
        cmd = "$(aws s3 ls | head -1) rest"
        end = _skip_shell_value(cmd, 0)
        assert cmd[end:].lstrip() == "rest"


class TestFullReproduction:
    """End-to-end reproduction of the failing complex command."""

    def test_complex_multiline_command_decomposes(self):
        """The original failing command: env var with $(), backslash continuation, newline."""
        cmd = (
            "MASTER_PASS=$(aws secretsmanager get-secret-value"
            " --secret-id master-pass"
            " | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"SecretString\"])')"
            "\n"
            "kubectl run psql-client --rm -it \\\n"
            "  --context my-cluster \\\n"
            "  --image=postgres:16"
        )
        segments = split_on_operators(cmd)
        # Should produce exactly 2 top-level segments (newline split)
        # 1. The MASTER_PASS=... assignment
        # 2. kubectl run ... (continuations collapsed)
        assert len(segments) == 2
        assert "MASTER_PASS=$(" in segments[0]
        assert "kubectl run" in segments[1]
        assert "--context my-cluster" in segments[1]
        assert "--image=postgres:16" in segments[1]

    def test_complex_command_falls_through(self):
        """Unknown commands should fall through (not crash or garble)."""
        cmd = "MASTER_PASS=$(aws s3 ls)\nkubectl run foo --rm"
        decision, _ = decide(cmd, MOCK_SETTINGS)
        # aws and kubectl are not in allow list, so should fall through
        assert decision is None


# ---------------------------------------------------------------------------
# Shell structural keywords & standalone assignments
# ---------------------------------------------------------------------------

class TestStripKeywordPrefix:
    def test_do_prefix(self):
        assert strip_keyword_prefix("do echo hello") == "echo hello"

    def test_then_prefix(self):
        assert strip_keyword_prefix("then git status") == "git status"

    def test_else_prefix(self):
        assert strip_keyword_prefix("else echo fallback") == "echo fallback"

    def test_elif_prefix(self):
        assert strip_keyword_prefix("elif [ -f foo ]") == "[ -f foo ]"

    def test_bare_keyword_unchanged(self):
        assert strip_keyword_prefix("do") == "do"
        assert strip_keyword_prefix("done") == "done"

    def test_non_keyword_unchanged(self):
        assert strip_keyword_prefix("echo hello") == "echo hello"
        assert strip_keyword_prefix("docker run") == "docker run"

    def test_for_loop_do_semicolon_pattern(self):
        """'do echo ...' from 'for x in ...; do echo ...' after split on ;"""
        cmd = 'do echo "=== $svc ==="'
        assert strip_keyword_prefix(cmd) == 'echo "=== $svc ==="'


class TestIsShellStructural:
    def test_keywords(self):
        for kw in ('do', 'done', 'then', 'else', 'elif', 'fi', 'esac', '{', '}'):
            assert is_shell_structural(kw), f"{kw} should be structural"

    def test_compound_headers(self):
        assert is_shell_structural("for app in a b c")
        assert is_shell_structural("while true")
        assert is_shell_structural("until done_flag")
        assert is_shell_structural("if [ -f foo ]")
        assert is_shell_structural("case $x in")
        assert is_shell_structural("select opt in a b c")

    def test_non_structural(self):
        assert not is_shell_structural("echo hello")
        assert not is_shell_structural("git status")
        assert not is_shell_structural("foreach")  # not a keyword
        assert not is_shell_structural("format")   # starts with 'for' but not keyword


class TestIsStandaloneAssignment:
    def test_simple_assignment(self):
        assert is_standalone_assignment("FOO=bar")

    def test_subshell_assignment(self):
        assert is_standalone_assignment("result=$(curl -s http://example.com)")

    def test_assignment_with_command(self):
        # FOO=bar git status — NOT standalone, has a command following
        assert not is_standalone_assignment("FOO=bar git status")

    def test_not_assignment(self):
        assert not is_standalone_assignment("git status")
        assert not is_standalone_assignment("echo hello")


class TestShellStructuralFiltering:
    """Verify decompose_command filters out structural keywords."""

    def test_for_loop_keywords_filtered(self):
        cmd = "for x in a b; do echo $x; done"
        subs = decompose_command(cmd)
        assert "do" not in subs
        assert "done" not in subs
        assert not any(s.startswith("for ") for s in subs)
        assert any("echo" in s for s in subs)

    def test_standalone_assignment_filtered(self):
        cmd = "result=$(curl -s http://example.com) && echo $result"
        subs = decompose_command(cmd)
        assert not any(s.startswith("result=") for s in subs)
        # But the subshell content (curl) should still be there
        assert any("curl" in s for s in subs)
        assert any("echo" in s for s in subs)

    def test_do_prefixed_command_normalized(self):
        """'do echo ...' after semicolon split should normalize to 'echo ...'."""
        cmd = 'for svc in a b; do echo "=== $svc ==="; done'
        subs = decompose_command(cmd)
        assert any(s.startswith("echo") for s in subs)
        assert not any(s.startswith("do ") for s in subs)

    def test_for_semicolon_do_approved(self):
        """for ...; do cmd; done pattern should be fully approved."""
        cmd = (
            'for svc in a b c; do echo "=== $svc ==="; '
            'kubectl get secret ${svc}-env -n staging --context ctx '
            "-o jsonpath='{.data.FOO}' 2>&1 | base64 -d 2>&1; echo; done"
        )
        settings = {
            "permissions": {
                "allow": [
                    "Bash(echo:*)", "Bash(kubectl get:*)", "Bash(base64:*)",
                ],
                "deny": [],
            }
        }
        decision, _ = decide(cmd, settings)
        assert decision == "allow"

    def test_for_loop_with_curl_approved(self):
        """The real-world for+curl command should be approved."""
        cmd = (
            "for app in svc-a svc-b; do\n"
            "  result=$(curl -sk \"https://example.com/api/$app\" 2>/dev/null "
            "| python3 -c \"import json,sys; print(json.loads(sys.stdin.read()))\" 2>/dev/null)\n"
            "  echo \"$app: $result\"\n"
            "done"
        )
        settings = {
            "permissions": {
                "allow": [
                    "Bash(curl:*)",
                    "Bash(python3:*)",
                    "Bash(echo:*)",
                ],
                "deny": [],
            }
        }
        decision, _ = decide(cmd, settings)
        assert decision == "allow"


# ---------------------------------------------------------------------------
# Heredoc stripping
# ---------------------------------------------------------------------------

class TestStripHeredocs:
    def test_simple_heredoc(self):
        cmd = "cat <<EOF\nhello world\nEOF"
        result = strip_heredocs(cmd)
        assert "hello world" not in result
        assert "cat <<EOF" in result

    def test_quoted_heredoc_delimiter(self):
        cmd = "cat <<'EOF'\nhello world\nEOF"
        result = strip_heredocs(cmd)
        assert "hello world" not in result

    def test_double_quoted_delimiter(self):
        cmd = 'cat <<"EOF"\nhello world\nEOF'
        result = strip_heredocs(cmd)
        assert "hello world" not in result

    def test_dash_heredoc(self):
        cmd = "cat <<-EOF\n\thello world\nEOF"
        result = strip_heredocs(cmd)
        assert "hello world" not in result

    def test_no_heredoc(self):
        cmd = "git status && git diff"
        assert strip_heredocs(cmd) == cmd

    def test_multiline_heredoc_body(self):
        cmd = "cat <<'EOF'\nline1\nline2\nline3\nEOF"
        result = strip_heredocs(cmd)
        assert "line1" not in result
        assert "line2" not in result
        assert "line3" not in result

    def test_command_after_heredoc(self):
        cmd = "cat <<'EOF'\nbody\nEOF\ngit status"
        result = strip_heredocs(cmd)
        assert "body" not in result
        assert "git status" in result


class TestHeredocInCommit:
    """Regression: heredoc in git commit -m should not block approval."""

    def test_commit_heredoc_decomposes_cleanly(self):
        cmd = (
            "cd /tmp && git add file.txt && "
            "git commit -m \"$(cat <<'EOF'\n"
            "fix: some commit message\n"
            "\n"
            "Detailed description here.\n"
            "EOF\n"
            ")\" 2>&1"
        )
        subs = decompose_command(cmd)
        # Heredoc body lines should NOT appear as sub-commands
        assert not any("fix: some commit message" in s for s in subs)
        assert not any("Detailed description" in s for s in subs)

    def test_commit_heredoc_approved(self):
        cmd = (
            "cd /tmp && git add file.txt && "
            "git commit -m \"$(cat <<'EOF'\n"
            "fix: some commit message\n"
            "\n"
            "Detailed description here.\n"
            "\n"
            "Co-Authored-By: test\n"
            "EOF\n"
            ")\" 2>&1"
        )
        settings = {
            "permissions": {
                "allow": [
                    "Bash(cd:*)", "Bash(git add:*)", "Bash(git commit:*)",
                    "Bash(cat:*)",
                ],
                "deny": [],
            }
        }
        decision, _ = decide(cmd, settings)
        assert decision == "allow"

    def test_real_world_commit_approved(self):
        """The exact pattern from the bug report."""
        cmd = (
            "cd /Users/yair/workspace/src/buff.game/buff.infra && git add "
            "deployments/buff.avatar-gateway/kustomize/overlays/staging/kustomization.yaml "
            "deployments/buff.avatar-service/kustomize/overlays/staging/kustomization.yaml "
            "&& git commit -m \"$(cat <<'EOF'\n"
            "fix(staging): remove invalid HPA for zero-replica avatar services\n"
            "\n"
            "K8s HPA doesn't support minReplicas: 0 or maxReplicas: 0. Remove the\n"
            "HPA entirely for avatar-gateway and avatar-service in staging since\n"
            "they're scaled to zero.\n"
            "\n"
            "Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>\n"
            "EOF\n"
            ")\" 2>&1"
        )
        settings = {
            "permissions": {
                "allow": [
                    "Bash(cd:*)", "Bash(git add:*)", "Bash(git commit:*)",
                    "Bash(cat:*)",
                ],
                "deny": [],
            }
        }
        decision, _ = decide(cmd, settings)
        assert decision == "allow"


# ---------------------------------------------------------------------------
# Project settings merge
# ---------------------------------------------------------------------------

class TestLoadMergedSettings:
    """Tests for load_merged_settings merging global + project settings."""

    @pytest.fixture
    def global_settings(self, tmp_path):
        settings = {
            "permissions": {
                "allow": ["Bash(git status:*)", "Bash(echo:*)"],
                "deny": ["Bash(rm:*)"],
            }
        }
        path = tmp_path / "global_settings.json"
        path.write_text(json.dumps(settings))
        return str(path)

    @pytest.fixture
    def project_dir(self, tmp_path):
        """Create a project dir with both .claude/settings.json and settings.local.json."""
        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)
        # Committed project settings
        shared = {
            "permissions": {
                "allow": ["Bash(terraform:*)"],
                "deny": ["Bash(kubectl delete:*)"],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(shared))
        # Local project settings (gitignored)
        local = {
            "permissions": {
                "allow": [
                    "Bash(aws secretsmanager:*)",
                    "Bash(kubectl get:*)",
                ],
                "deny": ["Bash(sudo:*)"],
            }
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(local))
        return str(tmp_path / "project")

    def test_merge_all_three_layers(self, global_settings, project_dir, monkeypatch):
        """All three settings layers should be merged."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
        settings = load_merged_settings(global_settings)
        allow = settings["permissions"]["allow"]
        # Global
        assert "Bash(git status:*)" in allow
        assert "Bash(echo:*)" in allow
        # Project shared
        assert "Bash(terraform:*)" in allow
        # Project local
        assert "Bash(aws secretsmanager:*)" in allow
        assert "Bash(kubectl get:*)" in allow

    def test_merge_deny_all_three_layers(self, global_settings, project_dir, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
        settings = load_merged_settings(global_settings)
        deny = settings["permissions"]["deny"]
        assert "Bash(rm:*)" in deny               # global
        assert "Bash(kubectl delete:*)" in deny    # project shared
        assert "Bash(sudo:*)" in deny              # project local

    def test_no_project_dir_returns_global_only(self, global_settings, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        settings = load_merged_settings(global_settings)
        assert settings["permissions"]["allow"] == ["Bash(git status:*)", "Bash(echo:*)"]
        assert settings["permissions"]["deny"] == ["Bash(rm:*)"]

    def test_project_dir_no_settings_files(self, global_settings, tmp_path, monkeypatch):
        """CLAUDE_PROJECT_DIR set but no settings files → global only."""
        empty_project = tmp_path / "empty_project"
        empty_project.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(empty_project))
        settings = load_merged_settings(global_settings)
        assert settings["permissions"]["allow"] == ["Bash(git status:*)", "Bash(echo:*)"]

    def test_only_shared_project_settings(self, global_settings, tmp_path, monkeypatch):
        """Only .claude/settings.json (no local) should still merge."""
        claude_dir = tmp_path / "proj" / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(terraform plan:*)"]}
        }))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "proj"))
        settings = load_merged_settings(global_settings)
        assert "Bash(terraform plan:*)" in settings["permissions"]["allow"]
        assert "Bash(git status:*)" in settings["permissions"]["allow"]

    def test_only_local_project_settings(self, global_settings, tmp_path, monkeypatch):
        """Only .claude/settings.local.json (no shared) should still merge."""
        claude_dir = tmp_path / "proj" / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.local.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(kubectl get:*)"]}
        }))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "proj"))
        settings = load_merged_settings(global_settings)
        assert "Bash(kubectl get:*)" in settings["permissions"]["allow"]
        assert "Bash(git status:*)" in settings["permissions"]["allow"]

    def test_deduplication_across_all_layers(self, tmp_path, monkeypatch):
        """Duplicate patterns across all three layers are deduplicated."""
        global_path = tmp_path / "global.json"
        global_path.write_text(json.dumps({
            "permissions": {"allow": ["Bash(git status:*)", "Bash(echo:*)"]}
        }))
        proj_dir = tmp_path / "proj" / ".claude"
        proj_dir.mkdir(parents=True)
        (proj_dir / "settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(git status:*)", "Bash(terraform:*)"]}
        }))
        (proj_dir / "settings.local.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(git status:*)", "Bash(kubectl get:*)"]}
        }))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "proj"))
        settings = load_merged_settings(str(global_path))
        allow = settings["permissions"]["allow"]
        assert allow.count("Bash(git status:*)") == 1
        assert "Bash(echo:*)" in allow
        assert "Bash(terraform:*)" in allow
        assert "Bash(kubectl get:*)" in allow

    def test_project_allow_approves_command(self, global_settings, project_dir, monkeypatch):
        """Command allowed by project settings (not global) should be approved."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
        settings = load_merged_settings(global_settings)
        decision, _ = decide("aws secretsmanager get-secret-value --secret-id foo", settings)
        assert decision == "allow"

    def test_shared_project_allow_approves_command(self, global_settings, project_dir, monkeypatch):
        """Command allowed by committed project settings should be approved."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
        settings = load_merged_settings(global_settings)
        decision, _ = decide("terraform plan", settings)
        assert decision == "allow"

    def test_global_only_rejects_project_command(self, global_settings, monkeypatch):
        """Without project settings, project-specific commands fall through."""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        settings = load_merged_settings(global_settings)
        decision, _ = decide("aws secretsmanager get-secret-value --secret-id foo", settings)
        assert decision is None


class TestIntegrationProjectSettings:
    """Integration tests for project settings merge via subprocess."""

    @pytest.fixture
    def global_file(self, tmp_path):
        settings = {
            "permissions": {
                "allow": ["Bash(git status:*)", "Bash(echo:*)"],
                "deny": ["Bash(rm:*)"],
            }
        }
        path = tmp_path / "global_settings.json"
        path.write_text(json.dumps(settings))
        return str(path)

    @pytest.fixture
    def project_dir(self, tmp_path):
        claude_dir = tmp_path / "project" / ".claude"
        claude_dir.mkdir(parents=True)
        settings = {
            "permissions": {
                "allow": ["Bash(aws secretsmanager:*)", "Bash(kubectl get:*)"],
                "deny": [],
            }
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(settings))
        return str(tmp_path / "project")

    def _run(self, input_data, global_file, project_dir=None):
        script = os.path.join(os.path.dirname(__file__), "smart_approve.py")
        env = os.environ.copy()
        env["CLAUDE_SETTINGS_PATH"] = global_file
        if project_dir:
            env["CLAUDE_PROJECT_DIR"] = project_dir
        else:
            env.pop("CLAUDE_PROJECT_DIR", None)
        result = subprocess.run(
            [sys.executable, script],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_project_pattern_allows(self, global_file, project_dir):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "aws secretsmanager get-secret-value --secret-id foo"}},
            global_file,
            project_dir,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_without_project_dir_falls_through(self, global_file):
        result = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "aws secretsmanager get-secret-value --secret-id foo"}},
            global_file,
            project_dir=None,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""
