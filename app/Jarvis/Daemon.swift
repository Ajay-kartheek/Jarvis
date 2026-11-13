// The Python daemon runs as a child of the app
//
// Being the app's child matters: macOS then counts what the daemon does (and the
// audio helper it starts) as Jarvis, so the permissions the user gave the app
// cover it. The app keeps the daemon running and takes it down when it quits.

import Foundation

final class DaemonSupervisor {
  private static let backoff: [TimeInterval] = [1, 2, 5, 10, 30]
  private static let healthySeconds: TimeInterval = 60

  /// Called on the main queue each time the daemon process ends by itself.
  var onExit: (() -> Void)?

  private let config: AppConfig
  private var process: Process?
  // The daemon watches its standard input and exits when this pipe closes,
  // which also happens if the app dies without a chance to stop it.
  private var lifeline: Pipe?
  private var startedAt = Date.distantPast
  private var attempt = 0
  private var stopping = false
  private var pendingRestart: DispatchWorkItem?

  init(config: AppConfig) {
    self.config = config
  }

  func start() {
    stopping = false
    launch()
  }

  private func launch() {
    guard !stopping else { return }
    let child = Process()
    child.executableURL = URL(fileURLWithPath: config.daemon[0])
    child.arguments = Array(config.daemon.dropFirst())
    if let cwd = config.cwd {
      child.currentDirectoryURL = URL(fileURLWithPath: cwd, isDirectory: true)
    }
    var environment = ProcessInfo.processInfo.environment
    environment["JARVIS_APP"] = "1"
    environment["JARVIS_HELPER"] = Bundle.main.executablePath ?? CommandLine.arguments[0]
    // Started at login, the app has almost no environment of its own.
    environment["PATH"] =
      "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    if environment["LANG"] == nil { environment["LANG"] = "en_US.UTF-8" }
    environment["PYTHONUNBUFFERED"] = "1"  // so the daemon's log is written as things happen
    child.environment = environment

    let pipe = Pipe()
    child.standardInput = pipe
    if let log = Paths.openLog(Paths.daemonLog) {
      child.standardOutput = log
      child.standardError = log
    }
    child.terminationHandler = { [weak self] finished in
      DispatchQueue.main.async { self?.exited(finished) }
    }
    do {
      try child.run()
    } catch {
      Log.write("could not start the daemon (\(config.daemon[0])): \(error.localizedDescription)")
      scheduleRestart()
      return
    }
    process = child
    lifeline = pipe
    startedAt = Date()
    Log.write("daemon started (pid \(child.processIdentifier)): \(config.daemon.joined(separator: " "))")
  }

  private func exited(_ finished: Process) {
    guard finished === process else { return }
    let ran = Date().timeIntervalSince(startedAt)
    Log.write("daemon exited (status \(finished.terminationStatus)) after \(Int(ran)) s")
    process = nil
    lifeline = nil
    if ran >= DaemonSupervisor.healthySeconds { attempt = 0 }
    if !stopping { onExit?() }
    scheduleRestart()
  }

  private func scheduleRestart() {
    guard !stopping else { return }
    let delay = DaemonSupervisor.backoff[min(attempt, DaemonSupervisor.backoff.count - 1)]
    attempt += 1
    Log.write("starting the daemon again in \(Int(delay)) s")
    let work = DispatchWorkItem { [weak self] in self?.launch() }
    pendingRestart = work
    DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
  }

  /// Asks the daemon to stop and waits up to three seconds before killing it.
  func stop() {
    stopping = true
    pendingRestart?.cancel()
    guard let child = process, child.isRunning else { return }
    try? lifeline?.fileHandleForWriting.close()
    child.terminate()
    let deadline = Date().addingTimeInterval(3)
    while child.isRunning && Date() < deadline { usleep(50_000) }
    if child.isRunning {
      kill(child.processIdentifier, SIGKILL)
      Log.write("the daemon did not stop in time and was killed")
    } else {
      Log.write("daemon stopped")
    }
    process = nil
    lifeline = nil
  }
}
