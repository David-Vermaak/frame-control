#pragma once
// Host-only logcat replacement. Android builds use the NDK header and liblog.
#include <cstdarg>
#include <cstdio>
#define ANDROID_LOG_INFO 4
inline int __android_log_print(int, const char*, const char* fmt, ...) {
    va_list args; va_start(args, fmt); auto r = std::vfprintf(stderr, fmt, args);
    va_end(args); std::fputc('\n', stderr); return r;
}
