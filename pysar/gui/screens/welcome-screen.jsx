function Welcome({ onOpen, onOpenRecent, onForgetRecent, recent, loading, error }) {
  const [drag, setDrag] = React.useState(false);
  function droppedPath(event) {
    const file = event.dataTransfer?.files?.[0];
    return file?.path || file?.webkitRelativePath || "";
  }
  return (
    <div className="empty-state"
         onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
         onDragLeave={() => setDrag(false)}
         onDrop={(e) => {
           e.preventDefault();
           setDrag(false);
           const path = droppedPath(e);
           if (path) onOpenRecent(path);
           else onOpen();
         }}>
      <div className={"empty-card" + (drag ? " drag" : "")}>
        <h2>Open a BRSAR archive</h2>
        <p>Drag and drop a .brsar file here, or select one from disk.</p>
        <div className="empty-actions">
          <Button primary onClick={onOpen} disabled={loading}>{loading ? "Opening..." : "Open archive..."}</Button>
        </div>
        {error && <p style={{ color: "var(--accent)", marginTop: 10 }}>{error}</p>}
        <div className="recent">
          <div className="recent-title">Recent archives</div>
          {recent.length === 0 && <div className="recent-empty">No recent archives yet.</div>}
          {recent.map((item) => (
            <div key={item.path} className={"recent-item" + (item.exists === false ? " missing" : "")}>
              <button className="recent-open" onClick={() => onOpenRecent(item.path)} disabled={loading || item.exists === false} title={item.path}>
                <span className="recent-lines">
                  <span className="name">{item.name}</span>
                  {}
                  <span className="path">{item.path}</span>
                </span>
                {item.exists === false && <span className="recent-missing">Missing</span>}
              </button>
              <button className="recent-remove" onClick={() => onForgetRecent(item.path)} title="Remove from recents">
                x
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

window.Welcome = Welcome;