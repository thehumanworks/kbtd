# Tasks

## Backlog

## In Progress

## Done

- [x] Allow a task or subtask to have a description and a due date.
  - [x] New description command added to cli
  - [x] New description command visible in the TUI and updateable there
- [x] User can define watchers on the board - such that if a "trigger" activates the user shell command (watcher Action) is executed in the background.
  - [x] Implement watcher action workflow engine
  - [x] SPIKE: decide (ADR and showcase) the workflow for the watcher. Agree on trigger kinds for MVP while remaining generic (watcher actions are shell commands) - owner assignment? Should we add "labels"?
  - [x] SPIKE: decide which tool to use for reliable running of the watcher actions. Low infra and simple - DBOS w/sqlite, Vercel Workflow SDK...
- [x] Enable moving cards and subcards up and down the board to showcase priority changes.
- [x] Allow for theme customization @Codex
  - [x] Include a Vercel light and dark mode themes. Default theme to system. @Codex
- [x] a watch trigger can be a composition of events as a condition (eg owner.changed /\ task.status = "Backlog" @codex
  - [x] Update triggers to allow for conditions that check against event value (eg - owner.changed = "codex")
  - [x] Update triggers to allow for composition of events using a TSA+ like syntax
