# .agents

Paste a task, come back to a pull request that has already been built, tested, reviewed and (optionally) run.

One Python file (`run.py`, standard library only) owns git, the checks and GitHub. Claude sessions only plan, write code, review and QA, each in a fresh context with the least access it needs. You merge.

Needs: `git`, `gh` (logged in), `claude`, Python 3.9+, and a GitHub `origin`.

## Use it

```bash
cp -R path/to/agent-loop-boiler/.agents your-project/      # install
cd your-project
python3 .agents/run.py init          # once: finds your setup/build/lint/test commands, runs them, writes config.json + context.md
git add .agents && git commit -m "add agent loop"

python3 .agents/run.py task "Fix the login redirect loop when the session cookie is expired"
python3 .agents/run.py task notes/task.md       # or a file
python3 .agents/run.py queue                    # split .agents/OBJECTIVES.md into tasks, one PR each
python3 .agents/run.py status                   # what happened while you were away
```

`init` reads `CLAUDE.md`, `.claude/`, the README, manifests and CI to fill in the config. Read what it wrote: every gate below depends on the test command being right.

To stop a run, create the file `.agents/STOP`.

If you run out of Claude usage mid-run, it waits and retries the interrupted session every 15 minutes until usage returns. The machine has to stay awake: on a Mac, start long runs with `caffeinate -i python3 .agents/run.py queue`.

## What happens to a task

1. A new worktree and branch `agent/<slug>` from `origin/<base>`, in `../<repo>.worktrees/`. Your checkout is never touched.
2. The checks run on the untouched base. A red base stops the task: there is no way to tell a regression from an old failure.
3. **Planner** (read-only) turns your words into a brief: goal, exact file list, acceptance criteria, assumptions, out-of-scope list.
4. **Worker** implements the brief. It cannot commit, push, use `gh`, or edit protected paths.
5. **Gates** (plain code, no model) run on the result. See the table.
6. **Reviewer** (read-only, fresh context, sees only the brief and the diff) looks for unmet criteria, broken callers, unrequested code and weakened tests.
7. **QA** (optional, no edit tools) starts the program and tries the change as a user would. Runs only if `qa.instructions` is set.
8. Any failure in 5 to 7 goes back to the same worker session with the exact reason. Three rounds at most.
9. Everything green: the work is squashed to one commit, pushed, and a PR is opened with the evidence in its body.

A task that cannot pass is **blocked**: nothing is pushed, the worktree and branch stay on disk for you to inspect, and `status` shows the reason. Logs of every session are in `.agents/state/<slug>/`.

## The gates

| Gate | What it catches |
|---|---|
| Diff stays inside the brief's file list | Drive-by refactors, surprise dependencies, edits to CI or agent config |
| Added non-test lines under `max_added_lines` (400) | Bloat and unrequested features |
| A test file must be added or changed | Untested changes |
| No existing test file deleted; passing-test count must not drop | Tests removed or skipped to get green |
| Build, lint and the full suite pass | Regressions in code the task did not mean to touch |
| Source changes reverted, suite re-run, must fail | Tests that pass without the change and so prove nothing |
| Reviewer has zero `important` findings | Broken callers, unmet criteria, weakened tests, leftover unrequested code |
| QA verdict is not `fail` | Bugs that only show when the program runs |

The pass/fail results in the PR body come from the harness running the commands on the final commit, not from what an agent says it did.

## Queue

`queue` splits `OBJECTIVES.md` (or the file you name) into tasks and works through them in order. A task that depends on another is built on that task's branch and its PR targets that branch, so merge them in order. If a task is blocked, the tasks that depend on it are skipped and the rest continue. Run `queue` again to retry blocked and skipped tasks; finished ones are not redone. Editing the objectives file makes it plan again from scratch.

## config.json

Only the keys you set override the defaults at the top of `run.py`.

| Key | Default | Meaning |
|---|---|---|
| `base` | `main` | Branch PRs target |
| `checks.setup` / `build` / `lint` / `test` | empty | Shell commands, run from a fresh worktree. `test` is required |
| `copy` | `[]` | Gitignored files a fresh worktree needs, such as `.env.test` |
| `protected` | `.agents/ .github/ .claude/ CLAUDE.md .env*` | Paths no agent may change |
| `max_added_lines` | 400 | Budget for non-test lines |
| `max_rounds` | 3 | Fix rounds before a task is blocked |
| `prove_tests` | true | Run the revert-and-fail check (costs one extra test run) |
| `models` | planner and reviewer `opus`, worker and qa `sonnet` | Per role |
| `budget_usd` | call 5, task 20 | Spend caps |
| `limit_wait` | 900 | Seconds between retries while your Claude usage is exhausted |
| `worker_allow` | `[]` | Extra permission rules, such as `"Bash(npm test *)"` |
| `qa.instructions` | empty | How to start and exercise the program. Empty turns QA off |
| `qa.allow` | `[]` | Commands QA may run, such as `"Bash(npm run dev*)"`, `"Bash(curl *localhost*)"` |

`context.md` is appended to every agent's instructions. Keep it short: where the spec lives, rules that are not visible in the code, known traps.

## Limits

- The test-count gate reads counts from the output of common runners (pytest, jest, cargo, XCTest, unittest). When it finds none it is skipped; the other gates still apply.
- The revert-and-fail check passes if the suite fails for any reason, including a compile error. It rules out tests that prove nothing; it does not prove the tests are thorough.
- The worker's shell is an allow-list, not a sandbox, and test commands run project code. Do not point this at a repository you do not trust.
- Protect `main` on GitHub. Nothing here merges, but branch protection is the backstop that does not depend on this script.
- Tasks run one at a time. A queue task can depend on only one other task.
- QA needs a program that can be driven from a shell. Browser QA needs a browser tool added to `qa.allow` by you.

## Changing the harness

`python3 .agents/test_run.py` runs the whole pipeline against real local git with a fake `claude` and `gh`: no network, no spend. Run it after any edit to `run.py`.
