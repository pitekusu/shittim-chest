# Public certificate-chain fixtures

Captured on 2026-10-21. These files contain public certificates, never private
keys. Tests validate at 2026-10-21 12:00 UTC so leaf expiry does not change the
incident trust-boundary regression.

- `emudhra/`: all 12 chains from
  <https://emudhra.we-have-a-problem.com/chains.zip>, SHA-256
  `8a2edc81b6c51e3ad27d45ca1e441ec84c961e2dee0fc547f2a02c0f74315f95`.
  This covers DV/OV/EV G3B/G3C and the AAA cross-sign paths.
- `valid/`: ordinary TLS handshakes to `api.openai.com`, `discord.com`, and
  `www.python.org`; no HTTP or authentication was sent.
- `roots/emsign-g3.pem`: public compromised-chain trust anchor from the deployed
  image, SHA-256
  `86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b`.

Tests do not contact these hosts, derive compromised keys, or run a TLS server
using the compromised certificates.
