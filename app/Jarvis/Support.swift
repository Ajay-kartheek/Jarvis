// Where the app keeps things, its log, and its settings file.

import Foundation

enum Paths {
  static let home = FileManager.default.homeDirectoryForCurrentUser

  static var support: URL {
    home.appendingPathComponent("Library/Application Support/Jarvis", isDirectory: true)
  }
  static var logs: URL { home.appendingPathComponent("Library/Logs/Jarvis", isDirectory: true) }
  static var appLog: URL { logs.appendingPathComponent("app.log") }
  static var daemonLog: URL { logs.appendingPathComponent("daemon.log") }
  static var lock: URL { support.appendingPathComponent("app.lock") }

  /// The settings file: $JARVIS_APP_CONFIG when set, otherwise app.json in the data folder.
  static var config: URL {
    if let override = ProcessInfo.processInfo.environment["JARVIS_APP_CONFIG"], !override.isEmpty {
      return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
    }
    return support.appendingPathComponent("app.json")
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

  static let defaultPort = 8765
  static let defaultCallApps = [
    "com.microsoft.teams", "us.zoom.xos", "com.cisco.webexmeetingsapp", "com.webex.meetingmanager",
    "com.tinyspeck.slackmacgap", "com.apple.FaceTime", "net.whatsapp.WhatsApp", "com.hnc.Discord",
    "com.google.Chrome", "com.apple.Safari", "com.microsoft.edgemac", "company.thebrowser.Browser",
    "org.mozilla.firefox", "com.brave.Browser",
  ]

  /// nil when the file is missing or does not name a daemon command.
  static func load() -> AppConfig? {
    guard let data = try? Data(contentsOf: Paths.config) else { return nil }
    guard let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
      let daemon = raw["daemon"] as? [String], !daemon.isEmpty
    else {
      Log.write("\(Paths.config.path) is not usable: it needs a \"daemon\" command list")
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
      prompts: raw["prompts"] as? Bool ?? true)
  }
}
