#pragma once
#include <openxr/openxr.h>
#include <algorithm>
#include <string>
#include <vector>

namespace frame {
using Names = std::vector<std::string>;
inline constexpr const char* layerName = "XR_APILAYER_FRAME_compat";
inline constexpr const char* stubs[] = {"XR_KHR_android_thread_settings", "XR_OCULUS_android_session_state_enable"};
// OpenXR-Docs release-1.1.63 registry: promotedto="XR_VERSION_1_1".
inline constexpr const char* promotions[] = {
    "XR_KHR_locate_spaces", "XR_EXT_local_floor", "XR_EXT_uuid", "XR_EXT_palm_pose",
    "XR_VARJO_quad_views", "XR_EXT_samsung_odyssey_controller", "XR_EXT_hp_mixed_reality_controller",
    "XR_HTC_vive_cosmos_controller_interaction", "XR_HTC_vive_focus3_controller_interaction",
    "XR_ML_ml2_controller_interaction", "XR_FB_touch_controller_pro", "XR_META_touch_controller_plus",
    "XR_BD_controller_interaction", "XR_KHR_maintenance1"};
inline bool has(const Names& names, const std::string& n) {
    return std::find(names.begin(), names.end(), n) != names.end();
}
inline bool downgrade(XrVersion v) { return v >= XR_MAKE_VERSION(1, 1, 0); }
inline XrVersion runtimeVersion(XrVersion v) { return downgrade(v) ? XR_API_VERSION_1_0 : v; }
struct ExtensionPlan { Names downstream, stubbed, missing, added; };
inline ExtensionPlan extensions(const Names& requested, const Names& available, bool promote) {
    ExtensionPlan p;
    for (const auto& n : requested) {
        if (!has(available, n) && (n == stubs[0] || n == stubs[1])) p.stubbed.push_back(n);
        else {
            if (!has(p.downstream, n)) p.downstream.push_back(n);
            if (!has(available, n)) p.missing.push_back(n);
        }
    }
    if (promote) for (auto n : promotions) if (has(available, n) && !has(p.downstream, n)) {
        p.downstream.push_back(n); p.added.push_back(n);
    }
    return p;
}
// 1.1's grip_surface is XR_EXT_palm_pose's palm_ext renamed (same pose), not the grip pose.
inline std::string rewritePath(std::string p, bool palm) {
    const std::string suffix = "/input/grip_surface/pose";
    if (palm && (p == "/user/hand/left" + suffix || p == "/user/hand/right" + suffix))
        p.replace(p.size() - suffix.size(), suffix.size(), "/input/palm_ext/pose");
    return p;
}
// Profiles added to 1.1. The three legacy Meta splits have no 1.0 extension.
// The renamed Pro/Plus profiles have different component semantics; do not
// claim the old extension implements the new profile merely because it exists.
inline bool dropProfile(const std::string& p, const Names& enabled) {
    for (auto name : {"touch_pro_controller", "touch_plus_controller", "touch_controller_rift_cv1",
                      "touch_controller_quest_1_rift_s", "touch_controller_quest_2"})
        if (p == std::string("/interaction_profiles/meta/") + name) return true;
    const char* profiles[][2] = {
        {"samsung/odyssey_controller", "XR_EXT_samsung_odyssey_controller"},
        {"hp/mixed_reality_controller", "XR_EXT_hp_mixed_reality_controller"},
        {"htc/vive_cosmos_controller", "XR_HTC_vive_cosmos_controller_interaction"},
        {"htc/vive_focus3_controller", "XR_HTC_vive_focus3_controller_interaction"},
        {"ml/ml2_controller", "XR_ML_ml2_controller_interaction"},
        {"bytedance/pico_neo3_controller", "XR_BD_controller_interaction"},
        {"bytedance/pico4_controller", "XR_BD_controller_interaction"},
        {"bytedance/pico_g3_controller", "XR_BD_controller_interaction"}};
    for (auto& entry : profiles) if (p == std::string("/interaction_profiles/") + entry[0])
        return !has(enabled, entry[1]);
    return false;
}
inline XrStructureType extensionType(XrStructureType t) {
    switch (t) {
    case XR_TYPE_SPACES_LOCATE_INFO: return XR_TYPE_SPACES_LOCATE_INFO_KHR;
    case XR_TYPE_SPACE_LOCATIONS: return XR_TYPE_SPACE_LOCATIONS_KHR;
    case XR_TYPE_SPACE_VELOCITIES: return XR_TYPE_SPACE_VELOCITIES_KHR;
    default: return t;
    }
}
inline XrResult locate(PFN_xrLocateSpacesKHR next, XrSession session,
                       const XrSpacesLocateInfo* info, XrSpaceLocations* locations) {
    if (!info || !locations) return XR_ERROR_VALIDATION_FAILURE;
    static_assert(XR_TYPE_SPACE_VELOCITIES == XR_TYPE_SPACE_VELOCITIES_KHR);
    static_assert(XR_TYPE_SPACE_LOCATIONS == XR_TYPE_SPACE_LOCATIONS_KHR);
    static_assert(XR_TYPE_SPACES_LOCATE_INFO == XR_TYPE_SPACES_LOCATE_INFO_KHR);
    auto in = *info;
    auto out = *locations;
    in.type = extensionType(in.type); out.type = extensionType(out.type);
    // The enums and typedefs are exact aliases. All chained velocities,
    // including unknown next structures, can be forwarded without mutation.
    const auto result = next(session, &in, &out);
    locations->locationCount = out.locationCount;
    return result;
}
}
