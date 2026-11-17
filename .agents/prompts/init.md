# Init: learn how this repository is checked

Read the repository: `CLAUDE.md`, the `.claude/` folder, the README, manifests (`package.json`, `pyproject.toml`, `Cargo.toml`, `Package.swift`, `go.mod`, `Makefile`), and CI workflows. You do not edit anything and do not run the commands: the harness executes them right after you answer and shows the result. Ignore the `.agents/` folder; it is the harness itself, not the project.

Return shell commands that run from the repository root in a fresh checkout and exit non-zero on failure. Leave a field empty when the project has no such step; never invent a command.
- `setup`: installs dependencies in a new git worktree (for example `npm ci`).
- `build`: compiles or type-checks.
- `lint`: the project's linter in check mode, not fix mode.
- `test`: the full test suite, non-interactive, no watch mode.
- `qa_instructions`: how to start the program and exercise it from a shell (the command, the port or CLI entry point, how to stop it). Empty if it cannot be driven from a shell.
- `context`: under 40 lines for agents working in this repository. Only what they cannot see quickly in the code: where the specification or source-of-truth documents live, architecture rules and forbidden dependencies, conventions the project documents enforce, and known traps. Paths, not summaries of files.
