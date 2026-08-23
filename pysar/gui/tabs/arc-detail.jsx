function ArchiveDetail({
  archive,
  selectedWaveIndex,
  onSelectWave,
  onPlayWave,
  onNavigate,
  onImportWave,
  onExportWave,
  onReplaceWave,
  onDeleteWave,
  refreshRevision = 0,
}) {
  const [details, setDetails] = useStateB(null);
  const [loading, setLoading] = useStateB(true);
  const [error, setError] = useStateB(null);
  const promotedRef = React.useRef(null);

  React.useEffect(() => {
    promotedRef.current = null;
  }, [archive.id]);

  React.useEffect(() => {
    let cancelled = false;
    setDetails(null);
    setError(null);
    setLoading(true);
    if (!window.pysar) {
      setLoading(false);
      return;
    }
    window.pysar.call("get_wave_archive_details", archive.id).then((result) => {
      if (cancelled) return;
      if (result?.ok) setDetails(result.data);
      else setError(result?.error || "Could not load wave archive");
      setLoading(false);
    }).catch((e) => {
      if (cancelled) return;
      setError(String(e));
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [archive.id, refreshRevision]);

  // When something else (e.g. a click on a WAVE_N tile in the bank detail)
  // navigates to a wave inside this archive, that nav initially carries only
  // {archiveId, waveIndex}. Once the BRWAR's been loaded, promote the
  // selection to the full wave metadata so the inspector + table can show it.
  React.useEffect(() => {
    if (!details || !onSelectWave || selectedWaveIndex == null) return;
    const key = `${archive.id}:${selectedWaveIndex}`;
    if (promotedRef.current === key) return;
    const wave = details.waves.find((w) => w.index === selectedWaveIndex);
    if (!wave) return;
    promotedRef.current = key;
    onSelectWave(buildWaveSelection(archive, wave));
  }, [details, selectedWaveIndex, archive.id, onSelectWave]);

  if (loading) {
    return <div className="empty-state" data-pysar-reference={pysarReferenceKey({ kind: "archive", id: archive.id })} tabIndex={-1}><div className="empty-card" style={{ borderStyle: "solid" }}><p>Loading wave archive…</p></div></div>;
  }
  if (error) {
    return (
      <div className="empty-state" data-pysar-reference={pysarReferenceKey({ kind: "archive", id: archive.id })} tabIndex={-1}>
        <div className="empty-card" style={{ borderStyle: "solid" }}>
          <h2>Could not load wave archive</h2>
          <p>{error}</p>
        </div>
      </div>
    );
  }
  if (!details) return null;

  const linkedBanks = archive.linkedBanks || [];
  const rows = (details.waves || []).map((w) => ({ ...w, id: w.index }));
  const selectedWave = rows.find((wave) => wave.index === selectedWaveIndex) || null;

  return (
    <div className="war-detail" data-pysar-reference={pysarReferenceKey({ kind: "archive", id: archive.id })} tabIndex={-1}>
      <div className="war-summary">
        <div className="war-summary-title mono">{details.name}</div>
        <div className="war-summary-meta">
          <span>{details.waveCount} waves</span>
          <span>{formatBytes(details.size)}</span>
          {linkedBanks.length > 0 && (
            <span className="inline-reference-list">
              Linked:{" "}
              {linkedBanks.slice(0, 3).map((name, index) => {
                const bank = (window.PYSAR_DATA.banks || []).find((item) => item.name === name);
                return (
                  <React.Fragment key={`${name}:${index}`}>
                    {index > 0 && ", "}
                    {bank && onNavigate
                      ? <button className="inline-reference" onClick={() => onNavigate({ kind: "bank", id: bank.id })}>{name}</button>
                      : name}
                  </React.Fragment>
                );
              })}
              {linkedBanks.length > 3 ? ` (+${linkedBanks.length - 3})` : ""}
            </span>
          )}
        </div>
      </div>
      <div className="toolbar resource-toolbar war-wave-actions">
        <Button
          primary
          disabled={!onImportWave}
          onClick={() => onImportWave?.(archive.id)}
          title="Append a BRWAV or encoded WAV to this archive"
        >Add</Button>
        <Button
          disabled={!selectedWave || !onExportWave}
          onClick={() => selectedWave && onExportWave?.(archive.id, selectedWave.index)}
          title="Export the selected BRWAV as raw BRWAV or decoded WAV"
        >Export</Button>
        <Button
          disabled={!selectedWave || !onReplaceWave}
          onClick={() => selectedWave && onReplaceWave?.(archive.id, selectedWave.index)}
          title="Replace the selected BRWAV from a BRWAV or WAV file"
        >Replace</Button>
        <Button
          className="danger"
          disabled={!selectedWave || !onDeleteWave || !!selectedWave?.protected}
          onClick={() => selectedWave && onDeleteWave?.(archive.id, selectedWave.index)}
          title={selectedWave?.protected
            ? "Safe Mode protects this original sample"
            : "Delete the selected BRWAV and repair its references"}
        >Delete</Button>
        <span className="grow"></span>
        <span className="war-wave-actions-status mono">
          {selectedWave ? `Selected BRWAV #${selectedWave.index}` : "Select a BRWAV to export or replace"}
        </span>
      </div>
      <GenericTable
        columns={[
          {
            key: "_play",
            label: "",
            style: { width: 42 },
            render: (_, w) => (
              <button
                className="row-play war-play"
                title="Preview raw wave"
                onClick={(e) => {
                  e.stopPropagation();
                  const selection = buildWaveSelection(archive, w);
                  onSelectWave && onSelectWave(selection);
                  onPlayWave && onPlayWave(selection);
                }}
              >
                <SoundIcons.Play />
              </button>
            ),
          },
          { key: "index", label: "#", style: { width: 64 }, align: "num", mono: true },
          {
            key: "encoding", label: "Encoding", style: { width: 100 }, mono: true,
            render: (v) => <span className={"type-pill " + waveEncodingClass(v)}>{v}</span>,
          },
          { key: "channels", label: "Ch", style: { width: 50 }, align: "num" },
          {
            key: "sampleRate", label: "Sample rate", style: { width: 110 }, align: "num", mono: true,
            render: (v) => v.toLocaleString() + " Hz",
          },
          { key: "samples", label: "Samples", style: { width: 110 }, align: "num", mono: true,
            render: (v) => v.toLocaleString() },
          {
            key: "durationMs", label: "Duration", style: { width: 90 }, align: "num", mono: true,
            render: formatDurationMs,
          },
          {
            key: "looped", label: "Is Looped?", style: { width: 110 }, mono: true,
            render: (v) => v ? "Yes" : "No",
          },
          { key: "sizeBytes", label: "Size", style: { width: 100 }, align: "num", render: formatBytes },
        ]}
        rows={rows}
        onOpen={(w) => onSelectWave && onSelectWave(buildWaveSelection(archive, w))}
        referenceForRow={(w) => ({ kind: "wave", archiveId: archive.id, waveIndex: w.index })}
        openId={selectedWaveIndex}
        status={<span>{rows.length} waves</span>}
      />
    </div>
  );
}

function buildWaveSelection(archive, wave) {
  return {
    kind: "wave",
    id: wave.index,
    name: `${archive.name} · #${wave.index}`,
    item: { ...wave, archiveId: archive.id, archiveName: archive.name },
  };
}

window.ArchiveDetail = ArchiveDetail;
