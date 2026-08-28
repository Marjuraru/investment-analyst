# Claude Code Context - investment-analyst

You are operating inside the investment-analyst repository.

This file only provides Claude Code integration context.
It does not override repository governance, workflows, roles, skills, or policies.

## Source of truth

The authoritative project instructions are:

- AGENTS.md
- docs/development_protocol.md
- .agents/skills/

Before performing any project task, load and follow the relevant instructions from these sources.

## Role execution

The project defines role behavior through skills.

Available role skills:

- PLAN: .agents/skills/plan/
- BUILD: .agents/skills/build/
- AUDIT: .agents/skills/audit/
- UI: .agents/skills/ui/

Do not recreate, duplicate, or override role definitions here.

## Repository awareness

Before any action that depends on project state, verify live information:

- current git branch
- working tree status
- active Work Blocks
- GitHub Issues
- Pull Requests
- CI status

Do not rely on historical context when live state is available.

## Financial domain invariants

Always preserve project invariants defined by the repository, including:

- Point-In-Time correctness
- available_at semantics
- Decimal exactness
- append-only evidence
- provenance and traceability
- missing != zero

Maintain the separation between:

evidence
→ analytics
→ signal
→ recommendation
→ decision
→ execution

## Safety boundary

Default mode:

Read, analyze, and review.

Do not modify files, Git state, GitHub state, runtime, dependencies, or project governance unless explicitly instructed by the applicable role skill and authorized project workflow.

## Communication

When analyzing repository state, distinguish clearly:

- Facts: verified evidence
- Hypotheses: possible explanations
- Recommendations: proposed actions
