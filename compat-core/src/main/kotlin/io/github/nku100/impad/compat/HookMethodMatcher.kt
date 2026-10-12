package io.github.nku100.impad.compat

enum class HookMethodMatchStatus {
    MISSING_ANCHOR,
    MISSING_DESCRIPTOR,
    SIGNATURE_MISMATCH,
    AMBIGUOUS_MATCH,
    UNIQUE_MATCH,
}

data class HookMethodMatch(
    val status: HookMethodMatchStatus,
    val anchoredMethods: List<DexMethodFact>,
    val shapeMatchedMethods: List<DexMethodFact>,
)

object HookMethodMatcher {
    fun match(rule: HookRule, facts: List<DexMethodFact>): HookMethodMatch {
        val anchoredMethods = when (rule.locator) {
            HookLocator.STRING_ANCHOR -> facts.filter { rule.stringAnchor in it.strings }
            HookLocator.EXACT_DESCRIPTOR -> facts.filter { it.descriptor == rule.expectedDescriptor }
        }
        if (anchoredMethods.isEmpty()) {
            val status = when (rule.locator) {
                HookLocator.STRING_ANCHOR -> HookMethodMatchStatus.MISSING_ANCHOR
                HookLocator.EXACT_DESCRIPTOR -> HookMethodMatchStatus.MISSING_DESCRIPTOR
            }
            return HookMethodMatch(status, anchoredMethods, emptyList())
        }

        val shapeMatchedMethods = anchoredMethods.filter {
            it.parameterDescriptors == rule.parameterDescriptors && it.returnDescriptor == rule.returnDescriptor
        }
        val status = when {
            shapeMatchedMethods.isEmpty() -> HookMethodMatchStatus.SIGNATURE_MISMATCH
            shapeMatchedMethods.size > 1 -> HookMethodMatchStatus.AMBIGUOUS_MATCH
            else -> HookMethodMatchStatus.UNIQUE_MATCH
        }
        return HookMethodMatch(status, anchoredMethods, shapeMatchedMethods)
    }
}
