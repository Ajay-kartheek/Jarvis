// Jarvis's voice, played by the app, and the microphone the ear hears while he speaks
//
// Inside the app the page does not play Jarvis's voice itself: it hands each sentence to the app
// (bridge action "speech" with id, seq, text and url, and "speech_stop"), and the app plays them in
// order through one AVAudioEngine. It tells the page when a sentence starts, when the voice has gone
// quiet or was stopped, and how loud it is (the orb follows it), and it tells the daemon when the
// voice starts and stops sounding (POST /api/voice/playing), as the page used to.
//
// On the Mac's own speakers the same engine has voice processing on its input: echo cancellation,
// with what the engine plays as its reference. The ear then hears the user over Jarvis's voice and
// can stop him by any speech, not only by his name. Measured on a MacBook Air at full volume, his
// voice comes out of the canceller about 100 dB down, from the first syllable on. (macOS cancels the
// sound of other apps too, so a test cannot stand in for the user with a sound the Mac plays.)
// Voice processing is never used with headphones, which need no cancelling, nor with a Bluetooth
// microphone (it would switch earphones to call mode), nor while a call is on.
//
// Voice processing changes the microphone for every app while it runs (its format becomes one that
// a recording made straight from the device cannot read: the installed copy's ear lost its microphone
// whenever a test ran it), and it turns other sound down a little and costs energy. So it runs only
// during a conversation: from the moment a spoken message has ended (the key let go, or the page told
// of a spoken turn: bridge action "speech_soon"), or Jarvis begins to say something by himself, until
// 20 s after his voice last went quiet ("speech_keep" from the page keeps it open while the user is
// still talking). Its start (about 1.2 s) overlaps the time Claude takes to answer. Outside a
// conversation the ear hears the plain microphone, as before. While voice processing runs, every
// microphone the app itself needs is taken from this engine (EchoCancelledMicrophone), and it is never
// started while the app records straight from the device (DirectMicrophones): the ear lets go of its
// plain microphone first, and takes it again only once the engine has stopped.

import AVFoundation
import AudioToolbox
import Foundation

/// The echo-cancelled microphone, as a sound source for the ear or a spoken message: 16 kHz mono
/// float32, as from any other source.
final class EchoCancelledMicrophone: SoundSource {
  private let speaker: Speaker
  private let onAudio: (Data, Float) -> Void
  private var listener: Int?

  init(speaker: Speaker, onAudio: @escaping (Data, Float) -> Void) {
    self.speaker = speaker
    self.onAudio = onAudio
  }

  func start() throws {
    guard listener == nil else { return }
    listener = try speaker.addListener(onAudio)
  }

  func stop() {
    if let listener { speaker.removeListener(listener) }
    listener = nil
  }
}

final class Speaker {
  enum Mode: String {
    case off
    case output  // playing, no microphone
    case duplex  // voice processing: the echo-cancelled microphone, and playing
  }

  /// Every sentence is played in this format; the engine's mixer takes it to the output's.
  static let playFormat = AVAudioFormat(
    commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
  /// Between sentences the voice still counts as speaking for this long.
  static let lingerSeconds = 0.7
  /// A sentence that has not arrived by then, while later ones have, is passed over.
  static let skipAfterSeconds = 4.0
  /// The engine stays up this long after the last sound, or after the microphone was let go, so
  /// that the next sentence or the ear coming back does not have to start it again.
  static let idleSeconds = 2.0
  /// After the engine failed to start with voice processing, it is not tried again for this long.
  static let retrySeconds: TimeInterval = 60
  /// A conversation lasts this long after his voice last went quiet (or after it opened, if he has not
  /// spoken yet). The test copy can shorten it with JARVIS_TEST_CONVERSATION_SECONDS.
  static let conversationSeconds: TimeInterval =
    TestMode.seconds("JARVIS_TEST_CONVERSATION_SECONDS") ?? 20
  /// What he says first in a conversation waits at most this long for the echo-cancelled engine.
  static let firstWordWaitSeconds = 1.5
  private static let levelHz = 25.0
  private static let hushedKept = 50

  /// The daemon's port, for fetching the sentences.
  var port = AppConfig.defaultPort
  /// JSON for the page (window.jarvisVoice); called on the main queue.
  var onPageEvent: ((String) -> Void)?
  /// The voice of speech `id` started or stopped sounding; called on the main queue.
  var onPlaying: ((_ id: String, _ playing: Bool) -> Void)?
  /// A conversation began or ended (the ear changes microphone); called on the main queue.
  var onConversationChange: (() -> Void)?

  // Everything below is touched on this queue only, except where it says otherwise.
  private let queue = DispatchQueue(label: "jarvis.app.speaker", qos: .userInitiated)
  private var engine: AVAudioEngine?
  private var player: AVAudioPlayerNode?
  private var inputUsed = false  // the engine's input was made (and has voice processing on)
  private var mode = Mode.off
  private var observer: NSObjectProtocol?
  private var allowed = false  // voice processing may be used now (speakers, the setting, no Bluetooth microphone)
  private var callActive = false
  private var failedAt: Date?
  private var micUntil = Date.distantPast  // the microphone was let go; the engine keeps it a moment
  private var outputUntil = Date.distantPast  // the voice went quiet; the engine keeps playing a moment
  private var idleCheck: DispatchWorkItem?
  private var conversationUntil = Date.distantPast  // distantFuture while he speaks
  private var conversationOpen = false  // as last told
  private var conversationOpenedAt = DispatchTime.now()
  private var earExpected = false  // the ear wants a microphone now: it will take the echo-cancelled one
  private var holdSince: DispatchTime?  // a sentence has been waiting for the engine since then
  private var waitingForDevice = false  // voice processing is wanted, but the device is being recorded

  // The microphone's listeners, read on the audio thread too.
  private let listenerLock = NSLock()
  private var listeners: [Int: (Data, Float) -> Void] = [:]
  private var nextListener = 0
  private var resampler: Resampler?  // audio thread only
  private var resamplerFormat: AVAudioFormat?

  // The speech.
  private final class Session {
    let id: String
    var clips: [Int: (buffer: AVAudioPCMBuffer?, text: String)] = [:]
    var next: Int
    var playing: Int?  // the sentence being played
    var generation = 0  // counts the times its sentence was handed to a player
    var firstReady: DispatchTime?  // when its first sentence arrived, until it is played
    var started = false  // a sentence of it has been played
    init(id: String, next: Int) {
      self.id = id
      self.next = next
    }
  }
  private var session: Session?
  private var hushed: [String] = []
  private var skip: DispatchWorkItem?
  private var linger: DispatchWorkItem?
  private var sounding: String?  // the speech the daemon was last told is playing
  private let fetcher: URLSession = {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = 20
    configuration.connectionProxyDictionary = [:]
    return URLSession(configuration: configuration)
  }()

  // How loud the voice is: written on the audio thread, read on the main queue.
  private let levelLock = NSLock()
  private var latestLevel: Float = 0
  private var levelTimer: DispatchSourceTimer?

  // Test copy only: a sound file mixed into the echo-cancelled microphone, this long after a speech
  // starts playing (JARVIS_TEST_MIX=<16 kHz mono WAV>@<seconds>), to stand in for the user talking
  // over Jarvis. When it begins and when the voice stops are logged.
  private let testMix: (samples: [Float], after: Double)? = Speaker.loadTestMix()
  private var mixAt = 0  // audio thread: where in the mix it is; -1 when not mixing
  private var mixArmed: DispatchWorkItem?
  private var mixBegan: DispatchTime?

  init() {
    mixAt = -1
  }

  // MARK: What the app tells it

  /// Whether voice processing may be used: the Mac plays through its speakers (or others the
  /// microphone hears), the input is not a Bluetooth one, and the setting allows it.
  func setVoiceProcessingAllowed(_ value: Bool) {
    queue.async {
      guard value != self.allowed else { return }
      self.allowed = value
      Log.write("speaker: echo cancellation \(value ? "may be used" : "is not used now")")
      self.reconcile()
    }
  }

  /// A call is on, or being listened to: the microphone is let go at once, and the conversation
  /// is over.
  func setCallActive(_ value: Bool) {
    queue.async {
      guard value != self.callActive else { return }
      self.callActive = value
      if value {
        self.conversationUntil = .distantPast
        self.micUntil = .distantPast
      }
      self.reconcile()
      if value { Log.write("speaker: a call: the engine is \(self.mode.rawValue)") }
    }
  }

  /// Whether the ear wants a microphone now (it will take the echo-cancelled one in a conversation).
  func setEarExpected(_ value: Bool) {
    queue.async { self.earExpected = value }
  }

  /// A conversation begins, or goes on: a spoken message has just ended, so an answer is coming.
  func openConversation(reason: String) {
    queue.async { self.extendConversation(reason: reason) }
  }

  /// The user is still talking: a conversation that is open stays open; none is opened.
  func keepConversation() {
    queue.async {
      guard Date() < self.conversationUntil else { return }
      self.extendConversation(reason: nil)
    }
  }

  /// Whether a microphone taken now should be the echo-cancelled one: in a conversation, on the
  /// speakers, with no call.
  var echoCancellationWanted: Bool {
    queue.sync { allowed && !callActive && !recentlyFailed && Date() < conversationUntil }
  }

  /// How long ago the conversation began, for the log.
  var conversationAge: Int {
    queue.sync {
      Int((DispatchTime.now().uptimeNanoseconds &- conversationOpenedAt.uptimeNanoseconds) / 1_000_000)
    }
  }

  /// The echo-cancelled microphone is open now, so the device itself cannot be recorded from.
  var microphoneOpen: Bool { queue.sync { mode == .duplex } }

  private var recentlyFailed: Bool {
    failedAt.map { Date().timeIntervalSince($0) < Speaker.retrySeconds } ?? false
  }

  /// Takes the echo-cancelled microphone; throws if it cannot be had now.
  func addListener(_ onAudio: @escaping (Data, Float) -> Void) throws -> Int {
    try queue.sync {
      guard allowed, !callActive, !recentlyFailed, Date() < conversationUntil else {
        throw EarsError("echo cancellation cannot be used now")
      }
      guard DirectMicrophones.open == 0 else {
        throw EarsError("the microphone is being recorded straight from the device")
      }
      listenerLock.lock()
      nextListener += 1
      let id = nextListener
      listeners[id] = onAudio
      listenerLock.unlock()
      reconcile()
      guard mode == .duplex else {
        listenerLock.lock()
        listeners[id] = nil
        listenerLock.unlock()
        throw EarsError("the echo-cancelled microphone did not start")
      }
      return id
    }
  }

  /// Lets go of the echo-cancelled microphone. Waits until the engine has let go of it too, when
  /// no one else needs it and the conversation is over, so that the device can be opened next.
  func removeListener(_ id: Int) {
    queue.sync {
      listenerLock.lock()
      let had = listeners.removeValue(forKey: id) != nil
      let left = listeners.count
      listenerLock.unlock()
      if had && left == 0 && Date() < conversationUntil {
        micUntil = Date().addingTimeInterval(Speaker.idleSeconds)  // the ear comes back after a message
      }
      reconcile()
    }
  }

  /// One sentence to say, from the page. A different id from the one playing replaces it.
  func enqueue(id: String, seq: Int, text: String, url: String) {
    queue.async {
      guard !self.hushed.contains(id) else { return }
      if self.session?.id != id {
        self.stopLocked(reason: nil, tellPage: false)
        self.session = Session(id: id, next: seq)
        self.extendConversation(reason: "he is about to say something")
      }
      guard let mine = self.session,
        let address = URL(string: url, relativeTo: URL(string: "http://127.0.0.1:\(self.port)"))
      else { return }
      self.fetcher.dataTask(with: address) { [weak self] data, response, _ in
        let ok = (response as? HTTPURLResponse)?.statusCode == 200
        let buffer = ok ? data.flatMap(Speaker.decode) : nil
        let arrived = DispatchTime.now()
        self?.queue.async {
          guard let self, self.session === mine else { return }  // stopped or replaced meanwhile
          if buffer == nil { Log.write("speaker: sentence \(seq) of \(id) could not be played") }
          mine.clips[seq] = (buffer, text)
          if !mine.started, mine.firstReady == nil, buffer != nil { mine.firstReady = arrived }
          self.pump()
        }
      }.resume()
    }
  }

  /// Stops talking and forgets what was queued and what is still to come of it. Says whether
  /// anything was being said or waited to be said.
  @discardableResult
  func stop(reason: String, tellPage: Bool = false) -> Bool {
    queue.sync { stopLocked(reason: reason, tellPage: tellPage) }
  }

  /// The same, without waiting (the main thread must not wait for an engine that is starting).
  func hush(reason: String, tellPage: Bool = false) {
    queue.async { _ = self.stopLocked(reason: reason, tellPage: tellPage) }
  }

  /// The app is quitting.
  func shutdown() {
    queue.sync {
      _ = stopLocked(reason: nil, tellPage: false)
      listenerLock.lock()
      listeners = [:]
      listenerLock.unlock()
      stopEngine()
    }
  }

  // MARK: Playing

  private func pump() {
    guard let now = session, now.playing == nil else { return }
    skip?.cancel()
    while true {
      guard let clip = now.clips[now.next] else {
        // Not here yet. If later sentences are waiting, give this one a while, then pass over it.
        let later = now.clips.keys.filter { $0 > now.next }
        if let first = later.min() {
          let work = DispatchWorkItem { [weak self] in
            guard let self, self.session === now, now.playing == nil, now.clips[now.next] == nil
            else { return }
            now.next = first
            self.pump()
          }
          skip = work
          queue.asyncAfter(deadline: .now() + Speaker.skipAfterSeconds, execute: work)
        }
        if linger == nil, sounding != nil {
          let work = DispatchWorkItem { [weak self] in self?.wentQuiet() }
          linger = work
          queue.asyncAfter(deadline: .now() + Speaker.lingerSeconds, execute: work)
        }
        return
      }
      guard let buffer = clip.buffer else {
        now.clips[now.next] = nil
        now.next += 1
        continue
      }
      outputUntil = .distantFuture
      conversationUntil = .distantFuture  // open as long as he speaks
      // In a conversation the ear is moving to the echo-cancelled engine: wait for it a moment
      // rather than start a plain output and have to start again.
      if mode != .duplex, allowed, !callActive, !recentlyFailed, earExpected {
        let since = holdSince ?? .now()
        holdSince = since
        let waited = Double(DispatchTime.now().uptimeNanoseconds &- since.uptimeNanoseconds) / 1e9
        if waited < Speaker.firstWordWaitSeconds {
          queue.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.pump() }
          return
        }
        Log.write("speaker: the echo-cancelled engine did not come within \(Speaker.firstWordWaitSeconds) s; playing without it")
      }
      holdSince = nil
      reconcile()
      guard let player, mode != .off else {
        Log.write("speaker: there is no sound output; sentence \(now.next) of \(now.id) passed over")
        now.clips[now.next] = nil
        now.next += 1
        outputUntil = Date()
        continue
      }
      linger?.cancel()
      linger = nil
      if !now.started, let ready = now.firstReady {
        // What he says first in a conversation can wait for the engine to start: how long it did.
        let waited = Int((DispatchTime.now().uptimeNanoseconds &- ready.uptimeNanoseconds) / 1_000_000)
        if waited >= 20 { Log.write("speaker: the first sentence waited \(waited) ms for the engine") }
      }
      now.started = true
      let seq = now.next
      now.playing = seq
      now.generation += 1
      let generation = now.generation
      player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
        self?.queue.async { self?.finished(now, seq, generation) }
      }
      if !player.isPlaying { player.play() }
      tellPage(["type": "playing", "id": now.id, "seq": seq, "text": clip.text])
      tellDaemon(now.id)
      startLevels()
      armTestMix()
      return
    }
  }

  private func finished(_ played: Session, _ seq: Int, _ generation: Int) {
    guard session === played, played.playing == seq, played.generation == generation else { return }
    played.playing = nil
    played.clips[seq] = nil
    played.next = seq + 1
    pump()
  }

  /// Nothing more to say for now: the gap after the last sentence was long enough.
  private func wentQuiet() {
    linger = nil
    guard session?.playing == nil else { return }
    stopLevels()
    if let id = sounding { tellPage(["type": "quiet", "id": id]) }
    tellDaemon(nil)
    outputUntil = Date().addingTimeInterval(Speaker.idleSeconds)
    quietedConversation()
    scheduleIdleCheck()
  }

  /// His voice went quiet (or was stopped): the conversation lasts a while longer.
  private func quietedConversation() {
    if conversationUntil == .distantFuture {
      conversationUntil = Date().addingTimeInterval(Speaker.conversationSeconds)
    }
  }

  private func extendConversation(reason: String?) {
    guard !callActive else { return }  // no conversation with Jarvis's ears during a call
    let until = Date().addingTimeInterval(Speaker.conversationSeconds)
    if conversationUntil != .distantFuture && until > conversationUntil { conversationUntil = until }
    if !conversationOpen {
      conversationOpen = true
      conversationOpenedAt = .now()
      Log.write("speaker: a conversation begins (\(reason ?? "the user is talking"))")
      DispatchQueue.main.async { [weak self] in self?.onConversationChange?() }
    }
    scheduleIdleCheck()
  }

  private func stopLocked(reason: String?, tellPage page: Bool) -> Bool {
    skip?.cancel()
    skip = nil
    linger?.cancel()
    linger = nil
    mixArmed?.cancel()
    mixArmed = nil
    guard let old = session else { return false }
    hushed.append(old.id)
    if hushed.count > Speaker.hushedKept { hushed.removeFirst(hushed.count - Speaker.hushedKept) }
    session = nil
    let wasSaying = old.playing != nil || !old.clips.isEmpty || sounding != nil
    player?.stop()  // drops what was scheduled; its completion finds the session gone
    stopLevels()
    if let reason, wasSaying {
      var line = "speaker: stopped (\(reason))"
      if let began = mixBegan {
        line += "; \(Int((DispatchTime.now().uptimeNanoseconds &- began.uptimeNanoseconds) / 1_000_000)) ms"
          + " after the test sound began"
      }
      Log.write(line)
    }
    mixBegan = nil
    holdSince = nil
    if page { tellPage(["type": "stopped", "id": old.id]) }
    tellDaemon(nil)
    outputUntil = Date().addingTimeInterval(Speaker.idleSeconds)
    quietedConversation()
    scheduleIdleCheck()
    return wasSaying
  }

  // MARK: The engine

  /// Brings the engine in line with what is needed now.
  private func reconcile() {
    let now = Date()
    let inConversation = now < conversationUntil && !callActive
    if conversationOpen && !inConversation {
      conversationOpen = false
      micUntil = .distantPast
      Log.write("speaker: the conversation is over")
      DispatchQueue.main.async { [weak self] in self?.onConversationChange?() }
    }
    listenerLock.lock()
    let listening = !listeners.isEmpty
    listenerLock.unlock()
    let micWanted =
      allowed && !callActive && !recentlyFailed && inConversation && (listening || now < micUntil)
    let outputWanted = session != nil && now < outputUntil
    var wanted: Mode = micWanted ? .duplex : outputWanted ? .output : .off
    if wanted == .duplex && mode != .duplex && DirectMicrophones.open > 0 {
      // Never while the app records straight from the device: whoever does lets go first.
      if !waitingForDevice {
        Log.write("speaker: voice processing waits: the microphone is being recorded straight from the device")
      }
      waitingForDevice = true
      wanted = outputWanted ? .output : .off
    } else {
      waitingForDevice = false
    }
    if wanted != mode { restart(wanted) }
    scheduleIdleCheck()
  }

  /// Looks again when a moment of grace, or the conversation, runs out.
  private func scheduleIdleCheck() {
    idleCheck?.cancel()
    let next = [micUntil, outputUntil, conversationUntil].filter { $0 > Date() && $0 != .distantFuture }.min()
    guard let next else { return }
    let work = DispatchWorkItem { [weak self] in self?.reconcile() }
    idleCheck = work
    queue.asyncAfter(deadline: .now() + max(0.05, next.timeIntervalSinceNow + 0.05), execute: work)
  }

  private func restart(_ wanted: Mode) {
    let resume = session?.playing != nil
    stopEngine()
    if let current = session, current.playing != nil {
      current.playing = nil  // said again from its start once the engine is back
    }
    guard wanted != .off else { return }
    let began = DispatchTime.now()
    do {
      try startEngine(wanted)
    } catch {
      stopEngine()
      if wanted == .duplex {
        failedAt = Date()
        Log.write("speaker: voice processing did not start (\(error)); not tried again for a minute")
        if session != nil {
          do { try startEngine(.output) } catch { stopEngine() }
        }
      } else {
        Log.write("speaker: the sound output did not start (\(error))")
      }
    }
    let took = Int((DispatchTime.now().uptimeNanoseconds &- began.uptimeNanoseconds) / 1_000_000)
    Log.write("speaker: engine \(mode.rawValue) (took \(took) ms)")
    if resume || session != nil { pump() }
  }

  private func startEngine(_ wanted: Mode) throws {
    let engine = AVAudioEngine()
    // Only the duplex engine touches its input: merely making it opens the default input device.
    let input: AVAudioInputNode? = wanted == .duplex ? engine.inputNode : nil
    self.engine = engine
    inputUsed = input != nil
    if let input {
      try input.setVoiceProcessingEnabled(true)
      // Its own gain control on: without it a voice across the room reached the ear about 10 dB
      // quieter, where the voice model starts to doubt it. In a quiet room it raised nothing.
      input.isVoiceProcessingAGCEnabled = true
      // Music, a video or a call app are turned down as little as voice processing allows.
      input.voiceProcessingOtherAudioDuckingConfiguration =
        AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
          enableAdvancedDucking: false, duckingLevel: .min)
      // The mixer must run at the input's rate or the voice-processing unit will not start.
      let rate = input.outputFormat(forBus: 0).sampleRate
      guard rate > 0, let mixFormat = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)
      else { throw EarsError("voice processing has no input format") }
      engine.connect(engine.mainMixerNode, to: engine.outputNode, format: mixFormat)
    }
    let player = AVAudioPlayerNode()
    engine.attach(player)
    engine.connect(player, to: engine.mainMixerNode, format: Speaker.playFormat)
    player.volume = Float(TestMode.text("JARVIS_TEST_VOLUME").flatMap(Double.init) ?? 1)
    input?.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
      self?.heard(buffer)
    }
    engine.mainMixerNode.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
      self?.measure(buffer)
    }
    engine.prepare()
    self.engine = engine
    self.player = player
    try engine.start()
    mode = wanted
    observer = NotificationCenter.default.addObserver(
      forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
    ) { [weak self] _ in
      // Another output or input device: set up again once it has settled.
      self?.queue.asyncAfter(deadline: .now() + 0.5) {
        guard let self, self.engine === engine else { return }
        Log.write("speaker: the sound devices changed; starting the engine again")
        let current = self.mode
        self.mode = .off
        self.restart(current)
        self.reconcile()
      }
    }
  }

  private func stopEngine() {
    if let observer { NotificationCenter.default.removeObserver(observer) }
    observer = nil
    if let engine {
      let duplex = inputUsed
      if duplex { engine.inputNode.removeTap(onBus: 0) }
      engine.mainMixerNode.removeTap(onBus: 0)
      player?.stop()
      engine.stop()
      if duplex { try? engine.inputNode.setVoiceProcessingEnabled(false) }
    }
    engine = nil
    player = nil
    inputUsed = false
    if mode != .off { Log.write("speaker: engine stopped") }
    mode = .off
  }

  // MARK: The microphone (audio thread)

  private func heard(_ buffer: AVAudioPCMBuffer) {
    // Voice processing gives the same processed sound on every channel: the first is taken.
    guard buffer.frameLength > 0, let channels = buffer.floatChannelData else { return }
    let rate = buffer.format.sampleRate
    guard let mono = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1),
      let single = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: buffer.frameLength)
    else { return }
    single.frameLength = buffer.frameLength
    if buffer.format.isInterleaved {
      let count = Int(buffer.format.channelCount)
      for index in 0..<Int(buffer.frameLength) {
        single.floatChannelData![0][index] = channels[0][index * count]
      }
    } else {
      memcpy(single.floatChannelData![0], channels[0], Int(buffer.frameLength) * 4)
    }
    if resampler == nil || resamplerFormat != mono {
      resampler = Resampler(from: mono)
      resamplerFormat = mono
    }
    guard var converted = resampler?.convert(single) else { return }
    if mixAt >= 0, let mix = testMix {
      var samples = [Float](repeating: 0, count: converted.data.count / 4)
      _ = samples.withUnsafeMutableBytes { converted.data.copyBytes(to: $0) }
      for index in samples.indices where mixAt < mix.samples.count {
        samples[index] += mix.samples[mixAt]
        mixAt += 1
      }
      if mixAt >= mix.samples.count { mixAt = -1 }
      converted.data = samples.withUnsafeBufferPointer { Data(buffer: $0) }
      converted.peak = samples.map(abs).max() ?? converted.peak
    }
    listenerLock.lock()
    let all = Array(listeners.values)
    listenerLock.unlock()
    for listener in all { listener(converted.data, converted.peak) }
  }

  // MARK: Levels and telling others

  private func measure(_ buffer: AVAudioPCMBuffer) {
    guard let channels = buffer.floatChannelData, buffer.frameLength > 0 else { return }
    var sum: Float = 0
    let count = Int(buffer.frameLength)
    for index in 0..<count { sum += channels[0][index] * channels[0][index] }
    levelLock.lock()
    latestLevel = (sum / Float(count)).squareRoot()
    levelLock.unlock()
  }

  private func startLevels() {
    DispatchQueue.main.async { [weak self] in
      guard let self, self.levelTimer == nil else { return }
      let timer = DispatchSource.makeTimerSource(queue: .main)
      timer.schedule(deadline: .now(), repeating: 1 / Speaker.levelHz, leeway: .milliseconds(5))
      timer.setEventHandler { [weak self] in
        guard let self else { return }
        self.levelLock.lock()
        let level = self.latestLevel
        self.levelLock.unlock()
        self.onPageEvent?("{\"type\":\"level\",\"level\":\(String(format: "%.4f", level))}")
      }
      timer.resume()
      self.levelTimer = timer
    }
  }

  private func stopLevels() {
    DispatchQueue.main.async { [weak self] in
      self?.levelTimer?.cancel()
      self?.levelTimer = nil
    }
    levelLock.lock()
    latestLevel = 0
    levelLock.unlock()
  }

  private func tellPage(_ event: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: event),
      let text = String(data: data, encoding: .utf8)
    else { return }
    DispatchQueue.main.async { [weak self] in self?.onPageEvent?(text) }
  }

  /// Notes which speech is sounding, nil for none, and tells the daemon of the change.
  private func tellDaemon(_ id: String?) {
    guard sounding != id else { return }
    let before = sounding
    sounding = id
    DispatchQueue.main.async { [weak self] in
      if let before { self?.onPlaying?(before, false) }
      if let id { self?.onPlaying?(id, true) }
    }
  }

  // MARK: The test copy's stand-in for the user

  private static func loadTestMix() -> (samples: [Float], after: Double)? {
    guard let spec = TestMode.text("JARVIS_TEST_MIX") else { return nil }
    let parts = spec.split(separator: "@")
    guard parts.count == 2, let after = Double(parts[1]),
      let file = try? AVAudioFile(forReading: URL(fileURLWithPath: String(parts[0]))),
      let buffer = AVAudioPCMBuffer(
        pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
      (try? file.read(into: buffer)) != nil, file.processingFormat.sampleRate == targetSampleRate,
      let channel = buffer.floatChannelData?[0]
    else {
      Log.write("test: JARVIS_TEST_MIX must be <16 kHz mono sound file>@<seconds>")
      return nil
    }
    return (Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength))), after)
  }

  private func armTestMix() {
    guard let mix = testMix, mixArmed == nil, mixBegan == nil else { return }
    let work = DispatchWorkItem { [weak self] in
      guard let self, self.session != nil, self.mode == .duplex else { return }
      self.mixBegan = .now()
      self.mixAt = 0
      Log.write("test: the stand-in for the user begins (\(mix.samples.count) samples)")
    }
    mixArmed = work
    queue.asyncAfter(deadline: .now() + mix.after, execute: work)
  }

  // MARK: Decoding a sentence, in memory

  private final class Memory {
    let data: Data
    init(_ data: Data) { self.data = data }
  }

  /// MP3 (the cloud voice) or WAV (the voice on this Mac), as 48 kHz mono float32.
  static func decode(_ data: Data) -> AVAudioPCMBuffer? {
    let box = Unmanaged.passRetained(Memory(data))
    defer { box.release() }
    let hint: AudioFileTypeID = data.starts(with: Array("RIFF".utf8)) ? kAudioFileWAVEType : kAudioFileMP3Type
    var file: AudioFileID?
    let read: AudioFile_ReadProc = { client, position, count, buffer, actual in
      let memory = Unmanaged<Memory>.fromOpaque(client).takeUnretainedValue()
      let start = Int(position)
      guard start >= 0, start < memory.data.count else {
        actual.pointee = 0
        return noErr
      }
      let length = min(Int(count), memory.data.count - start)
      memory.data.copyBytes(
        to: buffer.assumingMemoryBound(to: UInt8.self), from: start..<(start + length))
      actual.pointee = UInt32(length)
      return noErr
    }
    let size: AudioFile_GetSizeProc = { client in
      Int64(Unmanaged<Memory>.fromOpaque(client).takeUnretainedValue().data.count)
    }
    guard AudioFileOpenWithCallbacks(box.toOpaque(), read, nil, size, nil, hint, &file) == noErr,
      let file
    else { return nil }
    defer { AudioFileClose(file) }
    var reader: ExtAudioFileRef?
    guard ExtAudioFileWrapAudioFileID(file, false, &reader) == noErr, let reader else { return nil }
    defer { ExtAudioFileDispose(reader) }
    var client = playFormat.streamDescription.pointee
    guard
      ExtAudioFileSetProperty(
        reader, kExtAudioFileProperty_ClientDataFormat,
        UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &client) == noErr
    else { return nil }
    var samples: [Float] = []
    let chunk = 8192
    var piece = [Float](repeating: 0, count: chunk)
    while true {
      var frames = UInt32(chunk)
      let status = piece.withUnsafeMutableBytes { raw -> OSStatus in
        var list = AudioBufferList(
          mNumberBuffers: 1,
          mBuffers: AudioBuffer(
            mNumberChannels: 1, mDataByteSize: UInt32(raw.count), mData: raw.baseAddress))
        return ExtAudioFileRead(reader, &frames, &list)
      }
      guard status == noErr else { return nil }
      if frames == 0 { break }
      samples.append(contentsOf: piece[0..<Int(frames)])
      if samples.count > 48_000 * 120 { break }  // no sentence is two minutes long
    }
    guard !samples.isEmpty,
      let buffer = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(samples.count))
    else { return nil }
    buffer.frameLength = AVAudioFrameCount(samples.count)
    samples.withUnsafeBufferPointer {
      buffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count)
    }
    return buffer
  }
}
