package io.github.nku100.impad.rules.wechat

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
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction11n
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction21c
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction21s
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction22t
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction35c
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction10t
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableFieldReference
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableMethodReference
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableStringReference
import com.android.tools.smali.dexlib2.writer.pool.DexPool
import java.io.File
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

internal object WeChatRuleDexFixture {
    private const val ENVIRONMENT = "Lfixture/DisplayMetrics;"
    private const val WIDTH_PROVIDER = "Lfixture/WidthProvider;"
    private const val TABLET_ANCHOR = "Lenovo TB-9707F"
    private const val LOGIN_OWNER = "com/tencent/mm/plugin/account/model/LoginUtil\$Companion"

    fun apk(
        tabletOwners: List<String> = listOf("Lfixture/TabletGate;"),
        loginOwners: List<String> = listOf("Lfixture/LoginBinding;"),
        tabletSemanticAnchor: Boolean = true,
        tabletReadsModel: Boolean = true,
        tabletUsesDisplayMetrics: Boolean = true,
        tabletWidthThreshold: Int = 600,
        tabletWidthBranchOpcode: Opcode = Opcode.IF_LT,
        tabletWidthRegister: Int = 2,
        tabletWidthBranchOffset: Int = 6,
        tabletDecoyThreshold: Int? = null,
        tabletComparisonRegisterSeed: Int? = null,
        tabletGetterOpcode: Opcode = Opcode.INVOKE_VIRTUAL,
        widthComparisonControlsReturnedValue: Boolean = true,
        loginSemanticAnchor: Boolean = true,
        loginCallsTabletGate: Boolean = true,
        duplicateTabletAcrossDex: Boolean = false,
        proveCallFlow: Boolean = false,
    ): File {
        val path = Files.createTempFile("wechat-rule-fixture", ".apk").toFile()
        val tabletClasses = tabletOwners.map {
            tabletClass(
                it,
                tabletSemanticAnchor,
                tabletReadsModel,
                tabletUsesDisplayMetrics,
                tabletWidthThreshold,
                tabletWidthBranchOpcode,
                tabletWidthRegister,
                tabletWidthBranchOffset,
                tabletDecoyThreshold,
                tabletComparisonRegisterSeed,
                tabletGetterOpcode,
                widthComparisonControlsReturnedValue,
                proveCallFlow,
            )
        }
        val loginClasses = loginOwners.map { loginClass(it, tabletOwners.firstOrNull(), loginSemanticAnchor, loginCallsTabletGate, proveCallFlow) }
        val dexGroups = buildList {
            add(tabletClasses + loginClasses + widthEnvironmentClasses())
            if (duplicateTabletAcrossDex) {
                require(tabletOwners.size == 1)
                add(
                    listOf(
                        tabletClass(
                            tabletOwners.single(), tabletSemanticAnchor, tabletReadsModel, tabletUsesDisplayMetrics,
                            tabletWidthThreshold, tabletWidthBranchOpcode, tabletWidthRegister,
                            tabletWidthBranchOffset, tabletDecoyThreshold, tabletComparisonRegisterSeed,
                            tabletGetterOpcode, widthComparisonControlsReturnedValue, proveCallFlow,
                        ),
                    ),
                )
            }
        }
        ZipOutputStream(path.outputStream().buffered()).use { zip ->
            dexGroups.forEachIndexed { index, classes ->
                val dex = Files.createTempFile("wechat-rule-fixture", ".dex")
                try {
                    DexPool.writeTo(dex.toString(), ImmutableDexFile(Opcodes.getDefault(), classes))
                    zip.putNextEntry(ZipEntry(if (index == 0) "classes.dex" else "classes${index + 1}.dex"))
                    Files.copy(dex, zip)
                    zip.closeEntry()
                } finally {
                    Files.deleteIfExists(dex)
                }
            }
        }
        return path
    }

    private fun tabletClass(
        owner: String,
        anchors: Boolean,
        readsModel: Boolean,
        readsDisplay: Boolean,
        widthThreshold: Int,
        widthBranchOpcode: Opcode,
        widthRegister: Int,
        widthBranchOffset: Int,
        decoyThreshold: Int?,
        comparisonRegisterSeed: Int?,
        getterOpcode: Opcode,
        widthComparisonControlsReturnedValue: Boolean,
        proveCallFlow: Boolean,
    ): ImmutableClassDef {
        val fields = if (readsModel) listOf(
            ImmutableField(owner, "modelName", "Ljava/lang/String;", ACC_PRIVATE or ACC_STATIC, null, emptySet(), emptySet()),
        ) else emptyList()
        val strings = if (anchors) listOf(TABLET_ANCHOR, "X2", "A2") else listOf("phone-only")
        val instructions = mutableListOf<com.android.tools.smali.dexlib2.iface.instruction.Instruction>()
        strings.forEach { value ->
            instructions += ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference(value))
        }
        if (readsModel) {
            instructions += ImmutableInstruction21c(
                Opcode.SGET_OBJECT,
                0,
                ImmutableFieldReference("Landroid/os/Build;", "MODEL", "Ljava/lang/String;"),
            )
        }
        decoyThreshold?.let {
            instructions += ImmutableInstruction21s(Opcode.CONST_16, 4, it)
        }
        comparisonRegisterSeed?.let {
            instructions += ImmutableInstruction21s(Opcode.CONST_16, 4, it)
        }
        if (readsDisplay) {
            instructions += ImmutableInstruction35c(
                getterOpcode,
                1,
                1,
                0,
                0,
                0,
                0,
                ImmutableMethodReference(WIDTH_PROVIDER, "read", emptyList(), "Ljava/lang/Object;"),
            )
            instructions += ImmutableInstruction11x(Opcode.MOVE_RESULT_OBJECT, 1)
            instructions += ImmutableInstruction21c(
                Opcode.CHECK_CAST,
                1,
                com.android.tools.smali.dexlib2.immutable.reference.ImmutableTypeReference("Ljava/lang/Integer;"),
            )
            instructions += ImmutableInstruction35c(
                Opcode.INVOKE_VIRTUAL,
                1,
                1,
                0,
                0,
                0,
                0,
                ImmutableMethodReference("Ljava/lang/Integer;", "intValue", emptyList(), "I"),
            )
            instructions += ImmutableInstruction11x(Opcode.MOVE_RESULT, 2)
            instructions += ImmutableInstruction21s(Opcode.CONST_16, 3, widthThreshold)
            instructions += ImmutableInstruction22t(widthBranchOpcode, widthRegister, 3, widthBranchOffset)
            val trueWidthResult = if (widthComparisonControlsReturnedValue) 1 else 0
            instructions += ImmutableInstruction11n(Opcode.CONST_4, 0, trueWidthResult)
            instructions += ImmutableInstruction10t(Opcode.GOTO, 5)
            instructions += ImmutableInstruction11n(Opcode.CONST_4, 0, 1)
            instructions += ImmutableInstruction11x(Opcode.RETURN, 0)
            instructions += ImmutableInstruction11n(Opcode.CONST_4, 0, 0)
            instructions += ImmutableInstruction11x(Opcode.RETURN, 0)
            instructions += ImmutableInstruction11x(Opcode.RETURN, 0)
        }
        if (!readsDisplay) instructions += ImmutableInstruction11x(Opcode.RETURN, 0)
        val detector = ImmutableMethod(
            owner,
            "detect",
            listOf(ImmutableMethodParameter(ENVIRONMENT, emptySet(), null)),
            "Z",
            ACC_PUBLIC or ACC_STATIC,
            emptySet(),
            emptySet(),
            ImmutableMethodImplementation(5, instructions, emptyList(), emptyList()),
        )
        val wrapper = ImmutableMethod(
            owner,
            "tabletEnabled",
            emptyList(),
            "Z",
            ACC_PUBLIC or ACC_STATIC,
            emptySet(),
            emptySet(),
            ImmutableMethodImplementation(
                1,
                buildList {
                    if (proveCallFlow) add(ImmutableInstruction11n(Opcode.CONST_4, 0, 0))
                    add(ImmutableInstruction35c(
                        Opcode.INVOKE_STATIC,
                        1,
                        0,
                        0,
                        0,
                        0,
                        0,
                        ImmutableMethodReference(owner, "detect", listOf(ENVIRONMENT), "Z"),
                    ))
                    if (proveCallFlow) add(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0))
                    add(ImmutableInstruction11x(Opcode.RETURN, 0))
                },
                emptyList(),
                emptyList(),
            ),
        )
        return ImmutableClassDef(owner, ACC_PUBLIC, "Ljava/lang/Object;", emptyList(), null, emptySet(), fields, listOf(detector, wrapper))
    }

    private fun widthEnvironmentClasses(): List<ImmutableClassDef> = listOf(
        ImmutableClassDef(
            ENVIRONMENT,
            ACC_PUBLIC,
            WIDTH_PROVIDER,
            emptyList(),
            null,
            emptySet(),
            emptyList(),
            emptyList(),
        ),
        ImmutableClassDef(
            WIDTH_PROVIDER,
            ACC_PUBLIC,
            "Ljava/lang/Object;",
            emptyList(),
            null,
            emptySet(),
            emptyList(),
            listOf(
                ImmutableMethod(
                    WIDTH_PROVIDER,
                    "read",
                    emptyList(),
                    "Ljava/lang/Object;",
                    ACC_PUBLIC,
                    emptySet(),
                    emptySet(),
                    ImmutableMethodImplementation(1, listOf(ImmutableInstruction11x(Opcode.RETURN_OBJECT, 0)), emptyList(), emptyList()),
                ),
            ),
        ),
    )

    private fun loginClass(owner: String, tabletOwner: String?, anchors: Boolean, linksToTablet: Boolean, proveCallFlow: Boolean): ImmutableClassDef {
        val instructions = mutableListOf<com.android.tools.smali.dexlib2.iface.instruction.Instruction>()
        if (anchors) {
            instructions += ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference(LOGIN_OWNER))
            instructions += ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference("updateLoginAsExdevice"))
        }
        if (linksToTablet && tabletOwner != null) {
            instructions += ImmutableInstruction35c(
                Opcode.INVOKE_STATIC,
                0,
                0,
                0,
                0,
                0,
                0,
                ImmutableMethodReference(tabletOwner, "tabletEnabled", emptyList(), "Z"),
            )
        }
        if (proveCallFlow) {
            if (linksToTablet && tabletOwner != null) {
                instructions += ImmutableInstruction11x(Opcode.MOVE_RESULT, 0)
                instructions += com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction21t(Opcode.IF_NEZ, 0, 7)
            }
            instructions += ImmutableInstruction21s(Opcode.CONST_16, 0, 8)
        }
        instructions += ImmutableInstruction35c(
            Opcode.INVOKE_VIRTUAL,
            2,
            1,
            if (proveCallFlow) 0 else 2,
            0,
            0,
            0,
            ImmutableMethodReference("Landroid/view/View;", "setVisibility", listOf("I"), "V"),
        )
        instructions += ImmutableInstruction10x(Opcode.RETURN_VOID)
        val method = ImmutableMethod(
            owner,
            "update",
            listOf(
                ImmutableMethodParameter("Landroid/view/View;", emptySet(), null),
                ImmutableMethodParameter("Landroidx/lifecycle/LifecycleOwner;", emptySet(), null),
            ),
            "V",
            ACC_PUBLIC or ACC_STATIC,
            emptySet(),
            emptySet(),
            ImmutableMethodImplementation(3, instructions, emptyList(), emptyList()),
        )
        return ImmutableClassDef(owner, ACC_PUBLIC, "Ljava/lang/Object;", emptyList(), null, emptySet(), emptyList(), listOf(method))
    }

    private const val ACC_PUBLIC = 0x0001
    private const val ACC_PRIVATE = 0x0002
    private const val ACC_STATIC = 0x0008
}
