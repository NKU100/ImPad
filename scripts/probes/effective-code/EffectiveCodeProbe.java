import android.content.pm.ApplicationInfo;
import android.os.Build;
import dalvik.system.DelegateLastClassLoader;
import dalvik.system.PathClassLoader;
import java.io.File;
import java.lang.reflect.Array;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.Arrays;
import org.json.JSONArray;
import org.json.JSONObject;

/** Isolated experiment; fixture execution markers are test oracles, not provenance APIs. */
public final class EffectiveCodeProbe {
    private static final String TARGET = "probe.fixture.Target";
    private static Class<?> bridgeType;

    private static Field field(Class<?> type, String name) throws Exception {
        for (Class<?> t = type; t != null; t = t.getSuperclass()) {
            try { Field f = t.getDeclaredField(name); f.setAccessible(true); return f; }
            catch (NoSuchFieldException ignored) { }
        }
        throw new NoSuchFieldException(name);
    }

    private static Object singleton(ClassLoader loader, String name) throws Exception {
        return loader.loadClass(name).getField("INSTANCE").get(null);
    }

    private static String hash(File file) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file.toPath()));
        StringBuilder result = new StringBuilder();
        for (byte b : digest) result.append(String.format("%02x", b & 255));
        return result.toString();
    }

    private static String marker(Class<?> target) throws Exception {
        return (String) target.getMethod("marker").invoke(null);
    }

    private static JSONObject inspect(ClassLoader loader, Class<?> actual, File output, boolean memory) throws Exception {
        output.mkdirs();
        JSONObject result = new JSONObject().put("useMemoryDexFile", memory);
        Object bridge = bridgeType.getMethod("create", ClassLoader.class, boolean.class).invoke(null, loader, memory);
        try {
            int dexCount = (Integer) bridgeType.getMethod("getDexNum").invoke(bridge);
            result.put("dexCount", dexCount);
            bridgeType.getMethod("exportDexFile", String.class).invoke(bridge, output.getAbsolutePath());
            JSONArray hashes = new JSONArray();
            JSONArray files = new JSONArray();
            File[] exported = output.listFiles();
            if (exported != null) {
                Arrays.sort(exported);
                for (File f : exported) if (f.isFile()) {
                    hashes.put(hash(f)); files.put(f.getName());
                }
            }
            result.put("exportedDexSha256", hashes).put("exportedFiles", files);
            Method real = actual.getMethod("marker");
            Object data = bridgeType.getMethod("getMethodData", Method.class).invoke(bridge, real);
            if (data == null) result.put("descriptorLookup", JSONObject.NULL);
            else {
                result.put("descriptorLookup", data.getClass().getMethod("getDescriptor").invoke(data));
                result.put("descriptorLookupStrings", new JSONArray((java.util.Collection<?>) data.getClass().getMethod("getUsingStrings").invoke(data)));
            }
            return result;
        } finally { bridgeType.getMethod("close").invoke(bridge); }
    }

    private static JSONObject run(String name, String expected, File root) throws Exception {
        File work = new File(root, name); work.mkdirs();
        String a = new File(root, "a.jar").toString();
        String b = new File(root, "b.jar").toString();
        String extra = new File(root, "extra.jar").toString();
        String same = new File(root, "same.jar").toString();
        ClassLoader system = ClassLoader.getSystemClassLoader();
        ClassLoader loader;
        Class<?> actual;
        if (name.equals("parent_first") || name.equals("delegate_last")) {
            ClassLoader parent = new PathClassLoader(a, system);
            loader = name.equals("parent_first") ? new PathClassLoader(b, parent) : new DelegateLastClassLoader(b, parent);
            actual = loader.loadClass(TARGET);
        } else if (name.equals("split_class")) {
            loader = new PathClassLoader(extra, new PathClassLoader(a, system));
            actual = loader.loadClass("probe.fixture.Extra");
        } else if (name.equals("loaded_then_replace")) {
            File changing = new File(work, "active.jar");
            Files.copy(new File(a).toPath(), changing.toPath());
            loader = new PathClassLoader(changing.toString(), system);
            actual = loader.loadClass(TARGET);
            if (!marker(actual).equals("A")) throw new AssertionError("preload A failed");
            File replacement = new File(work, "replacement.jar");
            Files.copy(new File(b).toPath(), replacement.toPath());
            Files.move(replacement.toPath(), changing.toPath(), StandardCopyOption.REPLACE_EXISTING);
        } else if (name.equals("loaded_then_prepend")) {
            loader = new PathClassLoader(a, system);
            actual = loader.loadClass(TARGET);
            if (!marker(actual).equals("A")) throw new AssertionError("preload A failed");
            ClassLoader injected = new PathClassLoader(b, system);
            Object pathList = field(loader.getClass(), "pathList").get(loader);
            Object otherList = field(injected.getClass(), "pathList").get(injected);
            Field elementsField = field(pathList.getClass(), "dexElements");
            Object oldElements = elementsField.get(pathList);
            Object newElements = elementsField.get(otherList);
            int n = Array.getLength(newElements), m = Array.getLength(oldElements);
            Object combined = Array.newInstance(oldElements.getClass().getComponentType(), n + m);
            System.arraycopy(newElements, 0, combined, 0, n);
            System.arraycopy(oldElements, 0, combined, n, m);
            elementsField.set(pathList, combined);
            if (loader.loadClass(TARGET) != actual) throw new AssertionError("loaded class identity changed");
        } else if (name.equals("unknown_loader")) {
            final ClassLoader hidden = new PathClassLoader(a, system);
            loader = new ClassLoader(system) {
                @Override protected Class<?> loadClass(String n, boolean resolve) throws ClassNotFoundException {
                    return n.equals(TARGET) ? hidden.loadClass(n) : super.loadClass(n, resolve);
                }
            };
            actual = loader.loadClass(TARGET);
        } else {
            String path = a;
            if (name.equals("multi_dex")) path = new File(root, "multi.jar").toString();
            if (name.equals("whole_class_override")) path = b + File.pathSeparator + a;
            if (name.equals("export_completeness")) path = a + File.pathSeparator + same;
            loader = new PathClassLoader(path, system);
            actual = loader.loadClass(TARGET);
        }
        String observed = marker(actual);
        if (!observed.equals(expected)) throw new AssertionError(name + ": " + observed + " != " + expected);
        if (name.equals("multi_dex")) {
            if (!marker(loader.loadClass("probe.fixture.Extra")).equals("Extra")) throw new AssertionError("second dex missing");
        }
        JSONObject result = new JSONObject().put("caseName", name).put("expectedMarker", expected).put("observedMarker", observed);
        JSONArray topology = new JSONArray();
        for (ClassLoader l = loader; l != null; l = l.getParent()) topology.put(l.getClass().getName());
        result.put("loaderTopology", topology).put("actualDefiningLoader", actual.getClassLoader().getClass().getName());
        result.put("originEvidence", new JSONObject());
        result.put("status", "UNSUPPORTED").put("reason", "Retained APIs expose descriptor lookup, not a Class/Method-to-live-DEX origin proof");
        JSONArray inspections = new JSONArray();
        if (!name.equals("unknown_loader")) {
            for (boolean memory : new boolean[]{false, true}) inspections.put(inspect(loader, actual, new File(work, memory ? "memory" : "disk"), memory));
            result.put("exportedDexSha256", inspections.getJSONObject(1).getJSONArray("exportedDexSha256"));
        } else {
            result.put("exportedDexSha256", new JSONArray());
            result.put("reason", "Custom loadClass delegation is not modeled; rejected before native loader scanning");
        }
        if (name.equals("whole_class_override")) {
            boolean absent = false;
            try { actual.getMethod("oldOnly"); } catch (NoSuchMethodException e) { absent = true; }
            if (!absent) throw new AssertionError("old method resurrected");
            result.put("oldMethodAbsent", true);
        }
        return result.put("inspections", inspections);
    }

    public static void main(String[] args) throws Exception {
        File root = new File(args[0]);
        System.setProperty("java.io.tmpdir", root.toString());
        ClassLoader module = new PathClassLoader(args[1], ClassLoader.getSystemClassLoader());
        ApplicationInfo info = new ApplicationInfo();
        info.packageName = "io.github.nku100.impad"; info.sourceDir = args[1]; info.publicSourceDir = args[1];
        Object runtime = singleton(module, "io.github.nku100.impad.runtime.AndroidDexKitRuntime");
        runtime.getClass().getMethod("load", ApplicationInfo.class, ClassLoader.class).invoke(runtime, info, module);
        bridgeType = module.loadClass("org.luckypray.dexkit.DexKitBridge");
        JSONObject fixtures = new JSONObject(new String(Files.readAllBytes(new File(root, "fixtures.json").toPath()), "UTF-8"));
        JSONArray cases = fixtures.getJSONArray("cases"), results = new JSONArray();
        for (int i = 0; i < cases.length(); i++) {
            JSONObject c = cases.getJSONObject(i);
            String name = c.getString("name");
            try { results.put(run(name, c.getString("expectedMarker"), root)); }
            catch (Throwable t) {
                t.printStackTrace();
                results.put(new JSONObject().put("caseName", name).put("expectedMarker", c.getString("expectedMarker"))
                    .put("observedMarker", JSONObject.NULL).put("exportedDexSha256", new JSONArray()).put("loaderTopology", new JSONArray())
                    .put("originEvidence", new JSONObject()).put("status", "ERROR").put("reason", t.toString()));
            }
        }
        JSONObject report = new JSONObject().put("caseResults", results).put("api", Build.VERSION.SDK_INT).put("abi", Build.SUPPORTED_ABIS[0]);
        Files.write(new File(root, "device-report.json").toPath(), report.toString(2).getBytes("UTF-8"));
        System.out.println("EFFECTIVE_CODE_PROBE_FINISHED cases=" + results.length());
    }
}
