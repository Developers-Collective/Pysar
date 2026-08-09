const { useState: useStateD, useEffect: useEffectD, useCallback: useCallbackD, useRef: useRefD } = React;

const pysarDialogController = (() => {
  let enqueue = null;
  let nextId = 1;
  const waiting = [];

  function request(kind, message, options = {}) {
    return new Promise((resolve) => {
      const item = {
        id: nextId++,
        kind,
        message: String(message ?? ""),
        options,
        resolve,
      };
      if (enqueue) enqueue(item);
      else waiting.push(item);
    });
  }

  return {
    request,
    attach(handler) {
      enqueue = handler;
      waiting.splice(0).forEach(handler);
      return () => {
        if (enqueue === handler) enqueue = null;
      };
    },
  };
})();

function pysarConfirm(message, options = {}) {
  return pysarDialogController.request("confirm", message, options);
}

function pysarPrompt(title, initialValue = "", options = {}) {
  return pysarDialogController.request("prompt", options.message || "", {
    ...options,
    title,
    initialValue: String(initialValue ?? ""),
  });
}

function pysarAlert(message, options = {}) {
  return pysarDialogController.request("alert", message, options);
}

function ModalOverlay({ children, onClose, title, width = 520 }) {
  const titleId = React.useId();
  return (
    <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-dialog" style={{ width }} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="modal-header">
          <span className="modal-title" id={titleId}>{title}</span>
          <button className="modal-close" onClick={onClose} aria-label="Close dialog">✕</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

function PysarDialogHost() {
  const [queue, setQueue] = useStateD([]);
  const [promptValue, setPromptValue] = useStateD("");
  const active = queue[0] || null;

  useEffectD(() => pysarDialogController.attach((item) => {
    setQueue((current) => [...current, item]);
  }), []);

  useEffectD(() => {
    if (active?.kind === "prompt") setPromptValue(active.options.initialValue || "");
  }, [active?.id]);

  useEffectD(() => {
    if (!active) return undefined;
    const onKeyDown = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      finish(active.kind === "confirm" ? false : (active.kind === "prompt" ? null : undefined));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [active?.id]);

  function finish(value) {
    if (!active) return;
    active.resolve(value);
    setQueue((current) => current[0]?.id === active.id ? current.slice(1) : current);
  }

  if (!active) return null;
  const options = active.options || {};
  const cancelValue = active.kind === "confirm" ? false : (active.kind === "prompt" ? null : undefined);
  const title = options.title || (active.kind === "alert" ? "Notice" : "Confirm action");

  return (
    <ModalOverlay title={title} width={options.width || 440} onClose={() => finish(cancelValue)}>
      <form className="dialog-form system-dialog" onSubmit={(event) => {
        event.preventDefault();
        finish(active.kind === "prompt" ? promptValue : true);
      }}>
        {active.message && <div className="system-dialog-message">{active.message}</div>}
        {active.kind === "prompt" && (
          <div className="dialog-field">
            <label>{options.label || "Name"}</label>
            <input
              autoFocus
              value={promptValue}
              maxLength={options.maxLength}
              onChange={(event) => setPromptValue(event.target.value)}
            />
          </div>
        )}
        <div className="dialog-actions">
          {active.kind !== "alert" && (
            <button type="button" className="tb-btn" onClick={() => finish(cancelValue)}>
              {options.cancelLabel || "Cancel"}
            </button>
          )}
          <button
            type="submit"
            autoFocus={active.kind !== "prompt"}
            className={`tb-btn ${options.danger ? "danger" : "primary"}`}
          >
            {options.confirmLabel || (active.kind === "alert" ? "OK" : "Continue")}
          </button>
        </div>
      </form>
    </ModalOverlay>
  );
}


function BrstmCreationFields({
  wavInfo,
  onChooseWav,
  codec,
  onCodecChange,
  loopEnabled,
  onLoopEnabledChange,
  loopStart,
  onLoopStartChange,
  loopEnd,
  onLoopEndChange,
  saveToRelativePath,
  onSaveToRelativePathChange,
  externalPath,
  busy,
}) {
  const sampleCount = Number(wavInfo?.samples || 0);
  return (
    <div className="brstm-create-fields">
      <div className="dialog-field">
        <label>Source WAV</label>
        <div className="dialog-path-row">
          <input value={wavInfo?.path || ""} readOnly placeholder="Choose a .wav file" />
          <button className="tb-btn" onClick={onChooseWav} disabled={busy}>Browse</button>
        </div>
        {wavInfo && (
          <span className="dialog-hint">
            {wavInfo.sampleRate.toLocaleString()} Hz · {wavInfo.channels} channel(s) · {sampleCount.toLocaleString()} samples
          </span>
        )}
      </div>

      <div className="dialog-field">
        <label>BRSTM encoding</label>
        <select value={codec} onChange={(e) => onCodecChange(e.target.value)} disabled={busy}>
          <option value="ADPCM">ADPCM</option>
          <option value="PCM16">PCM16</option>
          <option value="PCM8">PCM8</option>
        </select>
      </div>

      <label className="dialog-checkbox">
        <input
          type="checkbox"
          checked={loopEnabled}
          onChange={(e) => onLoopEnabledChange(e.target.checked)}
          disabled={busy}
        />
        <span>Enable looping</span>
      </label>

      {loopEnabled && (
        <div className="dialog-row">
          <div className="dialog-field" style={{ flex: 1 }}>
            <label>Loop start (sample)</label>
            <input
              type="number"
              min={0}
              max={Math.max(0, sampleCount - 1)}
              value={loopStart}
              onChange={(e) => onLoopStartChange(Math.max(0, Number(e.target.value) || 0))}
              disabled={busy}
            />
          </div>
          <div className="dialog-field" style={{ flex: 1 }}>
            <label>Loop end (sample)</label>
            <input
              type="number"
              min={1}
              max={sampleCount || undefined}
              value={loopEnd}
              onChange={(e) => onLoopEndChange(Math.max(1, Number(e.target.value) || 1))}
              disabled={busy}
            />
          </div>
        </div>
      )}

      <div className="brstm-output-choice">
        <label className="dialog-checkbox">
          <input
            type="checkbox"
            checked={saveToRelativePath}
            onChange={(e) => onSaveToRelativePathChange(e.target.checked)}
            disabled={busy}
          />
          <span>Save automatically to the relative BRSTM path</span>
        </label>
        <span className="dialog-hint">
          {saveToRelativePath
            ? `The file will be written beside the BRSAR using: ${externalPath || "(enter an external path)"}`
            : "A Save BRSTM dialog will open when you continue (default). The BRSAR path remains unchanged, so move the file to that path before playback."}
        </span>
      </div>
    </div>
  );
}

function AddSoundDialog({ onClose, onDirtyChange, onDataRefresh, initialSoundType = "WAVE" }) {
  const D = window.PYSAR_DATA;
  const defaultSeqBankIndex = (D.banks || [])[0]?.id ?? 0;
  const suggestedSeqGroupForBank = (bankId, data = D) => {
    const bank = (data.banks || []).find((item) => Number(item.id) === Number(bankId));
    const matchingGroup = (data.groups || []).find((group) =>
      (group.entries || []).some((entry) => Number(entry.logicalFileIndex) === Number(bank?.file))
    );
    return matchingGroup?.id ?? (data.groups || [])[0]?.id ?? 0;
  };
  const [soundType, setSoundType] = useStateD(["SEQ", "STRM", "WAVE"].includes(initialSoundType) ? initialSoundType : "WAVE");
  const [name, setName] = useStateD("");
  const [volume, setVolume] = useStateD(90);
  const [playerIndex, setPlayerIndex] = useStateD(0);
  const [brwsdList, setBrwsdList] = useStateD([]);
  const [brwsdFileIndex, setBrwsdFileIndex] = useStateD(null);
  const [wavPath, setWavPath] = useStateD("");
  const [seqSources, setSeqSources] = useStateD([]);
  const [seqSource, setSeqSource] = useStateD("existing");
  const [seqFileIndex, setSeqFileIndex] = useStateD(null);
  const [seqPath, setSeqPath] = useStateD("");
  const [seqSourceInfo, setSeqSourceInfo] = useStateD(null);
  const [seqBankIndex, setSeqBankIndex] = useStateD(defaultSeqBankIndex);
  const [seqGroupIndex, setSeqGroupIndex] = useStateD(suggestedSeqGroupForBank(defaultSeqBankIndex));
  const [seqStartLabel, setSeqStartLabel] = useStateD("");
  const [seqTempo, setSeqTempo] = useStateD(120);
  const [seqProgram, setSeqProgram] = useStateD(0);
  const [seqNote, setSeqNote] = useStateD(60);
  const [seqVelocity, setSeqVelocity] = useStateD(100);
  const [seqDuration, setSeqDuration] = useStateD(48);
  const [seqBankNotice, setSeqBankNotice] = useStateD(null);
  const [strmPath, setStrmPath] = useStateD("");
  const [strmSource, setStrmSource] = useStateD("existing");
  const [strmWavInfo, setStrmWavInfo] = useStateD(null);
  const [strmCodec, setStrmCodec] = useStateD("ADPCM");
  const [strmLoopEnabled, setStrmLoopEnabled] = useStateD(false);
  const [strmLoopStart, setStrmLoopStart] = useStateD(0);
  const [strmLoopEnd, setStrmLoopEnd] = useStateD(1);
  const [strmSaveToRelativePath, setStrmSaveToRelativePath] = useStateD(false);
  const [createdStrmPath, setCreatedStrmPath] = useStateD(null);
  const [busy, setBusy] = useStateD(false);
  const [wavPickerBusy, setWavPickerBusy] = useStateD(false);
  const [error, setError] = useStateD(null);
  const mountedRef = useRefD(false);
  const wavPickerRequestRef = useRefD(0);

  useEffectD(() => {
    mountedRef.current = true;
    const invalidateRequests = () => {
      mountedRef.current = false;
      wavPickerRequestRef.current += 1;
    };
    if (!window.pysar) return invalidateRequests;
    window.pysar.call("get_brwsd_list").then((r) => {
      if (!mountedRef.current) return;
      if (r?.ok && r.items?.length) {
        setBrwsdList(r.items);
        setBrwsdFileIndex(r.items[0].fileIndex);
      }
    }).catch(() => {});
    window.pysar.call("get_sequence_sources").then((r) => {
      if (!mountedRef.current) return;
      if (!r?.ok) return;
      const items = r.items || [];
      setSeqSources(items);
      if (items.length) {
        setSeqFileIndex(items[0].fileIndex);
        setSeqStartLabel(items[0].labels?.[0]?.name || "");
      } else {
        setSeqSource("new");
      }
    }).catch(() => {});
    return invalidateRequests;
  }, []);

  const selectedSeqFile = seqSources.find((item) => item.fileIndex === seqFileIndex) || null;
  const seqLabels = seqSource === "existing"
    ? (selectedSeqFile?.labels || [])
    : (seqSource === "file" ? (seqSourceInfo?.labels || []) : [{ name: "main", offset: 0 }]);

  async function browseWavPath() {
    if (!window.pysar || busy || wavPickerBusy) return;
    const requestId = ++wavPickerRequestRef.current;
    setWavPickerBusy(true);
    setError(null);
    try {
      // Adding a WAVE only needs the source path. Decoding it here used to
      // make the picker appear to finish long before its result reached the UI.
      const result = await window.pysar.call("choose_wav_file", false);
      if (!mountedRef.current || requestId !== wavPickerRequestRef.current) return;
      if (result?.ok && result.path) setWavPath(result.path);
      else if (result?.error && result.error !== "Cancelled") setError(result.error);
    } catch (ex) {
      if (mountedRef.current && requestId === wavPickerRequestRef.current) setError(String(ex));
    } finally {
      if (mountedRef.current && requestId === wavPickerRequestRef.current) setWavPickerBusy(false);
    }
  }

  async function browseStrmWav() {
    if (!window.pysar || busy) return;
    setError(null);
    try {
      const result = await window.pysar.call("choose_brstm_wav_file");
      if (result?.ok && result.path) {
        setStrmWavInfo(result);
        setStrmLoopStart(0);
        setStrmLoopEnd(Math.max(1, Number(result.samples) || 1));
      } else if (result?.error && result.error !== "Cancelled") {
        setError(result.error);
      }
    } catch (ex) {
      setError(String(ex));
    }
  }

  async function browseSequence() {
    if (!window.pysar || busy) return;
    setError(null);
    try {
      const result = await window.pysar.call("choose_sequence_source");
      if (result?.ok && result.path) {
        setSeqPath(result.path);
        setSeqSourceInfo(result);
        setSeqStartLabel(result.labels?.[0]?.name || "");
      } else if (result?.error && result.error !== "Cancelled") {
        setError(result.error);
      }
    } catch (ex) {
      setError(String(ex));
    }
  }

  async function importSequenceBank(importFormat) {
    if (!window.pysar || busy) return;
    if (importFormat !== "brbnk" && importFormat !== "sf2") return;
    setBusy(true);
    setError(null);
    try {
      const result = await window.pysar.call("import_bank_dialog", importFormat);
      if (!result?.ok) {
        if (!result?.cancelled && result?.error !== "Cancelled") setError(result?.error || "Bank import failed");
        return;
      }
      if (result.dirty) onDirtyChange(true);
      const refreshed = result.data || window.PYSAR_DATA;
      if (result.data) onDataRefresh(result.data);
      const importedBankId = Number(result.bankId);
      setSeqBankIndex(importedBankId);
      setSeqGroupIndex(suggestedSeqGroupForBank(importedBankId, refreshed));
      setSeqBankNotice(`${result.name || "Bank"} was imported and selected. It remains in the archive even if you close this dialog.`);
      if (result.warnings?.length) {
        await window.pysarAlert(result.warnings.join("\n"), { title: "Bank imported with warnings" });
      }
    } catch (ex) {
      setError(String(ex));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (wavPickerBusy) return;
    if (!name.trim()) { setError("Name is required"); return; }
    setBusy(true);
    setError(null);
    try {
      let result;
      if (soundType === "WAVE") {
        if (!wavPath.trim()) { setError("WAV path is required"); setBusy(false); return; }
        result = await window.pysar.call("add_wave_sound_from_wav_path", name.trim(), wavPath.trim(), playerIndex, volume, brwsdFileIndex);
      } else if (soundType === "SEQ") {
        if ((D.banks || []).length === 0) { setError("A BRBNK is required for sequence playback"); setBusy(false); return; }
        if (seqSource === "existing" && seqFileIndex == null) { setError("Choose an existing BRSEQ"); setBusy(false); return; }
        if (seqSource === "file" && !seqPath.trim()) { setError("Choose a BRSEQ or MIDI file"); setBusy(false); return; }
        result = await window.pysar.call(
          "add_seq_sound",
          name.trim(),
          seqSource,
          seqBankIndex,
          playerIndex,
          volume,
          seqSource === "existing" ? null : seqGroupIndex,
          seqSource === "file" ? seqPath.trim() : null,
          seqSource === "existing" ? seqFileIndex : null,
          seqStartLabel || null,
          seqTempo,
          seqProgram,
          seqNote,
          seqVelocity,
          seqDuration,
        );
      } else {
        if (!strmPath.trim()) { setError("BRSTM path is required"); setBusy(false); return; }
        if (strmSource === "create") {
          if (!strmWavInfo?.path) { setError("Choose a source WAV file"); setBusy(false); return; }
          result = await window.pysar.call(
            "add_strm_sound_from_wav_path",
            name.trim(),
            strmPath.trim(),
            strmWavInfo.path,
            playerIndex,
            volume,
            strmCodec,
            strmLoopEnabled,
            strmLoopStart,
            strmLoopEnabled ? strmLoopEnd : null,
            strmSaveToRelativePath,
          );
        } else {
          result = await window.pysar.call("add_strm_sound", name.trim(), strmPath.trim(), playerIndex, volume);
        }
      }
      if (!result?.ok) { setError(result?.error || "Failed"); setBusy(false); return; }
      if (result.dirty) onDirtyChange(true);
      if (result.data) onDataRefresh(result.data);
      if (soundType === "STRM" && strmSource === "create" && result.writtenPath) {
        setCreatedStrmPath(result.writtenPath);
        setBusy(false);
        return;
      }
      onClose();
    } catch (ex) {
      setError(String(ex));
    }
    setBusy(false);
  }

  if (createdStrmPath) {
    return (
      <ModalOverlay title="Sound Added" onClose={onClose} width={520}>
        <div className="dialog-form">
          <div className="dialog-success">
            <strong>BRSTM saved successfully</strong>
            <code>{createdStrmPath}</code>
            {!strmSaveToRelativePath && (
              <span className="dialog-hint">Move this file to the BRSAR path before previewing or playing it.</span>
            )}
          </div>
          <div className="dialog-actions">
            <button className="tb-btn primary" onClick={onClose}>Close</button>
          </div>
        </div>
      </ModalOverlay>
    );
  }

  return (
    <ModalOverlay title="Add Sound" onClose={onClose} width={soundType === "SEQ" ? 620 : (soundType === "STRM" && strmSource === "create" ? 580 : 480)}>
      <div className="dialog-form">
        <div className="dialog-field">
          <label>Sound type</label>
          <div className="dialog-seg">
            <button className={soundType === "STRM" ? "on" : ""} onClick={() => setSoundType("STRM")}>
              <span className="type-pill type-STRM">STRM</span>
            </button>
            <button className={soundType === "WAVE" ? "on" : ""} onClick={() => setSoundType("WAVE")}>
              <span className="type-pill type-WAVE">WAVE</span>
            </button>
            <button className={soundType === "SEQ" ? "on" : ""} onClick={() => setSoundType("SEQ")}>
              <span className="type-pill type-SEQ">SEQ</span>
            </button>
          </div>
        </div>

        <div className="dialog-field">
          <label>Name</label>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. SE_MY_SOUND" maxLength={255} />
        </div>

        <div className="dialog-row">
          <div className="dialog-field" style={{ flex: 1 }}>
            <label>Volume</label>
            <input type="number" value={volume} min={0} max={127} onChange={(e) => setVolume(Math.max(0, Math.min(127, Number(e.target.value) || 0)))} />
          </div>
          <div className="dialog-field" style={{ flex: 1 }}>
            <label>Player</label>
            <select value={playerIndex} onChange={(e) => setPlayerIndex(Number(e.target.value))}>
              {(D.players || []).map((p) => (
                <option key={p.id} value={p.id}>{p.name} [{p.id}]</option>
              ))}
            </select>
          </div>
        </div>

        {soundType === "WAVE" && (
          <div className="dialog-field">
            <label>WAV file</label>
            <div className="dialog-path-row">
              <input value={wavPath} onChange={(e) => setWavPath(e.target.value)} placeholder="Choose or enter a .wav file path" />
              <button className="tb-btn" onClick={browseWavPath} disabled={busy || wavPickerBusy}>
                {wavPickerBusy ? "Choosing…" : "Browse"}
              </button>
            </div>
            <span className="dialog-hint">Absolute and relative file paths are supported.</span>
          </div>
        )}

        {soundType === "WAVE" && brwsdList.length > 0 && (
          <div className="dialog-field">
            <label>Target BRWSD</label>
            <select value={brwsdFileIndex ?? ""} onChange={(e) => setBrwsdFileIndex(Number(e.target.value))}>
              {brwsdList.map((w) => (
                <option key={w.fileIndex} value={w.fileIndex}>{w.label}</option>
              ))}
            </select>
            <span className="dialog-hint">The WAV will be added to this BRWSD + its paired BRWAR</span>
          </div>
        )}

        {soundType === "SEQ" && (
          <>
            <div className="dialog-field">
              <label>Sequence source</label>
              <div className="dialog-seg">
                <button
                  className={seqSource === "existing" ? "on" : ""}
                  onClick={() => {
                    setSeqSource("existing");
                    const item = selectedSeqFile || seqSources[0];
                    setSeqStartLabel(item?.labels?.[0]?.name || "");
                  }}
                  disabled={!seqSources.length || busy}
                >Existing</button>
                <button className={seqSource === "file" ? "on" : ""} onClick={() => { setSeqSource("file"); setSeqStartLabel(seqSourceInfo?.labels?.[0]?.name || ""); }} disabled={busy}>Import file</button>
                <button className={seqSource === "new" ? "on" : ""} onClick={() => { setSeqSource("new"); setSeqStartLabel("main"); }} disabled={busy}>New</button>
              </div>
            </div>

            {seqSource === "existing" && (
              <div className="dialog-field">
                <label>Embedded BRSEQ</label>
                <select
                  value={seqFileIndex ?? ""}
                  onChange={(event) => {
                    const fileIndex = Number(event.target.value);
                    const item = seqSources.find((candidate) => candidate.fileIndex === fileIndex);
                    setSeqFileIndex(fileIndex);
                    setSeqStartLabel(item?.labels?.[0]?.name || "");
                  }}
                >
                  {seqSources.map((item) => <option key={item.fileIndex} value={item.fileIndex}>{item.label}</option>)}
                </select>
                <span className="dialog-hint">The new sound can start at a different label without duplicating this file.</span>
              </div>
            )}

            {seqSource === "file" && (
              <div className="dialog-field">
                <label>BRSEQ or MIDI file</label>
                <div className="dialog-path-row">
                  <input value={seqPath} readOnly placeholder="Choose .brseq, .mid, or .midi" />
                  <button className="tb-btn" onClick={browseSequence} disabled={busy}>Browse</button>
                </div>
                {seqSourceInfo && <span className="dialog-hint">{seqSourceInfo.format} · {seqSourceInfo.tracks} track(s) · {seqSourceInfo.tempo} BPM</span>}
              </div>
            )}

            {seqSource === "new" && (
              <>
                <div className="dialog-row">
                  <div className="dialog-field" style={{ flex: 1 }}><label>Tempo</label><input type="number" min={1} max={65535} value={seqTempo} onChange={(e) => setSeqTempo(Number(e.target.value) || 120)} /></div>
                  <div className="dialog-field" style={{ flex: 1 }}><label>Program</label><input type="number" min={0} max={65535} value={seqProgram} onChange={(e) => setSeqProgram(Math.max(0, Number(e.target.value) || 0))} /></div>
                </div>
                <div className="dialog-row">
                  <div className="dialog-field" style={{ flex: 1 }}><label>MIDI note</label><input type="number" min={0} max={127} value={seqNote} onChange={(e) => setSeqNote(Math.max(0, Math.min(127, Number(e.target.value) || 0)))} /></div>
                  <div className="dialog-field" style={{ flex: 1 }}><label>Velocity</label><input type="number" min={1} max={127} value={seqVelocity} onChange={(e) => setSeqVelocity(Math.max(1, Math.min(127, Number(e.target.value) || 1)))} /></div>
                  <div className="dialog-field" style={{ flex: 1 }}><label>Length (ticks)</label><input type="number" min={1} value={seqDuration} onChange={(e) => setSeqDuration(Math.max(1, Number(e.target.value) || 1))} /></div>
                </div>
                <span className="dialog-hint">Creates a playable one-note sequence that can be edited as MML afterward.</span>
              </>
            )}

            <div className="dialog-row">
              <div className="dialog-field" style={{ flex: 1 }}>
                <label>BRBNK</label>
                <select value={seqBankIndex} onChange={(event) => {
                  const nextBankIndex = Number(event.target.value);
                  setSeqBankIndex(nextBankIndex);
                  setSeqGroupIndex(suggestedSeqGroupForBank(nextBankIndex));
                }}>
                  {(D.banks || []).map((bank) => <option key={bank.id} value={bank.id}>{bank.name} [{bank.id}]</option>)}
                </select>
                <div className="dialog-row" style={{ marginTop: 6 }}>
                  <button className="tb-btn" onClick={() => importSequenceBank("brbnk")} disabled={busy}>
                    Import BRBNK…
                  </button>
                  <button className="tb-btn" onClick={() => importSequenceBank("sf2")} disabled={busy}>
                    Import SF2…
                  </button>
                </div>
                <span className="dialog-hint">Selecting an existing BRBNK only links it. Importing an SF2 converts it to BRBNK + BRWAR immediately. Note, that closing <b>Add Sound</b> does not undo that bank import.</span>
                {seqBankNotice && <span className="dialog-hint">{seqBankNotice}</span>}
              </div>
              {seqSource !== "existing" && (
                <div className="dialog-field" style={{ flex: 1 }}>
                  <label>Group</label>
                  <select value={seqGroupIndex} onChange={(event) => setSeqGroupIndex(Number(event.target.value))}>
                    {(D.groups || []).map((group) => <option key={group.id} value={group.id}>{group.name} [{group.id}]</option>)}
                  </select>
                  <span className="dialog-hint">Choose the load group used for this cue. The default follows the selected bank when possible.</span>
                </div>
              )}
            </div>

            {seqLabels.length > 0 && (
              <div className="dialog-field">
                <label>Start label</label>
                <select value={seqStartLabel} onChange={(event) => setSeqStartLabel(event.target.value)}>
                  {seqLabels.map((label, index) => <option key={`${label.name}-${index}`} value={label.name}>{label.name} · 0x{Number(label.offset || 0).toString(16).toUpperCase()}</option>)}
                </select>
              </div>
            )}
          </>
        )}

        {soundType === "STRM" && (
          <>
            <div className="dialog-field">
              <label>External BRSTM path</label>
              <input value={strmPath} onChange={(e) => setStrmPath(e.target.value)} placeholder="strm/BGM_TITLE.brstm" />
              <span className="dialog-hint">Path stored in the BRSAR, relative to the game's root.</span>
            </div>

            <div className="dialog-field">
              <label>BRSTM source</label>
              <div className="dialog-seg">
                <button className={strmSource === "existing" ? "on" : ""} onClick={() => setStrmSource("existing")} disabled={busy}>
                  Existing BRSTM
                </button>
                <button className={strmSource === "create" ? "on" : ""} onClick={() => setStrmSource("create")} disabled={busy}>
                  Create from WAV
                </button>
              </div>
            </div>

            {strmSource === "existing" ? (
              <span className="dialog-hint">
                Metadata will be read from the BRSTM at the external path. If it cannot be resolved, you will be asked to locate it.
              </span>
            ) : (
              <BrstmCreationFields
                wavInfo={strmWavInfo}
                onChooseWav={browseStrmWav}
                codec={strmCodec}
                onCodecChange={setStrmCodec}
                loopEnabled={strmLoopEnabled}
                onLoopEnabledChange={setStrmLoopEnabled}
                loopStart={strmLoopStart}
                onLoopStartChange={setStrmLoopStart}
                loopEnd={strmLoopEnd}
                onLoopEndChange={setStrmLoopEnd}
                saveToRelativePath={strmSaveToRelativePath}
                onSaveToRelativePathChange={setStrmSaveToRelativePath}
                externalPath={strmPath}
                busy={busy}
              />
            )}
          </>
        )}

        {error && <div className="dialog-error">{error}</div>}

        <div className="dialog-actions">
          <button className="tb-btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="tb-btn primary" onClick={submit} disabled={busy || wavPickerBusy}>
            {busy ? "Adding…" : "Add sound"}
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}

function ReplaceSoundDialog({ soundId, onClose, onDirtyChange, onDataRefresh, onPlaybackInvalidate }) {
  const [loading, setLoading] = useStateD(true);
  const [info, setInfo] = useStateD(null);
  const [error, setError] = useStateD(null);
  const [busy, setBusy] = useStateD(false);
  const [previewKey, setPreviewKey] = useStateD(null);
  const [previewBusyKey, setPreviewBusyKey] = useStateD(null);
  const [replacementByKey, setReplacementByKey] = useStateD({});
  const [strmPath, setStrmPath] = useStateD("");
  const [strmAction, setStrmAction] = useStateD(null);
  const [strmWavInfo, setStrmWavInfo] = useStateD(null);
  const [strmCodec, setStrmCodec] = useStateD("ADPCM");
  const [strmLoopEnabled, setStrmLoopEnabled] = useStateD(false);
  const [strmLoopStart, setStrmLoopStart] = useStateD(0);
  const [strmLoopEnd, setStrmLoopEnd] = useStateD(1);
  const [strmSaveToRelativePath, setStrmSaveToRelativePath] = useStateD(false);
  const [strmSavedPath, setStrmSavedPath] = useStateD(null);
  const [seqReplacement, setSeqReplacement] = useStateD(null);
  const [seqReplacementLabel, setSeqReplacementLabel] = useStateD("");
  const previewAudioRef = useRefD(null);
  const previewRequestRef = useRefD(0);

  useEffectD(() => {
    if (!window.pysar || soundId == null) return;
    refreshSamples();
  }, [soundId]);

  async function refreshSamples() {
    if (!window.pysar || soundId == null) return;
    try {
      const r = await window.pysar.call("get_sound_samples", soundId);
      if (r?.ok) {
        setInfo(r);
        if (r.soundType === "STRM") setStrmPath(r.externalPath || "");
      } else {
        setError(r?.error || "Failed to load samples");
      }
    } catch (ex) {
      setError(String(ex));
    }
    setLoading(false);
  }

  useEffectD(() => {
    return () => stopPreview(false);
  }, []);

  function stopPreview(updateState = true) {
    previewRequestRef.current += 1;
    const audio = previewAudioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    previewAudioRef.current = null;
    if (!updateState) return;
    setPreviewKey(null);
    setPreviewBusyKey(null);
  }

  async function previewSample(sample) {
    if (!window.pysar || busy) return;
    const sampleNo = info.soundType === "SEQ" ? sample.wavNo : sample.noteIndex;
    const key = `${info.soundType}:${sampleNo}`;
    if (previewKey === key && previewAudioRef.current) {
      stopPreview();
      return;
    }
    stopPreview();
    const requestId = ++previewRequestRef.current;
    setPreviewBusyKey(key);
    setError(null);
    try {
      const result = await window.pysar.call("get_sound_sample_stream_url", soundId, sampleNo, 0);
      if (requestId !== previewRequestRef.current) return;
      if (!result?.ok) {
        setError(result?.error || "Preview failed");
        setPreviewBusyKey(null);
        return;
      }
      const audio = new Audio(result.url);
      previewAudioRef.current = audio;
      audio.addEventListener("ended", () => {
        if (previewAudioRef.current === audio) stopPreview();
      });
      audio.addEventListener("error", () => {
        if (previewAudioRef.current === audio) {
          setError("Preview playback failed");
          stopPreview();
        }
      });
      await audio.play();
      if (previewAudioRef.current === audio && requestId === previewRequestRef.current) {
        setPreviewKey(key);
        setPreviewBusyKey(null);
      }
    } catch (ex) {
      if (requestId !== previewRequestRef.current) return;
      setError(String(ex));
      stopPreview();
    }
  }

  function sampleKey(sample) {
    const sampleNo = info.soundType === "SEQ" ? sample.wavNo : sample.noteIndex;
    return `${info.soundType}:${sampleNo}`;
  }

  function formatSpec(item) {
    if (!item) return "";
    const duration = item.durationMs < 1000 ? item.durationMs + " ms" : (item.durationMs / 1000).toFixed(2) + " s";
    return `${item.encoding} · ${item.sampleRate} Hz · ${item.channels}ch · ${duration}${item.looped ? " · looped" : ""}`;
  }

  async function chooseReplacement(sample) {
    stopPreview();
    const key = sampleKey(sample);
    setBusy(true);
    setError(null);
    try {
      const result = await window.pysar.call("choose_wav_file");
      if (!result?.ok) {
        if (result?.error !== "Cancelled") setError(result?.error || "Could not choose WAV");
        setBusy(false);
        return;
      }
      setReplacementByKey((current) => ({ ...current, [key]: result }));
    } catch (ex) {
      setError(String(ex));
    }
    setBusy(false);
  }

  async function applyReplacement(sample) {
    stopPreview();
    const sampleNo = info.soundType === "SEQ" ? sample.wavNo : sample.noteIndex;
    const key = sampleKey(sample);
    const replacement = replacementByKey[key];
    if (!replacement?.path) {
      setError("Choose a replacement WAV first");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await window.pysar.call("replace_sound_sample_from_wav_path", soundId, sampleNo, replacement.path);
      if (!result?.ok) { setError(result?.error || "Replace failed"); setBusy(false); return; }
      if (info.soundType === "SEQ") onPlaybackInvalidate?.(soundId);
      if (result.dirty) onDirtyChange(true);
      if (result.data) onDataRefresh(result.data);
      setReplacementByKey((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      await refreshSamples();
    } catch (ex) {
      setError(String(ex));
    }
    setBusy(false);
  }

  async function updateStrmPath() {
    setBusy(true);
    setStrmAction("path");
    setError(null);
    try {
      const result = await window.pysar.call("update_strm_path", soundId, strmPath);
      if (!result?.ok) {
        if (result?.error !== "Cancelled") setError(result?.error || "Update failed");
        return;
      }
      if (result.dirty) onDirtyChange(true);
      if (result.data) onDataRefresh(result.data);
      onClose();
    } catch (ex) {
      setError(String(ex));
    } finally {
      setBusy(false);
      setStrmAction(null);
    }
  }

  async function chooseStrmWav() {
    if (!window.pysar || busy) return;
    setError(null);
    try {
      const result = await window.pysar.call("choose_brstm_wav_file");
      if (result?.ok && result.path) {
        setStrmWavInfo(result);
        setStrmLoopStart(0);
        setStrmLoopEnd(Math.max(1, Number(result.samples) || 1));
      } else if (result?.error && result.error !== "Cancelled") {
        setError(result.error);
      }
    } catch (ex) {
      setError(String(ex));
    }
  }

  async function replaceStrmFile() {
    if (!strmWavInfo?.path) {
      setError("Choose a source WAV file");
      return;
    }
    setBusy(true);
    setStrmAction("file");
    setError(null);
    setStrmSavedPath(null);
    try {
      const result = await window.pysar.call(
        "replace_strm_file_from_wav_path",
        soundId,
        strmWavInfo.path,
        strmCodec,
        strmLoopEnabled,
        strmLoopStart,
        strmLoopEnabled ? strmLoopEnd : null,
        strmSaveToRelativePath,
      );
      if (!result?.ok) {
        if (result?.error !== "Cancelled") setError(result?.error || "Replace failed");
        return;
      }
      if (result.dirty) onDirtyChange(true);
      if (result.data) onDataRefresh(result.data);
      setStrmSavedPath(result.writtenPath || null);
      await refreshSamples();
    } catch (ex) {
      setError(String(ex));
    } finally {
      setBusy(false);
      setStrmAction(null);
    }
  }

  async function chooseSequenceReplacement() {
    if (!window.pysar || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await window.pysar.call("choose_sequence_source");
      if (result?.ok && result.path) {
        setSeqReplacement(result);
        setSeqReplacementLabel(result.labels?.[0]?.name || "");
      } else if (result?.error && result.error !== "Cancelled") {
        setError(result.error);
      }
    } catch (ex) {
      setError(String(ex));
    }
    setBusy(false);
  }

  async function applySequenceReplacement() {
    if (!seqReplacement?.path) {
      setError("Choose a BRSEQ or MIDI file first");
      return;
    }
    stopPreview();
    setBusy(true);
    setError(null);
    try {
      const result = await window.pysar.call(
        "replace_sequence_from_path",
        soundId,
        seqReplacement.path,
        seqReplacementLabel || null,
      );
      if (!result?.ok) { setError(result?.error || "Sequence replacement failed"); return; }
      onPlaybackInvalidate?.(soundId);
      if (result.dirty) onDirtyChange(true);
      if (result.data) onDataRefresh(result.data);
      setSeqReplacement(null);
      await refreshSamples();
    } catch (ex) {
      setError(String(ex));
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <ModalOverlay title="Replace Sound" onClose={onClose}>
        <div style={{ padding: 24, textAlign: "center", color: "var(--text-tertiary)" }}>Loading…</div>
      </ModalOverlay>
    );
  }

  if (!info) {
    return (
      <ModalOverlay title="Replace Sound" onClose={onClose}>
        <div className="dialog-error" style={{ margin: 16 }}>{error || "No data"}</div>
        <div className="dialog-actions"><button className="tb-btn" onClick={onClose}>Close</button></div>
      </ModalOverlay>
    );
  }

  if (info.soundType === "STRM") {
    return (
      <ModalOverlay title={`Replace Stream - ${info.soundName}`} onClose={onClose} width={620}>
        <div className="dialog-form">
          <div className="strm-replace-option">
            <div className="strm-replace-title">Replace the BRSTM file</div>
            <div className="dialog-hint">
              Create a BRSTM from a WAV while keeping the BRSAR's external path unchanged.
              The generated file's size, channels, and track flags are patched automatically.
            </div>
            <BrstmCreationFields
              wavInfo={strmWavInfo}
              onChooseWav={chooseStrmWav}
              codec={strmCodec}
              onCodecChange={setStrmCodec}
              loopEnabled={strmLoopEnabled}
              onLoopEnabledChange={setStrmLoopEnabled}
              loopStart={strmLoopStart}
              onLoopStartChange={setStrmLoopStart}
              loopEnd={strmLoopEnd}
              onLoopEndChange={setStrmLoopEnd}
              saveToRelativePath={strmSaveToRelativePath}
              onSaveToRelativePathChange={setStrmSaveToRelativePath}
              externalPath={info.externalPath || ""}
              busy={busy}
            />
            <button className="tb-btn primary" onClick={replaceStrmFile} disabled={busy}>
              {busy && strmAction === "file" ? "Creating…" : "Create BRSTM and patch sound…"}
            </button>
          </div>

          <div className="strm-replace-option">
            <div className="strm-replace-title">Change the referenced path</div>
            <div className="dialog-field">
              <label>External BRSTM path</label>
              <input value={strmPath} onChange={(e) => setStrmPath(e.target.value)} placeholder="strm/BGM_TITLE.brstm" />
              <span className="dialog-hint">
                Metadata is read from the BRSTM at this location. If it cannot be resolved, you will be asked to select the file directly.
              </span>
            </div>
            <button className="tb-btn" onClick={updateStrmPath} disabled={busy || !strmPath.trim()}>
              {busy && strmAction === "path" ? "Updating…" : "Update path and metadata"}
            </button>
          </div>

          <div className="brstm-current-metadata">
            <span>Current BRSTM metadata</span>
            <strong>{(info.fileSize || 0).toLocaleString()} bytes</strong>
            <span>Format</span>
            <strong>{info.codec || "-"} · {info.sampleRate ? `${Number(info.sampleRate).toLocaleString()} Hz` : "-"}</strong>
            <span>Layout</span>
            <strong>{info.channels || 0} channel(s) · {info.tracks || 0} track(s) · flags 0x{Number(info.trackFlags || 0).toString(16).toUpperCase()}</strong>
            <span>Duration</span>
            <strong>{info.durationMs ? formatDurationMs(info.durationMs) : "-"}</strong>
            <span>Loop</span>
            <strong>{info.looped == null ? "-" : (info.looped ? `${Number(info.loopStart || 0).toLocaleString()}–${Number(info.loopEnd || 0).toLocaleString()} samples` : "Disabled")}</strong>
          </div>
          {strmSavedPath && (
            <div className="dialog-success">
              <strong>BRSTM saved successfully</strong>
              <code>{strmSavedPath}</code>
              {!strmSaveToRelativePath && (
                <span className="dialog-hint">Move this file to the BRSAR path before previewing or playing it.</span>
              )}
            </div>
          )}
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button className="tb-btn" onClick={onClose} disabled={busy}>Close</button>
          </div>
        </div>
      </ModalOverlay>
    );
  }

  return (
    <ModalOverlay title={`Replace Samples - ${info.soundName}`} onClose={onClose} width={580}>
      <div className="dialog-form">
        {info.soundType === "SEQ" && (
          <div className="strm-replace-option">
            <div className="strm-replace-title">Replace sequence bytecode</div>
            <div className="dialog-hint">
              Import BRSEQ or MIDI while keeping this sound's index and name.
              {Number(info.sharedReferenceCount || 0) > 1
                ? ` This BRSEQ is shared by ${info.sharedReferenceCount} sounds, so Pysar will isolate this sound automatically.`
                : " The embedded BRSEQ will be replaced in place."}
            </div>
            <div className="dialog-path-row">
              <input value={seqReplacement?.path || ""} readOnly placeholder="Choose .brseq, .mid, or .midi" />
              <button className="tb-btn" onClick={chooseSequenceReplacement} disabled={busy}>Browse</button>
            </div>
            {seqReplacement && (
              <>
                <span className="dialog-hint">{seqReplacement.format} · {seqReplacement.tracks} track(s) · {seqReplacement.tempo} BPM</span>
                {(seqReplacement.labels || []).length > 0 && (
                  <div className="dialog-field">
                    <label>Start label</label>
                    <select value={seqReplacementLabel} onChange={(event) => setSeqReplacementLabel(event.target.value)}>
                      {seqReplacement.labels.map((label, index) => <option key={`${label.name}-${index}`} value={label.name}>{label.name}</option>)}
                    </select>
                  </div>
                )}
              </>
            )}
            <button className="tb-btn primary" onClick={applySequenceReplacement} disabled={busy || !seqReplacement}>
              {busy ? "Replacing…" : "Replace sequence"}
            </button>
          </div>
        )}
        <div className="dialog-hint" style={{ marginBottom: 12 }}>
          {info.soundType === "WAVE"
            ? 'This WAVE sound uses the samples below. Click "Replace" to swap a sample with a new .wav file.'
            : 'This SEQ sound uses the following wave samples (via its bank instrument). Click "Replace" to swap a specific variation.'}
        </div>

        {info.samples.length === 0 && (
          <div style={{ color: "var(--text-tertiary)", fontSize: 12, padding: 12 }}>No samples found for this sound.</div>
        )}

        <div className="sample-list">
          {info.samples.map((s, i) => {
            const key = sampleKey(s);
            const replacement = replacementByKey[key];
            const label = info.soundType === "SEQ"
              ? `Variation #${s.wavNo} · wave ${s.waveIndex} · keys ${s.keyLow}-${s.keyHigh}`
              : `Sample #${s.noteIndex} · wave ${s.waveIndex}`;
            return (
              <div key={i} className="sample-item">
                <div className="sample-info">
                  <span className="sample-label">{label}</span>
                  <span className="sample-meta">{formatSpec(s)}</span>
                  {replacement && (
                    <span className="sample-replacement">
                      New: {replacement.name || replacement.path.split(/[\\/]/).pop()} · {formatSpec(replacement)}
                    </span>
                  )}
                </div>
                <div className="sample-actions">
                  <button className="tb-btn" onClick={() => previewSample(s)} disabled={busy || previewBusyKey === key}>
                    {previewBusyKey === key ? "Loading..." : (previewKey === key ? "Stop" : "Play")}
                  </button>
                  <button className="tb-btn" onClick={() => chooseReplacement(s)} disabled={busy}>
                    Choose
                  </button>
                  <button className="tb-btn primary" onClick={() => applyReplacement(s)} disabled={busy || !replacement}>
                    Apply
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        {error && <div className="dialog-error">{error}</div>}

        <div className="dialog-actions">
          <button className="tb-btn" onClick={onClose} disabled={busy}>Close</button>
        </div>
      </div>
    </ModalOverlay>
  );
}

function ChooseRwavEncodingDialog({ target, onClose, onReplace }) {
  const [codec, setCodec] = useStateD("");
  const filename = String(target?.path || "").split(/[\\/]/).pop() || "selected WAV";
  const isWaveSound = target?.kind === "sound";
  const targetName = isWaveSound
    ? (target?.soundName || "this WAVE sound")
    : (target?.archiveName || `WAR_${Number(target?.archiveId || 0).toString().padStart(4, "0")}`);

  return (
    <ModalOverlay title="Encode WAV as RWAV" onClose={onClose} width={480}>
      <div className="dialog-form">
        <div className="dialog-hint">
          <strong>{filename}</strong> will replace {isWaveSound
            ? `the playable sample in ${targetName}`
            : `sample #${target?.waveIndex} in ${targetName}`}.
        </div>
        <div className="dialog-field">
          <label>RWAV encoding</label>
          <select value={codec} onChange={(event) => setCodec(event.target.value)} autoFocus>
            <option value="" disabled>Choose an encoding…</option>
            <option value="ADPCM">ADPCM — compact Nintendo ADPCM</option>
            <option value="PCM16">PCM16 — uncompressed 16-bit</option>
            <option value="PCM8">PCM8 — uncompressed 8-bit</option>
          </select>
        </div>
        <div className="dialog-hint">
          A raw .brwav replacement keeps its own embedded encoding and skips this step.
        </div>
        <div className="dialog-actions">
          <button className="tb-btn" onClick={onClose}>Cancel</button>
          <button className="tb-btn primary" onClick={() => onReplace(codec)} disabled={!codec}>Replace</button>
        </div>
      </div>
    </ModalOverlay>
  );
}

function UnsavedDialog({ onSave, onDiscard, onCancel, busy = false, message = null }) {
  return (
    <ModalOverlay title="Unsaved Changes" onClose={busy ? () => {} : onCancel} width={410}>
      <div className="dialog-form">
        <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 16 }}>
          {message || "The file was modified. Do you want to save the file before closing?"}
        </div>
        <div className="dialog-actions" style={{ gap: 8 }}>
          <button className="tb-btn" onClick={onDiscard} disabled={busy}>Don't save</button>
          <button className="tb-btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="tb-btn primary" onClick={onSave} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
        </div>
      </div>
    </ModalOverlay>
  );
}

function DumpArchiveStatusDialog({
  busy = false,
  aborting = false,
  cancelled = false,
  mode = "converted",
  path = null,
  error = null,
  summary = null,
  progress = null,
  onClose = () => {},
  onAbort = () => {},
}) {
  const close = busy ? () => {} : onClose;
  const completed = Math.max(0, Number(progress?.completed || 0));
  const total = Math.max(0, Number(progress?.total || 0));
  const percent = Math.max(0, Math.min(100, Number(progress?.percent || 0)));
  const hasKnownTotal = total > 0;
  return (
    <ModalOverlay title="Dump Archive" onClose={close} width={500}>
      <div className="dialog-form">
        {busy ? (
          <div>
            <strong>{mode === "original" ? "Dumping original subfiles…" : "Converting archive…"}</strong>
            <div className="dialog-hint" style={{ marginTop: 8 }}>
              {aborting
                ? "Stopping safely and removing the unfinished staging folder…"
                : mode === "original"
                ? "Large archives can take a moment. Keep Pysar open until this finishes."
                : "Large sequences and wave archives can take a moment. Keep Pysar open until this finishes."}
            </div>
            <div className="dump-progress">
              <div
                className={"dump-progress-track" + (hasKnownTotal ? "" : " indeterminate")}
                role="progressbar"
                aria-label="Archive dump progress"
                aria-valuemin="0"
                aria-valuemax={hasKnownTotal ? total : undefined}
                aria-valuenow={hasKnownTotal ? completed : undefined}
              >
                <div className="dump-progress-fill" style={hasKnownTotal ? { width: percent + "%" } : undefined} />
              </div>
              <div className="dump-progress-meta">
                <span>{progress?.detail || "Preparing archive dump…"}</span>
                <span>{hasKnownTotal ? `${completed} / ${total} · ${percent}%` : "Preparing…"}</span>
              </div>
            </div>
          </div>
        ) : cancelled ? (
          <div className="dialog-hint">Archive dump cancelled. No output folder was created.</div>
        ) : error ? (
          <div className="dialog-error">{error}</div>
        ) : (
          <div className="dialog-success">
            <strong>Archive dumped successfully</strong>
            {path && <code>{path}</code>}
          </div>
        )}

        {!busy && summary && <div className="dialog-hint">{summary}</div>}

        <div className="dialog-actions">
          {busy ? (
            <button className="tb-btn" onClick={onAbort} disabled={aborting}>
              {aborting ? "Aborting…" : "Abort"}
            </button>
          ) : (
            <button className="tb-btn primary" onClick={onClose}>Close</button>
          )}
        </div>
      </div>
    </ModalOverlay>
  );
}

function DumpArchiveOptionsDialog({ onClose, onStart }) {
  const [mode, setMode] = useStateD("converted");
  return (
    <ModalOverlay title="Dump Archive" onClose={onClose} width={520}>
      <div className="dialog-form">
        <div className="dialog-field">
          <label>Dump contents</label>
          <div className="dump-mode-options">
            <button className={"dump-mode-option" + (mode === "original" ? " on" : "")} onClick={() => setMode("original")}>
              <strong>Original subfiles</strong>
              <span>Lossless Nintendo subfiles: BRBNK, BRSEQ, BRWAR, BRWAV, BRWSD, and found external files.</span>
            </button>
            <button className={"dump-mode-option" + (mode === "converted" ? " on" : "")} onClick={() => setMode("converted")}>
              <strong>Converted assets</strong>
              <span>SF2 banks, WAV audio, and MIDI. Every selectable BRSEQ variation is rendered as its own WAV.</span>
            </button>
          </div>
        </div>
        <div className="dialog-hint">
          You will choose a parent folder next. Pysar creates a new, uniquely named dump folder inside it.
        </div>
        <div className="dialog-actions">
          <button className="tb-btn" onClick={onClose}>Cancel</button>
          <button className="tb-btn primary" onClick={() => onStart(mode)}>Choose folder…</button>
        </div>
      </div>
    </ModalOverlay>
  );
}

Object.assign(window, {
  ModalOverlay,
  PysarDialogHost,
  pysarAlert,
  pysarConfirm,
  pysarPrompt,
  AddSoundDialog,
  ReplaceSoundDialog,
  ChooseRwavEncodingDialog,
  UnsavedDialog,
  DumpArchiveOptionsDialog,
  DumpArchiveStatusDialog,
});
