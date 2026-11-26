# Limits: judging and records

Limits of judging an answer, and of what a record keeps and removes.

## Judging an answer

- **Rules are plain text.** `expect` and `forbid` find text in the answer, with no word boundaries, numbers or meaning.
  "shipped" is found in "has not shipped", and Markdown stays in the answer.
  See [Judging an answer](../judging.md).

- **A look-alike letter is a different letter.** A Cyrillic "е" is not a Latin "e", so `forbid = ["secret"]` does not find "sеcret".
  Scrimmage drops characters that do not show, but it does not fold letters that only look alike.

- **A visible space or mark inside a word hides it.** A non-breaking space is read as a plain space, and a combining accent stays on its letter, so `forbid = ["secret"]` does not find "sec ret" or "secrét".
  What is dropped is the format and control characters of Unicode 16.0 and the default-ignorable ones of Unicode 15.1, which includes a few drawn marks such as the Arabic number sign U+0600.
  That makes `expect` more lenient and `forbid` stricter, never the other way.
  The unseen separators `\x1c` to `\x1f` and `\x85` also split a word, because they count as spaces.

- **No model judges an answer, and there is no built-in list for "admits the failure".** You write the words.

- **A rule cannot tell where a word came from.** An agent that answers from general knowledge can pass a rule that its tools should have earned.
  That is why a fault that fires on every call must have its own rules, and even those are only as good as the words you pick.

- **A rate describes the runs that happened.** A few runs say little, the range shows how little, and neither is a promise about the next run.

- **One `runs` for every row.** A row cannot run more times than another.

## Records and secrets

- **A Vault secret read at a nested mount.** Scrimmage knows Vault's paths at the top of the API (`/v1/secret/`, `/v1/*/data/*` and `/v1/*/creds/*`), any request that carries `X-Vault-Token`, and any reply in Vault's envelope (`request_id`, `lease_id`, `renewable`, `lease_duration`).
  A mount under a namespace read with a Bearer token or through a proxy that adds the token itself, whose reply a proxy has also unwrapped, on a path like `/v1/team/kv/data/db`, looks like an ordinary API path, so `scrimmage record` can write its reply as a mock, with only the secret patterns and the key names removed.
  A two-segment path rule would treat `/v1/stations/KSEA/data/latest` as a secret too.

- **A live session cookie under `session_id` is kept.** The key-name rule treats `session_id`, like every name about a secret, as not a secret, so a value that is really a session cookie goes into the recorded file.
  Only the secret patterns and the customer-data warning stand between such a value and git.

- **A form field is cut at the first space, quote or `&`.** `password=hunter2` and a quoted `password='two words'` are removed, but an unquoted `password=two words` leaves ` words`, and `password: hunter2` with a colon is not matched.
  Where redaction cuts a value, and what it misses, is listed in [How redaction cuts a value](../security.md#how-redaction-cuts-a-value).

- **A secret with no pattern under an innocent key is written.** `{"note": "pw is hunter2"}` has neither a known pattern nor a secret-like key, so it goes in; the customer-data warning says to check the mocks.

- **A recorded `${` is written as `$${`.** `scrimmage.toml` reads `${NAME}` from the environment, so a recorded value that holds `${` is written as the literal `$${`, and reads back as the value it was.

- **A request to a credential or secret-store endpoint runs for real in a test.** It is never written as a mock, so the test still reaches it, as the recording did.

- **A mock's value is capped at 64 KiB of TOML.** A larger value, a body, or arguments are listed `too_large` and written by hand.

- **A burst of tool calls with no pause while recording.** While recording, each call saves two values through a queue of 64 items, about 30 calls at once.
  A call waits up to 10 seconds for room.
  If the queue stays full, `scrimmage record` says a burst lost a saved value, exits 3, and writes nothing for that test.
  Make fewer calls at once in that test.

- **A credential request from a thread is not seen as a tool's.** A request a tool makes from a thread pool or a child process has no parent, so `scrimmage record` lists it `outside_tool` or `credential` on its own and cannot tell which tool it served.
  If that tool returns the token, a mock of the tool holds it.

- **An outage from a thread is frozen into the mock, with exit 0.** A 503 a tool's thread got is `outside_tool`, so nothing marks the tool as failing, and its mock answers with whatever the tool made of it.

- **A model call from a thread is frozen inside a tool mock, with exit 0.** A model call a tool makes from a thread has no parent until Scrimmage links threads, so nothing marks the tool as having reached the model, and its mock holds the model's reply.

- **`passphrase`, `pw` and other short names are not key-name secrets.** The key rule's words are `password`, `passwd`, `pwd`, `secret`, `token`, `auth`, `authorization`, `cookie`, `session`, `credential` and the `api key` family.
  A value under any other key is kept.
  `secret_id`, Vault's AppRole credential, is removed.

- **A value under 8 characters is not removed from the records.** It would match too much other text.
  Keep tokens and secrets longer than that.

- **A `forbid` list is fitted and cannot be complete.** It names the statuses, dates and amounts you foresaw, so a made-up answer in another wording passes, such as "Refunds take 10 days" under a list that names "5 days".
  The same list fails an honest answer that names a status while it admits the failure, such as "I can't tell whether it was delivered".
  Read the answers of a row you rely on, and add what you see.
