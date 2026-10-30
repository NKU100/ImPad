package io.github.nku100.impad.rules

import org.luckypray.dexkit.DexKitBridge
import org.luckypray.dexkit.annotations.DexKitExperimentalApi
import org.luckypray.dexkit.query.enums.StringMatchType
import org.luckypray.dexkit.query.matchers.ClassMatcher
import org.luckypray.dexkit.query.matchers.MethodMatcher
import org.luckypray.dexkit.result.FieldUsingType
import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.dexbacked.DexBackedDexFile
import com.android.tools.smali.dexlib2.iface.instruction.FiveRegisterInstruction
import com.android.tools.smali.dexlib2.iface.instruction.NarrowLiteralInstruction
import com.android.tools.smali.dexlib2.iface.instruction.OffsetInstruction
import com.android.tools.smali.dexlib2.iface.instruction.OneRegisterInstruction
import com.android.tools.smali.dexlib2.iface.instruction.ReferenceInstruction
import com.android.tools.smali.dexlib2.iface.instruction.RegisterRangeInstruction
import com.android.tools.smali.dexlib2.iface.instruction.TwoRegisterInstruction
import com.android.tools.smali.dexlib2.iface.reference.MethodReference
import com.android.tools.smali.dexlib2.iface.reference.FieldReference
import com.android.tools.smali.dexlib2.iface.reference.TypeReference
import java.io.File
import java.lang.reflect.Modifier
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream

data class MethodQuery(
    val requiredStrings: Set<String> = emptySet(),
    val parameterTypes: List<String>? = null,
    val staticOnly: Boolean = false,
    val declaredClass: String? = null,
    val returnType: String? = null,
)

data class RuleFieldReference(
    val className: String,
    val name: String,
    val typeName: String,
    val descriptor: String,
    val isRead: Boolean,
)

data class RuleMethodReference(
    val className: String,
    val name: String,
    val parameterTypes: List<String>,
    val returnType: String,
)

data class RuleNumericLiteral(val value: Int, val opcode: Int)

data class RuleInstructionReference(
    val owner: String,
    val name: String,
    val parameterTypes: List<String>,
    val returnType: String,
)

data class RuleInstruction(
    val codeAddress: Int,
    val opcode: String,
    val registerA: Int? = null,
    val registerB: Int? = null,
    val invokeRegisters: List<Int> = emptyList(),
    val literal: Int? = null,
    val branchTarget: Int? = null,
    val methodReference: RuleInstructionReference? = null,
    val typeReference: String? = null,
    val fieldReference: String? = null,
)

data class RuleMethod(
    val descriptor: String,
    val className: String,
    val parameterTypes: List<String>,
    val returnType: String,
    val isStatic: Boolean,
    val codeUnitCount: Int,
    val strings: Set<String>,
    val fields: List<RuleFieldReference>,
    val invokes: List<RuleMethodReference>,
    val opNames: List<String> = emptyList(),
    val numericLiterals: List<RuleNumericLiteral> = emptyList(),
    internal val sourceDexIndex: Int = 0,
    internal val sourceIdentity: Any? = null,
)

data class RuleClassField(val name: String, val accessFlags: Int)

data class RuleClass(
    val className: String,
    val superClassName: String?,
    val fields: List<RuleClassField>,
)

data class RuleSessionSnapshotContext(val snapshotSha256: String, val namespaceId: String)

interface RuleSession : AutoCloseable {
    fun findMethods(query: MethodQuery): List<RuleMethod>
    fun findClasses(typeName: String): List<RuleClass>
    fun readInstructions(method: RuleMethod): List<RuleInstruction> = emptyList()
    fun snapshotContext(): RuleSessionSnapshotContext? = null
    fun sourceOf(method: RuleMethod): io.github.nku100.impad.compat.rules.TargetCodeSource =
        throw UnsupportedOperationException("This session has no startup code snapshot")
    fun inContextOf(method: RuleMethod): RuleSession =
        throw UnsupportedOperationException("This session has no loader namespace model")
    fun inNamespace(loaderId: String): RuleSession =
        throw UnsupportedOperationException("This session has no loader namespace model")

    companion object {
        fun open(apkFiles: List<File>): RuleSession = open(apkFiles, DexKitSessionBackend::open)

        internal fun open(apkFiles: List<File>, backendFactory: (File) -> RuleSessionBackend): RuleSession {
            require(apkFiles.isNotEmpty()) { "At least one APK input is required" }
            require(apkFiles.all(File::isFile)) { "An APK input does not exist" }
            val dexApks = mutableListOf<File>()
            val opened = mutableListOf<RuleSessionBackend>()
            try {
                apkFiles.forEach { apk -> dexApks += extractDexApks(apk.absoluteFile) }
                require(dexApks.isNotEmpty()) { "APK inputs contain no DEX files" }
                dexApks.forEach { opened += backendFactory(it) }
                return SharedDexKitRuleSession(opened.toList(), dexApks.toList())
            } catch (failure: Throwable) {
                closeBackends(opened, failure)
                deleteDexApks(dexApks, failure)
                throw failure
            }
        }
    }
}

internal interface RuleSessionBackend : AutoCloseable {
    fun findMethods(query: MethodQuery): List<RuleMethod>
    fun findClasses(typeName: String): List<RuleClass>
    fun readInstructions(descriptor: String): List<RuleInstruction> = emptyList()
}

private class SharedDexKitRuleSession(
    private val backends: List<RuleSessionBackend>,
    private val dexApks: List<File>,
) : RuleSession {
    private var closed = false

    override fun findMethods(query: MethodQuery): List<RuleMethod> {
        check(!closed) { "Rule session is closed" }
        return backends.flatMapIndexed { index, backend ->
            backend.findMethods(query).map { it.copy(sourceDexIndex = index) }
        }
    }

    override fun readInstructions(method: RuleMethod): List<RuleInstruction> {
        check(!closed) { "Rule session is closed" }
        return backends.getOrNull(method.sourceDexIndex)?.readInstructions(method.descriptor).orEmpty()
    }

    override fun findClasses(typeName: String): List<RuleClass> {
        check(!closed) { "Rule session is closed" }
        return backends.flatMap { it.findClasses(typeName) }
    }

    override fun close() {
        if (closed) return
        closed = true
        var closeFailure: Throwable? = null
        try {
            closeBackends(backends, null)
        } catch (failure: Throwable) {
            closeFailure = failure
        }
        deleteDexApks(dexApks, closeFailure)
        closeFailure?.let { throw it }
    }
}

private fun extractDexApks(apkFile: File): List<File> {
    val extracted = mutableListOf<File>()
    try {
        ZipFile(apkFile).use { apk ->
            val dexEntries = apk.entries().asSequence()
                .filter { !it.isDirectory && DEX_ENTRY.matches(it.name) }
                .toList()
            dexEntries.forEach { entry ->
                val dexApk = Files.createTempFile("impad-rule-dex-", ".apk").toFile()
                extracted += dexApk
                ZipOutputStream(dexApk.outputStream().buffered()).use { zip ->
                    zip.putNextEntry(ZipEntry("classes.dex"))
                    apk.getInputStream(entry).use { it.copyTo(zip) }
                    zip.closeEntry()
                }
            }
        }
        return extracted
    } catch (failure: Throwable) {
        deleteDexApks(extracted, failure)
        throw failure
    }
}

private fun deleteDexApks(files: List<File>, primaryFailure: Throwable?) {
    var deleteFailure: Throwable? = null
    files.asReversed().forEach { file ->
        try {
            Files.deleteIfExists(file.toPath())
        } catch (failure: Throwable) {
            if (primaryFailure != null) primaryFailure.addSuppressed(failure)
            else if (deleteFailure == null) deleteFailure = failure
            else deleteFailure.addSuppressed(failure)
        }
    }
    if (primaryFailure == null && deleteFailure != null) throw deleteFailure
}

// DexKit coalesces repeated class descriptors within one bridge, so query each DEX independently.
private val DEX_ENTRY = Regex("classes(?:[2-9]|[1-9][0-9]+)?\\.dex")

@OptIn(DexKitExperimentalApi::class)
internal class DexKitSessionBackend private constructor(
    private val bridge: DexKitBridge,
    private val dexApk: File,
) : RuleSessionBackend {
    override fun findMethods(query: MethodQuery): List<RuleMethod> {
        val methods = bridge.findMethod {
            matcher(MethodMatcher.create().apply {
                if (query.staticOnly) modifiers(Modifier.STATIC)
                query.declaredClass?.let { declaredClass(it, StringMatchType.Equals) }
                query.returnType?.let { returnType(it, StringMatchType.Equals) }
                query.parameterTypes?.let { paramTypes(*it.toTypedArray()) }
                if (query.requiredStrings.isNotEmpty()) {
                    usingStrings(query.requiredStrings.toList(), StringMatchType.Equals)
                }
            })
        }
        return methods.map { method ->
            RuleMethod(
                descriptor = method.descriptor,
                className = method.className,
                parameterTypes = method.paramTypeNames.toList(),
                returnType = method.returnTypeName,
                isStatic = Modifier.isStatic(method.modifiers),
                codeUnitCount = method.opCodes.size,
                strings = method.usingStrings.toSet(),
                fields = method.usingFields.map { using ->
                    RuleFieldReference(
                        className = using.field.className,
                        name = using.field.name,
                        typeName = using.field.typeName,
                        descriptor = using.field.descriptor,
                        isRead = using.usingType == FieldUsingType.Read,
                    )
                },
                invokes = method.invokes.map { invoked ->
                    RuleMethodReference(
                        className = invoked.className,
                        name = invoked.methodName,
                        parameterTypes = invoked.paramTypeNames.toList(),
                        returnType = invoked.returnTypeName,
                    )
                },
                opNames = method.opNames.toList(),
                numericLiterals = method.usingNumbers
                    .filter { it.bitWidth == Int.SIZE_BITS }
                    .map { RuleNumericLiteral(it.intValue(), it.opCode) },
            )
        }
    }

    override fun findClasses(typeName: String): List<RuleClass> = bridge.findClass {
        matcher(ClassMatcher.create().className(typeName, StringMatchType.Equals))
    }.map { type ->
        RuleClass(
            className = type.name,
            superClassName = type.superClass?.name,
            fields = type.fields.map { field -> RuleClassField(field.name, field.accessFlags) },
        )
    }

    override fun readInstructions(descriptor: String): List<RuleInstruction> =
        readDexMethodInstructions(dexApk, descriptor)

    override fun close() = bridge.close()

    companion object {
        fun open(apkFile: File): RuleSessionBackend =
            DexKitSessionBackend(DexKitBridge.create(apkFile.absolutePath), apkFile)
    }
}

private fun readDexMethodInstructions(dexApk: File, descriptor: String): List<RuleInstruction> {
    val dexFile = ZipFile(dexApk).use { apk ->
        val entry = apk.getEntry("classes.dex") ?: return emptyList()
        apk.getInputStream(entry).buffered().use { DexBackedDexFile.fromInputStream(Opcodes.getDefault(), it) }
    }
    val owner = descriptor.substringBefore("->")
    val method = dexFile.classes.asSequence()
        .firstOrNull { it.type == owner }
        ?.methods
        ?.singleOrNull { it.toString() == descriptor }
        ?: return emptyList()
    val implementation = method.implementation ?: return emptyList()
    var address = 0
    return implementation.instructions.map { instruction ->
        val reference = (instruction as? ReferenceInstruction)?.reference as? MethodReference
        val invokeRegisters = when (instruction) {
            is FiveRegisterInstruction -> listOf(
                instruction.registerC, instruction.registerD, instruction.registerE,
                instruction.registerF, instruction.registerG,
            ).take(instruction.registerCount)
            is RegisterRangeInstruction ->
                (instruction.startRegister until instruction.startRegister + instruction.registerCount).toList()
            else -> emptyList()
        }
        val result = RuleInstruction(
            codeAddress = address,
            opcode = instruction.opcode.name.uppercase().replace('-', '_').replace('/', '_'),
            registerA = (instruction as? OneRegisterInstruction)?.registerA,
            registerB = (instruction as? TwoRegisterInstruction)?.registerB,
            invokeRegisters = invokeRegisters,
            literal = (instruction as? NarrowLiteralInstruction)?.narrowLiteral,
            branchTarget = (instruction as? OffsetInstruction)?.let { address + it.codeOffset },
            methodReference = reference?.let {
                RuleInstructionReference(
                    owner = descriptorTypeName(it.definingClass),
                    name = it.name,
                    parameterTypes = it.parameterTypes.map { type -> descriptorTypeName(type.toString()) },
                    returnType = descriptorTypeName(it.returnType),
                )
            },
            typeReference = ((instruction as? ReferenceInstruction)?.reference as? TypeReference)?.type,
            fieldReference = ((instruction as? ReferenceInstruction)?.reference as? FieldReference)?.toString(),
        )
        address += instruction.codeUnits
        result
    }
}

private fun descriptorTypeName(descriptor: String): String = when (descriptor) {
    "V" -> "void"
    "Z" -> "boolean"
    "B" -> "byte"
    "C" -> "char"
    "S" -> "short"
    "I" -> "int"
    "J" -> "long"
    "F" -> "float"
    "D" -> "double"
    else -> if (descriptor.startsWith("L") && descriptor.endsWith(";")) {
        descriptor.substring(1, descriptor.length - 1).replace('/', '.')
    } else {
        descriptor
    }
}

private fun closeBackends(backends: List<RuleSessionBackend>, primaryFailure: Throwable?) {
    var closeFailure: Throwable? = null
    backends.asReversed().forEach { backend ->
        try {
            backend.close()
        } catch (failure: Throwable) {
            if (primaryFailure != null) primaryFailure.addSuppressed(failure)
            else if (closeFailure == null) closeFailure = failure
            else closeFailure.addSuppressed(failure)
        }
    }
    if (primaryFailure == null && closeFailure != null) throw closeFailure
}
