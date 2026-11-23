# Contributing

## Implementation changes

Open an Issue before a large change. Keep Pull Requests focused and include:

- the behavior and motivation;
- tests and validation results;
- current official references and their verification date;
- security, AWS cost, Discord, and data-retention impact.

Implementation code, infrastructure as code, tools, and public samples are
contributed under the MIT License. Never include production identifiers,
persona configuration, credentials, user content, or local absolute paths.

Pull Request workflows are untrusted and intentionally receive no secrets or
AWS OIDC credentials. External workflow runs require maintainer approval.

## Design documents

The [design documents under `docs/`](docs/README.md) are the canonical version
maintained in this GitHub repository. Submit design corrections and proposals
through GitHub Issues. Open a documentation Pull Request only if the
maintainer explicitly requests one. See the [documentation license](docs/LICENSE.md)
for the terms.

## Commit and review expectations

Use concise imperative commit messages. All changes enter `main` through a
Pull Request, must resolve review conversations, and must pass the configured
checks. Force pushes and deletion of `main` are prohibited.

