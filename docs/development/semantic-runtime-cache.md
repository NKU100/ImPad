# Startup snapshot and semantic-plan cache

## Startup flow

`ImPadModule` delegates startup work to `SemanticRuntimeCoordinator`. Each app adapter determines its ready point. The coordinator verifies installed package identity and startup loader layout, observes code inputs, selects one complete plan, and installs its hooks as an atomic batch. A later attach invalidates the old batch and requires an app restart; the running process does not switch plans in place.

## Cache identity and validation

`SemanticPlanCacheKey` binds the protocol, verified package/signature/ABI/version identity, all installed APK content hashes, loader topology, ordered container hashes, complete semantic catalog, and implementation/strategy/provider/dependency execution fingerprints. Paths and loader object addresses are not persistent identity, but they are checked against the current process.

An observed input is provisional until its bytes have been read and hashed. The implementation checks file identity, size, and time during reading, then rechecks content before use. The first scan copies and parses the observed bytes and records each loader/container position with ordered DEX hashes. The captured mapping must reconstruct the same snapshot and match the initial container identity. This protects the cache key from an input changing during capture, including an A→B→A change when B is what was captured. The guarantee covers observable standard startup file inputs.

A cached plan records the loader layout, complete container mapping, and full plan. Reads validate positions, topology, container bytes, DEX order, snapshot digest, strategy revision, roles, stages, configuration, and target source scope. Missing or inconsistent entries become cache misses. A hit avoids native queries and a full DEX class scan; it still binds each method to its declared loader and verifies its shape. This is reuse of a validated app-private result, not tamper-proof storage against a privileged process.

## Lease lifecycle

Snapshot directories use an owned marker and a locked lease. The stable root file `.impad-code-lifecycle.lock` serializes snapshot publication, scavenging, and close cleanup. It must remain in place: deleting it while a process holds its inode could let another process lock a new inode and bypass mutual exclusion. Normal close removes the private snapshot tree and releases the lease. A later capture can reclaim an owned directory only after confirming its marker, lease, real-directory status, and symlink-free tree, and acquiring the lease. Unmarked or unsafe directories are retained.

The focused host lifecycle suite recorded 67 passing tests, and the earlier a1 local API 30/6 GiB QQ run recorded cache miss/hit, normal cleanup, and recovery of a dead-owned snapshot after controlled `SIGKILL`. These lifecycle records remain attributed to that earlier run. The a703 artifact audit confirms its DEX, native libraries, and app assets match a1; the new a703 local review covered WeChat UI evidence and corrected QQ/WeCom captures, not a new lease or 6 GiB run. See the `evidence/semantic-startup-1.1.2-20261028/index.json`.

## Limits

The cache does not guarantee atomicity against arbitrary concurrent writes, runtime code replacement, or injection, and it does not authenticate the origin of ART-resident methods. The complete original WeChat hot-update package replay and hot-update business acceptance remain unverified.

Local artifact and evidence paths above refer to retained validation archives, which are not included in the source checkout. These observations describe the recorded build; the subsequent zlib dependency-lock update requires a new build and source-fingerprint check.
