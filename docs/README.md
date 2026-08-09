# Design document mirror

The 20 project documents in this directory are a public, one-way mirror of the
operator's canonical Obsidian notes. They include requirements, design, and the
append-only implementation/test evidence record. The local Vault path is
intentionally not stored in this public repository.

The three supplemental scale-to-zero authorities are mirrored under the exact
relative directory `100_Ondemand Fargate/`:

- `10_scale-to-zero-goal.md`: requirements and design authority
- `30_scale-to-zero-commit-plan.md`: commit and interruption-recovery authority
- `20_scale-to-zero-completion-checklist.md`: completion authority

The synchronization tool validates the directory and these exact files,
rejects unexpected entries and symlinks recursively, and mirrors all three
without rewriting their bytes. Do not document the source's local absolute
path.

Do not edit the numbered design documents directly. Set the source path for the
current shell, then synchronize from the repository root:

```sh
export SHITTIM_DOCS_SOURCE="<path-to-public-obsidian-project-folder>"
python tools/sync_docs.py --write --source "$SHITTIM_DOCS_SOURCE"
python tools/sync_docs.py --check --source "$SHITTIM_DOCS_SOURCE"
```

The synchronization tool requires the 17 approved root Markdown filenames and
the exact nested directory with its three approved files (20 documents total).
It rejects missing or unexpected entries, symlinks, representative credentials,
Discord snowflakes, absolute home paths, and email addresses, and compares file
bytes without rewriting Markdown formatting. Production identifiers and persona
configuration belong in a separate non-mirrored operator source and versioned
SSM parameters.

The documents in this directory are not licensed under the repository's MIT
License. See [LICENSE.md](LICENSE.md).
