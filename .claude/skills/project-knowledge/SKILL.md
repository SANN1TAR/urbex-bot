---
name: project-knowledge
description: |
  Use when you need information about this project's architecture, tech stack,
  coding patterns, data model, deployment setup, git workflow, or UX guidelines.
  Contains comprehensive project documentation including design decisions,
  technical specifications, and development standards.
---

# Project Knowledge

This skill provides access to project documentation that defines how this project works, how code should be written, and how features should be developed.

## When to use

Activate this skill when you need to:
- Understand project architecture, tech stack, and data model
- Learn coding patterns, git workflow, and testing approach
- Check deployment setup, monitoring, and operational procedures
- Apply UX guidelines and design system
- Make technical decisions aligned with project standards

## Core references

All documentation is in the `references/` folder:

- **[project.md](references/project.md)** - Project overview, purpose, target audience, core features, scope boundaries
- **[architecture.md](references/architecture.md)** - Tech stack, project structure, dependencies, external integrations, data flow, data model (schema, migrations, sensitive data)
- **[patterns.md](references/patterns.md)** - Project-specific coding conventions, git workflow (branching, testing, security gates), business rules
- **[deployment.md](references/deployment.md)** - Deployment platform, environment variables, CI/CD triggers, rollback, monitoring & observability
- **[ux-guidelines.md](references/ux-guidelines.md)** - Bot tone of voice, message style, emoji usage, user-facing text patterns

## How to use

Read specific guides as needed for your task:

- Starting feature development → read project.md, architecture.md, patterns.md
- Implementing database changes → read architecture.md (Data Model section)
- Working on bot messages/UX → read ux-guidelines.md
- Setting up deployment → read deployment.md
- Creating branches or PRs → read patterns.md (Git Workflow section)
- Investigating errors → read deployment.md (Monitoring section)
- Working with search/scraping logic → read architecture.md (External Integrations section)
