# Local QQ runtime acceptance

Both Android versions use the same QQ request acceptance implementation. Each version has its own entry point, AVD and evidence directory.

| Android | Local entry | AVD | Evidence |
| --- | --- | --- | --- |
| 11 / API 30 | `scripts/ci/run_local_qq_api30.sh` | `ImPadQqApi30` | `work/qq-local-api30/evidence` |
| API 37.2 | `scripts/ci/run_local_qq_api37_2.sh` | `ImPadQqApi37_2` | `work/qq-local-api37_2/evidence` |

Launch a dedicated ARM64 AVD with the verified ramdisk for that image. The API 30 recipe is `environment/avd-api30-google-arm64.json`; the x86_64 CI recipe remains `environment/avd-api30-google.json`. Never use one image's ramdisk with another image.

Supply an input directory containing `qq.apk`, `magisk.apk`, `zygisk-next.zip`, `lsposed.zip`, `manager.apk`, `ui-hierarchy.jar`, and `module/app-debug.apk`. The UI helper must support API 28 or newer.

```sh
bash scripts/ci/run_local_qq_api30.sh --serial emulator-5582 --inputs /path/to/inputs
bash scripts/ci/run_local_qq_api37_2.sh --serial emulator-5584 --inputs /path/to/inputs
```

The runner checks Android API, ABI and emulator identity before installing anything. It enables Zygisk Next with anonymous memory disabled and the system linker, installs LSPosed and ImPad, then captures actual phone/tablet requests. During capture, other UIDs' external TCP is temporarily blocked and the rules are removed on exit. It never enters credentials or submits a login.

Preserve or move an existing evidence directory before another run. A successful installation or hook log is insufficient: `smoke-report.json` must report `RUNTIME_SMOKE_VERIFIED` with the expected phone/tablet AppIDs and verified socket ownership. Local ARM64 acceptance validates automation; independent CI x86_64 acceptance also tests ARM translation.
