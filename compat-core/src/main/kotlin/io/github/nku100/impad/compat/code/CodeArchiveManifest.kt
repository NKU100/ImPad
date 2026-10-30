package io.github.nku100.impad.compat.code

enum class CodeContainerKind { ZIP, DEX }

class CodeArchiveManifest(
    val loaderId: String,
    val containerIndex: Int,
    val kind: CodeContainerKind,
    val containerSha256: String,
    dexSha256: List<String>,
) {
    val dexSha256: List<String> = java.util.Collections.unmodifiableList(dexSha256.toList())

    init {
        require(loaderId.isNotBlank())
        require(containerIndex >= 0)
        require(containerSha256.matches(SHA256))
        require(this.dexSha256.all { it.matches(SHA256) })
        if (kind == CodeContainerKind.DEX) {
            require(this.dexSha256.size == 1 && this.dexSha256.single().equals(containerSha256, ignoreCase = true))
        }
    }

    private companion object {
        val SHA256 = Regex("[0-9a-fA-F]{64}")
    }
}
