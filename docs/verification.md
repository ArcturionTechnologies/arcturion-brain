# Synthetic verification

The source edition was checked on Python 3.11 and 3.14 with PyYAML 6.0.3 and SQLite/FTS support. The reproducible entry point is `guarded_proof.py`; the repository's source-check workflow runs the dependency-backed proof on both versions.

## What the proof exercises

| Area | Observed check |
| --- | --- |
| Configuration | Missing or malformed deployment policy is refused. |
| Ontology | The ordinary default API loads the shipped vocabulary; a declared transitive relationship is recognized. Relative or missing resource overrides are refused. |
| Ingestion | Two invented Markdown sources are indexed with their YAML titles. |
| Directed relationships | An explicit workflow → `cites` → decision edge is stored and returned with the expected direction. |
| Retrieval | Both intended IDs and source-path citations appear in the direct result and its relationship text. |
| HTTP and MCP | The actual in-process request dispatch returns those same invented IDs, citations and relationship. |
| Source update | Re-ingestion changes the indexed content hash for a revised invented document. |
| Missing source | Missing-path cleanup removes the deleted invented document from the index. |
| Restricted paths | Both configured protected roots are checked through ordinary writes and direct SQL insert/update triggers, using absolute and supported relative paths. Underscore and percent characters are literal; similarly named allowed neighbors preserve their contents. |
| Optional answer judging | A qualifying invented journal query makes no queue submission by default. Explicitly enabled behavior reaches an in-process recorder; no provider executable is invoked. |

Separate focused checks used invented SQLite data for the optional corpus adapter. A configured source-bound pin appeared in the actual scoped query and coverage result. Changed rules suppressed it before reindexing; explicit empty rules withdrew it during synchronization. Changed sources invalidated their pins. Missing or malformed configuration and changed prepared-rule inputs were refused. These additional probes did not exercise a real desk or its private corpus.

The ontology's parsed data was also compared with the captured source reference: its vocabulary and axioms were preserved while private observational comments were removed.

## How the execution is bounded

The proof creates a fresh `.proof-*` directory and invented home, policy, documents and index. It starts from a clean environment and installs a Python audit guard before importing the engine. Audited out-of-scope file operations, networking and process launches are rejected; a recorded denial fails the run.

This is a check of observed operations in inspected, trusted Python code. Native code, filesystem metadata operations, directory-relative paths and inherited descriptors are outside that guard's guarantees. It is not operating-system confinement.

The directed edge is inserted explicitly. HTTP and MCP checks exercise request dispatch in process, rather than a listening server or a launched stdio client. Embeddings are off. No private database, corpus, account, real model or provider was used.

## Source coverage and remaining limits

The source reference contains 83 engine Python modules, 67 Python test-source files and five selected entry/schema/viewer files. [Reconciliation](../source-reconciliation.json) records each reference path and its original and adapted hashes. It establishes a point-in-time comparison of on-disk source, rather than the code loaded by an already-running service.

The inherited test files retain useful regression examples. Several depend on additional host programs, companion packages or fixtures; the complete legacy suite was not run and is not the source-check workflow.

Real model compatibility and retrieval quality, native-platform integrations, provider operation, full watch/reindex workflows and the live viewer have not been qualified by this proof. Deployment-specific rules and source material must be supplied separately. Review [configuration and safety](../SAFETY.md) and [third-party dependencies](../THIRD_PARTY.md) before enabling those paths.

These checks provide reproducible evidence for the tested behavior. They are not an authenticated multi-user isolation assessment or a formal security audit.
