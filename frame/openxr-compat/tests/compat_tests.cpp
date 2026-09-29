#include "compat_logic.h"
#include <cstdlib>
#include <iostream>
#include <type_traits>
#define CHECK(expr) do { if (!(expr)) { std::cerr << __LINE__ << ": " #expr "\n"; std::exit(1); } } while (false)
namespace {
void* sentinel = reinterpret_cast<void*>(0x1234);
XrResult XRAPI_CALL fakeLocate(XrSession, const XrSpacesLocateInfo* in, XrSpaceLocations* out) {
    CHECK(in->type == XR_TYPE_SPACES_LOCATE_INFO_KHR);
    CHECK(out->type == XR_TYPE_SPACE_LOCATIONS_KHR);
    CHECK(in->next == sentinel);
    auto* velocities = static_cast<XrSpaceVelocitiesKHR*>(out->next);
    CHECK(velocities->type == XR_TYPE_SPACE_VELOCITIES_KHR);
    CHECK(velocities->next == sentinel);
    CHECK(in->spaceCount == 1 && in->time == 123);
    out->locations[0].pose.position.x = 42;
    velocities->velocities[0].linearVelocity.y = 7;
    return XR_SUCCESS;
}
}
int main() {
    using namespace frame;
    CHECK(!downgrade(XR_MAKE_VERSION(1, 0, 99)));
    CHECK(runtimeVersion(XR_MAKE_VERSION(1, 0, 42)) == XR_MAKE_VERSION(1, 0, 42));
    CHECK(runtimeVersion(XR_MAKE_VERSION(1, 1, 0)) == XR_API_VERSION_1_0);
    CHECK(runtimeVersion(XR_MAKE_VERSION(1, 1, 999)) == XR_API_VERSION_1_0);
    CHECK(runtimeVersion(XR_MAKE_VERSION(2, 0, 0)) == XR_API_VERSION_1_0);
    Names available{"XR_KHR_locate_spaces", "XR_EXT_palm_pose", "XR_EXT_hand_interaction", "XR_VARJO_quad_views"};
    Names requested{stubs[0], stubs[1], "XR_MISSING_test", "XR_EXT_palm_pose"};
    auto p = extensions(requested, available, true);
    CHECK(p.stubbed.size() == 2 && p.missing == Names{"XR_MISSING_test"});
    CHECK(has(p.downstream, "XR_MISSING_test"));
    CHECK(has(p.downstream, "XR_KHR_locate_spaces") && has(p.downstream, "XR_VARJO_quad_views"));
    CHECK(!has(p.downstream, "XR_EXT_hand_interaction"));
    CHECK(!has(p.downstream, "XR_EXT_local_floor"));
    CHECK(std::count(p.downstream.begin(), p.downstream.end(), "XR_EXT_palm_pose") == 1);
    available.push_back(stubs[0]);
    CHECK(extensions(requested, available, true).stubbed == Names{stubs[1]});
    CHECK(extensions({}, available, false).downstream.empty());
    CHECK(rewritePath("/user/hand/left/input/grip_surface/pose", true) == "/user/hand/left/input/palm_ext/pose");
    CHECK(rewritePath("/user/hand/right/input/grip_surface/pose", true) == "/user/hand/right/input/palm_ext/pose");
    CHECK(rewritePath("/user/hand/left/input/grip_surface/pose", false) == "/user/hand/left/input/grip_surface/pose");
    CHECK(rewritePath("/user/hand/left/input/grip_surface/pose/extra", true) == "/user/hand/left/input/grip_surface/pose/extra");
    CHECK(rewritePath("/user/hand/left/input/grip/pose", true) == "/user/hand/left/input/grip/pose");
    CHECK(dropProfile("/interaction_profiles/meta/touch_pro_controller", {}));
    CHECK(dropProfile("/interaction_profiles/meta/touch_controller_quest_2", {}));
    CHECK(dropProfile("/interaction_profiles/hp/mixed_reality_controller", {}));
    CHECK(!dropProfile("/interaction_profiles/hp/mixed_reality_controller", {"XR_EXT_hp_mixed_reality_controller"}));
    CHECK(!dropProfile("/interaction_profiles/oculus/touch_controller", {}));
    CHECK(!dropProfile("/interaction_profiles/khr/simple_controller", {}));
    CHECK(!dropProfile("/interaction_profiles/valve/frame_controller", {}));
    CHECK(!dropProfile("/interaction_profiles/ext/hand_interaction_ext", {}));
    CHECK(extensionType(XR_TYPE_SPACES_LOCATE_INFO) == XR_TYPE_SPACES_LOCATE_INFO_KHR);
    CHECK(extensionType(XR_TYPE_SPACE_LOCATIONS) == XR_TYPE_SPACE_LOCATIONS_KHR);
    CHECK(extensionType(XR_TYPE_SPACE_VELOCITIES) == XR_TYPE_SPACE_VELOCITIES_KHR);
    CHECK(extensionType(XR_TYPE_INSTANCE_CREATE_INFO) == XR_TYPE_INSTANCE_CREATE_INFO);
    static_assert(std::is_same_v<XrSpaceVelocities, XrSpaceVelocitiesKHR>);
    XrSpace space{};
    XrSpaceLocationData data{};
    XrSpaceVelocityData velocity{};
    XrSpaceVelocities velocities{XR_TYPE_SPACE_VELOCITIES, sentinel, 1, &velocity};
    XrSpaceLocations locations{XR_TYPE_SPACE_LOCATIONS, &velocities, 1, &data};
    XrSpacesLocateInfo info{XR_TYPE_SPACES_LOCATE_INFO, sentinel, XR_NULL_HANDLE, 123, 1, &space};
    CHECK(locate(fakeLocate, XR_NULL_HANDLE, &info, &locations) == XR_SUCCESS);
    CHECK(data.pose.position.x == 42 && velocity.linearVelocity.y == 7);
    CHECK(info.next == sentinel && locations.next == &velocities && velocities.next == sentinel);
    CHECK(info.type == XR_TYPE_SPACES_LOCATE_INFO && locations.type == XR_TYPE_SPACE_LOCATIONS && velocities.type == XR_TYPE_SPACE_VELOCITIES);
    CHECK(locate(fakeLocate, XR_NULL_HANDLE, nullptr, &locations) == XR_ERROR_VALIDATION_FAILURE);
    std::cout << "All version, extension, path/profile and locate-chain checks passed\n";
}
