function PropertiesTab({ item, onNavigate, onUpdateSound, onRenameSound, onDeleteSound, onRenameBank, onReplaceBank, onExportBank, onDeleteBank, onUpdateGroup, onUpdatePlayer, onRenamePlayer, onDeletePlayer, onDeleteGroup, onReplaceSound, onExportSound, onReplaceWave, onExportWave, onDeleteWave, onUpdateWave }) {
  const D = window.PYSAR_DATA;
  // SOUND properties
  if (item.kind === "sound") {
    const s = item.item;
    const patch = (key, val) => { if (onUpdateSound) onUpdateSound(s.id, { [key]: val }); };
    return (
      <>
        <CollapsibleSection title="Identity">
          <Field label="Name"><TextInput value={s.name} disabled={!!s.protected} onChange={(v) => patch("name", v)} /></Field>
          <Field label="ID"><ReadOnly value={s.id} /></Field>
          <Field label="Type"><ReadOnlyPill>{pillFor(s.type)}</ReadOnlyPill></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Routing">
          <Field label="Player"><RefSelect value={s.player} options={D.players || []} onChange={(v) => patch("player", v)} onNavigate={onNavigate} referenceKind="player" /></Field>
          <Field label="Bank"><RefSelect value={s.bank} options={D.banks || []} empty="No bank" disabled onNavigate={onNavigate} referenceKind="bank" /></Field>
          <Field label="Group"><RefSelect value={s.group} options={D.groups || []} empty="No group" disabled onNavigate={onNavigate} referenceKind="group" /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Volume & Pan">
          <Field label="Volume"><NumberInput value={s.volume} min={0} max={127} onChange={(v) => patch("volume", v)} /></Field>
          <Field label="Priority"><NumberInput value={s.priority} min={0} max={127} onChange={(v) => patch("priority", v)} /></Field>
          <Field label="Pan"><NumberInput value={s.pan} min={-64} max={63} disabled /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Pitch" defaultOpen={false}>
          <Field label="Pitch"><NumberInput value={s.pitch ?? 1} min={0.01} max={16} step={0.001} disabled /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="File" defaultOpen={false}>
          <Field label="File index"><ReadOnly value={s.file} /></Field>
          <Field label="Data file"><RefSelect value={s.dataFileId} options={D.files || []} disabled empty="No data file" onNavigate={onNavigate} referenceKind="file" /></Field>
          <Field label="Audio file"><RefSelect value={s.audioFileId} options={D.waveArchives || []} disabled empty="No audio file" onNavigate={onNavigate} referenceKind="archive" /></Field>
          {s.type === "STRM" && (
            <>
              <Field label="External path"><ReadOnly value={s.externalPath || "-"} /></Field>
              <Field label="File size"><ReadOnly value={formatBytesValue(s.fileSize)} /></Field>
              <Field label="Channels"><ReadOnly value={s.channels ?? "-"} /></Field>
              <Field label="Track flags"><ReadOnly value={`0x${Number(s.trackFlags || 0).toString(16).toUpperCase()}`} /></Field>
            </>
          )}
        </CollapsibleSection>
        <CollapsibleSection title="Actions" defaultOpen={true}>
          <div className="inspector-actions">
            <button
              className="tb-btn"
              disabled={!!s.protected}
              title={s.protected ? "Safe Mode protects this original sound" : "Rename sound"}
              onClick={() => onRenameSound?.(s)}
            >
              Rename
            </button>
            <button className="tb-btn" onClick={() => { if (onReplaceSound) onReplaceSound(s.id); }}>
              Replace
            </button>
            {(["STRM", "WAVE", "SEQ"].includes(s.type)) && (
              <button className="tb-btn" onClick={() => { if (onExportSound) onExportSound(s.id); }}>
                Export
              </button>
            )}
            <button className="tb-btn danger" disabled={!!s.protected} onClick={() => onDeleteSound?.(s)}>
              Delete
            </button>
          </div>
        </CollapsibleSection>
      </>
    );
  }
  if (item.kind === "bank") {
    const b = item.item;
    return (
      <>
        <CollapsibleSection title="Identity">
          <Field label="Name"><ReadOnly value={b.name} className="" /></Field>
          <Field label="ID"><ReadOnly value={b.id} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Contents">
          <Field label="Instruments"><ReadOnly value={b.instruments} /></Field>
          <Field label="Waves"><ReadOnly value={b.waves} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Files" defaultOpen={false}>
          <Field label="File index"><ReadOnly value={b.file} /></Field>
          <Field label="Data file"><RefSelect value={b.dataFileId} options={D.files || []} disabled empty="No data file" onNavigate={onNavigate} referenceKind="file" /></Field>
          <Field label="Audio file"><RefSelect value={b.audioFileId} options={D.waveArchives || []} disabled empty="No audio file" onNavigate={onNavigate} referenceKind="archive" /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Actions">
          <div className="inspector-actions">
            <button className="tb-btn" onClick={() => onReplaceBank?.(b)}>
              Replace
            </button>
            <button className="tb-btn" onClick={() => onExportBank?.(b)}>
              Export
            </button>
            <button className="tb-btn" disabled={!!b.protected} onClick={() => onRenameBank?.(b)}>
              Rename
            </button>
            <button className="tb-btn danger" disabled={!!b.protected} onClick={() => onDeleteBank?.(b)}>
              Delete
            </button>
          </div>
        </CollapsibleSection>
      </>
    );
  }
  if (item.kind === "player") {
    const p = item.item;
    const patch = (key, value) => onUpdatePlayer?.(p.id, { [key]: value });
    return (
      <>
        <CollapsibleSection title="Identity">
          <Field label="Name"><TextInput value={p.name} disabled={!!p.protected} onChange={(value) => patch("name", value)} /></Field>
          <Field label="ID"><ReadOnly value={p.id} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Capacity">
          <Field label="Playable sounds"><NumberInput value={p.playableSounds} min={0} max={255} onChange={(value) => patch("playableSounds", value)} /></Field>
          <Field label="Heap size"><NumberInput value={p.heap || 0} min={0} max={4294967295} onChange={(value) => patch("heapSize", value)} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Actions">
          <div className="inspector-actions">
            <button
              className="tb-btn"
              disabled={!!p.protected}
              title={p.protected ? "Safe Mode protects this original player" : "Rename player"}
              onClick={() => onRenamePlayer?.(p)}
            >Rename</button>
            <button
              className="tb-btn danger"
              disabled={!!p.protected}
              title={p.protected ? "Safe Mode protects this original player" : "Delete player"}
              onClick={() => onDeletePlayer?.(p)}
            >Delete</button>
          </div>
        </CollapsibleSection>
      </>
    );
  }
  if (item.kind === "group") {
    const g = item.item;
    return (
      <>
        <CollapsibleSection title="Identity">
          <Field label="Name"><TextInput value={g.name} disabled={!!g.protected} onChange={(v) => onUpdateGroup?.(g.id, { name: v })} /></Field>
          <Field label="ID"><ReadOnly value={g.id} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Contents">
          <Field label="Items"><ReadOnly value={g.items} /></Field>
          <Field label="File data"><ReadOnly value={formatBytesValue(g.fileSize)} /></Field>
          <Field label="Audio data"><ReadOnly value={formatBytesValue(g.audioSize)} /></Field>
          <Field label="Total size"><ReadOnly value={formatBytesValue(g.size)} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Files" defaultOpen={false}>
          <Field label="File ID"><RefSelect value={g.fileId} options={D.files || []} disabled empty="No file" onNavigate={onNavigate} referenceKind="file" /></Field>
          <Field label="Audio file"><RefSelect value={g.audioFileId} options={D.waveArchives || []} disabled empty="No audio file" onNavigate={onNavigate} referenceKind="archive" /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Actions">
          <button className="tb-btn danger" style={{ width: "100%" }} disabled={!!g.protected} onClick={() => onDeleteGroup?.(g.id, g.name)}>
            Delete
          </button>
        </CollapsibleSection>
      </>
    );
  }
  if (item.kind === "archive") {
    const a = item.item;
    return (
      <>
        <CollapsibleSection title="Identity">
          <Field label="Name"><ReadOnly value={a.name} className="" /></Field>
          <Field label="File ID"><ReadOnly value={a.id} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Contents">
          <Field label="Waves"><ReadOnly value={a.waves} /></Field>
          <Field label="Size"><ReadOnly value={formatBytesValue(a.size)} /></Field>
          <Field label="Linked banks">
            {(a.linkedBanks || []).length === 0
              ? <ReadOnly value="-" className="mono" />
              : <span className="input-ish linked-banks-value">
                {(a.linkedBanks || []).map((name, index) => {
                  const bank = (D.banks || []).find((candidate) => candidate.name === name);
                  return (
                    <React.Fragment key={`${name}:${index}`}>
                      <span className="linked-bank-entry">
                        {bank && onNavigate
                          ? <button className="inline-reference" onClick={() => onNavigate({ kind: "bank", id: bank.id })}>{name}</button>
                          : name}
                        {index < (a.linkedBanks || []).length - 1 && ","}
                      </span>
                      {index < (a.linkedBanks || []).length - 1 && " "}
                    </React.Fragment>
                  );
                })}
              </span>}
          </Field>
        </CollapsibleSection>
      </>
    );
  }
  if (item.kind === "wave") {
    const w = item.item || {};
    const sizeStr = (w.sizeBytes || 0) >= 1024
      ? ((w.sizeBytes || 0) / 1024).toFixed(1) + " KB"
      : (w.sizeBytes || 0) + " B";
    const durationStr = (w.durationMs == null)
      ? "-"
      : w.durationMs < 1000 ? w.durationMs + " ms" : (w.durationMs / 1000).toFixed(2) + " s";
    return (
      <>
        <CollapsibleSection title="Identity">
          <Field label="Index"><ReadOnly value={w.index ?? w.waveIndex} /></Field>
          <Field label="Archive"><RefSelect value={w.archiveId} options={D.waveArchives || []} disabled empty={w.archiveName || "No archive"} onNavigate={onNavigate} referenceKind="archive" /></Field>
          {w.encoding && (
            <Field label="Encoding">
              <ReadOnlyPill>
                <span className={"type-pill " + (w.encoding === "ADPCM" ? "type-RWAR" : w.encoding === "PCM16" ? "type-RBNK" : w.encoding === "PCM8" ? "type-RWSD" : "type-BIN")}>
                  {w.encoding}
                </span>
              </ReadOnlyPill>
            </Field>
          )}
        </CollapsibleSection>
        <CollapsibleSection title="Audio">
          <Field label="Channels"><ReadOnly value={w.channels} /></Field>
          <Field label="Sample rate">
            <ReadOnly value={w.sampleRate ? w.sampleRate.toLocaleString() + " Hz" : "-"} />
          </Field>
          <Field label="Samples">
            <ReadOnly value={w.samples != null ? w.samples.toLocaleString() : "-"} />
          </Field>
          <Field label="Duration"><ReadOnly value={durationStr} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Loop">
          <Field label="Looped">
            <input
              type="checkbox"
              checked={!!w.looped}
              disabled={!!w.protected}
              onChange={(event) => onUpdateWave?.(
                w.archiveId,
                w.index ?? w.waveIndex,
                { looped: event.target.checked, loopStart: Number(w.loopStart || 0) },
              )}
            />
          </Field>
          <Field label="Loop start">
            <NumberInput
              value={w.loopStart ?? 0}
              min={0}
              max={Math.max(0, Number(w.samples || 1) - 1)}
              disabled={!!w.protected || !w.looped}
              onChange={(value) => onUpdateWave?.(
                w.archiveId,
                w.index ?? w.waveIndex,
                { looped: true, loopStart: value },
              )}
            />
          </Field>
        </CollapsibleSection>
        <CollapsibleSection title="Storage" defaultOpen={false}>
          <Field label="Archive ID"><ReadOnly value={w.archiveId} /></Field>
          <Field label="Size"><ReadOnly value={sizeStr} /></Field>
        </CollapsibleSection>
        <CollapsibleSection title="Actions" defaultOpen={true}>
          <div className="inspector-actions">
            <button
              className="tb-btn"
              style={{ flex: 1 }}
              disabled={w.archiveId == null || (w.index ?? w.waveIndex) == null}
              onClick={() => onReplaceWave?.(w.archiveId, w.index ?? w.waveIndex)}
            >Replace</button>
            <button
              className="tb-btn"
              style={{ flex: 1 }}
              disabled={w.archiveId == null || (w.index ?? w.waveIndex) == null}
              onClick={() => onExportWave?.(w.archiveId, w.index ?? w.waveIndex)}
            >Export</button>
            <button
              className="tb-btn danger"
              style={{ flex: 1 }}
              disabled={!!w.protected || w.archiveId == null || (w.index ?? w.waveIndex) == null}
              title={w.protected ? "Safe Mode protects this original sample" : "Delete sample"}
              onClick={() => onDeleteWave?.(w.archiveId, w.index ?? w.waveIndex)}
            >Delete</button>
          </div>
        </CollapsibleSection>
      </>
    );
  }
  if (item.kind === "file") {
    const f = item.item;
    const sizeStr = formatBytesValue(f.size);
    return (
      <>
        <CollapsibleSection title="Identity">
          <Field label="Label"><ReadOnly value={f.label} className="" /></Field>
          <Field label="ID">
            <ReadOnly value={f.id < 0 ? "-" : f.id} />
          </Field>
          <Field label="File index"><ReadOnly value={f.fileIndex} /></Field>
          <Field label="Kind">
            <ReadOnlyPill>
              <span className={"type-pill type-" + f.kind}>{f.kind}</span>
            </ReadOnlyPill>
          </Field>
        </CollapsibleSection>
        <CollapsibleSection title="Storage">
          <Field label="Size"><ReadOnly value={sizeStr} /></Field>
          <Field label="Source">
            <ReadOnly value={f.external ? "External file" : "Embedded in BRSAR"} className="" />
          </Field>
          {f.external && f.externalPath && (
            <Field label="Path"><ReadOnly value={f.externalPath} /></Field>
          )}
        </CollapsibleSection>
      </>
    );
  }
  return null;
}

window.PropertiesTab = PropertiesTab;
