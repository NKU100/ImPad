package io.github.nku100.impad.compat

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.dexbacked.DexBackedDexFile
import com.android.tools.smali.dexlib2.iface.Method
import com.android.tools.smali.dexlib2.iface.instruction.ReferenceInstruction
import com.android.tools.smali.dexlib2.iface.reference.StringReference
import java.io.BufferedInputStream
import java.io.File
import java.util.zip.ZipFile

object DexFactReader {
    private val dexEntryPattern = Regex("classes(\\d*)\\.dex")

    fun scan(apkFiles: List<File>, stringAnchors: Set<String>): List<DexMethodFact> {
        return scan(apkFiles, stringAnchors, emptySet())
    }

    fun scan(apkFiles: List<File>, rules: List<HookRule>): List<DexMethodFact> = scan(
        apkFiles = apkFiles,
        stringAnchors = rules.mapNotNullTo(linkedSetOf()) { rule ->
            rule.stringAnchor.takeIf { rule.locator == HookLocator.STRING_ANCHOR }
        },
        exactDescriptors = rules.filter { it.locator == HookLocator.EXACT_DESCRIPTOR }
            .mapTo(linkedSetOf(), HookRule::expectedDescriptor),
    )

    private fun scan(
        apkFiles: List<File>,
        stringAnchors: Set<String>,
        exactDescriptors: Set<String>,
    ): List<DexMethodFact> {
        if (stringAnchors.isEmpty() && exactDescriptors.isEmpty()) return emptyList()

        return buildList {
            for (apkFile in apkFiles) {
                ZipFile(apkFile).use { apk ->
                    val dexEntries = apk.entries().asSequence()
                        .filter { !it.isDirectory && dexEntryPattern.matches(it.name) }
                        .sortedBy { dexIndex(it.name) }
                        .toList()

                    for (entry in dexEntries) {
                        BufferedInputStream(apk.getInputStream(entry)).use { dexInput ->
                            val dex = DexBackedDexFile.fromInputStream(Opcodes.getDefault(), dexInput)
                            for (classDefinition in dex.classes) {
                                for (method in classDefinition.methods) {
                                    val descriptor = method.toDescriptor()
                                    val methodAnchors = if (stringAnchors.isEmpty()) {
                                        emptySet()
                                    } else {
                                        method.implementation?.let { implementation ->
                                        buildSet {
                                            for (instruction in implementation.instructions) {
                                                val reference = (instruction as? ReferenceInstruction)?.reference
                                                val string = (reference as? StringReference)?.string
                                                if (string != null && string in stringAnchors) add(string)
                                            }
                                        }
                                        }.orEmpty()
                                    }
                                    if (methodAnchors.isNotEmpty() || descriptor in exactDescriptors) {
                                        add(method.toFact(methodAnchors))
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private fun dexIndex(entryName: String): Int =
        dexEntryPattern.matchEntire(entryName)?.groupValues?.get(1)?.toIntOrNull() ?: 1

    private fun Method.toFact(strings: Set<String>): DexMethodFact = DexMethodFact(
        descriptor = toDescriptor(),
        parameterDescriptors = parameterTypes.map(CharSequence::toString),
        returnDescriptor = returnType,
        strings = strings,
    )

    private fun Method.toDescriptor(): String = buildString {
        append(definingClass)
        append("->")
        append(name)
        append('(')
        parameterTypes.forEach { append(it) }
        append(')')
        append(returnType)
    }
}
