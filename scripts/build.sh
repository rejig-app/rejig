#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
app="$project_dir/build/Rejig.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
xcrun swiftc -swift-version 6 -target arm64-apple-macosx15.0 -O -framework Cocoa -framework WebKit \
    "$project_dir/Sources/main.swift" -o "$app/Contents/MacOS/Rejig"
cp "$project_dir/scripts/smoke.js" "$app/Contents/Resources/smoke.js"
iconset="$project_dir/build/AppIcon.iconset"
mkdir -p "$iconset"
xcrun swift "$project_dir/scripts/icon.swift" "$project_dir/build/icon.png"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$project_dir/build/icon.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    doubled=$((size * 2))
    sips -z "$doubled" "$doubled" "$project_dir/build/icon.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Rejig</string>
<key>CFBundleIdentifier</key><string>app.rejig.mac</string>
<key>CFBundleName</key><string>Rejig</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>15.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSLocalNetworkUsageDescription</key><string>Rejig connects to your paired iPhone to read and apply your Home Screen layout over Wi-Fi.</string>
<key>NSBonjourServices</key><array><string>_remotepairing._tcp</string></array>
</dict></plist>
PLIST
codesign --force --sign - "$app"
"$app/Contents/MacOS/Rejig" --self-test
printf '%s\n' "$app"
