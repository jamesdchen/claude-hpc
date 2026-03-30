Sync the current repo with its remote. Run these steps in order:

## 1. Fetch
`git fetch --all --prune`

## 2. Check local state
`git status` — identify uncommitted changes (staged, unstaged, untracked).

## 3. Selective commit before sync
If there are uncommitted changes:
- Run `git diff` and `git diff --cached` to inspect what changed.
- **Only commit changes that are related to the current task or branch purpose.** Judge relevance by the branch name, recent commit messages (`git log --oneline -5`), and the nature of the changes.
- If there are **related changes**: stage only those files and commit with a descriptive message.
- If there are **unrelated changes** (e.g. scratch files, unrelated experiments, debug prints, editor artifacts): leave them unstaged. Briefly note what was skipped and why.
- If ALL changes are unrelated, stash everything with `git stash push -m "auto-stash before sync: unrelated changes"`.
- If there's a MIX: commit the related files, then stash the rest.

## 4. Pull with rebase
`git pull --rebase origin $(git branch --show-current)`

### Handle merge conflicts
If the rebase produces conflicts:
1. Run `git diff --name-only --diff-filter=U` to list conflicted files.
2. For each conflicted file, read it and resolve the conflict:
   - Prefer **keeping both** changes when they touch different logic.
   - Prefer **ours** (local) when local changes are clearly more recent or intentional.
   - Prefer **theirs** (remote) for upstream formatting, CI config, or boilerplate.
   - If the conflict is ambiguous, **stop and ask the user** before resolving.
3. After resolving, `git add` the resolved files and `git rebase --continue`.
4. If the conflict is too complex (>3 files or intertwined logic), abort with `git rebase --abort`, restore the stash if applicable, and report the situation.

## 5. Restore stashed changes
If changes were stashed in step 3:
- `git stash pop`
- If the pop conflicts, resolve the same way as step 4 (prefer local working changes).

## 6. Push
`git push origin $(git branch --show-current)`

## 7. Summary
Show a final `git log --oneline -5` and note:
- What was committed
- What was left uncommitted/stashed
- Whether any conflicts were resolved

If any step fails unexpectedly, stop and report — do not force-push or discard changes.
