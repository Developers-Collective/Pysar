const { useState: useStateS, useEffect: useEffectS, useMemo: useMemoS } = React;

function pillFor(t) { return <span className={"type-pill type-" + t}>{t}</span>; }

const SoundIcons = {
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
};

const SOUND_COLUMNS = [
  { key: "id", label: "ID", className: "num", sortable: true, width: 64 },
  { key: "name", label: "Name", className: "mono", sortable: true, width: 220, minWidth: 80 },
  { key: "type", label: "Type", width: 80 },
];

const SOUND_TABLE_COLUMNS = [
  { key: "play", label: "Preview", width: 38, minWidth: 32 },
  ...SOUND_COLUMNS,
];

function SoundsScreen({ filter = "ALL", onFilterChange, query, onClearSearch, onOpen, onActivate, onWarm, onVisibleSoundsChange, openId, density, onPlay, playingId, onAddSound, onReplaceSound, onExportSound, selectedSoundId }) {
  const D = window.PYSAR_DATA;
  const [sortBy, setSortBy] = useStateS("id");
  const [sortDir, setSortDir] = useStateS("asc");
  const trimmedQuery = (query || "").trim().toLowerCase();
  const columnSizing = useResizableTableColumns(SOUND_TABLE_COLUMNS);

  // Scroll the selected row into view whenever the selection changes — so
  // jumping in from the file inspector's "Used by" list lands the user on
  // the right row rather than just highlighting it off-screen.
  const tableWrapRef = React.useRef(null);
  React.useEffect(() => {
    if (openId == null || !tableWrapRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const row = Array.from(tableWrapRef.current?.querySelectorAll("tr[data-sound-id]") || [])
        .find((candidate) => candidate.getAttribute("data-sound-id") === String(openId));
      if (!row) return;
      row.scrollIntoView?.({ block: "center", behavior: "smooth" });
      try { row.focus?.({ preventScroll: true }); } catch (_) { row.focus?.(); }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [openId]);

  const rows = useMemoS(() => {
    let r = D.sounds;
    if (filter !== "ALL") r = r.filter((s) => s.type === filter);
    if (trimmedQuery) {
      r = r.filter((s) =>
        String(s.name).toLowerCase().includes(trimmedQuery) ||
        String(s.id).toLowerCase().includes(trimmedQuery)
      );
    }
    r = [...r].sort((a, b) => {
      let av = a[sortBy], bv = b[sortBy];
      if (typeof av === "string") return (sortDir === "asc" ? 1 : -1) * av.localeCompare(bv);
      return (sortDir === "asc" ? 1 : -1) * ((av || 0) - (bv || 0));
    });
    return r;
  }, [D, filter, sortBy, sortDir, trimmedQuery]);

  useEffectS(() => {
    onVisibleSoundsChange?.(rows.map((sound) => sound.id));
  }, [rows, onVisibleSoundsChange]);

  const visibleColumnDefs = SOUND_COLUMNS;
  const replaceSoundId = rows.some((s) => s.id === selectedSoundId) ? selectedSoundId : null;
  const exportSoundId = replaceSoundId;
  const searchHasNoResults = trimmedQuery.length > 0 && rows.length === 0;

  React.useEffect(() => {
    if (!onWarm || filter !== "SEQ" || rows.length === 0) return;
    const timer = window.setTimeout(() => {
      rows.slice(0, 3).forEach((sound) => onWarm(sound));
    }, 150);
    return () => window.clearTimeout(timer);
  }, [onWarm, filter, rows]);

  function clickSort(col) {
    if (sortBy === col) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortBy(col); setSortDir("asc"); }
  }
  function cellValue(s, key, isOpen) {
    if (key === "id") return s.id;
    if (key === "name") {
      return (
        <span className="sound-name-cell">
          <span>{s.name}</span>
          {isOpen && <span className="row-hint">double-click for details</span>}
        </span>
      );
    }
    if (key === "type") return pillFor(s.type);
    return "";
  }
  function Th({ col, children, className = "", label = "" }) {
    return (
      <th className={className}>
        {children}
        <TableColumnResizer columnKey={col} label={label} sizing={columnSizing} />
      </th>
    );
  }
  const arrow = (col) => sortBy === col ? <span className="sort-arrow">{sortDir === "asc" ? "↑" : "↓"}</span> : null;

  return (
    <>
      <div className="toolbar sounds-toolbar">
        <div className="sounds-toolbar-filters">
          <Seg
            value={filter}
            onChange={(value) => onFilterChange && onFilterChange(value)}
            options={[
              { value: "ALL", label: "All", accent: "all" },
              { value: "STRM", label: "Stream", accent: "STRM" },
              { value: "WAVE", label: "Wave", accent: "WAVE" },
              { value: "SEQ", label: "Sequence", accent: "SEQ" },
            ]}
          />
        </div>
        <div className="sounds-toolbar-actions">
          <Button primary onClick={onAddSound}>Add new sound</Button>
          <Button onClick={() => onReplaceSound && onReplaceSound(replaceSoundId)} disabled={replaceSoundId == null}>
            Replace sound
          </Button>
          <Button onClick={() => onExportSound && onExportSound(exportSoundId)} disabled={exportSoundId == null} title="Export selected sound">
            Export
          </Button>
        </div>
      </div>

      <div className="table-wrap" ref={tableWrapRef}>
        <table className="tbl sounds-table" ref={columnSizing.tableRef} style={columnSizing.tableStyle}>
          <ResizableTableColGroup columns={SOUND_TABLE_COLUMNS} sizing={columnSizing} />
          <thead>
            <tr>
              <Th col="play" className="play-col" label="Preview"></Th>
              {visibleColumnDefs.map((col) => (
                <Th key={col.key} col={col.key} className={col.className || ""} label={col.label}>
                  {col.sortable
                    ? <span className="sortable" onClick={() => clickSort(col.key)}>{col.label} {arrow(col.key)}</span>
                    : col.label}
                </Th>
              ))}
            </tr>
          </thead>
          <tbody>
            {searchHasNoResults ? (
              <tr className="sounds-empty-search">
                <td colSpan={visibleColumnDefs.length + 1}>
                  <div>
                    <strong>No sounds match “{query.trim()}”.</strong>
                    <span>Try another search, or clear the current search filter.</span>
                    <Button onClick={onClearSearch}>Clear search</Button>
                  </div>
                </td>
              </tr>
            ) : rows.map((s) => {
              const isOpen = openId === s.id;
              const isPlaying = playingId === s.id;
              return (
                <tr
                  key={s.id}
                  data-sound-id={s.id}
                  data-pysar-reference={pysarReferenceKey({ kind: "sound", id: s.id })}
                  tabIndex={-1}
                  className={isOpen ? "selected" : ""}
                  onMouseEnter={() => onWarm && onWarm(s)}
                  onClick={() => onOpen(s)}
                  onDoubleClick={() => (onActivate || onOpen)(s)}
                >
                  <td className="play-col" onClick={(e) => { e.stopPropagation(); onOpen(s); onPlay(s); }}>
                    <span className={"row-play" + (isPlaying ? " playing" : "")}>
                      {isPlaying ? <SoundIcons.Pause /> : <SoundIcons.Play />}
                    </span>
                  </td>
                  {visibleColumnDefs.map((col) => <td key={col.key} className={col.className || ""}>{cellValue(s, col.key, isOpen)}</td>)}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function StreamSoundDetail({ sound, onPlay, onNavigate, onPlaybackInvalidate, playingId, playheadMs = 0, refreshRevision = 0 }) {
  const D = window.PYSAR_DATA;
  const [details, setDetails] = useStateS(null);
  const [error, setError] = useStateS(null);
  const [sourceRevision, setSourceRevision] = useStateS(0);
  const [sourceBusy, setSourceBusy] = useStateS(false);
  const sourceRequestRef = React.useRef(0);
  const isPlaying = playingId === sound.id;
  const player = D.players.find((item) => item.id === sound.player);
  const group = D.groups.find((item) => item.id === sound.group);
  const externalFile = (D.files || []).find((file) => file.external && file.fileIndex === sound.file) || null;

  useEffectS(() => () => { sourceRequestRef.current += 1; }, []);

  useEffectS(() => {
    let active = true;
    setError(null);
    window.pysar.call("get_strm_sound_details", sound.id).then((result) => {
      if (!active) return;
      if (result?.ok) setDetails(result);
      else setError(result?.error || "Could not load BRSTM details");
    }).catch((reason) => {
      if (active) setError(String(reason));
    });
    return () => { active = false; };
  }, [sound.id, refreshRevision, sourceRevision]);

  async function chooseOriginalGamePath() {
    const requestId = ++sourceRequestRef.current;
    setSourceBusy(true);
    setError(null);
    try {
      const result = await window.pysar.call("choose_original_game_path");
      if (requestId !== sourceRequestRef.current) return;
      if (result?.ok) {
        const applied = await window.pysar.call("set_original_game_path", result.path);
        if (requestId !== sourceRequestRef.current) return;
        if (applied?.ok) {
          onPlaybackInvalidate?.();
          setSourceRevision((value) => value + 1);
        } else {
          setError(applied?.error || "Could not set the original game folder");
        }
      }
      else if (!result?.cancelled) setError(result?.error || "Could not set the original game folder");
    } catch (reason) {
      if (requestId === sourceRequestRef.current) setError(String(reason));
    } finally {
      if (requestId === sourceRequestRef.current) setSourceBusy(false);
    }
  }

  async function clearOriginalGamePath() {
    const requestId = ++sourceRequestRef.current;
    setSourceBusy(true);
    setError(null);
    try {
      const result = await window.pysar.call("clear_original_game_path");
      if (requestId !== sourceRequestRef.current) return;
      if (result?.ok) {
        onPlaybackInvalidate?.();
        setSourceRevision((value) => value + 1);
      }
      else setError(result?.error || "Could not clear the original game folder");
    } catch (reason) {
      if (requestId === sourceRequestRef.current) setError(String(reason));
    } finally {
      if (requestId === sourceRequestRef.current) setSourceBusy(false);
    }
  }

  const durationMs = Number(details?.durationMs || 0);
  const playhead = isPlaying && durationMs > 0 ? Math.max(0, Math.min(1, playheadMs / durationMs)) : 0;
  const externalPath = details?.externalPath ?? sound.externalPath ?? "-";
  const metadataRows = [
    ["Codec", details?.codec || "-"],
    ["Sample rate", details?.sampleRate ? `${Number(details.sampleRate).toLocaleString()} Hz` : "-"],
    ["Channels", details?.channels ?? sound.channels ?? "-"],
    ["Tracks", details?.tracks ?? "-"],
    ["BRSAR track flags", details ? `0x${Number(details.trackFlags || 0).toString(16).toUpperCase()}` : "-"],
    ["Samples", details?.totalSamples ? Number(details.totalSamples).toLocaleString() : "-"],
    ["Duration", durationMs ? formatDurationMs(durationMs) : "-"],
    ["Loop", details?.looped == null ? "-" : (details.looped ? "Enabled" : "Disabled")],
  ];
  if (details?.looped) {
    metadataRows.push(["Loop start", Number(details.loopStart || 0).toLocaleString()]);
    metadataRows.push(["Loop end", Number(details.loopEnd || 0).toLocaleString()]);
  }
  const sourceLabel = details?.resolvedSource === "patch"
    ? "Patch file"
    : details?.resolvedSource === "original-game"
      ? "Original game fallback"
      : "Unavailable";
  const sourceClass = details?.resolvedSource === "patch"
    ? "patch"
    : details?.resolvedSource === "original-game"
      ? "fallback"
      : "missing";
  const mismatchFields = new Set((details?.metadataMismatches || []).map((item) => item.field));

  return (
    <div className="detail" data-pysar-reference={pysarReferenceKey({ kind: "sound", id: sound.id })} tabIndex={-1}>
      <div className="detail-h">
        <div>
          <div className="crumbs">Library / <span>STRM</span> / {sound.id}</div>
          <h1>{sound.name}</h1>
        </div>
        <div className="detail-actions">
          <Button onClick={() => onPlay(sound)}>{isPlaying ? "Pause" : "Preview"}</Button>
        </div>
      </div>

      {(error || details?.metadataError) && (
        <div className="dialog-error" style={{ marginBottom: 12 }}>
          {error || details.metadataError}
        </div>
      )}

      {details?.metadataMismatch && (
        <div className="stream-metadata-warning" role="status">
          <strong>BRSAR metadata does not match the BRSTM being played.</strong>
          <span>
            {(details.metadataMismatches || []).map((item) => {
              const expected = item.field === "File size" ? formatBytes(item.expected) : item.expected;
              const actual = item.field === "File size" ? formatBytes(item.actual) : item.actual;
              return `${item.field}: BRSAR ${expected}, BRSTM ${actual}`;
            }).join(" · ")}
          </span>
        </div>
      )}

      <div className="cards">
        <div className="card full">
          <div className="card-h">
            <h3>Waveform</h3>
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-tertiary)" }}>
              <span>{formatDurationMs(Math.min(playheadMs, durationMs))}</span>
              <span>/</span>
              <span>{durationMs ? formatDurationMs(durationMs) : "-"}</span>
              <span style={{ marginLeft: 12 }}>
                {details?.sampleRate ? `${Math.round(details.sampleRate / 100) / 10} kHz` : "-"} · {details?.channels ? `${details.channels}ch` : "-"} · {details?.codec || "-"}
              </span>
            </div>
          </div>
          <div className="card-body" style={{ padding: 12 }}>
            <Waveform playhead={playhead} samples={details?.waveform || []} />
          </div>
        </div>

        <div className="card">
          <div className="card-h"><h3>Sound</h3></div>
          <div className="card-body">
            <div className="stream-detail-grid">
              <span>ID</span><strong>{sound.id}</strong>
              <span>Type</span><strong>{pillFor("STRM")}</strong>
              <span>Player</span>
              <strong>
                {player && onNavigate
                  ? <button className="inline-reference" onClick={() => onNavigate({ kind: "player", id: player.id })}>{player.name}</button>
                  : (player?.name || sound.player)}
              </strong>
              <span>Group</span>
              <strong>
                {group && onNavigate
                  ? <button className="inline-reference" onClick={() => onNavigate({ kind: "group", id: group.id })}>{group.name}</button>
                  : (group?.name || "-")}
              </strong>
              <span>Volume</span><strong>{sound.volume}</strong>
              <span>Priority</span><strong>{sound.priority}</strong>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-h"><h3>BRSTM metadata</h3></div>
          <div className="card-body">
            <div className="stream-detail-grid">
              {metadataRows.map(([label, value]) => (
                <React.Fragment key={label}>
                  <span>{label}</span><strong>{value}</strong>
                </React.Fragment>
              ))}
            </div>
          </div>
        </div>

        <div className="card full">
          <div className="card-h stream-storage-header">
            <h3>Storage</h3>
            <span className={`stream-source-badge ${sourceClass}`}>{sourceLabel}</span>
          </div>
          <div className="card-body">
            <div className="stream-detail-grid">
              <span>BRSAR path</span>
              <strong className="mono">
                {externalFile && onNavigate
                  ? <button className="inline-reference" onClick={() => onNavigate({ kind: "file", id: externalFile.id, fileIndex: externalFile.fileIndex })}>{externalPath}</button>
                  : externalPath}
              </strong>
              <span>Patch file</span><strong className="mono">{details?.expectedPath || "Not available"}</strong>
              <span>Original game folder</span>
              <strong className="stream-source-setting">
                <span className="mono">{details?.originalGamePath || "Not configured"}</span>
                <span className="stream-source-actions">
                  <Button onClick={chooseOriginalGamePath} disabled={sourceBusy}>
                    {sourceBusy ? "Choosing…" : details?.originalGamePath ? "Change…" : "Choose…"}
                  </Button>
                  {details?.originalGamePath && <Button onClick={clearOriginalGamePath} disabled={sourceBusy}>Clear</Button>}
                </span>
              </strong>
              <span>Fallback file</span><strong className="mono">{details?.fallbackPath || "Not configured"}</strong>
              <span>Resolved file</span><strong className="mono">{details?.resolvedPath || "Not available"}</strong>
              <span>BRSAR file size</span><strong className={mismatchFields.has("File size") ? "metadata-mismatch" : ""}>{formatBytes(details?.fileSize ?? sound.fileSize ?? 0)}</strong>
              <span>BRSTM file size</span><strong className={mismatchFields.has("File size") ? "metadata-mismatch" : ""}>{details?.actualFileSize != null ? formatBytes(details.actualFileSize) : "-"}</strong>
              <span>BRSAR channels</span><strong className={mismatchFields.has("Channels") ? "metadata-mismatch" : ""}>{sound.channels ?? "-"}</strong>
              <span>BRSTM channels</span><strong className={mismatchFields.has("Channels") ? "metadata-mismatch" : ""}>{details?.metadataAvailable ? details.channels : "-"}</strong>
              <span>BRSAR track flags</span><strong className={mismatchFields.has("Track allocation") ? "metadata-mismatch" : ""}>{details ? `0x${Number(details.trackFlags || 0).toString(16).toUpperCase()}` : "-"}</strong>
              <span>BRSTM tracks</span><strong className={mismatchFields.has("Track allocation") ? "metadata-mismatch" : ""}>{details?.metadataAvailable ? details.tracks : "-"}</strong>
            </div>
            <p className="stream-fallback-hint">
              Pysar always uses the patch file first. The original game folder is only checked when that file is missing. You may choose the game's root, Sound/rsar, or the stream folder itself.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

function SoundDetail({ sound, onPlay, onNavigate, playingId, playingSoundId = null, playheadMs = 0, durationMs = 0 }) {
  const D = window.PYSAR_DATA;
  const bank = D.banks.find(b => b.id === sound.bank);
  const group = D.groups.find(g => g.id === sound.group);
  const player = D.players.find(p => p.id === sound.player);
  const isPlaying = playingId === sound.id;
  const isCurrent = playingSoundId === sound.id;
  const totalDurationMs = Math.max(0, Number(durationMs || sound.durationMs) || 0);
  const shownPlayheadMs = isCurrent ? Math.max(0, Math.min(totalDurationMs, Number(playheadMs) || 0)) : 0;
  const waveformPlayhead = totalDurationMs > 0 ? shownPlayheadMs / totalDurationMs : 0;

  // Only references resolved from the archive model belong here. Earlier
  // placeholder SOUND-SET/SCENE rows looked real but had no BRSAR target.
  const uses = [];
  if (bank) uses.push({ kind: "bank", id: bank.id, badge: "BANK", name: bank.name });
  if (player) uses.push({ kind: "player", id: player.id, badge: "PLAYER", name: player.name });
  if (sound.audioFileId != null) {
    const archive = (D.waveArchives || []).find((item) => item.id === sound.audioFileId);
    if (archive) uses.push({ kind: "archive", id: archive.id, badge: "WAR", name: archive.name });
  }
  if (sound.dataFileId != null) {
    const file = (D.files || []).find((item) => item.id === sound.dataFileId);
    if (file) uses.push({ kind: "file", id: file.id, fileIndex: file.fileIndex, badge: file.kind, name: file.label });
  }

  const usedBy = [];
  if (group) usedBy.push({ kind: "group", id: group.id, badge: "GROUP", name: group.name });

  function referenceRow(reference) {
    const clickable = !!onNavigate;
    return (
      <div
        key={`${reference.kind}:${reference.id}`}
        className="ref-row"
        role={clickable ? "button" : undefined}
        tabIndex={clickable ? 0 : undefined}
        onClick={clickable ? () => onNavigate(reference) : undefined}
        onKeyDown={clickable ? (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onNavigate(reference);
          }
        } : undefined}
      >
        <span className={"type-pill type-" + reference.badge}>{reference.badge}</span>
        <span className="name">{reference.name}</span>
      </div>
    );
  }

  return (
    <div className="detail" data-pysar-reference={pysarReferenceKey({ kind: "sound", id: sound.id })} tabIndex={-1}>
      <div className="detail-h">
        <div>
          <h1>{sound.name}</h1>
        </div>
        <div className="detail-actions">
          <Button onClick={() => onPlay(sound)}>
            {isPlaying ? "Pause" : "Preview"}
          </Button>
        </div>
      </div>

      <div className="cards">
        <div className="card full">
          <div className="card-h">
            <h3>Waveform</h3>
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-tertiary)" }}>
              <span>{formatDurationMs(shownPlayheadMs)}</span>
              <span>/</span>
              <span>{totalDurationMs ? formatDurationMs(totalDurationMs) : "-"}</span>
              <span style={{ marginLeft: 12 }}>32 kHz · stereo · ADPCM</span>
            </div>
          </div>
          <div className="card-body" style={{ padding: 12 }}>
            <Waveform playhead={waveformPlayhead} />
          </div>
        </div>

        <div className="card">
          <div className="card-h">
            <h3>Properties</h3>
            <Button ghost style={{ height: 22, padding: "0 6px", fontSize: 11 }}>Edit</Button>
          </div>
          <div className="card-body">
            <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "6px 14px", fontSize: 12 }}>
              <span style={{ color: "var(--text-tertiary)" }}>ID</span><span className="mono">{sound.id}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Type</span><span>{pillFor(sound.type)}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Player</span><span className="mono">{player?.name}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Bank</span><span className="mono">{bank?.name || "-"}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Group</span><span className="mono">{group?.name}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Volume</span><span className="mono">{sound.volume}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Priority</span><span className="mono">{sound.priority}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Pan</span><span className="mono">{sound.pan === 0 ? "centered" : sound.pan}</span>
              <span style={{ color: "var(--text-tertiary)" }}>Pitch</span><span className="mono">{(sound.pitch ?? 1).toFixed(3)}</span>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-h">
            <h3>References</h3>
          </div>
          <div className="card-body">
            <div className="ref-cols">
              <div>
                <div className="ref-col-h">Uses</div>
                {uses.map(referenceRow)}
                {uses.length === 0 && <span className="dim">nothing</span>}
              </div>
              <div>
                <div className="ref-col-h">Used by</div>
                {usedBy.map(referenceRow)}
                {usedBy.length === 0 && <span className="dim">nothing</span>}
              </div>
            </div>
            <div style={{ marginTop: 12, fontSize: 11, color: "var(--text-tertiary)", display: "flex", alignItems: "center", gap: 8 }}>
              Showing direct references for this sound.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

window.SoundsScreen = SoundsScreen;
window.SoundDetail = SoundDetail;
window.StreamSoundDetail = StreamSoundDetail;
window.pillFor = pillFor;
