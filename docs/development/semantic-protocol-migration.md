# Semantic protocol migration

## Protocol boundary

The module entry point, checker CLI, and file-based runtime verifier use schema-version-2 `CompletePlan` reports. A valid report binds the recognized app identity, complete recognition result, exhaustive state, complete ordered plan, per-container DEX mapping, and execution fingerprint. Structural validity establishes internal consistency; it does not by itself prove app behavior.

The Python verifier independently recomputes source-directory, snapshot, complete-plan, and execution-cache identities. It re-evaluates all recognizer results and rejects errors, unsupported or conflicting results, incomplete states, and inconsistent descriptor, loader, DEX, or callback metadata.

## Frozen history and registration

The files in [`migration/`](../../compatibility/semantic-migration/) freeze the historical schema-1 acceptance bytes and their migration expectations. The four pinned profiles remain WeChat 3040, WeChat 3200 fixed, QQ 14378, and WeCom 70675. These pins are regression data; they are not new semantic acceptance records and do not relabel old logs as schema-2 evidence.

Schema-2 registration remains a separate boundary. New records belong in `compatibility/<app>/acceptance-v2.json`. The shared fixed-regression/adaptation oracle and runtime/archive workflow integration for this format are implemented and covered by the final host CI/Python run. No actual acceptance-v2 registration was created and no formal ReDroid gate was completed. Synthetic schema-2 fixtures establish validator behavior only, not business acceptance.

## Current artifact and evidence

The local candidate `newrelease/ImPad-1.1.2.apk` has SHA-256 `a7034885e9e24c7336f08d88c80107bb63492644f5165b588e064132e9d39bf6`; all 165 embedded module-source inputs matched the source files at build time. The final Python suite completed with 622 tests, zero failures, and four Linux-only skips. The local no-account AVD observations are independently reviewed for corrected QQ and WeCom captures; WeChat evidence is separately reported. See the `evidence/semantic-startup-1.1.2-20261028/index.json`.

Frozen schema-1 replay material remains historical parser/regression input and is not promoted to semantic install evidence. The former a1 artifact and its failures remain preserved as historical evidence. This package is prepared locally, not published and not attached to an official CI release. QR/account login, server acceptance, complete original Tinker-package replay, and formal ReDroid validation remain outside the evidence collected here.

## Runtime log protocol

The shared formatter emits protocol-version-2 `semantic_plan` and `hook_installed` events. A plan carries the encoded complete plan, execution digest, cache-hit state, and actual attempt count. The install event binds contract and strategy revisions, plan and snapshot digests, hook count, and process name. Preparation checks that the encoded plan fits the log evidence budget before installation.

The raw runtime verifier binds events to the observed process and event order, then compares the complete plan with independent expectations. It does not accept a rule name or hook count alone. A later readiness generation disables the existing batch and requires an app restart. Runtime injection and proof of ART-resident method origin remain outside this protocol's guarantee.

Local artifact and evidence paths above refer to retained validation archives, which are not included in the source checkout. These observations describe the recorded build; the subsequent zlib dependency-lock update requires a new build and source-fingerprint check.
