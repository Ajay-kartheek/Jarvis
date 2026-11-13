// Which call is this?
//
// While a call is on, the titles of the call app's windows say what the call is:
// "Weekly sync | Microsoft Teams", "Meet - abc-defg-hij - Google Chrome". Reading them takes
// the Accessibility permission (System Settings, Privacy & Security, Accessibility). Without it
// nothing is read and the daemon is told so; everything else works as before.

import AppKit
import ApplicationServices

enum WindowTitles {
  /// What was read from one app's windows.
  struct Reading: Equatable {
    /// The title of the focused window, or else of the main one; nil when neither has a title.
    var title: String?
    /// Up to `most` distinct, non-empty titles, `title` first.
    var titles: [String]

    static let empty = Reading(title: nil, titles: [])
  }

  static let most = 6
  private static let longest = 200  // a title longer than this is cut

  /// Test copy only: a JSON file standing in for what the Accessibility API would say, e.g.
  /// {"focused": "Weekly sync | Teams", "main": "Inbox", "windows": ["Chat", ""],
  ///  "accessibility": "granted"}. It goes through the same choosing of titles as a real reading.
  private static var fakeFile: String? { TestMode.text("JARVIS_FAKE_TITLES") }

  private static func fake() -> [String: Any]? {
    guard let path = fakeFile, let data = FileManager.default.contents(atPath: path) else {
      return nil
    }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
  }

  /// Whether this app may read the windows of other apps.
  static var allowed: Bool {
    if let fake = fake() { return (fake["accessibility"] as? String ?? "granted") == "granted" }
    return AXIsProcessTrusted()
  }

  /// Shows macOS's own Accessibility prompt. The caller does this once per launch, and not at
  /// all when prompts are off. (The key is the documented value of kAXTrustedCheckOptionPrompt.)
  static func ask() {
    _ = AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true] as CFDictionary)
  }

  /// Chooses what to report from what the windows say: the focused window's title, then the main
  /// window's, then the others in order; blanks and repeats left out; at most `most`.
  static func arrange(focused: String?, main: String?, windows: [String?]) -> Reading {
    func clean(_ raw: String?) -> String? {
      guard let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty
      else { return nil }
      return trimmed.count > longest ? String(trimmed.prefix(longest)) : trimmed
    }
    var titles: [String] = []
    for candidate in [focused, main] + windows {
      guard let title = clean(candidate), !titles.contains(title), titles.count < most else {
        continue
      }
      titles.append(title)
    }
    return Reading(title: clean(focused) ?? clean(main), titles: titles)
  }

  /// The windows of the app with this process number. nil when they cannot be read: no
  /// permission, the app is gone, or it does not answer.
  static func read(pid: pid_t) -> Reading? {
    if let fake = fake() {
      guard (fake["accessibility"] as? String ?? "granted") == "granted" else { return nil }
      return arrange(
        focused: fake["focused"] as? String, main: fake["main"] as? String,
        windows: (fake["windows"] as? [String]) ?? [])
    }
    guard AXIsProcessTrusted() else { return nil }
    return readWindows(of: pid)
  }

  /// The Accessibility reading itself, without asking whether this app is trusted first. (An app
  /// may be allowed to read its own windows when it is not trusted to read other apps'.)
  static func readWindows(of pid: pid_t) -> Reading? {
    let app = AXUIElementCreateApplication(pid)
    // An app that does not answer must not hold us up for the system's default of six seconds.
    AXUIElementSetMessagingTimeout(app, 1)
    var value: CFTypeRef?
    switch AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) {
    case .success: break
    case .noValue, .attributeUnsupported: return .empty  // readable, but it has no windows
    default: return nil  // no permission, gone, or not answering
    }
    let windows = (value as? [AXUIElement]) ?? []
    return arrange(
      focused: title(of: window(of: app, kAXFocusedWindowAttribute)),
      main: title(of: window(of: app, kAXMainWindowAttribute)),
      windows: windows.prefix(40).map { title(of: $0) })
  }

  private static func window(of app: AXUIElement, _ attribute: String) -> AXUIElement? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, attribute as CFString, &value) == .success,
      let value, CFGetTypeID(value) == AXUIElementGetTypeID()
    else { return nil }
    return unsafeBitCast(value, to: AXUIElement.self)
  }

  private static func title(of window: AXUIElement?) -> String? {
    guard let window else { return nil }
    AXUIElementSetMessagingTimeout(window, 1)
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &value) == .success
    else { return nil }
    return value as? String
  }
}

/// The process number of the running app with this bundle identifier, if there is one.
func runningPid(bundleID: String) -> pid_t? {
  NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
    .first { !$0.isTerminated }?.processIdentifier
}

/// For `Jarvis --list-windows`: the window titles of each app that has audio running, as the
/// daemon would be told them during a call. Nothing here asks for the permission.
func printWindowTable() {
  let owners = OwnerResolver()
  // (JARVIS_FAKE_AUDIO stands in for the table here as it does for the call monitor.)
  let table =
    ProcessInfo.processInfo.environment["JARVIS_FAKE_AUDIO"].map(CallMonitor.readTable)
    ?? AudioProcesses.list(owners: owners)
  let running = table.filter { $0.input || $0.output }
  func pad(_ text: String, _ width: Int) -> String {
    text.padding(toLength: max(width, text.count), withPad: " ", startingAt: 0)
  }
  print("accessibility: \(WindowTitles.allowed ? "granted" : "denied")")
  print("\(pad("APP", 44)) \(pad("PID", 7)) \(pad("AUDIO", 10)) WINDOWS")
  var seen = Set<String>()
  for process in running.sorted(by: { $0.pid < $1.pid }) {
    let app = process.owner.isEmpty ? process.bundleID : process.owner
    guard !app.isEmpty, seen.insert(app).inserted else { continue }
    let audio = running.filter { ($0.owner.isEmpty ? $0.bundleID : $0.owner) == app }
    let kinds = [audio.contains { $0.input } ? "in" : nil, audio.contains { $0.output } ? "out" : nil]
      .compactMap { $0 }.joined(separator: "+")
    let pid = owners.ownerPid(of: process.pid) ?? runningPid(bundleID: app)
    let windows: String
    if !WindowTitles.allowed {
      windows = "no accessibility"
    } else if let pid, let reading = WindowTitles.read(pid: pid) {
      windows =
        reading.titles.isEmpty
        ? "no windows" : reading.titles.map { "\"\($0)\"" }.joined(separator: " | ")
    } else {
      windows = "not readable"
    }
    print("\(pad(app, 44)) \(pad(pid.map(String.init) ?? "-", 7)) \(pad(kinds, 10)) \(windows)")
  }
}
