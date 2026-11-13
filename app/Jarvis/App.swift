// The menu-bar app
//
// Always running. It keeps the Python daemon alive, watches for calls, tells
// the daemon what it sees every two seconds, and shows the panel when
// something needs the user's eyes.

import AVFoundation
import AppKit

final class AppController: NSObject, NSApplicationDelegate, NSMenuDelegate {
  private static let heartbeatSeconds: TimeInterval = 2
  private static let audioPollSeconds: TimeInterval = 1.5
  private static let configCheckSeconds: TimeInterval = 5
  // A daemon that misses this many heartbeats in a row counts as down.
  private static let failuresBeforeDown = 3

  private var config: AppConfig?
  private var port = AppConfig.defaultPort
  private var statusItem: NSStatusItem!
  private var panel: PanelController!
  private var daemon: DaemonSupervisor?
  private let monitor = CallMonitor(callApps: AppConfig.defaultCallApps)
  private var hotkey: Hotkey?
  private var timers: [Timer] = []
  private var signalSources: [DispatchSourceSignal] = []
  private var activity: NSObjectProtocol?
  private let session: URLSession = {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = 3
    configuration.waitsForConnectivity = false
    return URLSession(configuration: configuration)
  }()

  // What the app last saw and sent.
  private var headphones = false
  private var microphone = "undetermined"
  private var systemAudio = "undetermined"
  private var lastBody = ""
  private var beats = 0
  private var sent = 0
  private var answered = 0

  // What the daemon last said.
  private var status = "idle"
  private var attention: Int?
  private var reachable = false
  private var failures = 0
  private var wentDown = false

  // MARK: Start and stop

  func applicationDidFinishLaunching(_ notification: Notification) {
    let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    Log.write("Jarvis \(version) starting (pid \(getpid()))")
    // Without this macOS slows the timers of an app that has no window open.
    activity = ProcessInfo.processInfo.beginActivity(
      options: [.userInitiatedAllowingIdleSystemSleep], reason: "Watching for calls")

    installMainMenu()
    installStatusItem()
    config = AppConfig.load()
    port = config?.port ?? AppConfig.defaultPort
    panel = PanelController(port: port, hideFromScreenShare: config?.hideFromScreenShare ?? true)
    panel.onQuit = { NSApp.terminate(nil) }
    monitor.onChange = { [weak self] active, _ in
      guard let self else { return }
      if active { self.panel.show(reason: "call", activate: false) }
      self.heartbeat()
    }

    let shortcut = Hotkey.controlOptionJ { [weak self] in self?.togglePanel(reason: "hotkey") }
    let registered = shortcut.register()
    Log.write(
      registered == noErr
        ? "shortcut Control+Option+J registered"
        : "could not register Control+Option+J (OSStatus \(registered)); another app may hold it")
    hotkey = shortcut
    installSignals()

    if let config {
      apply(config)
    } else {
      Log.write("no settings at \(Paths.config.path): not set up yet")
      every(AppController.configCheckSeconds) { [weak self] in self?.lookForConfig() }
    }
    panel.load()
    refreshPermissions()
    every(AppController.audioPollSeconds) { [weak self] in self?.pollAudio() }
    every(AppController.heartbeatSeconds) { [weak self] in self?.beat() }
    pollAudio()  // also sends the first report

    let quiet = ProcessInfo.processInfo.environment["JARVIS_NO_PROMPTS"] == "1"
    if config?.prompts != false && !quiet { requestPermissions() }
  }

  private func apply(_ config: AppConfig) {
    self.config = config
    port = config.port
    monitor.callApps = config.callApps
    panel.update(port: config.port, hideFromScreenShare: config.hideFromScreenShare)
    let supervisor = DaemonSupervisor(config: config)
    supervisor.onExit = { [weak self] in self?.daemonWentAway() }
    daemon = supervisor
    supervisor.start()
  }

  /// The page holds a live connection to the daemon, so it must load again
  /// once a new daemon answers, however quickly that one came up.
  private func daemonWentAway() {
    reachable = false
    wentDown = true
    status = "idle"
    attention = nil
    refreshStatusItem()
  }

  /// The settings file is written by `jarvis install`, possibly after the app started.
  private func lookForConfig() {
    guard config == nil, let found = AppConfig.load() else { return }
    Log.write("settings found at \(Paths.config.path)")
    apply(found)
  }

  private func every(_ seconds: TimeInterval, _ work: @escaping () -> Void) {
    let timer = Timer(timeInterval: seconds, repeats: true) { _ in work() }
    // Common modes keep it firing while a menu is open or the panel is dragged.
    RunLoop.main.add(timer, forMode: .common)
    timers.append(timer)
  }

  private func installSignals() {
    // launchd stops the app with SIGTERM; treat it like Quit so the daemon is stopped too.
    for number in [SIGTERM, SIGINT] {
      signal(number, SIG_IGN)
      let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
      source.setEventHandler { NSApp.terminate(nil) }
      source.resume()
      signalSources.append(source)
    }
    // `kill -USR1` does what the shortcut does; handy when testing.
    signal(SIGUSR1, SIG_IGN)
    let toggle = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
    toggle.setEventHandler { [weak self] in self?.togglePanel(reason: "hotkey") }
    toggle.resume()
    signalSources.append(toggle)
  }

  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    Log.write("quitting")
    timers.forEach { $0.invalidate() }
    daemon?.stop()
    Log.flush()
    return .terminateNow
  }

  // MARK: Permissions

  private func refreshPermissions() {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: microphone = "granted"
    case .notDetermined: microphone = "undetermined"
    default: microphone = "denied"
    }
    switch AudioCapturePermission.state() {
    case .allowed: systemAudio = "granted"
    case .denied: systemAudio = "denied"
    case .unknown: systemAudio = "undetermined"
    }
  }

  /// Ask once, at the start, so the user is not interrupted when the first call begins.
  private func requestPermissions() {
    DispatchQueue.global(qos: .userInitiated).async { [weak self] in
      if AudioCapturePermission.state() == .unknown {
        Log.write("asking for the system-audio permission")
        let answer = AudioCapturePermission.request()
        Log.write("system audio: \(answer.map { $0 ? "allowed" : "refused" } ?? "prompt unavailable")")
      }
      if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
        Log.write("asking for the microphone permission")
        Log.write("microphone: \(microphoneAllowed() ? "allowed" : "refused")")
      }
      DispatchQueue.main.async {
        self?.refreshPermissions()
        self?.heartbeat()
      }
    }
  }

  // MARK: Talking to the daemon

  private func pollAudio() {
    monitor.poll()
    headphones = OutputDevice.isHeadphones()
    if bodyText() != lastBody { heartbeat() }
  }

  private func beat() {
    beats += 1
    // Until both permissions are granted, look every time so the interface
    // updates as soon as the user answers; after that, now and then.
    if microphone != "granted" || systemAudio != "granted" || beats % 5 == 0 {
      refreshPermissions()
    }
    heartbeat()
  }

  private func bodyText() -> String {
    let callApp: Any = monitor.app ?? NSNull()
    let body: [String: Any] = [
      "call": ["active": monitor.active, "app": callApp],
      "headphones": headphones,
      "permissions": ["microphone": microphone, "system_audio": systemAudio],
    ]
    let data = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    return data.flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
  }

  private func heartbeat() {
    let body = bodyText()
    lastBody = body
    sent += 1
    let number = sent
    post("/api/app", body: body) { [weak self] reply in
      guard let self, number > self.answered else { return }  // an older answer arriving late
      self.answered = number
      self.heard(reply)
    }
  }

  private func post(_ path: String, body: String, then: (([String: Any]?) -> Void)? = nil) {
    guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return }
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = body.data(using: .utf8)
    session.dataTask(with: request) { data, response, _ in
      var reply: [String: Any]?
      if (response as? HTTPURLResponse)?.statusCode == 200, let data {
        reply = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
      }
      DispatchQueue.main.async { then?(reply) }
    }.resume()
  }

  private func heard(_ reply: [String: Any]?) {
    guard let reply else {
      failures += 1
      if failures == AppController.failuresBeforeDown {
        if reachable {
          Log.write("the daemon stopped answering")
          wentDown = true
        }
        reachable = false
        status = "idle"
        attention = nil
        refreshStatusItem()
      }
      return
    }
    failures = 0
    let newStatus = reply["status"] as? String ?? "idle"
    let newAttention = reply["attention"] as? Int ?? 0
    if !reachable {
      // First answer after starting or after a gap: take it as the baseline.
      reachable = true
      Log.write("the daemon is answering (status \(newStatus), attention \(newAttention))")
      if wentDown { panel.load() }
      wentDown = false
    } else {
      if status == "idle" && newStatus != "idle" && !panel.isVisible {
        panel.show(reason: "call", activate: false)
      }
      if let seen = attention, newAttention > seen {
        panel.show(reason: "attention", activate: false)
      }
    }
    if newStatus != status { Log.write("daemon status: \(newStatus)") }
    status = newStatus
    attention = newAttention
    refreshStatusItem()
  }

  // MARK: Menu bar

  private func installStatusItem() {
    statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    statusItem.autosaveName = "Jarvis"
    let menu = NSMenu()
    menu.delegate = self
    statusItem.menu = menu
    refreshStatusItem()
  }

  private func refreshStatusItem() {
    guard let button = statusItem.button else { return }
    let listening = reachable && status != "idle"
    let name = !reachable ? "waveform.slash" : listening ? "waveform.circle.fill" : "waveform"
    let image =
      NSImage(systemSymbolName: name, accessibilityDescription: "Jarvis")
      ?? NSImage(systemSymbolName: "waveform", accessibilityDescription: "Jarvis")
    image?.isTemplate = true
    button.image = image
    button.appearsDisabled = !reachable
    button.toolTip =
      !reachable ? "Jarvis is starting" : listening ? "Jarvis is listening" : "Jarvis is ready"
  }

  func menuNeedsUpdate(_ menu: NSMenu) {
    menu.removeAllItems()
    if config == nil {
      menu.addItem(withTitle: "Not set up yet", action: nil, keyEquivalent: "")
      menu.addItem(.separator())
    }
    let toggle = menu.addItem(
      withTitle: panel.isVisible ? "Hide Jarvis" : "Show Jarvis",
      action: #selector(togglePanelFromMenu), keyEquivalent: "j")
    toggle.keyEquivalentModifierMask = [.control, .option]
    toggle.target = self
    let listening = status != "idle"
    let listen = menu.addItem(
      withTitle: listening ? "Stop Listening" : "Listen Now",
      action: reachable ? #selector(toggleListening) : nil, keyEquivalent: "")
    listen.target = self
    menu.addItem(.separator())
    menu.addItem(withTitle: "Open Logs Folder", action: #selector(openLogs), keyEquivalent: "")
      .target = self
    menu.addItem(.separator())
    menu.addItem(
      withTitle: "Quit Jarvis", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
  }

  /// A hidden main menu, so copy, paste and undo work while typing in the panel.
  private func installMainMenu() {
    let main = NSMenu()
    let appItem = main.addItem(withTitle: "Jarvis", action: nil, keyEquivalent: "")
    let appMenu = NSMenu(title: "Jarvis")
    appMenu.addItem(withTitle: "Hide Jarvis", action: #selector(hidePanel), keyEquivalent: "w")
      .target = self
    appItem.submenu = appMenu
    let editItem = main.addItem(withTitle: "Edit", action: nil, keyEquivalent: "")
    let edit = NSMenu(title: "Edit")
    edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
    edit.addItem(.separator())
    edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    editItem.submenu = edit
    NSApp.mainMenu = main
  }

  // MARK: Actions

  /// Pressing the shortcut while the panel has the keyboard hides it; otherwise it brings it up.
  private func togglePanel(reason: String) {
    if panel.isVisible && panel.isKey {
      panel.hide()
    } else {
      panel.show(reason: reason, activate: true)
    }
  }

  @objc private func togglePanelFromMenu() {
    if panel.isVisible { panel.hide() } else { panel.show(reason: "menu", activate: true) }
  }

  @objc private func hidePanel() {
    panel.hide()
  }

  @objc private func toggleListening() {
    post(status != "idle" ? "/api/meeting/stop" : "/api/meeting/start", body: "{}") {
      [weak self] _ in self?.heartbeat()
    }
  }

  @objc private func openLogs() {
    Paths.ensure(Paths.logs)
    NSWorkspace.shared.open(Paths.logs)
  }
}

// MARK: - Entry

/// Only one menu-bar app at a time; a second start leaves quietly.
private func acquireInstanceLock() -> Bool {
  Paths.ensure(Paths.support)
  let descriptor = open(Paths.lock.path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
  guard descriptor >= 0 else { return true }  // cannot tell, so run
  if flock(descriptor, LOCK_EX | LOCK_NB) != 0 {
    close(descriptor)
    return false
  }
  return true  // the descriptor stays open, and the lock with it, until the app exits
}

func runApp() -> Never {
  guard acquireInstanceLock() else {
    Log.write("Jarvis is already running; this copy is leaving")
    Log.flush()
    exit(0)
  }
  let application = NSApplication.shared
  application.setActivationPolicy(.accessory)
  let controller = AppController()
  application.delegate = controller
  withExtendedLifetime(controller) { application.run() }
  exit(0)
}
