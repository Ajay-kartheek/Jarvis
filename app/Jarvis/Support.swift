// Where the app keeps things, its log, and its settings file.

import Foundation

/// A second copy for testing, started with JARVIS_TEST=1 next to the installed
/// one. It keeps its own lock, logs, window positions and shortcut
/// (Control+Option+K), takes its settings only from $JARVIS_APP_CONFIG, never
/// asks for permissions, and can be driven without a keyboard or a mouse:
///   SIGUSR1  the shortcut goes down        SIGUSR2  the shortcut comes up
///   SIGHUP   "Open Dashboard" chosen in the status menu
///   SIGURG   the dashboard's close button pressed
///   SIGINFO  write what the app is doing to the log
/// Other settings only the test copy reads (see `text`, `flag` and `seconds`):
///   JARVIS_VOICE_MAX_SECONDS  how long a spoken message may last (120 otherwise)
///   JARVIS_INPUT_DEVICE       part of the name of the microphone to use, whatever voice_input says
///   JARVIS_TEST_MIC=ramp      a counting signal instead of the microphone
///   JARVIS_TEST_DEFAULT_INPUT part of the name of the device to see as the default input
///   JARVIS_TEST_BLUETOOTH=1   treat the default input as a Bluetooth one
///   JARVIS_TEST_NO_BUILTIN=1  pretend this Mac has no built-in microphone
///   JARVIS_TEST_NO_OPEN=1     links that would open in the browser are only logged
/// That way it cannot get in the way of the copy the user relies on.
enum TestMode {
  static let on = ProcessInfo.processInfo.environment["JARVIS_TEST"] == "1"
  /// Added to file names so they differ from the installed copy's.
  static let fileSuffix = on ? "-test" : ""
  /// Added to the names macOS saves window and menu-bar positions under.
  static let nameSuffix = on ? "Test" : ""

  /// A setting from the environment, honoured by the test copy only.
  static func text(_ name: String) -> String? {
    guard on, let value = ProcessInfo.processInfo.environment[name], !value.isEmpty else {
      return nil
    }
    return value
  }

  static func flag(_ name: String) -> Bool { text(name) == "1" }

  /// A number of seconds from the environment, honoured by the test copy only.
  static func seconds(_ name: String) -> TimeInterval? {
    guard let text = text(name), let value = TimeInterval(text), value > 0 else { return nil }
    return value
  }
}

enum Paths {
  static let home = FileManager.default.homeDirectoryForCurrentUser

  static var support: URL {
    home.appendingPathComponent("Library/Application Support/Jarvis", isDirectory: true)
  }
  static var logs: URL { home.appendingPathComponent("Library/Logs/Jarvis", isDirectory: true) }
  static var appLog: URL { logs.appendingPathComponent("app\(TestMode.fileSuffix).log") }
  static var daemonLog: URL { logs.appendingPathComponent("daemon\(TestMode.fileSuffix).log") }
  static var lock: URL { support.appendingPathComponent("app\(TestMode.fileSuffix).lock") }

  /// The settings file: $JARVIS_APP_CONFIG when set, otherwise app.json in the data folder.
  /// The test copy reads only $JARVIS_APP_CONFIG: without it there is no file, so no settings.
  static var config: URL? {
    if let override = ProcessInfo.processInfo.environment["JARVIS_APP_CONFIG"], !override.isEmpty {
      return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
    }
    return TestMode.on ? nil : support.appendingPathComponent("app.json")
  }

  static func ensure(_ folder: URL) {
    try? FileManager.default.createDirectory(
      at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
  }

  /// Opens a log for appending, owner-only, and not passed on to child processes.
  /// A log that has grown past 5 MB is set aside as "<name>.1" first.
  static func openLog(_ file: URL) -> FileHandle? {
    ensure(logs)
    let attributes = try? FileManager.default.attributesOfItem(atPath: file.path)
    if let size = attributes?[.size] as? Int, size > 5_000_000 {
      let older = file.appendingPathExtension("1")
      try? FileManager.default.removeItem(at: older)
      try? FileManager.default.moveItem(at: file, to: older)
    }
    let descriptor = open(file.path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0o600)
    guard descriptor >= 0 else { return nil }
    return FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
  }
}

enum Log {
  private static let queue = DispatchQueue(label: "jarvis.app.log")
  private static let handle = Paths.openLog(Paths.appLog)
  private static let toTerminal = isatty(STDERR_FILENO) != 0
  private static let stamp: DateFormatter = {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
    return formatter
  }()

  static func write(_ message: String) {
    let now = Date()
    queue.async {
      let line = "\(stamp.string(from: now)) \(message)\n"
      if let data = line.data(using: .utf8) { handle?.write(data) }
      if toTerminal { fputs(line, stderr) }
    }
  }

  /// Blocks until everything written so far is in the file.
  static func flush() { queue.sync {} }
}

/// Which microphone a spoken message is recorded from: "voice_input" in app.json. It is about
/// talking to Jarvis only; recording a call always uses the system's default input.
enum VoiceInput: String {
  /// The built-in microphone when the default input is a Bluetooth one and this Mac has a
  /// built-in microphone to use (opened at once, and the earphones stay as they are);
  /// otherwise the default input.
  case auto
  /// Always the system's default input.
  case system = "default"

  /// What app.json says. Missing means "auto"; so does anything not understood, with a note.
  init(setting: Any?) {
    guard let setting else {
      self = .auto
      return
    }
    switch (setting as? String)?.lowercased() {
    case "auto": self = .auto
    case "default": self = .system
    default:
      Log.write("voice_input \(setting) is not \"auto\" or \"default\"; using \"auto\"")
      self = .auto
    }
  }
}

struct AppConfig: Equatable {
  /// The command that runs the Python daemon, program first.
  var daemon: [String]
  var cwd: String?
  var port: Int
  /// Bundle-identifier prefixes of the apps whose microphone use counts as a call.
  var callApps: [String]
  var hideFromScreenShare: Bool
  /// Ask for the microphone and system-audio permissions at start-up.
  var prompts: Bool
  /// Which microphone talking to Jarvis uses.
  var voiceInput: VoiceInput

  static let defaultPort = 8765
  static let defaultCallApps = [
    "com.microsoft.teams", "us.zoom.xos", "com.cisco.webexmeetingsapp", "com.webex.meetingmanager",
    "com.tinyspeck.slackmacgap", "com.apple.FaceTime", "net.whatsapp.WhatsApp", "com.hnc.Discord",
    "com.google.Chrome", "com.apple.Safari", "com.microsoft.edgemac", "company.thebrowser.Browser",
    "org.mozilla.firefox", "com.brave.Browser",
  ]

  /// nil when the file is missing or does not name a daemon command.
  static func load() -> AppConfig? {
    guard let file = Paths.config, let data = try? Data(contentsOf: file) else { return nil }
    guard let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
      let daemon = raw["daemon"] as? [String], !daemon.isEmpty
    else {
      Log.write("\(file.path) is not usable: it needs a \"daemon\" command list")
      return nil
    }
    var port = raw["port"] as? Int
    if port == nil, let flag = daemon.firstIndex(of: "--port"), flag + 1 < daemon.count {
      port = Int(daemon[flag + 1])
    }
    let callApps = (raw["call_apps"] as? [String])?.filter { !$0.isEmpty }
    return AppConfig(
      daemon: daemon,
      cwd: raw["cwd"] as? String,
      port: port ?? defaultPort,
      callApps: callApps?.isEmpty == false ? callApps! : defaultCallApps,
      hideFromScreenShare: raw["hide_from_screen_share"] as? Bool ?? true,
      prompts: raw["prompts"] as? Bool ?? true,
      voiceInput: VoiceInput(setting: raw["voice_input"]))
  }
}
