const { useState, useEffect, useRef, useMemo, useCallback } = React;

const PYSAR_ACCENTS = {
  all:        { accent: "#5a3fa8", hover: "#7558c9" },
  streams:   { accent: "#3ec19a", hover: "#84d8ba" },
  waves:     { accent: "#f0b132", hover: "#f3c97a" },
  sequences: { accent: "#7c5cff", hover: "#b6a5ff" },
  banks:     { accent: "#3aa4c4", hover: "#7fcadf" },
  groups:    { accent: "#c4cad3", hover: "#d3d8df" },
  players:   { accent: "#5b8def", hover: "#9bb7f6" },
  archives:  { accent: "#ff8c42", hover: "#ffb585" },
  files:     { accent: "#9aa3ad", hover: "#c3c9d1" },
  SEQ:       { accent: "#7c5cff", hover: "#b6a5ff" },
  STRM:      { accent: "#3ec19a", hover: "#84d8ba" },
  WAVE:      { accent: "#f0b132", hover: "#f3c97a" },
};

const PYSAR_VIEW_ICONS = Object.freeze({
  all: "project.png",
  streams: "stream.png",
  waves: "wave_sound.png",
  sequences: "sequence.png",
  banks: "bank.png",
  groups: "group.png",
  players: "player.png",
  archives: "wave_archive.png",
  files: "file.png",
});

const PYSAR_SOUND_ICONS = Object.freeze({
  SEQ: "sequence.png",
  STRM: "stream.png",
  WAVE: "wave_sound.png",
});

function pysarIconForView(view) {
  return PYSAR_VIEW_ICONS[view] || null;
}

function pysarIconForTab(tab) {
  if (!tab) return null;
  if (tab.kind === "view") return pysarIconForView(tab.view);
  if (tab.kind === "sound") return PYSAR_SOUND_ICONS[tab.item?.type] || pysarIconForView("all");
  const detailViews = {
    bank: "banks",
    group: "groups",
    player: "players",
    archive: "archives",
    file: "files",
  };
  return pysarIconForView(detailViews[tab.kind]);
}

function pysarIconForPlayback(item) {
  if (!item) return pysarIconForView("all");
  if (item.kind === "bank_note" || item.type === "BANK") return pysarIconForView("banks");
  if (item.kind === "wave") return pysarIconForView("waves");

  const soundViews = { SEQ: "sequences", STRM: "streams", WAVE: "waves" };
  return pysarIconForView(soundViews[item.type]) || pysarIconForView("all");
}

function normalizePysarReference(reference) {
  if (!reference) return null;
  const kind = String(reference.kind || reference.type || "").toLowerCase();
  if (!kind) return null;
  return {
    ...reference,
    kind,
    id: reference.id == null ? reference.id : Number(reference.id),
    archiveId: reference.archiveId == null ? reference.archiveId : Number(reference.archiveId),
    waveIndex: reference.waveIndex == null ? reference.waveIndex : Number(reference.waveIndex),
    fileIndex: reference.fileIndex == null ? reference.fileIndex : Number(reference.fileIndex),
  };
}

function pysarReferenceKey(reference) {
  const ref = normalizePysarReference(reference);
  if (!ref) return "";
  if (ref.kind === "wave") return `wave:${ref.archiveId}:${ref.waveIndex ?? ref.id}`;
  if (ref.kind === "file" && Number(ref.id) < 0 && ref.fileIndex != null) {
    return `file:${ref.id}:${ref.fileIndex}`;
  }
  return `${ref.kind}:${ref.id}`;
}

let pysarFocusRequestSerial = 0;

// Navigation changes React state before the destination is necessarily in the
// DOM. Retry while an async editor loads, then focus and reveal the exact row
// or editor. This also works when the same reference is clicked twice.
function focusPysarReference(reference) {
  const key = pysarReferenceKey(reference);
  if (!key) return;
  const serial = ++pysarFocusRequestSerial;
  let attempts = 0;
  function focusWhenReady() {
    if (serial !== pysarFocusRequestSerial) return;
    const target = Array.from(document.querySelectorAll("[data-pysar-reference]"))
      .find((node) => node.getAttribute("data-pysar-reference") === key);
    if (!target && attempts++ < 360) {
      window.requestAnimationFrame(focusWhenReady);
      return;
    }
    if (!target) return;
    target.scrollIntoView?.({ block: "center", behavior: "smooth" });
    try { target.focus?.({ preventScroll: true }); } catch (_) { target.focus?.(); }
    target.classList.remove("reference-focus-flash");
    // Force a fresh animation when an already-selected reference is clicked.
    void target.offsetWidth;
    target.classList.add("reference-focus-flash");
    window.setTimeout(() => target.classList.remove("reference-focus-flash"), 700);
  }
  window.requestAnimationFrame(focusWhenReady);
}

function PysarIcon({ name, className = "" }) {
  if (!name) return null;
  return (
    <img
      className={("pysar-icon " + className).trim()}
      src={`icons/${name}`}
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  );
}

function accentVars(key, prefix) {
  const p = PYSAR_ACCENTS[key];
  return {
    [`--${prefix}-accent`]: p ? p.accent : "var(--accent)",
    [`--${prefix}-accent-2`]: p ? p.hover : "var(--accent-hover)",
  };
}

function Toggle({ on, onChange, label }) {
  return (
    <button className={"toggle" + (on ? " on" : "")} onClick={() => onChange(!on)}>
      <span className="sw"></span>
      {label && <span>{label}</span>}
    </button>
  );
}

function Stepper({ value, onChange, min = 0, max = 999, step = 1, suffix }) {
  return (
    <div className="stepper">
      <input
        value={value + (suffix || "")}
        onChange={(e) => {
          const v = parseFloat(e.target.value);
          if (!isNaN(v)) onChange(Math.max(min, Math.min(max, v)));
        }}
      />
      <button onClick={() => onChange(Math.max(min, value - step))}>−</button>
      <button onClick={() => onChange(Math.min(max, value + step))}>+</button>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div className="field">
      <span className="lbl">{label}</span>
      <span className="val">{children}</span>
    </div>
  );
}

function CollapsibleSection({ title, defaultOpen = true, right, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="insp-section">
      <div className={"insp-h" + (open ? "" : " collapsed")} onClick={() => setOpen(!open)}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {title}
        </span>
        {right}
      </div>
      {open && <div className="insp-content">{children}</div>}
    </div>
  );
}

function Button({ children, onClick, primary, ghost, className = "", ...rest }) {
  let cls = "tb-btn";
  if (primary) cls += " primary";
  if (ghost) cls += " ghost";
  cls += " " + className;
  return <button className={cls} onClick={onClick} {...rest}>{children}</button>;
}

const PYSAR_MIN_COLUMN_WIDTH = 44;

function pysarColumnWidth(column) {
  const candidate = column?.width ?? column?.style?.width;
  const width = typeof candidate === "number" ? candidate : Number.parseFloat(candidate);
  return Number.isFinite(width) && width > 0 ? width : null;
}

function pysarResizedColumnWidths(columns, widths, columnKey, requestedWidth, containerWidth, fallbackMinimum) {
  const key = String(columnKey);
  const column = columns.find((candidate) => String(candidate.key) === key);
  if (!column || !Number.isFinite(widths[key])) return { ...widths };

  const configuredMinimum = Number(column.minWidth);
  const minimum = Number.isFinite(configuredMinimum) && configuredMinimum > 0
    ? configuredMinimum
    : fallbackMinimum;
  const currentWidth = widths[key];
  const nextWidth = Math.max(minimum, requestedWidth);
  const result = { ...widths, [key]: nextWidth };

  // Once a table fills its viewport, shrinking one column gives that room to
  // a neighbour. Wider tables still contract normally until overflow is gone.
  const shrink = currentWidth - nextWidth;
  if (shrink <= 0) return result;
  const totalWidth = Object.values(widths).reduce(
    (total, width) => total + (Number.isFinite(width) ? width : 0),
    0,
  );
  const overflow = Math.max(0, totalWidth - Math.max(0, Number(containerWidth) || 0));
  const roomToRedistribute = Math.max(0, shrink - overflow);
  if (roomToRedistribute <= 0) return result;

  const index = columns.findIndex((candidate) => String(candidate.key) === key);
  const neighbour = columns.slice(index + 1).find((candidate) => Number.isFinite(widths[String(candidate.key)]))
    || columns.slice(0, index).reverse().find((candidate) => Number.isFinite(widths[String(candidate.key)]));
  if (neighbour) {
    const neighbourKey = String(neighbour.key);
    result[neighbourKey] = widths[neighbourKey] + roomToRedistribute;
  }
  return result;
}

function useResizableTableColumns(columns, { minimumWidth = PYSAR_MIN_COLUMN_WIDTH } = {}) {
  const tableRef = React.useRef(null);
  const activeResizeRef = React.useRef(null);
  const columnsRef = React.useRef(columns);
  const [widths, setWidths] = React.useState({});
  columnsRef.current = columns;

  const columnKeys = columns.map((column) => String(column.key)).join("\u001f");
  React.useEffect(() => {
    setWidths((current) => {
      const validKeys = new Set(columnsRef.current.map((column) => String(column.key)));
      const next = {};
      let changed = false;
      Object.entries(current).forEach(([key, width]) => {
        if (validKeys.has(key)) next[key] = width;
        else changed = true;
      });
      return changed ? next : current;
    });
  }, [columnKeys]);

  function minimumFor(column) {
    const value = Number(column?.minWidth);
    return Number.isFinite(value) && value > 0 ? value : minimumWidth;
  }

  function measuredWidths() {
    const result = {};
    const headers = tableRef.current?.tHead?.rows?.[0]?.cells || [];
    columnsRef.current.forEach((column, index) => {
      const measured = headers[index]?.getBoundingClientRect?.().width;
      const fallback = pysarColumnWidth(column);
      const value = Number.isFinite(measured) && measured > 0 ? measured : fallback;
      if (value != null) result[String(column.key)] = Math.max(minimumFor(column), value);
    });
    return result;
  }

  function applyWidthsToTable(nextWidths) {
    const table = tableRef.current;
    if (!table) return;
    const colElements = table.querySelectorAll("colgroup > col");
    columnsRef.current.forEach((column, index) => {
      const width = nextWidths[String(column.key)];
      if (colElements[index] && Number.isFinite(width)) colElements[index].style.width = `${width}px`;
    });
    const values = columnsRef.current.map((column) => nextWidths[String(column.key)]);
    if (values.every((width) => Number.isFinite(width))) {
      const total = Math.ceil(values.reduce((sum, width) => sum + width, 0));
      table.style.width = `max(100%, ${total}px)`;
      table.style.minWidth = `${total}px`;
    }
  }

  function stopActiveResize(commit = true) {
    activeResizeRef.current?.finish?.(commit);
  }

  React.useEffect(() => () => stopActiveResize(false), []);

  function beginResize(columnKey, event) {
    if (event.button != null && event.button !== 0) return;
    const key = String(columnKey);
    const column = columnsRef.current.find((candidate) => String(candidate.key) === key);
    if (!column) return;

    event.preventDefault();
    event.stopPropagation();
    stopActiveResize();

    const snapshot = measuredWidths();
    const startWidth = snapshot[key] ?? pysarColumnWidth(column) ?? minimumFor(column);
    const startX = event.clientX;
    const pointerId = event.pointerId;
    const containerWidth = tableRef.current?.parentElement?.clientWidth || 0;
    document.body.classList.add("resizing-column");

    let finished = false;
    const activeResize = { latestWidths: snapshot, finish: null };
    const finish = (commit = true) => {
      if (finished) return;
      finished = true;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      window.removeEventListener("blur", onEnd);
      document.body.classList.remove("resizing-column");
      if (activeResizeRef.current === activeResize) activeResizeRef.current = null;
      if (commit) setWidths(activeResize.latestWidths);
    };
    const onMove = (moveEvent) => {
      if (pointerId != null && moveEvent.pointerId !== pointerId) return;
      const width = startWidth + moveEvent.clientX - startX;
      activeResize.latestWidths = pysarResizedColumnWidths(
        columnsRef.current,
        snapshot,
        key,
        width,
        containerWidth,
        minimumWidth,
      );
      applyWidthsToTable(activeResize.latestWidths);
    };
    const onEnd = (endEvent) => {
      if (pointerId != null && endEvent?.pointerId != null && endEvent.pointerId !== pointerId) return;
      finish(true);
    };

    activeResize.finish = finish;
    activeResizeRef.current = activeResize;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
    window.addEventListener("blur", onEnd);
  }

  function resizeBy(columnKey, amount) {
    const key = String(columnKey);
    const column = columnsRef.current.find((candidate) => String(candidate.key) === key);
    if (!column) return;
    const snapshot = measuredWidths();
    const base = snapshot[key] ?? pysarColumnWidth(column) ?? minimumFor(column);
    const containerWidth = tableRef.current?.parentElement?.clientWidth || 0;
    setWidths(pysarResizedColumnWidths(
      columnsRef.current,
      snapshot,
      key,
      base + amount,
      containerWidth,
      minimumWidth,
    ));
  }

  function widthFor(columnKey) {
    const key = String(columnKey);
    const liveWidth = activeResizeRef.current?.latestWidths?.[key];
    if (liveWidth != null) return liveWidth;
    if (widths[key] != null) return widths[key];
    return pysarColumnWidth(columnsRef.current.find((column) => String(column.key) === key));
  }

  const resolvedWidths = columns.map((column) => widthFor(column.key));
  const allColumnsSized = resolvedWidths.every((width) => Number.isFinite(width));
  const totalWidth = allColumnsSized
    ? Math.ceil(resolvedWidths.reduce((total, width) => total + width, 0))
    : null;

  return {
    tableRef,
    beginResize,
    resizeBy,
    widthFor,
    tableStyle: totalWidth == null ? undefined : {
      width: `max(100%, ${totalWidth}px)`,
      minWidth: totalWidth,
    },
  };
}

function ResizableTableColGroup({ columns, sizing }) {
  return (
    <colgroup>
      {columns.map((column) => {
        const width = sizing.widthFor(column.key);
        return <col key={column.key} style={width == null ? undefined : { width }} />;
      })}
    </colgroup>
  );
}

function TableColumnResizer({ columnKey, label, sizing }) {
  function stopEvent(event) {
    event.preventDefault();
    event.stopPropagation();
  }
  return (
    <span
      className="col-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${label || "column"}`}
      tabIndex={0}
      onPointerDown={(event) => sizing.beginResize(columnKey, event)}
      onClick={stopEvent}
      onDoubleClick={stopEvent}
      onKeyDown={(event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        event.stopPropagation();
        const direction = event.key === "ArrowLeft" ? -1 : 1;
        sizing.resizeBy(columnKey, direction * (event.shiftKey ? 24 : 8));
      }}
    />
  );
}

function Seg({ value, onChange, options }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.value} className={value === o.value ? "on" : ""} style={accentVars(o.accent || o.value, "seg")} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Dropdown({ label, value, options, onChange }) {
  return (
    <div className="dropdown" onClick={() => {
      const idx = options.findIndex((o) => o === value || o.value === value);
      const next = options[(idx + 1) % options.length];
      onChange(next.value !== undefined ? next.value : next);
    }}>
      {label && <span className="lbl">{label}</span>}
      <span className="val">{typeof value === "object" ? value.label : value}</span>
    </div>
  );
}

function Waveform({ playhead = 0.34, accent, samples = null }) {
  const bars = useMemo(() => {
    if (Array.isArray(samples)) {
      return samples.map((value) => Math.max(0, Math.min(1, Number(value) || 0)));
    }
    const arr = [];
    const N = 220;
    for (let i = 0; i < N; i++) {
      const t = i / N;
      const env = Math.sin(t * Math.PI) * 0.7 + 0.3;
      const osc = (Math.sin(t * 47) + Math.sin(t * 13.3) + Math.sin(t * 91)) / 3;
      arr.push(Math.max(0.04, Math.abs(osc) * env));
    }
    return arr;
  }, [samples]);
  return (
    <div className="waveform">
      <svg viewBox="0 0 220 36" preserveAspectRatio="none">
        {bars.map((h, i) => {
          const played = i / bars.length < playhead;
          const x = i + 0.5;
          const y = 18 - h * 16;
          return (
            <rect key={i} x={x} y={y} width={0.7} height={h * 32}
              fill={played ? "var(--accent)" : "var(--text-tertiary)"}
              opacity={played ? 0.9 : 0.55}
            />
          );
        })}
        <line x1={playhead * 220} x2={playhead * 220} y1={2} y2={34} stroke="var(--accent)" strokeWidth="1" />
      </svg>
    </div>
  );
}

Object.assign(window, {
  Toggle,
  Stepper,
  Field,
  CollapsibleSection,
  Button,
  Seg,
  Dropdown,
  Waveform,
  PysarIcon,
  pysarIconForView,
  pysarIconForTab,
  pysarIconForPlayback,
  normalizePysarReference,
  pysarReferenceKey,
  focusPysarReference,
});
