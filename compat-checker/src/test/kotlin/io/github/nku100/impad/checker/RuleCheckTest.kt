package io.github.nku100.impad.checker

import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.rules.MethodShape
import io.github.nku100.impad.compat.rules.ResolvedPlan
import io.github.nku100.impad.compat.rules.ResolvedTarget
import io.github.nku100.impad.compat.rules.RuleDefinition
import io.github.nku100.impad.compat.rules.RuleRef
import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.rules.MethodQuery
import io.github.nku100.impad.rules.RuleClass
import io.github.nku100.impad.rules.RuleMethod
import io.github.nku100.impad.rules.RuleSession
import io.github.nku100.impad.rules.RuleExecutionFingerprint
import io.github.nku100.impad.rules.DexKitRuntimeLoader
import java.nio.file.Files
import java.security.MessageDigest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class RuleCheckTest {
    @Test
    fun sharedCatalogCliSchemaExposesDefinitionsBindingsAndTargets() {
        val arguments = parseRuleCatalogArguments(listOf("--app-id", "qq", "--report", "catalog.json"))
        assertEquals("qq", arguments.appId)
        val catalog = io.github.nku100.impad.rules.RuleCatalog.forApp(arguments.appId)
        val report = RuleCatalogReport(
            appId = catalog.appId,
            ruleCatalogSha256 = catalog.sha256,
            definitions = catalog.definitions,
            bindings = catalog.bindings,
            strategyIds = catalog.strategyIds.sorted(),
        )
        val destination = java.nio.file.Files.createTempFile("impad-catalog-test", ".json").toFile()
        try {
            RuleCatalogReportWriter.write(report, destination)
            val decoded = kotlinx.serialization.json.Json.decodeFromString<RuleCatalogReport>(destination.readText())
            assertEquals(1, decoded.schemaVersion)
            assertEquals(catalog.sha256, decoded.ruleCatalogSha256)
            assertEquals(setOf("device_type"), decoded.definitions.single().requiredTargetIds)
            assertEquals("qq_tablet_001", decoded.definitions.single().strategyId)
            assertEquals("qq_device_type_001", decoded.bindings.single().ruleId)
        } finally {
            destination.delete()
        }
    }

    @Test
    fun rulesCommandRequiresAnExplicitAppId() {
        assertFailsWith<IllegalArgumentException> {
            parseRuleCheckArguments(listOf("--apk", "qq.apk", "--report", "report.json"))
        }
    }

    @Test
    fun rulesCommandAcceptsApkWithoutTargetsFile() {
        val arguments = parseRuleCheckArguments(
            listOf("--app-id", "qq", "--apk", "qq.apk", "--report", "report.json"),
        )

        assertEquals("qq", arguments.appId)
        assertEquals(emptyList(), arguments.abiSplitApks)
    }

    @Test
    fun rejectsWrongPackageSignerAndAbiBeforeOpeningDexSession() = withApk { apk ->
        val valid = identity()
        val wrongIdentities = listOf(
            valid.copy(packageName = "com.example.other"),
            valid.copy(signerSha256 = "0".repeat(64)),
            valid.copy(abi = "armeabi-v7a"),
        )

        wrongIdentities.forEach { identity ->
            var opened = false
            val checker = RuleCheck(
                identityReader = { _, _ -> identity },
                sessionOpener = { opened = true; error("session must not open") },
            )
            assertFailsWith<IllegalArgumentException> { checker.check("qq", listOf(apk)) }
            assertEquals(false, opened)
        }
    }

    @Test
    fun unknownApkShaStillRunsTheSharedRuleAndProducesIdentityAndHashes() = withApk { apk ->
        var queries = 0
        var sessionClosed = false
        val checker = RuleCheck(
            identityReader = { _, _ -> identity(apkSha256 = "f".repeat(64)) },
            sessionOpener = { files ->
                assertEquals(listOf(apk), files)
                object : RuleSession {
                    override fun findMethods(query: MethodQuery): List<RuleMethod> {
                        queries++
                        return emptyList()
                    }
                    override fun findClasses(typeName: String): List<RuleClass> = emptyList()
                    override fun close() { sessionClosed = true }
                }
            },
        )

        val report = checker.check("qq", listOf(apk))

        assertEquals(1, queries)
        assertEquals("f".repeat(64), report.identity.apkSha256)
        assertEquals(64, report.inputHashes.single().sha256.length)
        assertTrue(report.rules.isNotEmpty())
        assertEquals(true, sessionClosed)
        assertTrue(reportJson(report).contains("qq_device_type_001"))
        assertTrue(reportJson(report).contains("STATIC_ONLY"))
    }

    @Test
    fun splitIdentityMismatchesAreRejectedBeforeSessionOpens() = withSplitFixture { sdkRoot, base, split ->
        listOf("package", "version", "signer").forEach { mismatch ->
            var opened = false
            val inspector = ApkIdentityInspector(sdkRoot) { command ->
                val isSplit = command.last() == split.absolutePath
                when {
                    command.getOrNull(1) == "manifest" -> when (command.getOrNull(2)) {
                        "application-id" -> if (isSplit && mismatch == "package") "com.example.foreign" else "com.tencent.mobileqq"
                        "version-name" -> "9.3.1"
                        "version-code" -> if (isSplit && mismatch == "version") "14379" else "14378"
                        else -> error("Unexpected manifest query: $command")
                    }
                    command.getOrNull(1) == "verify" -> if (isSplit && mismatch == "signer") {
                        "V3 Signer: certificate SHA-256 digest: ${"1".repeat(64)}"
                    } else "V3 Signer: certificate SHA-256 digest: ${identity().signerSha256}"
                    else -> error("Unexpected command: $command")
                }
            }
            val checker = RuleCheck(
                identityReader = { apk, splits -> inspector.inspect(apk, splits) },
                sessionOpener = { opened = true; error("session must not open") },
            )

            assertFailsWith<IllegalArgumentException> { checker.check("qq", listOf(base, split)) }
            assertFalse(opened)
        }
    }

    @Test
    fun queryFailureWritesErrorReportAndClosesSession() = withApk { apk ->
        var closed = false
        val session = object : RuleSession {
            override fun findMethods(query: MethodQuery): List<RuleMethod> = error("query backend failed")
            override fun findClasses(typeName: String): List<RuleClass> = emptyList()
            override fun close() { closed = true }
        }
        val report = RuleCheck(
            identityReader = { _, _ -> identity() },
            sessionOpener = { session },
        ).check("qq", listOf(apk))
        val destination = java.io.File("build/test-evidence/query-error-report.json")
        assertFailsWith<IllegalStateException> { writeRuleCheckReport(report, destination) }
        val json = destination.readText()
        val manifest = RuleExecutionFingerprint.loadManifest()
        assertEquals("ERROR", report.selection.status)
        assertTrue(json.contains("qq_device_type_001"))
        assertTrue(json.contains("candidate counts unavailable"))
        assertTrue(json.contains(report.executionIdentity.ruleImplementationSha256))
        assertTrue(json.contains(manifest.dexkitLockSha256))
        assertTrue(json.contains(manifest.dexkitApiSha256))
        assertTrue(json.contains(manifest.dexkitLinuxX8664NativeSha256))
        assertTrue(closed)
    }

    @Test
    fun reportKeepsHashesForBaseAndValidatedSplitInputs() = withSplitFixture { _, base, split ->
        val checker = RuleCheck(
            identityReader = { _, _ -> identity() },
            sessionOpener = { files ->
                assertEquals(listOf(base, split), files)
                object : RuleSession {
                    override fun findMethods(query: MethodQuery): List<RuleMethod> = emptyList()
                    override fun findClasses(typeName: String): List<RuleClass> = emptyList()
                    override fun close() = Unit
                }
            },
        )

        val report = checker.check("qq", listOf(base, split))

        assertEquals(listOf(base.absolutePath, split.absolutePath), report.inputHashes.map { it.path })
        assertEquals(listOf(sha256(base), sha256(split)), report.inputHashes.map { it.sha256 })
    }

    @Test
    fun linuxCheckerReportIncludesTheLoadedDexKitRuntimeIdentity() = withApk { apk ->
        val osName = System.getProperty("os.name")
        val architecture = System.getProperty("os.arch").lowercase()
        if (!osName.contains("linux", ignoreCase = true) || architecture !in setOf("x86_64", "amd64", "aarch64", "arm64")) {
            return@withApk
        }
        val runtime = DexKitRuntimeLoader.load(
            java.io.File(requireNotNull(System.getenv("DEXKIT_RUNTIME_DIR"))),
        )
        val report = RuleCheck(
            identityReader = { _, _ -> identity() },
            sessionOpener = { EmptySession },
            runtimeIdentityProvider = { runtime },
        ).check("qq", listOf(apk))
        val destination = java.io.File("build/test-evidence/runtime-identity-report.json")
        RuleCheckReportWriter.write(report, destination)
        val json = destination.readText()

        assertEquals(runtime.platform, report.executionIdentity.dexkitPlatform)
        assertEquals(runtime.nativeSha256, report.executionIdentity.dexkitNativeSha256)
        assertTrue(json.contains(runtime.nativeSha256))
        assertTrue(json.contains(runtime.sourceCommit))
    }

    private fun identity(
        packageName: String = "com.tencent.mobileqq",
        abi: String = "arm64-v8a",
        signerSha256: String = "ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38",
        apkSha256: String? = "a".repeat(64),
    ) = BuildIdentity(packageName, "9.3.1", 14378, abi, apkSha256, signerSha256)

    private fun withApk(block: (java.io.File) -> Unit) {
        val path = Files.createTempFile("rule-check", ".apk")
        try {
            Files.write(path, byteArrayOf(1, 2, 3))
            block(path.toFile())
        } finally {
            Files.deleteIfExists(path)
        }
    }

    private object EmptySession : RuleSession {
        override fun findMethods(query: MethodQuery): List<RuleMethod> = emptyList()
        override fun findClasses(typeName: String): List<RuleClass> = emptyList()
        override fun close() = Unit
    }

    private fun sha256(file: java.io.File): String = MessageDigest.getInstance("SHA-256")
        .digest(file.readBytes())
        .joinToString("") { "%02x".format(it) }

    private fun withSplitFixture(block: (java.io.File, java.io.File, java.io.File) -> Unit) {
        val directory = Files.createTempDirectory("rule-check-split")
        try {
            val sdkRoot = directory.resolve("sdk")
            Files.createDirectories(sdkRoot.resolve("cmdline-tools/latest/bin"))
            Files.createDirectories(sdkRoot.resolve("build-tools/37.0.0"))
            val base = directory.resolve("base.apk").toFile()
            val split = directory.resolve("config.arm64_v8a.apk").toFile()
            java.util.zip.ZipOutputStream(Files.newOutputStream(base.toPath())).use { it.finish() }
            java.util.zip.ZipOutputStream(Files.newOutputStream(split.toPath())).use { zip ->
                zip.putNextEntry(java.util.zip.ZipEntry("lib/arm64-v8a/libfixture.so"))
                zip.write(byteArrayOf(1, 2, 3))
                zip.closeEntry()
            }
            block(sdkRoot.toFile(), base, split)
        } finally {
            Files.walk(directory).use { paths ->
                paths.sorted(java.util.Comparator.reverseOrder<java.nio.file.Path>()).forEach(Files::deleteIfExists)
            }
        }
    }
}
