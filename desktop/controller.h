/* Shared latency controller. Times are monotonic host microseconds.
 * Callers serialize access. Capture is gated BEFORE encoding: dropping an
 * encoded inter frame would break the decoder's reference chain. */
#ifndef FRAME_CONTROLLER_H
#define FRAME_CONTROLLER_H
#include <stdint.h>
#ifdef _WIN32
#define FC_API __declspec(dllexport)
#else
#define FC_API
#endif
#ifdef __cplusplus
extern "C" {
#endif
typedef struct FCController FCController;
FC_API FCController *fc_new(int fps, int enabled);
FC_API void fc_free(FCController *c);
FC_API void fc_ceiling(FCController *c, int bps);
FC_API int fc_gate(FCController *c, int64_t now, int counts);
FC_API void fc_capture(FCController *c, int64_t now);
FC_API void fc_sent(FCController *c, uint32_t seq, int bytes, int64_t now);
FC_API int fc_ack(FCController *c, uint32_t seq, int64_t now);
FC_API int fc_update(FCController *c, int64_t now);
/* target, ceiling, tier, fps, scale percent, baseline us, in flight, slack us */
FC_API int64_t fc_value(FCController *c, int field);
#ifdef __cplusplus
}
#endif
#endif
