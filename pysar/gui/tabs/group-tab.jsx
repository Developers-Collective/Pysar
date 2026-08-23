const GROUP_ENTRY_COLUMNS = [
  { key: "selected", label: "Selection", width: 38, minWidth: 32 },
  { key: "file", label: "File", width: 82 },
  { key: "name", label: "Name", minWidth: 80 },
  { key: "kind", label: "Kind", width: 84 },
  { key: "size", label: "Size", width: 112 },
];

function GroupsTab({ onOpen, onNavigate, onDelete, openId, query, safeMode = true, onSafetyChange, onDataRefresh, onDirty, onError }) {
  const D = window.PYSAR_DATA;
  const rows = filterByQuery(D.groups, query);
  const activeGroup = D.groups.find((g) => g.id === openId) || rows[0] || D.groups[0] || null;
  const [newName, setNewName] = React.useState("");
  const [renameValue, setRenameValue] = React.useState(activeGroup?.name || "");
  const [selectedFiles, setSelectedFiles] = React.useState(() => new Set());
  const [dropGroupId, setDropGroupId] = React.useState(null);
  const [bulkTarget, setBulkTarget] = React.useState("");
  const [operationError, setOperationError] = React.useState(null);
  const entryColumnSizing = useResizableTableColumns(GROUP_ENTRY_COLUMNS);

  const GroupIcons = {
    Up: () => (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M8 3.8 4.5 7.3m3.5-3.5 3.5 3.5M8 4v8.2" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
    Down: () => (
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M8 12.2 4.5 8.7m3.5 3.5 3.5-3.5M8 12V3.8" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
  };

  React.useEffect(() => {
    setRenameValue(activeGroup?.name || "");
    setSelectedFiles(new Set());
    setBulkTarget("");
    setOperationError(null);
  }, [activeGroup?.id, activeGroup?.name]);

  async function commit(method, args = [], focusName = null) {
    if (!window.pysar) return null;
    setOperationError(null);
    const result = await window.pysar.call(method, ...args).catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      const message = result?.error || "Group update failed";
      setOperationError(message);
      onError?.(message);
      return null;
    }
    if (result.dirty) onDirty?.(true);
    onSafetyChange?.(result);
    if (result.data) onDataRefresh?.(result.data);
    if (focusName && result.data?.groups) {
      const focused = result.data.groups.find((group) => group.name === focusName);
      if (focused) onOpen?.({ kind: "group", id: focused.id, name: focused.name, item: focused });
    }
    return result;
  }

  function createGroup() {
    const name = newName.trim() || `GROUP_${String(D.groups.length).padStart(4, "0")}`;
    setNewName("");
    commit("create_group", [name], name);
  }

  function renameGroup() {
    if (!activeGroup) return;
    const name = renameValue.trim();
    if (!name || name === activeGroup.name) return;
    commit("rename_group", [activeGroup.id, name], name);
  }

  function moveGroup(group, direction) {
    const order = D.groups.map((g) => g.id);
    const index = order.indexOf(group.id);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= order.length) return;
    if (safeMode && (!group.isNew || !D.groups[nextIndex]?.isNew)) {
      onError?.("Safe Mode keeps original group indexes fixed.");
      return;
    }
    [order[index], order[nextIndex]] = [order[nextIndex], order[index]];
    commit("reorder_groups", [order], group.name);
  }

  async function deleteGroup() {
    if (!activeGroup) return;
    await onDelete?.(activeGroup.id, activeGroup.name);
  }

  function activeSelectedFiles() {
    const activeFileIndexes = new Set((activeGroup?.entries || []).map(entryIdentity));
    return Array.from(selectedFiles).filter((fileIndex) => activeFileIndexes.has(fileIndex));
  }

  function entryIdentity(entry) {
    return Number(entry?.logicalFileIndex ?? entry?.fileIndex);
  }

  function toggleFile(entry, checked) {
    const identity = entryIdentity(entry);
    setSelectedFiles((current) => {
      const next = new Set(current);
      if (checked) next.add(identity);
      else next.delete(identity);
      return next;
    });
  }

  function toggleAll(checked) {
    setSelectedFiles(checked ? new Set((activeGroup?.entries || []).map(entryIdentity)) : new Set());
  }

  function dragFilesFor(entry) {
    const selected = activeSelectedFiles();
    const identity = entryIdentity(entry);
    return selected.includes(identity) ? selected : [identity];
  }

  function beginEntryDrag(event, entry) {
    if (safeMode && !entry.isNew) {
      event.preventDefault();
      return;
    }
    const files = dragFilesFor(entry);
    const identity = entryIdentity(entry);
    if (!selectedFiles.has(identity)) {
      setSelectedFiles(new Set([identity]));
    }
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-pysar-files", JSON.stringify(files));
    event.dataTransfer.setData("text/plain", files.join(","));
  }

  function droppedFiles(event) {
    try {
      const raw = event.dataTransfer.getData("application/x-pysar-files");
      if (raw) return JSON.parse(raw).map((value) => Number(value)).filter(Number.isInteger);
    } catch (_) {}
    return (event.dataTransfer.getData("text/plain") || "")
      .split(",")
      .map((value) => Number(value.trim()))
      .filter(Number.isInteger);
  }

  function dropOnGroup(event, group) {
    event.preventDefault();
    setDropGroupId(null);
    const files = droppedFiles(event);
    if (!files.length) return;
    if (activeGroup && group.id === activeGroup.id) return;
    commit("move_files_to_group", [files, group.id], group.name);
  }

  function moveSelectionToTarget() {
    const files = activeSelectedFiles();
    const target = Number(bulkTarget);
    if (!files.length || !Number.isInteger(target) || !activeGroup || target === activeGroup.id) return;
    commit("move_files_to_group", [files, target], activeGroup.name);
  }

  const groupOptions = D.groups || [];
  const activeEntries = activeGroup?.entries || [];
  const selectedCount = activeSelectedFiles().length;
  const selectedEntries = activeEntries.filter((entry) => selectedFiles.has(entryIdentity(entry)));
  const selectedMoveAllowed = !safeMode || selectedEntries.every((entry) => entry.isNew);
  const allEntriesSelected = activeEntries.length > 0 && selectedCount === activeEntries.length;
  const totalSize = D.groups.reduce((s, g) => s + (g.size || 0), 0);

  function groupEntryTarget(entry) {
    if (entry.fileIndex == null) return null;
    const file = (D.files || []).find((candidate) => candidate.id === entry.fileIndex);
    if (!file) return { kind: "file", id: entry.fileIndex };

    // Open the actual resource editor when the physical group entry has one
    // unambiguous logical owner. Shared files deliberately fall back to the
    // exact Raw Files row instead of choosing an arbitrary sound/bank.
    if (file.kind === "RWAR" && (D.waveArchives || []).some((archive) => archive.id === file.id)) {
      return { kind: "archive", id: file.id };
    }
    const ownerKind = file.kind === "RBNK"
      ? "bank"
      : (["RSEQ", "RWSD"].includes(file.kind) ? "sound" : null);
    const owners = ownerKind ? (file.linked || []).filter((reference) => reference.kind === ownerKind) : [];
    if (owners.length === 1) return { kind: owners[0].kind, id: owners[0].id };
    return { kind: "file", id: file.id, fileIndex: file.fileIndex };
  }

  return (
    <div className="group-workbench">
      {operationError && <div className="seq-operation-error" role="alert">{operationError}</div>}
      <div className="group-layout">
        <div className="group-list">
          <div className="group-list-head">
            <div>
              <div className="group-kicker">Groups</div>
              <div className="group-total">{rows.length}{rows.length !== D.groups.length ? ` of ${D.groups.length}` : ""} groups - {formatBytes(totalSize)}</div>
            </div>
          </div>
          <div className="group-create-row">
            <input
              className="group-new-input"
              value={newName}
              placeholder={`GROUP_${String(D.groups.length).padStart(4, "0")}`}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") createGroup(); }}
            />
            <Button primary onClick={createGroup}>New group</Button>
          </div>
          <div className="group-card-list">
            {rows.map((group) => (
              <div
                key={group.id}
                role="button"
                tabIndex={0}
                className={
                  "group-card" +
                  (activeGroup?.id === group.id ? " selected" : "") +
                  (dropGroupId === group.id ? " drop-target" : "")
                }
                data-pysar-reference={pysarReferenceKey({ kind: "group", id: group.id })}
                onClick={() => onOpen({ kind: "group", id: group.id, name: group.name, item: group })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpen({ kind: "group", id: group.id, name: group.name, item: group });
                  }
                }}
                onDragOver={(event) => {
                  if (!activeGroup || activeGroup.id === group.id) return;
                  event.preventDefault();
                  event.dataTransfer.dropEffect = "move";
                  setDropGroupId(group.id);
                }}
                onDragLeave={() => setDropGroupId((current) => current === group.id ? null : current)}
                onDrop={(event) => dropOnGroup(event, group)}
              >
                <span className="group-card-order">
                  <button
                    type="button"
                    className="group-mini-btn"
                    title="Move group up"
                    disabled={group.id === 0 || (safeMode && (!group.isNew || !D.groups[group.id - 1]?.isNew))}
                    onClick={(event) => { event.stopPropagation(); moveGroup(group, -1); }}
                  >
                    <GroupIcons.Up />
                  </button>
                  <button
                    type="button"
                    className="group-mini-btn"
                    title="Move group down"
                    disabled={group.id === D.groups.length - 1 || (safeMode && (!group.isNew || !D.groups[group.id + 1]?.isNew))}
                    onClick={(event) => { event.stopPropagation(); moveGroup(group, 1); }}
                  >
                    <GroupIcons.Down />
                  </button>
                </span>
                <span className="group-card-main">
                  <span className="group-card-name">{group.name}</span>
                  <span className="group-card-meta">#{group.id} - {group.items} items - {formatBytes(group.size)}</span>
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="group-editor">
          {activeGroup ? (
            <>
              <div className="group-editor-head">
                <div className="group-title-stack">
                  <span className="group-kicker">Selected group</span>
                  <input
                    className="group-name-input"
                    value={renameValue}
                    disabled={!!activeGroup.protected}
                    onChange={(event) => setRenameValue(event.target.value)}
                    onKeyDown={(event) => { if (event.key === "Enter") renameGroup(); }}
                  />
                </div>
                <div className="group-editor-actions">
                  <Button onClick={renameGroup} disabled={!!activeGroup.protected || !renameValue.trim() || renameValue.trim() === activeGroup.name}>
                    Rename
                  </Button>
                  <Button className="danger" onClick={deleteGroup} disabled={!!activeGroup.protected}>
                    Delete
                  </Button>
                </div>
              </div>
              <div className="group-metrics">
                <span>{activeGroup.items} items</span>
                <span>{formatBytes(activeGroup.fileSize)} file</span>
                <span>{formatBytes(activeGroup.audioSize)} audio</span>
                {selectedCount > 0 && <span>{selectedCount} selected</span>}
              </div>
              <div className="group-bulk-bar">
                <span>{selectedCount ? `${selectedCount} selected` : "No selection"}</span>
                <span className="grow"></span>
                <select value={bulkTarget} onChange={(event) => setBulkTarget(event.target.value)}>
                  <option value="">Move selected to...</option>
                  {groupOptions.filter((group) => group.id !== activeGroup.id).map((group) => (
                    <option key={group.id} value={group.id}>{group.name}</option>
                  ))}
                </select>
                <Button onClick={moveSelectionToTarget} disabled={!selectedCount || bulkTarget === "" || !selectedMoveAllowed}>Move selected</Button>
              </div>
              <div className="table-wrap group-entry-wrap">
                <table className="tbl" ref={entryColumnSizing.tableRef} style={entryColumnSizing.tableStyle}>
                  <ResizableTableColGroup columns={GROUP_ENTRY_COLUMNS} sizing={entryColumnSizing} />
                  <thead>
                    <tr>
                      <th>
                        <input
                          type="checkbox"
                          checked={allEntriesSelected}
                          onChange={(event) => toggleAll(event.target.checked)}
                        />
                        <TableColumnResizer columnKey="selected" label="Selection" sizing={entryColumnSizing} />
                      </th>
                      {GROUP_ENTRY_COLUMNS.slice(1).map((column) => (
                        <th key={column.key}>
                          {column.label}
                          <TableColumnResizer columnKey={column.key} label={column.label} sizing={entryColumnSizing} />
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {activeEntries.map((entry) => {
                      const identity = entryIdentity(entry);
                      const selected = selectedFiles.has(identity);
                      const openTarget = groupEntryTarget(entry);
                      return (
                        <tr
                          key={`${activeGroup.id}:${entry.slot}:${entry.fileIndex}`}
                          data-file-id={entry.fileIndex}
                          className={(selected ? "selected" : "") + ((!safeMode || entry.isNew) ? " entry-draggable" : "")}
                          draggable={!safeMode || entry.isNew}
                          onDragStart={(event) => beginEntryDrag(event, entry)}
                          onClick={(event) => {
                            if (event.target.closest("button, select, input")) return;
                            toggleFile(entry, !selected);
                          }}
                          onDoubleClick={(event) => {
                            if (event.target.closest("button, select, input")) return;
                            if (openTarget) onNavigate?.(openTarget);
                          }}
                          title={openTarget ? "Double-click to open this resource" : undefined}
                        >
                          <td>
                            <input
                              type="checkbox"
                              checked={selected}
                              onChange={(event) => toggleFile(entry, event.target.checked)}
                            />
                          </td>
                          <td className="mono">{entry.fileIndex}</td>
                          <td>
                            <div className="group-entry-name">
                              <span className="group-entry-primary">
                                <span className="mono">{entry.label}</span>
                                {selected && openTarget && <span className="row-hint">double-click to open</span>}
                              </span>
                              {entry.linkedText && <span>{entry.linkedText}</span>}
                            </div>
                          </td>
                          <td className="mono dim">{entry.kind}</td>
                          <td className="num">{formatBytes(entry.size)}</td>
                        </tr>
                      );
                    })}
                    {(!activeGroup.entries || activeGroup.entries.length === 0) && (
                      <tr><td colSpan="5" className="dim">Empty group</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <div className="empty-state"><div className="empty-card"><h2>No groups</h2></div></div>
          )}
        </div>
      </div>
    </div>
  );
}

window.GroupsTab = GroupsTab;
