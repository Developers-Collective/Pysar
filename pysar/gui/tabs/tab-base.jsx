const { useState: useStateB } = React;

const FILE_KIND_LABEL = {
  RWAR: "Wave archive",
  RWSD: "Wave sound data",
  RBNK: "Bank",
  RSEQ: "Sequence",
  RSTM: "Stream",
  EXT: "External",
  BIN: "Other",
};

function fileKindLabel(kind) {
  return FILE_KIND_LABEL[kind] || kind || "Other";
}

function formatDurationMs(ms) {
  const v = Math.max(0, Number(ms) || 0);
  if (v < 1000) return v + " ms";
  const s = v / 1000;
  if (s < 60) return s.toFixed(2) + " s";
  const m = Math.floor(s / 60);
  const r = (s - m * 60).toFixed(1);
  return `${m}:${r.padStart(4, "0")}`;
}

function waveEncodingClass(encoding) {
  // Reuse existing palette: ADPCM is the most common - pair it with the same
  // orange we use for RWAR; PCM variants pick from the cooler hues.
  if (encoding === "ADPCM") return "type-RWAR";
  if (encoding === "PCM16") return "type-RBNK";
  if (encoding === "PCM8") return "type-RWSD";
  return "type-BIN";
}

function filterByQuery(rows, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((r) =>
    String(r.name ?? "").toLowerCase().includes(q) ||
    String(r.id ?? "").toLowerCase().includes(q)
  );
}

function formatBytes(value) {
  const v = Number(value) || 0;
  if (v >= 1048576) return (v / 1048576).toFixed(2) + " MB";
  if (v >= 1024) return (v / 1024).toFixed(1) + " KB";
  return v + " B";
}

function GenericTable({ columns, rows, onOpen, onActivate, canActivate, openId, status, rowKey, referenceForRow, isRowSelected }) {
  // Scroll the highlighted row into view whenever `openId` changes - this is
  // what makes "click a reference in the inspector" feel like an actual jump.
  const wrapRef = React.useRef(null);
  const columnSizing = useResizableTableColumns(columns);
  React.useEffect(() => {
    if (openId == null || !wrapRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      // Prefer the caller's exact selection predicate. This matters for
      // external files, which all use id=-1 and are distinguished by their
      // FILE-table index.
      const row = wrapRef.current?.querySelector("tr.selected") || Array.from(
        wrapRef.current?.querySelectorAll("tr[data-row-id]") || [],
      ).find((candidate) => candidate.getAttribute("data-row-id") === String(openId));
      if (!row) return;
      row.scrollIntoView?.({ block: "center", behavior: "smooth" });
      try { row.focus?.({ preventScroll: true }); } catch (_) { row.focus?.(); }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [openId]);

  return (
    <>
      <div className="table-wrap" ref={wrapRef}>
        <table className="tbl" ref={columnSizing.tableRef} style={columnSizing.tableStyle}>
          <ResizableTableColGroup columns={columns} sizing={columnSizing} />
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} className={c.align === "num" ? "num" : ""}>
                  {c.label}
                  <TableColumnResizer columnKey={c.key} label={c.label} sizing={columnSizing} />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, index) => {
              const key = rowKey ? rowKey(r, index) : r.id;
              const reference = referenceForRow?.(r, index) || null;
              const selected = isRowSelected ? !!isRowSelected(r, index) : (openId === key || openId === r.id);
              const activatable = !!onActivate && (!canActivate || !!canActivate(r, index));
              return (
              <tr
                key={key}
                data-row-id={r.id}
                data-pysar-reference={reference ? pysarReferenceKey(reference) : undefined}
                tabIndex={-1}
                className={selected ? "selected" : ""}
                onClick={() => onOpen && onOpen(r)}
                onDoubleClick={() => activatable && onActivate(r)}
                title={activatable ? "Double-click to open" : undefined}
              >
                {columns.map((c) => {
                  const content = c.render ? c.render(r[c.key], r) : r[c.key];
                  const showOpenHint = selected && activatable && (c.key === "name" || c.key === "label");
                  return (
                    <td key={c.key} className={[
                      c.align === "num" ? "num" : "",
                      c.mono ? "mono" : "",
                      c.dim ? "dim" : "",
                      c.cellClassName || "",
                    ].filter(Boolean).join(" ")}>
                      {showOpenHint
                        ? <span className="table-open-cell"><span>{content}</span><span className="row-hint">double-click to open</span></span>
                        : content}
                    </td>
                  );
                })}
              </tr>
            );})}
          </tbody>
        </table>
      </div>
      <div className="tbl-status">
        {status}
      </div>
    </>
  );
}
