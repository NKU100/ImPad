"""Exercise Gradle's DexKit preparation-to-consumer dependency edges."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "gradle/dexkit-lock.json"
SYSTEM_DEPS_ACTION = "./.trusted-dexkit/.github/actions/prepare-dexkit-system-deps"


def parsed_workflows() -> dict[str, dict]:
    paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
    paths += sorted((ROOT / ".github/workflows").glob("*.yaml"))
    ruby = r'''require "yaml"; require "json"; ARGV.each { |path| puts JSON.generate({"path" => path, "workflow" => YAML.safe_load(File.read(path), aliases: true)}) }'''
    parsed = subprocess.run(["ruby", "-e", ruby, *map(str, paths)], cwd=ROOT,
                            text=True, capture_output=True, check=False)
    if parsed.returncode != 0:
        raise AssertionError(f"Could not parse GitHub workflows with Ruby Psych: {parsed.stderr}")
    return {Path(item["path"]).name: item["workflow"] for item in map(json.loads, parsed.stdout.splitlines())}


def _step_runs_checker(step: dict) -> bool:
    run = step.get("run", "")
    if not isinstance(run, str):
        return False
    if re.search(r"(?:\./gradlew|gradlew\s).*?(?::compat-checker:|:compat-rules:)", run, re.S):
        return True
    for module_match in re.finditer(r"scripts\.ci\.([a-zA-Z0-9_]+)([^\n]*)", run):
        module, arguments = module_match.groups()
        if module == "app_policy":
            continue
        if module == "codex_adaptation" and not re.search(r"\bvalidate\b", arguments):
            continue
        source = ROOT / "scripts/ci" / f"{module}.py"
        if source.is_file() and re.search(r"(?:\./gradlew|:compat-checker:|:compat-rules:)", source.read_text()):
            return True
    if "unittest discover" in run:
        for source in (ROOT / "scripts").glob("test_*.py"):
            contents = source.read_text()
            if re.search(r"(?:\./gradlew|:compat-checker:|:compat-rules:)", contents):
                return True
    return False


def workflow_checker_consumers(workflows: dict[str, dict]) -> list[tuple[str, str, int, dict]]:
    consumers = []
    for workflow_name, workflow in workflows.items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
                continue
            for index, step in enumerate(job["steps"]):
                if _step_runs_checker(step):
                    consumers.append((workflow_name, job_name, index, job))
    return consumers


def _resolve_github_ref(expression: str, context: dict) -> str:
    match = re.fullmatch(r"\s*\$\{\{\s*(.*?)\s*\}\}\s*", expression, re.S)
    if match is None:
        raise AssertionError(f"Unsupported checkout ref expression: {expression!r}")
    for candidate in match.group(1).split("||"):
        parts = candidate.strip().split(".")
        if not parts or parts[0] != "github":
            raise AssertionError(f"Unsupported checkout ref expression: {expression!r}")
        value = context
        for part in parts:
            value = value.get(part) if isinstance(value, dict) else None
        if value:
            return str(value)
    raise AssertionError(f"Checkout ref expression resolved to an empty value: {expression!r}")


def _run_fixture_git(repository: Path, *arguments: str) -> str:
    command = [
        "git",
        "-c", "user.name=Workflow Test Fixture",
        "-c", "user.email=workflow-test-fixture@example.invalid",
        "-C", str(repository),
        *arguments,
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"Fixture git command failed: {command!r}\n{result.stderr}")
    return result.stdout.strip()


def _github_if_matches(expression: str, event_name: str, ref: str,
                       step_outputs: dict[str, str] | None = None) -> bool:
    normalized = re.sub(
        r"startsWith\(\s*github\.ref\s*,\s*('[^']*'|\"[^\"]*\")\s*\)",
        lambda match: str(ref.startswith(ast.literal_eval(match.group(1)))),
        expression,
    )
    normalized = normalized.replace("github.event_name", repr(event_name))
    normalized = normalized.replace("github.ref", repr(ref))
    for name, value in (step_outputs or {}).items():
        normalized = normalized.replace(name, repr(value))
    normalized = normalized.replace("&&", " and ").replace("||", " or ")
    root = ast.parse(normalized, mode="eval").body

    def evaluate(node: ast.AST) -> object:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bool)):
            return node.value
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [bool(evaluate(value)) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
            left = evaluate(node.left)
            right = evaluate(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(f"Unsupported GitHub if expression: {expression!r}")

    return bool(evaluate(root))


def host_platform() -> str:
    if "linux" not in platform.system().lower():
        return "android-arm64-v8a"
    arch = platform.machine().lower()
    if arch in {"x86_64", "amd64", "x64"}:
        return "linux-x86_64"
    if arch in {"aarch64", "arm64"}:
        return "linux-aarch64"
    raise AssertionError(f"Unsupported Linux test runner: {arch}")


class DexKitGradleIntegrationTest(unittest.TestCase):
    def test_build_pr_revision_contains_dexkit_action_before_its_consumer(self):
        workflow = parsed_workflows()["build.yml"]
        job = workflow["jobs"]["build"]
        steps = job["steps"]
        preparation_checkouts = [
            step for step in steps
            if step.get("uses") == "actions/checkout@v7" and step.get("with", {}).get("path")
        ]
        self.assertEqual(len(preparation_checkouts), 1)
        preparation_checkout = preparation_checkouts[0]
        checkout = preparation_checkout["with"]
        local_action = next(
            step for step in steps
            if step.get("uses", "").endswith("/.github/actions/prepare-dexkit-system-deps")
        )
        action_index = steps.index(local_action)
        checkout_index = steps.index(preparation_checkout)
        self.assertLess(checkout_index, action_index)
        consumers = [
            consumer_index for workflow_name, job_name, consumer_index, _ in
            workflow_checker_consumers({"build.yml": workflow})
            if workflow_name == "build.yml" and job_name == "build"
        ]
        self.assertTrue(consumers)
        self.assertLess(action_index, min(consumers))

        with tempfile.TemporaryDirectory(prefix="dexkit-pr-bootstrap-") as temporary:
            temporary_root = Path(temporary)
            repository = temporary_root / "repository"
            repository.mkdir()
            _run_fixture_git(repository, "init", "--quiet", "--initial-branch=main")
            (repository / "README.md").write_text("base revision\n")
            _run_fixture_git(repository, "add", "README.md")
            _run_fixture_git(repository, "commit", "--quiet", "-m", "base without preparation action")
            base_sha = _run_fixture_git(repository, "rev-parse", "HEAD")
            base_action = subprocess.run(
                ["git", "-C", str(repository), "cat-file", "-e",
                 f"{base_sha}:.github/actions/prepare-dexkit-system-deps/action.yml"],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(base_action.returncode, 0, "Fixture base unexpectedly contains the new action")

            _run_fixture_git(repository, "switch", "--quiet", "-c", "pull-request")
            fixture_files = {
                ".github/actions/prepare-dexkit-system-deps/action.yml": "name: fixture preparation action\n",
                "scripts/ci/prepare_dexkit_system_deps.py": "# fixture helper\n",
                "gradle/dexkit-lock.json": '{"fixture": true}\n',
            }
            for relative_path, contents in fixture_files.items():
                target = repository / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)
            _run_fixture_git(repository, "add", *fixture_files)
            _run_fixture_git(repository, "commit", "--quiet", "-m", "add preparation action")
            pull_request_sha = _run_fixture_git(repository, "rev-parse", "HEAD")
            merge_tree = _run_fixture_git(repository, "rev-parse", f"{pull_request_sha}^{{tree}}")
            merge_sha = _run_fixture_git(
                repository, "commit-tree", merge_tree,
                "-p", base_sha, "-p", pull_request_sha,
                "-m", "synthetic pull request merge revision",
            )
            self.assertEqual(_run_fixture_git(repository, "rev-parse", f"{merge_sha}^1"), base_sha)
            self.assertEqual(_run_fixture_git(repository, "rev-parse", f"{merge_sha}^2"), pull_request_sha)

            context = {"github": {
                "sha": merge_sha,
                "event_name": "pull_request",
                "event": {"pull_request": {"base": {"sha": base_sha}}},
            }}
            resolved_ref = _resolve_github_ref(checkout["ref"], context)
            self.assertEqual(resolved_ref, merge_sha)
            checkout_path = temporary_root / "workspace" / checkout["path"]
            checkout_path.parent.mkdir(parents=True, exist_ok=True)
            _run_fixture_git(repository, "worktree", "add", "--quiet", "--detach", str(checkout_path), resolved_ref)
            for relative_path in fixture_files:
                self.assertTrue(
                    (checkout_path / relative_path).is_file(),
                    f"PR preparation checkout {resolved_ref} lacks {relative_path}",
                )
            self.assertNotIn("trusted", preparation_checkout.get("name", "").lower())
            self.assertIs(checkout.get("persist-credentials"), False)

            push_context = {"github": {"sha": pull_request_sha}}
            self.assertEqual(_resolve_github_ref(checkout["ref"], push_context), pull_request_sha)

    def test_build_pr_has_read_only_permissions_and_skips_secret_steps(self):
        workflow = parsed_workflows()["build.yml"]
        job = workflow["jobs"]["build"]
        self.assertEqual(job.get("permissions", workflow.get("permissions")), {"contents": "read"})
        self.assertNotIn("secrets.", json.dumps(workflow.get("env", {})))
        self.assertNotIn("secrets.", json.dumps(job.get("env", {})))

        secret_steps = [
            step for step in job["steps"]
            if "secrets." in json.dumps(step)
        ]
        self.assertTrue(secret_steps, "Expected the stable signing step to be explicitly gated")
        for step in secret_steps:
            self.assertFalse(
                _github_if_matches(step.get("if", ""), "pull_request", "refs/pull/42/merge"),
                f"PR event can enter secret-bearing step {step.get('name')!r}",
            )

        publication_steps = [
            step for step in job["steps"]
            if re.search(r"(?:release|publish)", step.get("name", ""), re.I)
            or re.search(r"(?:release|publish)", step.get("uses", ""), re.I)
            or re.search(r"\b(?:gh\s+release|release\s+create)\b", step.get("run", ""), re.I)
        ]
        for step in publication_steps:
            self.assertFalse(
                _github_if_matches(step.get("if", ""), "pull_request", "refs/pull/42/merge",
                                   {"steps.validation-mode.outputs.enabled": "false"}),
                f"PR event can enter release/publish step {step.get('name')!r}",
            )

    def test_every_workflow_checker_consumer_prepares_locked_system_dependencies_first(self):
        workflows = parsed_workflows()
        self.assertTrue(workflows, "Expected at least one GitHub workflow YAML file")
        shared = workflows["compatibility-app.yml"]
        for wrapper_job in ("verify", "rules-analysis"):
            with self.subTest(wrapper_job=wrapper_job):
                self.assertEqual(
                    shared["jobs"][wrapper_job]["uses"],
                    "./.github/workflows/runtime-validation.yml",
                )
                self.assertIn("app_id", shared["jobs"][wrapper_job]["with"])

        action_path = ROOT / ".github/actions/prepare-dexkit-system-deps/action.yml"
        action = subprocess.run(
            ["ruby", "-e", 'require "yaml"; require "json"; puts JSON.generate(YAML.safe_load(File.read(ARGV.fetch(0)), aliases: true))', str(action_path)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(action.returncode, 0, action.stderr)
        action_steps = json.loads(action.stdout)["runs"]["steps"]
        action_runs = "\n".join(step.get("run", "") for step in action_steps)
        self.assertIn("scripts.ci.prepare_dexkit_system_deps", action_runs)
        self.assertIn("gradle/dexkit-lock.json", action_runs)
        self.assertIn("--install", action_runs)
        self.assertRegex(action_runs, r"sudo\s+python3\s+-B\s+-m\s+scripts\.ci\.prepare_dexkit_system_deps")
        consumers = workflow_checker_consumers(workflows)
        locations = {(workflow, job) for workflow, job, _, _ in consumers}
        self.assertEqual(locations, {
            ("build.yml", "build"),
            ("compatibility-app.yml", "adapt"),
            ("runtime-validation.yml", "prepare"),
        }, f"Expected build, trusted adaptation and shared validation consumers; found {sorted(locations)}")
        expected_runners = {
            ("build.yml", "build"): "ubuntu-24.04",
            ("compatibility-app.yml", "adapt"): "ubuntu-24.04-arm",
            ("runtime-validation.yml", "prepare"): "ubuntu-24.04",
        }

        trusted_consumers = {
            (workflow_name, job_name)
            for workflow_name, job_name in locations
            if (workflow_name, job_name) != ("build.yml", "build")
        }
        self.assertEqual(trusted_consumers, {
            ("compatibility-app.yml", "adapt"),
            ("runtime-validation.yml", "prepare"),
        })

        for workflow_name, job_name, consumer_index, job in consumers:
            self.assertEqual(job.get("runs-on"), expected_runners[(workflow_name, job_name)],
                             f"{workflow_name}:{job_name} must use its locked Ubuntu 24.04 architecture")
            steps = job["steps"]
            build_job = (workflow_name, job_name) == ("build.yml", "build")
            expected_action = ("./.dexkit-preparation/.github/actions/prepare-dexkit-system-deps"
                               if build_job else SYSTEM_DEPS_ACTION)
            action_indexes = [index for index, step in enumerate(steps)
                              if step.get("uses") == expected_action]
            self.assertTrue(action_path.is_file())
            self.assertTrue(action_indexes, f"{workflow_name}:{job_name} lacks the common locked prep action")
            action_index = action_indexes[0]
            self.assertLess(action_index, consumer_index, f"{workflow_name}:{job_name} prepares after its consumer")
            preparation_checkout_path = ".dexkit-preparation" if build_job else ".trusted-dexkit"
            preparation_checkout_indexes = [
                index for index, step in enumerate(steps)
                if step.get("uses") == "actions/checkout@v7"
                and step.get("with", {}).get("path") == preparation_checkout_path
            ]
            self.assertTrue(preparation_checkout_indexes,
                            f"{workflow_name}:{job_name} lacks its preparation-source checkout")
            preparation_checkout = steps[preparation_checkout_indexes[0]]
            self.assertLess(preparation_checkout_indexes[0], action_index)
            self.assertIs(preparation_checkout.get("with", {}).get("persist-credentials"), False)
            if build_job:
                self.assertEqual(preparation_checkout.get("with", {}).get("ref"), "${{ github.sha }}")
                self.assertNotIn("trusted", preparation_checkout.get("name", "").lower())
            else:
                self.assertIn("github.event.pull_request.base.sha || github.sha",
                              preparation_checkout.get("with", {}).get("ref", ""))
                sparse_paths = preparation_checkout.get("with", {}).get("sparse-checkout", "").splitlines()
                self.assertTrue({".github/actions/prepare-dexkit-system-deps", "scripts/ci", "gradle"}.issubset(sparse_paths),
                                f"{workflow_name}:{job_name} does not fetch the complete trusted preparation inputs")
            self.assertGreater(
                min((index for index, step in enumerate(steps)
                     if step.get("uses") == "actions/checkout@v7"
                     and step.get("with", {}).get("path") is None), default=len(steps)),
                action_index,
                f"{workflow_name}:{job_name} checks out consumer source before locked prep",
            )

    def test_clean_output_is_prepared_and_tampering_fails_at_consumers(self):
        with tempfile.TemporaryDirectory(prefix="dexkit-gradle-integration-") as temporary:
            output_root = Path(temporary) / "dexkit"
            self.assertFalse(output_root.exists())

            first_run = self._run_gradle(output_root)
            self.assertEqual(first_run.returncode, 0, first_run.stdout + first_run.stderr)
            self.assert_mocked_official_download(output_root)
            expected_platform = host_platform()
            host_dir = output_root / expected_platform
            android_dir = output_root / "android-arm64-v8a"
            self.assertTrue((host_dir / "dexkit.jar").is_file())
            self.assertTrue((android_dir / "dexkit.aar").is_file())
            self.assertEqual(
                json.loads((host_dir / "dexkit-prepared.json").read_text())["platform"],
                expected_platform,
            )
            self.assertEqual(
                hashlib.sha256((host_dir / "dexkit.jar").read_bytes()).hexdigest(),
                json.loads(LOCK_PATH.read_text())["artifacts"]["jvmApi"]["sha256"],
            )
            self.assertEqual(
                json.loads((host_dir / "dexkit-prepared.json").read_text())["sourceCommit"],
                json.loads(LOCK_PATH.read_text())["source"]["commit"],
            )

            (host_dir / "dexkit.jar").write_bytes(b"tampered API")
            jar_run = self._run_gradle(output_root, ":compat-checker:compileKotlin")
            self.assertNotEqual(jar_run.returncode, 0, jar_run.stdout + jar_run.stderr)
            self.assertRegex(
                jar_run.stdout + jar_run.stderr,
                r"Prepared (Android API JAR does not match the locked AAR|artifact dexkit\.jar SHA-256 mismatch)",
            )

            aar_output_root = Path(temporary) / "aar-dexkit"
            aar_setup = self._run_gradle(aar_output_root)
            self.assertEqual(aar_setup.returncode, 0, aar_setup.stdout + aar_setup.stderr)
            self.assert_mocked_official_download(aar_output_root)
            android_dir = aar_output_root / "android-arm64-v8a"
            (android_dir / "dexkit.aar").write_bytes(b"tampered Android AAR")
            aar_run = self._run_gradle(aar_output_root, ":app:checkDebugAarMetadata")
            self.assertNotEqual(aar_run.returncode, 0, aar_run.stdout + aar_run.stderr)
            self.assertIn("Prepared artifact dexkit.aar SHA-256 mismatch", aar_run.stdout + aar_run.stderr)

    def _run_gradle(self, output_root: Path, *tasks: str) -> subprocess.CompletedProcess[str]:
        selected_tasks = tasks or (
            ":compat-checker:test",
            ":compat-rules:compileKotlin",
            ":app:checkDebugAarMetadata",
        )
        lock = json.loads(LOCK_PATH.read_text())
        artifact = lock["artifacts"]["androidAar"]
        fixture = ROOT / "scripts/testdata/dexkit-2.3.0.aar"
        self.assertTrue(fixture.is_file(), "The authentic locked DexKit AAR test fixture is required")
        self.assertEqual(artifact["sha256"], hashlib.sha256(fixture.read_bytes()).hexdigest())
        fixture_root = output_root.parent / f".{output_root.name}-dexkit-test-mock"
        wrapper_dir = fixture_root / "bin"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        download_log = fixture_root / "official-download-urls.txt"
        toolchain_download_log = fixture_root / "official-toolchain-download-urls.txt"
        wrapper = wrapper_dir / "python3"
        wrapper.write_text(textwrap.dedent(f'''\
            #!{sys.executable}
            import os, sys
            sys.path.insert(0, {str(ROOT)!r})
            arguments = sys.argv[1:]
            script = {str(ROOT / "scripts/ci/prepare_dexkit.py")!r}
            if arguments and os.path.realpath(arguments[0]) == os.path.realpath(script):
                import scripts.ci.prepare_dexkit as prepare
                expected_url = os.environ["DEXKIT_TEST_OFFICIAL_URL"]
                allowed_toolchain_urls = set(os.environ["DEXKIT_TEST_TOOLCHAIN_URLS"].splitlines())
                original_urlopen = prepare.urllib.request.urlopen
                fixture = os.environ["DEXKIT_TEST_AAR_FIXTURE"]
                log = os.environ["DEXKIT_TEST_DOWNLOAD_LOG"]
                toolchain_log = os.environ["DEXKIT_TEST_TOOLCHAIN_LOG"]
                class Response:
                    status = 200
                    headers = {{}}
                    def __init__(self, payload): self.payload, self.offset = payload, 0
                    def read(self, size=-1):
                        end = len(self.payload) if size < 0 else min(len(self.payload), self.offset + size)
                        block = self.payload[self.offset:end]
                        self.offset = end
                        return block
                    def __enter__(self): return self
                    def __exit__(self, *_args): return False
                def mocked_urlopen(request, timeout=60):
                    url = request.full_url
                    if url in allowed_toolchain_urls:
                        with open(toolchain_log, "a", encoding="utf-8") as output:
                            output.write(url + "\\n")
                        return original_urlopen(request, timeout=timeout)
                    if url != expected_url:
                        raise AssertionError("Unexpected DexKit artifact URL: " + url)
                    with open(log, "a", encoding="utf-8") as output:
                        output.write(url + "\\n")
                    with open(fixture, "rb") as source:
                        return Response(source.read())
                prepare.urllib.request.urlopen = mocked_urlopen
                sys.argv = [script, *arguments[1:]]
                prepare.main()
            else:
                os.execv({sys.executable!r}, [{sys.executable!r}, *arguments])
        '''))
        wrapper.chmod(0o755)
        environment = dict(os.environ)
        environment["PATH"] = str(wrapper_dir) + os.pathsep + environment.get("PATH", "")
        environment["DEXKIT_TEST_OFFICIAL_URL"] = artifact["url"]
        environment["DEXKIT_TEST_AAR_FIXTURE"] = str(fixture)
        environment["DEXKIT_TEST_DOWNLOAD_LOG"] = str(download_log)
        environment["DEXKIT_TEST_TOOLCHAIN_LOG"] = str(toolchain_download_log)
        environment["DEXKIT_TEST_TOOLCHAIN_URLS"] = "\n".join(sorted({
            spec["url"]
            for build in lock["build"].values()
            for spec in build["toolchain"].values()
        }))
        return subprocess.run(
            [
                "./gradlew",
                "--no-daemon",
                *selected_tasks,
                f"-PdexkitOutputRoot={output_root}",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_mocked_official_download(self, output_root: Path) -> None:
        log = output_root.parent / f".{output_root.name}-dexkit-test-mock/official-download-urls.txt"
        self.assertTrue(log.is_file(), "DexKit integration must explicitly exercise its download mock")
        urls = log.read_text().splitlines()
        expected_url = json.loads(LOCK_PATH.read_text())["artifacts"]["androidAar"]["url"]
        self.assertTrue(urls, "DexKit integration did not exercise the official AAR acquisition path")
        self.assertEqual([expected_url] * len(urls), urls)


if __name__ == "__main__":
    unittest.main()
