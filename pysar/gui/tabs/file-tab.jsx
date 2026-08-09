function FilesTab({ query, kindPreset = null, onOpen, onActivate, onNavigate, openId, openFileIndex = null }) {
  const D = window.PYSAR_DATA;
  const allFiles = D.files || [];
  const [kindFilter, setKindFilter] = useStateB(kindPreset || "ALL");
  const [expandedReferences, setExpandedReferences] = React.useState(() => new Set());

  function referenceKey(file) {
    return `${file.id}:${file.kind}:${file.fileIndex ?? ""}:${file.label ?? ""}`;
  }

  function toggleReferences(file) {
    const key = referenceKey(file);
    setExpandedReferences((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function activationTarget(file) {
    if (!file) return null;
    if (file.kind === "RWAR") {
      const archive = (D.waveArchives || []).find((item) => item.id === file.id);
      if (archive) return { kind: "archive", id: archive.id };
    }

    const preferredKind = file.kind === "RBNK"
      ? "bank"
      : (["RSEQ", "RWSD", "RSTM", "EXT"].includes(file.kind) ? "sound" : null);
    const preferred = preferredKind
      ? (file.linked || []).filter((reference) => reference.kind === preferredKind)
      : [];
    if (preferred.length === 1) return preferred[0];

    const linked = (file.linked || []).filter((reference) => (
      ["sound", "bank", "archive", "player", "group"].includes(reference.kind)
    ));
    return linked.length === 1 ? linked[0] : null;
  }

  const counts = useMemoS(() => {
    const c = {};
    for (const f of allFiles) c[f.kind] = (c[f.kind] || 0) + 1;
    return c;
  }, [allFiles]);

  const kindOptions = [{ value: "ALL", label: "All" }];
  for (const k of ["RWAR", "RWSD", "RBNK", "RSEQ", "RSTM", "EXT", "BIN"]) {
    if (counts[k]) kindOptions.push({ value: k, label: `${fileKindLabel(k)} (${counts[k]})` });
  }
  const effectiveKindFilter = kindPreset || kindFilter;
  React.useEffect(() => {
    if (kindPreset) setKindFilter(kindPreset);
  }, [kindPreset]);
  React.useEffect(() => {
    if (!kindPreset && !kindOptions.some((option) => option.value === kindFilter)) setKindFilter("ALL");
  }, [kindPreset, kindFilter, kindOptions.map((option) => option.value).join("|")]);
  React.useEffect(() => {
    if (kindPreset || openId == null || kindFilter === "ALL") return;
    const target = allFiles.find((file) => (
      file.id === openId
      && (openId >= 0 || openFileIndex == null || file.fileIndex === openFileIndex)
    ));
    if (target && target.kind !== kindFilter) setKindFilter("ALL");
  }, [kindPreset, kindFilter, openId, openFileIndex, allFiles]);

  const q = (query || "").trim().toLowerCase();
  const rows = allFiles.filter((f) => {
    if (effectiveKindFilter !== "ALL" && f.kind !== effectiveKindFilter) return false;
    if (!q) return true;
    if (String(f.label ?? "").toLowerCase().includes(q)) return true;
    if (String(f.id ?? "").toLowerCase().includes(q)) return true;
    if (String(f.kind ?? "").toLowerCase().includes(q)) return true;
    if ((f.linked || []).some((r) => r.name.toLowerCase().includes(q))) return true;
    if (f.externalPath && f.externalPath.toLowerCase().includes(q)) return true;
    return false;
  });

  if (allFiles.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-card" style={{ borderStyle: "solid" }}>
          <h2>Raw files</h2>
          <p>This archive doesn't expose any embedded or external files.</p>
        </div>
      </div>
    );
  }

  const totalSize = rows.reduce((s, f) => s + (f.size || 0), 0);

  return (
    <>
      <div className="toolbar">
        {kindPreset
          ? <span className={"type-pill type-" + kindPreset}>{fileKindLabel(kindPreset)}</span>
          : <Seg value={kindFilter} onChange={setKindFilter} options={kindOptions} />
        }
        <span className="grow"></span>
        <span style={{ fontSize: 11, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
          {rows.length} files · {formatBytes(totalSize)}
        </span>
      </div>
      <GenericTable
        columns={[
          {
            key: "id", label: "ID", style: { width: 70 }, align: "num", mono: true,
            render: (v) => v < 0 ? "-" : v,
          },
          {
            key: "kind", label: "Kind", style: { width: 110 }, mono: true,
            render: (v) => <span className={"type-pill type-" + v}>{v}</span>,
          },
          { key: "label", label: "Label", mono: true },
          {
            key: "linked", label: "Used by", style: { width: 220 }, mono: true, dim: true,
            render: (v, r) => {
              if (r.external && r.externalPath) return r.externalPath;
              if (!v || v.length === 0) return "-";
              const expanded = expandedReferences.has(referenceKey(r));
              return (
                <span className={`reference-table-list${expanded ? " is-expanded" : ""}`}>
                  <span className="reference-table-content">
                    {v.map((reference, index) => (
                      <React.Fragment key={`${reference.kind}:${reference.id}:${index}`}>
                        <span className="reference-table-entry">
                          {onNavigate
                            ? <a
                              className="inline-reference"
                              role="button"
                              tabIndex={0}
                              onClick={(event) => { event.stopPropagation(); onNavigate(reference); }}
                              onKeyDown={(event) => {
                                if (event.key !== "Enter" && event.key !== " ") return;
                                event.preventDefault();
                                event.stopPropagation();
                                onNavigate(reference);
                              }}
                            >{reference.name}</a>
                            : reference.name}
                          {index < v.length - 1 && ","}
                        </span>
                        {index < v.length - 1 && " "}
                      </React.Fragment>
                    ))}
                  </span>
                  {v.length > 1 && <button
                    className="reference-table-toggle"
                    title={expanded ? "Show one line" : "Show all references"}
                    onClick={(event) => { event.stopPropagation(); toggleReferences(r); }}
                  >{expanded ? "Less" : "All"}</button>}
                </span>
              );
            },
            cellClassName: "reference-table-cell",
          },
          { key: "size", label: "Size", style: { width: 120 }, align: "num", render: formatBytes },
        ]}
        rows={rows}
        onOpen={onOpen ? (r) => onOpen({ kind: "file", id: r.id, name: r.label, item: r }) : undefined}
        onActivate={(file) => {
          const target = activationTarget(file);
          if (target && onNavigate) onNavigate(target);
          else onActivate?.(file);
        }}
        canActivate={(file) => !!activationTarget(file) || !!onActivate}
        referenceForRow={(r) => ({ kind: "file", id: r.id, fileIndex: r.fileIndex })}
        isRowSelected={(r) => (
          r.id === openId
          && (r.id >= 0 || openFileIndex == null || r.fileIndex === openFileIndex)
        )}
        openId={openId}
        rowKey={(r, index) => `file:${r.id}:${r.kind}:${r.fileIndex ?? index}:${r.label}`}
        status={
          <span>
            {rows.length}{rows.length !== allFiles.length ? ` of ${allFiles.length}` : ""} files
          </span>
        }
      />
    </>
  );
}

window.FilesTab = FilesTab;
