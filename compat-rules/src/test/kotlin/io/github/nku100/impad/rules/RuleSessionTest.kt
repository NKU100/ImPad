package io.github.nku100.impad.rules

import io.github.nku100.impad.compat.rules.RuleResolution
import io.github.nku100.impad.rules.wecom.WeComLocalRule
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertTrue

class RuleSessionTest {
    @Test
    fun opensAllInputsOnceAndClosesEveryBridgeInReverseOrder() = withFiles(2) { files ->
        val opened = mutableListOf<String>()
        val closed = mutableListOf<String>()

        RuleSession.open(files) { file ->
            val id = "backend${opened.size + 1}"
            opened += id
            FakeBackend(id, onClose = { closed += id })
        }.use { session ->
            assertEquals(listOf("backend1", "backend2"), opened)
            assertEquals(2, session.findMethods(MethodQuery()).size)
        }

        assertEquals(listOf("backend2", "backend1"), closed)
    }

    @Test
    fun closesAlreadyOpenedBridgesWhenOpeningAnotherInputFails() = withFiles(2) { files ->
        val closed = mutableListOf<String>()
        var openCount = 0

        val error = assertFailsWith<IllegalStateException> {
            RuleSession.open(files) { file ->
                if (++openCount == 2) error("native open failed")
                val id = "backend$openCount"
                FakeBackend(id, onClose = { closed += id })
            }
        }

        assertEquals("native open failed", error.message)
        assertEquals(listOf("backend1"), closed)
    }

    @Test
    fun closeIsIdempotentAndQueriesAfterCloseAreRejected() = withFiles(1) { files ->
        val session = RuleSession.open(files) { FakeBackend(it.name, onClose = {}) }
        session.close()
        session.close()

        assertFailsWith<IllegalStateException> { session.findMethods(MethodQuery()) }
    }

    @Test
    fun queryFailureStillClosesEveryOpenedBackend() = withFiles(2) { files ->
        val closed = mutableListOf<String>()
        var openCount = 0
        val session = RuleSession.open(files) { file ->
            val id = "backend${++openCount}"
            FakeBackend(id, { closed += id }, failQuery = id == "backend2")
        }

        assertFailsWith<IllegalStateException> { session.use { it.findMethods(MethodQuery()) } }

        assertEquals(listOf("backend2", "backend1"), closed)
    }

    @Test
    fun repeatedTargetAcrossDexEntriesMakesTheWholeWecomRuleAmbiguous() = withMultiDexFile(2) { apk ->
        var dexIndex = 0
        val session = RuleSession.open(listOf(apk)) {
            val currentDex = dexIndex++
            object : RuleSessionBackend {
                override fun findMethods(query: MethodQuery): List<RuleMethod> = when {
                    query.declaredClass == APPLICATION && query.returnType == "int" -> listOf(localDeviceType())
                    currentDex == 0 && query.declaredClass == APPLICATION && query.returnType == "boolean" ->
                        listOf(applicationPad())
                    currentDex == 0 && query.declaredClass == SERVICE && query.returnType == "boolean" ->
                        listOf(servicePad())
                    else -> emptyList()
                }

                override fun findClasses(typeName: String): List<RuleClass> = emptyList()
                override fun close() = Unit
            }
        }

        val result = session.use { WeComLocalRule.resolve(it) }

        val ambiguous = assertIs<RuleResolution.Ambiguous>(result)
        assertEquals(mapOf("device_type" to 2, "app_pad" to 1, "service_pad" to 1), ambiguous.candidateCounts)
    }

    private fun withFiles(count: Int, block: (List<java.io.File>) -> Unit) {
        val directory = Files.createTempDirectory("rule-session")
        try {
            val files = (1..count).map { index ->
                directory.resolve(if (index == 1) "one.apk" else "two.apk").toFile().apply {
                    ZipOutputStream(outputStream()).use { zip ->
                        zip.putNextEntry(ZipEntry("classes.dex"))
                        zip.write(byteArrayOf(index.toByte()))
                        zip.closeEntry()
                    }
                }
            }
            block(files)
        } finally {
            Files.walk(directory).use { paths -> paths.sorted(Comparator.reverseOrder()).forEach(Files::deleteIfExists) }
        }
    }

    private fun withMultiDexFile(count: Int, block: (java.io.File) -> Unit) {
        val directory = Files.createTempDirectory("rule-session-multidex")
        try {
            val apk = directory.resolve("multi.apk").toFile()
            ZipOutputStream(apk.outputStream()).use { zip ->
                repeat(count) { index ->
                    zip.putNextEntry(ZipEntry(if (index == 0) "classes.dex" else "classes${index + 1}.dex"))
                    zip.write(byteArrayOf(index.toByte()))
                    zip.closeEntry()
                }
            }
            block(apk)
        } finally {
            Files.walk(directory).use { paths -> paths.sorted(Comparator.reverseOrder()).forEach(Files::deleteIfExists) }
        }
    }

    private fun localDeviceType() = RuleMethod(
        descriptor = "$APPLICATION;->getDeviceTypeFromLocal()I",
        className = APPLICATION,
        parameterTypes = emptyList(),
        returnType = "int",
        isStatic = false,
        codeUnitCount = 3,
        strings = emptySet(),
        fields = emptyList(),
        invokes = listOf(RuleMethodReference(APPLICATION, "nativeGetDeviceTypeFromLocal", emptyList(), "int")),
    )

    private fun applicationPad() = RuleMethod(
        descriptor = "$APPLICATION;->isAndroidPad()Z",
        className = APPLICATION,
        parameterTypes = emptyList(),
        returnType = "boolean",
        isStatic = true,
        codeUnitCount = 6,
        strings = emptySet(),
        fields = listOf(RuleFieldReference(APPLICATION, "mApplicationApi", SERVICE_API,
            "Lcom/tencent/wework/foundation/logic/Application;->mApplicationApi:L$SERVICE_API;", true)),
        invokes = listOf(RuleMethodReference(SERVICE_API, "isAndroidPad", emptyList(), "boolean")),
    )

    private fun servicePad() = RuleMethod(
        descriptor = "$SERVICE;->isAndroidPad()Z",
        className = SERVICE,
        parameterTypes = emptyList(),
        returnType = "boolean",
        isStatic = false,
        codeUnitCount = 10,
        strings = emptySet(),
        fields = emptyList(),
        invokes = listOf(
            RuleMethodReference("wdz", "f", emptyList(), "wdz"),
            RuleMethodReference("wdz", "f", emptyList(), "wdz"),
            RuleMethodReference("wdz", "l", emptyList(), "boolean"),
        ),
    )

    private companion object {
        const val APPLICATION = "com.tencent.wework.foundation.logic.Application"
        const val SERVICE_API = "com.tencent.wework.foundation.logic.IWeworkServiceApi"
        const val SERVICE = "com.tencent.wework.foundation.impl.WeworkServiceImpl"
    }

    private class FakeBackend(
        name: String,
        private val onClose: () -> Unit,
        private val failQuery: Boolean = false,
    ) : RuleSessionBackend {
        private val method = RuleMethod(
            descriptor = "L$name;->run()V",
            className = name,
            parameterTypes = emptyList(),
            returnType = "void",
            isStatic = true,
            codeUnitCount = 1,
            strings = emptySet(),
            fields = emptyList(),
            invokes = emptyList(),
        )

        override fun findMethods(query: MethodQuery): List<RuleMethod> {
            check(!failQuery) { "fixture query failed" }
            return listOf(method)
        }
        override fun findClasses(typeName: String): List<RuleClass> = emptyList()
        override fun close() = onClose()
    }
}
