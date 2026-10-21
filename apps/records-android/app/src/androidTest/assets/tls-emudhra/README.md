Public certificate fixtures for the October 2026 eMudhra incident. No private keys.

- The twelve host chains come from https://emudhra.we-have-a-problem.com/chains.zip.
- `compromised-intermediates.pem` contains all sixteen certificate representations
  of the eight compromised keys from
  https://emudhra.we-have-a-problem.com/impacted-intermediates.json.
- `isolated-test-roots.pem` contains the four roots from
  https://emudhra.we-have-a-problem.com/roots.json, plus the public self-signed
  GTS Root R4 certificate used only for the legitimate control.
- `normal-chain.pem` is the public discord.com chain observed on 2026-10-21.

These roots are deliberately isolated test inputs, never app trust anchors.
Path validation is fixed at 2026-10-21T00:00:00Z so fixture expiry does not weaken
the regression. Revocation lookup is disabled only in this offline PKIX test to
reproduce a stale trust store; production TLS validation is unchanged.

The tests first prove that a stale store can build all twelve paths and then
assert that the application rejects each path before sending HTTP data. They
also rebuild a path when an intermediate comes from a local certificate store
instead of the supplied leaf chain. No network requests, forged certificates,
or interception of real services are involved.
