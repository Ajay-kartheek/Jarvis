// Loudness numbers that move too fast for React state. The waveform and the orb read them once a frame.

type LevelSource = "system" | "mic" | "voice";

/**
 * Latest level per source, 0 to 1, with the time it arrived. They come about ten times a second.
 * `system` is the other side of a call, `mic` is the user during a call, and `voice` is the user's
 * microphone while the key is held to talk to Jarvis.
 */
export const levels: Record<LevelSource, { level: number; at: number }> = {
  system: { level: 0, at: 0 },
  mic: { level: 0, at: 0 },
  voice: { level: 0, at: 0 },
};

/** How loud Jarvis's own voice is right now, 0 to 1. Written by the audio player, read by the orb. */
export const voiceOut = { level: 0 };
