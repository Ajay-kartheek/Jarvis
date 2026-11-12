#!/bin/bash
# Builds ears/build/JarvisEars.app with the Command Line Tools (no Xcode needed).
#
# The app is signed ad hoc, so macOS treats every rebuild as a new app and asks
# again for the microphone and system-audio permissions.
set -euo pipefail
cd "$(dirname "$0")"

APP=build/JarvisEars.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp JarvisEars/Info.plist "$APP/Contents/Info.plist"

swiftc -O -swift-version 5 -target arm64-apple-macos14.2 \
  -framework AVFoundation -framework AudioToolbox -framework CoreAudio \
  JarvisEars/main.swift -o "$APP/Contents/MacOS/JarvisEars"

codesign --force --sign - --identifier com.ajay.jarvis.ears "$APP"
echo "Built $(pwd)/$APP"
