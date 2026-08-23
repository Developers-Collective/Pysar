(function installProgressiveLoopClock(global) {
  "use strict";

  function normalizeStrmTrackSelection(tracks, selectedTrackIndices) {
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
    return validSelection.length ? validSelection : orderedTrackIndices;
  }

  function nextVisibleSoundId(visibleSoundIds, currentSoundId) {
    const orderedSoundIds = [];
    const seen = new Set();
    for (const item of Array.isArray(visibleSoundIds) ? visibleSoundIds : []) {
      const id = Number(item && typeof item === "object" ? item.id : item);
      if (!Number.isInteger(id) || id < 0 || seen.has(id)) continue;
      seen.add(id);
      orderedSoundIds.push(id);
    }

    const currentId = Number(
      currentSoundId && typeof currentSoundId === "object"
        ? currentSoundId.id
        : currentSoundId,
    );
    const position = orderedSoundIds.indexOf(currentId);
    return position >= 0 && position + 1 < orderedSoundIds.length
      ? orderedSoundIds[position + 1]
      : null;
  }

  function isSoundListTransport(sound) {
    return !sound?.kind && ["STRM", "SEQ", "WAVE"].includes(sound?.type);
  }

  function shouldPreserveSoundTransport(activeSound, requestedSound, isPlaying) {
    if (!isPlaying || !isSoundListTransport(activeSound) || !isSoundListTransport(requestedSound)) {
      return false;
    }
    return Number(activeSound.id) === Number(requestedSound.id);
  }

  function followAutoplayInSoundTab(tabs, activeTabId, currentSoundId, nextSound) {
    if (!nextSound) return tabs;
    let changed = false;
    const nextTabs = (Array.isArray(tabs) ? tabs : []).map((tab) => {
      if (
        tab?.id !== activeTabId
        || tab.kind !== "sound"
        || Number(tab.item?.id) !== Number(currentSoundId)
      ) {
        return tab;
      }
      changed = true;
      return { ...tab, item: nextSound, title: nextSound.name };
    });
    return changed ? nextTabs : tabs;
  }

  function createPlayheadStore(initialValue = 0) {
    let value = Math.max(0, Number(initialValue) || 0);
    const listeners = new Set();
    return {
      getSnapshot() {
        return value;
      },
      set(nextValue) {
        const next = Math.max(0, Number(nextValue) || 0);
        if (Object.is(next, value)) return;
        value = next;
        for (const listener of Array.from(listeners)) listener(value);
      },
      subscribe(listener) {
        if (typeof listener !== "function") return () => {};
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
  }

  function traceIndexAtOrBefore(trace, playheadMs) {
    const events = Array.isArray(trace) ? trace : [];
    const target = Number(playheadMs) || 0;
    let low = 0;
    let high = events.length - 1;
    let result = -1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if ((Number(events[middle]?.ms) || 0) <= target) {
        result = middle;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    return result;
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
  global.PysarStrmTrackSelection = normalizeStrmTrackSelection;
  global.PysarNextVisibleSoundId = nextVisibleSoundId;
  global.PysarIsSoundListTransport = isSoundListTransport;
  global.PysarShouldPreserveSoundTransport = shouldPreserveSoundTransport;
  global.PysarFollowAutoplayInSoundTab = followAutoplayInSoundTab;
  global.PysarCreatePlayheadStore = createPlayheadStore;
  global.PysarPlayheadStore = global.PysarPlayheadStore || createPlayheadStore();
  global.PysarTraceIndexAtOrBefore = traceIndexAtOrBefore;
})(typeof window !== "undefined" ? window : globalThis);
