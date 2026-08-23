function ArchivesTab({ onOpen, onActivate, onNavigate, onClear, openId, query, onDataRefresh, onDirty, onError }) {
  const D = window.PYSAR_DATA;
  const archives = D.waveArchives || [];
  const rows = filterByQuery(archives, query).filter((r) => {
    if (!query) return true;
    const q = query.trim().toLowerCase();
    if (!q) return true;
    if (String(r.name ?? "").toLowerCase().includes(q)) return true;
    if (String(r.id ?? "").toLowerCase().includes(q)) return true;
    return (r.linkedBanks || []).some((b) => b.toLowerCase().includes(q));
  });
  const active = archives.find((archive) => archive.id === openId) || null;
  const totalWaves = archives.reduce((s, a) => s + (a.waves || 0), 0);
  const totalSize = archives.reduce((s, a) => s + (a.size || 0), 0);
  const [expandedLinkedBanks, setExpandedLinkedBanks] = React.useState(() => new Set());

  function toggleLinkedBanks(archiveId) {
    setExpandedLinkedBanks((current) => {
      const next = new Set(current);
      if (next.has(archiveId)) next.delete(archiveId);
      else next.add(archiveId);
      return next;
    });
  }

  async function call(method, args = []) {
    if (!window.pysar) return null;
    const result = await window.pysar.call(method, ...args)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      if (result?.error !== "Cancelled") {
        const message = result?.error || "Wave archive operation failed";
        if (onError) onError(message);
        else window.pysarAlert(message, { title: "Wave archive operation failed" });
      }
      return result;
    }
    if (result.dirty) onDirty?.(true);
    if (result.data) onDataRefresh?.(result.data);
    return result;
  }

  async function importArchive() {
    const result = await call("import_wave_archive_dialog");
    const imported = result?.data?.waveArchives?.find((archive) => archive.id === result.fileId);
    if (imported) onOpen?.({ kind: "archive", id: imported.id, name: imported.name, item: imported });
  }

  async function replaceArchive() {
    if (!active) return;
    if (!await window.pysarConfirm(`Replace ${active.name} with another BRWAR? Existing references will be validated.`, {
      title: "Replace wave archive",
      confirmLabel: "Choose file…",
    })) return;
    await call("replace_wave_archive_dialog", [active.id]);
  }

  async function deleteArchive() {
    if (!active) return;
    if (!await window.pysarConfirm(`Delete ${active.name}?`, {
      title: "Delete wave archive",
      confirmLabel: "Delete",
      danger: true,
    })) return;
    let result = await window.pysar.call("delete_wave_archive", active.id, false)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (result?.requiresConfirmation) {
      const names = (result.references || []).map((ref) => ref.name).slice(0, 6);
      const extra = (result.references || []).length - names.length;
      const detail = names.join(", ") + (extra > 0 ? ` (+${extra})` : "");
      const confirmed = await window.pysarConfirm(
        `${active.name} is used by ${detail}. Delete it and detach those references? ` +
        "The linked banks/sounds will have no audio archive until repaired.",
        {
          title: "Detach references and delete",
          confirmLabel: "Detach and delete",
          danger: true,
        },
      );
      if (!confirmed) return;
      result = await window.pysar.call("delete_wave_archive", active.id, true)
        .catch((error) => ({ ok: false, error: String(error) }));
    }
    if (!result?.ok) {
      const message = result?.error || "Could not delete wave archive";
      if (onError) onError(message);
      else window.pysarAlert(message, { title: "Could not delete wave archive" });
      return;
    }
    if (result.dirty) onDirty?.(true);
    if (result.data) {
      onDataRefresh?.(result.data);
      const nextArchives = result.data.waveArchives || [];
      const oldIndex = archives.findIndex((archive) => archive.id === active.id);
      const next = nextArchives[Math.min(Math.max(0, oldIndex), nextArchives.length - 1)] || null;
      if (next) onOpen?.({ kind: "archive", id: next.id, name: next.name, item: next });
      else onClear?.();
    }
  }

  return (
    <>
      <div className="toolbar">
        <Button
          primary
          onClick={importArchive}
          title="Add a validated BRWAR as an unreferenced audio-only archive; links are not created automatically"
        >Import BRWAR</Button>
        <Button disabled={!active} onClick={() => call("export_wave_archive_dialog", [active.id])}>Export</Button>
        <Button disabled={!active} onClick={replaceArchive}>Replace</Button>
        <Button
          disabled={!active || !!active?.protected}
          className="danger"
          onClick={deleteArchive}
          title={active?.protected ? "Safe Mode protects this original wave archive" : undefined}
        >Delete</Button>
        <span className="grow"></span>
        <span style={{ fontSize: 11, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
          {archives.length} archives · {totalWaves} waves · {formatBytes(totalSize)}
        </span>
      </div>
      <GenericTable
        columns={[
          { key: "id", label: "ID", style: { width: 64 }, align: "num", mono: true },
          { key: "name", label: "Name", mono: true },
          {
            key: "linkedBanks",
            label: "Linked banks",
            style: { width: 260 },
            mono: true,
            dim: true,
            cellClassName: "linked-banks-cell",
            render: (v, archive) => {
              if (!v || v.length === 0) return "-";
              const expanded = expandedLinkedBanks.has(archive.id);
              return (
                <span className={`linked-banks-table-list${expanded ? " is-expanded" : ""}`}>
                  <span className="linked-banks-table-content">
                    {v.map((name, index) => {
                      const bank = D.banks.find((candidate) => candidate.name === name);
                      return (
                        <React.Fragment key={`${name}:${index}`}>
                          <span className="linked-bank-entry">
                            {bank && onNavigate
                              ? <a
                                className="inline-reference"
                                role="button"
                                tabIndex={0}
                                onClick={(event) => { event.stopPropagation(); onNavigate({ kind: "bank", id: bank.id }); }}
                                onKeyDown={(event) => {
                                  if (event.key !== "Enter" && event.key !== " ") return;
                                  event.preventDefault();
                                  event.stopPropagation();
                                  onNavigate({ kind: "bank", id: bank.id });
                                }}
                              >{name}</a>
                              : name}
                            {index < v.length - 1 && ","}
                          </span>
                          {index < v.length - 1 && " "}
                        </React.Fragment>
                      );
                    })}
                  </span>
                  {v.length > 1 && <button
                    className="linked-banks-toggle"
                    title={expanded ? "Show one line" : "Show all linked banks"}
                    onClick={(event) => { event.stopPropagation(); toggleLinkedBanks(archive.id); }}
                  >{expanded ? "Less" : "All"}</button>}
                </span>
              );
            },
          },
          { key: "waves", label: "Waves", style: { width: 90 }, align: "num" },
          { key: "size", label: "Size", style: { width: 120 }, align: "num", render: formatBytes },
        ]}
        rows={rows}
        onOpen={(r) => onOpen({ kind: "archive", id: r.id, name: r.name, item: r })}
        onActivate={(r) => onActivate?.({ kind: "archive", id: r.id, name: r.name, item: r })}
        referenceForRow={(r) => ({ kind: "archive", id: r.id })}
        openId={openId}
        status={
          archives.length === 0
            ? <span>No wave archives in this BRSAR.</span>
            : <span>{rows.length}{rows.length !== archives.length ? ` of ${archives.length}` : ""} archives</span>
        }
      />
    </>
  );
}

window.ArchivesTab = ArchivesTab;
