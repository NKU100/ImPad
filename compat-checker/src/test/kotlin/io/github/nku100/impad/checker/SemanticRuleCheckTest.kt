package io.github.nku100.impad.checker

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.immutable.ImmutableClassDef
import com.android.tools.smali.dexlib2.immutable.ImmutableDexFile
import com.android.tools.smali.dexlib2.writer.pool.DexPool
import io.github.nku100.impad.compat.BuildIdentity
import io.github.nku100.impad.compat.rules.HookPhase
import io.github.nku100.impad.compat.rules.RecognitionResult
import io.github.nku100.impad.rules.*
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SemanticRuleCheckTest {
    @Test fun `rejects duplicate canonical APK paths before identity inspection`() {
        val apk = apk()
        var inspected = false
        try {
            val alias = File(apk.parentFile, "./${apk.name}")
            val checker = SemanticRuleCheck(
                identityReader = { _, _ -> inspected = true; identity(apk) },
                sessionOpener = { error("must not open") },
            )
            assertFailsWith<IllegalArgumentException> { checker.check("wechat", listOf(apk, alias)) }
            assertTrue(!inspected)
        } finally { apk.delete() }
    }

    @Test fun `rejects incorrect official identity before opening a session`() {
        val apk = apk()
        var opened = false
        try {
            val checker = checker(apk, sessionOpener = { opened = true; error("must not open") },
                identity = identity(apk).copy(signerSha256 = "0".repeat(64)))
            assertFailsWith<IllegalArgumentException> { checker.check("wechat", listOf(apk)) }
            assertTrue(!opened)
        } finally { apk.delete() }
    }

    @Test fun `reports static snapshot graph and ordered archive hashes for no match`() {
        val base = apk()
        val split = apk()
        try {
            val checker = checker(base, sessionOpener = { snapshotSession(it) },
                query = { RecognitionResult.NoMatch() })
            val report = checker.check("wechat", listOf(base, split))
            assertEquals(2, report.schemaVersion)
            assertEquals("APK_FILE_SNAPSHOT", report.analysisScope)
            assertEquals(listOf(base, split).map(::sha256), report.inputHashes.map { it.sha256 })
            assertEquals("NO_MATCH", report.selection.status)
            assertEquals("loader-0", report.snapshot.readyLoaderId)
            assertEquals(listOf("loader-0"), report.snapshot.loaders.map { it.id })
            assertEquals(listOf(0, 1), report.snapshot.archives.map { it.containerIndex })
            assertEquals(listOf(sha256(base), sha256(split)), report.snapshot.archives.map { it.containerSha256 })
            assertEquals(report.snapshot.loaders.single().dexSha256.first(), report.snapshot.archives.first().dexSha256.single())
        } finally { base.delete(); split.delete() }
    }

    @Test fun `preserves a selected complete plan and recognizer evidence in wire output`() {
        val apk = apk()
        try {
            val checker = checker(apk, sessionOpener = { snapshotSession(it) }, query = { context ->
                val method = RuleMethod("Lfixture/Probe;->hit()V", "fixture.Probe", emptyList(), "void", true, 1, emptySet(), emptyList(), emptyList())
                RecognitionResult.Plans(listOf(context.plan("strategy", mapOf("entry" to method), listOf("anchor found"))))
            })
            val report = checker.check("wechat", listOf(apk))
            assertEquals("SELECTED", report.selection.status)
            assertNotNull(report.selection.plan)
            assertEquals(report.selection.plan.executionSha256(), report.selection.executionSha256)
            assertEquals(listOf("anchor found"), report.selection.evidenceByRecognizer.getValue("recognizer"))
            val json = Json.parseToJsonElement(semanticRuleCheckReportJson(report)).jsonObject
            assertEquals(2, json.getValue("schemaVersion").jsonPrimitive.content.toInt())
            assertTrue("snapshot" in json && "selection" in json && "ruleCatalog" in json)
        } finally { apk.delete() }
    }

    @Test fun `keeps discovered plans with incomplete selection and serializes nulls`() {
        val apk = apk()
        try {
            val checker = checker(apk, sessionOpener = { snapshotSession(it) }, query = { context ->
                val method = RuleMethod("Lfixture/Probe;->hit()V", "fixture.Probe", emptyList(), "void", true, 1, emptySet(), emptyList(), emptyList())
                val plan = context.plan("strategy", mapOf("entry" to method), listOf("partial evidence"))
                RecognitionResult.Incomplete("second pass unavailable", listOf(plan))
            })
            val report = checker.check("wechat", listOf(apk))
            assertEquals("INCOMPLETE", report.selection.status)
            assertNull(report.selection.plan)
            assertEquals(1, report.selection.alternatives.size)
            assertEquals("INCOMPLETE", report.recognizers.single().status)
            assertTrue(semanticRuleCheckReportJson(report).contains("\"plan\": null"))
        } finally { apk.delete() }
    }


    @Test fun `preserves conflicting complete plans as alternatives without selecting one`() {
        val apk = apk()
        try {
            val queries = listOf("first", "second").map { recognizerId -> object : SemanticRuleQuery {
                override val id = recognizerId
                override fun recognize(context: RecognitionContext): RecognitionResult {
                    val method = RuleMethod("Lfixture/Probe;->${if (recognizerId == "first") "hit" else "other"}()V",
                        "fixture.Probe", emptyList(), "void", true, 1, emptySet(), emptyList(), emptyList())
                    return RecognitionResult.Plans(listOf(context.plan("strategy", mapOf("entry" to method), listOf(recognizerId))))
                }
            } }
            val catalog = SemanticRuleCatalog("wechat", "test-contract", 1, setOf("entry"), queries,
                listOf(RuleStrategyContract("strategy", 1, listOf(HookRoleContract("entry", HookPhase.BEFORE)))))
            val report = SemanticRuleCheck(
                identityReader = { _, _ -> identity(apk) },
                sessionOpener = { snapshotSession(it) },
                catalogProvider = { catalog },
            ).check("wechat", listOf(apk))
            assertEquals("CONFLICT", report.selection.status)
            assertNull(report.selection.plan)
            assertEquals(2, report.selection.alternatives.size)
            assertEquals(setOf("first", "second"), report.selection.alternatives.flatMap { it.evidenceByRecognizer.keys }.toSet())
        } finally { apk.delete() }
    }

    @Test fun `rejects source changes after analysis and closes session and capture`() {
        val apk = apk()
        var sessionClosed = false
        var capture: CapturedCodeSnapshot? = null
        try {
            val checker = SemanticRuleCheck(
                identityReader = { _, _ -> identity(apk) },
                sessionOpener = { snapshot -> snapshotSession(snapshot).let { delegate ->
                    object : RuleSession by delegate { override fun close() { sessionClosed = true } }
                } },
                catalogProvider = { appId -> catalog(appId) { apk.appendBytes(byteArrayOf(1)); RecognitionResult.NoMatch() } },
                snapshotCapture = { ready, inputs -> StartupCodeSnapshot().capture(ready, inputs).also { capture = it } },
            )
            assertFailsWith<IllegalStateException> { checker.check("wechat", listOf(apk)) }
            assertTrue(sessionClosed)
            assertFailsWith<IllegalStateException> { capture!!.ensureOpen() }
        } finally { apk.delete() }
    }

    @Test fun `preserves evaluation errors in recognizer and selection output`() {
        val apk = apk()
        try {
            val report = checker(apk, sessionOpener = { snapshotSession(it) }, query = {
                RecognitionResult.EvaluationError(listOf("backend exploded"))
            }).check("wechat", listOf(apk))
            assertEquals("EVALUATION_ERROR", report.recognizers.single().status)
            assertEquals(listOf("backend exploded"), report.selection.diagnostics.getValue("recognizer"))
            assertEquals("EVALUATION_ERROR", report.selection.status)
        } finally { apk.delete() }
    }

    @Test fun `closes snapshot when session opening fails`() {
        val apk = apk()
        var captured: CapturedCodeSnapshot? = null
        try {
            val checker = checker(apk, snapshotCapture = { ready, inputs ->
                StartupCodeSnapshot().capture(ready, inputs).also { captured = it }
            }, sessionOpener = { error("open failed") })
            assertFailsWith<IllegalStateException> { checker.check("wechat", listOf(apk)) }
            assertFailsWith<IllegalStateException> { captured!!.ensureOpen() }
        } finally { apk.delete() }
    }

    private fun checker(
        apk: File,
        sessionOpener: (CapturedCodeSnapshot) -> RuleSession,
        identity: BuildIdentity = identity(apk),
        query: (RecognitionContext) -> RecognitionResult = { RecognitionResult.NoMatch() },
        snapshotCapture: (String, List<CodeLoaderInput>) -> CapturedCodeSnapshot = { ready, inputs -> StartupCodeSnapshot().capture(ready, inputs) },
    ) = SemanticRuleCheck(
        identityReader = { _, _ -> identity },
        sessionOpener = sessionOpener,
        catalogProvider = { appId -> catalog(appId, query) },
        snapshotCapture = snapshotCapture,
    )

    private fun catalog(appId: String, recognize: (RecognitionContext) -> RecognitionResult) = SemanticRuleCatalog(
        appId, "test-contract", 1, setOf("entry"), listOf(object : SemanticRuleQuery {
            override val id = "recognizer"
            override fun recognize(context: RecognitionContext) = recognize(context)
        }), listOf(RuleStrategyContract("strategy", 1, listOf(HookRoleContract("entry", HookPhase.BEFORE)))),
    )

    private fun identity(apk: File) = BuildIdentity("com.tencent.mm", "8.0.0", 1, "arm64-v8a", sha256(apk),
        "0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c")

    private fun snapshotSession(snapshot: CapturedCodeSnapshot): RuleSession = object : RuleSession {
        override fun findMethods(query: MethodQuery): List<RuleMethod> {
            val owner = query.declaredClass ?: "fixture.Probe"
            return listOf("hit", "other").map { name -> RuleMethod(
                "L${owner.replace('.', '/')};->$name()V", owner, emptyList(), "void", true, 1, emptySet(), emptyList(), emptyList()) }
        }
        override fun findClasses(typeName: String) = emptyList<RuleClass>()
        override fun snapshotContext() = RuleSessionSnapshotContext(snapshot.metadata.contentSha256(), "loader-0")
        override fun sourceOf(method: RuleMethod) = io.github.nku100.impad.compat.rules.TargetCodeSource(
            "loader-0", 0, snapshot.metadata.loaders.single().dexSha256.single())
        override fun inNamespace(loaderId: String): RuleSession = this
        override fun close() {}
    }

    private fun apk(): File {
        val file = Files.createTempFile("semantic-check", ".apk").toFile()
        val dex = Files.createTempFile("semantic-check", ".dex")
        try {
            val klass = ImmutableClassDef("Lfixture/Empty;", 1, "Ljava/lang/Object;", emptyList(), null,
                emptySet(), emptyList(), emptyList())
            DexPool.writeTo(dex.toString(), ImmutableDexFile(Opcodes.getDefault(), listOf(klass)))
            ZipOutputStream(file.outputStream()).use { zip -> zip.putNextEntry(ZipEntry("classes.dex")); Files.copy(dex, zip); zip.closeEntry() }
        } finally { Files.deleteIfExists(dex) }
        return file
    }

    private fun sha256(file: File) = MessageDigest.getInstance("SHA-256").digest(file.readBytes()).joinToString("") { "%02x".format(it) }
}
