# Retrieval evaluation

Run `make eval-retrieval` from the repository root. It uses real temporary SQLite,
production `MemoryService.ranked_search` and `memory_context`, and no model download.
The fixture in `app/hivemind_app/retrieval_fixture.json` contains 24 memories and 12 queries:
decisions, blockers, literal paths/functions/tickets, paraphrases and distractors.
Relevance IDs are explicit human-authored labels; the evaluator never derives them
from search results. Separate topic labels supply deterministic one-hot vectors.
The `src/sync_retry.py` query deliberately has distractor-topic geometry: its
relevant source misses semantic top five and requires the literal channel to
reach hybrid top five. The report exposes this case separately, and a mutation
test verifies that removing keyword candidates fails the exact-ID gate.
These synthetic vectors exercise ranking behavior, **not production semantic quality**.

The JSON report contains macro-average Recall@5 (fraction of relevant IDs found)
and MRR@5 (reciprocal rank of the first relevant ID, zero when absent), separately
for semantic, keyword and hybrid. Context source coverage is relevant-source recall
averaged over queries at budgets 256, 800 and 2000. Budget compliance measures the
entire serialized pack using the production byte estimate; it is not a tokenizer
guarantee. Hidden deleted, superseded and quarantined conflict records are checked
in every returned search and context result. The command fails if hybrid exact-ID
Recall@5 falls below 1.0, any visibility leak occurs, or a pack exceeds its budget.

For optional real-model measurement, install the existing embeddings extra and
pre-populate the FastEmbed cache separately, then run:

```sh
PYTHONPATH=app app/.venv/bin/python -m hivemind_app.evaluate_retrieval --cached-model /path/to/existing/cache
```

This explicitly requests the existing BAAI/bge-small-en-v1.5 model with
`local_files_only=True` and offline Hugging Face flags. Missing cached files fail;
the evaluator does not fall back to downloading. Default CI never loads FastEmbed.
Real-model results on this small fixture are descriptive, not a general quality
benchmark. The same exact-ID and visibility regression gate still applies.

Search examples:

```json
{"query":"AUTH-317", "mode":"keyword", "top_k":5}
{"query":"reduce response time", "mode":"hybrid", "top_k":5}
```

Context excerpts are retrieved source text, not generated conclusions. Local sync
telemetry reports observations and never proves delivery to another node.
