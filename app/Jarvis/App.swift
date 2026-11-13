// The menu-bar app
//
// Always running. It keeps the Python daemon alive, watches for calls, tells
// the daemon what it sees every two seconds, and shows the panel when
// something needs the user's eyes. One shortcut does two things: a tap shows
// or hides the panel, a hold is the user talking to Jarvis.

import AVFoundation
import AppKit

final class AppController: NSObject, NSApplicationDelegate, NSMenuDelegate {
  private static let heartbeatSeconds: TimeInterval = 2
  private static let audioPollSeconds: TimeInterval = 1.5
  private static let configCheckSeconds: TimeInterval = 5
  // A daemon that misses this many heartbeats in a row counts as down.
  private static let failuresBeforeDown = 3
  // The shortcut held this long is the user talking; let go sooner and it was a tap.
  private static let holdSeconds: TimeInterval = 0.25

  private var config: AppConfig?
  private var port = AppConfig.defaultPort
  private var statusItem: NSStatusItem!
  private var panel: PanelController!
  private var dashboard: DashboardController?
  private var daemon: DaemonSupervisor?
  private let voice = VoiceController()
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

  // The shortcut: down or not, and what this press has turned out to be.
  private var shortcutIsDown = false
  private var holding = false
  private var holdTimer: DispatchWorkItem?
  private var noVoiceBecause: String?
  private var pageIsTalking = false
  private var voiceInput: VoiceInput { config?.voiceInput ?? .auto }

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
    panel.onAction = { [weak self] action in self?.pageAsked(action) }
    if let seconds = TestMode.seconds("JARVIS_VOICE_MAX_SECONDS") { voice.maxSeconds = seconds }
    // A message can end on its own (too long, refused, the daemon went away) while the
    // page still thinks its button is held; the page's later "stop" then means nothing.
    voice.onEnded = { [weak self] in self?.pageIsTalking = false }
    if TestMode.on {
      let settings = Paths.config?.path ?? "nowhere (JARVIS_APP_CONFIG is not set)"
      Log.write(
        "test copy: settings from \(settings),"
          + " lock \(Paths.lock.lastPathComponent), log \(Paths.appLog.lastPathComponent)")
    }
    monitor.onChange = { [weak self] active, _ in
      guard let self else { return }
      if active { self.panel.show(reason: "call", activate: false) }
      self.heartbeat()
    }

    let shortcut = Hotkey.controlOption(
      onDown: { [weak self] in self?.shortcutDown() }, onUp: { [weak self] in self?.shortcutUp() })
    let registered = shortcut.register()
    let keys = "Control+Option+\(Hotkey.letter)"
    Log.write(
      registered == noErr
        ? "shortcut \(keys) registered"
        : "could not register \(keys) (OSStatus \(registered)); another app may hold it")
    hotkey = shortcut
    installSignals()

    if let config {
      apply(config)
    } else {
      Log.write("no settings at \(Paths.config?.path ?? "any file"): not set up yet")
      every(AppController.configCheckSeconds) { [weak self] in self?.lookForConfig() }
    }
    panel.load()
    refreshPermissions()
    every(AppController.audioPollSeconds) { [weak self] in self?.pollAudio() }
    every(AppController.heartbeatSeconds) { [weak self] in self?.beat() }
    pollAudio()  // also sends the first report

    let quiet = ProcessInfo.processInfo.environment["JARVIS_NO_PROMPTS"] == "1" || TestMode.on
    if config?.prompts != false && !quiet { requestPermissions() }
  }

  private func apply(_ config: AppConfig) {
    self.config = config
    port = config.port
    monitor.callApps = config.callApps
    panel.update(port: config.port, hideFromScreenShare: config.hideFromScreenShare)
    dashboard?.update(port: config.port, hideFromScreenShare: config.hideFromScreenShare)
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
    Log.write("settings found at \(Paths.config?.path ?? "?")")
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
    for number in [SIGTERM, SIGINT] { onSignal(number) { NSApp.terminate(nil) } }
    // `kill -USR1` is a tap of the shortcut; handy when checking the installed app.
    // Nothing else is meant to stop the app, so SIGUSR2 is ignored rather than fatal.
    signal(SIGUSR2, SIG_IGN)
    guard TestMode.on else {
      onSignal(SIGUSR1) { [weak self] in self?.togglePanel(reason: "hotkey") }
      return
    }
    // The test copy is driven from outside (see TestMode): USR1 is the shortcut going down,
    // USR2 coming up, and the rest stand in for clicks that need no mouse.
    onSignal(SIGUSR1) { [weak self] in self?.shortcutDown() }
    onSignal(SIGUSR2) { [weak self] in self?.shortcutUp() }
    onSignal(SIGHUP) { [weak self] in self?.testChoosesFromStatusMenu("Open Dashboard") }
    onSignal(SIGURG) { [weak self] in self?.testPressesDashboardClose() }
    onSignal(SIGINFO) { [weak self] in self?.testDescribes() }
  }

  private func onSignal(_ number: Int32, _ work: @escaping () -> Void) {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler(handler: work)
    source.resume()
    signalSources.append(source)
  }

  // MARK: Driving the test copy

  /// SIGHUP: an item of the status menu is chosen the way a click on it would.
  private func testChoosesFromStatusMenu(_ title: String) {
    guard let menu = statusItem.menu else { return }
    menuNeedsUpdate(menu)  // as when the menu opens
    menu.update()
    guard let index = menu.items.firstIndex(where: { $0.title == title }) else {
      Log.write("test: the status menu has no item \"\(title)\"")
      return
    }
    let enabled = menu.items[index].isEnabled
    Log.write("test: \"\(title)\" chosen in the status menu (enabled: \(enabled))")
    menu.performActionForItem(at: index)
  }

  /// SIGURG: the close button of the dashboard window is pressed.
  private func testPressesDashboardClose() {
    guard let dashboard else {
      Log.write("test: there is no dashboard to close")
      return
    }
    Log.write("test: the dashboard's close button is pressed")
    dashboard.close()
  }

  /// SIGINFO: what the app is doing right now, in one line of the log.
  private func testDescribes() {
    let probe = NSMenu()
    menuNeedsUpdate(probe)
    probe.update()
    let items = probe.items.map {
      $0.isSeparatorItem ? "-" : $0.title + ($0.isEnabled ? "" : " (disabled)")
    }
    let windows = NSApp.windows.filter {
      $0.styleMask.contains(.titled) && ($0.isVisible || $0.isMiniaturized)
    }.map { window in
      let styles: [(NSWindow.StyleMask, String)] = [
        (.closable, "closable"), (.miniaturizable, "miniaturizable"), (.resizable, "resizable"),
      ]
      let has = styles.filter { window.styleMask.contains($0.0) }.map(\.1).joined(separator: "+")
      return "\(type(of: window)) \"\(window.title)\" \(NSStringFromRect(window.frame))"
        + " content-min \(NSStringFromSize(window.contentMinSize)) level \(window.level.rawValue)"
        + " autosave \"\(window.frameAutosaveName)\" sharing \(window.sharingType.rawValue)"
        + " [\(has)]"
    }
    let policy: String
    switch NSApp.activationPolicy() {
    case .regular: policy = "regular"
    case .accessory: policy = "accessory"
    default: policy = "prohibited"
    }
    let pick = VoiceMicrophone.choose(voiceInput)
    Log.write(
      "state: voice_input \"\(voiceInput.rawValue)\" would record from \"\(pick.name)\""
        + " (\(pick.why))")
    Log.write(
      "state: activation policy \(policy), app active \(NSApp.isActive), daemon status \(status),"
        + " reachable \(reachable), panel \(panel.isVisible ? "shown" : "hidden"),"
        + " dashboard \(dashboard == nil ? "none" : "open"), windows \(windows),"
        + " status menu \(items), shortcut down \(shortcutIsDown), holding \(holding),"
        + " page talking \(pageIsTalking)")
  }

  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    Log.write("quitting")
    timers.forEach { $0.invalidate() }
    voice.shutdown()
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
      if wentDown {
        panel.load()
        dashboard?.load()
      }
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
    statusItem.autosaveName = "Jarvis\(TestMode.nameSuffix)"
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
    menu.addItem(withTitle: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "")
      .target = self
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

  /// The main menu. It is out of sight while only the panel is open, and still makes copy,
  /// paste and undo work there; with the dashboard open it is the menu bar of an ordinary app.
  /// Quit has no key on purpose: Cmd-Q in a Jarvis window should not end the assistant.
  private func installMainMenu() {
    let main = NSMenu()
    let appItem = main.addItem(withTitle: "Jarvis", action: nil, keyEquivalent: "")
    let appMenu = NSMenu(title: "Jarvis")
    appMenu.addItem(withTitle: "Close Window", action: #selector(closeFrontWindow), keyEquivalent: "w")
      .target = self
    appMenu.addItem(.separator())
    appMenu.addItem(
      withTitle: "Quit Jarvis", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
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
    let windowItem = main.addItem(withTitle: "Window", action: nil, keyEquivalent: "")
    let window = NSMenu(title: "Window")
    window.addItem(withTitle: "Minimize", action: #selector(minimiseDashboard), keyEquivalent: "m")
      .target = self
    window.addItem(withTitle: "Dashboard", action: #selector(openDashboard), keyEquivalent: "")
      .target = self
    windowItem.submenu = window
    NSApp.mainMenu = main
  }

  // MARK: Actions

  // A plain toggle: the shortcut always closes a panel that is on screen, whether or not it has the keyboard.
  private func togglePanel(reason: String) {
    if panel.isVisible {
      panel.hide()
    } else {
      panel.show(reason: reason, activate: true)
    }
  }

  @objc private func togglePanelFromMenu() {
    if panel.isVisible { panel.hide() } else { panel.show(reason: "menu", activate: true) }
  }

  /// Cmd-W: the dashboard when that is the window in front, otherwise the panel.
  @objc private func closeFrontWindow() {
    if let dashboard, dashboard.isKey { dashboard.close() } else { panel.hide() }
  }

  @objc private func minimiseDashboard() {
    if let dashboard, dashboard.isKey { dashboard.minimise() }
  }

  /// What a page asked for through its bridge. The panel and the dashboard ask
  /// the same things, and they mean the same from either.
  private func pageAsked(_ action: String) {
    switch action {
    case "hide": panel.hide()
    case "show": panel.show(reason: "attention", activate: false)
    case "quit": NSApp.terminate(nil)
    case "dashboard": openDashboard()
    case "voice_start": pageVoiceStart()
    case "voice_stop": pageVoiceStop()
    default: Log.write("the page sent an unknown action: \(action)")
    }
  }

  // MARK: The shortcut: tap or hold

  /// Why the user cannot talk to Jarvis right now, or nil when they can.
  private func whyNoVoice() -> String? {
    if !reachable { return "the daemon is not answering" }
    if status != "idle" { return "the daemon is busy (status \(status))" }
    if microphone != "granted" { return "the microphone is not allowed" }
    return nil
  }

  private func shortcutDown() {
    guard !shortcutIsDown, !pageIsTalking else { return }  // a key that repeats is still one press
    shortcutIsDown = true
    holding = false
    noVoiceBecause = whyNoVoice()
    if noVoiceBecause == nil {
      // The microphone opens now, before it is known whether this is a tap, so the first word
      // is not lost. A Bluetooth one (only when the Mac has no microphone of its own to use
      // instead) waits for the hold: opening it switches the earphones to call mode, which a
      // tap should not do.
      voice.arm(at: .now(), setting: voiceInput, bluetoothWaits: true)
    }
    let work = DispatchWorkItem { [weak self] in self?.shortcutHeld() }
    holdTimer = work
    DispatchQueue.main.asyncAfter(deadline: .now() + AppController.holdSeconds, execute: work)
  }

  private func shortcutHeld() {
    guard shortcutIsDown, !holding else { return }
    holding = true
    if let why = noVoiceBecause {
      Log.write("the shortcut is held, but there is no talking now: \(why)")
      if !panel.isVisible { panel.show(reason: "hotkey", activate: false) }
      return
    }
    // The keyboard stays with the app the user is in; they are talking, not typing.
    panel.show(reason: "voice", activate: false)
    voice.commit(port: port)
  }

  private func shortcutUp() {
    guard shortcutIsDown else { return }
    shortcutIsDown = false
    holdTimer?.cancel()
    holdTimer = nil
    if holding {
      holding = false
      if noVoiceBecause == nil { voice.release() }
    } else {
      if noVoiceBecause == nil { voice.discard() }
      togglePanel(reason: "hotkey")
    }
  }

  /// The page's microphone button, held with the mouse: the same as holding the shortcut.
  private func pageVoiceStart() {
    guard !shortcutIsDown, !pageIsTalking else { return }
    if let why = whyNoVoice() {
      Log.write("the page asked to talk, but there is no talking now: \(why)")
      return
    }
    pageIsTalking = true
    voice.arm(at: .now(), setting: voiceInput, bluetoothWaits: false)
    panel.show(reason: "voice", activate: false)
    voice.commit(port: port)
  }

  private func pageVoiceStop() {
    guard pageIsTalking else { return }
    pageIsTalking = false
    voice.release()
  }

  // MARK: The dashboard

  @objc private func openDashboard() {
    if let dashboard {
      Log.write("dashboard is open already; bringing it forward")
      dashboard.show()
      return
    }
    let opened = DashboardController(
      port: port, hideFromScreenShare: config?.hideFromScreenShare ?? true)
    opened.onAction = { [weak self] action in self?.pageAsked(action) }
    opened.onClose = { [weak self] in self?.dashboardClosed() }
    dashboard = opened
    // A window of its own makes Jarvis an ordinary app for as long as it is
    // open: it is in the Dock and in Cmd-Tab, and has a menu bar.
    NSApp.setActivationPolicy(.regular)
    Log.write("dashboard opened; Jarvis is in the Dock while it is open")
    DispatchQueue.main.async { opened.show() }
  }

  private func dashboardClosed() {
    // This runs inside the window's own closing, so the controller is let go a moment later.
    let closing = dashboard
    dashboard = nil
    DispatchQueue.main.async { [weak self] in
      _ = closing
      guard let self, self.dashboard == nil else { return }
      NSApp.setActivationPolicy(.accessory)
      Log.write("Jarvis is back in the menu bar only")
    }
  }

  /// A click on the Dock icon, which is only there while the dashboard is open.
  func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
    dashboard?.show()
    return false
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
  // A test copy must never reach the installed copy's daemon: without settings of its own it
  // would fall back to that daemon's port and report calls to it. So it does not start then.
  if TestMode.on {
    guard let settings = AppConfig.load(), settings.port != AppConfig.defaultPort else {
      Log.write(
        "test copy: $JARVIS_APP_CONFIG must be a settings file whose port is not"
          + " \(AppConfig.defaultPort), the installed copy's; not starting")
      Log.flush()
      exit(2)
    }
  }
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
