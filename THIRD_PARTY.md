# Dependencies and notices

The MIT license at the repository root covers original Arcturion material. Third-party software, external integrations and model weights retain their own terms. This source edition does not bundle their distributions or model weights.

## Core YAML dependency

[PyYAML 6.0.3](https://pypi.org/project/PyYAML/6.0.3/) is pinned in requirements-core.txt. Its maintainer metadata declares the MIT license. Installing requirements downloads the dependency separately.

## Optional browser viewer

The source viewer references these fixed CDN artifacts. The exact responses were fetched and hashed without executing them; integrity attributes pin those bytes. The viewer requires network access to those CDNs and has not received a browser compatibility or complete dependency-security assessment.

| Library | Version | Declared upstream license | Upstream |
| --- | --- | --- | --- |
| force-graph | 1.52.0 | MIT | [Project](https://github.com/vasturiano/force-graph) |
| marked | 18.1.3 | MIT | [Project](https://github.com/markedjs/marked) |
| DOMPurify | 3.4.17 | MPL-2.0 OR Apache-2.0 | [Project](https://github.com/cure53/DOMPurify) |

The former DOMPurify 3.2.4 reference was replaced; it falls within the affected range of [GHSA-h8r8-wccr-v5f2](https://github.com/advisories/GHSA-h8r8-wccr-v5f2). This does not establish that the original viewer met the advisory's exploitation conditions, or that the new dependency set is free of vulnerabilities. Marked output must be sanitized before HTML insertion.

If you distribute library bytes, preserve the applicable upstream licenses and notices, including those of bundled dependencies. These CDN references are not a grant of rights to that software.

## Optional engine integrations

The preserved source includes optional vector/model, graph-ranking, native-platform, provider and external companion interfaces. Depending on the feature, these may require NumPy, NetworkX, scikit-learn, sentence-transformers, Torch, platform frameworks or separately supplied local programs. They are not part of the core proof or its installed dependency set. Review their versions, terms, permissions and behavior before enabling them.
