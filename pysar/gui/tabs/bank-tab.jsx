function BanksTab({ onSelect, onActivate, onReplace, onExport, onRename, onDelete, openId, query, onDataRefresh, onDirty, onError }) {
  const [, setRevision] = React.useState(0);
  const [busy, setBusy] = React.useState(false);
  const D = window.PYSAR_DATA;
  const rows = filterByQuery(D.banks, query);
  const active = D.banks.find((bank) => bank.id === openId) || null;

  function applyResult(result, operation) {
    if (!result?.ok) {
      if (!result?.cancelled) {
        const message = result?.error || `${operation} failed`;
        if (onError) onError(message);
        else window.pysarAlert(message, { title: `${operation} failed` });
      }
      return;
    }
    if (result.dirty) onDirty?.(true);
    if (result.data) {
      window.PYSAR_DATA = result.data;
      onDataRefresh?.(result.data);
      setRevision((value) => value + 1);
    }
    const imported = result.data?.banks?.find((item) => item.id === result.bankId);
    if (imported) onSelect?.({ kind: "bank", id: imported.id, name: imported.name, item: imported });
    if (result.warnings?.length) {
      window.pysarAlert(result.warnings.join("\n"), { title: `${operation} completed with warnings` });
    }
  }

  async function createBank() {
    if (!window.pysar || busy) return;
    const fallback = `BANK_${String(D.banks.length).padStart(4, "0")}`;
    const name = await window.pysarPrompt("New bank", fallback, {
      label: "Bank name",
      confirmLabel: "Create",
      maxLength: 255,
    });
    if (name == null) return;
    setBusy(true);
    const result = await window.pysar.call("create_bank", name.trim() || fallback)
      .catch((error) => ({ ok: false, error: String(error) }));
    setBusy(false);
    applyResult(result, "Create bank");
  }

  async function importBank() {
    if (!window.pysar || busy) return;
    setBusy(true);
    const result = await window.pysar.call("import_bank_dialog")
      .catch((error) => ({ ok: false, error: String(error) }));
    setBusy(false);
    applyResult(result, "Import bank");
  }

  async function runSelected(action) {
    if (!active || busy || !action) return;
    setBusy(true);
    try {
      await action(active);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="toolbar">
        <Button primary onClick={createBank} disabled={busy}>New</Button>
        <Button onClick={importBank} disabled={busy} title="Import a new bank from BRBNK or SF2">Import</Button>
        <Button disabled={!active || busy} onClick={() => runSelected(onReplace)} title="Replace the selected bank from BRBNK or SF2">Replace</Button>
        <Button disabled={!active || busy} onClick={() => runSelected(onExport)} title="Export the selected bank as BRBNK or SF2">Export</Button>
        <Button
          disabled={!active || busy || !!active?.protected}
          onClick={() => runSelected(onRename)}
          title={active?.protected ? "Safe Mode protects this original bank" : undefined}
        >Rename</Button>
        <Button
          className="danger"
          disabled={!active || busy || !!active?.protected}
          onClick={() => runSelected(onDelete)}
          title={active?.protected ? "Safe Mode protects this original bank" : undefined}
        >Delete</Button>
        <span className="grow"></span>
        <span style={{ fontSize: 11, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
          {D.banks.reduce((s, b) => s + b.instruments, 0)} instruments · {D.banks.reduce((s, b) => s + b.waves, 0)} waves
        </span>
      </div>
      <GenericTable
        columns={[
          { key: "id", label: "ID", style: { width: 64 }, align: "num", mono: true },
          { key: "name", label: "Name", mono: true },
          { key: "instruments", label: "Instruments", style: { width: 130 }, align: "num" },
          { key: "waves", label: "Waves", style: { width: 100 }, align: "num" },
          { key: "file", label: "File", style: { width: 280 }, mono: true, dim: true },
        ]}
        rows={rows}
        onOpen={(r) => onSelect?.({ kind: "bank", id: r.id, name: r.name, item: r })}
        onActivate={(r) => onActivate?.({ kind: "bank", id: r.id, name: r.name, item: r })}
        referenceForRow={(r) => ({ kind: "bank", id: r.id })}
        openId={openId}
        status={<span>{rows.length}{rows.length !== D.banks.length ? ` of ${D.banks.length}` : ""} banks</span>}
      />
    </>
  );
}

window.BanksTab = BanksTab;
