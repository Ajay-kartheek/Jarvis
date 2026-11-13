#!/bin/bash
# Builds app/build/Jarvis.app with the Command Line Tools (no Xcode needed).
#
# macOS ties the microphone and system-audio permissions to how an app is
# signed. An ad hoc signature changes with every build, so every rebuild would
# ask for the permissions again. This script signs with a certificate of its
# own instead: made once, kept in a keychain of its own
# (~/.config/jarvis/signing.keychain-db), and reused, so the permissions stay.
# The login keychain is never touched.
set -euo pipefail
cd "$(dirname "$0")"

APP=build/Jarvis.app
IDENTIFIER=com.ajay.jarvis
IDENTITY="Jarvis Local Signing"
SECRETS="$HOME/.config/jarvis"
KEYCHAIN="$SECRETS/signing.keychain-db"
PASSWORD_FILE="$SECRETS/signing.pass"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Jarvis/Info.plist "$APP/Contents/Info.plist"

swiftc -O -swift-version 5 -target arm64-apple-macos14.2 \
  -framework AppKit -framework WebKit -framework Carbon \
  -framework AVFoundation -framework AudioToolbox -framework CoreAudio \
  Jarvis/*.swift -o "$APP/Contents/MacOS/Jarvis"

# Makes the certificate and its keychain. Runs once per Mac.
make_identity() {
  mkdir -p "$SECRETS" && chmod 700 "$SECRETS" || return 1
  rm -f "$KEYCHAIN" "$PASSWORD_FILE"
  local work password transport
  work=$(mktemp -d) || return 1
  password=$(/usr/bin/openssl rand -hex 24) || return 1
  transport=$(/usr/bin/openssl rand -hex 16) || return 1
  (umask 077 && printf '%s' "$password" >"$PASSWORD_FILE") || return 1

  cat >"$work/certificate.cnf" <<CONFIG
[req]
distinguished_name = subject
x509_extensions = usage
prompt = no
[subject]
CN = $IDENTITY
[usage]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CONFIG
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -config "$work/certificate.cnf" -keyout "$work/key.pem" -out "$work/certificate.pem" \
    2>/dev/null || return 1
  /usr/bin/openssl pkcs12 -export -inkey "$work/key.pem" -in "$work/certificate.pem" \
    -name "$IDENTITY" -out "$work/identity.p12" -passout "pass:$transport" || return 1

  security create-keychain -p "$password" "$KEYCHAIN" || return 1
  chmod 600 "$KEYCHAIN" || return 1
  security set-keychain-settings "$KEYCHAIN" || return 1 # no auto-lock
  security unlock-keychain -p "$password" "$KEYCHAIN" || return 1
  security import "$work/identity.p12" -k "$KEYCHAIN" -P "$transport" -T /usr/bin/codesign \
    >/dev/null || return 1
  # Lets codesign use the key without a prompt.
  security set-key-partition-list -S apple-tool:,apple: -s -k "$password" "$KEYCHAIN" \
    >/dev/null || return 1
  rm -rf "$work"
  echo "Made the signing certificate \"$IDENTITY\" in $KEYCHAIN"
}

# The user's keychain search list without ours, one path per element.
SEARCH_LIST=()
SEARCH_LIST_CHANGED=0
read_search_list() {
  SEARCH_LIST=()
  local line
  while IFS= read -r line; do
    line=${line#"${line%%[![:space:]]*}"} # trim leading spaces
    line=${line#\"}
    line=${line%\"}
    if [ -n "$line" ] && [ "$line" != "$KEYCHAIN" ]; then
      SEARCH_LIST+=("$line")
    fi
  done < <(security list-keychains -d user)
}

# codesign finds certificates through the search list, so our keychain joins
# it while signing and leaves again afterwards (creating a keychain adds it too).
restore_search_list() {
  if [ "$SEARCH_LIST_CHANGED" = 1 ]; then
    SEARCH_LIST_CHANGED=0
    security list-keychains -d user -s ${SEARCH_LIST[@]+"${SEARCH_LIST[@]}"}
  fi
}
trap restore_search_list EXIT

sign_with_certificate() {
  read_search_list
  SEARCH_LIST_CHANGED=1
  if [ ! -f "$KEYCHAIN" ] || [ ! -f "$PASSWORD_FILE" ]; then
    make_identity || return 1
  fi
  security unlock-keychain -p "$(cat "$PASSWORD_FILE")" "$KEYCHAIN" || return 1
  security list-keychains -d user -s "$KEYCHAIN" ${SEARCH_LIST[@]+"${SEARCH_LIST[@]}"} || return 1
  codesign --force --sign "$IDENTITY" --keychain "$KEYCHAIN" --identifier "$IDENTIFIER" "$APP" \
    || return 1
}

if sign_with_certificate; then
  echo "Signed with \"$IDENTITY\": macOS permissions carry over to the next build."
else
  echo "WARNING: could not sign with the Jarvis certificate; signed ad hoc instead." >&2
  echo "WARNING: macOS will ask for the microphone and system-audio permissions again." >&2
  codesign --force --sign - --identifier "$IDENTIFIER" "$APP"
fi
restore_search_list

codesign --verify --strict "$APP"
echo "Built $(pwd)/$APP"
