import com.android.tools.smali.dexlib2.AccessFlags;
import com.android.tools.smali.dexlib2.Opcode;
import com.android.tools.smali.dexlib2.Opcodes;
import com.android.tools.smali.dexlib2.dexbacked.DexBackedDexFile;
import com.android.tools.smali.dexlib2.iface.ClassDef;
import com.android.tools.smali.dexlib2.iface.Field;
import com.android.tools.smali.dexlib2.iface.Method;
import com.android.tools.smali.dexlib2.iface.instruction.Instruction;
import com.android.tools.smali.dexlib2.iface.instruction.OffsetInstruction;
import com.android.tools.smali.dexlib2.iface.instruction.OneRegisterInstruction;
import com.android.tools.smali.dexlib2.iface.instruction.FiveRegisterInstruction;
import com.android.tools.smali.dexlib2.iface.instruction.ReferenceInstruction;
import com.android.tools.smali.dexlib2.iface.reference.FieldReference;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.ZipFile;

/** Audits a supplied frozen target directly; does not call the module recognizer. */
public final class FrozenCacheAudit {
    public static void main(String[] args) throws Exception {
        if (args.length != 5) throw new IllegalArgumentException("apk dex-entry owner field decision-descriptor required");
        try (ZipFile apk = new ZipFile(args[0])) {
            DexBackedDexFile dex;
            try (var in = new java.io.BufferedInputStream(apk.getInputStream(apk.getEntry(args[1])))) {
                dex = DexBackedDexFile.fromInputStream(Opcodes.getDefault(), in);
            }
            ClassDef owner = dex.getClasses().stream().filter(c -> c.getType().equals(args[2])).findFirst().orElseThrow();
            Field cache = null;
            for (Field field : owner.getFields()) if (field.getName().equals(args[3])) {
                if (cache != null) throw new IllegalStateException("Ambiguous field");
                cache = field;
            }
            if (cache == null || !cache.getType().equals("Ljava/lang/Boolean;") ||
                    (cache.getAccessFlags() & AccessFlags.STATIC.getValue()) == 0 ||
                    (cache.getAccessFlags() & AccessFlags.FINAL.getValue()) != 0) throw new IllegalStateException("Not a writable Boolean cache");
            Method decision = null;
            for (Method method : owner.getMethods()) if (method.toString().equals(args[4])) decision = method;
            if (decision == null || decision.getImplementation() == null ||
                    !decision.getReturnType().equals("Z") || decision.getParameterTypes().size() != 1 ||
                    (decision.getAccessFlags() & AccessFlags.STATIC.getValue()) == 0) throw new IllegalStateException("Invalid decision");
            List<Instruction> code = new ArrayList<>();
            decision.getImplementation().getInstructions().forEach(code::add);
            int reads = 0, writes = 0;
            String descriptor = cache.toString();
            for (Instruction instruction : code) if (descriptor.equals(field(instruction))) {
                if (instruction.getOpcode() == Opcode.SGET_OBJECT) reads++;
                if (instruction.getOpcode() == Opcode.SPUT_OBJECT) writes++;
            }
            if (reads == 0 || writes == 0 || !cachedReturn(code, descriptor)) throw new IllegalStateException("Missing cache return/write relationship");
            System.out.println(cache.getAccessFlags() + "\t" + reads + "\t" + writes);
        }
    }

    private static String field(Instruction instruction) {
        if (instruction instanceof ReferenceInstruction reference && reference.getReference() instanceof FieldReference field)
            return field.toString();
        return null;
    }

    private static boolean cachedReturn(List<Instruction> code, String field) {
        int[] addresses = new int[code.size() + 1];
        for (int i = 0; i < code.size(); i++) addresses[i + 1] = addresses[i] + code.get(i).getCodeUnits();
        for (int i = 0; i + 4 < code.size(); i++) {
            Instruction read = code.get(i), branch = code.get(i + 1);
            if (read.getOpcode() != Opcode.SGET_OBJECT || !field.equals(field(read)) || branch.getOpcode() != Opcode.IF_EQZ ||
                    ((OneRegisterInstruction) branch).getRegisterA() != ((OneRegisterInstruction) read).getRegisterA()) continue;
            int receiver = ((OneRegisterInstruction) read).getRegisterA(), unbox = i + 2;
            if (code.get(unbox).getOpcode() == Opcode.SGET_OBJECT) {
                if (!field.equals(field(code.get(unbox)))) continue;
                receiver = ((OneRegisterInstruction) code.get(unbox++)).getRegisterA();
            }
            if (unbox + 2 >= code.size()) continue;
            Instruction call = code.get(unbox), result = code.get(unbox + 1), returned = code.get(unbox + 2);
            if (call.getOpcode() != Opcode.INVOKE_VIRTUAL || !(call instanceof FiveRegisterInstruction registers) ||
                    registers.getRegisterCount() != 1 || registers.getRegisterC() != receiver ||
                    !((ReferenceInstruction) call).getReference().toString().equals("Ljava/lang/Boolean;->booleanValue()Z") ||
                    result.getOpcode() != Opcode.MOVE_RESULT || returned.getOpcode() != Opcode.RETURN ||
                    ((OneRegisterInstruction) result).getRegisterA() != ((OneRegisterInstruction) returned).getRegisterA()) continue;
            int target = addresses[i + 1] + ((OffsetInstruction) branch).getCodeOffset();
            for (int next = unbox + 3; next < code.size(); next++) if (addresses[next] == target) return true;
        }
        return false;
    }
}
