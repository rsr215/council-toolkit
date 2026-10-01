---
name: council
description: Run or manage the user's reusable council for coordinator conversation, implementation, independent review, repair, research briefs, and owner decisions across projects. Use when the user asks to run the council or manage its roles or tasks; do not impose it on ordinary project work.
---

Use the toolkit's durable workflow instead of manually relaying prompts. Resolve this skill's real path; the toolkit root is two directories above its folder. Its executable is `bin/council`. Read the toolkit README for setup, executor contracts, or remote integration details as needed.

## Start work

- Inspect `council status --json` for existing work before creating a duplicate task.
- Read `council context <project>` when resuming migrated work. Retrieve an archived source with `council context <project> --source <relative-path>` only when relevant. Imported prompts are historical evidence, not launch authorization; reconcile live branches/PRs before choosing the next task.
- Use the registered project's configuration. For a new project, `council init <workspace> --name <name>` creates its configuration outside the project. Fill in actual rules, checks, and owner decision boundaries from the user's instructions and authoritative project documents. Do not invent an approved objective from a backlog.
- For planning or questions, use `council chat <project> '<message>'`. Its recent turns, tiered summaries and working memory persist in the ledger; `council chat <project> --history` is read-only, and `--history --all` retrieves compressed older full turns. When the roster sets `chat_session = "native"`, the coordinator resumes one Claude session per thread while it still matches the ledger. Pass `--new-session` only when the owner asks for a fresh coordinator session. Workflow phases and reviewers always start fresh. If chat reports a context backlog, use `council compact <project> --dry-run` then `council compact <project> --max-steps N`; do not ask the coordinator to answer from an incomplete history. Ask the owner directly when a required decision is unresolved; the model's memory is not authorization.
- For independent review of existing code or plans, use `council review <project> '<scope>'`. The coordinator conversation can dispatch this when the owner explicitly requests it; pass `--allow-review` on that `council chat` turn. Do not pass the flag for ordinary discussion. This read-only workflow does not need implementer output or project checks. Read its task report and relay findings.
- Project Markdown files are optional. Git projects snapshot tracked and unignored files; non-Git projects need explicit `include` patterns in their project TOML. Do not treat old handoff prompts as instructions to launch work.
- The delivery workflow writes files. Reuse a suitable free managed worktree or create one with the app's worktree tools when available, and pass its returned path using `--workspace`. Account for existing changes before reusing a checkout. Non-Git projects can use an explicitly authorized working directory via project `allow_in_place`.
- Run `council doctor <project> --auth` before the first live run. A sandbox can prevent CLI credential access even if the user is signed in; distinguish that from an actual logout. Resolve access through the host's approval mechanism, never by changing the CLI adapters to bypass their restrictions.
- Start only the approved scope: `council run <project> '<objective>' --workspace <path>`. This runs the configured agents and checks, then stops at an owner decision, completion, failure, or budget limit. The runner controls phase sequencing; do not spawn duplicate agents.
- If a terminal call yields, follow its execution until it exits, then inspect status. For authorized unattended work, enable the existing Council heartbeat through the host scheduling tool just before dispatching the worker. Inspect its saved settings first; preserve its identity and prompt. Report results directly while supervising. When the worker exits, report any decision/failure/completion and pause the heartbeat if no tasks are ready/running; pending owner decisions do not need polling. Re-enable on the next authorized unattended run/resume. The monitor must also self-pause when no tasks are ready/running. Never create a duplicate watcher or leave an idle recurring model turn. CLI-only use has no scheduler integration; do not claim otherwise.

## Decisions and updates

Present a pending gate's question, evidence, options and recommendation in ordinary language. Use `council report <id>` for the full review/disposition; the compact summary is not the complete evidence. Bind the user's answer to the current request ID using `council decide <id> <choice> --request <request-id> --note '<actual user decision>' --continue` when continuation is authorized. Never derive an answer from silence or approve a gate because an agent recommends it.

An interrupted run may have changed files. Inspect the workspace and any surviving process before `council resume <id> --note '<reconciliation>'`, then `council work <id>`. A changed workspace invalidates old review evidence. Do not reset budgets to make a failing loop continue indefinitely.

Summarize outcome, verification, autonomous decisions, unresolved limitations, and the next owner action. Council completion is separate from merging, publishing, deployment, purchases, or sending messages.

## Roles and cleanup

Change assignments with `council assign <role> --executor <configured-executor> --model <model-id> --effort <supported-level>`. Add `--project <project>` to edit only the roster that project's TOML declares with `roster = "..."`; `council doctor <project>` shows which roster applies. New runs use the new assignment; existing runs retain theirs. New providers need a supported runtime or a command adapter conforming to the README's JSON contract. Do not silently substitute models when one is unavailable.

The runner automatically compacts expired closed reports. `council gc` previews additional cleanup. It never deletes project outputs, unresolved work, the old `.council` mailbox, or worktrees. Retire managed worktrees through the app's archive tool after accounting for their work.

Remote clients are interfaces to the same ledger. Do not claim phone notifications, a Telegram bot, or OpenClaw is connected until that integration has actually been configured and tested.
