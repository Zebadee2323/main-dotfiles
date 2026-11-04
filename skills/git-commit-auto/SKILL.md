---
name: git-commit-auto
description: create a git commit automatically, staging relevant files and committing without asking for confirmation. use when the user asks to "auto commit", "commit everything", "commit without asking", or invokes git-commit-auto. follows the git-commit-staged skill workflow but with staging and the confirmation step explicitly permitted to be skipped.
---

# Git Commit (Auto)

Follow the `git-commit-staged` skill in this repository for the full workflow (inspecting changes, detecting mistakes, writing the commit message, and committing).

## Explicit Permissions

Invoking this skill is the user's explicit instruction to override the following `git-commit-staged` rules:

- **You are allowed to stage files.** Use `git add` to stage the changes that belong in the commit. Do not stop because nothing is staged.
- **You are allowed to commit without user confirmation.** Show the change summary, any warnings, and the commit message, then run `git commit` immediately. Do not prompt for `yes/edit/cancel`.

All other rules and steps of `git-commit-staged` still apply.
