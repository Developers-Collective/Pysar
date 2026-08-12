const SEQ_TRACK_COLORS = ["#5b8def", "#7c5cff", "#3ec19a", "#f0b132", "#e85d4a", "#c47ad9", "#3aa4c4", "#e879a7"];
function trackColor(trackIndex) {
  return SEQ_TRACK_COLORS[Math.abs((trackIndex | 0)) % SEQ_TRACK_COLORS.length];
}

const SEQ_MML_FLOW_COMMANDS = new Set([
  "alloc_track", "open_track", "jump", "call", "ret", "fin",
]);
const SEQ_MML_TIME_COMMANDS = new Set([
  "wait", "tempo", "timebase", "loop_start", "loop_end",
]);

function sequenceMmlCommentIndex(line) {
  const positions = [line.indexOf(";"), line.indexOf("#")].filter((index) => index >= 0);
  return positions.length ? Math.min(...positions) : -1;
}

function sequenceMmlTargetRange(source, target, tracks = []) {
  if (!source || !target) return null;
  const targetOffset = target.startOffset == null ? NaN : Number(target.startOffset);
  const targetTrack = Number.isFinite(targetOffset)
    ? tracks.find((track) => targetOffset >= Number(track.startOffset) && targetOffset < Number(track.endOffset))
    : null;
  const candidates = [...new Set([
    target.name,
    target.startLabel,
    Number.isFinite(targetOffset) ? `_entry_${targetOffset.toString(16).toUpperCase().padStart(6, "0")}` : null,
    targetTrack?.name,
  ].filter(Boolean).map((name) => String(name).replace(/^::/, "")))];

  let cursor = 0;
  const lines = String(source).split("\n");
  const declarations = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const commentIndex = sequenceMmlCommentIndex(line);
    const code = line.slice(0, commentIndex < 0 ? line.length : commentIndex);
    const match = code.match(/^\s*((?:::)?[^\s:;]+)\s*:/);
    if (match) {
      declarations.push({
        name: match[1].replace(/^::/, ""),
        displayName: match[1],
        start: cursor + line.indexOf(match[1]),
        line: index + 1,
      });
    }
    cursor += line.length + 1;
  }
  for (const candidate of candidates) {
    const declaration = declarations.find((item) => item.name === candidate);
    if (declaration) {
      return {
        start: declaration.start,
        end: declaration.start + declaration.displayName.length,
        line: declaration.line,
      };
    }
  }
  return null;
}

function sequenceMmlTokenClass(token, isCommand) {
  if (/^(?:0x[\da-f]+|[-+]?\d+)$/i.test(token)) return "number";
  if (isCommand) {
    const base = token.toLowerCase().replace(/_(?:if|tr|tv|t|r|v)$/, "");
    if (/^[a-g](?:s|f|#|b)?-?\d+$/.test(base)) return "note";
    if (SEQ_MML_FLOW_COMMANDS.has(base)) return "flow";
    if (SEQ_MML_TIME_COMMANDS.has(base)) return "time";
    return "command";
  }
  if (/^[A-Z][A-Z\d_]*$/.test(token)) return "constant";
  if (/^(?:::)?[A-Za-z_][\w]*$/.test(token)) return "reference";
  return "";
}

function SequenceMmlHighlight({ source, focusedLine, errorLine }) {
  const sourceLines = String(source).split("\n");
  return sourceLines.map((line, lineIndex) => {
    const lineNumber = lineIndex + 1;
    const commentIndex = sequenceMmlCommentIndex(line);
    const code = line.slice(0, commentIndex < 0 ? line.length : commentIndex);
    const comment = commentIndex < 0 ? "" : line.slice(commentIndex);
    const labelMatch = code.match(/^(\s*)((?:::)?[^\s:;]+)(\s*:)(.*)$/);
    let renderedCode;
    if (labelMatch) {
      renderedCode = (
        <>
          {labelMatch[1]}<span className="mml-label">{labelMatch[2]}</span><span className="mml-punctuation">{labelMatch[3]}</span>{labelMatch[4]}
        </>
      );
    } else {
      let commandSeen = false;
      const tokens = code.match(/0x[\da-f]+|::[A-Za-z_][\w]*|[A-Za-z_][\w]*|[-+]?\d+|\s+|[,()+\-*/%&|^~<>]+|./gi) || [];
      renderedCode = tokens.map((token, tokenIndex) => {
        const word = /^(?:0x[\da-f]+|::[A-Za-z_][\w]*|[A-Za-z_][\w]*|[-+]?\d+)$/i.test(token);
        const isCommand = word && !commandSeen;
        if (isCommand) commandSeen = true;
        const tokenClass = sequenceMmlTokenClass(token, isCommand);
        return tokenClass
          ? <span key={tokenIndex} className={`mml-${tokenClass}`}>{token}</span>
          : <React.Fragment key={tokenIndex}>{token}</React.Fragment>;
      });
    }
    const classes = [
      "seq-mml-line",
      focusedLine === lineNumber ? "current" : "",
      errorLine === lineNumber ? "error" : "",
    ].filter(Boolean).join(" ");
    return (
      <span key={`${lineIndex}:${line}`} className={classes} data-line={lineNumber}>
        {code.length === 0 && !comment ? "\u200B" : renderedCode}
        {comment && <span className="mml-comment">{comment}</span>}
      </span>
    );
  });
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

function SequenceDetail({ sound, editorSourceText = null, onEditorSourceCommit, playheadMs = 0, durationMs = 0, isPlaying = false, playingSound = null, selectedVariation = null, onVariation, variations = [], onLoadVariations, onSoundChange, safeMode = true, onDirty, onDataRefresh, onPlaybackInvalidate, onError, onDelete }) {
  const [details, setDetails] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [operationError, setOperationError] = React.useState(null);
  const [operationBusy, setOperationBusy] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  const [sourceText, setSourceText] = React.useState("");
  const [refreshRevision, setRefreshRevision] = React.useState(0);
  const [follow, setFollow] = React.useState(true);
  const [exportMenuOpen, setExportMenuOpen] = React.useState(false);
  const [lintState, setLintState] = React.useState({ status: "idle", error: "", line: null });
  const codeRef = React.useRef(null);
  const activeLineRef = React.useRef(null);
  const exportMenuRef = React.useRef(null);
  const sourceEditorRef = React.useRef(null);
  const sourceHighlightRef = React.useRef(null);
  const preservedEditorSourceRef = React.useRef(null);
  const lintRequestRef = React.useRef(0);
  const playbackWasRunningRef = React.useRef(false);
  const safeModeBlocksEditing = safeMode && sound.isNew !== true;

  React.useEffect(() => {
    if (!exportMenuOpen) return undefined;
    function closeExportMenu(event) {
      if (event.type === "keydown" && event.key !== "Escape") return;
      if (event.type !== "keydown" && exportMenuRef.current?.contains(event.target)) return;
      setExportMenuOpen(false);
    }
    document.addEventListener("pointerdown", closeExportMenu);
    document.addEventListener("keydown", closeExportMenu);
    return () => {
      document.removeEventListener("pointerdown", closeExportMenu);
      document.removeEventListener("keydown", closeExportMenu);
    };
  }, [exportMenuOpen]);

  React.useEffect(() => setExportMenuOpen(false), [sound.id, editing]);

  React.useEffect(() => {
    if (!safeModeBlocksEditing || !editing) return;
    setEditing(false);
    setSourceText(editorSourceText ?? details?.sourceText ?? "");
    setOperationError(null);
    setRefreshRevision((value) => value + 1);
  }, [safeModeBlocksEditing, editing, details?.sourceText]);

  React.useEffect(() => {
    const preservedSource = (
      Number(preservedEditorSourceRef.current?.soundId) === Number(sound.id)
        ? preservedEditorSourceRef.current.sourceText
        : null
    );
    let cancelled = false;
    setError(null);
    if (preservedSource == null) {
      setDetails(null);
      setLoading(true);
    }
    if (!window.pysar) {
      setError("Desktop API is not available");
      setLoading(false);
      return;
    }
    window.pysar.call("get_sequence_details", sound.id).then((result) => {
      if (cancelled) return;
      if (result?.ok) {
        setDetails(result.data);
        setSourceText(
          preservedSource
          ?? editorSourceText
          ?? result.data.sourceText
          ?? ""
        );
        if (Number(preservedEditorSourceRef.current?.soundId) === Number(sound.id)) {
          preservedEditorSourceRef.current = null;
        }
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

  async function replaceSequence() {
    const result = await runMutation("replace_sequence_dialog", sound.id);
    if (result) {
      onEditorSourceCommit?.(sound.id, null);
      setEditing(false);
      setRefreshRevision((value) => value + 1);
    }
  }

  async function compileAndApply() {
    const submittedSource = sourceText;
    const result = await runMutation(
      "compile_sequence_text",
      sound.id,
      submittedSource,
      // Let the backend preserve the old label when it still exists, but
      // fall back to the compiled sequence's default entry when the editor
      // intentionally renamed or removed that label.
      null,
    );
    if (result) {
      preservedEditorSourceRef.current = {
        soundId: Number(sound.id),
        sourceText: submittedSource,
      };
      onEditorSourceCommit?.(sound.id, submittedSource);
      setSourceText(submittedSource);
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
  const displayTracks = details?.relatedTracks?.length
    ? details.relatedTracks
    : tracks.slice(details?.startTrackIndex || 0, (details?.startTrackIndex || 0) + 1);
  const trace = details?.trace || [];
  const currentEditorTarget = React.useMemo(() => (
    (details?.sharedSounds || []).find((candidate) => Number(candidate.id) === Number(sound.id)) || {
      id: sound.id,
      name: sound.name,
      startLabel: details?.startLabel,
      startOffset: details?.startOffset,
      seqLabelOffset: details?.seqLabelOffset,
    }
  ), [details, sound.id, sound.name]);
  const currentEditorRange = React.useMemo(
    () => sequenceMmlTargetRange(sourceText, currentEditorTarget, tracks),
    [sourceText, currentEditorTarget, tracks],
  );

  function syncSourceEditorScroll() {
    if (!sourceEditorRef.current || !sourceHighlightRef.current) return;
    sourceHighlightRef.current.scrollTop = sourceEditorRef.current.scrollTop;
    sourceHighlightRef.current.scrollLeft = sourceEditorRef.current.scrollLeft;
  }

  function cancelEditing() {
    setEditing(false);
    setSourceText(editorSourceText ?? details?.sourceText ?? "");
    setRefreshRevision((value) => value + 1);
  }

  function jumpToMmlTarget(target, { selectLabel = true } = {}) {
    const editor = sourceEditorRef.current;
    if (!editor) return;
    const range = sequenceMmlTargetRange(sourceText, target, tracks);
    if (!range) {
      editor.focus();
      return;
    }
    const lineHeight = parseFloat(window.getComputedStyle(editor).lineHeight) || 19;
    editor.focus();
    editor.setSelectionRange(selectLabel ? range.start : range.end, range.end);
    editor.scrollTop = Math.max(0, (range.line - 4) * lineHeight);
    syncSourceEditorScroll();
  }

  function jumpToMmlLine(lineNumber) {
    const editor = sourceEditorRef.current;
    const line = Math.max(1, Number(lineNumber) || 1);
    if (!editor) return;
    const sourceLines = sourceText.split("\n");
    const start = sourceLines.slice(0, line - 1).reduce((length, value) => length + value.length + 1, 0);
    const end = start + (sourceLines[line - 1]?.length || 0);
    const lineHeight = parseFloat(window.getComputedStyle(editor).lineHeight) || 19;
    editor.focus();
    editor.setSelectionRange(start, end);
    editor.scrollTop = Math.max(0, (line - 4) * lineHeight);
    syncSourceEditorScroll();
  }

  function selectSharedSound(nextSoundId) {
    const nextId = Number(nextSoundId);
    const target = (details?.sharedSounds || []).find((candidate) => Number(candidate.id) === nextId);
    if (!target || nextId === Number(sound.id)) return;
    preservedEditorSourceRef.current = editing
      ? { soundId: nextId, sourceText }
      : null;
    onSoundChange?.(nextId, { reuseActiveTab: true });
  }

  React.useEffect(() => {
    if (!editing || !currentEditorTarget) return undefined;
    if (Number(details?.soundId) !== Number(sound.id)) return undefined;
    const frame = window.requestAnimationFrame(() => jumpToMmlTarget(currentEditorTarget));
    return () => window.cancelAnimationFrame(frame);
  }, [
    editing,
    sound.id,
    details?.soundId,
    currentEditorTarget?.id,
    currentEditorTarget?.startOffset,
    tracks,
  ]);

  React.useEffect(() => {
    if (!editing) {
      setLintState({ status: "idle", error: "", line: null });
      return undefined;
    }
    const requestId = ++lintRequestRef.current;
    setLintState((current) => ({ ...current, status: "checking" }));
    const timeout = window.setTimeout(() => {
      if (!window.pysar) {
        setLintState({ status: "error", error: "Desktop validation API is unavailable", line: null });
        return;
      }
      window.pysar.call("lint_sequence_text", sourceText).then((result) => {
        if (requestId !== lintRequestRef.current) return;
        if (!result?.ok || !result.valid) {
          setLintState({
            status: "error",
            error: result?.error || "MML validation failed",
            line: result?.line == null ? null : Number(result.line),
          });
          return;
        }
        setLintState({ status: "valid", error: "", line: null });
      }).catch((error) => {
        if (requestId !== lintRequestRef.current) return;
        setLintState({ status: "error", error: String(error), line: null });
      });
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [editing, sourceText]);

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
    if (!isThisPlaying || !displayTracks.length) return null;
    if (!currentTrace) return null;
    const currentTrack = displayTracks.find(
      (track) => Number(track.index) === Number(currentTrace.trackIndex),
    );
    return currentTrack?.lines.find(
      (line) => line.op && Number(line.offset) === Number(currentTrace.offset),
    ) || null;
  }, [isThisPlaying, displayTracks, currentTrace]);

  React.useEffect(() => {
    if (!follow || !activeLineRef.current || !codeRef.current) return;
    const container = codeRef.current;
    const lineRect = activeLineRef.current.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    const nextTop = (
      container.scrollTop
      + lineRect.top - containerRect.top
      - container.clientHeight / 2
      + lineRect.height / 2
    );
    container.scrollTo({ top: Math.max(0, nextTop), behavior: "smooth" });
  }, [follow, activeLine?.line]);

  React.useEffect(() => {
    if (isThisPlaying) {
      playbackWasRunningRef.current = true;
      return undefined;
    }
    if (!playbackWasRunningRef.current) return undefined;
    playbackWasRunningRef.current = false;
    const reachedEnd = durationMs > 0 && playheadMs >= Math.max(0, durationMs - 50);
    const returnedToStart = playheadMs <= 1;
    if (!follow || (!reachedEnd && !returnedToStart)) return undefined;
    const frame = window.requestAnimationFrame(() => {
      codeRef.current?.scrollTo({ top: 0, left: 0, behavior: "smooth" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [isThisPlaying, follow, playheadMs, durationMs, sound.id]);

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
      <div className="toolbar seq-toolbar">
        <div className="seq-toolbar-row seq-toolbar-context">
          {(details?.sharedSounds || []).length > 1 ? (
            <select
              className="seq-track-select seq-sound-picker"
              value={String(sound.id)}
              onChange={(event) => selectSharedSound(parseInt(event.target.value, 10))}
              disabled={operationBusy}
              title={editing ? "Select a sound and jump to its MML entry" : "Sound using this BRSEQ"}
            >
              {details.sharedSounds.map((sharedSound) => (
                <option key={sharedSound.id} value={String(sharedSound.id)}>{sharedSound.name}</option>
              ))}
            </select>
          ) : (details?.labels || []).length > 0 && (
            <select
              className="seq-track-select seq-sound-picker"
              value={details.startLabel || (details.labels.find((label) => label.startOffset === details.seqLabelOffset)?.name || "")}
              onChange={(event) => updateStartLabel(event.target.value)}
              disabled={operationBusy || editing || safeModeBlocksEditing}
              title={safeModeBlocksEditing ? "Safe Mode protects the original sequence entry point" : "Sound start label"}
            >
              {details.labels.map((label, index) => (
                <option key={`${label.name}-${index}`} value={label.name}>{label.name}</option>
              ))}
            </select>
          )}
          {variations.length > 0 && (
            <select
              className="seq-track-select seq-variation-picker"
              value={selectedVariation?.id || ""}
              title="Sample variation"
              onChange={(event) => {
                const next = variations.find((variation) => variation.id === event.target.value) || null;
                if (onVariation) onVariation(sound, next);
              }}
            >
              <option value="">Default variation</option>
              {variations.map((variation) => (
                <option key={variation.id} value={variation.id}>{variation.label}</option>
              ))}
            </select>
          )}
          {details?.sharedReferenceCount > 1 && (
            <span className="seq-shared-badge">{details.sharedReferenceCount} shared sounds</span>
          )}
        </div>

        <div className="seq-toolbar-row seq-toolbar-actions">
          <div className="seq-edit-actions">
            {editing ? (
              <>
                <Button onClick={cancelEditing} disabled={operationBusy}>Cancel</Button>
                <Button
                  primary
                  onClick={compileAndApply}
                  disabled={operationBusy || lintState.status !== "valid"}
                  title={lintState.status === "error" ? lintState.error : "Compile and replace this sequence"}
                >{operationBusy ? "Compiling…" : "Compile & Apply"}</Button>
              </>
            ) : (
              <Button
                primary
                onClick={() => { setOperationError(null); setEditing(true); }}
                disabled={operationBusy || safeModeBlocksEditing}
                title={safeModeBlocksEditing ? "Disable Safe Mode to edit this original sequence" : "Edit the BRSEQ MML source"}
              >Edit MML</Button>
            )}
            {!editing && <Toggle on={follow} onChange={setFollow} label="Follow playhead" />}
          </div>

          <div className="seq-file-actions">
            <Button
              onClick={replaceSequence}
              disabled={operationBusy || editing || safeModeBlocksEditing}
              title={safeModeBlocksEditing ? "Disable Safe Mode to replace this original sequence" : "Replace this sound from a BRSEQ or MIDI file"}
            >Replace sequence…</Button>
            <div className="seq-export-menu" ref={exportMenuRef}>
              <Button
                onClick={() => setExportMenuOpen((open) => !open)}
                disabled={operationBusy || editing}
                aria-haspopup="menu"
                aria-expanded={exportMenuOpen}
              >Export <span className="seq-menu-caret" aria-hidden="true">⌄</span></Button>
              {exportMenuOpen && (
                <div className="menu-dropdown seq-export-dropdown" role="menu">
                  <button className="menu-entry" role="menuitem" onClick={() => { setExportMenuOpen(false); exportSequence("brseq"); }}>
                    <span>Export BRSEQ…</span>
                    <span className="seq-export-hint">Nintendo</span>
                  </button>
                  <button className="menu-entry" role="menuitem" onClick={() => { setExportMenuOpen(false); exportSequence("midi"); }}>
                    <span>Export MIDI…</span>
                    <span className="seq-export-hint">Standard MIDI</span>
                  </button>
                </div>
              )}
            </div>
            {onDelete && (
              <Button
                className="seq-delete-action"
                onClick={() => onDelete(sound)}
                disabled={operationBusy || editing || !!sound.protected}
                title={sound.protected ? "Safe Mode protects original sounds from deletion" : "Delete this new sequence sound"}
              >Delete</Button>
            )}
          </div>
        </div>
      </div>
      {operationError && <div className="seq-operation-error">{operationError}</div>}
      {editing ? (
        <div className="seq-source-editor-shell">
          <div className="seq-source-editor-layer">
            <pre className="seq-source-highlight" ref={sourceHighlightRef} aria-hidden="true">
              <code><SequenceMmlHighlight
                  source={sourceText}
                  focusedLine={currentEditorRange?.line || null}
                  errorLine={lintState.status === "error" ? lintState.line : null}
                /></code>
            </pre>
            <textarea
              ref={sourceEditorRef}
              className="seq-source-editor"
              value={sourceText}
              onChange={(event) => setSourceText(event.target.value)}
              onScroll={syncSourceEditorScroll}
              onKeyDown={(event) => {
                if (event.key !== "Tab") return;
                event.preventDefault();
                const editor = event.currentTarget;
                const start = editor.selectionStart;
                const end = editor.selectionEnd;
                setSourceText(`${sourceText.slice(0, start)}    ${sourceText.slice(end)}`);
                window.requestAnimationFrame(() => editor.setSelectionRange(start + 4, start + 4));
              }}
              wrap="off"
              spellCheck={false}
              disabled={operationBusy}
              aria-label="BRSEQ MML source"
            />
          </div>
          <button
            type="button"
            className={`seq-lint-status ${lintState.status}`}
            disabled={lintState.status !== "error" || lintState.line == null}
            onClick={() => jumpToMmlLine(lintState.line)}
            title={lintState.error || undefined}
            aria-live="polite"
          >
            <span className="seq-lint-indicator" aria-hidden="true"></span>
            {lintState.status === "checking" && "Checking MML…"}
            {lintState.status === "valid" && "No syntax errors"}
            {lintState.status === "error" && (lintState.error || "MML validation failed")}
            {lintState.status === "idle" && "MML validation ready"}
          </button>
        </div>
      ) : (
        <>
          <div className="seq-code" ref={codeRef}>
            {displayTracks.map((track, sectionIndex) => {
              const relation = track.relation || { kind: sectionIndex === 0 ? "root" : "related" };
              const relationText = relation.kind === "root"
                ? "Selected sequence"
                : relation.kind === "open"
                  ? `Opened track ${relation.trackNo}`
                  : relation.kind === "call"
                    ? "Called sequence"
                    : relation.kind === "jump"
                      ? "Jump target"
                      : relation.kind === "fallthrough"
                        ? "Continued sequence"
                      : "Related sequence";
              return (
                <section
                  key={track.index}
                  className={`seq-track-section ${relation.kind === "root" ? "root" : "related"}`}
                >
                  <div className="seq-track-section-header">
                    <strong>{relation.kind === "root" ? sound.name : (track.displayName || track.name)}</strong>
                    <span>{relationText}</span>
                  </div>
                  {track.lines.map((l) => {
                    const active = (
                      activeLine?.line === l.line
                      && Number(currentTrace?.trackIndex) === Number(track.index)
                    );
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
                    );
                  })}
                </section>
              );
            })}
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
