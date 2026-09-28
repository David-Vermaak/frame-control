/* Frame Control's OpenXR gaze source. No values on stdout/stderr or disk.
 * The Python bridge supplies a private pipe with --fd; standalone probes only
 * report counters. Uses a headless session, never submits frames or takes focus. */
#define XR_USE_TIMESPEC
#include <time.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t stopped;
static void stop(int sig) { (void)sig; stopped = 1; }
#define CHECK(call) do { result = (call); if (XR_FAILED(result)) { \
    fprintf(stderr, "%s failed (%d)\n", #call, result); goto cleanup; } } while (0)

int main(int argc, char **argv) {
    int seconds = 10, fd = -1, running = 0, rc = 1;
    unsigned active = 0, valid = 0, samples = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seconds") && i+1 < argc) seconds = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fd") && i+1 < argc) fd = atoi(argv[++i]);
        else { fprintf(stderr, "usage: gaze [--seconds 1..86400] [--fd private-pipe]\n"); return 2; }
    }
    if (seconds < 1 || seconds > 86400 || (fd != -1 && fd < 3)) return 2;
    FILE *out = fd == -1 ? NULL : fdopen(fd, "w");
    if (fd != -1 && !out) return 2;
    signal(SIGINT, stop); signal(SIGTERM, stop); signal(SIGHUP, stop); signal(SIGPIPE, SIG_IGN);
    XrResult result;
    XrInstance instance = XR_NULL_HANDLE;
    XrSession session = XR_NULL_HANDLE;
    XrActionSet set = XR_NULL_HANDLE;
    XrSpace gaze = XR_NULL_HANDLE, view = XR_NULL_HANDLE;
    const char *extensions[] = {"XR_EXT_eye_gaze_interaction", "XR_MND_headless", "XR_KHR_convert_timespec_time"};
    XrInstanceCreateInfo create = {.type = XR_TYPE_INSTANCE_CREATE_INFO};
    strcpy(create.applicationInfo.applicationName, "Frame Control gaze");
    create.applicationInfo.apiVersion = XR_MAKE_VERSION(1, 0, 0);
    create.enabledExtensionCount = 3; create.enabledExtensionNames = extensions;
    CHECK(xrCreateInstance(&create, &instance));
    XrSystemGetInfo get = {.type = XR_TYPE_SYSTEM_GET_INFO, .formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY};
    XrSystemId system;
    CHECK(xrGetSystem(instance, &get, &system));
    XrSystemEyeGazeInteractionPropertiesEXT eye = {.type = XR_TYPE_SYSTEM_EYE_GAZE_INTERACTION_PROPERTIES_EXT};
    XrSystemProperties props = {.type = XR_TYPE_SYSTEM_PROPERTIES, .next = &eye};
    CHECK(xrGetSystemProperties(instance, system, &props));
    printf("supportsEyeGazeInteraction=%u\n", eye.supportsEyeGazeInteraction);
    if (!eye.supportsEyeGazeInteraction) goto cleanup;
    XrSessionCreateInfo sc = {.type = XR_TYPE_SESSION_CREATE_INFO, .systemId = system};
    CHECK(xrCreateSession(instance, &sc, &session));
    XrActionSetCreateInfo asc = {.type = XR_TYPE_ACTION_SET_CREATE_INFO};
    strcpy(asc.actionSetName, "gaze"); strcpy(asc.localizedActionSetName, "Gaze");
    CHECK(xrCreateActionSet(instance, &asc, &set));
    XrActionCreateInfo ac = {.type = XR_TYPE_ACTION_CREATE_INFO, .actionType = XR_ACTION_TYPE_POSE_INPUT};
    strcpy(ac.actionName, "gaze_pose"); strcpy(ac.localizedActionName, "Gaze pose");
    XrAction action;
    CHECK(xrCreateAction(set, &ac, &action));
    XrPath profile, input;
    CHECK(xrStringToPath(instance, "/interaction_profiles/ext/eye_gaze_interaction", &profile));
    CHECK(xrStringToPath(instance, "/user/eyes_ext/input/gaze_ext/pose", &input));
    XrActionSuggestedBinding binding = {action, input};
    XrInteractionProfileSuggestedBinding suggested = {.type = XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING,
        .interactionProfile = profile, .countSuggestedBindings = 1, .suggestedBindings = &binding};
    CHECK(xrSuggestInteractionProfileBindings(instance, &suggested));
    XrSessionActionSetsAttachInfo attach = {.type = XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO, .countActionSets = 1, .actionSets = &set};
    CHECK(xrAttachSessionActionSets(session, &attach));
    XrActionSpaceCreateInfo space = {.type = XR_TYPE_ACTION_SPACE_CREATE_INFO, .action = action, .poseInActionSpace.orientation.w = 1};
    CHECK(xrCreateActionSpace(session, &space, &gaze));
    XrReferenceSpaceCreateInfo ref = {.type = XR_TYPE_REFERENCE_SPACE_CREATE_INFO, .referenceSpaceType = XR_REFERENCE_SPACE_TYPE_VIEW,
        .poseInReferenceSpace.orientation.w = 1};
    CHECK(xrCreateReferenceSpace(session, &ref, &view));
    PFN_xrConvertTimespecTimeToTimeKHR convert;
    CHECK(xrGetInstanceProcAddr(instance, "xrConvertTimespecTimeToTimeKHR", (PFN_xrVoidFunction *)&convert));
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);
    while (!stopped) {
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec - start.tv_sec >= seconds) break;
        XrEventDataBuffer event = {.type = XR_TYPE_EVENT_DATA_BUFFER};
        while ((result = xrPollEvent(instance, &event)) == XR_SUCCESS) {
            if (event.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
                XrSessionState state = ((XrEventDataSessionStateChanged *)&event)->state;
                printf("sessionState=%d\n", state); fflush(stdout);
                if (state == XR_SESSION_STATE_READY && !running) {
                    XrSessionBeginInfo begin = {.type = XR_TYPE_SESSION_BEGIN_INFO, .primaryViewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO};
                    CHECK(xrBeginSession(session, &begin)); running = 1;
                } else if (state == XR_SESSION_STATE_STOPPING) {
                    CHECK(xrEndSession(session)); running = 0; stopped = 1;
                } else if (state == XR_SESSION_STATE_EXITING || state == XR_SESSION_STATE_LOSS_PENDING) stopped = 1;
            } else if (event.type == XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING) stopped = 1;
            event.type = XR_TYPE_EVENT_DATA_BUFFER;
        }
        if (XR_FAILED(result)) goto cleanup;
        if (running && !stopped) {
            XrActiveActionSet activeSet = {set, XR_NULL_PATH};
            XrActionsSyncInfo sync = {.type = XR_TYPE_ACTIONS_SYNC_INFO, .countActiveActionSets = 1, .activeActionSets = &activeSet};
            CHECK(xrSyncActions(session, &sync));
            if (result != XR_SUCCESS) {
                struct timespec delay = {.tv_nsec = 33333333};
                nanosleep(&delay, NULL);
                continue; /* No stale gaze when the runtime denies focus. */
            }
            XrActionStateGetInfo ag = {.type = XR_TYPE_ACTION_STATE_GET_INFO, .action = action};
            XrActionStatePose pose = {.type = XR_TYPE_ACTION_STATE_POSE};
            CHECK(xrGetActionStatePose(session, &ag, &pose));
            samples++;
            if (pose.isActive) {
                active++;
                XrTime time; CHECK(convert(instance, &now, &time));
                XrSpaceLocation location = {.type = XR_TYPE_SPACE_LOCATION};
                CHECK(xrLocateSpace(gaze, view, time, &location));
                XrSpaceLocationFlags needed = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT;
                if ((location.locationFlags & needed) == needed) {
                    valid++;
                    if (out) {
                        XrQuaternionf q = location.pose.orientation;
                        if (fprintf(out, "%g %g %g %g\n", q.x, q.y, q.z, q.w) < 0 || fflush(out)) goto cleanup;
                    }
                }
            }
        }
        struct timespec delay = {.tv_nsec = 33333333}; nanosleep(&delay, NULL);
    }
    printf("samples=%u active=%u valid=%u\n", samples, active, valid);
    rc = valid ? 0 : 3; /* Distinguish a working session from observed gaze. */
cleanup:
    if (view) xrDestroySpace(view);
    if (gaze) xrDestroySpace(gaze);
    if (session) xrDestroySession(session);
    if (set) xrDestroyActionSet(set);
    if (instance) xrDestroyInstance(instance);
    if (out) fclose(out);
    return rc;
}
