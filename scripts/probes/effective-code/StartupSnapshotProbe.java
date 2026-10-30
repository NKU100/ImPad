import android.content.pm.ApplicationInfo;
import android.os.Build;
import dalvik.system.DelegateLastClassLoader;
import dalvik.system.PathClassLoader;
import java.io.File;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.util.Collections;
import java.util.List;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONObject;

/** Verifies the STARTUP_FILE_SNAPSHOT contract only, with real Android DexKit queries. */
public final class StartupSnapshotProbe {
    private static ClassLoader module;
    private static Object reader;
    private static Class<?> readerType;
    private static Class<?> runtimeType;
    private static Class<?> capturedType;

    private static Object get(Object object, String name) throws Exception { return object.getClass().getMethod(name).invoke(object); }

    private static JSONObject check(String name, ClassLoader loader, File storage, String expected, boolean oldAbsent) throws Exception {
        storage.mkdirs();
        Object companion = runtimeType.getField("Companion").get(null);
        Object runtime = companion.getClass().getMethod("capture", ClassLoader.class, File.class, readerType).invoke(companion, loader, storage, reader);
        Object captured = get(runtime, "getSnapshot");
        Object session = null;
        try {
            Class<?> effective = module.loadClass("io.github.nku100.impad.rules.EffectiveCodeRuleSession");
            session = effective.getMethod("open", capturedType).invoke(effective.getField("INSTANCE").get(null), captured);
            Class<?> sessionType = module.loadClass("io.github.nku100.impad.rules.RuleSession");
            Class<?> queryType = module.loadClass("io.github.nku100.impad.rules.MethodQuery");
            Object query = queryType.getConstructor(Set.class, List.class, boolean.class, String.class, String.class)
                .newInstance(Collections.emptySet(), null, false, "probe.fixture.Target", null);
            List<?> methods = (List<?>) sessionType.getMethod("findMethods", queryType).invoke(session, query);
            Object marker = null;
            for (Object m : methods) {
                String descriptor = (String) get(m, "getDescriptor");
                if (descriptor.contains("->marker(")) marker = m;
                if (oldAbsent && descriptor.contains("->oldOnly(")) throw new AssertionError("old method resurrected");
            }
            if (marker == null || !((Set<?>) get(marker, "getStrings")).contains(expected)) throw new AssertionError("wrong effective method " + methods);
            Class<?> real = loader.loadClass("probe.fixture.Target");
            String actual = (String) real.getMethod("marker").invoke(null);
            if (!actual.equals(expected)) throw new AssertionError("runtime oracle mismatch " + actual);
            runtimeType.getMethod("requireDeclaringLoader", String.class, ClassLoader.class).invoke(runtime, "probe.fixture.Target", real.getClassLoader());
            Class<?> methodType = module.loadClass("io.github.nku100.impad.rules.RuleMethod");
            Object source = sessionType.getMethod("sourceOf", methodType).invoke(session, marker);
            java.util.Map<?, ?> bindings = (java.util.Map<?, ?>) get(get(runtime, "getLayout"), "getBindings");
            if (bindings.get(get(source, "getLoaderId")) != real.getClassLoader()) throw new AssertionError("wrong method source loader");
            List<?> instructions = (List<?>) sessionType.getMethod("readInstructions", methodType).invoke(session, marker);
            if (instructions.isEmpty()) throw new AssertionError("missing instructions");
            if (!((Boolean) runtimeType.getMethod("inputsStillCurrent").invoke(runtime))) throw new AssertionError("unexpectedly stale snapshot");
            String hash = (String) get(get(captured, "getMetadata"), "contentSha256");
            return new JSONObject().put("caseName", name).put("marker", actual).put("snapshotSha256", hash)
                .put("effectiveDescriptor", get(marker, "getDescriptor")).put("instructionCount", instructions.size())
                .put("declaringLoaderMatched", true).put("oldMethodAbsent", oldAbsent);
        } finally {
            if (session != null) module.loadClass("io.github.nku100.impad.rules.RuleSession").getMethod("close").invoke(session);
            runtimeType.getMethod("close").invoke(runtime);
        }
    }

    private static JSONObject checkParentNamespace(ClassLoader loader, File storage) throws Exception {
        storage.mkdirs();
        Object companion = runtimeType.getField("Companion").get(null);
        Object runtime = companion.getClass().getMethod("capture", ClassLoader.class, File.class, readerType).invoke(companion, loader, storage, reader);
        Class<?> sessionType = module.loadClass("io.github.nku100.impad.rules.RuleSession");
        Object session = null, scoped = null;
        try {
            Object captured = get(runtime, "getSnapshot");
            Class<?> effective = module.loadClass("io.github.nku100.impad.rules.EffectiveCodeRuleSession");
            session = effective.getMethod("open", capturedType).invoke(effective.getField("INSTANCE").get(null), captured);
            Class<?> queryType = module.loadClass("io.github.nku100.impad.rules.MethodQuery");
            java.lang.reflect.Constructor<?> query = queryType.getConstructor(Set.class, List.class, boolean.class, String.class, String.class);
            Object extraQuery = query.newInstance(Collections.emptySet(), null, false, "probe.fixture.Extra", null);
            List<?> extras = (List<?>) sessionType.getMethod("findMethods", queryType).invoke(session, extraQuery);
            Object method = extras.stream().filter(m -> {
                try { return ((String) get(m, "getDescriptor")).contains("->marker("); }
                catch (Exception e) { throw new RuntimeException(e); }
            }).findFirst().orElseThrow(() -> new AssertionError("parent Extra missing"));
            Class<?> methodType = module.loadClass("io.github.nku100.impad.rules.RuleMethod");
            scoped = sessionType.getMethod("inContextOf", methodType).invoke(session, method);
            Object targetQuery = query.newInstance(Collections.emptySet(), null, false, "probe.fixture.Target", null);
            List<?> targets = (List<?>) sessionType.getMethod("findMethods", queryType).invoke(scoped, targetQuery);
            Object target = targets.stream().filter(m -> {
                try { return ((String) get(m, "getDescriptor")).contains("->marker("); }
                catch (Exception e) { throw new RuntimeException(e); }
            }).findFirst().orElseThrow(() -> new AssertionError("parent Target missing"));
            if (!((Set<?>) get(target, "getStrings")).contains("A")) throw new AssertionError("parent query resolved child B");
            boolean rootReadRejected = false;
            try { sessionType.getMethod("readInstructions", methodType).invoke(session, target); }
            catch (java.lang.reflect.InvocationTargetException e) { rootReadRejected = e.getCause() instanceof IllegalArgumentException; }
            if (!rootReadRejected) throw new AssertionError("root read bypassed class shadowing");
            List<?> instructions = (List<?>) sessionType.getMethod("readInstructions", methodType).invoke(scoped, target);
            if (instructions.isEmpty()) throw new AssertionError("parent instructions missing");
            Class<?> actualParentTarget = loader.getParent().loadClass("probe.fixture.Target");
            String actual = (String) actualParentTarget.getMethod("marker").invoke(null);
            if (!actual.equals("A") || !loader.loadClass("probe.fixture.Target").getMethod("marker").invoke(null).equals("B"))
                throw new AssertionError("namespace runtime oracle mismatch");
            Object source = sessionType.getMethod("sourceOf", methodType).invoke(scoped, target);
            java.util.Map<?, ?> bindings = (java.util.Map<?, ?>) get(get(runtime, "getLayout"), "getBindings");
            if (bindings.get(get(source, "getLoaderId")) != actualParentTarget.getClassLoader()) throw new AssertionError("parent source loader mismatch");
            return new JSONObject().put("caseName", "parent_namespace").put("marker", actual).put("declaringLoaderMatched", true)
                .put("rootShadowedReadRejected", true).put("instructionCount", instructions.size());
        } finally {
            if (scoped != null) sessionType.getMethod("close").invoke(scoped);
            if (session != null) sessionType.getMethod("close").invoke(session);
            runtimeType.getMethod("close").invoke(runtime);
        }
    }

    public static void main(String[] args) throws Exception {
        File root = new File(args[0]);
        System.setProperty("java.io.tmpdir", root.toString());
        module = new PathClassLoader(args[1], ClassLoader.getSystemClassLoader());
        ApplicationInfo info = new ApplicationInfo();
        info.packageName = "io.github.nku100.impad"; info.sourceDir = args[1]; info.publicSourceDir = args[1];
        Class<?> runtimeNative = module.loadClass("io.github.nku100.impad.runtime.AndroidDexKitRuntime");
        runtimeNative.getMethod("load", ApplicationInfo.class, ClassLoader.class).invoke(runtimeNative.getField("INSTANCE").get(null), info, module);
        readerType = module.loadClass("io.github.nku100.impad.runtime.StartupLoaderLayoutReader");
        reader = readerType.getConstructor().newInstance();
        runtimeType = module.loadClass("io.github.nku100.impad.runtime.StartupRuntimeSnapshot");
        capturedType = module.loadClass("io.github.nku100.impad.rules.CapturedCodeSnapshot");
        ClassLoader system = ClassLoader.getSystemClassLoader();
        String a = new File(root, "a.jar").toString(), b = new File(root, "b.jar").toString();
        JSONArray checks = new JSONArray();
        checks.put(check("single", new PathClassLoader(a, system), new File(root, "single-snapshot"), "A", false));
        checks.put(check("multi", new PathClassLoader(new File(root, "multi.jar").toString(), system), new File(root, "multi-snapshot"), "A", false));
        checks.put(check("parent_first", new PathClassLoader(b, new PathClassLoader(a, system)), new File(root, "parent-snapshot"), "A", false));
        checks.put(check("delegate_last", new DelegateLastClassLoader(b, new PathClassLoader(a, system)), new File(root, "delegate-snapshot"), "B", true));
        checks.put(check("whole_class", new PathClassLoader(b + File.pathSeparator + a, system), new File(root, "whole-snapshot"), "B", true));
        checks.put(checkParentNamespace(new DelegateLastClassLoader(b,
            new PathClassLoader(new File(root, "multi.jar").toString(), system)), new File(root, "parent-namespace-snapshot")));
        ClassLoader unknown = new ClassLoader(system) { };
        boolean rejected = false;
        try { readerType.getMethod("read", ClassLoader.class).invoke(reader, unknown); }
        catch (java.lang.reflect.InvocationTargetException e) { rejected = e.getCause() instanceof IllegalArgumentException; }
        if (!rejected) throw new AssertionError("unknown loader not rejected");
        JSONObject report = new JSONObject().put("api", Build.VERSION.SDK_INT).put("abi", Build.SUPPORTED_ABIS[0])
            .put("guarantee", "STARTUP_FILE_SNAPSHOT").put("checks", checks).put("unknownLoaderRejected", true);
        Files.write(new File(root, "device-report.json").toPath(), report.toString(2).getBytes("UTF-8"));
        System.out.println("STARTUP_SNAPSHOT_VERIFIED checks=" + checks.length());
    }
}
