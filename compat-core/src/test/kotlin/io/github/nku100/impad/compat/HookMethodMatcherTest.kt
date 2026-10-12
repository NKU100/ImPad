package io.github.nku100.impad.compat

import kotlin.test.Test
import kotlin.test.assertEquals

class HookMethodMatcherTest {
    @Test
    fun exactDescriptorLocatorResolvesOneDeclarationWithoutStringAnchors() {
        val rule = exactRule()
        val match = HookMethodMatcher.match(rule, listOf(method(strings = emptySet())))

        assertEquals(HookMethodMatchStatus.UNIQUE_MATCH, match.status)
        assertEquals(listOf(DESCRIPTOR), match.shapeMatchedMethods.map(DexMethodFact::descriptor))
    }

    @Test
    fun exactDescriptorLocatorRejectsMissingDeclaration() {
        val match = HookMethodMatcher.match(exactRule(), listOf(method(descriptor = OTHER_DESCRIPTOR)))

        assertEquals(HookMethodMatchStatus.MISSING_DESCRIPTOR, match.status)
    }

    @Test
    fun exactDescriptorLocatorRejectsDuplicateDeclarations() {
        val fact = method(strings = emptySet())
        val match = HookMethodMatcher.match(exactRule(), listOf(fact, fact))

        assertEquals(HookMethodMatchStatus.AMBIGUOUS_MATCH, match.status)
    }

    @Test
    fun exactDescriptorLocatorDoesNotFallBackToAnUnrelatedStringAnchor() {
        val rule = exactRule().copy(stringAnchor = "some-anchor")
        val match = HookMethodMatcher.match(
            rule,
            listOf(method(descriptor = OTHER_DESCRIPTOR, strings = setOf("some-anchor"))),
        )

        assertEquals(HookMethodMatchStatus.MISSING_DESCRIPTOR, match.status)
    }

    @Test
    fun stringAnchorLocatorKeepsItsExistingBehavior() {
        val rule = HookRule(
            id = "legacy",
            stringAnchor = "some-anchor",
            parameterDescriptors = emptyList(),
            returnDescriptor = "Z",
            expectedDescriptor = DESCRIPTOR,
        )
        val match = HookMethodMatcher.match(rule, listOf(method(strings = setOf("some-anchor"))))

        assertEquals(HookMethodMatchStatus.UNIQUE_MATCH, match.status)
    }

    private fun exactRule() = HookRule(
        id = "exact",
        stringAnchor = null,
        locator = HookLocator.EXACT_DESCRIPTOR,
        parameterDescriptors = emptyList(),
        returnDescriptor = "Z",
        expectedDescriptor = DESCRIPTOR,
    )

    private fun method(
        descriptor: String = DESCRIPTOR,
        strings: Set<String> = setOf("unrelated"),
    ) = DexMethodFact(
        descriptor = descriptor,
        parameterDescriptors = emptyList(),
        returnDescriptor = "Z",
        strings = strings,
    )

    private companion object {
        const val DESCRIPTOR = "Lfixture/Tablet;->isPad()Z"
        const val OTHER_DESCRIPTOR = "Lfixture/Tablet;->isPhone()Z"
    }
}
