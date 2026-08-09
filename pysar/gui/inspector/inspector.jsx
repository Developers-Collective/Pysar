function Inspector({ active, onSwitch, item, onNavigateReferrer, onUpdateSound, onRenameBank, onDeleteBank, onUpdateGroup, onUpdatePlayer, onDeleteGroup, onReplaceSound, onExportSound, onReplaceWave, onExportWave }) {
  const archivePart = item?.kind === "wave" ? `:${item.item?.archiveId ?? "archive"}` : "";
  const playerPart = item?.kind === "player"
    ? `:${item.item?.name ?? ""}:${item.item?.playableSounds ?? 0}:${item.item?.heap ?? 0}`
    : "";
  const itemKey = item ? `${item.kind}:${item.id ?? item.name ?? "selected"}${archivePart}${playerPart}` : "none";
  if (!item) {
    return (
      <aside className="inspector">
        <div className="insp-tabs">
          <span className="insp-tab active">Properties</span>
          <span className="insp-tab">References</span>
        </div>
        <div className="insp-body" style={{ display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-tertiary)", fontSize: 12, padding: 24, textAlign: "center" }}>
          Nothing selected. <br/> Pick a sound, bank or player from the table.
        </div>
      </aside>
    );
  }

  return (
    <aside className="inspector">
      <div className="insp-tabs">
        <button className={"insp-tab" + (active === "props" ? " active" : "")} onClick={() => onSwitch("props")}>
          Properties
        </button>
        <button className={"insp-tab" + (active === "refs" ? " active" : "")} onClick={() => onSwitch("refs")}>
          References
        </button>
      </div>
      <div className="insp-body">
        {active === "props" && <PropertiesTab key={`props:${itemKey}`} item={item} onNavigate={onNavigateReferrer} onUpdateSound={onUpdateSound} onRenameBank={onRenameBank} onDeleteBank={onDeleteBank} onUpdateGroup={onUpdateGroup} onUpdatePlayer={onUpdatePlayer} onDeleteGroup={onDeleteGroup} onReplaceSound={onReplaceSound} onExportSound={onExportSound} onReplaceWave={onReplaceWave} onExportWave={onExportWave} />}
        {active === "refs" && <ReferencesTab key={`refs:${itemKey}`} item={item} onNavigateReferrer={onNavigateReferrer} />}
      </div>
    </aside>
  );
}

const NONE_VALUE = "__none__";

function optionLabel(value, fallback = "-") {
  return value == null || value === "" ? fallback : String(value);
}

function TextInput({ value, disabled = false, className = "", maxLength = 255, onChange }) {
  return <input className={className} defaultValue={optionLabel(value, "")} disabled={disabled} maxLength={maxLength}
    onBlur={(event) => {
      if (disabled || !onChange) return;
      const newVal = event.target.value.trim();
      if (newVal && newVal !== String(value || "").trim()) onChange(newVal);
    }}
    onKeyDown={(event) => { if (event.key === "Enter") event.target.blur(); }}
  />;
}

function NumberInput({ value, min, max, step = 1, disabled = false, suffix = "", onChange }) {
  const display = value == null ? "" : Number(value);
  return (
    <input
      className="mono"
      type="number"
      defaultValue={display}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      placeholder={suffix}
      onBlur={(event) => {
        if (disabled || event.target.value === "") return;
        const parsed = Number(event.target.value);
        if (!Number.isFinite(parsed)) {
          event.target.value = value == null ? "" : String(value);
          return;
        }
        const clamped = Math.max(min, Math.min(max, parsed));
        event.target.value = String(clamped);
        if (onChange && clamped !== Number(value)) onChange(clamped);
      }}
    />
  );
}

function ReadOnly({ value, className = "mono" }) {
  return <input className={className} value={optionLabel(value)} disabled readOnly />;
}

function ReadOnlyPill({ children }) {
  return <span className="input-ish" style={{ display: "flex", alignItems: "center" }}>{children}</span>;
}

function RefSelect({ value, options, empty = "-", disabled = false, onChange, onNavigate, referenceKind }) {
  const selected = value == null ? NONE_VALUE : String(value);
  const canNavigate = value != null && !!referenceKind && !!onNavigate;
  return (
    <span className="ref-select-wrap">
      <select value={selected} disabled={disabled} onChange={(event) => {
        if (!onChange) return;
        const val = event.target.value;
        onChange(val === NONE_VALUE ? null : Number(val));
      }}>
        <option value={NONE_VALUE}>{empty}</option>
        {options.map((option) => (
          <option key={String(option.id)} value={String(option.id)}>
            {optionLabel(option.name || option.label)} [{option.id}]
          </option>
        ))}
      </select>
      {canNavigate && (
        <button
          type="button"
          className="ref-jump-btn"
          title="Open referenced item"
          aria-label="Open referenced item"
          onClick={() => onNavigate({ kind: referenceKind, id: Number(value) })}
        >→</button>
      )}
    </span>
  );
}

function formatBytesValue(value) {
  const v = Number(value) || 0;
  if (v >= 1048576) return (v / 1048576).toFixed(2) + " MB";
  if (v >= 1024) return (v / 1024).toFixed(1) + " KB";
  return v + " B";
}

window.Inspector = Inspector;
