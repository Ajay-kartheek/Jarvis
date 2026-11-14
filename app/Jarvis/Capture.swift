// The audio helper
//
// Captures the two sides of a call on this Mac and streams them to the Jarvis
// Python process as 16 kHz mono float32 PCM over a local TCP socket:
//   - system audio (what the other people say), through a Core Audio process tap
//   - the microphone (what you say), through AVAudioEngine
//
// Nothing is written to disk here and nothing leaves the Mac.
//
// The tap plus tap-only aggregate device recipe follows audiotee
// (https://github.com/makeusabrew/audiotee, MIT).
//
// Usage: Jarvis --port <tcp port on 127.0.0.1> [--no-mic] [--no-system]

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

let targetSampleRate: Double = 16_000
let levelReportInterval: TimeInterval = 2

// MARK: - Wire format
//
// Packet = 8-byte header + payload.
//   bytes 0-1  magic "JE"
//   byte  2    kind: 0 = system audio, 1 = microphone, 2 = status (UTF-8 JSON)
//   byte  3    reserved, 0
//   bytes 4-7  payload length in bytes, little-endian
// Audio payload: float32 little-endian samples, 16 kHz, mono.

enum PacketKind: UInt8 {
  case system = 0
  case mic = 1
  case status = 2
}

struct EarsError: Error, CustomStringConvertible {
  let description: String
  init(_ description: String) { self.description = description }
}

func check(_ status: OSStatus, _ what: String) throws {
  guard status == noErr else { throw EarsError("\(what) failed (OSStatus \(status))") }
}

func globalAddress(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
  AudioObjectPropertyAddress(
    mSelector: selector,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
}

func defaultOutputDeviceUID() throws -> String {
  var deviceID = AudioDeviceID(kAudioObjectUnknown)
  var deviceSize = UInt32(MemoryLayout<AudioDeviceID>.size)
  var deviceAddress = globalAddress(kAudioHardwarePropertyDefaultOutputDevice)
  try check(
    AudioObjectGetPropertyData(
      AudioObjectID(kAudioObjectSystemObject), &deviceAddress, 0, nil, &deviceSize, &deviceID),
    "Finding the output device")
  var uid: CFString = "" as CFString
  var uidSize = UInt32(MemoryLayout<CFString>.stride)
  var uidAddress = globalAddress(kAudioDevicePropertyDeviceUID)
  try check(
    withUnsafeMutablePointer(to: &uid) {
      AudioObjectGetPropertyData(deviceID, &uidAddress, 0, nil, &uidSize, $0)
    }, "Reading the output device identifier")
  return uid as String
}

// MARK: - System-audio permission
//
// macOS has no public call that asks for the "System Audio Recording"
// permission, and on macOS 26 starting a tap without it fails rather than
// prompting. The TCC framework's own request function shows the prompt.
// (Same approach as AudioCap, https://github.com/insidegui/AudioCap.)

enum AudioCapturePermission {
  enum State { case allowed, denied, unknown }

  private typealias Preflight = @convention(c) (CFString, CFDictionary?) -> Int32
  private typealias Request =
    @convention(c) (CFString, CFDictionary?, @escaping @convention(block) (Bool) -> Void) -> Void

  private static let service = "kTCCServiceAudioCapture" as CFString
  private static let framework = dlopen(
    "/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)

  static func state() -> State {
    guard let framework, let symbol = dlsym(framework, "TCCAccessPreflight") else {
      return .unknown
    }
    switch unsafeBitCast(symbol, to: Preflight.self)(service, nil) {
    case 0: return .allowed
    case 1: return .denied
    default: return .unknown
    }
  }

  /// Shows the macOS prompt and waits for the answer; nil if it cannot be shown.
  static func request() -> Bool? {
    guard let framework, let symbol = dlsym(framework, "TCCAccessRequest") else { return nil }
    let done = DispatchSemaphore(value: 0)
    var granted = false
    unsafeBitCast(symbol, to: Request.self)(service, nil) { allowed in
      granted = allowed
      done.signal()
    }
    done.wait()
    return granted
  }
}

// MARK: - Link to the Python process

final class Link {
  private let fd: Int32
  private let queue = DispatchQueue(label: "jarvis.ears.link")

  init(port: UInt16) throws {
    fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { throw EarsError("socket() failed (errno \(errno))") }
    var one: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = port.bigEndian
    address.sin_addr.s_addr = inet_addr("127.0.0.1")
    let result = withUnsafePointer(to: &address) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
      }
    }
    guard result == 0 else {
      throw EarsError("cannot connect to 127.0.0.1:\(port) (errno \(errno))")
    }
  }

  func send(_ kind: PacketKind, _ payload: Data) {
    queue.async { [fd] in
      var header = Data([0x4A, 0x45, kind.rawValue, 0])
      var length = UInt32(payload.count).littleEndian
      withUnsafeBytes(of: &length) { header.append(contentsOf: $0) }
      if !Link.writeAll(fd, header) || !Link.writeAll(fd, payload) {
        // The Python side went away, so there is nobody left to listen for.
        exit(0)
      }
    }
  }

  func status(_ event: String, _ fields: [String: Any] = [:]) {
    var body = fields
    body["event"] = event
    if let data = try? JSONSerialization.data(withJSONObject: body) {
      send(.status, data)
    }
  }

  /// Blocks until everything queued so far has been written.
  func flush() { queue.sync {} }

  private static func writeAll(_ fd: Int32, _ data: Data) -> Bool {
    data.withUnsafeBytes { raw -> Bool in
      var offset = 0
      while offset < raw.count {
        let written = write(fd, raw.baseAddress! + offset, raw.count - offset)
        if written < 0 && errno == EINTR { continue }
        if written <= 0 { return false }
        offset += written
      }
      return true
    }
  }
}

// MARK: - Conversion to 16 kHz mono float32

final class Resampler {
  private let converter: AVAudioConverter
  private let inputFormat: AVAudioFormat
  private let outputFormat: AVAudioFormat

  init?(from inputFormat: AVAudioFormat) {
    guard
      let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: targetSampleRate, channels: 1,
        interleaved: false),
      let converter = AVAudioConverter(from: inputFormat, to: outputFormat)
    else { return nil }
    converter.downmix = inputFormat.channelCount > 1
    self.converter = converter
    self.inputFormat = inputFormat
    self.outputFormat = outputFormat
  }

  /// Returns the converted samples and their peak level, or nil if there was nothing to convert.
  func convert(_ buffer: AVAudioPCMBuffer) -> (data: Data, peak: Float)? {
    let ratio = outputFormat.sampleRate / inputFormat.sampleRate
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
    guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
      return nil
    }
    var consumed = false
    var conversionError: NSError?
    let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
      // Hand the buffer over once; "no data now" keeps the converter's state
      // for the next call, which "end of stream" would not.
      if consumed {
        inputStatus.pointee = .noDataNow
        return nil
      }
      consumed = true
      inputStatus.pointee = .haveData
      return buffer
    }
    guard status != .error, output.frameLength > 0, let channels = output.floatChannelData else {
      return nil
    }
    let count = Int(output.frameLength)
    var peak: Float = 0
    for index in 0..<count {
      let magnitude = abs(channels[0][index])
      if magnitude > peak { peak = magnitude }
    }
    return (Data(bytes: channels[0], count: count * MemoryLayout<Float>.size), peak)
  }
}

/// Reports the loudest sample seen every couple of seconds, so the Python side
/// can tell a working stream from a dead one.
final class LevelMeter {
  private let link: Link
  private let source: String
  private var peak: Float = 0
  private var lastReport = Date()

  init(link: Link, source: String) {
    self.link = link
    self.source = source
  }

  func add(_ value: Float) {
    if value > peak { peak = value }
    let now = Date()
    if now.timeIntervalSince(lastReport) >= levelReportInterval {
      link.status("level", ["source": source, "peak": Double(peak)])
      peak = 0
      lastReport = now
    }
  }
}

// MARK: - System audio

final class SystemAudioTap {
  private let link: Link
  private let queue = DispatchQueue(label: "jarvis.ears.system", qos: .userInitiated)
  private var tapID = AudioObjectID(kAudioObjectUnknown)
  private var aggregateID = AudioObjectID(kAudioObjectUnknown)
  private var procID: AudioDeviceIOProcID?
  private var format: AVAudioFormat?
  private var resampler: Resampler?
  private let meter: LevelMeter
  private var outputListener: AudioObjectPropertyListenerBlock?
  // Setting up and tearing down happens on its own queue. Stopping the capture
  // device waits for the audio callback to finish, so doing it from the
  // callback's queue would wait for itself.
  private let control = DispatchQueue(label: "jarvis.ears.system.control")
  private var pendingRebuild: DispatchWorkItem?
  private var watchdog: DispatchSourceTimer?
  private var lastAudio = DispatchTime.now().uptimeNanoseconds

  init(link: Link) {
    self.link = link
    self.meter = LevelMeter(link: link, source: "system")
  }

  func start() throws {
    try control.sync { try buildWithFallback() }
    watchDefaultOutputDevice()
    startWatchdog()
  }

  /// A running tap delivers buffers continuously, silence included. If they
  /// stop arriving, set the capture up again.
  private func startWatchdog() {
    let timer = DispatchSource.makeTimerSource(queue: control)
    timer.schedule(deadline: .now() + 5, repeating: 3)
    timer.setEventHandler { [weak self] in
      guard let self, self.aggregateID != kAudioObjectUnknown else { return }
      let idle = DispatchTime.now().uptimeNanoseconds &- self.lastAudio
      if idle > 4_000_000_000 { self.rebuild(reason: "capture stalled") }
    }
    timer.resume()
    watchdog = timer
  }

  /// A capture device that holds only the tap is the simplest arrangement. If
  /// macOS refuses to start it, pair the tap with the current output device.
  private func buildWithFallback() throws {
    do {
      try build(pairWithOutputDevice: false)
    } catch let first {
      teardown()
      do {
        try build(pairWithOutputDevice: true)
      } catch {
        teardown()
        throw EarsError("\(first); paired with the output device: \(error)")
      }
    }
  }

  private func build(pairWithOutputDevice: Bool) throws {
    // A mono mix of everything every other process plays.
    let description = CATapDescription()
    description.name = "Jarvis"
    description.processes = []
    description.isExclusive = true  // "all processes except the listed ones"
    description.isPrivate = true
    description.isMixdown = true
    description.isMono = true
    description.muteBehavior = .unmuted

    var newTap = AudioObjectID(kAudioObjectUnknown)
    try check(AudioHardwareCreateProcessTap(description, &newTap), "Creating the system-audio tap")
    tapID = newTap

    var formatAddress = globalAddress(kAudioTapPropertyFormat)
    var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.stride)
    var streamDescription = AudioStreamBasicDescription()
    try check(
      AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &streamDescription),
      "Reading the tap format")
    guard let tapFormat = AVAudioFormat(streamDescription: &streamDescription),
      let resampler = Resampler(from: tapFormat)
    else {
      throw EarsError("Unsupported system-audio format")
    }
    format = tapFormat
    self.resampler = resampler

    var uidAddress = globalAddress(kAudioTapPropertyUID)
    var uidSize = UInt32(MemoryLayout<CFString>.stride)
    var tapUID: CFString = "" as CFString
    try check(
      withUnsafeMutablePointer(to: &tapUID) {
        AudioObjectGetPropertyData(tapID, &uidAddress, 0, nil, &uidSize, $0)
      }, "Reading the tap identifier")

    var aggregate: [String: Any] = [
      kAudioAggregateDeviceNameKey: "Jarvis",
      kAudioAggregateDeviceUIDKey: UUID().uuidString,
      kAudioAggregateDeviceIsPrivateKey: true,
      kAudioAggregateDeviceIsStackedKey: false,
    ]
    if pairWithOutputDevice {
      let outputUID = try defaultOutputDeviceUID()
      aggregate[kAudioAggregateDeviceMainSubDeviceKey] = outputUID
      aggregate[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: outputUID]]
      aggregate[kAudioAggregateDeviceTapAutoStartKey] = true
      aggregate[kAudioAggregateDeviceTapListKey] = [
        [kAudioSubTapUIDKey: tapUID as String, kAudioSubTapDriftCompensationKey: true]
      ]
    } else {
      aggregate[kAudioAggregateDeviceSubDeviceListKey] = [] as CFArray
      aggregate[kAudioAggregateDeviceMainSubDeviceKey] = 0
    }
    var newAggregate = AudioObjectID(kAudioObjectUnknown)
    try check(
      AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &newAggregate),
      "Creating the capture device")
    aggregateID = newAggregate

    if !pairWithOutputDevice {
      var listAddress = globalAddress(kAudioAggregateDevicePropertyTapList)
      let tapList = [tapUID] as CFArray
      try check(
        withUnsafePointer(to: tapList) {
          AudioObjectSetPropertyData(
            aggregateID, &listAddress, 0, nil, UInt32(MemoryLayout<CFArray>.stride), $0)
        }, "Attaching the tap to the capture device")
    }

    var newProc: AudioDeviceIOProcID?
    try check(
      AudioDeviceCreateIOProcIDWithBlock(&newProc, aggregateID, queue) {
        [weak self] _, inputData, _, _, _ in
        self?.process(inputData)
      }, "Creating the audio callback")
    procID = newProc

    // The capture device can need a moment after the tap is attached, and
    // after the permission prompt is answered.
    var startStatus = AudioDeviceStart(aggregateID, procID)
    var attempts = 1
    while startStatus != noErr && attempts < 4 {
      usleep(300_000)
      startStatus = AudioDeviceStart(aggregateID, procID)
      attempts += 1
    }
    try check(startStatus, "Starting system-audio capture")

    link.status(
      "system_started",
      [
        "sample_rate": tapFormat.sampleRate,
        "channels": Int(tapFormat.channelCount),
        "paired_with_output_device": pairWithOutputDevice,
        "start_attempts": attempts,
      ])
  }

  private func process(_ inputData: UnsafePointer<AudioBufferList>) {
    // The tap's stream is the last buffer: when the capture device is paired
    // with an output device that also has inputs, those come first.
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inputData))
    lastAudio = DispatchTime.now().uptimeNanoseconds
    guard let format, let resampler, let last = buffers.last, last.mData != nil,
      last.mDataByteSize > 0
    else { return }
    var single = AudioBufferList(mNumberBuffers: 1, mBuffers: last)
    let converted = withUnsafePointer(to: &single) { pointer -> (data: Data, peak: Float)? in
      guard
        let buffer = AVAudioPCMBuffer(
          pcmFormat: format, bufferListNoCopy: pointer, deallocator: nil),
        buffer.frameLength > 0
      else { return nil }
      return resampler.convert(buffer)
    }
    guard let converted else { return }
    meter.add(converted.peak)
    link.send(.system, converted.data)
  }

  private func teardown() {
    if aggregateID != kAudioObjectUnknown {
      if let procID {
        AudioDeviceStop(aggregateID, procID)
        AudioDeviceDestroyIOProcID(aggregateID, procID)
      }
      AudioHardwareDestroyAggregateDevice(aggregateID)
    }
    if tapID != kAudioObjectUnknown {
      AudioHardwareDestroyProcessTap(tapID)
    }
    procID = nil
    aggregateID = AudioObjectID(kAudioObjectUnknown)
    tapID = AudioObjectID(kAudioObjectUnknown)
    format = nil
    resampler = nil
  }

  /// Headphones plugged in or pulled out change the tap's format, so rebuild.
  private func watchDefaultOutputDevice() {
    var address = globalAddress(kAudioHardwarePropertyDefaultOutputDevice)
    let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
      self?.scheduleRebuild(reason: "output device changed")
    }
    outputListener = listener
    AudioObjectAddPropertyListenerBlock(
      AudioObjectID(kAudioObjectSystemObject), &address, control, listener)
  }

  /// Device changes arrive in bursts; wait for them to settle, then rebuild once.
  private func scheduleRebuild(reason: String) {
    pendingRebuild?.cancel()
    let work = DispatchWorkItem { [weak self] in self?.rebuild(reason: reason) }
    pendingRebuild = work
    control.asyncAfter(deadline: .now() + 0.6, execute: work)
  }

  /// Runs on the control queue.
  private func rebuild(reason: String) {
    link.status("system_restarting", ["reason": reason])
    teardown()
    lastAudio = DispatchTime.now().uptimeNanoseconds
    do {
      try buildWithFallback()
    } catch {
      link.status("system_failed", ["error": "\(error)"])
    }
  }

  func stop() {
    watchdog?.cancel()
    control.sync { teardown() }
  }
}

// MARK: - Microphone

final class MicCapture {
  /// Receives each stretch of sound as 16 kHz mono float32 samples with its
  /// peak level. Called on the audio thread.
  private let onAudio: (Data, Float) -> Void
  /// Receives what happens to the capture: mic_started, mic_restarting, mic_failed.
  private let onStatus: (String, [String: Any]) -> Void
  // Where the capture is set up again after the input device changes. Whoever
  // owns the capture starts and stops it from this queue too.
  private let queue: DispatchQueue
  private let engine = AVAudioEngine()
  private var resampler: Resampler?
  private var observer: NSObjectProtocol?
  private var running = false
  private var counted = false  // counted among the microphones recorded straight from the device
  private var attempts = 1
  private var tapFormat: AVAudioFormat?
  // What the converter above was made for.
  private var resamplerFormat: AVAudioFormat?

  init(
    queue: DispatchQueue = .main, onAudio: @escaping (Data, Float) -> Void,
    onStatus: @escaping (String, [String: Any]) -> Void
  ) {
    self.queue = queue
    self.onAudio = onAudio
    self.onStatus = onStatus
  }

  /// The device the engine's input is bound to, as the engine reports it.
  var boundDevice: AudioDeviceID { engine.inputNode.auAudioUnit.deviceID }

  /// The helper's microphone: sound and status go to the Python process. It records from the
  /// system's default input, whatever app.json says about talking to Jarvis (see `install`
  /// for the one thing a test copy can change).
  convenience init(link: Link) {
    let meter = LevelMeter(link: link, source: "mic")
    self.init(
      onAudio: { data, peak in
        meter.add(peak)
        link.send(.mic, data)
      },
      onStatus: { event, fields in link.status(event, fields) })
  }

  /// How often starting is tried: voice processing in the app leaves the device's format settling for
  /// a moment after it stops, as when a call begins right after Jarvis has spoken.
  static let startAttempts = 5

  /// Can be called again after `stop`: the app opens the microphone once per
  /// spoken message.
  func start() throws {
    running = true
    var failure: Error?
    for attempt in 1...MicCapture.startAttempts {
      attempts = attempt
      do {
        try install()
        try begin()
        failure = nil
        break
      } catch {
        failure = error
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        if attempt < MicCapture.startAttempts { usleep(200_000) }
      }
    }
    if let failure {
      running = false
      throw failure
    }
    if !counted {
      counted = true
      DirectMicrophones.opened()
    }
    // The engine stops itself when the input device changes (AirPods connect,
    // a headset is unplugged), so set it up again.
    observer = NotificationCenter.default.addObserver(
      forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
    ) { [weak self] _ in
      // Give the new device a moment to settle, and do the work off the
      // thread that delivered the notification.
      guard let queue = self?.queue else { return }
      queue.asyncAfter(deadline: .now() + 0.6) {
        guard let self, self.running else { return }  // stopped meanwhile
        self.onStatus("mic_restarting", ["reason": "input device changed"])
        self.engine.inputNode.removeTap(onBus: 0)
        do {
          try self.install()
          try self.begin()
        } catch {
          self.onStatus("mic_failed", ["error": "\(error)"])
        }
      }
    }
  }

  /// Puts the tap on the input and prepares the engine, without starting it.
  private func install() throws {
    let input = engine.inputNode
    if let forced = InputDevice.testChoice() {
      // Only a test copy gets here: it listens to the microphone it is told to (the helper
      // regression test uses this), not the system's default one.
      guard forced != AudioDeviceID(kAudioObjectUnknown) else {
        throw EarsError("The input device JARVIS_INPUT_DEVICE names is not there")
      }
      try input.auAudioUnit.setDeviceID(forced)
    }
    let format = input.outputFormat(forBus: 0)
    guard format.sampleRate > 0, format.channelCount > 0 else {
      throw EarsError("No microphone input is available")
    }
    guard Resampler(from: format) != nil else {
      throw EarsError("Unsupported microphone format")
    }
    resampler = nil  // made from the first buffer, below
    resamplerFormat = nil
    // No format is given to the tap: it takes whatever the input delivers. A format of our own
    // that is stale by then (earphones switching to call mode change the input under a running
    // engine) is refused with an exception, which cannot be caught and ends the app. The
    // converter follows the format of the buffers themselves.
    input.installTap(onBus: 0, bufferSize: 2048, format: nil) { [weak self] buffer, _ in
      guard let self else { return }
      if self.resampler == nil || self.resamplerFormat != buffer.format {
        self.resampler = Resampler(from: buffer.format)
        self.resamplerFormat = buffer.format
      }
      guard let converted = self.resampler?.convert(buffer) else { return }
      self.onAudio(converted.data, converted.peak)
    }
    tapFormat = format
    engine.prepare()
  }

  /// Starts the engine, whose tap is in place, and says so.
  private func begin() throws {
    do {
      try engine.start()
    } catch {
      engine.inputNode.removeTap(onBus: 0)  // or the next start would find a tap already there
      throw error
    }
    onStatus(
      "mic_started",
      [
        "sample_rate": tapFormat?.sampleRate ?? 0,
        "channels": Int(tapFormat?.channelCount ?? 0),
        "attempts": attempts,
      ])
  }

  func stop() {
    running = false
    if let observer { NotificationCenter.default.removeObserver(observer) }
    observer = nil
    engine.inputNode.removeTap(onBus: 0)
    engine.stop()
    if counted {
      counted = false
      DirectMicrophones.closed()
    }
  }
}

// MARK: - Microphones recorded straight from the device

/// Counts the microphones this process records straight from the device (DeviceCapture, MicCapture).
/// Voice processing changes the device's format under them, so it is never started while one runs.
enum DirectMicrophones {
  private static let lock = NSLock()
  private static var count = 0

  static var open: Int {
    lock.lock()
    defer { lock.unlock() }
    return count
  }

  static func opened() {
    lock.lock()
    count += 1
    lock.unlock()
  }

  static func closed() {
    lock.lock()
    count = max(0, count - 1)
    lock.unlock()
  }
}

// MARK: - One named microphone, straight from Core Audio

/// Records from one particular input device through Core Audio, without AVAudioEngine.
///
/// An engine always starts out bound to the system's default input, and merely setting one up
/// disturbs earphones that happen to be the default (their reported sample rate flips, even
/// though nothing is recorded). This never looks at the default input, so a spoken message
/// can be recorded from the Mac's own microphone and the earphones are left entirely alone.
/// It also hands sound over every IO cycle (about 10 ms) rather than in blocks of 100 ms, so the
/// first sound comes sooner and the end of a message is not cut off.
final class DeviceCapture {
  let device: AudioDeviceID
  /// The rate the device runs at, known once started.
  private(set) var rate = 0.0
  private let onAudio: (Data, Float) -> Void
  // Audio callbacks run here. Stopping waits for a callback in progress, so it must not be
  // called from this queue.
  private let ioQueue = DispatchQueue(label: "jarvis.app.voice.io", qos: .userInteractive)
  private var procID: AudioDeviceIOProcID?
  private var format: AVAudioFormat?
  private var resampler: Resampler?

  /// Receives each stretch of sound as 16 kHz mono float32 samples with its peak level.
  init(device: AudioDeviceID, onAudio: @escaping (Data, Float) -> Void) {
    self.device = device
    self.onAudio = onAudio
  }

  /// Voice processing (Speaker.swift) leaves the device in a format this cannot read for a moment
  /// after it stops: this long at most is waited for the ordinary one to come back.
  static let settleSeconds = 1.0

  func start() throws {
    guard device != AudioDeviceID(kAudioObjectUnknown) else {
      throw EarsError("The chosen input device is not there")
    }
    func readable() -> (AVAudioFormat, Resampler)? {
      guard var description = InputDevice.streamFormat(of: device),
        let format = AVAudioFormat(streamDescription: &description),
        format.sampleRate > 0, format.channelCount > 0,
        format.isInterleaved || format.channelCount == 1,
        let resampler = Resampler(from: format)
      else { return nil }
      return (format, resampler)
    }
    var found = readable()
    let began = Date()
    while found == nil && Date().timeIntervalSince(began) < DeviceCapture.settleSeconds {
      usleep(50_000)
      found = readable()
    }
    guard let (format, resampler) = found else { throw EarsError("Unsupported microphone format") }
    if Date().timeIntervalSince(began) > 0.01 {
      Log.write("microphone: its format settled after \(Int(Date().timeIntervalSince(began) * 1000)) ms")
    }
    self.format = format
    self.resampler = resampler
    rate = format.sampleRate
    var newProc: AudioDeviceIOProcID?
    try check(
      AudioDeviceCreateIOProcIDWithBlock(&newProc, device, ioQueue) {
        [weak self] _, inputData, _, _, _ in
        self?.process(inputData)
      }, "Creating the audio callback")
    procID = newProc
    let status = AudioDeviceStart(device, procID)
    if status != noErr {
      if let procID { AudioDeviceDestroyIOProcID(device, procID) }
      procID = nil
      throw EarsError("Starting the microphone failed (OSStatus \(status))")
    }
    DirectMicrophones.opened()
  }

  func stop() {
    guard let procID else { return }
    AudioDeviceStop(device, procID)
    AudioDeviceDestroyIOProcID(device, procID)
    self.procID = nil
    DirectMicrophones.closed()
  }

  private func process(_ inputData: UnsafePointer<AudioBufferList>) {
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inputData))
    guard let format, let resampler, let first = buffers.first, first.mData != nil,
      first.mDataByteSize > 0
    else { return }
    var single = AudioBufferList(mNumberBuffers: 1, mBuffers: first)
    let converted = withUnsafePointer(to: &single) { pointer -> (data: Data, peak: Float)? in
      guard
        let buffer = AVAudioPCMBuffer(
          pcmFormat: format, bufferListNoCopy: pointer, deallocator: nil),
        buffer.frameLength > 0
      else { return nil }
      return resampler.convert(buffer)
    }
    guard let converted else { return }
    onAudio(converted.data, converted.peak)
  }
}

func microphoneAllowed() -> Bool {
  switch AVCaptureDevice.authorizationStatus(for: .audio) {
  case .authorized:
    return true
  case .notDetermined:
    let done = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .audio) { allowed in
      granted = allowed
      done.signal()
    }
    done.wait()
    return granted
  default:
    return false
  }
}

// MARK: - Helper mode

func argumentValue(_ name: String) -> String? {
  let arguments = CommandLine.arguments
  guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
  return arguments[index + 1]
}

// Signal sources stop working once released, so they are kept here.
private var helperSignalSources: [DispatchSourceSignal] = []

/// Captures until the socket closes or the process is told to stop.
func runCaptureHelper(port: UInt16) -> Never {
  let link: Link
  do {
    link = try Link(port: port)
  } catch {
    fputs("Jarvis: \(error)\n", stderr)
    exit(1)
  }
  link.status("hello", ["version": 2, "pid": Int(getpid()), "sample_rate": targetSampleRate])

  var systemTap: SystemAudioTap?
  var micCapture: MicCapture?

  if !CommandLine.arguments.contains("--no-system") {
    var allowed = true
    switch AudioCapturePermission.state() {
    case .allowed:
      break
    case .denied:
      allowed = false
    case .unknown:
      link.status("system_permission_prompt")
      // nil means the prompt could not be shown; try to start anyway.
      if let granted = AudioCapturePermission.request() { allowed = granted }
    }
    if allowed {
      let tap = SystemAudioTap(link: link)
      do {
        try tap.start()
        systemTap = tap
      } catch {
        link.status("system_failed", ["error": "\(error)"])
      }
    } else {
      link.status("system_denied")
    }
  }

  if !CommandLine.arguments.contains("--no-mic") {
    if microphoneAllowed() {
      let capture = MicCapture(link: link)
      do {
        try capture.start()
        micCapture = capture
      } catch {
        link.status("mic_failed", ["error": "\(error)"])
      }
    } else {
      link.status("mic_denied")
    }
  }

  if systemTap == nil && micCapture == nil {
    link.status("nothing_started")
    link.flush()
    exit(1)
  }

  // Stop cleanly when asked to; a closed socket also ends the process (see Link.send).
  signal(SIGTERM, SIG_IGN)
  signal(SIGINT, SIG_IGN)
  let signalQueue = DispatchQueue(label: "jarvis.ears.signals")
  helperSignalSources = [SIGTERM, SIGINT].map { number -> DispatchSourceSignal in
    let source = DispatchSource.makeSignalSource(signal: number, queue: signalQueue)
    source.setEventHandler {
      micCapture?.stop()
      systemTap?.stop()
      link.status("stopped")
      link.flush()
      exit(0)
    }
    source.resume()
    return source
  }

  dispatchMain()
}
