#include "compat_logic.h"
#include <loader_interfaces.h>
#include <jni.h>
#include <openxr/openxr_platform.h>
#include <android/log.h>
#include <cstring>
#include <memory>
#include <mutex>
#include <unordered_map>

#define LOG(...) __android_log_print(ANDROID_LOG_INFO, "FrameXrCompat", __VA_ARGS__)
#define GUARD_END catch (const std::bad_alloc&) { return XR_ERROR_OUT_OF_MEMORY; } catch (...) { return XR_ERROR_RUNTIME_FAILURE; }
namespace {
struct Dispatch {
    PFN_xrGetInstanceProcAddr gipa{};
    PFN_xrDestroyInstance destroy{};
    PFN_xrCreateSession createSession{};
    PFN_xrDestroySession destroySession{};
    PFN_xrLocateSpacesKHR locate{};
    PFN_xrStringToPath stringToPath{};
    PFN_xrPathToString pathToString{};
    PFN_xrSuggestInteractionProfileBindings suggest{};
    PFN_xrSetAndroidApplicationThreadKHR thread{};
    PFN_xrRequestDisplayRefreshRateFB refresh{};
    frame::Names enabled, stubbed;
    XrInstance instance{};
    bool promote{}, palm{};
};
std::mutex mutex;
std::unordered_map<XrInstance, std::shared_ptr<Dispatch>> instances;
std::unordered_map<XrSession, std::shared_ptr<Dispatch>> sessions;
// No runtime library is opened here: every call goes through the next layer.
PFN_xrEnumerateInstanceExtensionProperties enumerateNext{};
template<class T> T proc(PFN_xrGetInstanceProcAddr gipa, XrInstance i, const char* name) {
    PFN_xrVoidFunction f{};
    if (XR_FAILED(gipa(i, name, &f))) return nullptr;
    return reinterpret_cast<T>(f);
}
std::shared_ptr<Dispatch> get(XrInstance i) {
    std::lock_guard<std::mutex> lock(mutex);
    auto it = instances.find(i); return it == instances.end() ? nullptr : it->second;
}
std::shared_ptr<Dispatch> get(XrSession s) {
    std::lock_guard<std::mutex> lock(mutex);
    auto it = sessions.find(s); return it == sessions.end() ? nullptr : it->second;
}
XrResult properties(PFN_xrEnumerateInstanceExtensionProperties fn,
                    std::vector<XrExtensionProperties>& out) {
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    for (int attempt = 0; attempt < 4; ++attempt) {
        uint32_t n{};
        auto r = fn(nullptr, 0, &n, nullptr);
        if (XR_FAILED(r)) return r;
        out.assign(n, {XR_TYPE_EXTENSION_PROPERTIES, nullptr, {}, 0});
        if (!n) return XR_SUCCESS;
        r = fn(nullptr, n, &n, out.data());
        if (r == XR_ERROR_SIZE_INSUFFICIENT) continue;
        if (XR_FAILED(r)) return r;
        out.resize(n); return XR_SUCCESS;
    }
    return XR_ERROR_RUNTIME_FAILURE;
}
XrResult XRAPI_CALL enumerate(const char* layer, uint32_t capacity, uint32_t* count,
                              XrExtensionProperties* output) try {
    if (!count || (capacity && !output)) return XR_ERROR_VALIDATION_FAILURE;
    const bool own = layer && std::strcmp(layer, frame::layerName) == 0;
    PFN_xrEnumerateInstanceExtensionProperties next;
    { std::lock_guard<std::mutex> lock(mutex); next = enumerateNext; }
    if (layer && *layer && !own) return next ? next(layer, capacity, count, output) : XR_ERROR_API_LAYER_NOT_PRESENT;
    std::vector<XrExtensionProperties> all;
    if (!own && next) {
        auto r = properties(next, all); if (XR_FAILED(r)) return r;
    }
    for (unsigned i = 0; i < 2; ++i) {
        bool found = false;
        for (auto& e : all) if (!std::strcmp(e.extensionName, frame::stubs[i])) found = true;
        if (!found) {
            XrExtensionProperties p{XR_TYPE_EXTENSION_PROPERTIES, nullptr, {}, i == 0 ? XR_KHR_android_thread_settings_SPEC_VERSION : 1u};
            std::strcpy(p.extensionName, frame::stubs[i]); all.push_back(p);
        }
    }
    *count = static_cast<uint32_t>(all.size());
    LOG("enumerate extensions: %u (includes stubs)", *count);
    if (!capacity) return XR_SUCCESS;
    if (capacity < all.size()) return XR_ERROR_SIZE_INSUFFICIENT;
    for (size_t i = 0; i < all.size(); ++i) {
        if (output[i].type != XR_TYPE_EXTENSION_PROPERTIES) return XR_ERROR_VALIDATION_FAILURE;
        std::strcpy(output[i].extensionName, all[i].extensionName);
        output[i].extensionVersion = all[i].extensionVersion;
    }
    return XR_SUCCESS;
} GUARD_END
XrResult XRAPI_CALL destroyInstance(XrInstance i) try {
    auto d = get(i); if (!d) return XR_ERROR_HANDLE_INVALID;
    auto r = d->destroy(i);
    if (XR_SUCCEEDED(r)) {
        std::lock_guard<std::mutex> lock(mutex);
        for (auto it = sessions.begin(); it != sessions.end();) {
            if (it->second == d) it = sessions.erase(it); else ++it;
        }
        instances.erase(i);
        if (instances.empty()) enumerateNext = nullptr;
    }
    return r;
} GUARD_END
XrResult XRAPI_CALL createSession(XrInstance i, const XrSessionCreateInfo* info, XrSession* s) try {
    auto d = get(i); if (!d) return XR_ERROR_HANDLE_INVALID;
    auto r = d->createSession(i, info, s);
    if (XR_SUCCEEDED(r)) {
        try { std::lock_guard<std::mutex> lock(mutex); sessions.emplace(*s, d); }
        catch (...) { d->destroySession(*s); *s = XR_NULL_HANDLE; throw; }
    }
    return r;
} GUARD_END
XrResult XRAPI_CALL destroySession(XrSession s) try {
    auto d = get(s); if (!d) return XR_ERROR_HANDLE_INVALID;
    auto r = d->destroySession(s);
    if (XR_SUCCEEDED(r)) { std::lock_guard<std::mutex> lock(mutex); sessions.erase(s); }
    return r;
} GUARD_END
XrResult XRAPI_CALL locateSpaces(XrSession s, const XrSpacesLocateInfo* in, XrSpaceLocations* out) try {
    auto d = get(s); if (!d) return XR_ERROR_HANDLE_INVALID;
    if (!d->locate) return XR_ERROR_FUNCTION_UNSUPPORTED;
    LOG("xrLocateSpaces -> xrLocateSpacesKHR (core/KHR type aliases)");
    return frame::locate(d->locate, s, in, out);
} GUARD_END
XrResult XRAPI_CALL threadSettings(XrSession s, XrAndroidThreadTypeKHR type, uint32_t tid) try {
    auto d = get(s); if (!d) return XR_ERROR_HANDLE_INVALID;
    if (frame::has(d->stubbed, frame::stubs[0])) {
        LOG("stub xrSetAndroidApplicationThreadKHR type=%d tid=%u -> XR_SUCCESS (no scheduling change)", type, tid);
        return XR_SUCCESS;
    }
    return d->thread ? d->thread(s, type, tid) : XR_ERROR_FUNCTION_UNSUPPORTED;
} GUARD_END
// SteamVR offers only the current refresh rate; Quest apps ask for 72/90/120 Hz
// and abort on the error, so keep the current rate and report success.
XrResult XRAPI_CALL requestRefreshRate(XrSession s, float hz) try {
    auto d = get(s); if (!d) return XR_ERROR_HANDLE_INVALID;
    auto r = d->refresh(s, hz);
    if (r == XR_ERROR_DISPLAY_REFRESH_RATE_UNSUPPORTED_FB) {
        LOG("xrRequestDisplayRefreshRateFB %.1f Hz unsupported; keeping the current rate", hz);
        return XR_SUCCESS;
    }
    return r;
} GUARD_END
XrResult XRAPI_CALL stringToPath(XrInstance i, const char* path, XrPath* out) try {
    auto d = get(i); if (!d) return XR_ERROR_HANDLE_INVALID;
    if (!path) return XR_ERROR_VALIDATION_FAILURE;
    auto rewritten = frame::rewritePath(path, d->palm);
    if (rewritten != path) LOG("path %s -> %s", path, rewritten.c_str());
    return d->stringToPath(i, rewritten.c_str(), out);
} GUARD_END
XrResult pathString(const Dispatch& d, XrPath path, std::string& out) {
    uint32_t n{};
    auto r = d.pathToString(d.instance, path, 0, &n, nullptr);
    if (XR_FAILED(r)) return r;
    std::vector<char> text(n);
    r = d.pathToString(d.instance, path, n, &n, text.data());
    if (XR_SUCCEEDED(r)) out.assign(text.data());
    return r;
}
XrResult XRAPI_CALL suggest(XrInstance i, const XrInteractionProfileSuggestedBinding* info) try {
    auto d = get(i); if (!d) return XR_ERROR_HANDLE_INVALID;
    if (!info || (info->countSuggestedBindings && !info->suggestedBindings)) return XR_ERROR_VALIDATION_FAILURE;
    std::string profile;
    auto r = pathString(*d, info->interactionProfile, profile);
    if (XR_FAILED(r)) return r;
    if (d->promote && frame::dropProfile(profile, d->enabled)) {
        LOG("drop unsupported 1.1 interaction profile %s (%u suggestions)", profile.c_str(), info->countSuggestedBindings);
        return XR_SUCCESS;
    }
    auto copy = *info;
    std::vector<XrActionSuggestedBinding> bindings;
    for (uint32_t index = 0; index < info->countSuggestedBindings; ++index) {
        auto b = info->suggestedBindings[index];
        std::string path;
        r = pathString(*d, b.binding, path); if (XR_FAILED(r)) return r;
        auto rewritten = frame::rewritePath(path, d->palm);
        if (rewritten != path) {
            LOG("binding %s -> %s", path.c_str(), rewritten.c_str());
            r = d->stringToPath(i, rewritten.c_str(), &b.binding); if (XR_FAILED(r)) return r;
        }
        bindings.push_back(b);
    }
    copy.suggestedBindings = bindings.data();
    return d->suggest(i, &copy);
} GUARD_END
XrResult XRAPI_CALL gipa(XrInstance, const char*, PFN_xrVoidFunction*);
XrResult XRAPI_CALL createLayer(const XrInstanceCreateInfo* info, const XrApiLayerCreateInfo* layer,
                               XrInstance* instance) try {
    if (!info || !instance || !layer || layer->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_CREATE_INFO ||
        layer->structVersion != XR_API_LAYER_CREATE_INFO_STRUCT_VERSION || layer->structSize != sizeof(*layer) ||
        !layer->nextInfo || layer->nextInfo->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_NEXT_INFO ||
        layer->nextInfo->structVersion != XR_API_LAYER_NEXT_INFO_STRUCT_VERSION ||
        layer->nextInfo->structSize != sizeof(XrApiLayerNextInfo) ||
        !layer->nextInfo->nextGetInstanceProcAddr || !layer->nextInfo->nextCreateApiLayerInstance ||
        (info->enabledExtensionCount && !info->enabledExtensionNames)) return XR_ERROR_INITIALIZATION_FAILED;
    *instance = XR_NULL_HANDLE;
    auto next = layer->nextInfo->nextGetInstanceProcAddr;
    auto enumFn = proc<PFN_xrEnumerateInstanceExtensionProperties>(next, XR_NULL_HANDLE, "xrEnumerateInstanceExtensionProperties");
    std::vector<XrExtensionProperties> available;
    auto r = properties(enumFn, available); if (XR_FAILED(r)) { LOG("runtime extension enumeration failed: %d", r); return r; }
    frame::Names requested, supported;
    for (auto& p : available) supported.emplace_back(p.extensionName);
    for (uint32_t n = 0; n < info->enabledExtensionCount; ++n) {
        if (!info->enabledExtensionNames[n]) return XR_ERROR_VALIDATION_FAILURE;
        requested.emplace_back(info->enabledExtensionNames[n]);
    }
    auto d = std::make_shared<Dispatch>();
    d->promote = frame::downgrade(info->applicationInfo.apiVersion);
    auto plan = frame::extensions(requested, supported, d->promote);
    for (auto& n : plan.added) LOG("enable promoted extension %s", n.c_str());
    for (auto& n : plan.stubbed) LOG("emulate missing extension %s; remove downstream", n.c_str());
    for (auto& n : plan.missing) LOG("unsupported extension %s retained; runtime must reject", n.c_str());
    auto copy = *info;
    copy.applicationInfo.apiVersion = frame::runtimeVersion(info->applicationInfo.apiVersion);
    if (d->promote) LOG("API %u.%u.%u -> 1.0.%u", unsigned(XR_VERSION_MAJOR(info->applicationInfo.apiVersion)),
        unsigned(XR_VERSION_MINOR(info->applicationInfo.apiVersion)), unsigned(XR_VERSION_PATCH(info->applicationInfo.apiVersion)),
        unsigned(XR_VERSION_PATCH(copy.applicationInfo.apiVersion)));
    std::vector<const char*> names;
    for (auto& n : plan.downstream) names.push_back(n.c_str());
    copy.enabledExtensionCount = static_cast<uint32_t>(names.size()); copy.enabledExtensionNames = names.data();
    auto chain = *layer; chain.nextInfo = layer->nextInfo->next;
    r = layer->nextInfo->nextCreateApiLayerInstance(&copy, &chain, instance);
    if (XR_FAILED(r)) { LOG("downstream xrCreateInstance -> %d", r); return r; }
    d->gipa = next; d->instance = *instance;
    d->destroy = proc<PFN_xrDestroyInstance>(next, *instance, "xrDestroyInstance");
    // A successful runtime instance must expose xrDestroyInstance.
    if (!d->destroy) { LOG("invalid downstream: missing xrDestroyInstance"); return XR_ERROR_INITIALIZATION_FAILED; }
    try {
        d->enabled = std::move(plan.downstream); d->stubbed = std::move(plan.stubbed);
        d->palm = frame::has(d->enabled, "XR_EXT_palm_pose");
#define LOAD(field, name) d->field = proc<PFN_##name>(next, *instance, #name)
        LOAD(createSession, xrCreateSession); LOAD(destroySession, xrDestroySession);
        LOAD(stringToPath, xrStringToPath); LOAD(pathToString, xrPathToString);
        LOAD(suggest, xrSuggestInteractionProfileBindings);
        if (frame::has(d->enabled, "XR_KHR_locate_spaces")) LOAD(locate, xrLocateSpacesKHR);
        if (frame::has(d->enabled, frame::stubs[0])) LOAD(thread, xrSetAndroidApplicationThreadKHR);
        if (frame::has(d->enabled, "XR_FB_display_refresh_rate")) LOAD(refresh, xrRequestDisplayRefreshRateFB);
#undef LOAD
        if (!d->createSession || !d->destroySession || !d->stringToPath || !d->pathToString || !d->suggest) {
            d->destroy(*instance); *instance = XR_NULL_HANDLE; return XR_ERROR_INITIALIZATION_FAILED;
        }
        std::lock_guard<std::mutex> lock(mutex);
        instances.emplace(*instance, d); enumerateNext = enumFn;
    } catch (...) { d->destroy(*instance); *instance = XR_NULL_HANDLE; throw; }
    LOG("created instance; next-layer dispatch retained (Valve layer coexists)");
    return r;
} GUARD_END
XrResult XRAPI_CALL gipa(XrInstance i, const char* name, PFN_xrVoidFunction* out) try {
    if (!name || !out) return XR_ERROR_VALIDATION_FAILURE;
    *out = nullptr;
#define RETURN_PROC(n, fn) if (!std::strcmp(name, n)) { *out = reinterpret_cast<PFN_xrVoidFunction>(fn); return XR_SUCCESS; }
    RETURN_PROC("xrGetInstanceProcAddr", gipa)
    RETURN_PROC("xrCreateApiLayerInstance", createLayer)
    RETURN_PROC("xrEnumerateInstanceExtensionProperties", enumerate)
    auto d = get(i); if (!d) return XR_ERROR_HANDLE_INVALID;
    RETURN_PROC("xrDestroyInstance", destroyInstance)
    RETURN_PROC("xrCreateSession", createSession)
    RETURN_PROC("xrDestroySession", destroySession)
    RETURN_PROC("xrStringToPath", stringToPath)
    RETURN_PROC("xrSuggestInteractionProfileBindings", suggest)
    if (d->locate && d->promote) { RETURN_PROC("xrLocateSpaces", locateSpaces) }
    if (frame::has(d->stubbed, frame::stubs[0])) { RETURN_PROC("xrSetAndroidApplicationThreadKHR", threadSettings) }
    if (d->refresh) { RETURN_PROC("xrRequestDisplayRefreshRateFB", requestRefreshRate) }
#undef RETURN_PROC
    // Includes xrGetInstanceProperties: report the actual runtime's identity.
    return d->gipa(i, name, out);
} GUARD_END
} // namespace
extern "C" __attribute__((visibility("default"))) XrResult XRAPI_CALL xrNegotiateLoaderApiLayerInterface(
    const XrNegotiateLoaderInfo* loader, const char* name, XrNegotiateApiLayerRequest* request) {
    if (!loader || !name || !request || std::strcmp(name, frame::layerName) ||
        loader->structType != XR_LOADER_INTERFACE_STRUCT_LOADER_INFO ||
        loader->structVersion != XR_LOADER_INFO_STRUCT_VERSION || loader->structSize != sizeof(*loader) ||
        request->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST ||
        request->structVersion != XR_API_LAYER_INFO_STRUCT_VERSION || request->structSize != sizeof(*request) ||
        loader->minInterfaceVersion > 1 || loader->maxInterfaceVersion < 1 ||
        loader->minApiVersion > XR_CURRENT_API_VERSION || loader->maxApiVersion < XR_MAKE_VERSION(1, 1, 0))
        return XR_ERROR_INITIALIZATION_FAILED;
    request->layerInterfaceVersion = 1;
    request->layerApiVersion = std::min<XrVersion>(XR_CURRENT_API_VERSION, loader->maxApiVersion);
    request->getInstanceProcAddr = gipa; request->createApiLayerInstance = createLayer;
    LOG("negotiated interface 1, API 1.1");
    return XR_SUCCESS;
}
