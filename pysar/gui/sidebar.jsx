function Sidebar({ active, onPick, archive, collapsed }) {
  const D = window.PYSAR_DATA;
  const sections = [
    {
      label: "Library",
      items: [
        { id: "all", label: "All sounds", short: "All", count: D.sounds.length, accent: "all" },
        { id: "streams", label: "Streams", short: "Str", count: D.sounds.filter(s => s.type === "STRM").length, accent: "streams" },
        { id: "waves", label: "Waves", short: "Wav", count: D.sounds.filter(s => s.type === "WAVE").length, accent: "waves" },
        { id: "sequences", label: "Sequences", short: "Seq", count: D.sounds.filter(s => s.type === "SEQ").length, accent: "sequences" },
      ],
    },
    {
      label: "Organization",
      items: [
        { id: "banks", label: "Banks", short: "Bnk", count: D.banks.length, accent: "banks" },
        { id: "groups", label: "Groups", short: "Grp", count: D.groups.length, accent: "groups" },
        { id: "players", label: "Players", short: "Plr", count: D.players.length, accent: "players" },
      ],
    },
    {
      label: "Resources",
      items: [
        { id: "archives", label: "Wave archives", short: "War", count: D.waveArchives.length, accent: "archives" },
        { id: "files", label: "Raw files", short: "File", count: new Set(D.sounds.map(s => s.file).filter((id) => id != null && id >= 0)).size, accent: "files" },
      ],
    },
  ];

  return (
    <aside className="sidebar">
      {sections.map((sec) => (
        <React.Fragment key={sec.label}>
          <div className="sb-section">{sec.label}</div>
          <div className="sb-list">
            {sec.items.map((it) => {
              const icon = pysarIconForView(it.id);
              return (
                <button
                  key={it.id}
                  className={"sb-item" + (active === it.id ? " active" : "")}
                  style={accentVars(it.accent || it.id, "item")}
                  onClick={() => onPick(it.id)}
                  title={it.label}
                >
                  {icon
                    ? <PysarIcon name={icon} className="sb-icon" />
                    : <span className="short">{it.short || it.label.slice(0, 3)}</span>}
                  <span className="lbl">{it.label}</span>
                  {it.count !== undefined && <span className="count">{it.count}</span>}
                </button>
              );
            })}
          </div>
        </React.Fragment>
      ))}
    </aside>
  );
}

window.Sidebar = Sidebar;
