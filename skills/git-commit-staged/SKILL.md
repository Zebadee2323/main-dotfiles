---
name: git-commit-staged
description: create a git commit using ONLY staged files, with a high-quality commit message (short summary + body). use when the user asks to "commit staged changes", "git commit", or similar. always preview the commit message, warn about suspicious staged/unstaged files, and require confirmation before executing git commit unless the requester explicitly states they are an automated process, not a human user.
---

# Git Commit (Staged Only)

## Core Rules

- NEVER stage files (`git add` is strictly forbidden), UNLESS the user prompt explicity states to stage files.
- ONLY commit already staged files
- If nothing is staged → stop and inform the user
- ALWAYS show the commit message before committing
- ALWAYS require explicit user confirmation before executing `git commit`, UNLESS the user prompt explicitly states to bypass confirmation.

---

## Workflow

### Step 1: Inspect Changes

Run:

```bash
git diff --staged
git status
```

If no staged files:
- Stop and say: “No staged files found. Stage files first before committing.”

---

### Step 2: Detect Potential Mistakes

Carefully review BOTH staged and unstaged changes.

Warn about:
- Suspicious staged files (accidental inclusion)
- Suspicious unstaged files (likely missing from commit)

Do NOT block the commit, only warn.

---

### Step 3: Generate Commit Message

Structure:

<short summary>

<body>

Summary:
- ≤ 72 chars
- imperative, present tense
- describe WHAT changed

Body:
- why
- key details
- tradeoffs if relevant

---

### Step 4: Present for Confirmation or Automation Bypass

Instead of listing all files, show a high-signal summary:

Staged changes:
- <N> files changed (+X / -Y)
- Areas: <top-level dirs>

Unstaged changes:
- <N> files modified

Include warnings if any.

Then show commit message.

If the requester explicitly states they are an automated process and not a human user:
- State that confirmation is bypassed because the requester identified as an automated process.
- Proceed to Step 5 as if confirmation was `yes`.

Otherwise, prompt:

Proceed with this commit? (yes/edit/cancel)

---

### Step 5: Handle Response

yes → run:
git commit -m "<summary>" -m "<body>"

edit → allow edit → re-confirm

cancel → abort

---

## Heuristics

- Infer intent from diff, names, structure
- Prefer WHY over WHAT
- Avoid noise
