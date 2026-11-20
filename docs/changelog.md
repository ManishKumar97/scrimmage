# Changelog

Scrimmage has no published release yet.

## Unreleased

- The control channel can run over TLS for a service in a container: `control`, `control_tls` and `scrimmage cert`.
- An SDK older than this release gets `401 bad_signature` from the control server on a blob over 4 MB, because the server now checks the signature before it reads a large body.
- A service test compares the version of `scrimmage` in the service with the runner's.
  Only the same version passes, and any other version, even another patch or dev release, is refused with exit 2 before any run.
  Each claim of a run's plan carries the SDK's version, so a process added later is checked too.
- The service pings the control server before it answers the probe, and the test stops with exit 3 if the ping fails or another runner answers.
  The startup report is version 2, and `scrimmage test --json` gains `control_ping`.
- The control server refuses a large body whose signed header is more than 60 seconds old, before it reads the body.
- A service run's summary names the process that claimed it, `claimed_by`, and carries `plan_digest` and `claimed_plan_digest`, keyed digests of the plan the runner made and the plan the claiming process used.
  A run served under another plan is an infrastructure error.
  The record schema gains these fields and the notice `plan_changed`.
- The service's ping to the control server reports `error`, the class of its failure, on `http://` and `https://` alike.
  The runner asks again for up to 30 seconds when the ping times out, is refused or cannot resolve the name, and a failure after that names its fix: a NetworkPolicy, a Service selector or the host in `SCRIMMAGE_CONTROL_URL`.
  The startup report's `control` object gains `error`, an optional field.

Releases will be listed on this page, newest first, each with its version, its date and what changed.
