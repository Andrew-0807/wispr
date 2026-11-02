# Hindsight Memory Rules for Claude Code

This project uses Hindsight as a persistent memory backend via MCP tools.
Follow these rules in every session.

## Session Protocol

### Session Start
Before doing any work, call `hindsight_recall` with the current task topic:
```
hindsight_recall("current task topic or feature name")
```
This loads relevant context from previous sessions — do NOT re-explain the
full project from scratch; query Hindsight first and build on what's there.

### Task Complete
After finishing any meaningful task, call `hindsight_retain` with a summary
of what was done and why:
```
hindsight_retain("Implemented X using Y because Z. Key constraint: ...")
```
Include: what was done, the approach chosen, and the reason/constraint behind it.

### When Stuck or Repeating a Mistake
Before trying a new approach, call `hindsight_reflect`:
```
hindsight_reflect("problem description or recurring mistake")
```
This surfaces past decisions and lessons that may resolve the issue.

## What to Retain

Retain after:
- Completing a feature or fixing a bug
- Making an architectural or API decision
- Discovering a hard-to-debug constraint (hardware, OS, library quirk)
- Changing a tool, model, or config that affects the project

Do NOT retain:
- Trivial code changes with no lasting context value
- Things already documented in code comments or README
- Temporary debugging steps

## Memory Bank

All memories are stored in bank: `wispr`

## LLM Backend

Provider hierarchy: **OpenRouter** (nvidia/nemotron free) → auto-fallback.
Configured at `E:\Programming\Hindsight\.env`.
