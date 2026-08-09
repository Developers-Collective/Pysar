const SEQ_TRACK_COLORS = ["#5b8def", "#7c5cff", "#3ec19a", "#f0b132", "#e85d4a", "#c47ad9", "#3aa4c4", "#e879a7"];
function trackColor(trackIndex) {
  return SEQ_TRACK_COLORS[Math.abs((trackIndex | 0)) % SEQ_TRACK_COLORS.length];
}

function SeqKeyboard({ activeNotes }) {
  const KEYS = 128;
  const KEY_W = 6;
  const HEIGHT = 84;
  const WHITE_H = 66;
  const isBlack = (n) => [1, 3, 6, 8, 10].includes(n % 12);

  const litWhite = new Map();
  const litBlack = new Map();
  for (const a of activeNotes) {
    if (a.note < 0 || a.note > 127) continue;
    const map = isBlack(a.note) ? litBlack : litWhite;
    map.set(a.note, a.color);
  }

  const whiteKeys = [];
  const blackKeys = [];
  const whitePositions = new Map();
  for (let i = 0; i < KEYS; i++) {
    if (isBlack(i)) {
      const previousWhiteX = whitePositions.get(i - 1);
      blackKeys.push(
        <rect
          key={"b" + i}
          x={previousWhiteX + KEY_W - KEY_W * 0.32}
          y={0}
          width={KEY_W * 0.65}
          height={WHITE_H * 0.62}
          fill={litBlack.get(i) || "var(--key-black)"}
          stroke="none"
        />
      );
    } else {
      const x = whiteKeys.length * KEY_W;
      whitePositions.set(i, x);
      whiteKeys.push(
        <rect
          key={"w" + i}
          x={x}
          y={0}
          width={KEY_W}
          height={WHITE_H}
          fill={litWhite.get(i) || "var(--key-white)"}
          stroke="var(--key-white-edge)"
          strokeWidth="0.4"
        />
      );
    }
  }
  const totalWidth = whiteKeys.length * KEY_W;
  const activeKeyGlow = [];
  for (const [note, color] of litWhite) {
    const x = whitePositions.get(note);
    if (x != null) activeKeyGlow.push(
      <rect key={`gw${note}`} x={x} y={0} width={KEY_W} height={WHITE_H} fill={color} />,
    );
  }
  for (const [note, color] of litBlack) {
    const previousWhiteX = whitePositions.get(note - 1);
    if (previousWhiteX != null) activeKeyGlow.push(
      <rect key={`gb${note}`} x={previousWhiteX + KEY_W - KEY_W * 0.32} y={0} width={KEY_W * 0.65} height={WHITE_H * 0.62} fill={color} />,
    );
  }

  return (
    <div className="seq-keyboard">
      <svg
        viewBox={`0 0 ${totalWidth} ${HEIGHT}`}
        style={{ width: "100%", height: "100%", display: "block" }}
        preserveAspectRatio="none"
      >
        <defs>
          <filter id="seq-keyboard-color-glow" x="-8%" y="-18%" width="116%" height="150%">
            <feGaussianBlur stdDeviation="3" />
          </filter>
        </defs>
        <g filter="url(#seq-keyboard-color-glow)" opacity="0.72" pointerEvents="none">
          {activeKeyGlow}
        </g>
        {whiteKeys}
        {blackKeys}
      </svg>
    </div>
  );
}

function SequenceDetail({ sound, playheadMs = 0, isPlaying = false, playingSound = null, selectedVariation = null, onVariation, variations = [], onLoadVariations, onDirty, onDataRefresh, onPlaybackInvalidate, onError, onDelete }) {
  const [details, setDetails] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [operationError, setOperationError] = React.useState(null);
  const [operationBusy, setOperationBusy] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  const [sourceText, setSourceText] = React.useState("");
  const [refreshRevision, setRefreshRevision] = React.useState(0);
  const [trackIndex, setTrackIndex] = React.useState(0);
  const [follow, setFollow] = React.useState(true);
  const codeRef = React.useRef(null);
  const activeLineRef = React.useRef(null);

  React.useEffect(() => {
    let cancelled = false;
    setDetails(null);
    setError(null);
    setLoading(true);
    setTrackIndex(0);
    if (!window.pysar) {
      setError("Desktop API is not available");
      setLoading(false);
      return;
    }
    window.pysar.call("get_sequence_details", sound.id).then((result) => {
      if (cancelled) return;
      if (result?.ok) {
        setDetails(result.data);
        setSourceText(result.data.sourceText || "");
        setTrackIndex(result.data.startTrackIndex || 0);
      } else {
        setError(result?.error || "Could not load sequence");
      }
      setLoading(false);
    }).catch((e) => {
      if (cancelled) return;
      setError(String(e));
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [sound.id, refreshRevision]);

  async function runMutation(callName, ...args) {
    if (!window.pysar || operationBusy) return null;
    setOperationBusy(true);
    setOperationError(null);
    try {
      const result = await window.pysar.call(callName, ...args);
      if (!result?.ok) {
        if (result?.error !== "Cancelled") setOperationError(result?.error || "Operation failed");
        return null;
      }
      if (result.dirty) {
        onPlaybackInvalidate?.(sound.id);
        onDirty?.(true);
      }
      if (result.data) onDataRefresh?.(result.data);
      return result;
    } catch (ex) {
      setOperationError(String(ex));
      return null;
    } finally {
      setOperationBusy(false);
    }
  }

  async function importSequence() {
    const result = await runMutation("replace_sequence_dialog", sound.id);
    if (result) {
      setEditing(false);
      setRefreshRevision((value) => value + 1);
    }
  }

  async function compileAndApply() {
    const result = await runMutation(
      "compile_sequence_text",
      sound.id,
      sourceText,
      // Let the backend preserve the old label when it still exists, but
      // fall back to the compiled sequence's default entry when the editor
      // intentionally renamed or removed that label.
      null,
    );
    if (result) {
      if (result.sourceText != null) setSourceText(result.sourceText);
      setEditing(false);
      setRefreshRevision((value) => value + 1);
    }
  }

  async function updateStartLabel(label) {
    const result = await runMutation("update_sequence_start_label", sound.id, label);
    if (result) setRefreshRevision((value) => value + 1);
  }

  async function exportSequence(format) {
    const result = await runMutation("export_sequence_dialog", sound.id, format);
    if (!result && operationError) onError?.(operationError);
  }

  React.useEffect(() => {
    onLoadVariations?.(sound.id);
  }, [sound.id, onLoadVariations]);

  const tracks = details?.tracks || [];
  const activeTrack = tracks[Math.min(trackIndex, Math.max(0, tracks.length - 1))] || null;
  const lines = activeTrack?.lines || [];
  const trace = details?.trace || [];
  const isThisPlaying = !!playingSound && playingSound.id === sound.id && isPlaying;
  const currentTrace = React.useMemo(() => {
    if (!isThisPlaying || !trace.length) return null;
    let current = null;
    for (const event of trace) {
      if ((event.ms || 0) <= playheadMs) current = event;
      else break;
    }
    return current;
  }, [isThisPlaying, trace, playheadMs]);
  const activeLine = React.useMemo(() => {
    if (!isThisPlaying || !lines.length) return null;
    if (!currentTrace) return null;
    if (currentTrace.trackIndex !== activeTrack?.index) return null;
    return lines.find((line) => line.line === currentTrace.line) || null;
  }, [isThisPlaying, lines, currentTrace, activeTrack?.index]);

  React.useEffect(() => {
    if (!follow || !currentTrace || currentTrace.trackIndex == null) return;
    if (currentTrace.trackIndex !== trackIndex) setTrackIndex(currentTrace.trackIndex);
  }, [follow, currentTrace?.trackIndex, trackIndex]);

  React.useEffect(() => {
    if (!follow || !activeLineRef.current || !codeRef.current) return;
    activeLineRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [follow, activeLine?.line]);

  const activeNotes = React.useMemo(() => {
    if (!isThisPlaying || !trace.length) return [];
    const out = [];
    for (const ev of trace) {
      if (ev.ms > playheadMs) break;
      if (ev.note == null) continue;
      const end = ev.ms + (ev.lengthMs || 0);
      if (end > playheadMs) {
        out.push({ note: ev.note, color: trackColor(ev.trackIndex ?? 0) });
      }
    }
    return out;
  }, [isThisPlaying, trace, playheadMs]);

  if (loading) {
    return (
      <div className="empty-state" data-pysar-reference={pysarReferenceKey({ kind: "sound", id: sound.id })} tabIndex={-1}>
        <div className="empty-card" style={{ borderStyle: "solid" }}>
          <p>Loading sequence opcodes…</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="empty-state" data-pysar-reference={pysarReferenceKey({ kind: "sound", id: sound.id })} tabIndex={-1}>
        <div className="empty-card" style={{ borderStyle: "solid" }}>
          <p>{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div
      style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}
      data-pysar-reference={pysarReferenceKey({ kind: "sound", id: sound.id })}
      tabIndex={-1}
    >
      <div className="toolbar">
        {!editing && (
          <select
            className="seq-track-select"
            value={String(trackIndex)}
            onChange={(event) => setTrackIndex(parseInt(event.target.value, 10))}
          >
            {tracks.map((track) => (
              <option key={track.index} value={String(track.index)}>
                {track.name} · {track.lineCount} lines
              </option>
            ))}
          </select>
        )}
        {(details?.labels || []).length > 0 && (
          <select
            className="seq-track-select"
            value={details.startLabel || (details.labels.find((label) => label.startOffset === details.seqLabelOffset)?.name || "")}
            onChange={(event) => updateStartLabel(event.target.value)}
            disabled={operationBusy || editing}
            title="Sound start label"
          >
            {details.labels.map((label, index) => (
              <option key={`${label.name}-${index}`} value={label.name}>{label.name}</option>
            ))}
          </select>
        )}
        {variations.length > 0 && (
          <>
            <span className="sep"></span>
            <select
              className="seq-track-select"
              value={selectedVariation?.id || ""}
              title="Sample variation"
              onChange={(event) => {
                const next = variations.find((variation) => variation.id === event.target.value) || null;
                if (onVariation) onVariation(sound, next);
              }}
            >
              <option value="">Default variation</option>
              {variations.map((variation) => (
                <option key={variation.id} value={variation.id}>
                  {variation.label}
                </option>
              ))}
            </select>
          </>
        )}
        <span className="grow"></span>
        {details?.sharedReferenceCount > 1 && <span className="dialog-hint">Shared by {details.sharedReferenceCount} sounds</span>}
        <Button onClick={() => exportSequence("brseq")} disabled={operationBusy}>Export BRSEQ</Button>
        <Button onClick={() => exportSequence("midi")} disabled={operationBusy}>Export MIDI</Button>
        <Button onClick={importSequence} disabled={operationBusy}>Import/Replace…</Button>
        {editing ? (
          <>
            <Button onClick={() => { setEditing(false); setSourceText(details?.sourceText || ""); }} disabled={operationBusy}>Cancel</Button>
            <Button primary onClick={compileAndApply} disabled={operationBusy}>{operationBusy ? "Compiling…" : "Compile & Apply"}</Button>
          </>
        ) : (
          <Button primary onClick={() => { setOperationError(null); setEditing(true); }} disabled={operationBusy}>Edit MML</Button>
        )}
        {!editing && <Toggle on={follow} onChange={setFollow} label="Follow playhead" />}
        {onDelete && (
          <Button
            onClick={() => onDelete(sound)}
            disabled={operationBusy || !!sound.protected}
            title={sound.protected ? "Safe Mode protects original sounds from deletion" : "Delete this new sequence sound"}
          >Delete</Button>
        )}
      </div>
      {operationError && <div className="seq-operation-error">{operationError}</div>}
      {editing ? (
        <textarea
          className="seq-source-editor"
          value={sourceText}
          onChange={(event) => setSourceText(event.target.value)}
          spellCheck={false}
          disabled={operationBusy}
          aria-label="BRSEQ MML source"
        />
      ) : (
        <>
          <div className="seq-code" ref={codeRef}>
            {lines.map((l) => {
              const active = activeLine?.line === l.line;
              return (
              <div
                key={l.line}
                ref={active ? activeLineRef : null}
                className={"seq-line kind-" + l.kind + (active ? " active" : "")}
              >
                <span className="lno">{l.line}</span>
                <span className="off">{l.offsetHex}</span>
                <span className="lbl">{l.label}</span>
                <span className="src">
                  <span className="op">{l.op}</span>{l.op && l.arg && " "}<span className="arg">{l.arg}</span>
                </span>
              </div>
            );})}
          </div>
          <SeqKeyboard activeNotes={activeNotes} />
        </>
      )}
    </div>
  );
}

function Diagnostics() {
  const D = window.PYSAR_DATA;
  const issues = [
    { sev: "warn", title: "BANK_BGM_BOSS waves not packed contiguously", detail: "May cause stalls during streaming reads." },
    { sev: "warn", title: "PLAYER_SE_OBJECT playable sound limit exceeded 4 times in last preview", detail: "Some requested sounds could not be started." },
    { sev: "info", title: "Group GROUP_STAGE_FIRE has 8 unreferenced sounds", detail: "Consider removing or referencing them." },
    { sev: "error", title: "Wave file f_3422.brwav missing from archive", detail: "Referenced by SE_OBJ_DOOR_HARD." },
  ];
  return (
    <div className="detail">
      <div className="detail-h">
        <div>
          <div className="crumbs">Tools / Diagnostics</div>
          <h1 style={{ fontFamily: "var(--font-ui)", fontWeight: 600 }}>Archive health</h1>
        </div>
        <Button>Re-scan</Button>
      </div>
      <div className="cards">
        <div className="card full">
          <div className="card-h"><h3>Issues · {issues.length}</h3></div>
          <div className="card-body">
            {issues.map((it, i) => (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "70px 1fr", gap: 12, padding: "10px 0", borderBottom: i < issues.length - 1 ? "1px solid var(--border-subtle)" : "0" }}>
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 600, letterSpacing: 0.4, padding: "2px 6px", borderRadius: 3, height: 18,
                  background: it.sev === "error" ? "#e74c3c33" : it.sev === "warn" ? "#f0b13233" : "#5b8def33",
                  color: it.sev === "error" ? "#ff8c80" : it.sev === "warn" ? "#f3c97a" : "#9ab9f5",
                  textTransform: "uppercase", display: "inline-block", width: 50, textAlign: "center" }}>
                  {it.sev}
                </span>
                <div>
                  <div style={{ fontWeight: 500 }}>{it.title}</div>
                  <div style={{ color: "var(--text-tertiary)", fontSize: 11.5, marginTop: 2 }}>{it.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-h"><h3>Player capacity</h3></div>
          <div className="card-body">
            {D.players.slice(0, 6).map((p) => (
              <div key={p.id} style={{ display: "grid", gridTemplateColumns: "150px 1fr 60px", alignItems: "center", gap: 10, padding: "4px 0", fontSize: 12 }}>
                <span className="mono" style={{ color: "var(--text-secondary)" }}>{p.name}</span>
                <div style={{ height: 5, background: "var(--border-subtle)", borderRadius: 3, overflow: "hidden" }}>
                  <div style={{ height: "100%", width: Math.min(100, ((p.playableSounds || 0) / 64) * 100) + "%", background: "var(--accent)" }}></div>
                </div>
                <span className="mono" style={{ fontSize: 11, color: "var(--text-tertiary)", textAlign: "right" }}>{p.playableSounds || 0}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-h"><h3>Reference integrity</h3></div>
          <div className="card-body">
            <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "6px 14px", fontSize: 12 }}>
              <span style={{ color: "var(--text-tertiary)" }}>Sounds</span><span className="mono">{D.sounds.length}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Total references</span><span className="mono">{D.sounds.length * 3}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Broken</span><span className="mono" style={{ color: "#ff8c80" }}>1</span>
              <span style={{ color: "var(--text-tertiary)" }}>Orphans</span><span className="mono" style={{ color: "#f3c97a" }}>3</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

window.SequenceDetail = SequenceDetail;
window.Diagnostics = Diagnostics;
