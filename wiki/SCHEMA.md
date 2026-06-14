# Wiki Schema

## Domain
NIM Hedge Gateway — local LLM hedging gateway for improving reliability and tail-latency of NVIDIA NIM models and other OpenAI-compatible providers.

## Conventions
- File names: lowercase, hyphens, no spaces (e.g., `nim-hedging-strategy.md`)
- Every wiki page starts with YAML frontmatter
- Use `[[wikilinks]]` to link between pages (minimum 2 outbound links per page)
- When updating a page, always bump the `updated` date
- Every new page must be added to `index.md` under the correct section
- Every action must be appended to `log.md`

## Frontmatter
```yaml
---
title: Page Title
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: entity | concept | query
tags: [from taxonomy below]
sources: [path/to/source]
confidence: high | medium | low
---
```

## Tag Taxonomy
- Components: hedger, health, providers, validators, config, logging
- Features: hedging, fallback, cooldown, ranking, orchestration
- Models: large, medium, small, vision
- Meta: architecture, configuration, troubleshooting, pitfall

## Page Thresholds
- **Create a page** when a component/feature/pitfall is encountered in 2+ sessions or is critical
- **Add to existing page** when new info extends an existing topic
- **Split a page** when it exceeds ~200 lines

## Entity Pages
One page per notable entity. Include:
- Overview / what it is
- Key facts and configuration
- Relationships to other entities ([[wikilinks]])
- Source references

## Concept Pages
One page per concept or topic. Include:
- Definition / explanation
- Current state of knowledge
- Open questions or debates
- Related concepts ([[wikilinks]])

## Query Pages
How-to guides. Include:
- Step-by-step instructions
- Code examples
- Common pitfalls
- Related concepts ([[wikilinks]])
