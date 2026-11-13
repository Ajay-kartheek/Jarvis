// Talking to Jarvis
//
// While the shortcut is held (or the page's microphone button is), the
// microphone goes to the daemon over a WebSocket, ws://127.0.0.1:<port>/ws/voice,
// as binary frames of raw PCM: float32 little-endian, mono, 16 kHz, a tenth of
// a second each. Letting go closes the socket normally, which tells the daemon
// the message is complete. Nothing is written to disk here.
//
// The microphone can be opened before it is known whether the key is being
// tapped or held, so the first word is not lost: sound captured before the
// socket exists waits in memory and is sent first, in order. A tap throws it away.
//
// Which microphone: the system's default input, except that a Bluetooth one (earphones)
// is left alone when the Mac has a microphone of its own (see VoiceMicrophone).

import AVFoundation
import Foundation

/// What the voice controller needs from a microphone: once started, it hands over
/// sound as 16 kHz mono float32 samples until it is stopped.
protocol SoundSource: AnyObject {
  func start() throws
  func stop()
}

extension MicCapture: SoundSource {}
extension DeviceCapture: SoundSource {}

/// Test copy only (JARVIS_TEST_MIC=ramp): a counting signal instead of sound, one
/// sample after the other, 0, 1, 2 ..., at the pace of a real microphone. The
/// receiving end can then check that every sample arrived once, and in order.
final class RampSource: SoundSource {
  private static let samplesPerTick = 320  // 20 ms
  private let onAudio: (Data, Float) -> Void
  private let queue = DispatchQueue(label: "jarvis.app.voice.ramp")
  private var timer: DispatchSourceTimer?
  private var next: Float = 0  // exact for the first 16 million samples, which is 17 minutes

  init(onAudio: @escaping (Data, Float) -> Void) {
    self.onAudio = onAudio
  }

  func start() throws {
    queue.sync {
      next = 0
      let source = DispatchSource.makeTimerSource(queue: queue)
      source.schedule(
        deadline: .now() + .milliseconds(20), repeating: .milliseconds(20),
        leeway: .milliseconds(1))
      source.setEventHandler { [weak self] in self?.tick() }
      source.resume()
      timer = source
    }
  }

  func stop() {
    queue.sync {
      timer?.cancel()
      timer = nil
    }
  }

  private func tick() {
    var samples = [Float](repeating: 0, count: RampSource.samplesPerTick)
    for index in samples.indices {
      samples[index] = next
      next += 1
    }
    onAudio(samples.withUnsafeBufferPointer { Data(buffer: $0) }, 1)
  }
}

/// The microphone one spoken message is recorded from, and why.
///
/// A Bluetooth microphone is slow to open, clips the first word, and drops the earphones into
/// call mode, which spoils whatever the wearer is listening to. So with "voice_input": "auto"
/// (the default) a message is recorded from the Mac's own microphone whenever the default input
/// is a Bluetooth one and the Mac has a built-in microphone it can use. Without one (lid closed,
/// or a Mac that has none) the Bluetooth input is used, opened only once the key is known to be
/// held. "default" always takes the system's default input.
///
/// A microphone that is not Bluetooth is recorded from directly, by its device, and never as
/// "whatever the default is when the engine starts": the default can change at any moment
/// (earphones put in), and a message must not follow it onto them.
struct VoiceMicrophone {
  /// The device to record from directly. nil means the system's default input through an audio
  /// engine, which is how a Bluetooth microphone is recorded.
  let direct: AudioDeviceID?
  let name: String
  /// Whether opening it would switch earphones to call mode.
  let bluetooth: Bool
  let why: String

  static let unchosen = VoiceMicrophone(
    direct: nil, name: "none", bluetooth: false, why: "not chosen yet")

  static func choose(_ setting: VoiceInput) -> VoiceMicrophone {
    if let forced = InputDevice.testChoice() {
      return VoiceMicrophone(
        direct: forced, name: InputDevice.name(of: forced),
        bluetooth: InputDevice.isBluetooth(forced),
        why: "the microphone JARVIS_INPUT_DEVICE names (test copy)")
    }
    let standard = InputDevice.systemDefault()
    let standardName = InputDevice.name(of: standard)
    let standardIsBluetooth =
      TestMode.flag("JARVIS_TEST_BLUETOOTH") || (standard.map(InputDevice.isBluetooth) ?? false)
    let reason =
      setting == .system
      ? "the default input (voice_input is \"default\")" : "the default input"
    guard standardIsBluetooth else {
      return VoiceMicrophone(direct: standard, name: standardName, bluetooth: false, why: reason)
    }
    if setting == .auto, let builtIn = InputDevice.builtIn() {
      return VoiceMicrophone(
        direct: builtIn, name: InputDevice.name(of: builtIn), bluetooth: false,
        why: "the built-in microphone, because the default input \"\(standardName)\""
          + " is Bluetooth")
    }
    return VoiceMicrophone(
      direct: nil, name: standardName, bluetooth: true,
      why: setting == .auto
        ? "the default input, which is Bluetooth: this Mac has no built-in microphone to use"
          + " instead"
        : reason + ", which is Bluetooth")
  }
}

final class VoiceController: NSObject, URLSessionWebSocketDelegate {
  /// Samples per frame: a tenth of a second.
  static let frameSamples = 1600
  private static let bytesPerSample = MemoryLayout<Float32>.size
  private static let frameBytes = frameSamples * bytesPerSample
  /// A daemon on this Mac that has not completed the handshake by then is not going to.
  private static let connectSeconds: TimeInterval = 10

  private enum State {
    case idle
    case armed  // the key is down; not yet known whether it is a tap
    case live  // sending
    case draining  // the key is up; the last sound is on its way out
  }

  /// A spoken message ends by itself after this long (the key may be stuck).
  var maxSeconds: TimeInterval = 120
  /// Called on the main queue when a message that was asked for is over, however it ended.
  var onEnded: (() -> Void)?

  // Everything below is touched on this queue only.
  private let queue = DispatchQueue(label: "jarvis.app.voice", qos: .userInitiated)
  private var session: URLSession!
  private var state = State.idle
  private var chosen = VoiceMicrophone.unchosen  // for the message in progress
  private var mic: SoundSource?  // the capture of the message in progress
  private var micOn = false
  private var keyDownAt = DispatchTime.now()
  private var heardFirst = false
  private var chunk = Data()  // sound not yet enough for a whole frame
  private var early: [Data] = []  // frames captured before the socket was asked for
  private var cap: DispatchSourceTimer?
  private var connectWatch: DispatchSourceTimer?

  // The connection for the message in progress.
  private var task: URLSessionWebSocketTask?
  private var outbox: [Data] = []
  private var sending = false
  private var closeWhenEmpty = false
  private var capturedSamples = 0
  private var sentSamples = 0
  private var sentFrames = 0

  override init() {
    super.init()
    let configuration = URLSessionConfiguration.ephemeral
    // The daemon says nothing while it listens. That does not matter: measured, a 45 s
    // message to a silent server goes through with this timeout, as we send all the time.
    configuration.timeoutIntervalForRequest = 30
    configuration.waitsForConnectivity = false
    configuration.connectionProxyDictionary = [:]  // straight to this Mac, whatever proxy is set
    let callbacks = OperationQueue()
    callbacks.underlyingQueue = queue
    callbacks.maxConcurrentOperationCount = 1
    session = URLSession(configuration: configuration, delegate: self, delegateQueue: callbacks)
  }

  // MARK: What the app asks for

  /// The key went down. The capture starts now, ahead of knowing whether this is a tap, so
  /// the first word is not lost; except that with `bluetoothWaits` a Bluetooth microphone
  /// starts in `commit` instead, as opening it for a tap would switch the earphones to call mode.
  func arm(at keyDown: DispatchTime, setting: VoiceInput, bluetoothWaits: Bool) {
    queue.async {
      guard self.state == .idle else { return }
      self.state = .armed
      self.keyDownAt = keyDown
      self.heardFirst = false
      self.chunk = Data()
      self.early = []
      self.capturedSamples = 0
      self.chosen = VoiceMicrophone.choose(setting)
      if bluetoothWaits && self.chosen.bluetooth {
        Log.write(
          "voice: key down; \"\(self.chosen.name)\" is a Bluetooth microphone, so it opens only"
            + " if the key is held (\(self.chosen.why))")
      } else if !self.startMic() {
        self.state = .idle
      }
    }
  }

  /// It is a hold: connect to the daemon and start sending, oldest sound first.
  func commit(port: Int) {
    queue.async {
      guard self.state == .armed else {
        self.ended()
        return
      }
      if !self.micOn, !self.startMic() {
        self.state = .idle
        self.ended()
        return
      }
      guard let url = URL(string: "ws://127.0.0.1:\(port)/ws/voice") else {
        self.stopMic()
        self.state = .idle
        self.ended()
        return
      }
      self.state = .live
      self.outbox = self.early
      self.early = []
      self.sending = false
      self.closeWhenEmpty = false
      self.sentSamples = 0
      self.sentFrames = 0
      let task = self.session.webSocketTask(with: url)
      self.task = task
      task.resume()
      self.listen(task)
      self.pump()
      Log.write("voice: connecting, \(self.outbox.count) frames captured so far")

      self.cap = self.timer(after: self.maxSeconds) { [weak self] in
        guard let self, self.state == .live else { return }
        Log.write("voice: stopped by itself after \(Int(self.maxSeconds)) s")
        self.finish()
      }
      self.connectWatch = self.timer(after: VoiceController.connectSeconds) { [weak self] in
        guard let self, self.task === task else { return }
        self.stopped("the daemon did not answer within \(Int(VoiceController.connectSeconds)) s")
      }
    }
  }

  /// The key came up after a hold: send what is left and close the socket normally.
  func release() {
    queue.async {
      guard self.state == .live else { return }
      self.finish()
    }
  }

  /// The key came up at once: it was a tap, so nothing that was heard is kept.
  func discard() {
    queue.async {
      guard self.state == .armed else { return }
      self.stopMic()
      Log.write("voice: a tap, \(self.capturedSamples) samples thrown away")
      self.early = []
      self.chunk = Data()
      self.state = .idle
    }
  }

  /// The app is quitting.
  func shutdown() {
    queue.sync {
      self.stopTimers()
      self.stopMic()
      self.task?.cancel(with: .goingAway, reason: nil)
      self.task = nil
      self.state = .idle
    }
  }

  // MARK: The microphone

  private func onAudio() -> (Data, Float) -> Void {
    { [weak self] data, _ in self?.queue.async { self?.heard(data) } }
  }

  private func makeMicCapture() -> MicCapture {
    MicCapture(
      queue: queue, onAudio: onAudio(),
      onStatus: { event, fields in
        if event == "mic_started" {
          Log.write("voice: microphone started at \(fields["sample_rate"] ?? "?") Hz")
        } else {
          Log.write("voice: \(event) \(fields)")
        }
      })
  }

  private func makeSoundSource() -> SoundSource {
    if TestMode.text("JARVIS_TEST_MIC") == "ramp" { return RampSource(onAudio: onAudio()) }
    if let device = chosen.direct { return DeviceCapture(device: device, onAudio: onAudio()) }
    return makeMicCapture()
  }

  private func startMic() -> Bool {
    // An input that fails is tried once more from scratch.
    let began = DispatchTime.now()
    var failure: Error?
    for _ in 0..<2 {
      let source = makeSoundSource()
      mic = source
      do {
        try source.start()
        failure = nil
        break
      } catch {
        failure = error
        mic = nil
      }
    }
    if let failure {
      Log.write("voice: the microphone did not start: \(failure)")
      return false
    }
    micOn = true
    let device: String
    switch mic {
    case let engine as MicCapture:
      device = "\"\(InputDevice.name(of: engine.boundDevice))\""
    case let direct as DeviceCapture:
      device = "\"\(InputDevice.name(of: direct.device))\" at \(direct.rate) Hz"
    default:
      device = "the test signal"
    }
    Log.write(
      "voice: recording from \(device) (\(chosen.why)); microphone on"
        + " \(VoiceController.ms(keyDownAt)) ms after the key went down"
        + " (starting it took \(VoiceController.ms(began)) ms)")
    return true
  }

  private func stopMic() {
    guard micOn else { return }
    mic?.stop()
    mic = nil
    micOn = false
  }

  /// Sound from the microphone, already 16 kHz mono float32. Cuts it into frames.
  private func heard(_ data: Data) {
    guard state != .idle else { return }  // a last piece arriving after the end
    if !heardFirst {
      heardFirst = true
      Log.write("voice: first sound \(VoiceController.ms(keyDownAt)) ms after the key went down")
    }
    capturedSamples += data.count / VoiceController.bytesPerSample
    chunk.append(data)
    while chunk.count >= VoiceController.frameBytes {
      let frame = Data(chunk.prefix(VoiceController.frameBytes))
      chunk.removeFirst(VoiceController.frameBytes)
      deliver(frame)
    }
  }

  private func deliver(_ frame: Data) {
    if state == .armed {
      early.append(frame)
    } else {
      outbox.append(frame)
      pump()
    }
  }

  // MARK: The connection

  /// Stops the microphone, then sends the tail and closes once it has gone out.
  private func finish() {
    stopTimers()
    state = .draining
    stopMic()
    // Sound the microphone handed over just before it stopped is still queued
    // behind this block; the tail goes out after it.
    queue.async {
      guard self.state == .draining else { return }
      if !self.chunk.isEmpty {
        self.outbox.append(self.chunk)
        self.chunk = Data()
      }
      self.closeWhenEmpty = true
      self.pump()
    }
  }

  /// Runs `work` on the voice queue once, `seconds` from now and not later: left to itself the
  /// system may hold a timer back a second or two to save power, which a two-minute limit
  /// does not need.
  private func timer(after seconds: TimeInterval, _ work: @escaping () -> Void)
    -> DispatchSourceTimer
  {
    let source = DispatchSource.makeTimerSource(queue: queue)
    source.schedule(deadline: .now() + seconds, leeway: .milliseconds(0))
    source.setEventHandler(handler: work)
    source.resume()
    return source
  }

  private func stopTimers() {
    cap?.cancel()
    cap = nil
    connectWatch?.cancel()
    connectWatch = nil
  }

  /// Sends one frame at a time, so they arrive in the order they were heard.
  private func pump() {
    guard let task, !sending else { return }
    guard !outbox.isEmpty else {
      if closeWhenEmpty { closed(task) }
      return
    }
    sending = true
    let frame = outbox.removeFirst()
    task.send(.data(frame)) { [weak self] error in
      self?.queue.async {
        guard let self, self.task === task else { return }
        self.sending = false
        if let error {
          self.stopped("the connection failed (\(error.localizedDescription))")
        } else {
          self.sentSamples += frame.count / VoiceController.bytesPerSample
          self.sentFrames += 1
          self.pump()
        }
      }
    }
  }

  private func listen(_ task: URLSessionWebSocketTask) {
    task.receive { [weak self] result in
      self?.queue.async {
        guard let self, self.task === task else { return }
        switch result {
        case .success(.string(let text)):
          if let reason = VoiceController.refusal(in: text) {
            self.stopped("the daemon refused (\(reason))")
          } else {
            self.listen(task)
          }
        case .success:
          self.listen(task)
        case .failure(let error):
          self.stopped("the connection closed (\(error.localizedDescription))")
        }
      }
    }
  }

  /// What the daemon said when it will not take the message, e.g. {"error": "busy"}.
  private static func refusal(in text: String) -> String? {
    if let data = text.data(using: .utf8),
      let message = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
      let reason = message["error"]
    {
      return "\(reason)"
    }
    return text.contains("\"error\"") ? text : nil
  }

  /// Everything went out: close normally, which is how the daemon knows the message is whole.
  private func closed(_ task: URLSessionWebSocketTask) {
    task.cancel(with: .normalClosure, reason: nil)
    Log.write(
      "voice: sent \(sentFrames) frames, \(sentSamples) of \(capturedSamples) samples"
        + " (\(String(format: "%.1f", Double(sentSamples) / targetSampleRate)) s), closed normally")
    self.task = nil
    state = .idle
    ended()
  }

  /// The daemon refused or went away: stop quietly. The interface says why, if anyone does.
  private func stopped(_ why: String) {
    guard state == .live || state == .draining else { return }
    stopTimers()
    stopMic()
    task?.cancel(with: .goingAway, reason: nil)
    task = nil
    outbox = []
    chunk = Data()
    state = .idle
    Log.write("voice: stopped, \(why); \(sentSamples) of \(capturedSamples) samples had been sent")
    ended()
  }

  private func ended() {
    DispatchQueue.main.async { [weak self] in self?.onEnded?() }
  }

  func urlSession(
    _ session: URLSession, webSocketTask: URLSessionWebSocketTask,
    didOpenWithProtocol protocol: String?
  ) {
    guard webSocketTask === task else { return }
    connectWatch?.cancel()
    connectWatch = nil
    Log.write(
      "voice: connected \(VoiceController.ms(keyDownAt)) ms after the key went down,"
        + " \(capturedSamples) samples captured by then")
  }

  func urlSession(
    _ session: URLSession, webSocketTask: URLSessionWebSocketTask,
    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?
  ) {
    guard webSocketTask === task else { return }
    stopped("the daemon closed the connection (code \(closeCode.rawValue))")
  }

  func urlSession(_ session: URLSession, task finished: URLSessionTask, didCompleteWithError error: Error?) {
    guard finished === task, let error else { return }
    stopped("the connection failed (\(error.localizedDescription))")
  }

  private static func ms(_ since: DispatchTime) -> Int {
    Int((DispatchTime.now().uptimeNanoseconds &- since.uptimeNanoseconds) / 1_000_000)
  }
}
