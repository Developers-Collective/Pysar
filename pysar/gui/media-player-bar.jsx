const { useState: useStateT } = React;

function formatMediaTime(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "0:00.000";
  const total = Math.floor(ms);
  const minutes = Math.floor(total / 60000);
  const seconds = Math.floor((total % 60000) / 1000);
  const millis = String(total % 1000).padStart(3, "0");
  return `${minutes}:${String(seconds).padStart(2, "0")}.${millis}`;
}

const TP = {
  Play: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M5 3.5v9l7-4.5-7-4.5z" fill="currentColor" />
    </svg>
  ),
  Pause: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M4.5 3.5h2.3v9H4.5zM9.2 3.5h2.3v9H9.2z" fill="currentColor" />
    </svg>
  ),
  SkipBack: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M4 3v10M12.5 3.5 5.5 8l7 4.5z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  SkipForward: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M12 3v10M3.5 3.5l7 4.5-7 4.5z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  Volume: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M2.5 6.2v3.6h2.4L8 12.2V3.8L4.9 6.2H2.5z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M10.2 5.4a3.2 3.2 0 0 1 0 5.2M12.1 3.7a5.6 5.6 0 0 1 0 8.6" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  ),
  ChevronUp: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="m4 10 4-4 4 4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
};

const MEDIA_PLAYER_THEMES = {
  SEQ: { label: "Sequence", accent: "#7c5cff", accent2: "#b6a5ff" },
  STRM: { label: "Stream", accent: "#3ec19a", accent2: "#84d8ba" },
  WAVE: { label: "Wave", accent: "#f0b132", accent2: "#f3c97a" },
  RAW: { label: "Raw wave", accent: "#ff8c42", accent2: "#ffb585" },
  BANK: { label: "Bank note", accent: "#5b8def", accent2: "#9bbcff" },
  DEFAULT: { label: "Audio", accent: "#5a3fa8", accent2: "#7558c9" },
};

function getMediaPlayerTheme(item) {
  if (!item) return MEDIA_PLAYER_THEMES.DEFAULT;
  if (item.kind === "wave") return MEDIA_PLAYER_THEMES.RAW;
  return MEDIA_PLAYER_THEMES[item.type] || MEDIA_PLAYER_THEMES.DEFAULT;
}

function MediaPlayerBar({ playingSound, playingId, isPlaying, playheadMs, durationMs, volume, strmPlayback, autoPlayEnabled, seqVariations, onPlay, onPause, onStop, onSeek, onNext, onVolume, onStrmLoopChange, onAutoPlayChange, onStrmTrackSelectionChange, onSeqVariationChange }) {
  const [volumeOpen, setVolumeOpen] = useStateT(false);
  const [scrubMs, setScrubMs] = useStateT(null);
  const [tracksOpen, setTracksOpen] = useStateT(false);
  const [variationsOpen, setVariationsOpen] = useStateT(false);
  const scrubActiveRef = React.useRef(false);
  const theme = getMediaPlayerTheme(playingSound);
  const playbackIcon = pysarIconForPlayback(playingSound);
  const isStrmTransport = playingSound?.type === "STRM";
  const isSeqTransport = playingSound?.type === "SEQ";
  const isSoundTransport = window.PysarIsSoundListTransport(playingSound);
  const strmTracks = Array.isArray(strmPlayback?.tracks) ? strmPlayback.tracks : [];
  const selectedTrackIndices = Array.isArray(strmPlayback?.selectedTrackIndices) ? strmPlayback.selectedTrackIndices : [];
  const variationOptionsReady = Array.isArray(seqVariations);
  const sequenceVariations = variationOptionsReady ? seqVariations : [];
  const selectedVariation = playingSound?.seqVariation || null;
  const shownPlayheadMs = scrubMs == null ? playheadMs : scrubMs;
  const progress = durationMs ? Math.max(0, Math.min(1, shownPlayheadMs / durationMs)) : 0;
  const typeLabel = playingSound
    ? (
      playingSound.kind === "wave"
        ? `${theme.label} · ${playingSound.type || "sample"}`
        : `${theme.label}${playingSound.seqVariation ? ` · ${playingSound.seqVariation.label}` : ""}`
    )
    : "select a sound or wave to preview";

  function seekMsFromEvent(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    return pct * durationMs;
  }

  function beginScrub(event) {
    if (!playingSound || !durationMs) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    scrubActiveRef.current = true;
    setScrubMs(seekMsFromEvent(event));
  }

  function moveScrub(event) {
    if (!scrubActiveRef.current || !durationMs) return;
    setScrubMs(seekMsFromEvent(event));
  }

  function endScrub(event) {
    if (!scrubActiveRef.current || !durationMs) return;
    const next = seekMsFromEvent(event);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    scrubActiveRef.current = false;
    setScrubMs(null);
    onSeek(next, { resume: isPlaying });
  }

  function cancelScrub(event) {
    if (!scrubActiveRef.current) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    scrubActiveRef.current = false;
    setScrubMs(null);
  }

  function toggleStrmTrack(index) {
    const selected = selectedTrackIndices.includes(index)
      ? selectedTrackIndices.filter((item) => item !== index)
      : [...selectedTrackIndices, index].sort((a, b) => a - b);
    if (selected.length === 0) return;
    onStrmTrackSelectionChange?.(selected);
  }

  const trackSummary = selectedTrackIndices.length === strmTracks.length
    ? "All tracks"
    : selectedTrackIndices.length === 1
      ? `Track ${selectedTrackIndices[0] + 1}`
      : `${selectedTrackIndices.length} tracks`;
  const variationSummary = selectedVariation?.label || (
    variationOptionsReady
      ? (sequenceVariations.length > 0 ? "Default variation" : "None")
      : "Loading variations…"
  );

  React.useEffect(() => {
    if (!isStrmTransport) setTracksOpen(false);
  }, [isStrmTransport, playingSound?.id]);

  React.useEffect(() => {
    if (!isSeqTransport) setVariationsOpen(false);
  }, [isSeqTransport, playingSound?.id]);

  return (
    <footer
      className={"transport" + (playingSound ? " has-track" : "")}
      style={{ "--tp-accent": theme.accent, "--tp-accent-2": theme.accent2 }}
    >
      <div className="transport-island">
        <div className="tp-now">
          <div className="tp-art"><PysarIcon name={playbackIcon} className="tp-art-icon" /></div>
          <div style={{ minWidth: 0 }}>
            <div className="tp-name">{playingSound ? playingSound.name : "-"}</div>
            <div className="tp-sub">
              {playingSound
                ? <>{typeLabel} · vol {Math.round(volume * 100)}</>
                : typeLabel}
            </div>
          </div>
          </div>
          <div className="tp-controls">
            <div className="tp-cluster">
              {isSoundTransport && (
                <div className="tp-strm-controls">
                  {isStrmTransport && (
                    <label className={"tp-loop" + (strmPlayback?.looped ? "" : " unavailable")} title={strmPlayback?.looped ? "Loop this BRSTM at its embedded loop point" : "This BRSTM has no embedded loop point"}>
                      <input
                        type="checkbox"
                        checked={!!strmPlayback?.loopEnabled}
                        disabled={!strmPlayback?.looped}
                        onChange={(event) => onStrmLoopChange?.(event.target.checked)}
                      />
                      <span>Loop</span>
                    </label>
                  )}
                  <label className="tp-loop" title="Play the next sound in the current list when this sound finishes">
                    <input
                      type="checkbox"
                      checked={!!autoPlayEnabled}
                      onChange={(event) => onAutoPlayChange?.(event.target.checked)}
                    />
                    <span>Autoplay</span>
                  </label>
                  {isStrmTransport && (
                    <div className="tp-track-wrap">
                      <button className="tp-track-trigger" onClick={() => setTracksOpen((open) => !open)} disabled={strmTracks.length === 0} aria-expanded={tracksOpen} title="Choose BRSTM tracks">
                        <span>{trackSummary}</span>
                        <TP.ChevronUp />
                      </button>
                      {tracksOpen && (
                        <div className="tp-track-pop">
                          <div className="tp-track-pop-title">BRSTM tracks</div>
                          {strmTracks.map((track) => {
                            const index = Number(track.index);
                            const channels = (track.channels || []).map((channel) => Number(channel) + 1).join(", ");
                            const checked = selectedTrackIndices.includes(index);
                            return (
                              <label key={index} className="tp-track-option">
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={() => toggleStrmTrack(index)}
                                />
                                <span>Track {index + 1}</span>
                                <small>Ch. {channels}</small>
                              </label>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
              {isSeqTransport && (
                <div className="tp-variation-wrap">
                  <button className="tp-variation-trigger" onClick={() => setVariationsOpen((open) => !open)} disabled={!variationOptionsReady || sequenceVariations.length === 0} aria-expanded={variationsOpen} title="Choose BRSEQ variation">
                    <span>{variationSummary}</span>
                    <TP.ChevronUp />
                  </button>
                  {variationsOpen && (
                    <div className="tp-variation-pop">
                      <div className="tp-track-pop-title">BRSEQ variation</div>
                      <label className="tp-variation-option" title="Default variation">
                        <input
                          type="radio"
                          name={`seq-variation-${playingSound.id}`}
                          checked={!selectedVariation}
                          onChange={() => { setVariationsOpen(false); onSeqVariationChange?.(null); }}
                        />
                        <span>Default variation</span>
                      </label>
                      {sequenceVariations.map((variation) => (
                        <label key={variation.id} className="tp-variation-option" title={variation.label}>
                          <input
                            type="radio"
                            name={`seq-variation-${playingSound.id}`}
                            checked={selectedVariation?.id === variation.id}
                            onChange={() => { setVariationsOpen(false); onSeqVariationChange?.(variation); }}
                          />
                          <span>{variation.label}</span>
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              )}
              <button className="tp-skip" onClick={() => onSeek(0, { resume: isPlaying })} disabled={!playingSound} title="Previous"><TP.SkipBack /></button>
            <button className="tp-play" onClick={() => isPlaying ? onPause() : onPlay()} disabled={!playingSound} title={isPlaying ? "Pause" : "Play"}>
              {isPlaying ? <TP.Pause /> : <TP.Play />}
            </button>
            <button className="tp-skip" onClick={() => onNext ? onNext() : onSeek(durationMs, { resume: isPlaying })} disabled={!playingSound} title="Next sound in list"><TP.SkipForward /></button>
          </div>
          <div className="tp-progress">
            <span>{formatMediaTime(shownPlayheadMs)}</span>
            <div
              className="bar"
              onPointerDown={beginScrub}
              onPointerMove={moveScrub}
              onPointerUp={endScrub}
              onPointerCancel={cancelScrub}
            >
              <div className="fill" style={{ width: (progress * 100) + "%" }}></div>
              <div className="knob" style={{ left: (progress * 100) + "%" }}></div>
            </div>
            <span>{playingSound ? formatMediaTime(durationMs) : "--:--.--"}</span>
          </div>
        </div>
        <div className="tp-right">
          <div className="volume-pop-wrap">
            <button className={"tb-btn ghost volume-toggle" + (volumeOpen ? " active" : "")} onClick={() => setVolumeOpen(!volumeOpen)} title="Volume">
              <TP.Volume />
            </button>
            {volumeOpen && (
              <div className="volume-pop">
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.01"
                  value={volume}
                  onChange={(e) => onVolume(parseFloat(e.target.value))}
                  orient="vertical"
                />
                <span>{Math.round(volume * 100)}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </footer>
  );
}

window.MediaPlayerBar = MediaPlayerBar;
