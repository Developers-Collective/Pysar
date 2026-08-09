(function installProgressiveLoopClock(global) {
  "use strict";

  function decideStrmAutoplay(tracks, selectedTrackIndices, enabled, loopEnabled) {
    const orderedTrackIndices = [];
    const seen = new Set();
    for (const track of Array.isArray(tracks) ? tracks : []) {
      if (track == null) continue;
      const index = Number(track && typeof track === "object" ? track.index : track);
      if (!Number.isInteger(index) || index < 0 || seen.has(index)) continue;
      seen.add(index);
      orderedTrackIndices.push(index);
    }

    const requested = new Set(
      (Array.isArray(selectedTrackIndices) ? selectedTrackIndices : [])
        .map((index) => index == null ? Number.NaN : Number(index))
        .filter((index) => Number.isInteger(index) && seen.has(index)),
    );
    const validSelection = orderedTrackIndices.filter((index) => requested.has(index));
    const autoplayEnabled = !!enabled && orderedTrackIndices.length > 1;
    const currentTrackIndex = validSelection[0] ?? orderedTrackIndices[0] ?? null;
    const normalizedSelection = autoplayEnabled
      ? (currentTrackIndex == null ? [] : [currentTrackIndex])
      : (validSelection.length ? validSelection : orderedTrackIndices);

    let nextTrackIndex = null;
    if (autoplayEnabled && !loopEnabled && currentTrackIndex != null) {
      const position = orderedTrackIndices.indexOf(currentTrackIndex);
      if (position >= 0 && position + 1 < orderedTrackIndices.length) {
        nextTrackIndex = orderedTrackIndices[position + 1];
      }
    }
    return {
      enabled: autoplayEnabled,
      selectedTrackIndices: normalizedSelection,
      currentTrackIndex,
      nextTrackIndex,
    };
  }

  class ProgressiveLoopClock {
    constructor(sampleRate, startFrame, loopStartFrame, totalFrames, contextStartTime) {
      this.sampleRate = Math.max(1, Math.trunc(Number(sampleRate) || 1));
      this.totalFrames = Math.max(0, Math.trunc(Number(totalFrames) || 0));
      this.startFrame = Math.max(0, Math.min(this.totalFrames, Math.trunc(Number(startFrame) || 0)));
      this.loopStartFrame = Math.max(0, Math.min(this.totalFrames, Math.trunc(Number(loopStartFrame) || 0)));
      this.contextStartTime = Math.max(0, Number(contextStartTime) || 0);
      this.initialFrames = this.totalFrames - this.startFrame;
      this.loopFrames = this.totalFrames - this.loopStartFrame;
    }

    anchor(contextTime, leadSeconds = 0.03) {
      this.contextStartTime = Math.max(0, Number(contextTime) || 0)
        + Math.max(0, Number(leadSeconds) || 0);
      return this.contextStartTime;
    }

    initialTime(localFrame) {
      const frame = Math.max(0, Math.min(this.initialFrames, Math.trunc(Number(localFrame) || 0)));
      return this.contextStartTime + frame / this.sampleRate;
    }

    get firstEndTime() {
      return this.initialTime(this.initialFrames);
    }

    loopTime(localFrame, passIndex = 0) {
      const frame = Math.max(0, Math.min(this.loopFrames, Math.trunc(Number(localFrame) || 0)));
      const pass = Math.max(0, Math.trunc(Number(passIndex) || 0));
      return this.firstEndTime + (pass * this.loopFrames + frame) / this.sampleRate;
    }

    absoluteFrameAt(contextTime, loopEnabled = true) {
      const elapsed = Math.max(0, Number(contextTime) - this.contextStartTime);
      const elapsedFrames = Math.max(0, Math.floor(elapsed * this.sampleRate + 1.0e-7));
      if (elapsedFrames < this.initialFrames) return this.startFrame + elapsedFrames;
      if (!loopEnabled || this.loopFrames <= 0) return this.totalFrames;
      return this.loopStartFrame + ((elapsedFrames - this.initialFrames) % this.loopFrames);
    }
  }

  global.PysarProgressiveLoopClock = ProgressiveLoopClock;
  global.PysarStrmAutoplayDecision = decideStrmAutoplay;
})(typeof window !== "undefined" ? window : globalThis);
