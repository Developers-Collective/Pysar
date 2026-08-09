// PYSAR - virtual keyboard window (detachable, draggable)
const { useState: useStateK, useEffect: useEffectK, useRef: useRefK } = React;

// Map letters to a 2-row chromatic input similar to how DAWs do it.
// Lower row (Z S X D C V G B H N J M ,) -> C, C#, D, D#, E, F, F#, G, G#, A, A#, B, C
// Upper row (Q 2 W 3 E R 5 T 6 Y 7 U I) -> next octave
const KEYMAP = {
  z: 0, s: 1, x: 2, d: 3, c: 4, v: 5, g: 6, b: 7, h: 8, n: 9, j: 10, m: 11, ",": 12,
  q: 12, "2": 13, w: 14, "3": 15, e: 16, r: 17, "5": 18, t: 19, "6": 20, y: 21, "7": 22, u: 23, i: 24,
};
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function noteName(midi) { return NOTE_NAMES[midi % 12] + Math.floor(midi / 12 - 1); }

function VirtualKeyboard({ onClose }) {
  const [pos, setPos] = useStateK({ x: window.innerWidth - 920, y: window.innerHeight - 360 });
  const [octave, setOctave] = useStateK(4);
  const [velocity, setVelocity] = useStateK(96);
  const [sustain, setSustain] = useStateK(false);
  const [recording, setRecording] = useStateK(false);
  const [showHints, setShowHints] = useStateK(true);
  const [held, setHeld] = useStateK(new Set());
  const [midiConnected] = useStateK(true);
  const headerRef = useRefK(null);
  const dragRef = useRefK(null);

  // keyboard input
  useEffectK(() => {
    function onKey(e) {
      if (e.target.tagName === "INPUT") return;
      const k = e.key.toLowerCase();
      const off = KEYMAP[k];
      if (off === undefined) return;
      const midi = octave * 12 + off;
      if (midi < 0 || midi > 127) return;
      e.preventDefault();
      if (e.type === "keydown" && !e.repeat) {
        setHeld((h) => { const n = new Set(h); n.add(midi); return n; });
      } else if (e.type === "keyup") {
        if (sustain) return;
        setHeld((h) => { const n = new Set(h); n.delete(midi); return n; });
      }
    }
    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKey);
    };
  }, [octave, sustain]);

  // drag
  useEffectK(() => {
    const el = headerRef.current;
    if (!el) return;
    function down(e) {
      dragRef.current = { sx: e.clientX, sy: e.clientY, ox: pos.x, oy: pos.y, dragging: true };
      el.classList.add("dragging");
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    }
    function move(e) {
      const d = dragRef.current;
      if (!d) return;
      setPos({ x: d.ox + (e.clientX - d.sx), y: d.oy + (e.clientY - d.sy) });
    }
    function up() {
      dragRef.current = null;
      el.classList.remove("dragging");
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    }
    el.addEventListener("mousedown", down);
    return () => el.removeEventListener("mousedown", down);
  }, [pos]);

  const isBlack = (m) => [1, 3, 6, 8, 10].includes(m % 12);
  const startMidi = (octave - 1) * 12;
  const NUM_OCTAVES = 4;
  const totalKeys = NUM_OCTAVES * 7; // white keys
  const keyW = 28;
  const blackW = 18;
  const whiteH = 116;
  const blackH = 70;

  // build white-key positions
  const whiteKeys = [];
  let wIdx = 0;
  for (let i = 0; i < NUM_OCTAVES * 12; i++) {
    const m = startMidi + i;
    if (!isBlack(m)) {
      whiteKeys.push({ midi: m, x: wIdx * keyW });
      wIdx++;
    }
  }
  // black-key positions
  const blackKeys = [];
  for (let i = 0; i < NUM_OCTAVES * 12; i++) {
    const m = startMidi + i;
    if (isBlack(m)) {
      // place between previous white and this position
      const prevWhite = whiteKeys.find(w => w.midi === m - 1);
      if (prevWhite) blackKeys.push({ midi: m, x: prevWhite.x + keyW - blackW / 2 });
    }
  }

  function press(m) {
    setHeld((h) => { const n = new Set(h); n.add(m); return n; });
  }
  function release(m) {
    if (sustain) return;
    setHeld((h) => { const n = new Set(h); n.delete(m); return n; });
  }

  // keyboard hint reverse map: midi -> letter
  const hintFor = (midi) => {
    const baseMidi = octave * 12;
    const off = midi - baseMidi;
    const entry = Object.entries(KEYMAP).find(([_, v]) => v === off);
    return entry ? entry[0].toUpperCase() : null;
  };

  const totalWidth = whiteKeys.length * keyW;

  return (
    <div className="kbd-window" style={{ left: pos.x, top: pos.y, width: totalWidth + 24 }}>
      <div className="kbd-header" ref={headerRef}>
        <span className="title">Virtual Keyboard</span>
        <span className="meta">· detached · MIDI {midiConnected ? "in" : "off"}</span>
        <span style={{ flex: 1 }}></span>
        <button className="tb-btn ghost" onClick={onClose}>Close</button>
      </div>
      <div className="kbd-controls">
        <div className="group">
          <span className="lbl">Octave</span>
          <Stepper value={octave} onChange={setOctave} min={0} max={9} />
        </div>
        <div className="group" style={{ minWidth: 200 }}>
          <span className="lbl">Velocity</span>
          <input className="slider" type="range" min="1" max="127" value={velocity}
                 onChange={(e) => setVelocity(parseInt(e.target.value))} style={{ width: 120 }} />
          <span className="mono" style={{ fontSize: 11, color: "var(--text-tertiary)", width: 28 }}>{velocity}</span>
        </div>
        <Toggle on={sustain} onChange={setSustain} label="Sustain" />
        <Toggle on={showHints} onChange={setShowHints} label="Key hints" />
        <span style={{ flex: 1 }}></span>
        <div className="group">
          <button className={"tb-btn ghost" + (recording ? " primary" : "")}
                  onClick={() => setRecording(!recording)}
                  style={recording ? { background: "#e74c3c", borderColor: "#e74c3c", color: "#fff" } : {}}>
            {recording ? "Recording…" : "Record"}
          </button>
          <button className="tb-btn ghost">Stop</button>
        </div>
        <div className="group">
          <span className="lbl">Bank</span>
          <span className="mono" style={{ fontSize: 11, color: "var(--text-secondary)" }}>BANK_BGM_FOREST</span>
        </div>
      </div>
      <div className="kbd-piano" style={{ height: whiteH + 24 }}>
        <div className="kbd-keys" style={{ width: totalWidth, height: whiteH, position: "relative" }}>
          {whiteKeys.map((k) => {
            const active = held.has(k.midi);
            return (
              <div key={k.midi}
                   className={"kbd-key white" + (active ? " held" : "")}
                   style={{ left: k.x, width: keyW, height: whiteH }}
                   onMouseDown={() => press(k.midi)}
                   onMouseUp={() => release(k.midi)}
                   onMouseLeave={() => release(k.midi)}>
                <span className="pc">{noteName(k.midi)}</span>
                {showHints && hintFor(k.midi) && <span className="key-hint">{hintFor(k.midi)}</span>}
              </div>
            );
          })}
          {blackKeys.map((k) => {
            const active = held.has(k.midi);
            return (
              <div key={k.midi}
                   className={"kbd-key black" + (active ? " held" : "")}
                   style={{ left: k.x, width: blackW, height: blackH }}
                   onMouseDown={(e) => { e.stopPropagation(); press(k.midi); }}
                   onMouseUp={() => release(k.midi)}
                   onMouseLeave={() => release(k.midi)}>
                <span className="pc">{noteName(k.midi)}</span>
                {showHints && hintFor(k.midi) && <span className="key-hint">{hintFor(k.midi)}</span>}
              </div>
            );
          })}
          {/* octave labels */}
          {Array.from({ length: NUM_OCTAVES }, (_, i) => (
            <div key={i} className="kbd-octlbl" style={{ left: i * 7 * keyW + 6 }}>
              C{octave - 1 + i}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

window.VirtualKeyboard = VirtualKeyboard;
