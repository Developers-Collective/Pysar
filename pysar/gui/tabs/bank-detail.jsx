const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function noteName(midi) {
  const m = Math.max(0, Math.min(127, Math.round(midi)));
  return NOTE_NAMES[m % 12] + (Math.floor(m / 12) - 1);
}

function summariseInstrument(ins) {
  if (ins.isEmpty || !ins.zones || ins.zones.length === 0) {
    return { span: "-", root: "-" };
  }
  const isFullRange = ins.keyLow === 0 && ins.keyHigh === 127;
  const span = isFullRange ? "Full" : `${noteName(ins.keyLow)} – ${noteName(ins.keyHigh)}`;
  const roots = new Set(ins.zones.map((z) => z.originalKey));
  const root = roots.size === 1 ? noteName([...roots][0]) : "varies";
  return { span, root };
}

const ZONE_PALETTE = ["#5b8def", "#7c5cff", "#3ec19a", "#f0b132", "#e85d4a", "#c47ad9", "#3aa4c4", "#e879a7"];
const BANK_INSTRUMENT_COLUMNS = [
  { key: "program", label: "#", width: 56, align: "num" },
  { key: "name", label: "Name", minWidth: 80 },
  { key: "zones", label: "Zones", width: 70, align: "num" },
  { key: "span", label: "Span", width: 130 },
  { key: "root", label: "Root", width: 90 },
  { key: "waves", label: "Waves", width: 70, align: "num" },
];

function zoneColor(waveIndex) {
  const i = Math.abs(Number(waveIndex) | 0);
  return ZONE_PALETTE[i % ZONE_PALETTE.length];
}

function BankDetail({ bank, refreshRevision = 0, onNavigate, onDirty, onDataRefresh, onPlaybackInvalidate, onError, onPlayNote, playingNote, onReplace, onExport, onRename, onDelete }) {
  const [details, setDetails] = useStateB(null);
  const [loading, setLoading] = useStateB(true);
  const [error, setError] = useStateB(null);
  const [activeProg, setActiveProg] = useStateB(null);
  const [selectedZone, setSelectedZone] = useStateB(0);
  const [fileBusy, setFileBusy] = useStateB(false);
  const [instrumentBusy, setInstrumentBusy] = useStateB(false);
  const instrumentBusyRef = React.useRef(false);
  const instrumentColumnSizing = useResizableTableColumns(BANK_INSTRUMENT_COLUMNS);

  React.useEffect(() => {
    let cancelled = false;
    setDetails(null);
    setError(null);
    setLoading(true);
    setActiveProg(null);
    setSelectedZone(0);
    if (!window.pysar) {
      setLoading(false);
      return;
    }
    window.pysar.call("get_bank_details", bank.id).then((result) => {
      if (cancelled) return;
      if (result?.ok) {
        setDetails(result.data);
        const first = (result.data.instruments || []).find((ins) => !ins.isEmpty);
        if (first) setActiveProg(first.program);
      } else {
        setError(result?.error || "Could not load bank");
      }
      setLoading(false);
    }).catch((e) => {
      if (cancelled) return;
      setError(String(e));
      setLoading(false);
    });
    return () => { cancelled = true; };
  // Safe Mode changes the protection flags returned for instruments/zones,
  // even though the selected bank ID stays the same.
  }, [bank.id, bank.protected, refreshRevision]);

  React.useEffect(() => { setSelectedZone(0); }, [activeProg]);

  function reportError(message) {
    if (onError) onError(message);
    else window.pysarAlert(message, { title: "Bank operation failed" });
  }

  function refreshArchiveMetadata(result) {
    if (!result?.archiveData) return;
    window.PYSAR_DATA = result.archiveData;
    onPlaybackInvalidate?.(bank.id);
    onDataRefresh?.(result.archiveData);
  }

  async function runBankAction(action) {
    if (!action || fileBusy) return;
    setFileBusy(true);
    try {
      await action(bank);
    } finally {
      setFileBusy(false);
    }
  }

  async function updateZone(program, zoneIndex, patch) {
    if (!window.pysar) return;
    const result = await window.pysar.call("update_bank_zone", bank.id, program, zoneIndex, patch).catch((e) => ({ ok: false, error: String(e) }));
    if (result?.ok) {
      if (result.dirty) onDirty?.(true);
      if (result.data) setDetails(result.data);
      refreshArchiveMetadata(result);
    } else reportError(result?.error || "Could not update bank zone");
  }

  async function splitKeyRegion(program, atKey) {
    if (!window.pysar) return;
    const result = await window.pysar.call("split_bank_key_region", bank.id, program, atKey).catch((e) => ({ ok: false, error: String(e) }));
    if (result?.ok) {
      if (result.dirty) onDirty?.(true);
      if (result.data) setDetails(result.data);
      refreshArchiveMetadata(result);
    } else reportError(result?.error || "Could not split bank region");
  }

  async function deleteZone(program, zoneIndex) {
    if (!window.pysar) return;
    const result = await window.pysar.call("delete_bank_zone", bank.id, program, zoneIndex).catch((e) => ({ ok: false, error: String(e) }));
    if (result?.ok) {
      if (result.dirty) onDirty?.(true);
      if (result.data) setDetails(result.data);
      refreshArchiveMetadata(result);
      setSelectedZone(0);
    } else reportError(result?.error || "Could not delete bank zone");
  }

  async function addInstrument() {
    if (!window.pysar || !details || instrumentBusyRef.current) return;
    instrumentBusyRef.current = true;
    setInstrumentBusy(true);
    const nextProg = details.instruments.length;
    try {
      const result = await window.pysar.call("add_bank_instrument", bank.id, nextProg).catch((e) => ({ ok: false, error: String(e) }));
      if (result?.ok) {
        if (result.dirty) onDirty?.(true);
        if (result.data) { setDetails(result.data); setActiveProg(nextProg); }
        refreshArchiveMetadata(result);
      } else reportError(result?.error || "Could not add bank instrument");
    } finally {
      instrumentBusyRef.current = false;
      setInstrumentBusy(false);
    }
  }

  async function removeInstrument(program) {
    if (!window.pysar) return;
    if (!await window.pysarConfirm(`Remove instrument ${program}?`, {
      title: "Remove instrument",
      confirmLabel: "Remove",
      danger: true,
    })) return;
    const result = await window.pysar.call("remove_bank_instrument", bank.id, program).catch((e) => ({ ok: false, error: String(e) }));
    if (result?.ok) {
      if (result.dirty) onDirty?.(true);
      if (result.data) setDetails(result.data);
      refreshArchiveMetadata(result);
      if (activeProg === program) setActiveProg(null);
    } else reportError(result?.error || "Could not remove instrument");
  }

  if (loading) {
    return (
      <div className="empty-state" data-pysar-reference={pysarReferenceKey({ kind: "bank", id: bank.id })} tabIndex={-1}>
        <div className="empty-card" style={{ borderStyle: "solid" }}>
          <p>Loading bank…</p>
        </div>
      </div>
    );
  }
  if (error) {
    return (
      <div className="empty-state" data-pysar-reference={pysarReferenceKey({ kind: "bank", id: bank.id })} tabIndex={-1}>
        <div className="empty-card" style={{ borderStyle: "solid" }}>
          <h2>Could not load bank</h2>
          <p>{error}</p>
        </div>
      </div>
    );
  }
  if (!details || details.instruments.length === 0) {
    return (
      <div
        style={{ display: "flex", flexDirection: "column", height: "100%" }}
        data-pysar-reference={pysarReferenceKey({ kind: "bank", id: bank.id })}
        tabIndex={-1}
      >
        <div className="toolbar resource-toolbar">
          <Button primary onClick={addInstrument} disabled={!details || instrumentBusy}>
            {instrumentBusy ? "Adding…" : "New instrument"}
          </Button>
          <span className="sep"></span>
          <Button onClick={() => runBankAction(onReplace)} disabled={fileBusy || !onReplace}>Replace</Button>
          <Button onClick={() => runBankAction(onExport)} disabled={fileBusy || !onExport}>Export</Button>
          <Button onClick={() => runBankAction(onRename)} disabled={fileBusy || !!bank.protected || !onRename} title={bank.protected ? "Safe Mode protects this original bank" : "Rename bank"}>Rename</Button>
          <Button className="danger" onClick={() => runBankAction(onDelete)} disabled={fileBusy || !!bank.protected || !onDelete} title={bank.protected ? "Safe Mode protects this original bank" : "Delete bank"}>Delete</Button>
        </div>
        <div className="empty-state">
          <div className="empty-card" style={{ borderStyle: "solid" }}>
            <h2>{bank.name}</h2>
            <p>This bank contains no instruments yet.</p>
          </div>
        </div>
      </div>
    );
  }

  const active = details.instruments.find((i) => i.program === activeProg) || null;
  const showZones = active && !active.isEmpty && active.zones.length > 0;
  const selectedZoneData = active?.zones?.[Math.min(selectedZone, Math.max(0, (active?.zones?.length || 1) - 1))] || null;
  const activeKey = (
    playingNote
    && Number(playingNote.bankId) === Number(bank.id)
    && Number(playingNote.program) === Number(active?.program)
  ) ? Number(playingNote.note) : null;

  return (
    <div className="bank-split" data-pysar-reference={pysarReferenceKey({ kind: "bank", id: bank.id })} tabIndex={-1}>
      <div className="inst-list">
        <div className="toolbar resource-toolbar">
          <Button primary onClick={addInstrument} disabled={instrumentBusy}>
            {instrumentBusy ? "Adding…" : "New instrument"}
          </Button>
          <span className="sep"></span>
          <Button onClick={() => runBankAction(onReplace)} disabled={fileBusy || !onReplace}>Replace</Button>
          <Button onClick={() => runBankAction(onExport)} disabled={fileBusy || !onExport}>Export</Button>
          <Button onClick={() => runBankAction(onRename)} disabled={fileBusy || instrumentBusy || !!bank.protected || !onRename} title={bank.protected ? "Safe Mode protects this original bank" : "Rename bank"}>Rename</Button>
          <Button className="danger" onClick={() => runBankAction(onDelete)} disabled={fileBusy || instrumentBusy || !!bank.protected || !onDelete} title={bank.protected ? "Safe Mode protects this original bank" : "Delete bank"}>Delete</Button>
          <span className="grow"></span>
          <span style={{ fontSize: 11, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
            {details.activeInstrumentCount}/{details.instrumentCount} active · {details.waveCount} waves
          </span>
        </div>
        <div className="table-wrap">
          <table className="tbl" ref={instrumentColumnSizing.tableRef} style={instrumentColumnSizing.tableStyle}>
            <ResizableTableColGroup columns={BANK_INSTRUMENT_COLUMNS} sizing={instrumentColumnSizing} />
            <thead>
              <tr>
                {BANK_INSTRUMENT_COLUMNS.map((column) => (
                  <th key={column.key} className={column.align === "num" ? "num" : ""}>
                    {column.label}
                    <TableColumnResizer columnKey={column.key} label={column.label} sizing={instrumentColumnSizing} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {details.instruments.map((ins) => {
                const isActive = activeProg === ins.program;
                const cls = ["bank-inst-row"];
                if (isActive) cls.push("selected");
                if (ins.isEmpty) cls.push("empty");
                const summary = summariseInstrument(ins);
                return (
                  <tr
                    key={ins.program}
                    className={cls.join(" ")}
                    onClick={() => !ins.isEmpty && setActiveProg(ins.program)}
                    style={{ cursor: ins.isEmpty ? "default" : "pointer" }}
                  >
                    <td className="mono num">{ins.program}</td>
                    <td className="mono">{ins.name || (ins.isEmpty ? "-" : `Program ${ins.program}`)}</td>
                    <td className="num">{ins.isEmpty ? "-" : ins.zoneCount}</td>
                    <td className="mono dim">{summary.span}</td>
                    <td className="mono">{summary.root}</td>
                    <td className="num">{ins.isEmpty ? "-" : ins.waveIndices.length}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <div className="toolbar">
          <span style={{ fontSize: 11, color: "var(--text-tertiary)", marginRight: 8 }}>
            {active ? (active.name || `Program ${active.program}`) : "-"}
          </span>
          <span className="grow"></span>
          {active && !active.isEmpty && (
            <>
              <Button ghost onClick={() => splitKeyRegion(active.program, Math.floor((active.keyLow + active.keyHigh) / 2))}>Split</Button>
              <Button ghost disabled={!!selectedZoneData?.protected} title={selectedZoneData?.protected ? "Safe Mode protects this original zone" : undefined} onClick={() => deleteZone(active.program, selectedZone)}>Delete zone</Button>
              <Button className="danger" disabled={!!active.protected} title={active.protected ? "Safe Mode protects this original instrument" : undefined} onClick={() => removeInstrument(active.program)}>Remove</Button>
            </>
          )}
        </div>

        {showZones ? (
          <div className="bank-inst-detail">
            <ZoneEditor
              zones={active.zones}
              selectedZone={selectedZone}
              onSelectZone={setSelectedZone}
              onOpenWave={
                bank.audioFileId != null && onNavigate
                  ? (waveIndex) => onNavigate({ kind: "wave", archiveId: bank.audioFileId, waveIndex })
                  : null
              }
              onResize={(zoneIdx, newHigh) => {
                window.pysar && window.pysar.call("resize_bank_key_region", bank.id, active.program, zoneIdx, newHigh)
                  .then((r) => {
                    if (r?.ok && r.data) {
                      setDetails(r.data);
                      if (r.dirty) onDirty?.(true);
                      refreshArchiveMetadata(r);
                    } else if (!r?.ok) reportError(r?.error || "Could not resize bank region");
                  })
                  .catch((e) => reportError(String(e)));
              }}
              onSplit={(zoneIdx) => {
                const z = active.zones[zoneIdx];
                if (z) splitKeyRegion(active.program, Math.floor((z.keyLow + z.keyHigh) / 2));
              }}
              onDelete={(zoneIdx) => {
                if (active.zones[zoneIdx]?.protected) reportError("Safe Mode protects this original zone");
                else deleteZone(active.program, zoneIdx);
              }}
              onPlayNote={(key) => onPlayNote?.(active.program, key)}
              activeKey={activeKey}
            />
            <ZoneProperties
              zone={active.zones[Math.min(selectedZone, active.zones.length - 1)]}
              zoneIndex={Math.min(selectedZone, active.zones.length - 1)}
              program={active.program}
              onUpdate={updateZone}
              onOpenWave={
                bank.audioFileId != null && onNavigate
                  ? (waveIndex) => onNavigate({ kind: "wave", archiveId: bank.audioFileId, waveIndex })
                  : null
              }
            />
          </div>
        ) : (
          <div style={{ padding: "40px 24px", color: "var(--text-tertiary)", fontSize: 12, textAlign: "center" }}>
            {active && active.isEmpty
              ? "This program slot is empty."
              : "Pick an instrument on the left to inspect its zones."}
          </div>
        )}
      </div>
    </div>
  );
}

function ZoneEditor({ zones, selectedZone, onSelectZone, onOpenWave, onResize, onSplit, onDelete, onPlayNote, activeKey }) {
  const wrapRef = React.useRef(null);
  const [drag, setDrag] = React.useState(null);
  const [ctxMenu, setCtxMenu] = React.useState(null);
  const safeSel = Math.max(0, Math.min(selectedZone ?? 0, zones.length - 1));

  const REGION_H = 64;
  const EDGE_PX = 6;

  function keyToX(key, width) { return (key / 128) * width; }
  function xToKey(x, width) { return Math.round((x / width) * 128); }

  function handleMouseDown(e, zoneIdx, edge) {
    if (e.button !== 0) return;
    e.preventDefault();
    onSelectZone(zoneIdx);
    if (!edge || !onResize) return;
    const rect = wrapRef.current.getBoundingClientRect();
    setDrag({ zoneIdx, edge, startX: e.clientX, rect });
  }

  React.useEffect(() => {
    if (!drag) return;
    const zone = zones[drag.zoneIdx];
    if (!zone) { setDrag(null); return; }

    function onMove(e) {
      const x = e.clientX - drag.rect.left;
      const key = Math.max(0, Math.min(127, xToKey(x, drag.rect.width)));
      if (drag.edge === "right" && key > zone.keyLow && key !== zone.keyHigh) {
        onResize(drag.zoneIdx, key);
      }
    }
    function onUp() { setDrag(null); }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, [drag, zones]);

  function handleContext(e, zoneIdx) {
    e.preventDefault();
    onSelectZone(zoneIdx);
    setCtxMenu({ x: e.clientX, y: e.clientY, zoneIdx });
  }

  React.useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [ctxMenu]);

  function handleKeyDown(e) {
    if (e.key === "Delete" || e.key === "Backspace") {
      if (onDelete && zones.length > 1) onDelete(safeSel);
    }
    if (e.key === "ArrowLeft" && safeSel > 0) onSelectZone(safeSel - 1);
    if (e.key === "ArrowRight" && safeSel < zones.length - 1) onSelectZone(safeSel + 1);
  }

  return (
    <div className="zone-region-editor" ref={wrapRef} tabIndex={0} onKeyDown={handleKeyDown}>
      <div className="zone-region-canvas-shell">
        <div className="zone-region-glow-layer" aria-hidden="true">
          {zones.map((z, i) => {
            const left = (z.keyLow / 128) * 100;
            const width = ((z.keyHigh - z.keyLow + 1) / 128) * 100;
            return <div key={`glow-${i}`} className="zone-region-glow" style={{ left: left + "%", width: width + "%", background: zoneColor(z.waveIndex) }} />;
          })}
        </div>
        <div className="zone-region-canvas" style={{ height: REGION_H }}>
          {zones.map((z, i) => {
            const color = zoneColor(z.waveIndex);
            const left = (z.keyLow / 128) * 100;
            const width = ((z.keyHigh - z.keyLow + 1) / 128) * 100;
            const active = i === safeSel;
            return (
              <div
                key={i}
                className={"zone-region" + (active ? " active" : "")}
                style={{ left: left + "%", width: width + "%" }}
                onClick={() => onSelectZone(i)}
                onDoubleClick={() => onOpenWave && onOpenWave(z.waveIndex)}
                onContextMenu={(e) => handleContext(e, i)}
                title={onOpenWave ? "Double-click to open this wave" : undefined}
              >
                <div className="zone-region-fill" style={{ background: color, opacity: active ? 1 : 0.6 }} />
                <span className="zone-region-label">
                  {noteName(z.keyLow)}-{noteName(z.keyHigh)}
                </span>
                <span className="zone-region-wave">W{z.waveIndex}</span>
                {onResize && (
                  <div
                    className="zone-edge zone-edge-r"
                    onMouseDown={(e) => handleMouseDown(e, i, "right")}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>

      <PianoStrip
        zones={zones.map((z, i) => ({
          start: z.keyLow,
          end: z.keyHigh,
          color: zoneColor(z.waveIndex),
          active: i === safeSel,
        }))}
        onClickKey={onPlayNote}
        activeKey={activeKey}
      />

      {ctxMenu && (
        <div className="zone-ctx-menu" style={{ left: ctxMenu.x, top: ctxMenu.y }}>
          {onSplit && <button onClick={() => { onSplit(ctxMenu.zoneIdx); setCtxMenu(null); }}>Split region</button>}
          {onDelete && zones.length > 1 && <button onClick={() => { onDelete(ctxMenu.zoneIdx); setCtxMenu(null); }}>Delete</button>}
        </div>
      )}
    </div>
  );
}

function DetailPair({ label, value }) {
  return (
    <span className="zone-detail-pair">
      <span className="zone-detail-label">{label}</span>
      <span className="zone-detail-value mono">{value}</span>
    </span>
  );
}

function PianoStrip({ zones, onClickKey, activeKey = null }) {
  const KEYS = 128;
  const KEY_W = 6;
  const HEIGHT = 84;
  const KEYS_BOTTOM = 66;       // keys end here, leaving room for the zone band.
  const BAR_Y = KEYS_BOTTOM + 2;
  const BAR_H = HEIGHT - BAR_Y; // ~16 px solid colour band underneath the keys.
  const isBlack = (n) => [1, 3, 6, 8, 10].includes(n % 12);
  const whiteKeys = [];
  const whitePositions = new Map();
  for (let key = 0; key < KEYS; key += 1) {
    if (isBlack(key)) continue;
    whitePositions.set(key, whiteKeys.length * KEY_W);
    whiteKeys.push(key);
  }
  const totalWidth = whiteKeys.length * KEY_W;
  const selectKey = (event, key) => {
    event.stopPropagation();
    onClickKey?.(key);
  };
  return (
    <div className="piano-strip" style={{ position: "relative", height: HEIGHT }}>
      <svg
        viewBox={`0 0 ${totalWidth} ${HEIGHT}`}
        style={{ width: "100%", height: "100%", display: "block", cursor: onClickKey ? "pointer" : "default" }}
        preserveAspectRatio="none"
      >
        <defs>
          <filter id="bank-keyboard-color-glow" x="-8%" y="-18%" width="116%" height="150%">
            <feGaussianBlur stdDeviation="3" />
          </filter>
        </defs>
        <g filter="url(#bank-keyboard-color-glow)" opacity="0.72" pointerEvents="none">
          {zones.map((z, i) => {
            const x = (z.start / KEYS) * totalWidth;
            const w = ((z.end - z.start + 1) / KEYS) * totalWidth;
            return (
              <React.Fragment key={`gz${i}`}>
                <rect x={x} y={0} width={w} height={KEYS_BOTTOM} fill={z.color} />
                <rect x={x} y={BAR_Y} width={w} height={BAR_H} fill={z.color} />
              </React.Fragment>
            );
          })}
        </g>
        {/* white keys */}
        {whiteKeys.map((key) => {
          const held = key === activeKey;
          return <rect key={"w" + key} x={whitePositions.get(key)} y={0} width={KEY_W} height={KEYS_BOTTOM} fill={held ? "#e85d4a" : "var(--key-white)"} stroke="var(--key-white-edge)" strokeWidth="0.4" onClick={(event) => selectKey(event, key)} />;
        })}
        {/* black keys */}
        {Array.from({ length: KEYS }, (_, key) => {
          if (!isBlack(key)) return null;
          const previousWhiteX = whitePositions.get(key - 1);
          const held = key === activeKey;
          return <rect key={"b" + key} x={previousWhiteX + KEY_W - KEY_W * 0.32} y={0} width={KEY_W * 0.65} height={KEYS_BOTTOM * 0.62} fill={held ? "#e85d4a" : "var(--key-black)"} onClick={(event) => selectKey(event, key)} />;
        })}
        {/* zone band underneath the keyboard */}
        {zones.map((z, i) => {
          const x = (z.start / KEYS) * totalWidth;
          const w = ((z.end - z.start + 1) / KEYS) * totalWidth;
          return (
            <rect
              key={"z" + i}
              x={x}
              y={BAR_Y}
              width={w}
              height={BAR_H}
              fill={z.color}
              pointerEvents="none"
            />
          );
        })}
      </svg>
    </div>
  );
}

window.BankDetail = BankDetail;
window.PianoStrip = PianoStrip;

function ZoneProperties({ zone, zoneIndex, program, onUpdate, onOpenWave }) {
  if (!zone) return null;
  const commit = (field, value) => onUpdate(program, zoneIndex, { [field]: value });

  return (
    <div className="zone-props">
      <div className="zone-props-grid">
        <label>Wave</label>
        <div className="zone-props-row">
          <input type="number" className="zone-input" defaultValue={zone.waveIndex} min={0}
            onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.waveIndex) commit("waveIndex", v); }}
            onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
          />
          {onOpenWave && <button type="button" className="wave-link" onClick={() => onOpenWave(zone.waveIndex)} title="Go to wave">→</button>}
        </div>

        <label>Original Key</label>
        <div className="zone-props-row">
          <input type="number" className="zone-input" defaultValue={zone.originalKey} min={0} max={127}
            onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.originalKey) commit("originalKey", v); }}
            onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
          />
          <span className="zone-note-label">{noteName(zone.originalKey)}</span>
        </div>

        <label>Volume</label>
        <input type="number" className="zone-input" defaultValue={zone.volume} min={0} max={127}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.volume) commit("volume", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Pan</label>
        <input type="number" className="zone-input" defaultValue={zone.pan} min={0} max={127}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.pan) commit("pan", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Pitch</label>
        <input type="number" className="zone-input" defaultValue={zone.pitch} min={0} max={16} step={0.001}
          onBlur={(e) => { const v = parseFloat(e.target.value); if (!isNaN(v) && v !== zone.pitch) commit("pitch", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Attack</label>
        <input type="number" className="zone-input" defaultValue={zone.attack} min={0} max={127}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.attack) commit("attack", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Decay</label>
        <input type="number" className="zone-input" defaultValue={zone.decay} min={0} max={127}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.decay) commit("decay", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Sustain</label>
        <input type="number" className="zone-input" defaultValue={zone.sustain} min={0} max={127}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.sustain) commit("sustain", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Hold</label>
        <input type="number" className="zone-input" defaultValue={zone.hold} min={0} max={127}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.hold) commit("hold", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Release</label>
        <input type="number" className="zone-input" defaultValue={zone.release} min={0} max={127}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.release) commit("release", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />

        <label>Note Off</label>
        <select className="zone-input" defaultValue={zone.noteOffType}
          onChange={(e) => commit("noteOffType", parseInt(e.target.value))}>
          <option value={0}>Release</option>
          <option value={1}>Ignore</option>
          <option value={2}>Cut</option>
        </select>

        <label>Key Group</label>
        <input type="number" className="zone-input" defaultValue={zone.alternateAssign} min={0} max={255}
          onBlur={(e) => { const v = parseInt(e.target.value); if (!isNaN(v) && v !== zone.alternateAssign) commit("alternateAssign", v); }}
          onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
        />
      </div>
    </div>
  );
}
