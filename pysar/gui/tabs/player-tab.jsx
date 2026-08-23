function PlayersTab({ onOpen, onRename, onDelete, onClear, openId, query, onDataRefresh, onDirty, onError }) {
  const D = window.PYSAR_DATA;
  const rows = filterByQuery(D.players, query);
  const active = D.players.find((player) => player.id === openId) || null;

  async function call(method, args = []) {
    if (!window.pysar) return null;
    const result = await window.pysar.call(method, ...args)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      if (result?.error !== "Cancelled") {
        const message = result?.error || "Player operation failed";
        if (onError) onError(message);
        else window.pysarAlert(message, { title: "Player operation failed" });
      }
      return result;
    }
    if (result.dirty) onDirty?.(true);
    if (result.data) onDataRefresh?.(result.data);
    return result;
  }

  async function createPlayer() {
    const fallback = `PLAYER_${String(D.players.length).padStart(4, "0")}`;
    const requested = await window.pysarPrompt("New player", fallback, {
      label: "Player name",
      confirmLabel: "Create",
      maxLength: 255,
    });
    if (requested == null || !requested.trim()) return;
    const result = await call("create_player", [requested.trim()]);
    const created = result?.data?.players?.find((player) => player.id === result.playerId);
    if (created) onOpen?.({ kind: "player", id: created.id, name: created.name, item: created });
  }

  async function importPlayer() {
    const result = await call("import_player_dialog");
    const imported = result?.data?.players?.find((player) => player.id === result.playerId);
    if (imported) onOpen?.({ kind: "player", id: imported.id, name: imported.name, item: imported });
  }

  async function replacePlayer() {
    if (!active) return;
    if (!await window.pysarConfirm(`Replace ${active.name}'s metadata from JSON?`, {
      title: "Replace player",
      confirmLabel: "Choose file…",
    })) return;
    await call("replace_player_dialog", [active.id]);
  }

  return (
    <>
      <div className="toolbar">
        <Button primary onClick={createPlayer}>New</Button>
        <Button onClick={importPlayer}>Import</Button>
        <Button disabled={!active} onClick={() => call("export_player_dialog", [active.id])}>Export</Button>
        <Button disabled={!active} onClick={replacePlayer}>Replace</Button>
        <Button
          disabled={!active || !!active?.protected}
          onClick={() => active && onRename?.(active)}
          title={active?.protected ? "Safe Mode protects this original player" : "Rename selected player"}
        >Rename</Button>
        <Button
          disabled={!active || !!active?.protected}
          className="danger"
          onClick={() => active && onDelete?.(active)}
          title={active?.protected ? "Safe Mode protects this original player" : undefined}
        >Delete</Button>
        <span className="grow"></span>
        <span style={{ fontSize: 11, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
          {D.players.length} players · {D.players.reduce((s, p) => s + (p.playableSounds || 0), 0)} playable sounds
        </span>
      </div>
      <GenericTable
        columns={[
          { key: "id", label: "ID", style: { width: 64 }, align: "num", mono: true },
          { key: "name", label: "Name", mono: true },
          { key: "playableSounds", label: "Playable sounds", style: { width: 150 }, align: "num" },
        ]}
        rows={rows}
        onOpen={(r) => onOpen({ kind: "player", id: r.id, name: r.name, item: r })}
        referenceForRow={(r) => ({ kind: "player", id: r.id })}
        openId={openId}
        status={<span>{rows.length}{rows.length !== D.players.length ? ` of ${D.players.length}` : ""} players</span>}
      />
    </>
  );
}

window.PlayersTab = PlayersTab;
