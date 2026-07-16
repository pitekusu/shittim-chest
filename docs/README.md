# Design document mirror

The 13 design documents in this directory are a public, one-way mirror of the
operator's canonical Obsidian notes. The local Vault path is intentionally not
stored in this public repository.

Do not edit the numbered design documents directly. Set the source path for the
current shell, then synchronize from the repository root:

```sh
export SHITTIM_DOCS_SOURCE="<path-to-public-obsidian-project-folder>"
python tools/sync_docs.py --write
python tools/sync_docs.py --check
```

The synchronization tool requires exactly the 13 approved Markdown filenames,
rejects symlinks, representative credentials, Discord snowflakes, absolute
home paths, and email addresses, and compares file bytes without rewriting
Markdown formatting. Production identifiers and persona configuration belong
in a separate non-mirrored operator source and versioned SSM parameters.

The documents in this directory are not licensed under the repository's MIT
License. See [LICENSE.md](LICENSE.md).
