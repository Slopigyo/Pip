/* Local capture only: bounded PCM16 chunks, no network or persistent storage. */
class PipSpeechRecorder extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const maxFrames = options.processorOptions?.maxFrames;
    if (!Number.isInteger(sampleRate) || sampleRate < 8000 || sampleRate > 96000 || !Number.isInteger(maxFrames) || maxFrames <= 0 || maxFrames > sampleRate * 120) {
      throw new Error("The recorder requires bounded native-rate audio (8–96 kHz).");
    }
    this.maxFrames = maxFrames;
    this.frames = 0;
    this.used = 0;
    this.chunk = new Int16Array(4096);
    this.stopped = false;
    this.port.onmessage = ({ data }) => { if (data?.type === "stop") this.finish(); };
  }

  flush() {
    if (!this.used) return;
    const pcm = this.used === this.chunk.length ? this.chunk : this.chunk.slice(0, this.used);
    this.port.postMessage({ type: "chunk", pcm }, [pcm.buffer]);
    this.chunk = new Int16Array(4096);
    this.used = 0;
  }

  finish() {
    if (this.stopped) return;
    this.stopped = true;
    this.flush();
    this.port.postMessage({ type: "done", frames: this.frames });
  }

  process(inputs, outputs) {
    // A connected silent output keeps the graph active without playing back the microphone.
    for (const output of outputs) for (const channel of output) channel.fill(0);
    if (this.stopped) return false;
    const channels = inputs[0];
    if (!channels?.length) return true;
    const count = Math.min(channels[0].length, this.maxFrames - this.frames);
    for (let i = 0; i < count; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i];
      value /= channels.length;
      value = Number.isFinite(value) ? Math.max(-1, Math.min(1, value)) : 0;
      this.chunk[this.used++] = Math.round(value * (value < 0 ? 32768 : 32767));
      this.frames += 1;
      if (this.used === this.chunk.length) this.flush();
    }
    if (this.frames >= this.maxFrames) this.finish();
    return !this.stopped;
  }
}

registerProcessor("pip-speech-recorder", PipSpeechRecorder);
