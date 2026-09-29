#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ndk=${ANDROID_NDK_HOME:-"$HOME/Library/Android/ndk/30.0.16248370"}
cmake -S "$here" -B "$here/build-android" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$ndk/build/cmake/android.toolchain.cmake" \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-24 \
  -DANDROID_STL=c++_static -DCMAKE_BUILD_TYPE=Release
cmake --build "$here/build-android"
mkdir -p "$here/prebuilt/arm64-v8a"
cp "$here/build-android/libXrApiLayer_FRAME_compat.so" "$here/prebuilt/arm64-v8a/"
shasum -a 256 "$here/prebuilt/arm64-v8a/libXrApiLayer_FRAME_compat.so"
