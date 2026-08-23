function BanksTab({ onSelect, onActivate, onRename, onDelete, openId, query, onDataRefresh, onDirty, onError }) {
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

  async function importBank(importFormat) {
    if (!window.pysar || busy) return;
    setBusy(true);
    const result = await window.pysar.call("import_bank_dialog", importFormat)
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
        <Button onClick={createBank} disabled={busy}>New</Button>
        <Button ghost onClick={() => importBank("brbnk")} disabled={busy}>Import BRBNK</Button>
        <Button ghost onClick={() => importBank("sf2")} disabled={busy}>Import SF2</Button>
        <span className="sep"></span>
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
