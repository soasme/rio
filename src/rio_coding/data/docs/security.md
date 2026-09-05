# Project trust and security

rio resolves trust for the canonical project working directory before reading ambient project Markdown/JSON or importing project extensions. Protected inputs include project skills, prompts, themes, system-prompt files, `AGENTS.md` context, and extension candidates (`rio_coding.project_trust.ProtectedResourceDetector`).

Interactive users can save exact or displayed-parent decisions, or choose a run-only result. Headless `ask`/`never` defaults decline project inputs; explicit approve/decline overrides are run-only. Decisions are versioned, locked, and atomically persisted to `~/.rio/trust.json` (`rio_coding.project_trust.ProjectTrustStore`) so a crash mid-write can never resurrect or silently revoke trust -- a write that is interrupted leaves the store fail-closed until it is explicitly recovered.

## Credentials

API keys and OAuth tokens are collected through secret fields and kept in rio's private credential store (`rio_coding.credentials`), referenced by an opaque provider name rather than embedded in provider configuration, sessions, or exports.

## General boundary

Project trust is an input-loading guard, not a filesystem, process, shell, network, tool, credential, provider, model, package-install, prompt-injection, or exfiltration sandbox. Extensions execute arbitrary Python. Use an OS sandbox, container, VM, remote environment, and restricted credentials/network when isolation is required.
