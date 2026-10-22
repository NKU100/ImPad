import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import org.gradle.api.tasks.PathSensitivity
import java.security.MessageDigest

plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

kotlin {
    jvmToolchain(21)
}

dependencies {
    implementation(project(":compat-core"))
    implementation(libs.smali.dexlib2)
    implementation(libs.kotlinx.serialization.json)
    compileOnly(
        files(rootProject.file("${rootProject.extra["dexkitOutputRoot"]}/${rootProject.extra["dexkitHostPlatform"]}/dexkit.jar"))
            .builtBy(rootProject.tasks.named("prepareDexKitForHost")),
    )
    testImplementation(kotlin("test"))
    testImplementation(libs.smali.dexlib2)
    testImplementation(
        files(rootProject.file("${rootProject.extra["dexkitOutputRoot"]}/${rootProject.extra["dexkitHostPlatform"]}/dexkit.jar"))
            .builtBy(rootProject.tasks.named("prepareDexKitForHost")),
    )
    testImplementation(libs.flatbuffers.java)
}

tasks.test {
    environment(
        "DEXKIT_RUNTIME_DIR",
        rootProject.file("${rootProject.extra["dexkitOutputRoot"]}/${rootProject.extra["dexkitHostPlatform"]}").absolutePath,
    )
}

tasks.processResources {
    dependsOn(rootProject.tasks.named("prepareDexKitForHost"))
    from(rootProject.file("gradle/dexkit-lock.json"))
}

val ruleSourceRoot = projectDir.resolve("src/main/kotlin")
val resolverSourceRoot = rootProject.file("compat-core/src/main/kotlin/io/github/nku100/impad/compat/rules")
val qqStrategySourceRoot = rootProject.file("app/src/main/kotlin/io/github/nku100/impad/apps/qq")
val wecomStrategySourceRoot = rootProject.file("app/src/main/kotlin/io/github/nku100/impad/apps/wecom")
val wechatStrategySourceRoot = rootProject.file("app/src/main/kotlin/io/github/nku100/impad/apps/wechat")
val androidResolverSourceFiles = listOf(
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/TargetMethodResolver.kt"),
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/RuleRuntimeResolver.kt"),
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/RuleResolutionCache.kt"),
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/InstalledBuildIdentityReader.kt"),
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/AndroidDexKitRuntime.kt"),
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/RuleInstallCoordinator.kt"),
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt"),
)
val strategyRuntimeSourceFiles = listOf(
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt"),
    rootProject.file("app/src/main/kotlin/io/github/nku100/impad/runtime/HookCall.kt"),
)
val dexkitLockFile = rootProject.file("gradle/dexkit-lock.json")
val moduleSourceInputs = rootProject.fileTree(rootProject.projectDir) {
    include(
        "app/src/main/**",
        "app/src/release/keepRules/**",
        "compat-core/src/main/**",
        "compat-rules/src/main/**",
        "compat-checker/src/main/**",
        "scripts/ci/**",
        ".github/workflows/runtime-validation.yml",
        ".github/actions/prepare-dexkit-system-deps/**",
        "gradle.properties",
        "build.gradle.kts",
        "settings.gradle.kts",
        "app/build.gradle.kts",
        "compat-core/build.gradle.kts",
        "compat-rules/build.gradle.kts",
        "compat-checker/build.gradle.kts",
        "gradle/dexkit-lock.json",
        "gradle/libs.versions.toml",
        "gradle/wrapper/gradle-wrapper.properties",
    )
    exclude("**/build/**", "**/.gradle/**", "**/__pycache__/**", "**/*.pyc")
    exclude("scripts/ci/apps/qq/probe/collector_owner_test.c")
}
val fingerprintResource = layout.buildDirectory.file("generated/ruleExecutionFingerprint/rule-execution-fingerprint.json")
val generateRuleExecutionFingerprint = tasks.register("generateRuleExecutionFingerprint") {
    inputs.files(fileTree(ruleSourceRoot) { include("**/*.kt") }).withPropertyName("ruleSources")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.files(fileTree(resolverSourceRoot) { include("**/*.kt") }).withPropertyName("resolverSources")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.files(androidResolverSourceFiles).withPropertyName("androidResolverSources")
    inputs.files(fileTree(qqStrategySourceRoot) { include("**/*.kt") })
        .withPropertyName("qqStrategySources").withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.files(fileTree(wecomStrategySourceRoot) { include("**/*.kt") })
        .withPropertyName("wecomStrategySources").withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.files(fileTree(wechatStrategySourceRoot) { include("**/*.kt") })
        .withPropertyName("wechatStrategySources").withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.files(strategyRuntimeSourceFiles).withPropertyName("strategyRuntimeSources")
    inputs.files(moduleSourceInputs).withPropertyName("moduleSourceInputs")
        .withPathSensitivity(PathSensitivity.RELATIVE)
    inputs.file(dexkitLockFile).withPropertyName("dexkitLock")
    outputs.file(fingerprintResource)
    doLast {
        fun sha256(bytes: ByteArray) = MessageDigest.getInstance("SHA-256")
            .digest(bytes).joinToString("") { "%02x".format(it) }
        fun digestSources(sources: List<Map<String, String>>) = sha256(
            sources.sortedBy { it.getValue("path") }.joinToString("") {
                "${it.getValue("path")}\n${it.getValue("sha256")}\n"
            }.toByteArray(),
        )
        fun sources(root: File, prefix: String): List<Map<String, String>> = fileTree(root) { include("**/*.kt") }
            .files.sortedBy { it.relativeTo(root).invariantSeparatorsPath }
            .map { file ->
                mapOf(
                    "path" to "$prefix/${file.relativeTo(root).invariantSeparatorsPath}",
                    "sha256" to sha256(file.readBytes()),
                )
            }
        fun strategySources(): List<Map<String, String>> =
            (fileTree(qqStrategySourceRoot) { include("**/*.kt") }.files +
                fileTree(wecomStrategySourceRoot) { include("**/*.kt") }.files +
                fileTree(wechatStrategySourceRoot) { include("**/*.kt") }.files + strategyRuntimeSourceFiles)
            .distinct()
            .sortedBy { it.relativeTo(rootProject.projectDir).invariantSeparatorsPath }
            .map { file ->
                require(file.isFile) { "Missing QQ strategy source: $file" }
                mapOf(
                    "path" to file.relativeTo(rootProject.projectDir).invariantSeparatorsPath,
                    "sha256" to sha256(file.readBytes()),
                )
            }
        val lockBytes = dexkitLockFile.readBytes()
        val moduleSources = moduleSourceInputs.files.sortedBy { it.relativeTo(rootProject.projectDir).invariantSeparatorsPath }
            .map { file ->
                mapOf(
                    "path" to file.relativeTo(rootProject.projectDir).invariantSeparatorsPath,
                    "sha256" to sha256(file.readBytes()),
                )
            }
        val dependencyLockPaths = setOf(
            "gradle/dexkit-lock.json",
            "gradle/libs.versions.toml",
            "gradle/wrapper/gradle-wrapper.properties",
        )
        val routeSources = moduleSources.filter {
                it.getValue("path").startsWith("compat-core/src/main/") ||
                it.getValue("path").startsWith("compat-rules/src/main/")
        }
        val dependencySources = moduleSources.filter { it.getValue("path") in dependencyLockPaths }
        require(dependencySources.map { it.getValue("path") }.toSet() == dependencyLockPaths) {
            "A dependency lock input is missing from the module fingerprint"
        }
        val lock = JsonSlurper().parse(dexkitLockFile) as Map<*, *>
        val source = lock["source"] as Map<*, *>
        val artifacts = lock["artifacts"] as Map<*, *>
        fun artifact(key: String): Map<*, *> = artifacts[key] as Map<*, *>
        val document = linkedMapOf(
            "schemaVersion" to 1,
            "ruleSources" to sources(ruleSourceRoot, "compat-rules/src/main/kotlin"),
            "resolverSources" to (sources(
                resolverSourceRoot,
                "compat-core/src/main/kotlin/io/github/nku100/impad/compat/rules",
            ) + androidResolverSourceFiles.sortedBy { it.relativeTo(rootProject.projectDir).invariantSeparatorsPath }
                .map { file ->
                    require(file.isFile) { "Missing Android rule resolver source: $file" }
                    mapOf(
                        "path" to file.relativeTo(rootProject.projectDir).invariantSeparatorsPath,
                        "sha256" to sha256(file.readBytes()),
                    )
                }),
            "strategySources" to strategySources(),
            "moduleSources" to moduleSources,
            "sourceTreeSha256" to digestSources(moduleSources),
            "routeConfigurationSha256" to digestSources(routeSources),
            "dependencyLockSha256" to digestSources(dependencySources),
            "dexkitLockSha256" to sha256(lockBytes),
            "dexkitSourceCommit" to source["commit"],
            "dexkitApiSha256" to artifact("jvmApi")["sha256"],
            "dexkitAndroidApiSha256" to artifact("androidAar")["sha256"],
            "dexkitLinuxAarch64NativeSha256" to artifact("linuxAarch64")["sha256"],
            "dexkitLinuxX8664NativeSha256" to artifact("linuxX8664")["sha256"],
            "dexkitAndroidArm64NativeSha256" to artifact("androidArm64V8a")["sha256"],
        )
        val output = fingerprintResource.get().asFile
        output.parentFile.mkdirs()
        output.writeText(JsonOutput.prettyPrint(JsonOutput.toJson(document)) + "\n")
    }
}

tasks.processResources {
    dependsOn(generateRuleExecutionFingerprint)
    from(fingerprintResource.map { it.asFile.parentFile })
}
