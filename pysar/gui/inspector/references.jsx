function refBadgeClass(badge) {
  if (badge === "BANK") return "type-BANK";
  if (badge === "WAR") return "type-WAR";
  if (badge === "PLAYER") return "type-PLAYER";
  if (badge === "GROUP") return "type-GROUP";
  return "type-" + badge;
}

function RefRow({ entry, onClick }) {
  return (
    <div
      className="ref-row"
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={onClick ? (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      } : undefined}
      style={{ cursor: onClick ? "pointer" : "default" }}
    >
      <span className={"type-pill " + refBadgeClass(entry.badge)}>{entry.badge}</span>
      <span className="name">{entry.name}</span>
    </div>
  );
}

function buildReferences(item, D) {
  const uses = [];
  const usedBy = [];
  const wars = D.waveArchives || [];
  const files = D.files || [];

  if (item.kind === "sound") {
    const s = item.item;
    if (s.bank != null) {
      const b = D.banks.find((x) => x.id === s.bank);
      if (b) uses.push({ kind: "bank", id: b.id, name: b.name, badge: "BANK" });
    }
    const pl = D.players.find((x) => x.id === s.player);
    if (pl) uses.push({ kind: "player", id: pl.id, name: pl.name, badge: "PLAYER" });
    const g = D.groups.find((x) => x.id === s.group);
    if (g) usedBy.push({ kind: "group", id: g.id, name: g.name, badge: "GROUP" });
    // WAVE sounds reference an RWAR directly via the resolved audio file id.
    if (s.audioFileId != null) {
      const war = wars.find((a) => a.id === s.audioFileId);
      if (war) uses.push({ kind: "archive", id: war.id, name: war.name, badge: "WAR" });
    }
    // The data file itself is a great direct jump (BRWSD / BRSEQ / BRSTM).
    if (s.dataFileId != null) {
      const file = files.find((f) => f.id === s.dataFileId);
      if (file) uses.push({ kind: "file", id: file.id, fileIndex: file.fileIndex, name: file.label, badge: file.kind });
    } else if (s.file != null) {
      const external = files.find((file) => file.external && file.fileIndex === s.file);
      if (external) {
        uses.push({
          kind: "file",
          id: external.id,
          fileIndex: external.fileIndex,
          name: external.label,
          badge: external.kind,
        });
      }
    }
  } else if (item.kind === "bank") {
    const b = item.item;
    const sounds = D.sounds.filter((s) => s.bank === b.id);
    sounds.forEach((s) => usedBy.push({ kind: "sound", id: s.id, name: s.name, badge: s.type }));
    if (b.audioFileId != null) {
      const war = wars.find((a) => a.id === b.audioFileId);
      if (war) uses.push({ kind: "archive", id: war.id, name: war.name, badge: "WAR" });
    }
    if (b.dataFileId != null) {
      const file = files.find((f) => f.id === b.dataFileId);
      if (file) uses.push({ kind: "file", id: file.id, fileIndex: file.fileIndex, name: file.label, badge: file.kind });
    }
  } else if (item.kind === "player") {
    const p = item.item;
    D.sounds
      .filter((s) => s.player === p.id)
      .forEach((s) => usedBy.push({ kind: "sound", id: s.id, name: s.name, badge: s.type }));
  } else if (item.kind === "group") {
    const g = item.item;
    const seenFiles = new Set();
    for (const entry of g.entries || []) {
      const fileId = Number(entry.fileIndex);
      if (!Number.isInteger(fileId) || seenFiles.has(fileId)) continue;
      seenFiles.add(fileId);
      const file = files.find((candidate) => candidate.id === fileId);
      uses.push({
        kind: "file",
        id: fileId,
        fileIndex: file?.fileIndex,
        name: file?.label || entry.label || `FILE_${fileId}`,
        badge: file?.kind || entry.kind || "FILE",
      });
    }
  } else if (item.kind === "wave") {
    const w = item.item || {};
    if (w.archiveId != null) {
      const archive = (D.waveArchives || []).find((a) => a.id === w.archiveId);
      if (archive) uses.push({ kind: "archive", id: archive.id, name: archive.name, badge: "WAR" });
    }
  } else if (item.kind === "archive") {
    const a = item.item;
    const linkedNames = new Set(a.linkedBanks || []);
    D.banks
      .filter((b) => linkedNames.has(b.name))
      .forEach((b) => usedBy.push({ kind: "bank", id: b.id, name: b.name, badge: "BANK" }));
  } else if (item.kind === "file") {
    // File referrers come from the service. Promote the kind to the actual
    // sound type (SEQ / WAVE / STRM) so the badge colour is meaningful.
    const linked = item.item.linked || [];
    for (const r of linked) {
      if (r.kind === "sound") {
        const s = D.sounds.find((x) => x.id === r.id);
        usedBy.push({ kind: "sound", id: r.id, name: r.name, badge: s ? s.type : "SOUND" });
      } else if (r.kind === "bank") {
        usedBy.push({ kind: "bank", id: r.id, name: r.name, badge: "BANK" });
      }
    }
  }

  return { uses, usedBy };
}

function ReferencesTab({ item, onNavigateReferrer }) {
  const D = window.PYSAR_DATA;
  const { uses, usedBy } = buildReferences(item, D);
  const handleClick = (entry) => onNavigateReferrer && onNavigateReferrer(entry);

  return (
    <>
      {uses.length > 0 && (
        <CollapsibleSection title={`Uses · ${uses.length}`}>
          {uses.map((entry, i) => (
            <RefRow key={`u:${entry.kind}:${entry.id}:${i}`} entry={entry} onClick={() => handleClick(entry)} />
          ))}
        </CollapsibleSection>
      )}
      <CollapsibleSection title={`Used by · ${usedBy.length}`}>
        {usedBy.map((entry, i) => (
          <RefRow key={`b:${entry.kind}:${entry.id}:${i}`} entry={entry} onClick={() => handleClick(entry)} />
        ))}
        {usedBy.length === 0 && <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>nothing</span>}
      </CollapsibleSection>
    </>
  );
}

window.ReferencesTab = ReferencesTab;
