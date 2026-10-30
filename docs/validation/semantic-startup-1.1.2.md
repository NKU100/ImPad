# ImPad 1.1.2 startup validation

## Current candidate and build

The recorded local candidate is `newrelease/ImPad-1.1.2.apk` (a703).

- APK SHA-256: `a7034885e9e24c7336f08d88c80107bb63492644f5165b588e064132e9d39bf6` (954,429 bytes).
- Signature: APK Signature Scheme v2; signer certificate SHA-256 `d1e2f14a6851822874a6753041ea5681f91ad33e7ec0d9fd264d52f174b8b409`.
- R8 mapping: `newrelease/mapping-1.1.2-ci-closure.txt`, SHA-256 `8b80e7cff1488bab99d3cb087a388e0c448d1a849d9c5d50636427e63e6cf573`.
- Embedded fingerprint: all 165 module-source files matched the source tree at build time; source-tree SHA-256 `a5f45fe5d22c6c41dd011604c0594f93aadbfdb148eaa9e02c4840f28fd6ce25`, route configuration `11de8c46963bebf8e2aa1644489e957dd1c357fd0299cdfec60392c7015d8641`, dependency lock `33bc276f0e7db9e9f82af7d9d3f4f1e8bced386b58f847b648526ebdab9e66ee`.
- Gradle: the offline JDK 21 tasks `:compat-core:test`, `:compat-checker:test`, `:app:testDebugUnitTest`, `:app:testReleaseUnitTest`, `:app:assembleRelease`, and `:app:verifyReleaseModuleMetadata` completed successfully.
- Python: 622 tests, zero failures, four Linux-only skips. Three skips require Linux `flock`; one QQ owner-harness skip requires a Linux C toolchain. The full Linux-native `compat-rules:test` fixture suite was not run on this macOS host.

The CI/Python acceptance-v2 baseline integration is implemented and covered by the final host suite. It does not create a real registration. The build report, test log, final runtime review, and fingerprint audit are linked in the `evidence/semantic-startup-1.1.2-20261028/index.json`.

## Local no-account AVD observations

Independent runtime review found no new blocker in the corrected QQ and WeCom evidence. Together with the separately supplied WeChat capture, the bounded local, logged-out AVD preparation is complete. The review did not re-run WeChat; its observation remains tied to its own report and raw records.

| App | AVD and observed result | Evidence scope |
|---|---|---|
| WeChat 3200 fixed | API 37, 16 KiB AVD. Disabled state had no tablet entry; enabled state showed the tablet entry and public scan prompt. Cold plan miss and warm cache hit were recorded. | a703-specific report and selected summaries/hierarchies; no account credentials or QR image. |
| WeCom 70675 | API 30, 4 KiB AVD. Corrected enabled capture shows the tablet entry as `visible=true`; the public agreement dialog was accepted and the activity reached `LoginQrCodeForAndroidPad` with the public scan prompt. | Independent review verified the paired UI evidence. The older `visible=false` captures remain historical, not reinterpreted. No QR screenshot or payload was saved. |
| QQ 14378 | API 30, 4 KiB AVD. Independent production-decoder review confirmed 3 disabled phone frames with AppID `537360567` and 5 enabled tablet frames with AppID `537360606`. Each capture had one MSF owner and healthy ready/end records; for enabled PID 32683, hook installation preceded every captured request. | Exact a703 module and official QQ APK identities were checked. IPv6 settings were restored to `0`; this is an IPv4-only capture and does not generalize to arbitrary networks. |

The detailed `evidence/semantic-startup-1.1.2-20261028/a703/reports/20261028-a703-avd-observations.md`, `evidence/semantic-startup-1.1.2-20261028/a703/reports/20261028-a703-final-runtime-review.md`, and `evidence/semantic-startup-1.1.2-20261028/a703/manifest.json` identify source paths, durable paths, sizes, and hashes. The corrected 347-file manifest entries were independently verified against source byte counts and SHA-256 values.

## Historical a1 and remaining boundaries

The earlier a1 package and its evidence remain indexed as a historical source/build snapshot. Its earlier QQ failure and the a703 report's first WeCom `visible=false` observations have not been deleted or rewritten. Later a703 corrections are appended and reviewed separately. The a1 lease-lifecycle and native-map reports remain evidence for that earlier package/run; they are not presented as new a703 runtime trials. The a703 APK audit confirms its DEX, native libraries, and app assets match a1, while its CI/Python source fingerprint differs.

No actual `acceptance-v2.json` registration or formal ReDroid gate was completed. No complete original Tinker package replay, QR scan, authenticated login, server-side acceptance, or simultaneous-session test was performed. The local package is prepared but not published and is not an official CI artifact attachment. Future source or CI metadata changes require verification of the resulting official artifact attachment.

Local artifact and evidence paths above refer to retained validation archives, which are not included in the source checkout. These observations describe the recorded build; the subsequent zlib dependency-lock update requires a new build and source-fingerprint check.
