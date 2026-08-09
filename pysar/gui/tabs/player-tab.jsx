function PlayersTab({ onOpen, onClear, openId, query, onDataRefresh, onDirty, onError }) {
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

  async function deletePlayer() {
    if (!active) return;
    if (!await window.pysarConfirm(`Delete ${active.name}?`, {
      title: "Delete player",
      confirmLabel: "Delete",
      danger: true,
    })) return;
    let result = await window.pysar.call("delete_player", active.id, null)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (result?.requiresReplacement) {
      const soundCount = (result.references || []).length;
      const confirmed = await window.pysarConfirm(
        `${soundCount} sound${soundCount === 1 ? "" : "s"} use ${active.name}. ` +
        `Delete it and reassign them to ${result.replacementName}?`,
        {
          title: "Reassign sounds and delete",
          confirmLabel: "Reassign and delete",
          danger: true,
        },
      );
      if (!confirmed) return;
      result = await window.pysar.call("delete_player", active.id, result.suggestedReplacement)
        .catch((error) => ({ ok: false, error: String(error) }));
    }
    if (!result?.ok) {
      const message = result?.error || "Could not delete player";
      if (onError) onError(message);
      else window.pysarAlert(message, { title: "Could not delete player" });
      return;
    }
    if (result.dirty) onDirty?.(true);
    if (result.data) {
      onDataRefresh?.(result.data);
      const nextPlayers = result.data.players || [];
      const oldIndex = D.players.findIndex((player) => player.id === active.id);
      const next = nextPlayers[Math.min(Math.max(0, oldIndex), nextPlayers.length - 1)] || null;
      if (next) onOpen?.({ kind: "player", id: next.id, name: next.name, item: next });
      else onClear?.();
    }
  }

  return (
    <>
      <div className="toolbar">
        <Button primary onClick={createPlayer}>New player</Button>
        <Button onClick={importPlayer}>Import…</Button>
        <Button disabled={!active} onClick={() => call("export_player_dialog", [active.id])}>Export…</Button>
        <Button disabled={!active} onClick={replacePlayer}>Replace…</Button>
        <Button
          disabled={!active || !!active?.protected}
          className="danger"
          onClick={deletePlayer}
          title={active?.protected ? "Safe Mode protects this original player" : undefined}
        >Delete…</Button>
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
