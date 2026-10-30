import android.content.pm.ApplicationInfo;
import android.os.Build;
import dalvik.system.PathClassLoader;
import java.io.File;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.util.List;
import java.util.Map;
import org.json.JSONArray;
import org.json.JSONObject;

/** Parses exact local input snapshots; does not claim application/UI acceptance. */
public final class SemanticInputProbe {
    private static Object get(Object object, String name) throws Exception { return object.getClass().getMethod(name).invoke(object); }
    private static Object singleton(ClassLoader module, String name) throws Exception { return module.loadClass(name).getField("INSTANCE").get(null); }

    public static void main(String[] args) throws Exception {
        File root = new File(args[0]);
        JSONObject input = new JSONObject(new String(Files.readAllBytes(new File(root, "input.json").toPath()), "UTF-8"));
        System.setProperty("java.io.tmpdir", root.toString());
        ClassLoader module = new PathClassLoader(args[1], ClassLoader.getSystemClassLoader());
        ApplicationInfo info = new ApplicationInfo();
        info.packageName = "io.github.nku100.impad"; info.sourceDir = args[1]; info.publicSourceDir = args[1];
        Object nativeRuntime = singleton(module, "io.github.nku100.impad.runtime.AndroidDexKitRuntime");
        nativeRuntime.getClass().getMethod("load", ApplicationInfo.class, ClassLoader.class).invoke(nativeRuntime, info, module);
        JSONArray files = input.getJSONArray("codeFiles");
        StringBuilder paths = new StringBuilder();
        for (int i = 0; i < files.length(); i++) {
            if (i > 0) paths.append(File.pathSeparator);
            paths.append(new File(root, files.getString(i)).getAbsolutePath());
        }
        ClassLoader boot = ClassLoader.getSystemClassLoader();
        while (boot.getParent() != null) boot = boot.getParent();
        ClassLoader target = new PathClassLoader(paths.toString(), boot);
        Class<?> readerType = module.loadClass("io.github.nku100.impad.runtime.StartupLoaderLayoutReader");
        Object reader = readerType.getConstructor().newInstance();
        Class<?> runtimeType = module.loadClass("io.github.nku100.impad.runtime.StartupRuntimeSnapshot");
        Object companion = runtimeType.getField("Companion").get(null);
        File storage = new File(root, "snapshots"); storage.mkdirs();
        long started = System.nanoTime();
        Object runtime = companion.getClass().getMethod("capture", ClassLoader.class, File.class, readerType).invoke(companion, target, storage, reader);
        long captureDone = System.nanoTime();
        Object captured = get(runtime, "getSnapshot");
        Class<?> capturedType = module.loadClass("io.github.nku100.impad.rules.CapturedCodeSnapshot");
        Class<?> sessionType = module.loadClass("io.github.nku100.impad.rules.RuleSession");
        Object session = null;
        JSONObject report = new JSONObject().put("caseName", input.getString("caseName")).put("appId", input.getString("appId"))
            .put("scope", "REAL_DEX_PARSE_REFLECTION_PROBE").put("guarantee", "STARTUP_FILE_SNAPSHOT")
            .put("api", Build.VERSION.SDK_INT).put("abi", Build.SUPPORTED_ABIS[0]);
        try {
            Object effective = singleton(module, "io.github.nku100.impad.rules.EffectiveCodeRuleSession");
            session = effective.getClass().getMethod("open", capturedType).invoke(effective, captured);
            Object catalogs = singleton(module, "io.github.nku100.impad.rules.SemanticAppCatalogs");
            Object catalog = catalogs.getClass().getMethod("forApp", String.class).invoke(catalogs, input.getString("appId"));
            Class<?> catalogType = module.loadClass("io.github.nku100.impad.rules.SemanticRuleCatalog");
            Class<?> contextType = module.loadClass("io.github.nku100.impad.rules.RecognitionContext");
            Object context = contextType.getConstructor(sessionType, capturedType, catalogType).newInstance(session, captured, catalog);
            Class<?> identityType = module.loadClass("io.github.nku100.impad.compat.BuildIdentity");
            Object identity = identityType.getConstructor(String.class, String.class, long.class, String.class, String.class, String.class)
                .newInstance(input.getString("packageName"), input.getString("versionName"), input.getLong("versionCode"), "arm64-v8a",
                    input.getString("apkSha256"), input.getString("signerSha256"));
            Object resolver = singleton(module, "io.github.nku100.impad.rules.SemanticRuleResolver");
            Object run = resolver.getClass().getMethod("resolveDetailed", identityType, catalogType, contextType).invoke(resolver, identity, catalog, context);
            Object outcome = get(run, "getOutcome");
            report.put("status", outcome.getClass().getSimpleName());
            report.put("evaluatedRecognizers", new JSONArray((List<?>) get(run, "getEvaluatedRecognizerIds")));
            JSONObject results = new JSONObject();
            for (Map.Entry<?, ?> entry : ((Map<?, ?>) get(run, "getResults")).entrySet()) {
                JSONObject r = new JSONObject().put("status", entry.getValue().getClass().getSimpleName());
                try { r.put("diagnostics", new JSONArray((List<?>) get(entry.getValue(), "getDiagnostics"))); } catch (NoSuchMethodException ignored) { }
                try { r.put("reason", get(entry.getValue(), "getReason")); } catch (NoSuchMethodException ignored) { }
                results.put(entry.getKey().toString(), r);
            }
            report.put("recognizers", results);
            if (outcome.getClass().getSimpleName().equals("Selected")) {
                Object plan = get(get(outcome, "getMatch"), "getPlan");
                Class<?> shapeType = module.loadClass("io.github.nku100.impad.compat.rules.MethodShape");
                Object methodResolver = singleton(module, "io.github.nku100.impad.runtime.TargetMethodResolver");
                Map<?, ?> bindings = (Map<?, ?>) get(get(runtime, "getLayout"), "getBindings");
                JSONObject resultPlan = new JSONObject().put("snapshotSha256", get(plan, "getSnapshotSha256"))
                    .put("contractId", get(plan, "getContractId")).put("contractRevision", get(plan, "getContractRevision"))
                    .put("strategyId", get(plan, "getStrategyId")).put("strategyRevision", get(plan, "getStrategyRevision"))
                    .put("executionSha256", get(plan, "executionSha256"));
                JSONArray hooks = new JSONArray();
                for (Object hook : (List<?>) get(plan, "getHooks")) {
                    Object source = get(hook, "getSource");
                    Object shape = get(hook, "getShape");
                    ClassLoader definingLoader = (ClassLoader) bindings.get(get(source, "getLoaderId"));
                    Method method = (Method) methodResolver.getClass().getMethod("resolve", ClassLoader.class, String.class, shapeType)
                        .invoke(methodResolver, definingLoader, get(hook, "getDescriptor"), shape);
                    if (method.getDeclaringClass().getClassLoader() != definingLoader) throw new AssertionError("declaring loader mismatch");
                    hooks.put(new JSONObject().put("role", get(hook, "getRole")).put("descriptor", get(hook, "getDescriptor"))
                        .put("shape", new JSONObject().put("parameters", new JSONArray((List<?>) get(shape, "getParameters")))
                            .put("returnType", get(shape, "getReturnType")).put("isStatic", get(shape, "isStatic")))
                        .put("source", new JSONObject().put("loaderId", get(source, "getLoaderId")).put("dexIndex", get(source, "getDexIndex"))
                            .put("dexSha256", get(source, "getDexSha256"))).put("phase", get(hook, "getPhase").toString())
                        .put("configuration", new JSONObject((Map<?, ?>) get(hook, "getConfiguration"))).put("boundMethod", method.toString()));
                }
                resultPlan.put("hooks", hooks); report.put("plan", resultPlan);
                if (!((Boolean) runtimeType.getMethod("inputsStillCurrent").invoke(runtime))) throw new AssertionError("input changed");
            }
            report.put("captureMillis", (captureDone - started) / 1000000.0).put("totalMillis", (System.nanoTime() - started) / 1000000.0);
            Files.write(new File(root, "report.json").toPath(), report.toString(2).getBytes("UTF-8"));
            System.out.println("SEMANTIC_INPUT_PROBE " + input.getString("caseName") + " " + report.getString("status"));
        } finally {
            if (session != null) sessionType.getMethod("close").invoke(session);
            runtimeType.getMethod("close").invoke(runtime);
        }
    }
}
