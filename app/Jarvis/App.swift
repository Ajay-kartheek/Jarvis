// The menu-bar app
//
// Always running. It keeps the Python daemon alive, watches for calls, tells
// the daemon what it sees every two seconds (which call it is, from the titles of the call
// app's windows), and shows the panel when something needs the user's eyes. One shortcut does
// two things: a tap shows or hides the panel, a hold is the user talking to Jarvis. When the
// daemon asks for it, the microphone also stays open for "Hey Jarvis" (Ear.swift).
//
// Jarvis is meant to be always there, and launchd does not start an app again after a clean
// exit. So no key quits it (Command-Q hides the panel or closes the dashboard instead); it is
// quit by choosing "Quit Jarvis" with the pointer, and every quit logs who asked for it.

import AVFoundation
import AppKit

/// The Apple events and attributes that come with a quit.
private enum AppleEvents {
  static let core: AEEventClass = 0x6165_7674  // 'aevt'
  static let quit: AEEventID = 0x7175_6974  // 'quit'
  static let senderPid: AEKeyword = 0x7370_6964  // 'spid', who sent it
  static let quitReason: AEKeyword = 0x7768_793F  // 'why?', why the Mac is asking
  /// 'rlgo' log out, 'rrst' restart dialog, 'rsdn' shutdown dialog, 'logo', 'rest', 'shut'.
  static let endOfSession: Set<OSType> = [
    0x726C_676F, 0x7272_7374, 0x7273_646E, 0x6C6F_676F, 0x7265_7374, 0x7368_7574,
  ]
}

final class AppController: NSObject, NSApplicationDelegate, NSMenuDelegate {
  private static let heartbeatSeconds: TimeInterval = 2
  private static let audioPollSeconds: TimeInterval = 1.5
  private static let configCheckSeconds: TimeInterval = 5
  // A daemon that misses this many heartbeats in a row counts as down.
  private static let failuresBeforeDown = 3
  // The shortcut held this long is the user talking; let go sooner and it was a tap.
  private static let holdSeconds: TimeInterval = 0.25
  // How often the windows of the call app are read while a call is on.
  private static let windowsSeconds: TimeInterval = 3
  // The first report of a call waits this long at most for the first reading of the windows.
  private static let firstReadingSeconds: TimeInterval = 1.5

  private var config: AppConfig?
  private var port = AppConfig.defaultPort
  private var statusItem: NSStatusItem!
  private var panel: PanelController!
  private var dashboard: DashboardController?
  private var daemon: DaemonSupervisor?
  private let voice = VoiceController()
  private let ear = EarController()
  private let monitor = CallMonitor(callApps: AppConfig.defaultCallApps)
  private var hotkey: Hotkey?
  private var commandQMonitor: Any?
  // Who asked for the quit now under way, when the app's own code did.
  private var quitReason: String?
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
  private var accessibility = "denied"
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
  // A spoken message is going on (from its start to the end of its connection).
  private var messageInProgress = false
  private var voiceInput: VoiceInput { config?.voiceInput ?? .auto }

  // Which call this is: the titles of the call app's windows, read while a call is on.
  private var callWindows = WindowTitles.Reading.empty
  // The first reading of this call has been made (or cannot be): only then is the call reported.
  private var callWindowsReady = false
  private var readingWindows = false
  private var callGeneration = 0
  private var askedForAccessibility = false
  private let windowQueue = DispatchQueue(label: "jarvis.app.windows", qos: .utility)
  private var testWindows: [NSWindow] = []

  // What the daemon last said.
  private var status = "idle"
  private var attention: Int?
  private var earRequested = false  // "ear" in the daemon's last answer
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
    installCommandQ()
    config = AppConfig.load()
    port = config?.port ?? AppConfig.defaultPort
    panel = PanelController(port: port, hideFromScreenShare: config?.hideFromScreenShare ?? true)
    panel.onAction = { [weak self] action, message in self?.pageAsked(action, message) }
    if let seconds = TestMode.seconds("JARVIS_VOICE_MAX_SECONDS") { voice.maxSeconds = seconds }
    // A message can end on its own (too long, refused, the daemon went away) while the
    // page still thinks its button is held; the page's later "stop" then means nothing.
    voice.onEnded = { [weak self] in self?.messageEnded() }
    ear.onStateChange = { [weak self] in self?.reportIfChanged() }
    if TestMode.on {
      let settings = Paths.config?.path ?? "nowhere (JARVIS_APP_CONFIG is not set)"
      Log.write(
        "test copy: settings from \(settings),"
          + " lock \(Paths.lock.lastPathComponent), log \(Paths.appLog.lastPathComponent)")
    }
    monitor.onChange = { [weak self] active, _ in
      guard let self else { return }
      if active { self.panel.show(reason: "call", activate: false) }
      if active { self.callBegan() } else { self.callEnded() }
      self.updateEar()
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
    every(AppController.windowsSeconds) { [weak self] in self?.readCallWindows() }
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
    updateEar()
  }

  /// The page holds a live connection to the daemon, so it must load again
  /// once a new daemon answers, however quickly that one came up.
  private func daemonWentAway() {
    reachable = false
    wentDown = true
    status = "idle"
    attention = nil
    refreshStatusItem()
    updateEar()
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
    for (number, name) in [(SIGTERM, "SIGTERM"), (SIGINT, "SIGINT")] {
      onSignal(number) { [weak self] in self?.quit(because: "signal \(name)") }
    }
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

  /// More things a test page can ask for than the real pages can (test copy only).
  private func testAction(_ action: String, _ message: [String: Any]) -> Bool {
    func number(_ key: String) -> CGFloat { CGFloat((message[key] as? NSNumber)?.doubleValue ?? 0) }
    switch action {
    case "test_move_panel":  // as the user would with the mouse: dx, dy, and dw, dh to resize
      panel.testChangeFrame(
        dx: number("dx"), dy: number("dy"), dwidth: number("dw"), dheight: number("dh"))
      Log.write("test: the panel is now at \(panel.frameDescription)")
    case "test_press_panel":  // a press at x, y on the page, moving by steps [[dx, dy], ...]
      let steps = (message["steps"] as? [[Double]] ?? []).map { NSPoint(x: $0[0], y: $0[1]) }
      let before = panel.frameDescription
      panel.testPress(at: NSPoint(x: number("x"), y: number("y")), steps: steps)
      Log.write("test: a press on the panel with \(steps.count) moves: \(before) -> \(panel.frameDescription)")
    case "test_windows":  // small windows with these titles, to read back through Accessibility
      testWindows.forEach { $0.close() }
      testWindows = []
      for (index, title) in ((message["titles"] as? [String]) ?? []).enumerated() {
        let window = NSWindow(
          contentRect: NSRect(x: 40 + index * 24, y: 80 + index * 24, width: 120, height: 40),
          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = title
        window.isReleasedWhenClosed = false
        window.orderFront(nil)
        testWindows.append(window)
      }
      if let focus = message["focus"] as? Int, testWindows.indices.contains(focus) {
        testWindows[focus].makeKeyAndOrderFront(nil)
      }
      Log.write("test: \(testWindows.count) windows opened")
    case "test_stall_ear":
      ear.testStall()
    case "test_cmd_q", "test_cmd_w":  // the key, as an event sent through the app's own dispatch
      let key = action == "test_cmd_q" ? "q" : "w"
      var number = panel.windowNumber
      if message["target"] as? String == "dashboard" {
        guard let dashboard else {
          Log.write("test: there is no dashboard to press a key in")
          return true
        }
        dashboard.show()  // the key goes to the window in front, which must be this one
        number = dashboard.windowNumber
      }
      guard
        let event = NSEvent.keyEvent(
          with: .keyDown, location: .zero, modifierFlags: [.command],
          timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: number, context: nil,
          characters: key, charactersIgnoringModifiers: key, isARepeat: false,
          keyCode: key == "q" ? 12 : 13)
      else { return true }
      Log.write("test: Command-\(key.uppercased()) sent to the \(message["target"] as? String ?? "panel")")
      NSApp.sendEvent(event)
      Log.write("test: Command-\(key.uppercased()) was handled; the app is still here")
    case "test_status_menu":  // an item of the status menu, chosen the way a click would
      testChoosesFromStatusMenu(message["item"] as? String ?? "")
    case "test_main_menu":  // the same for the main menu (the one with the dashboard open)
      let found = NSApp.mainMenu?.items.compactMap { $0.submenu }.flatMap { menu in
        menu.items.enumerated().map { (menu, $0.offset, $0.element.title) }
      }.first { $0.2 == (message["item"] as? String ?? "") }
      if let found {
        Log.write("test: \"\(found.2)\" chosen in the main menu")
        found.0.performActionForItem(at: found.1)
      } else {
        Log.write("test: the main menu has no item \"\(message["item"] ?? "")\"")
      }
    case "test_quit_event":  // a quit Apple event as if it came from outside (sender: dock | logout)
      let event = NSAppleEventDescriptor(
        eventClass: AppleEvents.core, eventID: AppleEvents.quit,
        targetDescriptor: NSAppleEventDescriptor(processIdentifier: getpid()), returnID: -1,
        transactionID: 0)
      let dock = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock")
        .first?.processIdentifier
      switch message["sender"] as? String {
      case "dock":
        event.setAttribute(
          NSAppleEventDescriptor(int32: dock ?? 0), forKeyword: AppleEvents.senderPid)
        // An event made here does not keep an attribute the system alone sets, so the naming
        // of a sender is also shown directly, with the Dock's own process number.
        Log.write(
          "test: an event from the Dock (process \(dock.map(String.init) ?? "?")) would be logged as:"
            + " \(AppController.quitSource(senderPid: dock, reason: nil))")
      case "logout":
        event.setParam(NSAppleEventDescriptor(enumCode: 0x726C_676F), forKeyword: AppleEvents.quitReason)
      default:
        break
      }
      Log.write("test: a quit Apple event (sender \(message["sender"] as? String ?? "unknown")) is handled")
      var reply = AppleEvent(descriptorType: DescType(typeNull), dataHandle: nil)
      var unused = 0
      if let raw = event.aeDesc {
        NSAppleEventManager.shared().dispatchRawAppleEvent(
          raw, withRawReply: &reply, handlerRefCon: &unused)
      }
    case "test_close_windows":
      testWindows.forEach { $0.close() }
      testWindows = []
      Log.write("test: the extra windows are closed")
    case "test_ax_self":  // what the Accessibility reading says about this app's own windows
      let reading = WindowTitles.readWindows(of: getpid())
      let said = reading.map { "title \($0.title.map { "\"\($0)\"" } ?? "none"), titles \($0.titles)" }
      Log.write(
        "test: this app is trusted for Accessibility: \(AXIsProcessTrusted());"
          + " its own windows through the API: \(said ?? "not readable")")
    default:
      return false
    }
    return true
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
      ($0.styleMask.contains(.titled) || $0 is JarvisPanel) && ($0.isVisible || $0.isMiniaturized)
    }.map { window in
      let styles: [(NSWindow.StyleMask, String)] = [
        (.closable, "closable"), (.miniaturizable, "miniaturizable"), (.resizable, "resizable"),
      ]
      let has = styles.filter { window.styleMask.contains($0.0) }.map(\.1).joined(separator: "+")
      return "\(type(of: window)) \"\(window.title)\" \(NSStringFromRect(window.frame))"
        + " content-min \(NSStringFromSize(window.contentMinSize)) level \(window.level.rawValue)"
        + " movable-by-background \(window.isMovableByWindowBackground)"
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
    let quitKeys = [
      ("status menu", probe.items.first { $0.title == "Quit Jarvis" }),
      (
        "main menu",
        NSApp.mainMenu?.items.compactMap { $0.submenu }.flatMap { $0.items }
          .first { $0.title == "Quit Jarvis" }
      ),
    ].map { "\($0.0) \($0.1.map { $0.keyEquivalent.isEmpty ? "has no key" : "key \"\($0.keyEquivalent)\"" } ?? "has no such item")" }
    Log.write("state: \"Quit Jarvis\" in the \(quitKeys.joined(separator: ", the "))")
    Log.write(
      "state: panel form \(panel.size.rawValue), ear \(ear.stateName)"
        + " (the daemon asks for it: \(earRequested)), accessibility \(accessibility),"
        + " call reported \(callReported), call title \(callWindows.title ?? "none"),"
        + " titles \(callWindows.titles), message in progress \(messageInProgress)")
    Log.write(
      "state: activation policy \(policy), app active \(NSApp.isActive), daemon status \(status),"
        + " reachable \(reachable), panel \(panel.isVisible ? "shown" : "hidden"),"
        + " dashboard \(dashboard == nil ? "none" : "open"), windows \(windows),"
        + " status menu \(items), shortcut down \(shortcutIsDown), holding \(holding),"
        + " page talking \(pageIsTalking)")
  }

  /// Quits, with the reason in the log. Every way the app's own code quits goes through here.
  private func quit(because reason: String) {
    quitReason = reason
    NSApp.terminate(nil)
  }

  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    Log.write("quitting (\(quitReason ?? whoAskedToQuit()))")
    timers.forEach { $0.invalidate() }
    ear.shutdown()
    voice.shutdown()
    daemon?.stop()
    Log.flush()
    return .terminateNow
  }

  /// A quit that none of the app's own code asked for came as an Apple event: the Dock's menu
  /// (and the Command-Tab switcher's Q, which the Dock sends the same way), `tell application
  /// "Jarvis" to quit`, or logging out, restarting or shutting down. This says which, as far as
  /// the event tells.
  private func whoAskedToQuit() -> String {
    guard let event = NSAppleEventManager.shared().currentAppleEvent,
      event.eventClass == AppleEvents.core, event.eventID == AppleEvents.quit
    else {
      // Not an Apple event either: say where terminate was called from, for the next time.
      let callers = Thread.callStackSymbols.dropFirst(2).prefix(6).map { line in
        line.split(separator: " ", omittingEmptySubsequences: true).dropFirst(3).first.map(String.init)
          ?? "?"
      }
      return "reason unknown: terminate was called from \(callers.joined(separator: " <- "))"
    }
    return AppController.quitSource(
      senderPid: event.attributeDescriptor(forKeyword: AppleEvents.senderPid)?.int32Value,
      reason: event.paramDescriptor(forKeyword: AppleEvents.quitReason)?.enumCodeValue)
  }

  /// What to call a quit Apple event, from who sent it and the reason the Mac gave, if it did.
  static func quitSource(senderPid pid: Int32?, reason: OSType?) -> String {
    if let reason, AppleEvents.endOfSession.contains(reason) {
      return "the Mac is logging out, restarting or shutting down"
    }
    let sender = pid.flatMap { NSRunningApplication(processIdentifier: $0)?.bundleIdentifier }
    if sender == "com.apple.dock" { return "Dock" }  // its menu, or Command-Tab and Q
    return "Apple event from \(sender ?? pid.map { "process \($0)" } ?? "an unknown sender")"
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
    // macOS does not tell "not asked yet" from "refused" for Accessibility.
    let before = accessibility
    accessibility = WindowTitles.allowed ? "granted" : "denied"
    if accessibility != before {
      Log.write("accessibility: \(accessibility)")
      if monitor.active { readCallWindows() }  // the titles can be read now (or no longer)
    }
    updateEar()
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
        self?.askForAccessibility()
      }
    }
  }

  /// Once per launch, after the other questions: macOS shows its own prompt, which sends the
  /// user to System Settings to switch Jarvis on. Not asked again before the next launch.
  private func askForAccessibility() {
    guard !askedForAccessibility else { return }
    askedForAccessibility = true
    guard !WindowTitles.allowed else { return }
    Log.write("asking for the accessibility permission (to read the window titles of a call)")
    WindowTitles.ask()
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

  /// A call is reported once its window titles have been read (or cannot be), so that the
  /// report that says a call has begun also says which call it is.
  private var callReported: Bool { monitor.active && callWindowsReady }

  private func bodyText() -> String {
    let reported = callReported
    let callApp: Any = (reported ? monitor.app : nil) ?? NSNull()
    let title: Any = (reported ? callWindows.title : nil) ?? NSNull()
    let body: [String: Any] = [
      "call": [
        "active": reported, "app": callApp, "title": title,
        "titles": reported ? callWindows.titles : [],
      ],
      "headphones": headphones,
      "permissions": [
        "microphone": microphone, "system_audio": systemAudio, "accessibility": accessibility,
      ],
      "ear_state": ear.stateName,
    ]
    let data = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    return data.flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
  }

  /// Sends a report now if what it would say has changed since the last one.
  private func reportIfChanged() {
    if bodyText() != lastBody { heartbeat() }
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
        updateEar()
      }
      return
    }
    failures = 0
    let newStatus = reply["status"] as? String ?? "idle"
    let newAttention = reply["attention"] as? Int ?? 0
    let wantsEar = reply["ear"] as? Bool ?? false
    if wantsEar != earRequested {
      Log.write("the daemon \(wantsEar ? "asks for" : "no longer asks for") the ear")
      earRequested = wantsEar
    }
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
    updateEar()
  }

  // MARK: Which call this is

  private func callBegan() {
    callWindows = .empty
    callWindowsReady = false
    callGeneration += 1
    let generation = callGeneration
    readCallWindows()
    // A reading that does not come back (an app that does not answer) must not keep a call
    // from being reported.
    DispatchQueue.main.asyncAfter(deadline: .now() + AppController.firstReadingSeconds) {
      [weak self] in
      guard let self, self.callGeneration == generation, !self.callWindowsReady else { return }
      self.callWindowsReady = true
      self.heartbeat()
    }
  }

  private func callEnded() {
    callWindows = .empty
    callWindowsReady = false
    callGeneration += 1
    heartbeat()
  }

  /// Reads the titles of the windows of the app that owns the call (every few seconds while the
  /// call is on), and reports them at once when they have changed.
  private func readCallWindows() {
    guard monitor.active, let app = monitor.app else { return }
    // Test copy: read this app's own windows (which Accessibility allows without the permission)
    // as if they were the call app's, to try the whole way with the real API.
    let ownWindows = TestMode.flag("JARVIS_TEST_OWN_WINDOWS")
    guard ownWindows || WindowTitles.allowed else {
      finishReading(.empty, generation: callGeneration)  // no permission: no titles
      return
    }
    guard !readingWindows else { return }
    guard let pid = ownWindows ? getpid() : (monitor.appPid ?? runningPid(bundleID: app)) else {
      finishReading(.empty, generation: callGeneration)
      return
    }
    readingWindows = true
    let generation = callGeneration
    // Asking another app can take a while; never on the main thread.
    windowQueue.async { [weak self] in
      let reading =
        (ownWindows ? WindowTitles.readWindows(of: pid) : WindowTitles.read(pid: pid)) ?? .empty
      DispatchQueue.main.async {
        self?.readingWindows = false
        self?.finishReading(reading, generation: generation)
      }
    }
  }

  private func finishReading(_ reading: WindowTitles.Reading, generation: Int) {
    guard monitor.active, generation == callGeneration else { return }
    let first = !callWindowsReady
    callWindowsReady = true
    if reading != callWindows {
      callWindows = reading
      let title = reading.title.map { "\"\($0)\"" } ?? "none"
      Log.write("call windows: title \(title), \(reading.titles.count) titles")
      heartbeat()
    } else if first {
      heartbeat()
    }
  }

  // MARK: The ear

  /// Tells the ear what it needs to know to decide whether it is on.
  private func updateEar() {
    ear.update(
      EarConditions(
        requested: earRequested, daemonIdle: reachable && status == "idle",
        microphoneAllowed: microphone == "granted", callActive: monitor.active,
        messageActive: messageInProgress || pageIsTalking
          || (shortcutIsDown && holding && noVoiceBecause == nil),
        port: port, setting: voiceInput))
  }

  /// A spoken message is over, however it ended.
  private func messageEnded() {
    pageIsTalking = false
    messageInProgress = false
    updateEar()
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
    // No key: Jarvis is quit with the pointer, never by a key that may be pressed by mistake.
    menu.addItem(withTitle: "Quit Jarvis", action: #selector(quitFromStatusMenu), keyEquivalent: "")
      .target = self
  }

  /// The main menu. It is out of sight while only the panel is open, and still makes copy,
  /// paste and undo work there; with the dashboard open it is the menu bar of an ordinary app.
  /// Quit has no key on purpose: Command-Q in a Jarvis window must not end the assistant (see
  /// `installCommandQ`).
  private func installMainMenu() {
    let main = NSMenu()
    let appItem = main.addItem(withTitle: "Jarvis", action: nil, keyEquivalent: "")
    let appMenu = NSMenu(title: "Jarvis")
    appMenu.addItem(withTitle: "Close Window", action: #selector(closeFrontWindow), keyEquivalent: "w")
      .target = self
    appMenu.addItem(.separator())
    // (The test copy can be given the key again, to show that a key would quit it: the control
    // of the test that Command-Q does not.)
    let quitKey = TestMode.flag("JARVIS_TEST_QUIT_KEY") ? "q" : ""
    appMenu.addItem(withTitle: "Quit Jarvis", action: #selector(quitFromMainMenu), keyEquivalent: quitKey)
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

  @objc private func quitFromStatusMenu() { quit(because: "status menu") }

  @objc private func quitFromMainMenu() { quit(because: "main menu") }

  /// Command-Q does not quit Jarvis: in the panel it hides it, in the dashboard it closes it
  /// (the app is then back in the menu bar alone), the same as Command-W. Key presses are
  /// taken before any menu sees them, so no menu can turn the key into a quit.
  private func installCommandQ() {
    if TestMode.flag("JARVIS_TEST_QUIT_KEY") { return }  // the control, as above
    commandQMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
      guard let self, AppController.isCommandQ(event) else { return event }
      if !event.isARepeat { self.commandQPressed(in: event.window) }
      return nil
    }
  }

  private static func isCommandQ(_ event: NSEvent) -> Bool {
    let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
      .subtracting([.capsLock, .numericPad, .function])
    return flags == [.command] && event.charactersIgnoringModifiers?.lowercased() == "q"
  }

  private func commandQPressed(in window: NSWindow?) {
    if let dashboard, dashboard.owns(window) {
      Log.write("Command-Q in the dashboard: closing it, Jarvis keeps running")
      dashboard.close()
    } else {
      Log.write("Command-Q: hiding the panel, Jarvis keeps running")
      panel.hide()
    }
  }

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
  private func pageAsked(_ action: String, _ message: [String: Any]) {
    switch action {
    case "hide": panel.hide()
    case "show": panel.show(reason: "attention", activate: false)
    case "quit": quit(because: "page asked")
    case "dashboard": openDashboard()
    case "voice_start": pageVoiceStart()
    case "voice_stop": pageVoiceStop()
    case "size":
      if let mode = (message["mode"] as? String).flatMap(PanelController.Size.init(rawValue:)) {
        panel.setSize(mode)
      } else {
        Log.write("the page asked for a size that is not \"mini\" or \"full\": \(message["mode"] ?? "none")")
      }
    default:
      if TestMode.on, testAction(action, message) { return }
      Log.write("the page sent an unknown action: \(action)")
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
    messageInProgress = true
    updateEar()  // the ear steps aside while the message is spoken
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
      // A tap while Jarvis is talking stops him; otherwise it shows or hides the panel.
      panel.interrupt { [weak self] stopped in
        if stopped {
          Log.write("the shortcut stopped Jarvis talking")
        } else {
          self?.togglePanel(reason: "hotkey")
        }
      }
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
    messageInProgress = true
    updateEar()
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
    opened.onAction = { [weak self] action, message in self?.pageAsked(action, message) }
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
