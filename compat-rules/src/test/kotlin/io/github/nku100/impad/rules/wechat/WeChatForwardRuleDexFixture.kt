package io.github.nku100.impad.rules.wechat

import com.android.tools.smali.dexlib2.Opcodes
import com.android.tools.smali.dexlib2.Opcode
import com.android.tools.smali.dexlib2.iface.instruction.Instruction
import com.android.tools.smali.dexlib2.immutable.ImmutableClassDef
import com.android.tools.smali.dexlib2.immutable.ImmutableDexFile
import com.android.tools.smali.dexlib2.immutable.ImmutableField
import com.android.tools.smali.dexlib2.immutable.ImmutableMethod
import com.android.tools.smali.dexlib2.immutable.ImmutableMethodImplementation
import com.android.tools.smali.dexlib2.immutable.ImmutableMethodParameter
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction10t
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction10x
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction11n
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction11x
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction12x
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction21c
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction21s
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction21t
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction22t
import com.android.tools.smali.dexlib2.immutable.instruction.ImmutableInstruction35c
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableFieldReference
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableMethodReference
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableStringReference
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableTypeReference
import com.android.tools.smali.dexlib2.writer.pool.DexPool
import java.io.File
import java.nio.file.Files
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

internal object WeChatForwardRuleDexFixture {
    data class Options(
        val tabletOwners: List<String> = listOf("Lfixture/TabletDecision;"),
        val loginOwners: List<String> = listOf("Lfixture/LoginSetup;"),
        val tabletModelMarkers: Boolean = true,
        val modelGateControlsDecision: Boolean = true,
        val widthThreshold: Int = 600,
        val widthControlsResult: Boolean = true,
        val loginCallsTabletHelper: Boolean = true,
        val wrapperForwardsResult: Boolean = true,
        val loginBooleanControlsVisibility: Boolean = true,
        val reloadCachedResult: Boolean = false,
        val cachedWrapper: Boolean = false,
        val wrapperEarlyReturn: Boolean = false,
        val wrapperForceTablet: Boolean = false,
        val returnPathPadding: Int = 0,
    )

    fun apk(options: Options = Options()): File {
        require(options.tabletOwners.isNotEmpty())
        val path = Files.createTempFile("wechat-forward-rule-fixture", ".apk").toFile()
        val classes = buildList {
            add(modelGateClass())
            add(widthClass())
            options.tabletOwners.forEach { owner ->
                add(tabletClass(owner, options))
            }
            options.loginOwners.forEach { owner ->
                add(loginClass(owner, options.tabletOwners.first(), options))
            }
        }
        ZipOutputStream(path.outputStream().buffered()).use { zip ->
            val dex = Files.createTempFile("wechat-forward-rule-fixture", ".dex")
            try {
                DexPool.writeTo(dex.toString(), ImmutableDexFile(Opcodes.getDefault(), classes))
                zip.putNextEntry(ZipEntry("classes.dex"))
                Files.copy(dex, zip)
                zip.closeEntry()
            } finally {
                Files.deleteIfExists(dex)
            }
        }
        return path
    }

    private fun modelGateClass(): ImmutableClassDef {
        val cache = ImmutableField(MODEL_GATE, "cached", BOOLEAN_DESCRIPTOR, ACC_PRIVATE or ACC_STATIC, null, emptySet(), emptySet())
        val code = assemble(
            listOf(
                Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, cache.reference())),
                Branch21(Opcode.IF_EQZ, 0, "compare"),
                Code(invokeVirtual(0, ImmutableMethodReference(BOOLEAN, "booleanValue", emptyList(), "Z"))),
                Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0)),
                Code(ImmutableInstruction11x(Opcode.RETURN, 0)),
                Label("compare"),
                Code(ImmutableInstruction21c(Opcode.CONST_STRING, 1, ImmutableStringReference(LENOVO_MODEL))),
                Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, buildModelField())),
                Code(invokeVirtual(2, ImmutableMethodReference(STRING, "equalsIgnoreCase", listOf(STRING), "Z"))),
                Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 2)),
                Code(ImmutableInstruction35c(
                    Opcode.INVOKE_STATIC, 1, 2, 0, 0, 0, 0,
                    ImmutableMethodReference(BOOLEAN, "valueOf", listOf("Z"), BOOLEAN_DESCRIPTOR),
                )),
                Code(ImmutableInstruction11x(Opcode.MOVE_RESULT_OBJECT, 3)),
                Code(ImmutableInstruction21c(Opcode.SPUT_OBJECT, 3, cache.reference())),
                Code(ImmutableInstruction11x(Opcode.RETURN, 2)),
            ),
        )
        val method = staticMethod(MODEL_GATE, "matches", emptyList(), "Z", 4, code)
        return ImmutableClassDef(
            MODEL_GATE,
            ACC_PUBLIC,
            "Ljava/lang/Object;",
            emptyList(),
            null,
            emptySet(),
            listOf(cache),
            listOf(method),
        )
    }

    private fun tabletClass(owner: String, options: Options): ImmutableClassDef {
        val cache = ImmutableField(owner, "cached", BOOLEAN_DESCRIPTOR, ACC_PRIVATE or ACC_STATIC, null, emptySet(), emptySet())
        val body = mutableListOf<Node>()
        body += Code(ImmutableInstruction11n(Opcode.CONST_4, 4, 1))
        body += Code(ImmutableInstruction11n(Opcode.CONST_4, 3, 0))
        body += Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, cache.reference()))
        body += Branch21(Opcode.IF_EQZ, 0, "compute")
        if (options.reloadCachedResult) body += Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 2, cache.reference()))
        body += Code(invokeVirtual(if (options.reloadCachedResult) 2 else 0,
            ImmutableMethodReference(BOOLEAN, "booleanValue", emptyList(), "Z")))
        body += Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0))
        body += Code(ImmutableInstruction11x(Opcode.RETURN, 0))
        body += Label("compute")
        if (options.tabletModelMarkers) {
            body += Code(ImmutableInstruction21c(Opcode.CONST_STRING, 1, ImmutableStringReference("X2")))
            body += Code(ImmutableInstruction21c(Opcode.CONST_STRING, 1, ImmutableStringReference("A2")))
        } else {
            body += Code(ImmutableInstruction21c(Opcode.CONST_STRING, 1, ImmutableStringReference("phone")))
        }
        body += Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 2, buildModelField()))
        body += Code(invokeVirtual(2, ImmutableMethodReference(STRING, "equalsIgnoreCase", listOf(STRING), "Z")))
        body += Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0))
        body += Code(invokeVirtual(2, ImmutableMethodReference(STRING, "equalsIgnoreCase", listOf(STRING), "Z")))
        body += Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0))
        body += Code(ImmutableInstruction35c(
            Opcode.INVOKE_STATIC, 0, 0, 0, 0, 0, 0,
            ImmutableMethodReference(MODEL_GATE, "matches", emptyList(), "Z"),
        ))
        body += Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0))
        body += Branch21(Opcode.IF_EQZ, 0, "width")
        if (options.modelGateControlsDecision) {
            body += Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, booleanField("TRUE")))
            body += Code(ImmutableInstruction21c(Opcode.SPUT_OBJECT, 0, cache.reference()))
            body += Code(ImmutableInstruction11x(Opcode.RETURN, 4))
        } else {
            body += Jump("width")
        }
        body += Label("width")
        body += Code(invokeVirtual(7, ImmutableMethodReference(WIDTH_ACCESSOR, "read", emptyList(), "Ljava/lang/Object;")))
        body += Code(ImmutableInstruction11x(Opcode.MOVE_RESULT_OBJECT, 2))
        body += Code(ImmutableInstruction21c(Opcode.CHECK_CAST, 2, ImmutableTypeReference(INTEGER_DESCRIPTOR)))
        body += Code(invokeVirtual(2, ImmutableMethodReference(INTEGER, "intValue", emptyList(), "I")))
        body += Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0))
        body += Code(ImmutableInstruction21s(Opcode.CONST_16, 6, options.widthThreshold))
        body += Branch22(Opcode.IF_LT, 0, 6, "widthFalse")
        if (options.widthControlsResult) {
            body += Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, booleanField("TRUE")))
        } else {
            body += Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, booleanField("FALSE")))
        }
        body += Code(ImmutableInstruction21c(Opcode.SPUT_OBJECT, 0, cache.reference()))
        body += Code(ImmutableInstruction11x(Opcode.RETURN, if (options.widthControlsResult) 4 else 3))
        body += Label("widthFalse")
        repeat(options.returnPathPadding) { body += Code(ImmutableInstruction10x(Opcode.NOP)) }
        body += Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, booleanField("FALSE")))
        body += Code(ImmutableInstruction21c(Opcode.SPUT_OBJECT, 0, cache.reference()))
        body += Code(ImmutableInstruction11x(Opcode.RETURN, 3))

        val detector = staticMethod(owner, "decide", listOf(WIDTH_ACCESSOR), "Z", 8, assemble(body))
        val wrapperCode = mutableListOf(
            ImmutableInstruction21c(Opcode.NEW_INSTANCE, 0, ImmutableTypeReference(WIDTH_ACCESSOR_DESCRIPTOR)),
            ImmutableInstruction35c(
                Opcode.INVOKE_DIRECT, 1, 0, 0, 0, 0, 0,
                ImmutableMethodReference(WIDTH_ACCESSOR, "<init>", emptyList(), "V"),
            ),
            ImmutableInstruction35c(
                Opcode.INVOKE_STATIC, 1, 0, 0, 0, 0, 0,
                ImmutableMethodReference(owner, "decide", listOf(WIDTH_ACCESSOR), "Z"),
            ),
            ImmutableInstruction11x(Opcode.MOVE_RESULT, 0),
            ImmutableInstruction11x(Opcode.RETURN, 0),
        )
        if (!options.wrapperForwardsResult) {
            wrapperCode.add(wrapperCode.lastIndex, ImmutableInstruction11n(Opcode.CONST_4, 0, 0))
        }
        val lock = ImmutableField(owner, "lock", "Ljava/lang/Object;", ACC_PRIVATE or ACC_STATIC, null, emptySet(), emptySet())
        val cachedWrapper = mutableListOf<Node>(
            Code(ImmutableInstruction21c(Opcode.NEW_INSTANCE, 2, ImmutableTypeReference(WIDTH_ACCESSOR_DESCRIPTOR))),
            Code(ImmutableInstruction35c(Opcode.INVOKE_DIRECT, 1, 2, 0, 0, 0, 0,
                ImmutableMethodReference(WIDTH_ACCESSOR, "<init>", emptyList(), "V"))),
            Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, cache.reference())),
            Branch21(Opcode.IF_EQZ, 0, "cold"),
            Code(invokeVirtual(0, ImmutableMethodReference(BOOLEAN, "booleanValue", emptyList(), "Z"))),
            Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0)),
            Jump("end"),
            Label("cold"),
            Code(ImmutableInstruction21c(Opcode.SGET_OBJECT, 0, lock.reference())),
            Code(ImmutableInstruction11x(Opcode.MONITOR_ENTER, 0)),
            Code(ImmutableInstruction35c(Opcode.INVOKE_STATIC, 1, 2, 0, 0, 0, 0,
                ImmutableMethodReference(owner, "decide", listOf(WIDTH_ACCESSOR), "Z"))),
            Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 1)),
            Code(ImmutableInstruction11x(Opcode.MONITOR_EXIT, 0)),
            Code(ImmutableInstruction12x(Opcode.MOVE, 0, 1)),
            Label("end"),
        )
        if (!options.wrapperForwardsResult) cachedWrapper += Code(ImmutableInstruction11n(Opcode.CONST_4, 0, 0))
        cachedWrapper += Code(ImmutableInstruction11x(Opcode.RETURN, 0))
        if (options.wrapperForceTablet) cachedWrapper.addAll(2, listOf(
            Code(ImmutableInstruction21c(Opcode.SGET_BOOLEAN, 0, ImmutableFieldReference(owner, "disabled", "Z"))),
            Branch21(Opcode.IF_EQZ, 0, "cacheEntry"),
            Code(ImmutableInstruction11n(Opcode.CONST_4, 0, 1)),
            Jump("end"),
            Label("cacheEntry"),
        ))
        if (options.wrapperEarlyReturn) cachedWrapper.addAll(0, listOf(
            Code(ImmutableInstruction21c(Opcode.SGET_BOOLEAN, 2, ImmutableFieldReference(owner, "disabled", "Z"))),
            Branch21(Opcode.IF_EQZ, 2, "cacheEntry"),
            Code(ImmutableInstruction11n(Opcode.CONST_4, 0, 0)),
            Code(ImmutableInstruction11x(Opcode.RETURN, 0)),
            Label("cacheEntry"),
        ))
        val wrapper = if (options.cachedWrapper) {
            staticMethod(owner, "isTablet", emptyList(), "Z", 3, assemble(cachedWrapper))
        } else staticMethod(owner, "isTablet", emptyList(), "Z", 1, wrapperCode)
        return ImmutableClassDef(
            owner,
            ACC_PUBLIC,
            "Ljava/lang/Object;",
            emptyList(),
            null,
            emptySet(),
            (if (options.cachedWrapper) listOf(cache, lock) else listOf(cache)) +
                if (options.wrapperEarlyReturn || options.wrapperForceTablet) listOf(ImmutableField(owner, "disabled", "Z", ACC_PRIVATE or ACC_STATIC, null, emptySet(), emptySet())) else emptyList(),
            listOf(detector, wrapper),
        )
    }

    private fun widthClass(): ImmutableClassDef {
        val constructor = ImmutableMethod(
            WIDTH_ACCESSOR,
            "<init>",
            emptyList(),
            "V",
            ACC_PUBLIC,
            emptySet(),
            emptySet(),
            ImmutableMethodImplementation(
                1,
                listOf(
                    ImmutableInstruction35c(
                        Opcode.INVOKE_DIRECT, 1, 0, 0, 0, 0, 0,
                        ImmutableMethodReference("Ljava/lang/Object;", "<init>", emptyList(), "V"),
                    ),
                    ImmutableInstruction10x(Opcode.RETURN_VOID),
                ),
                emptyList(),
                emptyList(),
            ),
        )
        val read = ImmutableMethod(
            WIDTH_ACCESSOR,
            "read",
            emptyList(),
            "Ljava/lang/Object;",
            ACC_PUBLIC,
            emptySet(),
            emptySet(),
            ImmutableMethodImplementation(
                1,
                listOf(ImmutableInstruction11n(Opcode.CONST_4, 0, 0), ImmutableInstruction11x(Opcode.RETURN_OBJECT, 0)),
                emptyList(),
                emptyList(),
            ),
        )
        return ImmutableClassDef(
            WIDTH_ACCESSOR,
            ACC_PUBLIC,
            "Ljava/lang/Object;",
            emptyList(),
            null,
            emptySet(),
            emptyList(),
            listOf(constructor, read),
        )
    }

    private fun loginClass(owner: String, tabletOwner: String, options: Options): ImmutableClassDef {
        val code = buildList<Node> {
            add(Code(ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference(LOGIN_TRACE_OWNER))))
            add(Code(ImmutableInstruction21c(Opcode.CONST_STRING, 0, ImmutableStringReference(LOGIN_TRACE_METHOD))))
            if (options.loginCallsTabletHelper) {
                add(Code(ImmutableInstruction35c(
                    Opcode.INVOKE_STATIC, 0, 0, 0, 0, 0, 0,
                    ImmutableMethodReference(tabletOwner, "isTablet", emptyList(), "Z"),
                )))
                if (options.loginBooleanControlsVisibility) {
                    add(Code(ImmutableInstruction11x(Opcode.MOVE_RESULT, 0)))
                    add(Branch21(Opcode.IF_NEZ, 0, "done"))
                }
            }
            add(Code(ImmutableInstruction11n(Opcode.CONST_4, 1, 0)))
            add(Code(ImmutableInstruction35c(
                Opcode.INVOKE_VIRTUAL, 2, 2, 1, 0, 0, 0,
                ImmutableMethodReference(VIEW_DESCRIPTOR, "setVisibility", listOf("I"), "V"),
            )))
            add(Label("done"))
            add(Code(ImmutableInstruction10x(Opcode.RETURN_VOID)))
        }
        val method = ImmutableMethod(
            owner,
            "update",
            listOf(
                ImmutableMethodParameter(VIEW_DESCRIPTOR, emptySet(), null),
                ImmutableMethodParameter(LIFECYCLE_OWNER_DESCRIPTOR, emptySet(), null),
            ),
            "V",
            ACC_PUBLIC or ACC_STATIC,
            emptySet(),
            emptySet(),
            ImmutableMethodImplementation(4, assemble(code), emptyList(), emptyList()),
        )
        return ImmutableClassDef(owner, ACC_PUBLIC, "Ljava/lang/Object;", emptyList(), null, emptySet(), emptyList(), listOf(method))
    }

    private fun staticMethod(
        owner: String,
        name: String,
        parameters: List<String>,
        returnType: String,
        registerCount: Int,
        instructions: List<Instruction>,
    ) = ImmutableMethod(
        owner,
        name,
        parameters.map { ImmutableMethodParameter(it, emptySet(), null) },
        returnType,
        ACC_PUBLIC or ACC_STATIC,
        emptySet(),
        emptySet(),
        ImmutableMethodImplementation(registerCount, instructions, emptyList(), emptyList()),
    )

    private fun ImmutableField.reference() = ImmutableFieldReference(definingClass, name, type)

    private fun buildModelField() = ImmutableFieldReference("Landroid/os/Build;", "MODEL", "Ljava/lang/String;")

    private fun booleanField(name: String) = ImmutableFieldReference(BOOLEAN, name, BOOLEAN_DESCRIPTOR)

    private fun invokeVirtual(register: Int, reference: ImmutableMethodReference) =
        ImmutableInstruction35c(Opcode.INVOKE_VIRTUAL, 1, register, 0, 0, 0, 0, reference)

    private sealed interface Node
    private data class Code(val instruction: Instruction) : Node
    private data class Label(val name: String) : Node
    private data class Branch21(val opcode: Opcode, val register: Int, val target: String) : Node
    private data class Branch22(val opcode: Opcode, val left: Int, val right: Int, val target: String) : Node
    private data class Jump(val target: String) : Node

    private fun assemble(nodes: List<Node>): List<Instruction> {
        val labels = mutableMapOf<String, Int>()
        val addresses = mutableListOf<Int>()
        var address = 0
        nodes.forEach { node ->
            addresses += address
            when (node) {
                is Label -> labels[node.name] = address
                is Code -> address += node.instruction.codeUnits
                is Branch21, is Branch22 -> address += 2
                is Jump -> address += 1
            }
        }
        return nodes.mapIndexedNotNull { index, node ->
            val here = addresses[index]
            when (node) {
                is Label -> null
                is Code -> node.instruction
                is Branch21 -> ImmutableInstruction21t(node.opcode, node.register, labels.getValue(node.target) - here)
                is Branch22 -> ImmutableInstruction22t(node.opcode, node.left, node.right, labels.getValue(node.target) - here)
                is Jump -> ImmutableInstruction10t(Opcode.GOTO, labels.getValue(node.target) - here)
            }
        }
    }

    private const val MODEL_GATE = "Lfixture/ModelProbe;"
    private const val WIDTH_ACCESSOR = "Lfixture/WidthAccessor;"
    private const val WIDTH_ACCESSOR_DESCRIPTOR = "Lfixture/WidthAccessor;"
    private const val BOOLEAN = "Ljava/lang/Boolean;"
    private const val BOOLEAN_DESCRIPTOR = "Ljava/lang/Boolean;"
    private const val STRING = "Ljava/lang/String;"
    private const val INTEGER = "Ljava/lang/Integer;"
    private const val INTEGER_DESCRIPTOR = "Ljava/lang/Integer;"
    private const val VIEW_DESCRIPTOR = "Landroid/view/View;"
    private const val LIFECYCLE_OWNER_DESCRIPTOR = "Landroidx/lifecycle/LifecycleOwner;"
    private const val LENOVO_MODEL = "Lenovo TB-9707F"
    private const val LOGIN_TRACE_OWNER = "com/tencent/mm/plugin/account/model/LoginUtil\$Companion"
    private const val LOGIN_TRACE_METHOD = "updateLoginAsExdevice"
    private const val ACC_PUBLIC = 0x0001
    private const val ACC_PRIVATE = 0x0002
    private const val ACC_STATIC = 0x0008
}
