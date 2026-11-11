# Bundled OLCR operation knowledge

The canonical JA/EN guide lives in `backend/olcr_api/help_knowledge/`.
`topics.json` maps UI vocabulary to reviewed Markdown topics. The generated
`graphify-out/graph.json` contains document nodes and explicit navigation links,
source hashes, and an honest `reviewed_markdown_topics` extraction provenance.
It is not an inferred code graph.

`operation_help` retrieves sources using the existing `GraphifyRAG.search` path,
verifies the graph and source hashes, then returns the reviewed procedure itself.
No workspace, model call, embedding download, API key, or external access is
needed. The relocatable provenance marker is accepted only at the exact bundled
help directory; a project graph cannot impersonate bundled OLCR documentation.
The guide files and graph are included in Python package data.

Chat and streaming chat recognize operation questions before action routing,
custom-agent execution, and coding orchestration. Commands and implementation
requests continue through their normal owners. Answers, source provenance and
retrieval tasks are persisted to conversation history. Current account state,
permission grants, and successful actions must not be inferred from the guide.
Questions without a matching documented topic receive the overview and topic
menu; the guide does not invent undocumented operations.

When a UI flow changes, update both language files and topic aliases. Regenerate:

```
python3 scripts/build_operation_help.py
```

Run `test_operation_help.py` and isolated `test_operation_help_chat.py` before
shipping. They cover every topic's source integrity and language, ownership,
projectless use with models/network unavailable, persistence and stream parity.

## Question coverage and matching

The guide now has 53 topics (106 bilingual documents). In addition to the broad
feature guides, 29 procedure and troubleshooting articles separate conditions
such as permission denial, saved credentials, thinking logs, fallback ordering,
context limits and failed playback. `topics.json` records 212 anticipated JA/EN
questions and the implementation files reviewed for each topic.

Matching uses normalized UI terms plus conjunctions of feature and circumstance.
A matched procedure supersedes its broad parent, while separate features in one
question retain separate articles. English plural forms and command names quoted
in explanatory questions are supported. Imperative implementation clauses retain
their coding owner. Graph edges explicitly record parent/procedure relationships
from the topic index; this is curated metadata, not inferred implementation facts.

The question corpus is executed as regression coverage, not just examples in a
prompt. It checks language, primary article selection and verified provenance.
See `operation-help-coverage.md` for the reviewable question/source inventory.
