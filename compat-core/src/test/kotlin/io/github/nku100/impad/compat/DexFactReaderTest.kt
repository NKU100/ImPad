package io.github.nku100.impad.compat

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class DexFactReaderTest {
    @Test
    fun scansTheExactMethodDescriptorWithoutAnyStringAnchor() {
        val rule = HookRule(
            id = "device_type",
            locator = HookLocator.EXACT_DESCRIPTOR,
            parameterDescriptors = emptyList(),
            returnDescriptor = "Ljava/lang/String;",
            expectedDescriptor = "Lfixture/PrimaryDex;->tablet()Ljava/lang/String;",
        )

        val fact = DexFactReader.scan(
            apkFiles = listOf(File("src/test/resources/multidex-fixture.apk")),
            rules = listOf(rule),
        ).single()

        assertEquals(rule.expectedDescriptor, fact.descriptor)
        assertTrue(fact.strings.isEmpty())
    }

    @Test
    fun scansMatchingMethodsAcrossEveryDexInTheApk() {
        val facts = DexFactReader.scan(
            apkFiles = listOf(File("src/test/resources/multidex-fixture.apk")),
            stringAnchors = setOf(TABLET_ANCHOR, LOGIN_ANCHOR),
        )

        assertEquals(2, facts.size)
        assertEquals(
            setOf(
                "Lfixture/PrimaryDex;->tablet()Ljava/lang/String;",
                "Lfixture/SecondaryDex;->login()Ljava/lang/String;",
            ),
            facts.map(DexMethodFact::descriptor).toSet(),
        )
        assertTrue(facts.any { it.strings.contains(TABLET_ANCHOR) })
        assertTrue(facts.any { it.strings.contains(LOGIN_ANCHOR) })
        assertTrue(facts.none { "unrelated-literal" in it.strings })
    }

    @Test
    fun retainsTheMethodParametersAndReturnDescriptor() {
        val fact = DexFactReader.scan(
            apkFiles = listOf(File("src/test/resources/multidex-fixture.apk")),
            stringAnchors = setOf(TABLET_ANCHOR),
        ).single()

        assertEquals(emptyList(), fact.parameterDescriptors)
        assertEquals("Ljava/lang/String;", fact.returnDescriptor)
    }

    @Test
    fun ignoresNonStringReferencesWhileScanningCodeInstructions() {
        val facts = DexFactReader.scan(
            apkFiles = listOf(File("src/test/resources/multidex-fixture.apk")),
            stringAnchors = setOf(TABLET_ANCHOR),
        )

        assertEquals("Lfixture/PrimaryDex;->tablet()Ljava/lang/String;", facts.single().descriptor)
    }

    private companion object {
        const val TABLET_ANCHOR = "tablet-anchor"
        const val LOGIN_ANCHOR = "login-anchor"
    }
}
