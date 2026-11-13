# Changelog

Scrimmage has no published release yet.

## Unreleased

- The control channel can run over TLS for a service in a container: `control`, `control_tls` and `scrimmage cert`.
- An SDK older than this release gets `401 bad_signature` from the control server on a blob over 4 MB, because the server now checks the signature before it reads a large body.
  The SDK and the runner must be the same release until a version check lands.

Releases will be listed on this page, newest first, each with its version, its date and what changed.
