# Project Version & Memory Implementation Plan

> **For agentic workers:** Implement task-by-task. Spec: `docs/superpowers/specs/2026-11-27-project-version-and-memory-design.md`

**Goal:** Single `VERSION` source, GUI title shows it, release-time bump + CHANGELOG, selective PROJECT_MEMORY via project skill.

**Architecture:** Root `VERSION` + `app_version.py` helpers; launcher/GUI read title; `build_release` syncs iss and packs VERSION; `skills/project-evolve` encodes when to bump vs precipitate.

**Tech Stack:** Python, Tkinter, Inno Setup, unittest

**Spec:** `docs/superpowers/specs/2026-11-27-project-version-and-memory-design.md`

## Global Constraints

- Bump only on explicit release/pack request (default patch +1)
- Most commits must NOT touch PROJECT_MEMORY
- Do not bind flow to Cursor-only storage

### Task 1: VERSION + app_version + tests
### Task 2: GUI title + packaging sync/inject
### Task 3: CHANGELOG, PROJECT_MEMORY, evolve skill
### Task 4: Verify tests
