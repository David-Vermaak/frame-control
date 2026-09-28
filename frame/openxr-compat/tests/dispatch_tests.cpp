// Compile the actual layer with host-only JNI/logging shims, and a fake next
// layer. This exercises ABI routing without a runtime or a headset.
#include "../layer.cpp"
#include <cstdlib>
#include <iostream>
#define CHECK(expr) do { if (!(expr)) { std::cerr << __LINE__ << ": " #expr "\n"; std::exit(1); } } while (false)
namespace {
uintptr_t serial = 100;
frame::Names seenExtensions;
XrVersion seenVersion{};
XrApiLayerNextInfo* seenNext{};
int suggestionCalls{}, locateCalls{}, realThreadCalls{};
bool nativeThread{};
std::unordered_map<XrPath, std::string> paths;
XrResult XRAPI_CALL fakeEnumerate(const char*, uint32_t capacity, uint32_t* count, XrExtensionProperties* out) {
    const char* extensions[] = {"XR_KHR_locate_spaces", "XR_EXT_palm_pose", frame::stubs[0]};
    *count = nativeThread ? 3 : 2;
    if (!capacity) return XR_SUCCESS;
    if (capacity < *count) return XR_ERROR_SIZE_INSUFFICIENT;
    for (uint32_t j = 0; j < *count; ++j) { std::strcpy(out[j].extensionName, extensions[j]); out[j].extensionVersion = 1; }
    return XR_SUCCESS;
}
XrResult XRAPI_CALL fakeCreate(const XrInstanceCreateInfo* in, const XrApiLayerCreateInfo* layer, XrInstance* i) {
    seenVersion = in->applicationInfo.apiVersion; seenNext = layer->nextInfo;
    seenExtensions.clear();
    for (uint32_t j = 0; j < in->enabledExtensionCount; ++j) seenExtensions.emplace_back(in->enabledExtensionNames[j]);
    if (frame::has(seenExtensions, "XR_MISSING_test")) return XR_ERROR_EXTENSION_NOT_PRESENT;
    *i = reinterpret_cast<XrInstance>(++serial); return XR_SUCCESS;
}
XrResult XRAPI_CALL fakeDestroy(XrInstance) { return XR_SUCCESS; }
XrResult XRAPI_CALL fakeCreateSession(XrInstance, const XrSessionCreateInfo*, XrSession* s) {
    *s = reinterpret_cast<XrSession>(++serial); return XR_SUCCESS;
}
XrResult XRAPI_CALL fakeDestroySession(XrSession) { return XR_SUCCESS; }
XrResult XRAPI_CALL fakeString(XrInstance, const char* text, XrPath* p) {
    *p = ++serial; paths[*p] = text; return XR_SUCCESS;
}
XrResult XRAPI_CALL fakePath(XrInstance, XrPath p, uint32_t cap, uint32_t* count, char* out) {
    if (!paths.count(p)) return XR_ERROR_PATH_INVALID;
    *count = static_cast<uint32_t>(paths[p].size() + 1);
    if (!cap) return XR_SUCCESS;
    if (cap < *count) return XR_ERROR_SIZE_INSUFFICIENT;
    std::strcpy(out, paths[p].c_str()); return XR_SUCCESS;
}
XrResult XRAPI_CALL fakeSuggest(XrInstance, const XrInteractionProfileSuggestedBinding* info) {
    ++suggestionCalls;
    if (info->countSuggestedBindings) CHECK(paths[info->suggestedBindings[0].binding] == "/user/hand/left/input/palm_ext/pose");
    return XR_SUCCESS;
}
XrResult XRAPI_CALL fakeLocate(XrSession, const XrSpacesLocateInfo*, XrSpaceLocations*) { ++locateCalls; return XR_SUCCESS; }
XrResult XRAPI_CALL fakeThread(XrSession, XrAndroidThreadTypeKHR, uint32_t) { ++realThreadCalls; return XR_TIMEOUT_EXPIRED; }
XrResult XRAPI_CALL fakeProperties(XrInstance, XrInstanceProperties*) { return XR_SUCCESS; }
XrResult XRAPI_CALL fakeGipa(XrInstance, const char* n, PFN_xrVoidFunction* out) {
#define MAP(name, f) if (!std::strcmp(n, name)) { *out = reinterpret_cast<PFN_xrVoidFunction>(f); return XR_SUCCESS; }
    MAP("xrEnumerateInstanceExtensionProperties", fakeEnumerate)
    MAP("xrDestroyInstance", fakeDestroy)
    MAP("xrCreateSession", fakeCreateSession) MAP("xrDestroySession", fakeDestroySession)
    MAP("xrStringToPath", fakeString) MAP("xrPathToString", fakePath)
    MAP("xrSuggestInteractionProfileBindings", fakeSuggest) MAP("xrLocateSpacesKHR", fakeLocate)
    MAP("xrSetAndroidApplicationThreadKHR", fakeThread) MAP("xrGetInstanceProperties", fakeProperties)
#undef MAP
    *out = nullptr; return XR_ERROR_FUNCTION_UNSUPPORTED;
}
}
int main() {
    XrNegotiateLoaderInfo loader{XR_LOADER_INTERFACE_STRUCT_LOADER_INFO, 1, sizeof(XrNegotiateLoaderInfo), 1, 1,
                                XR_MAKE_VERSION(1,0,0), XR_MAKE_VERSION(1,1023,4095)};
    XrNegotiateApiLayerRequest request{};
    request.structType = XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST; request.structVersion = 1; request.structSize = sizeof(request);
    CHECK(xrNegotiateLoaderApiLayerInterface(&loader, frame::layerName, &request) == XR_SUCCESS);
    CHECK(request.getInstanceProcAddr && request.createApiLayerInstance);
    loader.maxApiVersion = XR_MAKE_VERSION(1,1,0);
    CHECK(xrNegotiateLoaderApiLayerInterface(&loader, frame::layerName, &request) == XR_SUCCESS);
    CHECK(request.layerApiVersion == loader.maxApiVersion);
    loader.maxApiVersion = XR_MAKE_VERSION(1,0,99);
    CHECK(xrNegotiateLoaderApiLayerInterface(&loader, frame::layerName, &request) == XR_ERROR_INITIALIZATION_FAILED);
    loader.maxApiVersion = XR_MAKE_VERSION(1,1023,4095);
    CHECK(xrNegotiateLoaderApiLayerInterface(&loader, "wrong", &request) == XR_ERROR_INITIALIZATION_FAILED);
    XrApiLayerNextInfo tail{};
    XrApiLayerNextInfo next{XR_LOADER_INTERFACE_STRUCT_API_LAYER_NEXT_INFO, 1, sizeof(XrApiLayerNextInfo), {}, fakeGipa, fakeCreate, &tail};
    XrApiLayerCreateInfo layer{XR_LOADER_INTERFACE_STRUCT_API_LAYER_CREATE_INFO, 1, sizeof(XrApiLayerCreateInfo), nullptr, {}, &next};
    XrInstanceCreateInfo info{XR_TYPE_INSTANCE_CREATE_INFO, nullptr, 0, {}, 0, nullptr, 2, frame::stubs};
    info.applicationInfo.apiVersion = XR_MAKE_VERSION(1,1,999);
    XrInstance a{}, b{};
    CHECK(createLayer(&info, &layer, &a) == XR_SUCCESS);
    CHECK(seenVersion == XR_API_VERSION_1_0 && info.applicationInfo.apiVersion == XR_MAKE_VERSION(1,1,999));
    CHECK(seenNext == &tail && layer.nextInfo == &next);
    CHECK(!frame::has(seenExtensions, frame::stubs[0]) && frame::has(seenExtensions, "XR_KHR_locate_spaces"));
    XrSession sa{}, sb{};
    CHECK(createSession(a, nullptr, &sa) == XR_SUCCESS);
    CHECK(threadSettings(sa, XR_ANDROID_THREAD_TYPE_APPLICATION_MAIN_KHR, 1) == XR_SUCCESS);
    nativeThread = true;
    CHECK(createLayer(&info, &layer, &b) == XR_SUCCESS);
    CHECK(frame::has(seenExtensions, frame::stubs[0]));
    CHECK(createSession(b, nullptr, &sb) == XR_SUCCESS);
    PFN_xrVoidFunction fn{};
    CHECK(gipa(b, "xrSetAndroidApplicationThreadKHR", &fn) == XR_SUCCESS);
    CHECK(reinterpret_cast<PFN_xrSetAndroidApplicationThreadKHR>(fn)(sb, XR_ANDROID_THREAD_TYPE_APPLICATION_MAIN_KHR, 1) == XR_TIMEOUT_EXPIRED);
    CHECK(realThreadCalls == 1);
    CHECK(threadSettings(sa, XR_ANDROID_THREAD_TYPE_APPLICATION_MAIN_KHR, 1) == XR_SUCCESS);
    CHECK(gipa(a, "xrGetInstanceProperties", &fn) == XR_SUCCESS && fn == reinterpret_cast<PFN_xrVoidFunction>(fakeProperties));
    CHECK(gipa(a, "xrLocateSpaces", &fn) == XR_SUCCESS);
    XrSpacesLocateInfo locateInfo{XR_TYPE_SPACES_LOCATE_INFO, nullptr, XR_NULL_HANDLE, 1, 0, nullptr};
    XrSpaceLocations locations{XR_TYPE_SPACE_LOCATIONS, nullptr, 0, nullptr};
    CHECK(reinterpret_cast<PFN_xrLocateSpaces>(fn)(sa, &locateInfo, &locations) == XR_SUCCESS && locateCalls == 1);
    XrPath binding{}, profile{};
    CHECK(stringToPath(a, "/user/hand/left/input/grip_surface/pose", &binding) == XR_SUCCESS);
    CHECK(paths[binding] == "/user/hand/left/input/palm_ext/pose");
    fakeString(a, "/user/hand/left/input/grip_surface/pose", &binding); // preexisting path: rewrite in suggest too
    fakeString(a, "/interaction_profiles/oculus/touch_controller", &profile);
    XrActionSuggestedBinding action{XR_NULL_HANDLE, binding};
    XrInteractionProfileSuggestedBinding suggested{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING, nullptr, profile, 1, &action};
    CHECK(suggest(a, &suggested) == XR_SUCCESS && suggestionCalls == 1);
    CHECK(paths[action.binding] == "/user/hand/left/input/grip_surface/pose");
    fakeString(a, "/interaction_profiles/meta/touch_pro_controller", &suggested.interactionProfile);
    CHECK(suggest(a, &suggested) == XR_SUCCESS && suggestionCalls == 1);
    uint32_t count{};
    CHECK(enumerate(frame::layerName, 0, &count, nullptr) == XR_SUCCESS && count == 2);
    XrExtensionProperties props[2]{{XR_TYPE_EXTENSION_PROPERTIES, nullptr, {}, 0}, {XR_TYPE_EXTENSION_PROPERTIES, nullptr, {}, 0}};
    CHECK(enumerate(frame::layerName, 1, &count, props) == XR_ERROR_SIZE_INSUFFICIENT);
    CHECK(enumerate(frame::layerName, 2, &count, props) == XR_SUCCESS && count == 2);
    CHECK(enumerate(nullptr, 0, &count, nullptr) == XR_SUCCESS && count == 4);
    const char* missing = "XR_MISSING_test";
    info.enabledExtensionCount = 1; info.enabledExtensionNames = &missing;
    XrInstance failed{};
    CHECK(createLayer(&info, &layer, &failed) == XR_ERROR_EXTENSION_NOT_PRESENT && failed == XR_NULL_HANDLE);
    CHECK(destroySession(sa) == XR_SUCCESS);
    CHECK(threadSettings(sa, XR_ANDROID_THREAD_TYPE_APPLICATION_MAIN_KHR, 1) == XR_ERROR_HANDLE_INVALID);
    CHECK(destroyInstance(a) == XR_SUCCESS);
    CHECK(get(sb) && !get(a));
    CHECK(destroyInstance(b) == XR_SUCCESS && !get(sb));
    CHECK(instances.empty() && sessions.empty() && enumerateNext == nullptr);
    std::cout << "Negotiation, next-layer chaining, multi-instance dispatch, stubs, forwarding and cleanup passed\n";
}
