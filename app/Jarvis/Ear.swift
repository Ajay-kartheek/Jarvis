// The ear: "Hey Jarvis"
//
// While the daemon asks for it ("ear" in its answer to the app's report), nothing is being
// listened to in a call, the microphone is allowed and no spoken message is going on, the voice
// microphone stays open and goes to the daemon over ws://127.0.0.1:<port>/ws/ear, as binary
// frames of raw PCM: float32 little-endian, mono, 16 kHz, a tenth of a second each. That is
// what a spoken message sends, without the limit of two minutes. The daemon listens for the wake
// phrase; nothing is kept here.
//
// The ear never opens a Bluetooth microphone (that would switch earphones to call mode and
// keep them there). It uses the same microphone as a spoken message (see VoiceMicrophone); if
// that is a Bluetooth one, it does not run, and says "no_microphone".

import AVFoundation
import Foundation

/// What decides whether the ear is on.
struct EarConditions: Equatable {
  /// "ear" in the daemon's last answer.
  var requested = false
  /// The daemon answers, and says "idle".
  var daemonIdle = false
  var microphoneAllowed = false
  /// A call is on, as far as the app can see.
  var callActive = false
  /// A spoken message (held key or the page's microphone button) is going on.
  var messageActive = false
  var port = AppConfig.defaultPort
  var setting = VoiceInput.auto
}

final class EarController: NSObject, URLSessionWebSocketDelegate {
  /// How long to wait before trying again, as often as the connection drops while the ear is
  /// wanted: 1, 2, 5, then 10 seconds.
  static let retryDelays: [TimeInterval] = [1, 2, 5, 10]
  /// A connection that lasted this long worked: the next drop starts again at one second.
  static let healthySeconds: TimeInterval = 20
  /// How long to wait after the daemon says "error".
  static let errorDelay: TimeInterval = 10
  /// A daemon on this Mac that has not completed the handshake by then is not going to.
  static let connectSeconds: TimeInterval = 10
  /// An open microphone that gives no sound for this long is started again.
  static let silenceSeconds: TimeInterval = 4
  /// Frames kept for a daemon that does not keep up (ten seconds' worth); older ones are dropped.
  static let outboxLimit = 100
  private static let frameBytes = VoiceController.frameSamples * MemoryLayout<Float32>.size

  /// Called on the main queue when what the ear reports about itself has changed.
  var onStateChange: (() -> Void)?

  // Everything below is touched on this queue only.
  private let queue = DispatchQueue(label: "jarvis.app.ear", qos: .userInitiated)
  private var session: URLSession!
  private var conditions = EarConditions()
  private var checking: DispatchSourceTimer?

  private var capture: SoundSource?  // the open microphone; nil when none is open
  private var captureDevice: AudioDeviceID?
  private var startedAt = DispatchTime.now()
  private var lastSound = DispatchTime.now()
  private var task: URLSessionWebSocketTask?
  private var taskPort = 0
  private var connectedAt: DispatchTime?
  private var connectWatch: DispatchSourceTimer?
  private var outbox: [Data] = []
  private var chunk = Data()
  private var sending = false
  private var sentFrames = 0
  private var droppedFrames = 0
  private var retryIndex = 0
  private var waitingUntil: DispatchTime?  // no new attempt before then
  private var retry: DispatchSourceTimer?

  // What the app reports: "on", "off", "paused" or "no_microphone".
  private let stateLock = NSLock()
  private var reported = "off"

  var stateName: String {
    stateLock.lock()
    defer { stateLock.unlock() }
    return reported
  }

  override init() {
    super.init()
    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = 30
    configuration.waitsForConnectivity = false
    configuration.connectionProxyDictionary = [:]  // straight to this Mac, whatever proxy is set
    let callbacks = OperationQueue()
    callbacks.underlyingQueue = queue
    callbacks.maxConcurrentOperationCount = 1
    session = URLSession(configuration: configuration, delegate: self, delegateQueue: callbacks)
    // Now and then: the microphone in use may have changed, or gone quiet.
    let timer = DispatchSource.makeTimerSource(queue: queue)
    timer.schedule(deadline: .now() + 2, repeating: 2, leeway: .milliseconds(100))
    timer.setEventHandler { [weak self] in self?.reconcile() }
    timer.resume()
    checking = timer
  }

  // MARK: What the app tells it

  func update(_ new: EarConditions) {
    queue.async {
      guard new != self.conditions else { return }
      self.conditions = new
      self.reconcile()
    }
  }

  /// The app is quitting.
  func shutdown() {
    queue.sync {
      checking?.cancel()
      checking = nil
      stop(why: "quitting")
    }
  }

  /// Test copy only: the open microphone is stopped behind the ear's back, so that no sound
  /// comes any more, as if the device had gone.
  func testStall() {
    queue.async {
      (self.capture as? DeviceCapture)?.stop()
      Log.write("test: the microphone is stopped behind the ear's back")
    }
  }

  // MARK: Deciding

  /// Brings what is running in line with what is wanted.
  private func reconcile() {
    let c = conditions
    var off: String?
    if !c.requested {
      off = "the daemon does not want the ear"
    } else if !c.daemonIdle {
      off = "the daemon is busy or not answering"
    } else if c.callActive {
      off = "a call is on"
    } else if !c.microphoneAllowed {
      off = "the microphone is not allowed"
    }
    if let off {
      stop(why: off)
      setState("off")
      return
    }
    if c.messageActive {
      stop(why: "a message is being spoken")
      setState("paused")
      return
    }
    let choice = VoiceMicrophone.choose(c.setting)
    let testSignal = TestMode.text("JARVIS_TEST_MIC") == "ramp"
    guard !choice.bluetooth, choice.direct != nil || testSignal else {
      stop(why: "the only microphone is a Bluetooth one (\(choice.name))")
      if stateName != "no_microphone" {
        Log.write(
          "ear: cannot be on: the only microphone is \"\(choice.name)\", a Bluetooth one, and the"
            + " ear never opens that (\(choice.why))")
      }
      setState("no_microphone")
      return
    }
    if let until = waitingUntil, DispatchTime.now() < until {
      setState("off")  // waiting to try again
      return
    }
    if capture == nil || task == nil || taskPort != c.port {
      if capture != nil || task != nil { stop(why: "starting over") }
      start(choice)
    } else if captureDevice != choice.direct {
      restartMicrophone(choice, why: "the microphone to use is now \"\(choice.name)\"")
    } else if elapsedSeconds(since: lastSound) > EarController.silenceSeconds {
      restartMicrophone(choice, why: "the microphone went quiet")
    }
    setState(capture != nil ? "on" : "off")
  }

  private func setState(_ name: String) {
    stateLock.lock()
    let changed = reported != name
    reported = name
    stateLock.unlock()
    if changed { DispatchQueue.main.async { [weak self] in self?.onStateChange?() } }
  }

  private func elapsedSeconds(since time: DispatchTime) -> Double {
    Double(DispatchTime.now().uptimeNanoseconds &- time.uptimeNanoseconds) / 1_000_000_000
  }

  // MARK: Running

  private func makeSource(_ choice: VoiceMicrophone) -> SoundSource? {
    let onAudio: (Data, Float) -> Void = { [weak self] data, _ in
      self?.queue.async { self?.heard(data) }
    }
    if TestMode.text("JARVIS_TEST_MIC") == "ramp" { return RampSource(onAudio: onAudio) }
    guard let device = choice.direct else { return nil }
    return DeviceCapture(device: device, onAudio: onAudio)
  }

  private func start(_ choice: VoiceMicrophone) {
    guard let source = makeSource(choice) else { return }
    do {
      try source.start()
    } catch {
      retryLater("the microphone did not start (\(error))")
      return
    }
    capture = source
    captureDevice = choice.direct
    waitingUntil = nil
    startedAt = .now()
    lastSound = .now()
    sentFrames = 0
    droppedFrames = 0
    openSocket()
    Log.write("ear: started on \"\(choice.name)\" (\(choice.why))")
  }

  /// A different (or a quiet) microphone under the same connection.
  private func restartMicrophone(_ choice: VoiceMicrophone, why: String) {
    capture?.stop()
    capture = nil
    chunk = Data()
    guard let source = makeSource(choice) else { return }
    do {
      try source.start()
    } catch {
      retryLater("the microphone did not start again (\(error))")
      return
    }
    capture = source
    captureDevice = choice.direct
    lastSound = .now()
    Log.write("ear: \(why); microphone started again on \"\(choice.name)\"")
  }

  /// A deliberate stop (the daemon, a call, a message): everything closed, and the next start
  /// is at once.
  private func stop(why: String) {
    let wasOn = capture != nil || task != nil
    capture?.stop()
    capture = nil
    captureDevice = nil
    closeSocket(code: .normalClosure)
    retry?.cancel()
    retry = nil
    waitingUntil = nil
    retryIndex = 0
    outbox = []
    chunk = Data()
    if wasOn { Log.write("ear: stopped: \(why) (\(sentFrames) frames sent)") }
  }

  /// The connection or the microphone failed: close, wait, and let `reconcile` try again.
  private func retryLater(_ why: String, after fixed: TimeInterval? = nil) {
    if let at = connectedAt, elapsedSeconds(since: at) >= EarController.healthySeconds {
      retryIndex = 0  // it had worked for a good while
    }
    let steps = EarController.retryDelays
    let delay = fixed ?? steps[min(retryIndex, steps.count - 1)]
    if fixed == nil { retryIndex += 1 }
    capture?.stop()
    capture = nil
    captureDevice = nil
    closeSocket(code: .goingAway)
    outbox = []
    chunk = Data()
    waitingUntil = .now() + delay
    Log.write("ear: \(why); trying again in \(Int(delay)) s (\(sentFrames) frames sent)")
    retry?.cancel()
    let timer = DispatchSource.makeTimerSource(queue: queue)
    timer.schedule(deadline: .now() + delay, leeway: .milliseconds(0))
    timer.setEventHandler { [weak self] in self?.reconcile() }
    timer.resume()
    retry = timer
    setState("off")
  }

  // MARK: Sound in, frames out

  private func heard(_ data: Data) {
    guard capture != nil else { return }  // a last piece arriving after a stop
    lastSound = .now()
    chunk.append(data)
    while chunk.count >= EarController.frameBytes {
      outbox.append(Data(chunk.prefix(EarController.frameBytes)))
      chunk.removeFirst(EarController.frameBytes)
    }
    if outbox.count > EarController.outboxLimit {
      let drop = outbox.count - EarController.outboxLimit / 2
      outbox.removeFirst(drop)
      droppedFrames += drop
      Log.write("ear: the daemon is not keeping up; \(droppedFrames) frames dropped so far")
    }
    pump()
  }

  /// Sends one frame at a time, so they arrive in the order they were heard.
  private func pump() {
    guard let task, !sending, !outbox.isEmpty else { return }
    sending = true
    let frame = outbox.removeFirst()
    task.send(.data(frame)) { [weak self] error in
      self?.queue.async {
        guard let self, self.task === task else { return }
        self.sending = false
        if let error {
          self.retryLater("the connection failed (\(error.localizedDescription))")
        } else {
          self.sentFrames += 1
          self.pump()
        }
      }
    }
  }

  // MARK: The connection

  private func openSocket() {
    guard let url = URL(string: "ws://127.0.0.1:\(conditions.port)/ws/ear") else { return }
    let task = session.webSocketTask(with: url)
    self.task = task
    taskPort = conditions.port
    connectedAt = nil
    sending = false
    task.resume()
    listen(task)
    pump()
    let watch = DispatchSource.makeTimerSource(queue: queue)
    watch.schedule(deadline: .now() + EarController.connectSeconds, leeway: .milliseconds(0))
    watch.setEventHandler { [weak self] in
      guard let self, self.task === task, self.connectedAt == nil else { return }
      self.retryLater("the daemon did not answer within \(Int(EarController.connectSeconds)) s")
    }
    watch.resume()
    connectWatch = watch
  }

  private func closeSocket(code: URLSessionWebSocketTask.CloseCode) {
    connectWatch?.cancel()
    connectWatch = nil
    task?.cancel(with: code, reason: nil)
    task = nil
    connectedAt = nil
    sending = false
  }

  private func listen(_ task: URLSessionWebSocketTask) {
    task.receive { [weak self] result in
      self?.queue.async {
        guard let self, self.task === task else { return }
        switch result {
        case .success(.string(let text)):
          if text.contains("\"error\"") {
            self.retryLater("the daemon said error", after: EarController.errorDelay)
          } else {
            self.listen(task)
          }
        case .success:
          self.listen(task)
        case .failure(let error):
          self.retryLater("the connection closed (\(error.localizedDescription))")
        }
      }
    }
  }

  func urlSession(
    _ session: URLSession, webSocketTask: URLSessionWebSocketTask,
    didOpenWithProtocol protocol: String?
  ) {
    guard webSocketTask === task else { return }
    connectWatch?.cancel()
    connectWatch = nil
    connectedAt = .now()
    let took = Int(elapsedSeconds(since: startedAt) * 1000)
    Log.write("ear: connected \(took) ms after the microphone started")
  }

  func urlSession(
    _ session: URLSession, webSocketTask: URLSessionWebSocketTask,
    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?
  ) {
    guard webSocketTask === task else { return }
    retryLater("the daemon closed the connection (code \(closeCode.rawValue))")
  }

  func urlSession(_ session: URLSession, task finished: URLSessionTask, didCompleteWithError error: Error?) {
    guard finished === task, let error else { return }
    retryLater("the connection failed (\(error.localizedDescription))")
  }
}
