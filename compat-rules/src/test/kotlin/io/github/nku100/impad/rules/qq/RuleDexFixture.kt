package io.github.nku100.impad.rules.qq

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.Opcode
import com.android.tools.smali.dexlib2.immutable.ImmutableClassDef
import com.android.tools.smali.dexlib2.immutable.ImmutableDexFile
import com.android.tools.smali.dexlib2.immutable.ImmutableField
import com.android.tools.smali.dexlib2.immutable.ImmutableMethod
import com.android.tools.smali.dexlib2.immutable.ImmutableMethodImplementation
import com.android.tools.smali.dexlib2.immutable.ImmutableMethodParameter
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction10x
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction11x
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction21c
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction35c
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableFieldReference
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableMethodReference
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableStringReference
import com.android.tools.smali.dexlib2.writer.pool.DexPool
import java.io.File
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

internal object RuleDexFixture {
    private const val CONTEXT = "Landroid/content/Context;"
    private const val DEVICE_TYPE = "Lfixture/DeviceType;"
    private const val ATOMIC_BOOLEAN = "Ljava/util/concurrent/atomic/AtomicBoolean;"

    fun apk(
        definitions: List<String> = listOf("Lfixture/TabletProbe;"),
        duplicateDefinitionAcrossDex: Boolean = false,
        includeAnchor: Boolean = true,
        includeReferenceOnlyCaller: Boolean = false,
        includeEnum: Boolean = true,
    ): File {
        val path = Files.createTempFile("qq-rule-fixture", ".apk").toFile()
        val dexClasses = mutableListOf<List<ImmutableClassDef>>()
        if (includeEnum) dexClasses += listOf(enumClass())

        val methodDefinitions = definitions.map { owner -> tabletProbe(owner, includeAnchor) }
        if (duplicateDefinitionAcrossDex) {
            require(methodDefinitions.size == 1)
            dexClasses += listOf(methodDefinitions.single())
            dexClasses += listOf(methodDefinitions.single())
        } else {
            dexClasses += methodDefinitions
        }
        if (includeReferenceOnlyCaller) dexClasses += listOf(referenceOnlyCaller())

        ZipOutputStream(path.outputStream().buffered()).use { zip ->
            dexClasses.forEachIndexed { index, classes ->
                val entryName = if (index == 0) "classes.dex" else "classes${index + 1}.dex"
                val dex = Files.createTempFile("qq-rule-fixture", ".dex")
                try {
                    DexPool.writeTo(dex.toString(), ImmutableDexFile(Opcodes.getDefault(), classes))
                    zip.putNextEntry(ZipEntry(entryName))
                    Files.copy(dex, zip)
                    zip.closeEntry()
                } finally {
                    Files.deleteIfExists(dex)
                }
            }
        }
        return path
    }

    private fun enumClass() = ImmutableClassDef(
        DEVICE_TYPE,
        ACC_PUBLIC or ACC_FINAL or ACC_ENUM,
        "Ljava/lang/Enum;",
        emptyList(),
        null,
        emptySet(),
        listOf(ImmutableField(DEVICE_TYPE, "TABLET", DEVICE_TYPE, ACC_PUBLIC or ACC_STATIC or ACC_FINAL or ACC_ENUM, null, emptySet(), emptySet())),
        emptyList(),
    )

    private fun tabletProbe(owner: String, includeAnchor: Boolean): ImmutableClassDef {
        val fields = listOf(
            ImmutableField(owner, "deviceType", DEVICE_TYPE, ACC_PRIVATE or ACC_STATIC, null, emptySet(), emptySet()),
            ImmutableField(owner, "enabled", ATOMIC_BOOLEAN, ACC_PRIVATE or ACC_STATIC, null, emptySet(), emptySet()),
        )
        val instructions = listOf(
            ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference("PadUtil")),
            ImmutableInstruction21c(
                Opcode.CONST_STRING,
                0,
                ImmutableStringReference(if (includeAnchor) "context is null" else "different anchor"),
            ),
            ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, ImmutableFieldReference(owner, "deviceType", DEVICE_TYPE)),
            ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, ImmutableFieldReference(owner, "enabled", ATOMIC_BOOLEAN)),
            ImmutableInstruction35c(
                Opcode.INVOKE_STATIC,
                1,
                2,
                0,
                0,
                0,
                0,
                ImmutableMethodReference(owner, "refresh", listOf(CONTEXT), "V"),
            ),
            ImmutableInstruction11x(Opcode.RETURN_OBJECT, 0),
        )
        return ImmutableClassDef(
            owner,
            ACC_PUBLIC,
            "Ljava/lang/Object;",
            emptyList(),
            null,
            emptySet(),
            fields,
            listOf(
                ImmutableMethod(
                    owner,
                    "deviceType",
                    listOf(ImmutableMethodParameter(CONTEXT, emptySet(), null)),
                    DEVICE_TYPE,
                    ACC_PUBLIC or ACC_STATIC,
                    emptySet(),
                    emptySet(),
                    ImmutableMethodImplementation(3, instructions, emptyList(), emptyList()),
                ),
                ImmutableMethod(
                    owner,
                    "refresh",
                    listOf(ImmutableMethodParameter(CONTEXT, emptySet(), null)),
                    "V",
                    ACC_PRIVATE or ACC_STATIC,
                    emptySet(),
                    emptySet(),
                    ImmutableMethodImplementation(1, listOf(ImmutableInstruction10x(Opcode.RETURN_VOID)), emptyList(), emptyList()),
                ),
            ),
        )
    }

    private fun referenceOnlyCaller(): ImmutableClassDef {
        val owner = "Lfixture/Caller;"
        return ImmutableClassDef(
            owner,
            ACC_PUBLIC,
            "Ljava/lang/Object;",
            emptyList(),
            null,
            emptySet(),
            emptyList(),
            listOf(
                ImmutableMethod(
                    owner,
                    "forward",
                    listOf(ImmutableMethodParameter(CONTEXT, emptySet(), null)),
                    DEVICE_TYPE,
                    ACC_PUBLIC or ACC_STATIC,
                    emptySet(),
                    emptySet(),
                    ImmutableMethodImplementation(
                        1,
                        listOf(
                            ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference("PadUtil")),
                            ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference("context is null")),
                            ImmutableInstruction35c(
                                Opcode.INVOKE_STATIC,
                                1,
                                0,
                                0,
                                0,
                                0,
                                0,
                                ImmutableMethodReference("Lfixture/TabletProbe;", "deviceType", listOf(CONTEXT), DEVICE_TYPE),
                            ),
                            ImmutableInstruction11x(Opcode.RETURN_OBJECT, 0),
                        ),
                        emptyList(),
                        emptyList(),
                    ),
                ),
            ),
        )
    }

    private const val ACC_PUBLIC = 0x0001
    private const val ACC_PRIVATE = 0x0002
    private const val ACC_STATIC = 0x0008
    private const val ACC_FINAL = 0x0010
    private const val ACC_ENUM = 0x4000
}
