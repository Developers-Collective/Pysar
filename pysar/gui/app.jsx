const { useState: useStateA, useEffect: useEffectA, useMemo: useMemoA, useCallback: useCallbackA } = React;
const ERROR_TOAST_DURATION_MS = 10000;

const ACCENT_PALETTE = {
  royal:      { accent: "#5a3fa8", hover: "#7558c9" },
  terracotta: { accent: "#e85d4a", hover: "#f06c5a" },
  azure:      { accent: "#5b8def", hover: "#71a0f7" },
  iris:       { accent: "#7c5cff", hover: "#9079ff" },
  jade:       { accent: "#3ec19a", hover: "#52d2ac" },
  amber:      { accent: "#f0b132", hover: "#f5c252" },
  graphite:   { accent: "#c4cad3", hover: "#dde1e8" },
};

const APPEARANCE = {
  "theme": "dark",
  "accent": "royal",
  "density": "compact",
  "showInspector": true,
  "sidebarCollapsed": false,
  "showWelcome": false
} ;

const AppNavIcons = {
  Back: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M10.5 3.5 6 8l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  Forward: () => (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M5.5 3.5 10 8l-4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
};

const SOUND_FILTER_BY_NAV = Object.freeze({
  all: "ALL",
  streams: "STRM",
  waves: "WAVE",
  sequences: "SEQ",
});
const SOUND_NAV_BY_FILTER = Object.freeze({
  ALL: "all",
  STRM: "streams",
  WAVE: "waves",
  SEQ: "sequences",
});
const SOUND_NAV_VIEWS = new Set(Object.keys(SOUND_FILTER_BY_NAV));

const PYSAR_IS_MACOS = /mac/i.test(String(
  navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || ""
));

function appShortcut(key, { shift = false, spaced = false } = {}) {
  const normalizedKey = String(key || "").toUpperCase();
  if (PYSAR_IS_MACOS) return `${shift ? "⇧" : ""}⌘${spaced ? " " : ""}${normalizedKey}`;
  return `Ctrl+${shift ? "Shift+" : ""}${normalizedKey}`;
}

function transportNoteName(midi) {
  const note = Math.max(0, Math.min(127, Math.round(Number(midi) || 0)));
  const names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  return names[note % 12] + (Math.floor(note / 12) - 1);
}

function scoreMatch(haystack, needle) {
  if (haystack == null || needle == null) return -1;
  const h = String(haystack).toLowerCase();
  const n = String(needle).toLowerCase();
  if (!n) return -1;
  if (h === n) return 1000;
  if (h.startsWith(n)) return 500 - (h.length - n.length);
  const idx = h.indexOf(n);
  if (idx === -1) return -1;
  return 100 - idx;
}

function bestSearchMatch(query, data) {
  const q = (query || "").trim();
  if (!q || !data) return null;
  const groups = [
    { kind: "sound", navId: "all", items: data.sounds || [], fields: ["name", "id"] },
    { kind: "bank", navId: "banks", items: data.banks || [], fields: ["name", "id"] },
    { kind: "group", navId: "groups", items: data.groups || [], fields: ["name", "id"] },
    { kind: "player", navId: "players", items: data.players || [], fields: ["name", "id"] },
    { kind: "archive", navId: "archives", items: data.waveArchives || [], fields: ["name", "id"] },
    { kind: "file", navId: "files", items: data.files || [], fields: ["label", "kind", "id"] },
  ];
  let best = { score: -1, kind: null, navId: null, item: null };
  for (const g of groups) {
    for (const item of g.items) {
      let score = -1;
      for (const f of g.fields) score = Math.max(score, scoreMatch(item[f], q));
      if (score > best.score) {
        best = { score, kind: g.kind, navId: g.navId, item };
      }
    }
  }
  return best.score >= 0 ? best : null;
}

function refreshedDataItem(kind, current, data) {
  if (!current || !data) return current;
  if (kind === "sound") return (data.sounds || []).find((item) => item.id === current.id) || current;
  if (kind === "bank") return (data.banks || []).find((item) => item.id === current.id) || current;
  if (kind === "group") return (data.groups || []).find((item) => item.id === current.id) || current;
  if (kind === "player") return (data.players || []).find((item) => item.id === current.id) || current;
  if (kind === "archive") return (data.waveArchives || []).find((item) => item.id === current.id) || current;
  if (kind === "file") {
    return (data.files || []).find((item) =>
      current.fileIndex != null
        ? item.fileIndex === current.fileIndex
        : item.id === current.id && item.kind === current.kind
    ) || current;
  }
  return current;
}

function refreshedSelection(selection, data) {
  if (!selection?.item) return selection;
  const item = refreshedDataItem(selection.kind, selection.item, data);
  if (item === selection.item) return selection;
  return {
    ...selection,
    id: item.id,
    name: item.name || item.label || selection.name,
    item,
  };
}

function refreshedTransportSelection(selection, data) {
  if (!selection) return null;
  // Wave-archive samples and virtual bank notes are transport-only objects;
  // their identifying data is already self-contained.
  if (selection.kind === "wave" || selection.kind === "bank_note") return selection;
  const sound = (data?.sounds || []).find((item) => Number(item.id) === Number(selection.id));
  if (!sound) return null;
  return {
    ...sound,
    durationMs: Math.max(0, Number(selection.durationMs) || 0),
    ...(selection.seqVariation ? { seqVariation: selection.seqVariation } : {}),
  };
}

function dataWithSoundPatch(data, soundPatch) {
  if (!data || !soundPatch || soundPatch.id == null) return data;
  const soundId = Number(soundPatch.id);
  const sounds = (data.sounds || []).map((sound) => (
    Number(sound.id) === soundId ? { ...sound, ...soundPatch, id: sound.id } : sound
  ));
  const activeDocumentId = data.activeDocumentId || null;
  return {
    ...data,
    archive: data.archive ? { ...data.archive, dirty: true } : data.archive,
    documents: (data.documents || []).map((document) => (
      document.id === activeDocumentId ? { ...document, dirty: true } : document
    )),
    sounds,
  };
}

function selectedItemReference(selection) {
  if (!selection) return null;
  if (selection.kind === "wave") {
    return {
      kind: "wave",
      archiveId: selection.item?.archiveId,
      waveIndex: selection.item?.waveIndex ?? selection.item?.index ?? selection.id,
    };
  }
  if (selection.kind === "file") {
    return {
      kind: "file",
      id: selection.id,
      fileIndex: selection.item?.fileIndex,
    };
  }
  return { kind: selection.kind, id: selection.id };
}

function AboutDialog({ appMeta, onClose, onError }) {
  const nsmlwDiscordUrl = "https://discord.gg/4s72Nnm";
  const openLink = async (url) => {
    const result = await window.pysar?.call("open_external_url", url)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) onError?.(result?.error || "Could not open the link");
  };
  const openNsmlwDiscord = (event) => {
    event.preventDefault();
    openLink(nsmlwDiscordUrl);
  };
  return (
    <ModalOverlay title="About" width={620} className="about-modal" onClose={onClose}>
      <div className="about-content">
        <div className="about-brand-row">
          <img src="../../resources/logo/pysar.png" className="about-logo" alt="PYSAR" />
          <div>
            <div className="about-name">PYSAR</div>
            <div className="about-version">Version {appMeta.displayVersion || appMeta.version}</div>
          </div>
        </div>
        <p className="about-summary">An editor for Nintendo Wii BRSAR sound archives.</p>
        <div className="about-credits">
          <h3>Credits</h3>
          <p>Made by Ogu99 and Nin0.</p>
          <p>Special thanks to RedStoneMatt for additional technical help, and 0D for extensive testing and bug reports.</p>
        </div>
        <div className="about-community-card">
          <a
            href={nsmlwDiscordUrl}
            className="about-community-logo-link"
            onClick={openNsmlwDiscord}
            title="Open the NSMLW Discord"
            aria-label="Open the NSMLW Discord"
          >
            <img
              src="../../resources/community/nsmlw-logo.png"
              className="about-community-logo"
              alt="New Super Mario Lost Worlds"
            />
          </a>
          <div className="about-community-copy">
            <div className="about-community-eyebrow">Community</div>
            <div className="about-community-title">New Super Mario Lost Worlds</div>
            <p>Used and powered by New Super Mario Lost Worlds!</p>
            <a
              href={nsmlwDiscordUrl}
              className="about-community-link"
              onClick={openNsmlwDiscord}
              title="Open the NSMLW Discord"
            >
              Click here or the logo to join the Discord <span aria-hidden="true">↗</span>
            </a>
          </div>
        </div>
        <p className="about-disclaimer">This is an independent project and not affiliated with or endorsed by Nintendo.</p>
        <div className="dialog-actions">
          <button type="button" className="tb-btn primary" onClick={onClose}>Close</button>
        </div>
      </div>
    </ModalOverlay>
  );
}

function App() {
  window.PYSAR_DATA = window.PYSAR_DATA || { archive: null, activeDocumentId: null, documents: [], sounds: [], banks: [], groups: [], players: [], waveArchives: [], files: [] };
  window.PYSAR_APP = window.PYSAR_APP || { name: "PYSAR - 1.0.0", version: "1.0.0", displayVersion: "1.0.0", phase: "Stable" };
  const D = window.PYSAR_DATA;
  const [tw, setTwState] = useStateA({ ...APPEARANCE });
  const setTweak = useCallbackA((key, value) => setTwState((prev) => ({ ...prev, [key]: value })), []);

  const [archive, setArchive] = useStateA(tw.showWelcome ? null : D.archive);
  const [documents, setDocuments] = useStateA(Array.isArray(D.documents) ? D.documents : []);
  const [activeDocumentId, setActiveDocumentId] = useStateA(D.activeDocumentId || null);
  const activeDocumentIdRef = React.useRef(activeDocumentId);
  activeDocumentIdRef.current = activeDocumentId;
  const archiveWorkspacesRef = React.useRef({});
  const archiveDataByDocumentRef = React.useRef({});
  const currentWorkspaceRef = React.useRef(null);
  const [appMeta, setAppMeta] = useStateA(window.PYSAR_APP);
  const [openErrorToast, setOpenErrorToast] = useStateA(null);
  const [errorToastHovered, setErrorToastHovered] = useStateA(false);
  const errorToastTimingRef = React.useRef({ id: null, remainingMs: ERROR_TOAST_DURATION_MS, startedAt: 0 });
  const openError = openErrorToast?.message || null;
  const setOpenError = useCallbackA((value) => {
    setErrorToastHovered(false);
    setOpenErrorToast((current) => {
      const next = typeof value === "function" ? value(current?.message || null) : value;
      if (next == null || next === "") return null;
      return { message: String(next), id: (current?.id || 0) + 1 };
    });
  }, []);
  const [loadingArchive, setLoadingArchive] = useStateA(false);
  const archiveActivationRef = React.useRef(false);
  const [recentArchives, setRecentArchives] = useStateA([]);
  const [navView, setNavView] = useStateA("all"); // sidebar selection
  const [soundFilter, setSoundFilter] = useStateA("ALL");
  const [tabs, setTabs] = useStateA([{ id: "all", kind: "view", view: "all", title: "All sounds" }]);
  const [activeTab, setActiveTab] = useStateA("all");
  const activeTabRef = React.useRef(activeTab);
  activeTabRef.current = activeTab;
  const [inspectorTab, setInspectorTab] = useStateA("props");
  const [selectedItem, setSelectedItem] = useStateA(null);
  const [history, setHistory] = useStateA([{ tabId: "all", navView: "all", soundFilter: "ALL", selectedItem: null }]);
  const [historyIndex, setHistoryIndex] = useStateA(0);
  const [draggingTabId, setDraggingTabId] = useStateA(null);
  const [dataRevision, setDataRevision] = useStateA(0);
  const [bankContentRevision, setBankContentRevision] = useStateA(0);

  // dirty / unsaved state
  const [dirty, setDirty] = useStateA(false);
  const [safeMode, setSafeMode] = useStateA(D.archive?.safeMode !== false);
  const [showAddSound, setShowAddSound] = useStateA(false);
  const [addSoundType, setAddSoundType] = useStateA("WAVE");
  const [replaceSoundId, setReplaceSoundId] = useStateA(null);
  const [replaceWaveTarget, setReplaceWaveTarget] = useStateA(null);
  const [unsavedAction, setUnsavedAction] = useStateA(null);
  const [unsavedBusy, setUnsavedBusy] = useStateA(false);
  const [pendingCloseDocumentId, setPendingCloseDocumentId] = useStateA(null);
  const [windowCloseInfo, setWindowCloseInfo] = useStateA(null);
  const [dumpOptionsOpen, setDumpOptionsOpen] = useStateA(false);
  const [dumpStatus, setDumpStatus] = useStateA(null);
  const [menuOpen, setMenuOpen] = useStateA(null);
  const [showAbout, setShowAbout] = useStateA(false);
  const bankMutationRef = React.useRef(false);

  // playback state
  const [playingId, setPlayingId] = useStateA(null);
  const [playingSound, setPlayingSound] = useStateA(null);
  const [isPlaying, setIsPlaying] = useStateA(false);
  const isPlayingRef = React.useRef(isPlaying);
  isPlayingRef.current = isPlaying;
  // The playhead changes up to twenty times per second.  Keep it outside the
  // root App state so a clock tick does not reconcile the complete archive UI.
  const playheadMsRef = React.useRef(window.PysarPlayheadStore.getSnapshot());
  const setPlayheadMs = useCallbackA((nextValue) => {
    const next = Math.max(0, Number(nextValue) || 0);
    playheadMsRef.current = next;
    window.PysarPlayheadStore.set(next);
  }, []);
  const [durationMs, setDurationMs] = useStateA(0);
  const [volume, setVolume] = useStateA(0.9);
  const [seqVariationBySound, setSeqVariationBySound] = useStateA({});
  const [seqVariationsBySound, setSeqVariationsBySound] = useStateA({});
  const [seqPlaybackBySound, setSeqPlaybackBySound] = useStateA({});
  const [seqVariationRevision, setSeqVariationRevision] = useStateA(0);
  const [seqEditorSourceBySound, setSeqEditorSourceBySound] = useStateA({});
  const [strmPlaybackBySound, setStrmPlaybackBySound] = useStateA({});
  const [soundListAutoPlayEnabled, setSoundListAutoPlayEnabled] = useStateA(false);
  const audioRef = React.useRef(null);
  const playingSoundRef = React.useRef(null);
  const audioBaseRef = React.useRef(0);
  const playheadTimerRef = React.useRef(null);
  const loopingFetchAbortRef = React.useRef(null);
  const playRequestRef = React.useRef(0);
  const durationRequestRef = React.useRef(0);
  const durationTargetRef = React.useRef(null);
  const strmPlaybackBySoundRef = React.useRef({});
  const strmPlaybackLoadsRef = React.useRef(new Set());
  const strmPlaybackRevisionRef = React.useRef(0);
  const strmTrackTransitionRef = React.useRef(null);
  const soundListAutoPlayEnabledRef = React.useRef(false);
  const visibleSoundIdsRef = React.useRef([]);
  const seqVariationsBySoundRef = React.useRef({});
  const seqPlaybackBySoundRef = React.useRef({});
  const seqVariationLoadsRef = React.useRef(new Set());
  const seqVariationRevisionRef = React.useRef(0);

  const currentWorkspace = {
    documentId: activeDocumentId,
    tabs,
    activeTab,
    navView,
    soundFilter,
    selectedItem,
    inspectorTab,
    history,
    historyIndex,
    seqVariationBySound,
    seqVariationsBySound,
    seqPlaybackBySound,
    seqEditorSourceBySound,
    strmPlaybackBySound,
    transportSound: playingSound,
    transportDurationMs: durationMs,
    transportPlayheadMs: playheadMsRef.current,
  };
  currentWorkspaceRef.current = currentWorkspace;
  // Keep every archive's navigation state current instead of relying on a
  // single snapshot taken at switch/close time. This also covers rapid close
  // actions immediately after selecting a detail tab.
  if (activeDocumentId) {
    archiveWorkspacesRef.current[activeDocumentId] = currentWorkspace;
  }

  const rememberVisibleSounds = useCallbackA((soundIds) => {
    visibleSoundIdsRef.current = Array.isArray(soundIds) ? soundIds : [];
  }, []);

  const rememberSequenceEditorSource = useCallbackA((soundId, sourceText) => {
    const id = Number(soundId);
    if (!Number.isInteger(id) || id < 0) return;
    setSeqEditorSourceBySound((current) => {
      const next = { ...current };
      if (typeof sourceText === "string") next[id] = sourceText;
      else delete next[id];
      return next;
    });
  }, []);

  const loadSequenceVariations = useCallbackA((soundId) => {
    const id = Number(soundId);
    if (!Number.isInteger(id) || id < 0 || !window.pysar) return;
    if (Object.prototype.hasOwnProperty.call(seqVariationsBySoundRef.current, id)) return;
    if (seqVariationLoadsRef.current.has(id)) return;

    const revision = seqVariationRevisionRef.current;
    seqVariationLoadsRef.current.add(id);
    window.pysar.call("get_sequence_variations", id).then((result) => {
      if (revision !== seqVariationRevisionRef.current) return;
      const variations = result?.ok && Array.isArray(result.data?.variations)
        ? result.data.variations
        : [];
      const next = { ...seqVariationsBySoundRef.current, [id]: variations };
      seqVariationsBySoundRef.current = next;
      setSeqVariationsBySound(next);
    }).catch(() => {
      if (revision !== seqVariationRevisionRef.current) return;
      const next = { ...seqVariationsBySoundRef.current, [id]: [] };
      seqVariationsBySoundRef.current = next;
      setSeqVariationsBySound(next);
    }).finally(() => {
      if (revision === seqVariationRevisionRef.current) {
        seqVariationLoadsRef.current.delete(id);
      }
    });
  }, [seqVariationRevision]);

  useEffectA(() => {
    if (playingSound?.type === "SEQ") {
      loadSequenceVariations(playingSound.id);
    }
  }, [playingSound?.id, playingSound?.type, loadSequenceVariations]);

  // virtual keyboard
  const [kbdOpen, setKbdOpen] = useStateA(false);
  const [sidebarWidth, setSidebarWidth] = useStateA(232);
  const [inspectorWidth, setInspectorWidth] = useStateA(312);

  // top-bar search
  const [searchQuery, setSearchQuery] = useStateA("");
  const searchInputRef = React.useRef(null);

  useEffectA(() => {
    // Keep errors on the welcome screen visible. Workspace errors dismiss
    // after a readable interval, preserving the remaining time while the
    // pointer is over the toast so its text can be selected and copied.
    const timing = errorToastTimingRef.current;
    if (!archive || !openErrorToast) {
      timing.id = null;
      timing.remainingMs = ERROR_TOAST_DURATION_MS;
      timing.startedAt = 0;
      return undefined;
    }
    const shownId = openErrorToast.id;
    if (timing.id !== shownId) {
      timing.id = shownId;
      timing.remainingMs = ERROR_TOAST_DURATION_MS;
      timing.startedAt = 0;
    }
    if (errorToastHovered) return undefined;

    timing.startedAt = Date.now();
    const timeout = window.setTimeout(() => {
      timing.remainingMs = 0;
      timing.startedAt = 0;
      setOpenErrorToast((current) => current?.id === shownId ? null : current);
    }, Math.max(0, timing.remainingMs));
    return () => {
      window.clearTimeout(timeout);
      if (timing.id === shownId && timing.startedAt) {
        timing.remainingMs = Math.max(0, timing.remainingMs - (Date.now() - timing.startedAt));
        timing.startedAt = 0;
      }
    };
  }, [archive, openErrorToast?.id, errorToastHovered]);

  // apply theme + accent
  useEffectA(() => {
    document.documentElement.setAttribute("data-theme", tw.theme);
    document.documentElement.setAttribute("data-density", tw.density);
    const palette = ACCENT_PALETTE[tw.accent] || ACCENT_PALETTE.terracotta;
    document.documentElement.style.setProperty("--accent", palette.accent);
    document.documentElement.style.setProperty("--accent-hover", palette.hover);
  }, [tw.theme, tw.accent, tw.density]);

  useEffectA(() => {
    if (window.pysar) {
      window.pysar.call("app_ready").catch(() => {});
      window.pysar.call("get_app_metadata").then((result) => {
        if (!result?.ok) return;
        const next = {
          name: result.name || window.PYSAR_APP.name,
          version: result.version || window.PYSAR_APP.version,
          displayVersion: result.displayVersion || window.PYSAR_APP.displayVersion,
          phase: result.phase || window.PYSAR_APP.phase,
        };
        window.PYSAR_APP = next;
        setAppMeta(next);
        document.title = next.name;
      }).catch(() => {});
      refreshRecentArchives();
    }
  }, []);

  async function findUnusedArchiveResources() {
    if (!window.pysar || !archive) return false;
    const scan = await window.pysar.call("scan_unused_archive_resources")
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!scan?.ok) {
      setOpenError(scan?.error || "Could not scan unused archive resources");
      return false;
    }
    const resources = Array.isArray(scan.resources) ? scan.resources : [];
    if (!resources.length) {
      await window.pysarAlert("No unused archive resources were found.", {
        title: "Unused archive resources",
      });
      return false;
    }

    const deletable = resources.filter((item) => !item.protected);
    const protectedCount = resources.length - deletable.length;
    if (!deletable.length) {
      await window.pysarAlert(
        `${resources.length} unused resource${resources.length === 1 ? " is" : "s are"} protected by Safe Mode. `
          + "Disable Safe Mode manually and scan again if you intend to remove original archive data.",
        { title: "Unused resources are protected" },
      );
      return false;
    }

    function resourceBadge(item) {
      if (item.resourceType === "bank") return "BANK";
      if (item.resourceType === "wave") return "BRWAV";
      if (item.resourceType === "wsd-entry") return "RWSD";
      if (item.resourceType === "embedded") return item.kind || "BIN";
      return item.kind || "FILE";
    }

    const choice = await window.pysarConsequence(
      `Delete ${deletable.length} unused archive resource${deletable.length === 1 ? "" : "s"}?`,
      {
        title: "Clean unused resources",
        caption: "Reachability from live sounds",
        actions: [{
          id: "cleanup",
          label: "Delete unused",
          description: protectedCount
            ? `${protectedCount} additional resource${protectedCount === 1 ? " is" : "s are"} protected and will be retained.`
            : "Only resources unreachable from every live sound will be removed.",
          confirmLabel: "Delete unused",
          tone: "danger",
        }],
        resources: resources.map((item, index) => ({
          id: `${item.resourceType}-${item.fileIndex ?? item.fileId ?? ""}-${item.id}-${index}`,
          resource: {
            badge: String(resourceBadge(item)).split(" ")[0].toUpperCase(),
            name: item.name,
          },
          outcomes: {
            cleanup: item.protected
              ? { text: "Retained by Safe Mode", status: "retained" }
              : { text: "Will be deleted", status: "deleted" },
          },
        })),
      },
    );
    if (choice?.action !== "cleanup") return false;

    const result = await window.pysar.call("delete_unused_archive_resources")
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setOpenError(result?.error || "Could not delete unused archive resources");
      return false;
    }
    if (!result.dirty) return false;

    stop();
    setDirty(true);
    if (result.data) handleDataRefresh(result.data);
    setTabs((current) => {
      const remaining = current.filter((item) => !["bank", "archive", "file"].includes(item.kind));
      return remaining.some((item) => item.id === "all")
        ? remaining
        : [{ id: "all", kind: "view", view: "all", title: "All sounds" }, ...remaining];
    });
    setActiveTab("all");
    setNavView(SOUND_NAV_BY_FILTER[soundFilter] || "all");
    setSelectedItem(null);
    setHistory([{ tabId: "all", navView: "all", soundFilter, selectedItem: null }]);
    setHistoryIndex(0);
    return true;
  }

  useEffectA(() => {
    function onKey(event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
        event.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
      } else if (event.key === "Escape" && document.activeElement === searchInputRef.current) {
        setSearchQuery("");
        searchInputRef.current.blur();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const searchMatch = useMemoA(() => bestSearchMatch(searchQuery, archive ? D : null), [searchQuery, archive, D]);
  useEffectA(() => {
    if (!archive || !searchMatch || !searchMatch.navId) return;
    if (searchMatch.navId === navView) return;
    if (searchMatch.kind === "sound" && SOUND_NAV_VIEWS.has(navView)) return;
    pickNav(searchMatch.navId, { record: false });
  }, [searchMatch, archive]);

  useEffectA(() => {
    function onPysarEvent(e) {
      const { type, payload } = e.detail || {};
      if (type === "window_close_requested") {
        setUnsavedBusy(false);
        setWindowCloseInfo(payload || null);
        setUnsavedAction("window");
        return;
      }
      if (type === "dump_progress" && payload) {
        setDumpStatus((current) => {
          if (!current?.busy) return current;
          return {
            ...current,
            mode: payload.mode || current.mode,
            progress: {
              completed: Math.max(0, Number(payload.completed || 0)),
              total: Math.max(0, Number(payload.total || 0)),
              percent: Math.max(0, Math.min(100, Number(payload.percent || 0))),
              detail: String(payload.detail || "Working…"),
            },
          };
        });
        return;
      }
      if (type === "duration_update" && payload) {
        const { soundId, durationMs: newDuration } = payload;
        const activeSound = playingSoundRef.current;
        const variation = activeSound?.seqVariation || null;
        const normalizedPairs = (value) => (Array.isArray(value) ? value : [])
          .map((pair) => [Number(pair?.[0]), Number(pair?.[1])])
          .filter((pair) => Number.isInteger(pair[0]) && Number.isFinite(pair[1]))
          .slice(0, 16)
          .sort((left, right) => left[0] - right[0] || left[1] - right[1]);
        const expectedNote = variation?.note == null ? null : Number(variation.note);
        const expectedProgram = variation?.program == null ? null : Number(variation.program);
        if (
          durationTargetRef.current !== soundId
          || activeSound?.id !== soundId
          || expectedNote !== payload.seqNoteOverride
          || expectedProgram !== payload.seqProgramOverride
          || JSON.stringify(normalizedPairs(variation?.randomOverrides))
            !== JSON.stringify(normalizedPairs(payload.seqRandomOverrides))
        ) return;
        if (payload.seqPlayback) applySeqPlaybackMetadata(soundId, payload.seqPlayback);
        setDurationMs((cur) => {
          if (cur > 0 && !payload.seqPlayback?.looped) return cur;
          return Math.max(0, Math.round(newDuration || 0));
        });
        setPlayingSound((cur) => {
          if (!cur || cur.id !== soundId) return cur;
          if (cur.durationMs > 0 && !payload.seqPlayback?.looped) return cur;
          return { ...cur, durationMs: Math.max(0, Math.round(newDuration || 0)) };
        });
      }
    }
    window.addEventListener("pysar-event", onPysarEvent);
    return () => window.removeEventListener("pysar-event", onPysarEvent);
  }, []);

  // helpers
  function snapshot(tabId = activeTab, nav = navView, selected = selectedItem, filter = soundFilter) {
    return { tabId, navView: nav, soundFilter: filter, selectedItem: selected };
  }
  function pushHistory(entry) {
    setHistory((prev) => {
      const currentIndex = Math.max(0, Math.min(historyIndex, prev.length - 1));
      const current = prev[currentIndex];
      const currentSelectionKey = pysarReferenceKey(selectedItemReference(current?.selectedItem));
      const nextSelectionKey = pysarReferenceKey(selectedItemReference(entry.selectedItem));
      if (current && current.tabId === entry.tabId && current.navView === entry.navView && currentSelectionKey === nextSelectionKey) {
        return prev;
      }
      const next = [...prev.slice(0, currentIndex + 1), entry];
      setHistoryIndex(next.length - 1);
      return next;
    });
  }
  function applyHistory(entry) {
    setActiveTab(entry.tabId);
    setNavView(entry.navView);
    const historicalFilter = entry.soundFilter || SOUND_FILTER_BY_NAV[entry.navView];
    if (historicalFilter) setSoundFilter(historicalFilter);
    setSelectedItem(entry.selectedItem || null);
  }
  function goHistory(delta) {
    const nextIndex = historyIndex + delta;
    if (nextIndex < 0 || nextIndex >= history.length) return;
    setHistoryIndex(nextIndex);
    applyHistory(history[nextIndex]);
  }
  function openTab(spec, options = {}) {
    setTabs((prev) => prev.some((t) => t.id === spec.id)
      ? prev.map((t) => t.id === spec.id ? { ...t, ...spec } : t)
      : [...prev, spec]);
    setActiveTab(spec.id);
    if (options.record !== false) pushHistory(snapshot(spec.id, navView, selectedItem));
  }
  function closeTab(id) {
    setTabs((prev) => {
      const idx = prev.findIndex((t) => t.id === id);
      if (idx === -1) return prev;
      const next = prev.filter((t) => t.id !== id);
      if (next.length === 0) {
        setActiveTab(null);
        return next;
      }
      if (activeTab === id) setActiveTab(next[Math.max(0, idx - 1)].id);
      return next;
    });
  }

  function pickNav(id, options = {}) {
    setNavView(id);
    const nextSoundFilter = SOUND_FILTER_BY_NAV[id];
    if (nextSoundFilter) {
      setSoundFilter(nextSoundFilter);
      openTab({ id: "all", kind: "view", view: "all", title: "All sounds" }, { record: false });
      if (options.record !== false) pushHistory(snapshot("all", id, selectedItem, nextSoundFilter));
      return;
    }
    const map = {
      banks: { title: "Banks", view: "banks" },
      groups: { title: "Groups", view: "groups" },
      players: { title: "Players", view: "players" },
      archives: { title: "Wave archives", view: "archives" },
      files: { title: "Raw files", view: "files" },
    };
    const e = map[id]; if (!e) return;
    openTab({ id, kind: "view", view: e.view, title: e.title }, { record: false });
    if (options.record !== false) pushHistory(snapshot(id, id, selectedItem));
  }

  function openSound(s, options = {}) {
    const item = { kind: "sound", id: s.id, name: s.name, item: s };
    setSelectedItem(item);
    queueSoundForPreview(s);
    if (s.type === "SEQ") {
      openTab({ id: "sound:" + s.id, kind: "sound", item: s, title: s.name }, { record: false });
    } else {
      openTab({ id: "sound:" + s.id, kind: "sound", item: s, title: s.name }, { record: false });
    }
    if (options.record !== false) pushHistory(snapshot("sound:" + s.id, navView, item));
  }
  function selectOnly(s, options = {}) {
    const item = { kind: "sound", id: s.id, name: s.name, item: s };
    setSelectedItem(item);
    queueSoundForPreview(s);
    if (options.record !== false) pushHistory(snapshot(activeTab, navView, item));
  }
  function selectSharedSequenceSound(soundId, options = {}) {
    const nextId = Number(soundId);
    const nextSound = (window.PYSAR_DATA?.sounds || []).find(
      (sound) => Number(sound.id) === nextId && sound.type === "SEQ"
    );
    if (!nextSound) return;
    if (options.reuseActiveTab) {
      const item = { kind: "sound", id: nextSound.id, name: nextSound.name, item: nextSound };
      setTabs((current) => current.map((currentTab) => (
        currentTab.id === activeTab && currentTab.kind === "sound"
          ? { ...currentTab, item: nextSound, title: nextSound.name }
          : currentTab
      )));
      setSelectedItem(item);
      queueSoundForPreview(nextSound);
      pushHistory(snapshot(activeTab, navView, item));
      return;
    }
    openSound(nextSound);
  }

  function openItem(it, options = {}) {
    setSelectedItem(it);
    const tabId = it.kind + ":" + it.id;
    openTab({ id: tabId, kind: it.kind, item: it.item, title: it.name }, { record: false });
    if (options.record !== false) pushHistory(snapshot(tabId, navView, it));
  }
  function selectOrgItem(it, options = {}) {
    setSelectedItem(it);
    if (options.record !== false) pushHistory(snapshot(activeTab, navView, it));
  }
  function selectedItemForTab(t) {
    if (!t || t.kind === "view") return selectedItem;
    const name = t.title || t.item?.name || t.item?.label || `${t.kind}:${t.item?.id ?? ""}`;
    return { kind: t.kind, id: t.item?.id, name, item: t.item };
  }
  function activateTab(t, options = {}) {
    if (!t) return;
    setActiveTab(t.id);
    const nextNav = t.kind === "view"
      ? (t.view === "all" ? (SOUND_NAV_BY_FILTER[soundFilter] || "all") : t.view)
      : navView;
    if (t.kind === "view") setNavView(nextNav);
    const nextSelected = selectedItemForTab(t);
    if (t.kind !== "view") setSelectedItem(nextSelected);
    if (options.record !== false) {
      pushHistory(snapshot(t.id, nextNav, t.kind === "view" ? selectedItem : nextSelected));
    }
  }

  function changeSoundFilter(filter, options = {}) {
    const nextFilter = SOUND_NAV_BY_FILTER[filter] ? filter : "ALL";
    const nextNav = SOUND_NAV_BY_FILTER[nextFilter];
    setSoundFilter(nextFilter);
    setNavView(nextNav);
    if (options.record !== false) {
      pushHistory(snapshot("all", nextNav, selectedItem, nextFilter));
    }
  }
  function moveTab(dragId, targetId) {
    if (!dragId || !targetId || dragId === targetId) return;
    setTabs((prev) => {
      const from = prev.findIndex((t) => t.id === dragId);
      const to = prev.findIndex((t) => t.id === targetId);
      if (from < 0 || to < 0) return prev;
      const next = [...prev];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });
  }

  function selectFile(it, options = {}) {
    setSelectedItem(it);
    if (options.record !== false) pushHistory(snapshot(activeTab, navView, it));
  }

  function navigateToReferrer(rawReference) {
    const ref = normalizePysarReference(rawReference);
    if (!ref) return;

    // A programmatic jump must reveal its target even when the current global
    // search would otherwise filter the destination row out.
    setSearchQuery("");
    setMenuOpen(null);

    if (ref.kind === "sound") {
      const sound = D.sounds.find((item) => item.id === ref.id);
      if (!sound) return;
      const item = { kind: "sound", id: sound.id, name: sound.name, item: sound };
      const nextNav = SOUND_NAV_BY_FILTER[sound.type] || "all";
      const nextFilter = Object.prototype.hasOwnProperty.call(SOUND_NAV_BY_FILTER, sound.type) ? sound.type : "ALL";
      setNavView(nextNav);
      setSoundFilter(nextFilter);
      openSound(sound, { record: false });
      pushHistory({ tabId: `sound:${sound.id}`, navView: nextNav, soundFilter: nextFilter, selectedItem: item });
      focusPysarReference(ref);
      return;
    }

    if (ref.kind === "file") {
      const files = D.files || [];
      const file = files.find((item) => (
        ref.fileIndex != null && item.fileIndex != null
          ? item.fileIndex === ref.fileIndex
          : item.id === ref.id
      ));
      if (!file) return;
      const item = { kind: "file", id: file.id, name: file.label, item: file };
      pickNav("files", { record: false });
      setSelectedItem(item);
      pushHistory({ tabId: "files", navView: "files", selectedItem: item });
      focusPysarReference({ kind: "file", id: file.id, fileIndex: file.fileIndex });
      return;
    }

    if (ref.kind === "bank") {
      const bank = D.banks.find((item) => item.id === ref.id);
      if (!bank) return;
      const item = { kind: "bank", id: bank.id, name: bank.name, item: bank };
      setNavView("banks");
      openItem(item, { record: false });
      pushHistory({ tabId: `bank:${bank.id}`, navView: "banks", selectedItem: item });
      focusPysarReference(ref);
      return;
    }

    if (ref.kind === "player") {
      const player = D.players.find((item) => item.id === ref.id);
      if (!player) return;
      const item = { kind: "player", id: player.id, name: player.name, item: player };
      pickNav("players", { record: false });
      setSelectedItem(item);
      pushHistory({ tabId: "players", navView: "players", selectedItem: item });
      focusPysarReference(ref);
      return;
    }

    if (ref.kind === "group") {
      const group = D.groups.find((item) => item.id === ref.id);
      if (!group) return;
      const item = { kind: "group", id: group.id, name: group.name, item: group };
      pickNav("groups", { record: false });
      setSelectedItem(item);
      pushHistory({ tabId: "groups", navView: "groups", selectedItem: item });
      focusPysarReference(ref);
      return;
    }

    if (ref.kind === "archive") {
      const archiveItem = (D.waveArchives || []).find((item) => item.id === ref.id);
      if (!archiveItem) return;
      const item = { kind: "archive", id: archiveItem.id, name: archiveItem.name, item: archiveItem };
      setNavView("archives");
      openItem(item, { record: false });
      pushHistory({ tabId: `archive:${archiveItem.id}`, navView: "archives", selectedItem: item });
      focusPysarReference(ref);
      return;
    }

    if (ref.kind === "wave") {
      const archiveItem = (D.waveArchives || []).find((item) => item.id === ref.archiveId);
      const waveIndex = ref.waveIndex ?? ref.id;
      if (!archiveItem || !Number.isInteger(waveIndex) || waveIndex < 0) return;
      const tabId = `archive:${archiveItem.id}`;
      setNavView("archives");
      openTab({ id: tabId, kind: "archive", item: archiveItem, title: archiveItem.name }, { record: false });
      const partial = {
        kind: "wave",
        id: waveIndex,
        name: ref.name || `${archiveItem.name} · #${waveIndex}`,
        item: {
          archiveId: archiveItem.id,
          archiveName: archiveItem.name,
          waveIndex,
          index: waveIndex,
        },
      };
      setSelectedItem(partial);
      queueWaveForPreview(partial);
      pushHistory({ tabId, navView: "archives", selectedItem: partial });
      focusPysarReference({ kind: "wave", archiveId: archiveItem.id, waveIndex });
    }
  }

  function startPanelResize(panel, event) {
    event.preventDefault();
    const startX = event.clientX;
    const startSidebar = sidebarWidth;
    const startInspector = inspectorWidth;
    function onMove(e) {
      const dx = e.clientX - startX;
      if (panel === "sidebar") {
        setSidebarWidth(Math.max(160, Math.min(420, startSidebar + dx)));
      } else {
        setInspectorWidth(Math.max(240, Math.min(560, startInspector - dx)));
      }
    }
    function onUp() {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.classList.remove("resizing-pane");
    }
    document.body.classList.add("resizing-pane");
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  function clearAudio() {
    // A looping BRSTM is not installed in audioRef until its complete WAV has
    // been fetched and decoded. Keep that pending work cancellable too, or a
    // fast sound/track change leaves two long BRSTM decodes competing.
    const pendingLoopFetch = loopingFetchAbortRef.current;
    loopingFetchAbortRef.current = null;
    pendingLoopFetch?.abort();
    if (playheadTimerRef.current) {
      window.clearInterval(playheadTimerRef.current);
      playheadTimerRef.current = null;
    }
    const audio = audioRef.current;
    if (!audio) return;
    audio.pause();
    audio.removeAttribute?.("src");
    audio.load?.();
    audioRef.current = null;
  }
  function attachAudio(src, duration, offsetMs = 0, t0 = null, onEnded = null, sourceStartsAtZero = false) {
    clearAudio();
    const audio = new Audio();
    audio.preload = "auto";
    audio.volume = volume;
    audioRef.current = audio;
    const playbackBaseMs = sourceStartsAtZero ? 0 : offsetMs;
    audioBaseRef.current = playbackBaseMs;
    setDurationMs(duration || 0);
    setPlayheadMs(offsetMs);
    function syncNativeDuration() {
      if (audio !== audioRef.current) return;
      const nativeDuration = Number.isFinite(audio.duration) ? Math.round(audio.duration * 1000) : 0;
      // A full/offset WAV's decoded duration is authoritative. Estimates can
      // include renderer tails or stale tick caps and made the needle stop in
      // the middle of the bar.
      // Unknown-length streaming WAV headers decode to roughly 9.3 hours at
      // 32 kHz; reject that sentinel while allowing any realistic preview.
      const nativeTotal = nativeDuration > 0 && nativeDuration < 28_800_000
        ? playbackBaseMs + nativeDuration
        : 0;
      const nextDuration = nativeTotal || duration;
      setDurationMs(nextDuration || 0);
      setPlayingSound((current) => current ? { ...current, durationMs: nextDuration || 0 } : current);
    }
    audio.addEventListener("loadedmetadata", () => {
      if (t0) console.log(`[PLAY] loadedmetadata: ${(performance.now()-t0).toFixed(0)}ms`);
      syncNativeDuration();
    });
    audio.addEventListener("durationchange", syncNativeDuration);
    function updatePlayhead() {
      if (audio !== audioRef.current) return;
      const next = playbackBaseMs + Math.round((audio.currentTime || 0) * 1000);
      setPlayheadMs(duration ? Math.min(duration, next) : next);
    }
    let firstUpdateLogged = false;
    audio.addEventListener("timeupdate", () => {
      if (audio !== audioRef.current) return;
      if (!firstUpdateLogged && t0) { firstUpdateLogged = true; console.log(`[PLAY] first timeupdate: ${(performance.now()-t0).toFixed(0)}ms  currentTime=${audio.currentTime.toFixed(3)}`); }
      updatePlayhead();
    });
    audio.addEventListener("ended", () => {
      if (audio !== audioRef.current) return;
      if (onEnded) {
        onEnded(playbackBaseMs + Math.round((Number.isFinite(audio.duration) ? audio.duration : 0) * 1000));
        return;
      }
      setIsPlaying(false);
      setPlayheadMs(playbackBaseMs + Math.round((Number.isFinite(audio.duration) ? audio.duration : 0) * 1000));
      setPlayingId(null);
      if (playheadTimerRef.current) {
        window.clearInterval(playheadTimerRef.current);
        playheadTimerRef.current = null;
      }
    });
    function beginPlayback() {
      if (audio !== audioRef.current) return;
      audio.play().then(() => {
        if (audio !== audioRef.current) return;
        if (t0) console.log(`[PLAY] audio.play() resolved: ${(performance.now()-t0).toFixed(0)}ms`);
        setIsPlaying(true);
      }).catch((error) => {
        if (audio !== audioRef.current) return;
        setOpenError(String(error));
        setIsPlaying(false);
        setPlayingId(null);
      });
    }
    // STRM URLs expose the complete selected-track mix so loop and seek
    // coordinates remain absolute. Seek before starting to avoid briefly
    // playing from zero on native-audio fallback browsers.
    if (sourceStartsAtZero && offsetMs > 0) {
      audio.addEventListener("loadedmetadata", () => {
        if (audio !== audioRef.current) return;
        const upper = Number.isFinite(audio.duration) ? Math.max(0, audio.duration - 0.001) : offsetMs / 1000;
        audio.currentTime = Math.min(offsetMs / 1000, upper);
        updatePlayhead();
        beginPlayback();
      }, { once: true });
    }
    // Set src after listeners are attached so no events are missed.
    audio.src = src;
    if (!(sourceStartsAtZero && offsetMs > 0)) beginPlayback();
    playheadTimerRef.current = window.setInterval(updatePlayhead, 100);
  }

  async function attachProgressivePcmAudio(
    src,
    duration,
    offsetMs = 0,
    requestId = null,
    onEnded = null,
    onStreamError = null,
    t0 = null,
    sequencePlayback = null,
    sequenceSoundId = null,
    transitionOptions = null,
  ) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || !window.fetch) return false;
    const deferAudioHandoff = !!transitionOptions?.deferAudioHandoff && !!audioRef.current;
    const previousAudio = deferAudioHandoff ? audioRef.current : null;
    if (!deferAudioHandoff) clearAudio();

    const abortController = typeof AbortController === "function" ? new AbortController() : null;
    let reader = null;
    let context = null;
    let adapter = null;
    try {
      const response = await fetch(src, {
        ...(abortController ? { signal: abortController.signal } : {}),
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Audio stream returned HTTP ${response.status}`);
      if (!response.body?.getReader) throw new Error("Streaming responses are not supported by this browser");
      reader = response.body.getReader();

      function appendBytes(left, right) {
        if (!left?.length) return right;
        if (!right?.length) return left;
        const joined = new Uint8Array(left.length + right.length);
        joined.set(left, 0);
        joined.set(right, left.length);
        return joined;
      }
      function chunkId(bytes, offset) {
        return String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3]);
      }
      function parsePcmWav(bytes) {
        if (bytes.length < 12) return null;
        if (chunkId(bytes, 0) !== "RIFF" || chunkId(bytes, 8) !== "WAVE") {
          throw new Error("Audio response is not a RIFF/WAVE stream");
        }
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        let cursor = 12;
        let format = null;
        while (cursor + 8 <= bytes.length) {
          const id = chunkId(bytes, cursor);
          const size = view.getUint32(cursor + 4, true);
          const payload = cursor + 8;
          if (id === "fmt ") {
            if (size < 16) throw new Error("WAV fmt chunk is too short");
            if (payload + 16 > bytes.length) return null;
            format = {
              encoding: view.getUint16(payload, true),
              channels: view.getUint16(payload + 2, true),
              sampleRate: view.getUint32(payload + 4, true),
              blockAlign: view.getUint16(payload + 12, true),
              bitsPerSample: view.getUint16(payload + 14, true),
            };
          } else if (id === "data") {
            if (!format) throw new Error("WAV data chunk precedes its format chunk");
            return { ...format, dataOffset: payload };
          }
          if (size === 0xffffffff) throw new Error(`Unsupported open-ended WAV ${id} chunk`);
          const next = payload + size + (size & 1);
          if (next > bytes.length) return null;
          cursor = next;
        }
        return null;
      }

      let preamble = new Uint8Array(0);
      let wav = null;
      while (!wav) {
        if (requestId != null && requestId !== playRequestRef.current) {
          abortController?.abort();
          return true;
        }
        const part = await reader.read();
        if (part.done) throw new Error("Audio stream ended before its WAV header");
        preamble = appendBytes(preamble, part.value);
        if (preamble.length > 1024 * 1024) throw new Error("WAV header exceeds 1 MiB");
        wav = parsePcmWav(preamble);
      }
      if (
        wav.encoding !== 1
        || wav.bitsPerSample !== 16
        || wav.channels < 1
        || wav.channels > 8
        || wav.sampleRate < 1
        || wav.blockAlign < wav.channels * 2
      ) {
        throw new Error("Progressive playback requires interleaved PCM16 WAV audio");
      }

      try {
        context = new AudioContextClass({ latencyHint: "interactive", sampleRate: wav.sampleRate });
      } catch (_) {
        context = new AudioContextClass();
      }
      const gain = context.createGain();
      gain.gain.value = deferAudioHandoff ? 0 : volume;
      gain.connect(context.destination);

      const sources = new Set();
      const firstBatchFrames = 1024;
      const laterBatchFrames = 4096;
      let pendingPcm = new Uint8Array(0);
      let scheduledFrames = 0;
      let nextStartTime = 0;
      let clockStartTime = 0;
      let lastSource = null;
      let readingFinished = false;
      let closed = false;
      let ended = false;
      let started = false;
      let failureHandled = false;
      const maxScheduleAheadSeconds = 2;
      const offsetFrame = Math.max(0, Math.round(offsetMs * wav.sampleRate / 1000));
      let transitionDiscardedFrames = 0;
      let playbackStartFrame = offsetFrame;
      let playbackStartMs = offsetMs;
      const maxSequenceLoopBytes = 64 * 1024 * 1024;
      let loopStartFrame = 0;
      let loopEndFrame = 0;
      let loopFrameCount = 0;
      const loopParts = [];
      let loopBytes = 0;
      let loopCaptureOverflow = false;
      const pendingMetadataParts = [];
      let pendingMetadataBytes = 0;
      let pendingMetadataOverflow = false;
      let loopMetadataResolved = false;
      let loopSource = null;
      let loopRequested = !!sequencePlayback?.loopEnabled;
      let loopEnabled = false;

      function captureLoopOverlap(absoluteStart, bytes) {
        if (loopFrameCount <= 0 || loopCaptureOverflow) return;
        const frames = Math.floor(bytes.length / wav.blockAlign);
        const absoluteEnd = absoluteStart + frames;
        const overlapStart = Math.max(loopStartFrame, absoluteStart);
        const overlapEnd = Math.min(loopEndFrame, absoluteEnd);
        if (overlapEnd <= overlapStart) return;
        const firstByte = (overlapStart - absoluteStart) * wav.blockAlign;
        const lastByte = (overlapEnd - absoluteStart) * wav.blockAlign;
        const copy = bytes.slice(firstByte, lastByte);
        if (loopBytes + copy.length <= maxSequenceLoopBytes) {
          loopParts.push(copy);
          loopBytes += copy.length;
        } else {
          loopCaptureOverflow = true;
          loopParts.length = 0;
          loopBytes = 0;
        }
      }

      function configureSequenceLoop(metadata) {
        if (!metadata || metadata.loading || typeof metadata.looped !== "boolean") return;
        const metadataSampleRate = Math.max(1, Number(metadata.sampleRate) || wav.sampleRate);
        const nextLoopStartFrame = metadata.looped ? Math.max(0, Math.round(
          metadata.loopStartFrame != null
            ? Number(metadata.loopStartFrame) * wav.sampleRate / metadataSampleRate
            : Number(metadata.loopStartMs || 0) * wav.sampleRate / 1000,
        )) : 0;
        const nextLoopEndFrame = metadata.looped ? Math.max(nextLoopStartFrame, Math.round(
          metadata.loopEndFrame != null
            ? Number(metadata.loopEndFrame) * wav.sampleRate / metadataSampleRate
            : Number(metadata.loopEndMs || 0) * wav.sampleRate / 1000,
        )) : 0;
        loopRequested = !!metadata.loopEnabled;
        if (
          loopMetadataResolved
          && nextLoopStartFrame === loopStartFrame
          && nextLoopEndFrame === loopEndFrame
        ) {
          loopEnabled = loopRequested && loopFrameCount > 0;
          if (loopEnabled && loopSource) loopSource.loop = true;
          if (loopEnabled && readingFinished) installSequenceLoop();
          if (!loopEnabled && loopSource) loopSource.loop = false;
          return;
        }
        loopMetadataResolved = true;
        loopStartFrame = nextLoopStartFrame;
        loopEndFrame = nextLoopEndFrame;
        loopFrameCount = loopEndFrame - loopStartFrame;
        loopEnabled = loopRequested && loopFrameCount > 0;
        loopParts.length = 0;
        loopBytes = 0;
        loopCaptureOverflow = pendingMetadataOverflow;
        if (!loopCaptureOverflow && loopFrameCount > 0) {
          for (const part of pendingMetadataParts) captureLoopOverlap(part.startFrame, part.bytes);
        }
        pendingMetadataParts.length = 0;
        pendingMetadataBytes = 0;
        if (readingFinished) installSequenceLoop();
      }

      function isCurrent() {
        return adapter === audioRef.current
          && (requestId == null || requestId === playRequestRef.current);
      }

      function localPosition() {
        if (!started) return 0;
        const elapsedFrames = Math.max(0, Math.floor((context.currentTime - clockStartTime) * wav.sampleRate));
        let absoluteFrame = playbackStartFrame + elapsedFrames;
        if (loopSource && loopFrameCount > 0 && absoluteFrame >= loopEndFrame) {
          absoluteFrame = loopStartFrame + ((absoluteFrame - loopEndFrame) % loopFrameCount);
        } else {
          absoluteFrame = Math.min(playbackStartFrame + scheduledFrames, absoluteFrame);
        }
        return (absoluteFrame - playbackStartFrame) / wav.sampleRate;
      }
      function finishPlayback() {
        if (closed || ended) return;
        ended = true;
        if (!isCurrent()) return;
        const exactTotal = playbackStartMs + Math.round(scheduledFrames * 1000 / wav.sampleRate);
        setDurationMs(exactTotal);
        setPlayingSound((current) => current ? { ...current, durationMs: exactTotal } : current);
        if (onEnded) onEnded(exactTotal);
      }
      function schedulePcm(bytes) {
        const frames = Math.floor(bytes.length / wav.blockAlign);
        if (!frames) return;
        const absoluteStart = playbackStartFrame + scheduledFrames;
        if (!loopMetadataResolved && !pendingMetadataOverflow) {
          const copy = bytes.slice();
          if (pendingMetadataBytes + copy.length <= maxSequenceLoopBytes) {
            pendingMetadataParts.push({ startFrame: absoluteStart, bytes: copy });
            pendingMetadataBytes += copy.length;
          } else {
            pendingMetadataOverflow = true;
            pendingMetadataParts.length = 0;
            pendingMetadataBytes = 0;
          }
        }
        if (loopMetadataResolved) captureLoopOverlap(absoluteStart, bytes);
        const buffer = context.createBuffer(wav.channels, frames, wav.sampleRate);
        const view = new DataView(bytes.buffer, bytes.byteOffset, frames * wav.blockAlign);
        for (let channel = 0; channel < wav.channels; channel += 1) {
          const output = buffer.getChannelData(channel);
          for (let frame = 0; frame < frames; frame += 1) {
            output[frame] = view.getInt16(frame * wav.blockAlign + channel * 2, true) / 32768;
          }
        }
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.connect(gain);
        if (!started) {
          clockStartTime = context.currentTime + 0.01;
          nextStartTime = clockStartTime;
          started = true;
          if (t0) console.log(`[PLAY] first PCM scheduled: ${(performance.now()-t0).toFixed(0)}ms`);
        } else if (nextStartTime < context.currentTime) {
          // Rendering should run well ahead of real time. If it ever falls
          // behind, schedule immediately instead of throwing or dropping PCM.
          nextStartTime = context.currentTime + 0.005;
        }
        lastSource = source;
        sources.add(source);
        source.onended = () => {
          sources.delete(source);
          source.disconnect();
          if (readingFinished && source === lastSource && !loopSource) finishPlayback();
        };
        source.start(nextStartTime);
        nextStartTime += frames / wav.sampleRate;
        scheduledFrames += frames;
      }
      function installSequenceLoop() {
        if (!loopEnabled || loopSource || loopFrameCount <= 0 || loopCaptureOverflow) return false;
        if (loopBytes !== loopFrameCount * wav.blockAlign) return false;
        const bytes = new Uint8Array(loopBytes);
        let cursor = 0;
        for (const part of loopParts) {
          bytes.set(part, cursor);
          cursor += part.length;
        }
        const buffer = context.createBuffer(wav.channels, loopFrameCount, wav.sampleRate);
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        for (let channel = 0; channel < wav.channels; channel += 1) {
          const output = buffer.getChannelData(channel);
          for (let frame = 0; frame < loopFrameCount; frame += 1) {
            output[frame] = view.getInt16(frame * wav.blockAlign + channel * 2, true) / 32768;
          }
        }
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.loop = true;
        source.connect(gain);
        source.onended = () => {
          source.disconnect();
          if (loopSource === source) loopSource = null;
          if (!loopEnabled) finishPlayback();
        };
        loopSource = source;
        source.start(nextStartTime);
        return true;
      }
      function consumePcm(bytes, flush = false) {
        pendingPcm = appendBytes(pendingPcm, bytes);
        if (deferAudioHandoff && !started && audioRef.current === previousAudio) {
          const previousAbsoluteMs = Math.max(
            offsetMs,
            audioBaseRef.current + Math.max(0, Number(previousAudio?.currentTime) || 0) * 1000,
            playheadMsRef.current,
          );
          const targetDiscardedFrames = Math.max(
            transitionDiscardedFrames,
            Math.round((previousAbsoluteMs - offsetMs) * wav.sampleRate / 1000),
          );
          const neededFrames = targetDiscardedFrames - transitionDiscardedFrames;
          const availableFrames = Math.floor(pendingPcm.length / wav.blockAlign);
          const discardFrames = Math.min(neededFrames, availableFrames);
          if (discardFrames > 0) {
            pendingPcm = pendingPcm.slice(discardFrames * wav.blockAlign);
            transitionDiscardedFrames += discardFrames;
            playbackStartFrame = offsetFrame + transitionDiscardedFrames;
            playbackStartMs = offsetMs + Math.round(transitionDiscardedFrames * 1000 / wav.sampleRate);
          }
        }
        while (true) {
          const batchFrames = started ? laterBatchFrames : firstBatchFrames;
          const availableFrames = Math.floor(pendingPcm.length / wav.blockAlign);
          if (availableFrames < batchFrames && !flush) break;
          if (availableFrames <= 0) break;
          const frames = flush ? availableFrames : batchFrames;
          const byteCount = frames * wav.blockAlign;
          schedulePcm(pendingPcm.subarray(0, byteCount));
          pendingPcm = pendingPcm.slice(byteCount);
        }
      }

      adapter = {
        isProgressivePcm: true,
        sequenceSoundId,
        get isSequenceLoop() { return loopFrameCount > 0; },
        get ended() { return ended; },
        get duration() {
          const known = Math.max(0, Number(duration || 0) - playbackStartMs) / 1000;
          return readingFinished ? scheduledFrames / wav.sampleRate : (known || scheduledFrames / wav.sampleRate);
        },
        get currentTime() { return localPosition(); },
        set currentTime(_) {},
        get volume() { return gain.gain.value; },
        set volume(value) { gain.gain.value = Math.max(0, Math.min(1, Number(value) || 0)); },
        setSequenceLoopMetadata(metadata) {
          configureSequenceLoop(metadata);
        },
        setLoopEnabled(value) {
          loopRequested = !!value;
          loopEnabled = loopRequested && loopFrameCount > 0;
          if (loopEnabled && loopSource) loopSource.loop = true;
          if (loopEnabled && readingFinished) installSequenceLoop();
          if (!loopEnabled && loopSource) loopSource.loop = false;
        },
        play() {
          if (closed) return Promise.reject(new Error("Audio context is closed"));
          return context.resume();
        },
        pause() {
          if (!closed) context.suspend().catch(() => {});
        },
        load() {
          if (closed) return;
          closed = true;
          abortController?.abort();
          reader?.cancel().catch(() => {});
          for (const source of sources) {
            try { source.stop(); } catch (_) {}
            source.disconnect();
          }
          sources.clear();
          if (loopSource) {
            try { loopSource.stop(); } catch (_) {}
            loopSource.disconnect();
            loopSource = null;
          }
          gain.disconnect();
          context.close().catch(() => {});
        },
      };

      configureSequenceLoop(sequencePlayback);

      consumePcm(preamble.subarray(wav.dataOffset));
      preamble = null;
      while (!started) {
        const part = await reader.read();
        if (part.done) {
          consumePcm(new Uint8Array(0), true);
          readingFinished = true;
          installSequenceLoop();
          break;
        }
        consumePcm(part.value);
      }
      if (!started) throw new Error("WAV stream contains no complete PCM frames");
      if (requestId != null && requestId !== playRequestRef.current) {
        adapter.load();
        return true;
      }

      if (deferAudioHandoff) clearAudio();
      audioRef.current = adapter;
      gain.gain.value = volume;
      audioBaseRef.current = playbackStartMs;
      setDurationMs(duration || 0);
      setPlayheadMs(playbackStartMs);
      await adapter.play();
      if (adapter !== audioRef.current) return true;
      setIsPlaying(true);
      playheadTimerRef.current = window.setInterval(() => {
        if (adapter !== audioRef.current) return;
        const absolute = playbackStartMs + Math.round(adapter.currentTime * 1000);
        setPlayheadMs(duration ? Math.min(duration, absolute) : absolute);
      }, 100);

      async function pump() {
        try {
          while (!closed) {
            // Keep enough PCM queued for glitch-free playback without
            // materializing an entire long/cyclic sequence in Web Audio.
            while (
              !closed
              && started
              && nextStartTime - context.currentTime > maxScheduleAheadSeconds
            ) {
              await new Promise((resolve) => window.setTimeout(resolve, 25));
            }
            if (closed) return;
            const part = await reader.read();
            if (part.done) break;
            consumePcm(part.value);
            if (nextStartTime - context.currentTime > 0.25) {
              // Do not let a fast local renderer monopolize the browser's
              // microtask queue while it fills the initial PCM cushion.
              await new Promise((resolve) => window.setTimeout(resolve, 0));
            }
          }
          if (closed) return;
          consumePcm(new Uint8Array(0), true);
          readingFinished = true;
          // The normal pump path is how every non-trivial sequence finishes
          // buffering. Install the captured loop before the last scheduled
          // PCM source ends, so the first wrap stays on the same AudioContext
          // timeline instead of falling back to a new HTTP/renderer request.
          installSequenceLoop();
          const exactTotal = playbackStartMs + Math.round(scheduledFrames * 1000 / wav.sampleRate);
          if (isCurrent()) {
            setDurationMs(exactTotal);
            setPlayingSound((current) => current ? { ...current, durationMs: exactTotal } : current);
          }
          if (lastSource && nextStartTime <= context.currentTime && !loopSource) finishPlayback();
        } catch (error) {
          if (closed || error?.name === "AbortError" || failureHandled) return;
          failureHandled = true;
          const absolute = playbackStartMs + Math.round(localPosition() * 1000);
          console.warn("Progressive PCM playback fallback:", error);
          if (adapter === audioRef.current && onStreamError) onStreamError(error, absolute);
        }
      }
      pump();
      return true;
    } catch (error) {
      abortController?.abort();
      reader?.cancel().catch(() => {});
      if (adapter) adapter.load();
      else context?.close().catch(() => {});
      if (requestId == null || requestId === playRequestRef.current) {
        console.warn("Progressive PCM playback unavailable:", error);
      }
      return requestId != null && requestId !== playRequestRef.current;
    }
  }

  async function attachLoopingAudio(
    src,
    duration,
    loopStartMs,
    offsetMs = 0,
    requestId = null,
    soundId = null,
    onEnded = null,
  ) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return false;
    clearAudio();
    const abortController = typeof AbortController === "function" ? new AbortController() : null;
    if (abortController) loopingFetchAbortRef.current = abortController;
    try {
      const response = await fetch(src, {
        ...(abortController ? { signal: abortController.signal } : {}),
      });
      if (!response.ok) throw new Error(`Audio stream returned HTTP ${response.status}`);
      const encoded = await response.arrayBuffer();
      if (requestId != null && requestId !== playRequestRef.current) return true;
      const context = new AudioContextClass();
      const buffer = await context.decodeAudioData(encoded.slice(0));
      if (requestId != null && requestId !== playRequestRef.current) {
        context.close().catch(() => {});
        return true;
      }
      const gain = context.createGain();
      gain.gain.value = volume;
      gain.connect(context.destination);
      const loopStart = Math.max(0, Math.min(buffer.duration, Number(loopStartMs || 0) / 1000));
      const loopLength = Math.max(0, buffer.duration - loopStart);
      const exactDuration = Math.round(buffer.duration * 1000) || duration || 0;
      let source = null;
      let sourceSerial = 0;
      let startedAt = 0;
      let position = Math.max(0, Math.min(buffer.duration, offsetMs / 1000));
      let active = false;
      let closed = false;
      let ended = false;
      let loopEnabled = loopLength > 0 && !!strmPlaybackBySoundRef.current[soundId]?.loopEnabled;

      function normalized(value, shouldLoop = loopEnabled) {
        const seconds = Math.max(0, Number(value) || 0);
        if (!shouldLoop || seconds < buffer.duration || loopLength <= 0) return Math.min(seconds, buffer.duration);
        return loopStart + ((seconds - loopStart) % loopLength);
      }
      function stopSource() {
        sourceSerial += 1;
        if (!source) return;
        try { source.stop(); } catch (_) {}
        source.disconnect();
        source = null;
      }
      function currentPosition() {
        return active ? normalized(position + (context.currentTime - startedAt)) : position;
      }
      function startSource() {
        stopSource();
        position = normalized(position);
        const serial = sourceSerial;
        source = context.createBufferSource();
        source.buffer = buffer;
        source.loop = loopEnabled;
        source.loopStart = loopStart;
        source.loopEnd = buffer.duration;
        source.connect(gain);
        source.onended = () => {
          if (serial !== sourceSerial || !active || closed) return;
          active = false;
          ended = true;
          position = buffer.duration;
          source = null;
          if (adapter === audioRef.current && onEnded) onEnded(exactDuration);
        };
        startedAt = context.currentTime;
        source.start(0, Math.min(position, Math.max(0, buffer.duration - 0.000001)));
      }

      const adapter = {
        isWebAudioLoop: true,
        get ended() { return ended; },
        get loopEnabled() { return loopEnabled; },
        get duration() { return buffer.duration; },
        get currentTime() { return currentPosition(); },
        set currentTime(value) {
          position = normalized(value);
          if (active) startSource();
        },
        get volume() { return gain.gain.value; },
        set volume(value) { gain.gain.value = Math.max(0, Math.min(1, Number(value) || 0)); },
        setLoopEnabled(value) {
          const nextEnabled = loopLength > 0 && !!value;
          if (nextEnabled === loopEnabled) return;
          // AudioBufferSourceNode.loop is mutable. Preserve the audible phase
          // and clock origin, then change the existing source in place: no
          // fetch, decode, restart, or playhead jump is needed.
          position = currentPosition();
          startedAt = context.currentTime;
          loopEnabled = nextEnabled;
          ended = false;
          if (source) source.loop = loopEnabled;
        },
        play() {
          if (closed) return Promise.reject(new Error("Audio context is closed"));
          return context.resume().then(() => {
            if (!active) {
              ended = false;
              active = true;
              startSource();
            }
          });
        },
        pause() {
          if (!active) return;
          position = currentPosition();
          active = false;
          stopSource();
        },
        load() {
          closed = true;
          active = false;
          stopSource();
          context.close().catch(() => {});
        },
      };
      audioRef.current = adapter;
      audioBaseRef.current = 0;
      setDurationMs(exactDuration);
      setPlayheadMs(Math.round(position * 1000));
      setPlayingSound((current) => current ? { ...current, durationMs: exactDuration } : current);
      await adapter.play();
      if (adapter !== audioRef.current) return true;
      setIsPlaying(true);
      playheadTimerRef.current = window.setInterval(() => {
        if (adapter !== audioRef.current) return;
        setPlayheadMs(Math.round(adapter.currentTime * 1000));
      }, 50);
      return true;
    } catch (error) {
      if (
        error?.name !== "AbortError"
        && (requestId == null || requestId === playRequestRef.current)
      ) console.warn("Web Audio loop fallback:", error);
      return false;
    } finally {
      // Never clear the controller belonging to a newer attach request.
      if (loopingFetchAbortRef.current === abortController) {
        loopingFetchAbortRef.current = null;
      }
    }
  }

  async function attachProgressiveLoopingAudio(
    streamInfo,
    duration,
    offsetMs = 0,
    requestId = null,
    soundId = null,
    onEnded = null,
    t0 = null,
  ) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    const ClockClass = window.PysarProgressiveLoopClock;
    if (!AudioContextClass || !ClockClass || !window.fetch || !streamInfo?.startUrl) return false;
    clearAudio();

    const controllers = [];
    const pendingRequest = {
      abort() {
        for (const controller of controllers) controller.abort();
      },
    };
    loopingFetchAbortRef.current = pendingRequest;
    let context = null;
    let adapter = null;
    let closed = false;

    function appendBytes(left, right) {
      if (!left?.length) return right;
      if (!right?.length) return left;
      const joined = new Uint8Array(left.length + right.length);
      joined.set(left, 0);
      joined.set(right, left.length);
      return joined;
    }
    function chunkId(bytes, offset) {
      return String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3]);
    }
    function parsePcmWav(bytes) {
      if (bytes.length < 12) return null;
      if (chunkId(bytes, 0) !== "RIFF" || chunkId(bytes, 8) !== "WAVE") {
        throw new Error("Audio response is not a RIFF/WAVE stream");
      }
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      let cursor = 12;
      let format = null;
      while (cursor + 8 <= bytes.length) {
        const id = chunkId(bytes, cursor);
        const size = view.getUint32(cursor + 4, true);
        const payload = cursor + 8;
        if (id === "fmt ") {
          if (size < 16) throw new Error("WAV fmt chunk is too short");
          if (payload + 16 > bytes.length) return null;
          format = {
            encoding: view.getUint16(payload, true),
            channels: view.getUint16(payload + 2, true),
            sampleRate: view.getUint32(payload + 4, true),
            blockAlign: view.getUint16(payload + 12, true),
            bitsPerSample: view.getUint16(payload + 14, true),
          };
        } else if (id === "data") {
          if (!format) throw new Error("WAV data chunk precedes its format chunk");
          return { ...format, dataOffset: payload, dataSize: size };
        }
        if (size === 0xffffffff) throw new Error(`Unsupported open-ended WAV ${id} chunk`);
        const next = payload + size + (size & 1);
        if (next > bytes.length) return null;
        cursor = next;
      }
      return null;
    }
    async function openPcmStream(src) {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      if (controller) controllers.push(controller);
      const response = await fetch(src, {
        ...(controller ? { signal: controller.signal } : {}),
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Audio stream returned HTTP ${response.status}`);
      if (!response.body?.getReader) throw new Error("Streaming responses are not supported by this browser");
      const reader = response.body.getReader();
      let preamble = new Uint8Array(0);
      let wav = null;
      while (!wav) {
        const part = await reader.read();
        if (part.done) throw new Error("Audio stream ended before its WAV header");
        preamble = appendBytes(preamble, part.value);
        if (preamble.length > 1024 * 1024) throw new Error("WAV header exceeds 1 MiB");
        wav = parsePcmWav(preamble);
      }
      if (
        wav.encoding !== 1
        || wav.bitsPerSample !== 16
        || wav.channels !== 2
        || wav.sampleRate !== Number(streamInfo.sampleRate)
        || wav.blockAlign !== 4
      ) throw new Error("Progressive BRSTM playback requires native-rate stereo PCM16");
      return { reader, wav, initial: preamble.subarray(wav.dataOffset) };
    }

    try {
      const startPromise = openPcmStream(streamInfo.startUrl);
      const separateLoop = !!streamInfo.loopUrl;
      const loopPromise = separateLoop ? openPcmStream(streamInfo.loopUrl) : null;
      const opened = await Promise.all(loopPromise ? [startPromise, loopPromise] : [startPromise]);
      const startStream = opened[0];
      const loopStream = opened[1] || null;
      if (requestId != null && requestId !== playRequestRef.current) {
        pendingRequest.abort();
        return true;
      }

      const sampleRate = Math.max(1, Math.trunc(Number(streamInfo.sampleRate) || 1));
      const totalFrames = Math.max(0, Math.trunc(Number(streamInfo.totalFrames) || 0));
      const startFrame = Math.max(0, Math.min(totalFrames, Math.trunc(Number(streamInfo.startFrame) || 0)));
      const loopStartFrame = Math.max(0, Math.min(totalFrames, Math.trunc(Number(streamInfo.loopStartFrame) || 0)));
      const expectedInitialFrames = totalFrames - startFrame;
      const expectedLoopFrames = totalFrames - loopStartFrame;
      if (expectedLoopFrames <= 0) throw new Error("BRSTM loop range is empty");

      try {
        context = new AudioContextClass({ latencyHint: "interactive", sampleRate });
      } catch (_) {
        context = new AudioContextClass();
      }
      const gain = context.createGain();
      gain.gain.value = volume;
      gain.connect(context.destination);
      const clock = new ClockClass(sampleRate, startFrame, loopStartFrame, totalFrames, 0);
      let clockAnchored = false;
      const initialSources = new Set();
      const loopPassRecords = [];
      let lastInitialSource = null;
      // Allocate the final loop buffer once and fill it as PCM packets arrive.
      // This avoids retaining a second giant byte array and avoids a large
      // main-thread conversion spike at the first loop boundary.
      const loopBuffer = context.createBuffer(2, expectedLoopFrames, sampleRate);
      let loopSource = null;
      let loopSourceStartPass = null;
      let initialComplete = false;
      let loopComplete = false;
      let active = false;
      let ended = false;
      let loopEnabled = !!strmPlaybackBySoundRef.current[soundId]?.loopEnabled;
      let loopExitContextTime = null;
      let loopExitFrame = totalFrames;
      let firstScheduledResolve;
      const firstScheduled = new Promise((resolve) => { firstScheduledResolve = resolve; });
      let firstWasScheduled = false;
      let loopPrimedResolve;
      let loopPrimedFlag = !separateLoop;
      const loopPrimed = new Promise((resolve) => { loopPrimedResolve = resolve; });
      if (loopPrimedFlag) loopPrimedResolve();

      function isCurrent() {
        return !closed
          && adapter === audioRef.current
          && (requestId == null || requestId === playRequestRef.current);
      }
      function makeBuffer(bytes) {
        const frames = Math.floor(bytes.length / 4);
        const buffer = context.createBuffer(2, frames, sampleRate);
        const view = new DataView(bytes.buffer, bytes.byteOffset, frames * 4);
        const left = buffer.getChannelData(0);
        const right = buffer.getChannelData(1);
        for (let frame = 0; frame < frames; frame += 1) {
          left[frame] = view.getInt16(frame * 4, true) / 32768;
          right[frame] = view.getInt16(frame * 4 + 2, true) / 32768;
        }
        return buffer;
      }
      function stopSource(source) {
        if (!source) return;
        try { source.stop(); } catch (_) {}
        try { source.disconnect(); } catch (_) {}
      }
      function finishPlayback() {
        if (closed || ended) return;
        ended = true;
        active = false;
        if (isCurrent() && onEnded) onEnded(Math.round(totalFrames * 1000 / sampleRate));
      }
      function failPlayback(message) {
        if (closed) return;
        if (isCurrent()) setOpenError(message);
        adapter?.load();
        setIsPlaying(false);
        setPlayingId(null);
      }
      function copyIntoLoopBuffer(buffer, sourceFrame, frameCount, destinationFrame) {
        if (frameCount <= 0) return;
        for (let channel = 0; channel < 2; channel += 1) {
          loopBuffer.copyToChannel(
            buffer.getChannelData(channel).subarray(sourceFrame, sourceFrame + frameCount),
            channel,
            destinationFrame,
          );
        }
      }
      function anchorClock() {
        if (clockAnchored) return;
        // Choose the audio epoch only after real PCM is ready. A slow header or
        // first decode can therefore never make scheduled times stale.
        clock.anchor(context.currentTime, 0.03);
        clockAnchored = true;
        if (loopComplete) scheduleLoopBuffer(0);
        else for (const record of loopPassRecords) scheduleLoopRecord(record);
      }
      function scheduleInitial(buffer, localFrame) {
        if (!buffer?.length || closed) return null;
        anchorClock();
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.connect(gain);
        const when = clock.initialTime(localFrame);
        source.start(when);
        initialSources.add(source);
        source.onended = () => {
          initialSources.delete(source);
          source.disconnect();
          if (source !== lastInitialSource || closed) return;
          if (!loopEnabled) finishPlayback();
          else if (!loopSource && !loopPassRecords.some((record) => record.source)) {
            failPlayback("BRSTM loop data was not ready at the sample boundary");
          }
        };
        lastInitialSource = source;
        if (!separateLoop) {
          const absoluteStart = startFrame + localFrame;
          const absoluteEnd = absoluteStart + buffer.length;
          const overlapStart = Math.max(loopStartFrame, absoluteStart);
          const overlapEnd = Math.min(totalFrames, absoluteEnd);
          if (overlapEnd > overlapStart) {
            copyIntoLoopBuffer(
              buffer,
              overlapStart - absoluteStart,
              overlapEnd - overlapStart,
              overlapStart - loopStartFrame,
            );
          }
        }
        if (!firstWasScheduled) {
          firstWasScheduled = true;
          firstScheduledResolve();
        }
        return source;
      }
      function scheduleLoopRecord(record) {
        if (record.source || !loopEnabled || closed) return;
        if (!clockAnchored) {
          if (expectedInitialFrames !== 0) return;
          anchorClock();
          if (record.source) return;
        }
        const when = clock.loopTime(record.localFrame, 0);
        if (when <= context.currentTime + 0.001) return;
        const source = context.createBufferSource();
        source.buffer = record.buffer;
        source.connect(gain);
        source.start(when);
        record.source = source;
        source.onended = () => {
          if (record.source === source) record.source = null;
          source.disconnect();
          const isLast = loopComplete && record === loopPassRecords[loopPassRecords.length - 1];
          if (
            isLast
            && context.currentTime >= clock.firstEndTime
            && !loopSource
            && !loopEnabled
          ) finishPlayback();
        };
        if (!firstWasScheduled && expectedInitialFrames === 0) {
          firstWasScheduled = true;
          firstScheduledResolve();
        }
      }
      function cancelLoopPass() {
        for (const record of loopPassRecords) {
          stopSource(record.source);
          record.source = null;
        }
      }
      function scheduleLoopBuffer(passIndex) {
        if (!loopComplete || !loopEnabled || closed) return false;
        const when = clock.loopTime(0, passIndex);
        if (when <= context.currentTime + 0.001) return false;
        stopSource(loopSource);
        const source = context.createBufferSource();
        source.buffer = loopBuffer;
        source.loop = true;
        source.loopStart = 0;
        source.loopEnd = expectedLoopFrames / sampleRate;
        source.connect(gain);
        source.start(when);
        source.onended = () => {
          if (source !== loopSource || closed) return;
          loopSource = null;
          if (!loopEnabled) finishPlayback();
        };
        loopSource = source;
        loopSourceStartPass = passIndex;
        return true;
      }
      function installCompletedLoop(frameCount) {
        if (closed) return;
        if (frameCount !== expectedLoopFrames) {
          throw new Error(`Loop PCM length mismatch (${frameCount} != ${expectedLoopFrames})`);
        }
        loopComplete = true;
        if (!clockAnchored) return;
        if (
          separateLoop
          && !loopEnabled
          && clockAnchored
          && context.currentTime + 0.001 >= clock.loopTime(expectedLoopFrames, 0)
          && !loopSource
        ) {
          finishPlayback();
          return;
        }
        // If the first loop pass has not begun, replace its many progressive
        // nodes with one sample-exact looping source at the same boundary.
        if (clock.firstEndTime > context.currentTime + 0.01) {
          cancelLoopPass();
          scheduleLoopBuffer(0);
        } else {
          scheduleLoopBuffer(1);
        }
      }
      function setLoopEnabled(value) {
        const next = !!value && expectedLoopFrames > 0;
        if (next === loopEnabled) return;
        if (!next && context.currentTime >= clock.firstEndTime) {
          loopExitContextTime = context.currentTime;
          loopExitFrame = clock.absoluteFrameAt(context.currentTime, true);
        } else if (next) {
          loopExitContextTime = null;
        }
        loopEnabled = next;
        ended = false;
        if (next) {
          if (loopSource) {
            loopSource.loop = true;
          } else if (loopComplete) {
            const pass = context.currentTime < clock.firstEndTime ? 0 : 1;
            scheduleLoopBuffer(pass);
          } else {
            for (const record of loopPassRecords) scheduleLoopRecord(record);
          }
          return;
        }
        if (context.currentTime < clock.firstEndTime) {
          cancelLoopPass();
          stopSource(loopSource);
          loopSource = null;
          loopSourceStartPass = null;
        } else if (loopSource) {
          const sourceStart = clock.loopTime(0, loopSourceStartPass || 0);
          if (context.currentTime + 0.001 < sourceStart) {
            // A progressive loop pass is already the current iteration; a
            // future buffer source would add one unwanted extra pass.
            stopSource(loopSource);
            loopSource = null;
            loopSourceStartPass = null;
          } else {
            // Match AudioBufferSourceNode.loop semantics: finish the currently
            // audible iteration, then end without a restart or phase jump.
            loopSource.loop = false;
          }
        }
      }

      async function pumpStream(opened, kind) {
        const expectedFrames = kind === "initial" ? expectedInitialFrames : expectedLoopFrames;
        let byteLength = 0;
        let pending = new Uint8Array(0);
        let scheduledFrames = 0;
        let firstBatch = true;

        function consume(bytes, flush = false) {
          if (bytes?.length) {
            byteLength += bytes.length;
            pending = appendBytes(pending, bytes);
          }
          while (true) {
            const batchFrames = firstBatch ? 4096 : 32768;
            const availableFrames = Math.floor(pending.length / 4);
            if (availableFrames < batchFrames && !flush) break;
            if (availableFrames <= 0) break;
            const frames = flush ? availableFrames : batchFrames;
            const byteCount = frames * 4;
            const batch = pending.slice(0, byteCount);
            pending = pending.slice(byteCount);
            const buffer = makeBuffer(batch);
            if (kind === "initial") {
              scheduleInitial(buffer, scheduledFrames);
            } else {
              copyIntoLoopBuffer(buffer, 0, frames, scheduledFrames);
              const record = { buffer, localFrame: scheduledFrames, source: null };
              loopPassRecords.push(record);
              if (!loopPrimedFlag) {
                // For a seek already inside the loop, do not start even a tiny
                // suffix until the first loop PCM is ready. Both HTTP streams
                // are opened in parallel, so this preserves quick startup and
                // guarantees that the first boundary cannot underrun.
                loopPrimedFlag = true;
                loopPrimedResolve();
              }
              scheduleLoopRecord(record);
            }
            scheduledFrames += frames;
            firstBatch = false;
          }
        }

        consume(opened.initial);
        while (!closed) {
          const part = await opened.reader.read();
          if (part.done) break;
          consume(part.value);
          if (scheduledFrames / sampleRate > 0.25) {
            await new Promise((resolve) => window.setTimeout(resolve, 0));
          }
        }
        if (closed) return null;
        consume(new Uint8Array(0), true);
        if (pending.length) throw new Error("BRSTM PCM stream ended on a partial frame");
        if (scheduledFrames !== expectedFrames || byteLength !== expectedFrames * 4) {
          throw new Error(`BRSTM PCM length mismatch (${scheduledFrames} != ${expectedFrames})`);
        }
        if (kind === "initial") {
          initialComplete = true;
          if (lastInitialSource && !loopEnabled) {
            // onended already observes the current loop state.
          }
          if (!separateLoop) {
            installCompletedLoop(expectedLoopFrames);
          }
        } else {
          installCompletedLoop(scheduledFrames);
        }
        return null;
      }

      const loopPump = loopStream ? pumpStream(loopStream, "loop") : Promise.resolve(null);
      const initialPump = separateLoop
        ? Promise.race([
          loopPrimed,
          loopPump.then(() => {
            if (!loopPrimedFlag) throw new Error("BRSTM loop stream contains no playable PCM frames");
          }),
        ]).then(() => pumpStream(startStream, "initial"))
        : pumpStream(startStream, "initial");
      const pumps = Promise.all([initialPump, loopPump]);
      pumps.catch((error) => {
        if (closed || error?.name === "AbortError") return;
        console.warn("Progressive BRSTM stream failed:", error);
        if (isCurrent()) {
          setOpenError(String(error));
          adapter.load();
          setIsPlaying(false);
          setPlayingId(null);
        }
      });

      await Promise.race([
        firstScheduled,
        pumps.then(() => {
          if (!firstWasScheduled) throw new Error("BRSTM stream contains no playable PCM frames");
        }),
      ]);
      if (requestId != null && requestId !== playRequestRef.current) {
        closed = true;
        pendingRequest.abort();
        context.close().catch(() => {});
        return true;
      }

      adapter = {
        isProgressiveStrmLoop: true,
        get ended() { return ended; },
        get loopEnabled() { return loopEnabled; },
        get duration() { return totalFrames / sampleRate; },
        get currentTime() {
          if (!loopEnabled && loopExitContextTime != null) {
            const elapsedFrames = Math.max(
              0,
              Math.floor((context.currentTime - loopExitContextTime) * sampleRate + 1.0e-7),
            );
            return Math.min(totalFrames, loopExitFrame + elapsedFrames) / sampleRate;
          }
          return clock.absoluteFrameAt(context.currentTime, loopEnabled) / sampleRate;
        },
        set currentTime(_) {},
        get volume() { return gain.gain.value; },
        set volume(value) { gain.gain.value = Math.max(0, Math.min(1, Number(value) || 0)); },
        setLoopEnabled,
        play() {
          if (closed) return Promise.reject(new Error("Audio context is closed"));
          active = true;
          return context.resume();
        },
        pause() {
          if (!closed) {
            active = false;
            context.suspend().catch(() => {});
          }
        },
        load() {
          if (closed) return;
          closed = true;
          active = false;
          pendingRequest.abort();
          for (const source of initialSources) stopSource(source);
          initialSources.clear();
          cancelLoopPass();
          stopSource(loopSource);
          loopSource = null;
          gain.disconnect();
          context.close().catch(() => {});
        },
      };
      audioRef.current = adapter;
      audioBaseRef.current = 0;
      if (loopingFetchAbortRef.current === pendingRequest) loopingFetchAbortRef.current = null;
      const exactDuration = Math.round(totalFrames * 1000 / sampleRate) || duration || 0;
      setDurationMs(exactDuration);
      setPlayheadMs(Math.round(startFrame * 1000 / sampleRate));
      setPlayingSound((current) => current ? { ...current, durationMs: exactDuration } : current);
      await adapter.play();
      if (adapter !== audioRef.current) return true;
      if (t0) console.log(`[PLAY] first BRSTM PCM scheduled: ${(performance.now()-t0).toFixed(0)}ms`);
      setIsPlaying(true);
      playheadTimerRef.current = window.setInterval(() => {
        if (adapter !== audioRef.current) return;
        setPlayheadMs(Math.round(adapter.currentTime * 1000));
      }, 50);
      return true;
    } catch (error) {
      pendingRequest.abort();
      if (loopingFetchAbortRef.current === pendingRequest) loopingFetchAbortRef.current = null;
      if (context && !adapter) context.close().catch(() => {});
      if (
        error?.name !== "AbortError"
        && (requestId == null || requestId === playRequestRef.current)
      ) console.warn("Progressive BRSTM fallback:", error);
      return requestId != null && requestId !== playRequestRef.current;
    }
  }
  function setStrmPlayback(soundId, patch) {
    const current = strmPlaybackBySoundRef.current[soundId] || {};
    const nextValue = { ...current, ...patch };
    const next = { ...strmPlaybackBySoundRef.current, [soundId]: nextValue };
    strmPlaybackBySoundRef.current = next;
    setStrmPlaybackBySound(next);
    return nextValue;
  }
  function setSeqPlayback(soundId, patch) {
    const current = seqPlaybackBySoundRef.current[soundId] || {};
    const nextValue = { ...current, ...patch };
    const next = { ...seqPlaybackBySoundRef.current, [soundId]: nextValue };
    seqPlaybackBySoundRef.current = next;
    setSeqPlaybackBySound(next);
    return nextValue;
  }
  function applySeqPlaybackMetadata(soundId, metadata) {
    if (!metadata) return seqPlaybackBySoundRef.current[soundId] || {};
    const current = seqPlaybackBySoundRef.current[soundId] || {};
    const looped = !!metadata.looped;
    const loopEnabled = looped && !!current.loopEnabled && !soundListAutoPlayEnabledRef.current;
    const next = setSeqPlayback(soundId, {
      loading: false,
      looped,
      loopStartMs: Math.max(0, Number(metadata.loopStartMs) || 0),
      loopEndMs: Math.max(0, Number(metadata.loopEndMs) || 0),
      loopStartFrame: Math.max(0, Number(metadata.loopStartFrame) || 0),
      loopEndFrame: Math.max(0, Number(metadata.loopEndFrame) || 0),
      sampleRate: Math.max(1, Number(metadata.sampleRate) || 32000),
      loopEnabled,
    });
    if (audioRef.current?.sequenceSoundId === soundId) {
      audioRef.current.setSequenceLoopMetadata?.(next);
    }
    return next;
  }
  function applyStrmPlaybackMetadata(soundId, metadata) {
    const tracks = Array.isArray(metadata?.tracks) ? metadata.tracks : [];
    const current = strmPlaybackBySoundRef.current[soundId] || {};
    const looped = !!metadata?.looped;
    const loopLayoutChanged = current.looped !== looped;
    const preferredLoopEnabled = loopLayoutChanged ? looped : (typeof current.loopEnabled === "boolean" ? current.loopEnabled : looped);
    const loopEnabled = soundListAutoPlayEnabledRef.current ? false : preferredLoopEnabled;
    const selectedTrackIndices = window.PysarStrmTrackSelection(tracks, current.selectedTrackIndices);
    return setStrmPlayback(soundId, {
      looped,
      loopStartMs: Math.max(0, Number(metadata?.loopStartMs) || 0),
      tracks,
      selectedTrackIndices,
      loopEnabled,
    });
  }
  function loadStrmPlaybackMetadata(sound) {
    if (!sound || sound.type !== "STRM" || !window.pysar) return;
    const soundId = Number(sound.id);
    if (!Number.isInteger(soundId) || soundId < 0) return;
    if (strmPlaybackBySoundRef.current[soundId]?.tracks?.length) return;
    if (strmPlaybackLoadsRef.current.has(soundId)) return;

    const revision = strmPlaybackRevisionRef.current;
    strmPlaybackLoadsRef.current.add(soundId);
    window.pysar.call("get_strm_playback_metadata", soundId).then((result) => {
      if (revision !== strmPlaybackRevisionRef.current || !result?.ok || !result.strmPlayback) return;
      applyStrmPlaybackMetadata(soundId, result.strmPlayback);
    }).catch(() => {}).finally(() => {
      strmPlaybackLoadsRef.current.delete(soundId);
    });
  }
  function changeStrmLoop(loopEnabled) {
    const sound = playingSoundRef.current || playingSound;
    if (!sound || sound.type !== "STRM") return;
    setStrmPlayback(sound.id, { loopEnabled: !!loopEnabled });
    if (loopEnabled) {
      soundListAutoPlayEnabledRef.current = false;
      setSoundListAutoPlayEnabled(false);
    }
    if (playingId !== sound.id) return;
    // Both BRSTM Web Audio adapters own their audio clock, so loop state can
    // change without a refetch, restart, or phase jump.
    audioRef.current?.setLoopEnabled?.(loopEnabled);
    if (audioRef.current?.isWebAudioLoop || audioRef.current?.isProgressiveStrmLoop) {
      setPlayheadMs(Math.round(audioRef.current.currentTime * 1000));
    }
  }
  function changeSeqLoop(loopEnabled) {
    const sound = playingSoundRef.current || playingSound;
    if (!sound || sound.type !== "SEQ") return;
    const current = seqPlaybackBySoundRef.current[sound.id] || {};
    if (!current.looped) return;
    const enabled = !!loopEnabled;
    setSeqPlayback(sound.id, { loopEnabled: enabled });
    if (enabled) {
      soundListAutoPlayEnabledRef.current = false;
      setSoundListAutoPlayEnabled(false);
    }
    audioRef.current?.setLoopEnabled?.(enabled);
  }
  function changeStrmTrackSelection(selectedTrackIndices) {
    if (!playingSound || playingSound.type !== "STRM") return;
    const current = strmPlaybackBySoundRef.current[playingSound.id] || {};
    const selection = window.PysarStrmTrackSelection(current.tracks, selectedTrackIndices);
    const next = setStrmPlayback(playingSound.id, { selectedTrackIndices: selection });
    if (playingId !== playingSound.id) return;
    if (isPlaying) {
      playStrmTrackTransition(playingSound, playheadMsRef.current, next.selectedTrackIndices);
      return;
    }
    // A paused stream must be regenerated before it is resumed so the new
    // selection is heard rather than the old audio element's channel mix.
    playRequestRef.current += 1;
    clearAudio();
  }
  function changeSoundListAutoPlay(autoPlayEnabled) {
    const sound = playingSoundRef.current || playingSound;
    if (!window.PysarIsSoundListTransport(sound)) return;
    const enabled = !!autoPlayEnabled;
    soundListAutoPlayEnabledRef.current = enabled;
    setSoundListAutoPlayEnabled(enabled);
    const current = sound.type === "STRM" ? (strmPlaybackBySoundRef.current[sound.id] || {}) : null;
    if (enabled && current?.loopEnabled) {
      setStrmPlayback(sound.id, { loopEnabled: false });
      audioRef.current?.setLoopEnabled?.(false);
      if (audioRef.current?.isWebAudioLoop || audioRef.current?.isProgressiveStrmLoop) {
        setPlayheadMs(Math.round(audioRef.current.currentTime * 1000));
      }
    }
    const sequencePlayback = sound.type === "SEQ" ? (seqPlaybackBySoundRef.current[sound.id] || {}) : null;
    if (enabled && sequencePlayback?.loopEnabled) {
      setSeqPlayback(sound.id, { loopEnabled: false });
      audioRef.current?.setLoopEnabled?.(false);
    }
  }
  function sequenceVariationFor(sound) {
    if (!sound) return null;
    if (Object.prototype.hasOwnProperty.call(sound, "seqVariation")) {
      return sound.seqVariation || null;
    }
    return seqVariationBySound[sound.id] || null;
  }
  function advanceToNextVisibleSound(currentSound, options = {}) {
    if (!currentSound || currentSound.kind === "wave" || currentSound.kind === "bank_note") return false;
    const nextSoundId = window.PysarNextVisibleSoundId(
      visibleSoundIdsRef.current,
      currentSound.id,
    );
    if (nextSoundId == null) return false;
    const nextSound = (window.PYSAR_DATA?.sounds || []).find((sound) => Number(sound.id) === nextSoundId);
    if (!nextSound) return false;

    setTabs((currentTabs) => window.PysarFollowAutoplayInSoundTab(
      currentTabs,
      activeTabRef.current,
      currentSound.id,
      nextSound,
    ));
    setSelectedItem({ kind: "sound", id: nextSound.id, name: nextSound.name, item: nextSound });
    if (options.resume) play(nextSound, 0, true);
    else queueSoundForPreview(nextSound);
    return true;
  }
  async function play(s, offsetMs = 0, force = false, explicitStrmTrackIndices = null, playbackOptions = null) {
    // A direct playback request supersedes any pending authored-track jump.
    // playStrmTrackTransition installs its new token immediately afterwards.
    strmTrackTransitionRef.current = null;
    if (!force && playingId === s.id && isPlaying) {
      pause();
      return;
    }
    const t0 = performance.now();
    const requestId = ++playRequestRef.current;
    const variation = sequenceVariationFor(s);
    const transportSound = variation ? { ...s, seqVariation: variation } : s;
    const strmTrackIndices = transportSound.type === "STRM"
      ? (explicitStrmTrackIndices ?? strmPlaybackBySoundRef.current[transportSound.id]?.selectedTrackIndices ?? null)
      : null;
    durationTargetRef.current = transportSound.id;
    const currentDuration = transportSound.durationMs || (playingSound?.id === transportSound.id ? durationMs : 0);
    playingSoundRef.current = transportSound;
    if (transportSound.type === "SEQ" && !seqPlaybackBySoundRef.current[transportSound.id]) {
      setSeqPlayback(transportSound.id, { loading: true, looped: false, loopEnabled: false });
    }
    setPlayingSound(transportSound);
    setPlayingId(s.id);
    setPlayheadMs(offsetMs);
    setDurationMs(currentDuration || 0);
    if (!window.pysar) return;
    const result = await window.pysar.call(
      "get_sound_stream_url",
      s.id,
      variation?.note ?? null,
      variation?.program ?? null,
      variation?.randomOverrides || null,
      offsetMs,
      strmTrackIndices,
    ).catch((error) => ({ ok: false, error: String(error) }));
    const t1 = performance.now();
    console.log(`[PLAY] bridge call: ${(t1-t0).toFixed(0)}ms  type=${s.type}  name=${s.name}  durationMs=${result.durationMs}`);
    if (!result.ok) {
      setOpenError(result.error || "Playback failed");
      setIsPlaying(false);
      setPlayingId(null);
      return;
    }
    if (requestId !== playRequestRef.current) return;
    if (transportSound.type === "SEQ" && result.seqPlayback) {
      applySeqPlaybackMetadata(transportSound.id, result.seqPlayback);
    }
    if (transportSound.type === "STRM" && result.strmPlayback) {
      applyStrmPlaybackMetadata(transportSound.id, result.strmPlayback);
    }
    const streamPlayback = strmPlaybackBySoundRef.current[transportSound.id];
    const handlePlaybackEnded = (endedDuration) => {
      // Every audio adapter calls this only while it still owns audioRef. The
      // request check additionally prevents stop, seek-to-end, or a sound
      // switch from advancing an obsolete BRSTM playback.
      if (requestId !== playRequestRef.current) return;
      if (playheadTimerRef.current) {
        window.clearInterval(playheadTimerRef.current);
        playheadTimerRef.current = null;
      }
      const playback = strmPlaybackBySoundRef.current[transportSound.id];
      if (transportSound.type === "STRM" && playback?.looped && playback.loopEnabled) {
        const audio = audioRef.current;
        const loopStartMs = playback.loopStartMs || 0;
        if (audio && audioBaseRef.current === 0 && Number.isFinite(audio.duration)) {
          audio.currentTime = Math.min(loopStartMs / 1000, Math.max(0, audio.duration - 0.001));
          setPlayheadMs(loopStartMs);
          audio.play().then(() => setIsPlaying(true)).catch((error) => {
            setOpenError(String(error));
            setIsPlaying(false);
          });
        } else {
          play(transportSound, loopStartMs, true, playback.selectedTrackIndices);
        }
      } else if (
        transportSound.type === "SEQ"
        && seqPlaybackBySoundRef.current[transportSound.id]?.looped
        && seqPlaybackBySoundRef.current[transportSound.id]?.loopEnabled
      ) {
        const sequencePlayback = seqPlaybackBySoundRef.current[transportSound.id];
        play(transportSound, sequencePlayback.loopStartMs || 0, true);
      } else {
        if (
          soundListAutoPlayEnabledRef.current
          && advanceToNextVisibleSound(transportSound, { resume: true })
        ) return;
        setIsPlaying(false);
        setPlayheadMs(endedDuration || result.durationMs || currentDuration || 0);
        setPlayingId(null);
      }
    };
    // Start looped BRSTM playback from the first decoded PCM block. The first
    // pass and loop pass share one integer-frame AudioContext clock, while the
    // complete loop buffer is assembled in the background for later cycles.
    if (transportSound.type === "STRM" && streamPlayback?.looped) {
      if (result.progressiveStrm) {
        const progressiveAttached = await attachProgressiveLoopingAudio(
          result.progressiveStrm,
          result.durationMs || currentDuration,
          offsetMs,
          requestId,
          transportSound.id,
          handlePlaybackEnded,
          t1,
        );
        if (progressiveAttached || requestId !== playRequestRef.current) return;
      }
      // Browsers without a streaming fetch/AudioContext path retain the
      // complete-buffer implementation as a correctness-first fallback.
      const attached = await attachLoopingAudio(
        result.url,
        result.durationMs || currentDuration,
        streamPlayback.loopStartMs || 0,
        offsetMs,
        requestId,
        transportSound.id,
        handlePlaybackEnded,
      );
      if (attached || requestId !== playRequestRef.current) return;
    }
    if (transportSound.type === "SEQ") {
      const streamDuration = result.durationMs || currentDuration;
      const attached = await attachProgressivePcmAudio(
        result.url,
        streamDuration,
        offsetMs,
        requestId,
        handlePlaybackEnded,
        () => {
          if (requestId !== playRequestRef.current) return;
          // A browser without a reliable streaming fetch path can still use
          // its native WAV decoder. This is deliberately a one-way fallback.
          attachAudio(result.url, streamDuration, offsetMs, performance.now(), handlePlaybackEnded);
        },
        t1,
        seqPlaybackBySoundRef.current[transportSound.id] || null,
        transportSound.id,
        playbackOptions,
      );
      if (attached || requestId !== playRequestRef.current) return;
    }
    attachAudio(
      result.url,
      result.durationMs || currentDuration,
      offsetMs,
      t1,
      handlePlaybackEnded,
      transportSound.type === "STRM",
    );
  }
  function playStrmTrackTransition(sound, offsetMs, selectedTrackIndices) {
    const pending = play(sound, offsetMs, true, selectedTrackIndices);
    const requestId = playRequestRef.current;
    strmTrackTransitionRef.current = requestId;
    const clearOwnToken = () => {
      if (strmTrackTransitionRef.current === requestId) {
        strmTrackTransitionRef.current = null;
      }
    };
    Promise.resolve(pending).then(clearOwnToken, clearOwnToken);
    return pending;
  }
  function pause() {
    const audio = audioRef.current;
    audio?.pause();
    if (audio?.ended || strmTrackTransitionRef.current === playRequestRef.current) {
      // A natural end may already be awaiting the next autoplay URL.
      // Invalidate that request so Pause during the transition stays paused.
      playRequestRef.current += 1;
      strmTrackTransitionRef.current = null;
      clearAudio();
    }
    setIsPlaying(false);
  }
  function stopBackendPlayback() {
    if (window.pysar) window.pysar.call("stop").catch(() => {});
  }
  function stop() {
    playRequestRef.current += 1;
    strmTrackTransitionRef.current = null;
    clearAudio();
    setIsPlaying(false);
    setPlayingId(null);
    setPlayheadMs(0);
    stopBackendPlayback();
  }
  function queueSoundForPreview(s) {
    if (window.PysarShouldPreserveSoundTransport?.(
      playingSoundRef.current,
      s,
      isPlaying,
    )) {
      return;
    }
    const variation = sequenceVariationFor(s);
    const transportSound = variation ? { ...s, seqVariation: variation } : s;
    if (transportSound.type === "SEQ" && !seqPlaybackBySoundRef.current[transportSound.id]) {
      setSeqPlayback(transportSound.id, { loading: true, looped: false, loopEnabled: false });
    }
    playingSoundRef.current = transportSound;
    playRequestRef.current += 1;
    strmTrackTransitionRef.current = null;
    const durationRequestId = ++durationRequestRef.current;
    durationTargetRef.current = transportSound.id;
    clearAudio();
    setPlayingSound(transportSound);
    setPlayingId(null);
    setIsPlaying(false);
    setPlayheadMs(0);
    setDurationMs(0);
    stopBackendPlayback();
    loadStrmPlaybackMetadata(transportSound);
    warmSoundPreview(transportSound, variation);
    if (!window.pysar) return;
    window.pysar.call(
      "get_sound_duration",
      s.id,
      variation?.note ?? null,
      variation?.program ?? null,
      variation?.randomOverrides || null
    ).then((result) => {
      if (durationRequestId !== durationRequestRef.current) return;
      if (durationTargetRef.current !== transportSound.id) return;
      if (!result?.ok) return;
      if (result.seqPlayback) applySeqPlaybackMetadata(transportSound.id, result.seqPlayback);
      const nextDuration = Math.max(0, Math.round(result.durationMs || 0));
      setDurationMs(nextDuration);
      setPlayingSound((current) => {
        if (!current || current.id !== transportSound.id) return current;
        return { ...current, durationMs: nextDuration };
      });
    }).catch(() => {});
  }
  function warmSoundPreview(s, explicitVariation) {
    if (!window.pysar || !s || s.kind === "wave") return;
    const variation = arguments.length >= 2 ? explicitVariation : sequenceVariationFor(s);
    window.pysar.call(
      "warm_sound_preview",
      s.id,
      variation?.note ?? null,
      variation?.program ?? null,
      variation?.randomOverrides || null
    ).catch(() => {});
  }
  function chooseSeqVariation(sound, variation) {
    if (!sound) return;
    setSeqVariationBySound((current) => {
      const next = { ...current };
      if (variation) next[sound.id] = variation;
      else delete next[sound.id];
      return next;
    });
    const baseSound = { ...sound };
    delete baseSound.seqVariation;
    const transportSound = { ...baseSound, seqVariation: variation || null };
    if (playingSoundRef.current?.id === sound.id) playingSoundRef.current = transportSound;
    setPlayingSound((current) => {
      if (!current || current.id !== sound.id) return current;
      return transportSound;
    });
    warmSoundPreview(transportSound, variation);
    if (playingSound?.id === sound.id && isPlaying) {
      play(transportSound, 0, true);
    } else if (playingSound?.id === sound.id) {
      // Do not let Play resume the paused audio element for the old variation.
      playRequestRef.current += 1;
      clearAudio();
      setIsPlaying(false);
      setPlayheadMs(0);
      setDurationMs(0);
    }
  }
  function audioHasBufferedTime(audio, seconds) {
    if (audio?.isWebAudioLoop) return true;
    const ranges = audio?.buffered;
    if (!ranges || !Number.isFinite(seconds) || seconds < 0) return false;
    for (let index = 0; index < ranges.length; index += 1) {
      if (seconds >= ranges.start(index) && seconds <= ranges.end(index)) return true;
    }
    return false;
  }
  function seek(ms, options = {}) {
    const next = Math.max(0, Math.min(durationMs || 0, ms));
    const shouldResume = options?.resume ?? isPlaying;
    setPlayheadMs(next);
    if (!playingSound) return;
    if (next >= (durationMs || 0)) {
      if (shouldResume) {
        stop();
      } else {
        playRequestRef.current += 1;
        clearAudio();
        setIsPlaying(false);
        setPlayheadMs(durationMs || 0);
      }
      return;
    }
    // Seek inside the element we already have. The backend serves byte ranges
    // only once that time is actually buffered. Asking an in-progress WAV for
    // an unbuffered range makes WebKit/Chromium download from the beginning.
    // In that case a new offset-native renderer is substantially faster.
    const audio = audioRef.current;
    const localSeconds = (next - audioBaseRef.current) / 1000;
    if (
      audio
      && Number.isFinite(audio.duration)
      && audio.duration > 0
      && audioHasBufferedTime(audio, localSeconds)
    ) {
      audio.currentTime = Math.min(localSeconds, Math.max(0, audio.duration - 0.001));
      if (shouldResume) {
        audio.play().then(() => setIsPlaying(true)).catch(() => {});
      } else {
        audio.pause();
        setIsPlaying(false);
      }
      return;
    }
    if (!shouldResume) {
      playRequestRef.current += 1;
      clearAudio();
      return;
    }
    if (playingSound.kind === "wave") {
      playWave({
        ...playingSound,
        index: playingSound.waveIndex,
        encoding: playingSound.type,
        durationMs,
      }, next, true);
    } else if (playingSound.kind === "bank_note") {
      playBankNote(playingSound, playingSound.program, playingSound.note, next);
    } else {
      play(playingSound, next, true);
    }
  }
  function nextTransportSound() {
    const sound = playingSoundRef.current || playingSound;
    if (advanceToNextVisibleSound(sound, { resume: isPlaying })) return;
    seek(durationMs, { resume: isPlaying });
  }
  function resumeCurrent() {
    const currentSound = playingSoundRef.current || playingSound;
    if (!currentSound) return;
    if (audioRef.current && !audioRef.current.ended) {
      audioRef.current.play().then(() => setIsPlaying(true)).catch((error) => setOpenError(String(error)));
      return;
    }
    const resumeMs = audioRef.current?.ended ? 0 : playheadMsRef.current;
    if (currentSound.kind === "wave") {
      playWave({
        ...currentSound,
        index: currentSound.waveIndex,
        encoding: currentSound.type,
        durationMs,
      }, resumeMs, true);
    } else if (currentSound.kind === "bank_note") {
      playBankNote(currentSound, currentSound.program, currentSound.note, resumeMs);
    } else {
      play(currentSound, resumeMs, true);
    }
  }
  function changeVolume(next) {
    const value = Math.max(0, Math.min(1, Number(next) || 0));
    setVolume(value);
    if (audioRef.current) audioRef.current.volume = value;
    setPlayingSound((current) => current ? { ...current, volume: Math.round(value * 100) } : current);
  }
  async function playWave(it, offsetMs = 0, force = false) {
    const wave = it?.item || it;
    if (!wave) return;
    const key = `wave:${wave.archiveId}:${wave.index}`;
    if (!force && playingId === key && isPlaying) {
      pause();
      return;
    }
    const requestId = ++playRequestRef.current;
    const item = waveTransportItem(wave);
    playingSoundRef.current = item;
    durationTargetRef.current = item.id;
    setPlayingSound(item);
    setPlayingId(key);
    setPlayheadMs(offsetMs);
    setDurationMs(item.durationMs);
    if (!window.pysar) return;
    const result = await window.pysar.call("get_wave_sample_stream_url", wave.archiveId, wave.index, offsetMs)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result.ok) {
      setOpenError(result.error || "Wave playback failed");
      setIsPlaying(false);
      return;
    }
    if (requestId !== playRequestRef.current) return;
    attachAudio(result.url, result.durationMs || item.durationMs, offsetMs);
  }
  function waveTransportItem(wave) {
    return {
      id: `wave:${wave.archiveId}:${wave.index}`,
      kind: "wave",
      name: `${wave.archiveName || "Wave archive"} · #${wave.index}`,
      type: wave.encoding || "WAVE",
      volume: Math.round(volume * 100),
      durationMs: wave.durationMs || 1000,
      archiveId: wave.archiveId,
      waveIndex: wave.index,
    };
  }
  async function playBankNote(bank, program, note, offsetMs = 0) {
    const bankId = Number(bank?.bankId ?? bank?.id);
    const bankName = bank?.bankName || bank?.name || `Bank ${bankId}`;
    const targetProgram = Math.max(0, Math.round(Number(program ?? bank?.program) || 0));
    const targetNote = Math.max(0, Math.min(127, Math.round(Number(note ?? bank?.note) || 0)));
    if (!Number.isInteger(bankId)) return;

    const id = `bank-note:${bankId}:${targetProgram}:${targetNote}`;
    const requestId = ++playRequestRef.current;
    const item = {
      id,
      kind: "bank_note",
      type: "BANK",
      name: `${bankName} · ${transportNoteName(targetNote)}`,
      volume: Math.round(volume * 100),
      durationMs: 0,
      bankId,
      bankName,
      program: targetProgram,
      note: targetNote,
    };
    durationTargetRef.current = id;
    clearAudio();
    playingSoundRef.current = item;
    setPlayingSound(item);
    setPlayingId(id);
    setIsPlaying(false);
    setPlayheadMs(offsetMs);
    setDurationMs(0);
    if (!window.pysar) return;
    const result = await window.pysar.call(
      "get_bank_note_stream_url", bankId, targetProgram, targetNote, 127, offsetMs
    ).catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      if (requestId !== playRequestRef.current) return;
      setOpenError(result?.error || "Bank-note playback failed");
      setPlayingId(null);
      return;
    }
    if (requestId !== playRequestRef.current) return;
    const duration = result.durationMs || 0;
    setPlayingSound((current) => current?.id === id ? { ...current, durationMs: duration } : current);
    attachAudio(result.url, duration, offsetMs);
  }
  function queueWaveForPreview(it) {
    const wave = it?.item || it;
    if (!wave) return;
    const item = waveTransportItem(wave);
    durationTargetRef.current = item.id;
    playRequestRef.current += 1;
    clearAudio();
    playingSoundRef.current = item;
    setPlayingSound(item);
    setPlayingId(null);
    setIsPlaying(false);
    setPlayheadMs(0);
    setDurationMs(item.durationMs);
    stopBackendPlayback();
  }

  async function refreshRecentArchives() {
    if (!window.pysar) return;
    const result = await window.pysar.call("get_recent_archives").catch(() => null);
    if (result?.ok && Array.isArray(result.recentArchives)) {
      setRecentArchives(result.recentArchives);
    }
  }

  function rememberActiveArchiveWorkspace() {
    const workspace = currentWorkspaceRef.current;
    const documentId = workspace?.documentId || activeDocumentIdRef.current;
    if (!documentId || !workspace) return;
    archiveWorkspacesRef.current[documentId] = {
      ...workspace,
      documentId,
      transportPlayheadMs: playheadMsRef.current,
    };
    if (window.PYSAR_DATA?.activeDocumentId === documentId) {
      archiveDataByDocumentRef.current[documentId] = window.PYSAR_DATA;
    }
  }

  function archiveDataWithDocumentState(data, documentId, documentList) {
    if (!data || !documentId) return null;
    const nextDocuments = Array.isArray(documentList) ? documentList : [];
    const activeDocument = nextDocuments.find((document) => document.id === documentId);
    return {
      ...data,
      activeDocumentId: documentId,
      documents: nextDocuments,
      archive: data.archive ? {
        ...data.archive,
        dirty: Boolean(activeDocument?.dirty ?? data.archive.dirty),
        safeMode: activeDocument?.safeMode !== false,
      } : data.archive,
    };
  }

  function refreshedWorkspace(workspace, data, documentId) {
    if (!workspace || (workspace.documentId && workspace.documentId !== documentId)) return null;
    const nextTabs = (workspace.tabs || []).map((tabItem) => {
      if (tabItem.kind === "view" || !tabItem.item) return tabItem;
      const item = refreshedDataItem(tabItem.kind, tabItem.item, data);
      return item === tabItem.item
        ? tabItem
        : { ...tabItem, item, title: item.name || item.label || tabItem.title };
    });
    const validTabs = nextTabs.length
      ? nextTabs
      : [{ id: "all", kind: "view", view: "all", title: "All sounds" }];
    const nextActiveTab = validTabs.some((tabItem) => tabItem.id === workspace.activeTab)
      ? workspace.activeTab
      : validTabs[0].id;
    const activeRestoredTab = validTabs.find((tabItem) => tabItem.id === nextActiveTab);
    const restoredSelection = refreshedSelection(workspace.selectedItem, data);
    const tabSelection = activeRestoredTab?.kind !== "view" && activeRestoredTab?.item
      ? {
          kind: activeRestoredTab.kind,
          id: activeRestoredTab.item.id,
          name: activeRestoredTab.item.name || activeRestoredTab.item.label || activeRestoredTab.title,
          item: activeRestoredTab.item,
        }
      : null;
    return {
      ...workspace,
      documentId,
      tabs: validTabs,
      activeTab: nextActiveTab,
      selectedItem: tabSelection || restoredSelection,
      history: (workspace.history || []).map((entry) => ({
        ...entry,
        selectedItem: refreshedSelection(entry.selectedItem, data),
      })),
    };
  }

  function acceptArchiveData(data, recent, { rememberCurrent = true, refreshRecent = true } = {}) {
    if (rememberCurrent) rememberActiveArchiveWorkspace();
    playRequestRef.current += 1;
    durationRequestRef.current += 1;
    clearAudio();
    stopBackendPlayback();
    window.PYSAR_DATA = data;
    setArchive(data.archive);
    setSafeMode(data.archive?.safeMode !== false);
    const nextDocuments = Array.isArray(data.documents) ? data.documents : [];
    const nextDocumentId = data.activeDocumentId || null;
    if (nextDocumentId) archiveDataByDocumentRef.current[nextDocumentId] = data;
    setDocuments(nextDocuments);
    setActiveDocumentId(nextDocumentId);
    activeDocumentIdRef.current = nextDocumentId;
    const activeDocument = nextDocuments.find((document) => document.id === nextDocumentId);
    setDirty(Boolean(activeDocument?.dirty ?? data.archive?.dirty));
    const initial = { tabId: "all", navView: "all", soundFilter: "ALL", selectedItem: null };
    const saved = nextDocumentId
      ? refreshedWorkspace(archiveWorkspacesRef.current[nextDocumentId], data, nextDocumentId)
      : null;
    setTabs(saved?.tabs || [{ id: "all", kind: "view", view: "all", title: "All sounds" }]);
    setActiveTab(saved?.activeTab || "all");
    setNavView(saved?.navView || "all");
    setSoundFilter(saved?.soundFilter || "ALL");
    setSelectedItem(saved?.selectedItem || null);
    setInspectorTab(saved?.inspectorTab || "props");
    const transportCandidate = saved?.transportSound
      || (saved?.selectedItem?.kind === "sound" ? saved.selectedItem.item : null);
    const restoredTransport = refreshedTransportSelection(transportCandidate, data);
    const restoredDuration = restoredTransport
      ? Math.max(0, Number(saved?.transportDurationMs ?? restoredTransport.durationMs) || 0)
      : 0;
    const restoredPlayhead = restoredTransport
      ? Math.max(0, Math.min(
          restoredDuration || Number.POSITIVE_INFINITY,
          Number(saved?.transportPlayheadMs) || 0,
        ))
      : 0;
    const pausedTransport = restoredTransport
      ? { ...restoredTransport, durationMs: restoredDuration }
      : null;
    playingSoundRef.current = pausedTransport;
    durationTargetRef.current = pausedTransport?.id ?? null;
    setPlayingSound(pausedTransport);
    setPlayingId(null);
    setIsPlaying(false);
    setPlayheadMs(restoredPlayhead);
    setDurationMs(restoredDuration);
    setSeqVariationBySound(saved?.seqVariationBySound || {});
    setSeqEditorSourceBySound({});
    if (saved?.seqEditorSourceBySound) setSeqEditorSourceBySound(saved.seqEditorSourceBySound);
    seqVariationRevisionRef.current += 1;
    setSeqVariationRevision((revision) => revision + 1);
    seqVariationsBySoundRef.current = saved?.seqVariationsBySound || {};
    seqVariationLoadsRef.current.clear();
    setSeqVariationsBySound(seqVariationsBySoundRef.current);
    seqPlaybackBySoundRef.current = saved?.seqPlaybackBySound || {};
    setSeqPlaybackBySound(seqPlaybackBySoundRef.current);
    setHistory(saved?.history?.length ? saved.history : [initial]);
    setHistoryIndex(saved?.history?.length
      ? Math.max(0, Math.min(saved.history.length - 1, Number(saved.historyIndex) || 0))
      : 0);
    setTweak("showWelcome", !data.archive);
    strmPlaybackBySoundRef.current = saved?.strmPlaybackBySound || {};
    strmPlaybackRevisionRef.current += 1;
    strmPlaybackLoadsRef.current.clear();
    setStrmPlaybackBySound(strmPlaybackBySoundRef.current);
    soundListAutoPlayEnabledRef.current = false;
    visibleSoundIdsRef.current = [];
    setSoundListAutoPlayEnabled(false);
    if (Array.isArray(recent)) setRecentArchives(recent);
    else if (refreshRecent) refreshRecentArchives();
  }

  async function openArchive() {
    if (!window.pysar) return;
    setOpenError(null);
    setLoadingArchive(true);
    const result = await window.pysar.call("open_archive_dialog").catch((error) => ({ ok: false, error: String(error) }));
    setLoadingArchive(false);
    if (!result) return;
    if (!result.ok) {
      setOpenError(result.error || "Could not open archive");
      return;
    }
    acceptArchiveData(result.data, result.recentArchives);
  }

  async function openRecentArchive(path) {
    if (!window.pysar || !path) return;
    setOpenError(null);
    setLoadingArchive(true);
    const result = await window.pysar.call("load_archive", path).catch((error) => ({ ok: false, error: String(error) }));
    setLoadingArchive(false);
    if (!result?.ok) {
      setOpenError(result?.error || "Could not open recent archive");
      refreshRecentArchives();
      return;
    }
    acceptArchiveData(result.data, result.recentArchives);
  }

  async function activateArchiveDocument(documentId) {
    if (!window.pysar || !documentId || documentId === activeDocumentIdRef.current || loadingArchive || archiveActivationRef.current || dumpStatus?.busy) return;
    rememberActiveArchiveWorkspace();
    const cachedData = archiveDataByDocumentRef.current[documentId] || null;
    archiveActivationRef.current = true;
    if (!cachedData) setLoadingArchive(true);
    const result = await window.pysar.call("activate_archive", documentId, !cachedData)
      .catch((error) => ({ ok: false, error: String(error) }));
    archiveActivationRef.current = false;
    if (!cachedData) setLoadingArchive(false);
    if (!result?.ok) {
      setOpenError(result?.error || "Could not switch archives");
      return;
    }
    const nextData = result.data || archiveDataWithDocumentState(
      cachedData,
      result.activeDocumentId || documentId,
      result.documents,
    );
    if (!nextData) {
      setOpenError("Could not restore the selected archive");
      return;
    }
    acceptArchiveData(nextData, null, { rememberCurrent: false, refreshRecent: false });
  }

  async function forgetRecentArchive(path) {
    if (!window.pysar || !path) return;
    const result = await window.pysar.call("forget_recent_archive", path).catch(() => null);
    if (result?.ok && Array.isArray(result.recentArchives)) setRecentArchives(result.recentArchives);
  }

  function handleDirtyChange(isDirty) {
    setDirty(isDirty);
    const documentId = activeDocumentIdRef.current;
    if (documentId) {
      setDocuments((current) => current.map((document) => (
        document.id === documentId ? { ...document, dirty: !!isDirty } : document
      )));
    }
  }
  function handleDataRefresh(data) {
    window.PYSAR_DATA = data;
    if (data?.activeDocumentId) archiveDataByDocumentRef.current[data.activeDocumentId] = data;
    if (Array.isArray(data.documents)) setDocuments(data.documents);
    if (data.activeDocumentId !== undefined) {
      setActiveDocumentId(data.activeDocumentId);
      activeDocumentIdRef.current = data.activeDocumentId;
    }
    // Sequence payload/labels may have changed while the sound ID stayed the
    // same.  Invalidate the derived variation cache so the editor and
    // transport cannot keep using pre-mutation offsets.
    seqVariationRevisionRef.current += 1;
    setSeqVariationRevision((revision) => revision + 1);
    seqVariationLoadsRef.current.clear();
    seqVariationsBySoundRef.current = {};
    setSeqVariationsBySound({});
    seqPlaybackBySoundRef.current = {};
    setSeqPlaybackBySound({});
    setArchive(data.archive);
    if (data.archive) setSafeMode(data.archive.safeMode !== false);
    if (data.archive) setDirty(!!data.archive.dirty);
    setDataRevision((revision) => revision + 1);
    setTabs((current) => current.map((tabItem) => {
      if (tabItem.kind === "view" || !tabItem.item) return tabItem;
      const item = refreshedDataItem(tabItem.kind, tabItem.item, data);
      if (item === tabItem.item) return tabItem;
      return { ...tabItem, item, title: item.name || item.label || tabItem.title };
    }));
    setSelectedItem((current) => refreshedSelection(current, data));
    setHistory((current) => current.map((entry) => ({
      ...entry,
      selectedItem: refreshedSelection(entry.selectedItem, data),
    })));
    setPlayingSound((current) => {
      if (!current || current.kind === "wave" || current.kind === "bank_note") return current;
      const item = refreshedDataItem("sound", current, data);
      return item === current ? current : {
        ...item,
        durationMs: current.durationMs,
        seqVariation: current.seqVariation,
      };
    });
  }

  function invalidateSequencePlayback(soundId) {
    const id = Number(soundId);
    if (!Number.isInteger(id) || id < 0) return;
    setSeqVariationBySound((current) => {
      if (!Object.prototype.hasOwnProperty.call(current, id)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });

    const active = playingSoundRef.current || playingSound;
    if (!active || Number(active.id) !== id) return;
    playRequestRef.current += 1;
    durationRequestRef.current += 1;
    clearAudio();
    stopBackendPlayback();
    const refreshed = { ...active, durationMs: 0 };
    delete refreshed.seqVariation;
    playingSoundRef.current = refreshed;
    durationTargetRef.current = null;
    setPlayingSound(refreshed);
    setPlayingId(null);
    setIsPlaying(false);
    setPlayheadMs(0);
    setDurationMs(0);
  }

  function invalidateStrmSources() {
    strmPlaybackRevisionRef.current += 1;
    strmPlaybackLoadsRef.current.clear();
    strmPlaybackBySoundRef.current = {};
    setStrmPlaybackBySound({});

    const active = playingSoundRef.current || playingSound;
    if (!active || active.type !== "STRM") return;
    playRequestRef.current += 1;
    durationRequestRef.current += 1;
    strmTrackTransitionRef.current = null;
    clearAudio();
    stopBackendPlayback();
    playingSoundRef.current = { ...active, durationMs: 0 };
    durationTargetRef.current = null;
    setPlayingSound(playingSoundRef.current);
    setPlayingId(null);
    setIsPlaying(false);
    setPlayheadMs(0);
    setDurationMs(0);
  }

  function invalidateBankSequencePlayback(bankId) {
    const targetBank = (D.banks || []).find((bank) => Number(bank.id) === Number(bankId));
    if (!targetBank) return;
    const affectedBanks = new Set(
      (D.banks || [])
        .filter((bank) => Number(bank.file) === Number(targetBank.file))
        .map((bank) => Number(bank.id)),
    );
    const affectedSounds = new Set(
      (D.sounds || [])
        .filter((sound) => sound.type === "SEQ" && affectedBanks.has(Number(sound.bank)))
        .map((sound) => Number(sound.id)),
    );
    if (!affectedSounds.size) return;
    setSeqVariationBySound((current) => {
      const next = { ...current };
      for (const soundId of affectedSounds) delete next[soundId];
      return next;
    });
    const active = playingSoundRef.current || playingSound;
    if (affectedSounds.has(Number(active?.id))) invalidateSequencePlayback(active.id);
  }

  async function saveArchive(documentId = activeDocumentIdRef.current) {
    if (!window.pysar) return false;
    const isActive = !documentId || documentId === activeDocumentIdRef.current;
    const result = await window.pysar.call(
      isActive ? "save_archive" : "save_archive_document",
      ...(isActive ? [] : [documentId]),
    ).catch((e) => ({ ok: false, error: String(e) }));
    if (result?.ok) {
      if (result.data?.documents) setDocuments(result.data.documents);
      if (isActive) {
        setDirty(false);
        if (result.data) handleDataRefresh(result.data);
      }
      return true;
    }
    setOpenError(result?.error || "Save failed");
    return false;
  }

  async function saveArchiveAs() {
    if (!window.pysar) return false;
    const result = await window.pysar.call("save_archive_as").catch((e) => ({ ok: false, error: String(e) }));
    if (result?.ok) {
      setDirty(false);
      if (result.data) handleDataRefresh(result.data);
      return true;
    }
    if (result?.error !== "Cancelled") setOpenError(result?.error || "Save failed");
    return false;
  }

  function chooseArchiveDump() {
    if (!window.pysar || dumpStatus?.busy) return;
    setMenuOpen(null);
    setDumpOptionsOpen(true);
  }

  async function dumpArchive(mode) {
    if (!window.pysar || dumpStatus?.busy) return;
    setMenuOpen(null);
    const dumpMode = mode === "original" ? "original" : "converted";
    setDumpStatus({
      busy: true,
      aborting: false,
      cancelled: false,
      mode: dumpMode,
      path: null,
      error: null,
      summary: null,
      progress: { completed: 0, total: 0, percent: 0, detail: "Waiting for a destination folder…" },
    });
    const result = await window.pysar.call("dump_archive_dialog", dumpMode)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      if (result?.cancelled) {
        setDumpStatus({
          busy: false,
          aborting: false,
          cancelled: true,
          mode: dumpMode,
          path: null,
          error: null,
          summary: null,
          progress: null,
        });
        return;
      }
      if (result?.error === "Cancelled") {
        setDumpStatus(null);
        return;
      }
      setDumpStatus({
        busy: false,
        aborting: false,
        cancelled: false,
        mode: dumpMode,
        path: null,
        error: result?.error || "Archive dump failed",
        summary: null,
        progress: null,
      });
      return;
    }
    const errorCount = Math.max(0, Number(result.errorCount || 0));
    const resultMode = result.mode || dumpMode;
    const isOriginalDump = resultMode === "original";
    const itemCount = Math.max(0, Number(isOriginalDump ? result.rawCount || 0 : result.decodedCount || 0));
    const itemLabel = isOriginalDump ? "original subfile" : "converted asset";
    const summary = result.summary || [
      itemCount ? `${itemCount} ${itemLabel}${itemCount === 1 ? "" : "s"}` : null,
      errorCount ? `${errorCount} item${errorCount === 1 ? "" : "s"} reported in manifest.json` : "No decode errors",
    ].filter(Boolean).join(" · ");
    setDumpStatus({
      busy: false,
      aborting: false,
      cancelled: false,
      mode: resultMode,
      path: result.path || null,
      error: null,
      summary,
      progress: null,
    });
  }

  async function abortArchiveDump() {
    if (!window.pysar || !dumpStatus?.busy || dumpStatus?.aborting) return;
    setDumpStatus((current) => current?.busy ? { ...current, aborting: true } : current);
    const result = await window.pysar.call("abort_dump")
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setDumpStatus((current) => current?.busy ? { ...current, aborting: false } : current);
      setOpenError(result?.error || "Could not abort archive dump");
    } else if (!result.abortRequested) {
      // The dump reached its atomic commit first; let its original request
      // deliver the final success/error state instead of claiming cancellation.
      setDumpStatus((current) => current?.busy ? { ...current, aborting: false } : current);
    }
  }

  async function closeArchive(documentId = activeDocumentIdRef.current, discard = false) {
    if (!window.pysar) return;
    const targetId = documentId || activeDocumentIdRef.current;
    const wasActive = targetId === activeDocumentIdRef.current;
    if (wasActive) {
      rememberActiveArchiveWorkspace();
      setSeqEditorSourceBySound({});
      stop();
    }
    const targetIndex = documents.findIndex((document) => document.id === targetId);
    const remainingDocuments = documents.filter((document) => document.id !== targetId);
    const expectedActiveId = wasActive
      ? (remainingDocuments[Math.min(Math.max(0, targetIndex), remainingDocuments.length - 1)]?.id || null)
      : activeDocumentIdRef.current;
    const cachedNextData = expectedActiveId
      ? archiveDataByDocumentRef.current[expectedActiveId] || null
      : null;
    const result = await window.pysar.call("close_archive", targetId, !!discard, !cachedNextData)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (result?.requiresSave) {
      setPendingCloseDocumentId(targetId);
      setUnsavedBusy(false);
      setUnsavedAction("close");
      return;
    }
    if (!result?.ok) {
      setOpenError(result?.error || "Could not close archive");
      return;
    }
    if (result?.ok) {
      delete archiveWorkspacesRef.current[targetId];
      delete archiveDataByDocumentRef.current[targetId];
      const nextData = result.data || archiveDataWithDocumentState(
        cachedNextData,
        result.activeDocumentId || expectedActiveId,
        result.documents,
      );
      if (wasActive || nextData?.activeDocumentId !== activeDocumentIdRef.current) {
        acceptArchiveData(nextData, null, { rememberCurrent: false, refreshRecent: false });
      } else {
        window.PYSAR_DATA = nextData;
        if (nextData?.activeDocumentId) {
          archiveDataByDocumentRef.current[nextData.activeDocumentId] = nextData;
        }
        setDocuments(Array.isArray(nextData?.documents) ? nextData.documents : []);
      }
    }
  }

  function requestOpen() {
    if (dumpStatus?.busy || loadingArchive) return;
    openArchive();
  }
  function requestOpenRecent(path) {
    if (!path || dumpStatus?.busy || loadingArchive) return;
    setMenuOpen(null);
    openRecentArchive(path);
  }
  function requestClose(documentId = activeDocumentIdRef.current) {
    if (dumpStatus?.busy) return;
    const document = documents.find((candidate) => candidate.id === documentId);
    const hasChanges = documentId === activeDocumentIdRef.current ? dirty : !!document?.dirty;
    if (hasChanges) {
      setPendingCloseDocumentId(documentId);
      setUnsavedBusy(false);
      setUnsavedAction("close");
      return;
    }
    closeArchive(documentId, false);
  }

  async function handleUnsavedSave() {
    const action = unsavedAction;
    if (!action || unsavedBusy) return;
    setUnsavedBusy(true);
    if (action === "window") {
      const saved = await window.pysar?.call("save_all_archives")
        .catch((error) => ({ ok: false, error: String(error) }));
      if (!saved?.ok) {
        setOpenError(saved?.error || "Could not save all archives");
        setUnsavedBusy(false);
        return;
      }
      const result = await window.pysar?.call("confirm_window_close").catch((error) => ({ ok: false, error: String(error) }));
      if (!result?.ok) {
        setOpenError(result?.error || "Could not close the application");
        setUnsavedBusy(false);
      }
      return;
    }
    const targetId = pendingCloseDocumentId || activeDocumentIdRef.current;
    const saved = await saveArchive(targetId);
    if (!saved) {
      setUnsavedBusy(false);
      return;
    }
    setUnsavedAction(null);
    setUnsavedBusy(false);
    setPendingCloseDocumentId(null);
    if (action === "close") closeArchive(targetId, false);
  }
  async function handleUnsavedDiscard() {
    const action = unsavedAction;
    if (!action || unsavedBusy) return;
    if (action === "window") {
      setUnsavedBusy(true);
      const result = await window.pysar?.call("confirm_window_close").catch((error) => ({ ok: false, error: String(error) }));
      if (!result?.ok) {
        setOpenError(result?.error || "Could not close the application");
        setUnsavedBusy(false);
      }
      return;
    }
    const targetId = pendingCloseDocumentId || activeDocumentIdRef.current;
    setUnsavedAction(null);
    setUnsavedBusy(false);
    setPendingCloseDocumentId(null);
    if (action === "close") closeArchive(targetId, true);
  }
  function handleUnsavedCancel() {
    const action = unsavedAction;
    if (unsavedBusy) return;
    setUnsavedAction(null);
    setUnsavedBusy(false);
    setPendingCloseDocumentId(null);
    setWindowCloseInfo(null);
    if (action === "window") {
      window.pysar?.call("cancel_window_close").catch(() => {});
    }
  }

  // property update
  async function updateSoundProperty(soundId, patch) {
    if (!window.pysar) return false;
    const result = await window.pysar.call("update_sound", soundId, patch).catch((e) => ({ ok: false, error: String(e) }));
    if (result?.ok) {
      if (result.dirty) setDirty(true);
      const refreshedData = result.data || dataWithSoundPatch(window.PYSAR_DATA, result.soundPatch);
      if (refreshedData) handleDataRefresh(refreshedData);
      if (Object.prototype.hasOwnProperty.call(patch, "bank")) {
        const active = playingSoundRef.current;
        if (
          active?.type === "SEQ"
          && Number(active.id) === Number(soundId)
          && isPlayingRef.current
        ) {
          const refreshed = (refreshedData?.sounds || []).find(
            (sound) => Number(sound.id) === Number(soundId),
          );
          if (refreshed) {
            const refreshedTransport = {
              ...refreshed,
              durationMs: active.durationMs,
              seqVariation: active.seqVariation || null,
            };
            await play(
              refreshedTransport,
              playheadMsRef.current,
              true,
              null,
              { deferAudioHandoff: true },
            );
          }
        }
      }
      return true;
    } else {
      setOpenError(result?.error || "Update failed");
      return false;
    }
  }

  async function renameSound(sound) {
    if (!window.pysar || sound?.id == null || sound.protected) return false;
    const requested = await window.pysarPrompt("Rename sound", sound.name || "", {
      label: "Sound name",
      confirmLabel: "Rename",
      maxLength: 255,
    });
    if (requested == null) return false;
    const name = requested.trim();
    if (!name || name === sound.name) return false;
    return updateSoundProperty(sound.id, { name });
  }

  async function exportSound(soundId) {
    if (!window.pysar || soundId == null) return;
    const result = await window.pysar.call("export_sound_dialog", soundId)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok && result?.error !== "Cancelled") {
      setOpenError(result?.error || "Export failed");
    }
  }

  async function requestSoundReplacement(soundId) {
    if (!window.pysar || soundId == null) return;
    const sound = (window.PYSAR_DATA?.sounds || []).find((candidate) => candidate.id === soundId);
    if (sound?.type !== "WAVE") {
      setReplaceSoundId(soundId);
      return;
    }

    const selection = await window.pysar.call("choose_wave_sound_replacement_source_dialog", soundId)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!selection?.ok) {
      if (selection?.error !== "Cancelled") setOpenError(selection?.error || "Could not choose a wave replacement");
      return;
    }
    setReplaceWaveTarget({
      kind: "sound",
      soundId: Number(soundId),
      soundName: sound.name,
      path: selection.path,
      sourceFormat: selection.sourceFormat,
      encoding: selection.encoding,
      looped: selection.looped,
      loopStart: selection.loopStart,
      samples: selection.samples,
    });
  }

  function refreshSelectedWave(data, archiveId, wave) {
    if (!data || !wave) return;
    const fileId = Number(archiveId);
    const waveIndex = Number(wave.index);
    const archiveItem = (data.waveArchives || []).find((item) => Number(item.id) === fileId);
    if (!archiveItem || !Number.isInteger(waveIndex) || waveIndex < 0) return;
    const replaceWave = (current) => {
      if (!current || current.kind !== "wave") return current;
      const currentArchiveId = Number(current.item?.archiveId);
      const currentWaveIndex = Number(current.item?.waveIndex ?? current.item?.index ?? current.id);
      if (currentArchiveId !== fileId || currentWaveIndex !== waveIndex) return current;
      return {
        ...current,
        id: waveIndex,
        name: `${archiveItem.name} · #${waveIndex}`,
        item: {
          ...wave,
          archiveId: fileId,
          archiveName: archiveItem.name,
          waveIndex,
          index: waveIndex,
        },
      };
    };
    setSelectedItem(replaceWave);
    setHistory((current) => current.map((entry) => ({
      ...entry,
      selectedItem: replaceWave(entry.selectedItem),
    })));
  }

  async function exportWaveArchiveSample(archiveId, waveIndex) {
    if (!window.pysar || archiveId == null || waveIndex == null) return;
    const result = await window.pysar.call("export_wave_archive_sample_dialog", archiveId, waveIndex)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok && result?.error !== "Cancelled") {
      const message = result?.error || "BRWAV export failed";
      setOpenError(message);
    }
  }

  async function replaceWaveArchiveSample(
    archiveId,
    waveIndex,
    path,
    encoding = null,
    looped = null,
    loopStart = 0,
  ) {
    if (!window.pysar || archiveId == null || waveIndex == null || !path) return;
    const result = await window.pysar.call(
      "replace_wave_archive_sample_from_path",
      archiveId,
      waveIndex,
      path,
      encoding,
      looped,
      loopStart,
    )
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      if (result?.error !== "Cancelled") {
        const message = result?.error || "BRWAV replacement failed";
        setOpenError(message);
      }
      return;
    }
    // The browser may still hold a previously fetched WAV for this sample (or
    // for a bank that uses it). Stop it so the next Play request always fetches
    // audio decoded from the replacement rather than resuming stale bytes.
    stop();
    if (result.dirty) setDirty(true);
    if (result.data) {
      handleDataRefresh(result.data);
      refreshSelectedWave(result.data, archiveId, result.wave);
    }
  }

  function selectArchiveWave(data, archiveId, wave) {
    if (!data) return;
    const fileId = Number(archiveId);
    const archiveItem = (data.waveArchives || []).find((item) => Number(item.id) === fileId);
    if (!archiveItem) return;
    if (!wave) {
      setSelectedItem({ kind: "archive", id: archiveItem.id, name: archiveItem.name, item: archiveItem });
      return;
    }
    const waveIndex = Number(wave.index);
    setSelectedItem({
      kind: "wave",
      id: waveIndex,
      name: `${archiveItem.name} · #${waveIndex}`,
      item: {
        ...wave,
        archiveId: fileId,
        archiveName: archiveItem.name,
        waveIndex,
        index: waveIndex,
      },
    });
  }

  async function addWaveArchiveSample(
    archiveId,
    path,
    encoding = null,
    looped = null,
    loopStart = 0,
  ) {
    if (!window.pysar || archiveId == null || !path) return false;
    const result = await window.pysar.call(
      "add_wave_archive_sample_from_path",
      archiveId,
      path,
      encoding,
      looped,
      loopStart,
    ).catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      if (result?.error !== "Cancelled") setOpenError(result?.error || "Could not add BRWAV");
      return false;
    }
    stop();
    if (result.dirty) setDirty(true);
    if (result.data) {
      handleDataRefresh(result.data);
      selectArchiveWave(result.data, archiveId, result.wave);
    }
    return true;
  }

  async function requestWaveArchiveImport(archiveId) {
    if (!window.pysar || archiveId == null) return;
    const selection = await window.pysar.call(
      "choose_wave_archive_sample_import_source_dialog",
      archiveId,
    ).catch((error) => ({ ok: false, error: String(error) }));
    if (!selection?.ok) {
      if (selection?.error !== "Cancelled") setOpenError(selection?.error || "Could not choose a BRWAV");
      return;
    }
    const archiveItem = (window.PYSAR_DATA.waveArchives || []).find(
      (item) => Number(item.id) === Number(archiveId),
    );
    setReplaceWaveTarget({
      kind: "archive",
      operation: "add",
      archiveId: Number(archiveId),
      archiveName: archiveItem?.name,
      path: selection.path,
      sourceFormat: selection.sourceFormat,
      encoding: selection.encoding,
      looped: selection.looped,
      loopStart: selection.loopStart,
      samples: selection.samples,
    });
  }

  async function updateWaveArchiveSample(archiveId, waveIndex, patch) {
    if (!window.pysar || archiveId == null || waveIndex == null) return false;
    const result = await window.pysar.call(
      "update_wave_archive_sample", archiveId, waveIndex, patch,
    ).catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setOpenError(result?.error || "Could not update BRWAV");
      return false;
    }
    stop();
    if (result.dirty) setDirty(true);
    if (result.data) {
      handleDataRefresh(result.data);
      refreshSelectedWave(result.data, archiveId, result.wave);
    }
    return true;
  }

  async function deleteWaveArchiveSample(archiveId, waveIndex) {
    if (!window.pysar || archiveId == null || waveIndex == null) return false;
    const impact = await window.pysar.call(
      "get_wave_archive_sample_delete_impact", archiveId, waveIndex,
    ).catch((error) => ({ ok: false, error: String(error) }));
    if (!impact?.ok) {
      setOpenError(impact?.error || "Could not inspect BRWAV references");
      return false;
    }
    const references = impact.references || [];
    const replacements = impact.replacements || [];
    if (references.length && !replacements.length) {
      await window.pysarAlert(
        "This is the only sample in the archive and it is still referenced. Add another sample before deleting it.",
        { title: "BRWAV cannot be deleted" },
      );
      return false;
    }
    const action = references.length ? {
      id: "reassign",
      label: "Reassign and delete",
      description: "Move every bank-zone and RWSD-note reference to the selected sample.",
      confirmLabel: "Reassign and delete",
      tone: "danger",
      selection: {
        label: "Replacement sample",
        options: replacements.map((item) => ({ value: item.id, label: item.name })),
      },
    } : {
      id: "delete",
      label: "Delete sample",
      description: "No bank zone or RWSD note directly references this sample.",
      confirmLabel: "Delete sample",
      tone: "danger",
    };
    const actionId = action.id;
    const choice = await window.pysarConsequence(`Delete BRWAV #${waveIndex}?`, {
      title: "Delete BRWAV",
      caption: "Exact consequences",
      actions: [action],
      resources: [
        {
          id: "sample",
          resource: { badge: "BRWAV", name: `BRWAV #${waveIndex} in WAR_${archiveId}` },
          outcomes: { [actionId]: { text: "Sample will be deleted", status: "deleted" } },
        },
        ...references.map((reference, index) => ({
          id: `reference-${index}`,
          resource: {
            badge: reference.kind === "bank-zone" ? "BANK" : "RWSD",
            name: reference.name,
          },
          outcomes: {
            [actionId]: references.length
              ? { text: "Sample reference will be reassigned", status: "modified" }
              : { text: "Reference remains unchanged", status: "retained" },
          },
        })),
      ],
    });
    if (choice?.action !== actionId) return false;
    const replacementWaveIndex = references.length ? Number(choice.selection) : null;
    const result = await window.pysar.call(
      "delete_wave_archive_sample", archiveId, waveIndex, replacementWaveIndex,
    ).catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setOpenError(result?.error || "Could not delete BRWAV");
      return false;
    }
    stop();
    if (result.dirty) setDirty(true);
    if (result.data) {
      handleDataRefresh(result.data);
      selectArchiveWave(result.data, archiveId, result.wave);
    }
    return true;
  }

  async function deleteWaveArchive(archiveItem) {
    if (!window.pysar || archiveItem?.id == null || archiveItem.protected) return false;
    const archiveId = Number(archiveItem.id);
    const impact = await window.pysar.call("get_wave_archive_delete_impact", archiveId)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!impact?.ok) {
      setOpenError(impact?.error || "Could not inspect wave archive dependencies");
      return false;
    }
    const files = impact.files || [];
    const banks = impact.banks || [];
    const sounds = impact.sounds || [];
    const groups = impact.groups || [];
    const replacements = impact.replacements || [];
    const hasDependents = files.length > 0;
    if (hasDependents && !replacements.length) {
      const required = Number(impact.requiredWaveCount) || 0;
      await window.pysarAlert(
        `No other wave archive has the ${required} wave${required === 1 ? "" : "s"} required by the linked files. ` +
        "Import or expand a compatible archive before deleting this one.",
        { title: "Wave archive cannot be deleted" },
      );
      return false;
    }
    const action = hasDependents ? {
      id: "relink",
      label: "Relink and delete",
      description: "Relink every paired bank/RWSD file to a compatible wave archive, then delete this archive.",
      confirmLabel: "Relink and delete",
      tone: "danger",
      selection: {
        label: "Replacement wave archive",
        options: replacements.map((item) => ({
          value: item.id,
          label: `${item.name} [${item.id}] · ${item.waves} waves`,
        })),
      },
    } : {
      id: "delete",
      label: "Delete wave archive",
      description: "No bank or RWSD data file depends on this archive.",
      confirmLabel: "Delete wave archive",
      tone: "danger",
    };
    const actionId = action.id;
    const choice = await window.pysarConsequence(`Delete ${archiveItem.name || `WAR_${archiveId}`}?`, {
      title: "Delete wave archive",
      caption: "Exact consequences",
      actions: [action],
      resources: [
        {
          id: "archive",
          resource: { badge: "RWAR", name: `${archiveItem.name} [${archiveId}]` },
          outcomes: { [actionId]: { text: "Wave archive will be deleted", status: "deleted" } },
        },
        ...files.map((file) => ({
          id: `file-${file.id}`,
          resource: { badge: "FILE", name: file.name },
          outcomes: { [actionId]: { text: "Audio archive link will be replaced", status: "modified" } },
        })),
        ...banks.map((bank) => ({
          id: `bank-${bank.id}`,
          resource: { badge: "BANK", name: `${bank.name} [${bank.id}]` },
          outcomes: { [actionId]: { text: "Retained; wave indices remain valid", status: "retained" } },
        })),
        ...sounds.map((sound) => ({
          id: `sound-${sound.id}`,
          resource: { badge: "WAVE", name: `${sound.name} [${sound.id}]` },
          outcomes: { [actionId]: { text: "Retained; backing file remains playable", status: "retained" } },
        })),
        ...groups.map((group) => ({
          id: `group-${group.id}`,
          resource: { badge: "GROUP", name: `${group.name} [${group.id}]` },
          outcomes: { [actionId]: { text: "Retained; file linkage will be repaired", status: "retained" } },
        })),
      ],
    });
    if (choice?.action !== actionId) return false;
    const replacementFileId = hasDependents ? Number(choice.selection) : null;
    const result = await window.pysar.call("delete_wave_archive", archiveId, replacementFileId)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setOpenError(result?.error || "Could not delete wave archive");
      return false;
    }
    stop();
    if (result.dirty) setDirty(true);
    const oldArchives = window.PYSAR_DATA.waveArchives || [];
    const oldIndex = oldArchives.findIndex((item) => Number(item.id) === archiveId);
    if (result.data) handleDataRefresh(result.data);
    const nextArchives = result.data?.waveArchives || [];
    const replacement = result.replacementFileId == null
      ? null
      : nextArchives.find((item) => Number(item.id) === Number(result.replacementFileId));
    const next = replacement
      || nextArchives[Math.min(Math.max(0, oldIndex), nextArchives.length - 1)]
      || null;
    const nextSelection = next
      ? { kind: "archive", id: next.id, name: next.name, item: next }
      : null;
    const sourceFileIds = new Set((impact.sourceFileIds || [archiveId]).map(Number));
    setTabs((current) => {
      const remaining = current.filter((item) => (
        item.kind !== "archive" || !sourceFileIds.has(Number(item.item?.id))
      ));
      return remaining.some((item) => item.id === "archives")
        ? remaining
        : [...remaining, { id: "archives", kind: "view", view: "archives", title: "Wave archives" }];
    });
    setActiveTab("archives");
    setNavView("archives");
    setSelectedItem(nextSelection);
    setHistory([{ tabId: "archives", navView: "archives", soundFilter, selectedItem: nextSelection }]);
    setHistoryIndex(0);
    return true;
  }

  async function replaceWaveSound(soundId, path, encoding = null, looped = null, loopStart = 0) {
    if (!window.pysar || soundId == null || !path) return;
    const result = await window.pysar.call(
      "replace_wave_sound_from_path",
      soundId,
      path,
      encoding,
      looped,
      loopStart,
    )
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      if (result?.error !== "Cancelled") setOpenError(result?.error || "Wave replacement failed");
      return;
    }
    if (result.dirty) setDirty(true);
    if (result.data) handleDataRefresh(result.data);
  }

  async function requestWaveArchiveReplacement(archiveId, waveIndex) {
    if (!window.pysar || archiveId == null || waveIndex == null) return;
    const selection = await window.pysar.call(
      "choose_wave_archive_sample_replacement_source_dialog",
      archiveId,
      waveIndex,
    ).catch((error) => ({ ok: false, error: String(error) }));
    if (!selection?.ok) {
      if (selection?.error !== "Cancelled") {
        const message = selection?.error || "Could not choose a BRWAV replacement";
        setOpenError(message);
      }
      return;
    }
    const archiveItem = (window.PYSAR_DATA.waveArchives || []).find(
      (item) => Number(item.id) === Number(archiveId),
    );
    setReplaceWaveTarget({
      kind: "archive",
      operation: "replace",
      archiveId: Number(archiveId),
      waveIndex: Number(waveIndex),
      archiveName: archiveItem?.name,
      path: selection.path,
      sourceFormat: selection.sourceFormat,
      encoding: selection.encoding,
      looped: selection.looped,
      loopStart: selection.loopStart,
      samples: selection.samples,
    });
  }

  async function updateGroupProperty(groupId, patch) {
    if (!window.pysar) return;
    if (patch.name) {
      const result = await window.pysar.call("rename_group", groupId, patch.name).catch((e) => ({ ok: false, error: String(e) }));
      if (result?.ok) {
        if (result.dirty) setDirty(true);
        if (result.data) handleDataRefresh(result.data);
      } else {
        setOpenError(result?.error || "Update failed");
      }
    }
  }

  async function updatePlayerProperty(playerId, patch) {
    if (!window.pysar) return false;
    const result = await window.pysar.call("update_player", playerId, patch)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (result?.ok) {
      if (result.dirty) setDirty(true);
      if (result.data) handleDataRefresh(result.data);
      return true;
    } else {
      setOpenError(result?.error || "Player update failed");
      return false;
    }
  }

  async function renamePlayer(player) {
    if (!window.pysar || player?.id == null || player.protected) return false;
    const requested = await window.pysarPrompt("Rename player", player.name || "", {
      label: "Player name",
      confirmLabel: "Rename",
      maxLength: 255,
    });
    if (requested == null) return false;
    const name = requested.trim();
    if (!name || name === player.name) return false;
    return updatePlayerProperty(player.id, { name });
  }

  async function deletePlayer(player) {
    if (!window.pysar || player?.id == null || player.protected) return false;
    const impact = await window.pysar.call("get_player_delete_impact", player.id)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!impact?.ok) {
      setOpenError(impact?.error || "Could not inspect player references");
      return false;
    }
    const references = impact.references || [];
    const replacements = impact.replacements || [];
    if (references.length && !replacements.length) {
      await window.pysarAlert(
        "This is the only player and sounds still reference it. Create another player before deleting it.",
        { title: "Player cannot be deleted" },
      );
      return false;
    }
    const action = references.length ? {
      id: "reassign",
      label: "Reassign and delete",
      description: "Move every sound reference to the selected replacement player.",
      confirmLabel: "Reassign and delete",
      tone: "danger",
      selection: {
        label: "Replacement player",
        options: replacements.map((item) => ({
          value: item.id,
          label: `${item.name} [${item.id}]`,
        })),
      },
    } : {
      id: "delete",
      label: "Delete player",
      description: "No sounds reference this player.",
      confirmLabel: "Delete player",
      tone: "danger",
    };
    const actionId = action.id;
    const choice = await window.pysarConsequence(`Delete ${player.name || `player #${player.id}`}?`, {
      title: "Delete player",
      caption: "Exact consequences",
      actions: [action],
      resources: [
        {
          id: "player",
          resource: { badge: "PLAYER", name: `${player.name} [${player.id}]` },
          outcomes: { [actionId]: { text: "Player entry will be deleted", status: "deleted" } },
        },
        ...references.map((sound) => ({
          id: `sound-${sound.id}`,
          resource: { badge: sound.type || "SOUND", name: `${sound.name} [${sound.id}]` },
          outcomes: { [actionId]: { text: "Player reference will be reassigned", status: "modified" } },
        })),
      ],
    });
    if (choice?.action !== actionId) return false;
    const replacementPlayerId = references.length ? Number(choice.selection) : null;
    const result = await window.pysar.call("delete_player", player.id, replacementPlayerId)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setOpenError(result?.error || "Could not delete player");
      return false;
    }
    if (result.dirty) setDirty(true);
    if (result.data) {
      const oldIndex = (D.players || []).findIndex((item) => Number(item.id) === Number(player.id));
      handleDataRefresh(result.data);
      const players = result.data.players || [];
      const next = players[Math.min(Math.max(0, oldIndex), players.length - 1)] || null;
      setSelectedItem(next ? { kind: "player", id: next.id, name: next.name, item: next } : null);
    }
    return true;
  }

  async function runBankMutation(operation) {
    if (bankMutationRef.current) return false;
    bankMutationRef.current = true;
    try {
      return await operation();
    } finally {
      bankMutationRef.current = false;
    }
  }

  async function renameBank(bank) {
    if (!window.pysar || bank?.id == null) return false;
    return runBankMutation(async () => {
      const requested = await window.pysarPrompt("Rename bank", bank.name || "", {
        label: "Bank name",
        confirmLabel: "Rename",
        maxLength: 255,
      });
      if (requested == null) return false;
      const name = requested.trim();
      if (!name || name === bank.name) return false;
      const result = await window.pysar.call("rename_bank", bank.id, name)
        .catch((error) => ({ ok: false, error: String(error) }));
      if (!result?.ok) {
        setOpenError(result?.error || "Could not rename bank");
        return false;
      }
      if (result.dirty) setDirty(true);
      if (result.data) handleDataRefresh(result.data);
      return true;
    });
  }

  async function replaceBank(bank) {
    if (!window.pysar || bank?.id == null) return false;
    return runBankMutation(async () => {
      let result = await window.pysar.call("replace_bank_dialog", bank.id, false)
        .catch((error) => ({ ok: false, error: String(error) }));
      if (result?.requiresConfirmation) {
        const sharedBanks = result.sharedBanks || [];
        const choice = await window.pysarConsequence(
          `${bank.name || `Bank #${bank.id}`} shares its RBNK data with other bank entries.`,
          {
            title: "Replace shared bank data",
            caption: "Banks that will receive the replacement",
            actions: [{
              id: "replace-all",
              label: "Replace for all",
              description: "Every bank listed below will use the selected BRBNK or SF2 data.",
              confirmLabel: "Choose replacement file",
              tone: "danger",
            }],
            resources: sharedBanks.map((name, index) => ({
              id: `bank-${index}`,
              resource: { badge: "BANK", name },
              outcomes: {
                "replace-all": {
                  text: "Shared RBNK data will be replaced",
                  status: "modified",
                },
              },
            })),
          },
        );
        if (choice?.action !== "replace-all") return false;
        result = await window.pysar.call("replace_bank_dialog", bank.id, true)
          .catch((error) => ({ ok: false, error: String(error) }));
      }
      if (!result?.ok) {
        if (!result?.cancelled) setOpenError(result?.error || "Bank replacement failed");
        return false;
      }
      invalidateBankSequencePlayback(bank.id);
      if (result.dirty) setDirty(true);
      if (result.archiveData) handleDataRefresh(result.archiveData);
      setBankContentRevision((revision) => revision + 1);
      if (result.warnings?.length) {
        await window.pysarAlert(result.warnings.join("\n"), {
          title: "Bank imported with warnings",
        });
      }
      return true;
    });
  }

  async function exportBank(bank) {
    if (!window.pysar || bank?.id == null) return false;
    return runBankMutation(async () => {
      let result = await window.pysar.call("export_bank_dialog", bank.id)
        .catch((error) => ({ ok: false, error: String(error) }));
      if (result?.requiresCompanionOverwrite) {
        const choice = await window.pysarConsequence(
          "The BRBNK export has a companion BRWAR, but a different file already exists at that destination.",
          {
            title: "Overwrite companion BRWAR",
            caption: "Files that will be written",
            actions: [{
              id: "overwrite",
              label: "Export and overwrite",
              description: "Write the BRBNK and replace the existing companion BRWAR as one atomic operation.",
              confirmLabel: "Overwrite and export",
              tone: "danger",
            }],
            resources: [
              {
                id: "bank-file",
                resource: { badge: "RBNK", name: result.path || `${bank.name}.brbnk` },
                outcomes: {
                  overwrite: { text: "Export file will be written", status: "modified" },
                },
              },
              {
                id: "wave-file",
                resource: { badge: "RWAR", name: result.companionPath },
                outcomes: {
                  overwrite: { text: "Existing companion will be overwritten", status: "warning" },
                },
              },
            ],
          },
        );
        if (choice?.action !== "overwrite") return false;
        result = await window.pysar.call(
          "export_bank_to_path",
          bank.id,
          result.path,
          result.format || "brbnk",
          true,
        ).catch((error) => ({ ok: false, error: String(error) }));
      }
      if (!result?.ok) {
        if (!result?.cancelled) setOpenError(result?.error || "Bank export failed");
        return false;
      }
      if (result.warnings?.length) {
        await window.pysarAlert(result.warnings.join("\n"), {
          title: "Bank exported with warnings",
        });
      }
      return true;
    });
  }

  async function deleteBank(bank) {
    if (!window.pysar || bank?.id == null) return false;
    return runBankMutation(async () => {
      const impact = await window.pysar.call("get_bank_delete_impact", bank.id)
        .catch((error) => ({ ok: false, error: String(error) }));
      if (!impact?.ok) {
        setOpenError(impact?.error || "Could not inspect bank references");
        return false;
      }
      const references = impact.references || [];
      const replacements = impact.replacements || [];
      if (references.length && !replacements.length) {
        await window.pysarAlert(
          "This is the only bank and sequence sounds still reference it. Create another bank before deleting it.",
          { title: "Bank cannot be deleted" },
        );
        return false;
      }
      const action = references.length ? {
        id: "reassign",
        label: "Reassign and delete",
        description: "Move every sequence reference to the selected replacement bank.",
        confirmLabel: "Reassign and delete",
        tone: "danger",
        selection: {
          label: "Replacement bank",
          options: replacements.map((item) => ({
            value: item.id,
            label: `${item.name} [${item.id}]${item.sharesFile ? " · shared data" : ""}`,
          })),
        },
      } : {
        id: "delete",
        label: "Delete bank",
        description: "No sequence sounds reference this bank.",
        confirmLabel: "Delete bank",
        tone: "danger",
      };
      const actionId = action.id;
      const backing = impact.backingFile || {};
      const choice = await window.pysarConsequence(
        `Delete ${bank.name || `bank #${bank.id}`}?`,
        {
          title: "Delete bank",
          caption: "Exact consequences",
          actions: [action],
          resources: [
            {
              id: "bank",
              resource: { badge: "BANK", name: `${bank.name} [${bank.id}]` },
              outcomes: { [actionId]: { text: "Bank entry will be deleted", status: "deleted" } },
            },
            ...references.map((sound) => ({
              id: `sound-${sound.id}`,
              resource: { badge: "SEQ", name: `${sound.name} [${sound.id}]` },
              outcomes: { [actionId]: { text: "Bank reference will be reassigned", status: "modified" } },
            })),
            {
              id: "backing-file",
              resource: { badge: "RBNK", name: backing.name || `Logical file #${backing.id}` },
              outcomes: {
                [actionId]: backing.willDelete
                  ? { text: "Orphaned RBNK/RWAR data will be deleted", status: "deleted" }
                  : { text: "Shared RBNK/RWAR data will be retained", status: "retained" },
              },
            },
          ],
        },
      );
      if (choice?.action !== actionId) return false;
      const replacementBankId = references.length ? Number(choice.selection) : null;
      const result = await window.pysar.call("delete_bank", bank.id, replacementBankId)
        .catch((error) => ({ ok: false, error: String(error) }));
      if (!result?.ok) {
        setOpenError(result?.error || "Could not delete bank");
        return false;
      }

      const affectedSequenceIds = new Set(
        (D.sounds || [])
          .filter((sound) => sound.type === "SEQ" && Number(sound.bank) === Number(bank.id))
          .map((sound) => Number(sound.id)),
      );
      const activeTransport = playingSoundRef.current || playingSound;
      if (activeTransport?.kind === "bank_note" && Number(activeTransport.bankId) >= Number(bank.id)) {
        stop();
      } else if (affectedSequenceIds.has(Number(activeTransport?.id))) {
        invalidateSequencePlayback(activeTransport.id);
      }

      if (result.dirty) setDirty(true);
      const nextBanks = result.data?.banks || [];
      const nextBank = nextBanks[Math.min(Number(bank.id), nextBanks.length - 1)] || null;
      const nextSelection = nextBank
        ? { kind: "bank", id: nextBank.id, name: nextBank.name, item: nextBank }
        : null;
      if (result.data) handleDataRefresh(result.data);
      setTabs((current) => {
        const remaining = current.filter((item) => item.kind !== "bank");
        return remaining.some((item) => item.id === "banks")
          ? remaining
          : [...remaining, { id: "banks", kind: "view", view: "banks", title: "Banks" }];
      });
      setActiveTab("banks");
      setNavView("banks");
      setSelectedItem(nextSelection);
      setHistory([{ tabId: "banks", navView: "banks", soundFilter, selectedItem: nextSelection }]);
      setHistoryIndex(0);
      return true;
    });
  }

  async function renameSelectedGroup() {
    if (selectedItem?.kind !== "group") return;
    const currentName = selectedItem.item?.name || selectedItem.name || "";
    const requested = await window.pysarPrompt("Rename group", currentName, {
      label: "Group name",
      confirmLabel: "Rename",
      maxLength: 255,
    });
    if (requested == null) return;
    const name = requested.trim();
    if (!name || name === currentName) return;
    updateGroupProperty(selectedItem.id, { name });
  }

  async function deleteGroup(groupId, groupName) {
    if (!window.pysar || groupId == null) return false;
    const name = groupName || `group #${groupId}`;
    const impact = await window.pysar.call("get_group_delete_impact", groupId)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!impact?.ok) {
      setOpenError(impact?.error || "Could not inspect group dependencies");
      return false;
    }
    const files = impact.files || [];
    const banks = impact.banks || [];
    const sounds = impact.sounds || [];
    const replacements = impact.replacements || [];
    if (files.length && !replacements.length) {
      await window.pysarAlert(
        "This is the only group and it is the final load location of archive files. Create another group before deleting it.",
        { title: "Group cannot be deleted" },
      );
      return false;
    }
    const action = files.length ? {
      id: "move",
      label: "Move files and delete",
      description: "Move every file that would lose its final load location to the selected group.",
      confirmLabel: "Move files and delete",
      tone: "danger",
      selection: {
        label: "Replacement group",
        options: replacements.map((item) => ({
          value: item.id,
          label: `${item.name} [${item.id}]`,
        })),
      },
    } : {
      id: "delete",
      label: "Delete group",
      description: "Every contained file already has another load-group location.",
      confirmLabel: "Delete group",
      tone: "danger",
    };
    const actionId = action.id;
    const choice = await window.pysarConsequence(`Delete ${name}?`, {
      title: "Delete group",
      caption: "Exact consequences",
      actions: [action],
      resources: [
        {
          id: "group",
          resource: { badge: "GROUP", name: `${name} [${groupId}]` },
          outcomes: { [actionId]: { text: "Group entry will be deleted", status: "deleted" } },
        },
        ...files.map((file) => ({
          id: `file-${file.fileIndex}`,
          resource: { badge: file.kind || "FILE", name: `${file.name} [FILE ${file.fileIndex}]` },
          outcomes: { [actionId]: { text: "Final load location will be moved", status: "modified" } },
        })),
        ...banks.map((bank) => ({
          id: `bank-${bank.id}`,
          resource: { badge: "BANK", name: `${bank.name} [${bank.id}]` },
          outcomes: { [actionId]: { text: "Retained; backing file remains loadable", status: "retained" } },
        })),
        ...sounds.map((sound) => ({
          id: `sound-${sound.id}`,
          resource: { badge: sound.type || "SOUND", name: `${sound.name} [${sound.id}]` },
          outcomes: { [actionId]: { text: "Retained; backing file remains loadable", status: "retained" } },
        })),
      ],
    });
    if (choice?.action !== actionId) return false;
    const replacementGroupId = files.length ? Number(choice.selection) : null;
    const result = await window.pysar.call("delete_group", groupId, replacementGroupId)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setOpenError(result?.error || "Delete failed");
      return false;
    }
    if (result.dirty) setDirty(true);
    if (result.data) {
      handleDataRefresh(result.data);
      const groups = result.data.groups || [];
      const replacement = result.replacementGroupId == null
        ? null
        : groups.find((group) => Number(group.id) === Number(result.replacementGroupId));
      const next = replacement || groups[Math.min(Number(groupId), groups.length - 1)] || null;
      setSelectedItem(next ? { kind: "group", id: next.id, name: next.name, item: next } : null);
    }
    return true;
  }

  async function deleteSound(sound) {
    if (!window.pysar || sound?.id == null) return;
    const impact = await window.pysar.call("get_sound_delete_impact", sound.id)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!impact?.ok) {
      setOpenError(impact?.error || "Could not inspect the sound");
      return;
    }
    const backing = impact.backingFile || {};
    const cleanupResources = Array.isArray(impact.cleanupResources)
      ? impact.cleanupResources
      : [];
    const cleanupAvailable = cleanupResources.some((item) => !item.protected);
    const cleanupContainsBackingFile = cleanupResources.some(
      (item) => item.resourceType === "file" && Number(item.fileIndex) === Number(backing.id),
    );
    const cleanupBadge = (item) => {
      if (item.resourceType === "bank") return "BANK";
      if (item.resourceType === "wave") return "BRWAV";
      if (item.resourceType === "wsd-entry") return "RWSD";
      return String(item.kind || item.resourceType || "FILE").split(" ")[0].toUpperCase();
    };
    const actions = [{
      id: "delete",
      label: "Delete sound only",
      description: "Keep its backing sequence, wave, or stream data for reuse and later cleanup.",
      confirmLabel: "Delete sound",
      tone: "danger",
    }];
    if (cleanupAvailable) {
      actions.push({
        id: "cleanup",
        label: "Delete and clean up",
        description: "Also remove resources made unreachable by deleting this sound. Existing unrelated leftovers are retained.",
        confirmLabel: "Delete and clean up",
        tone: "danger",
      });
    }
    const choice = await window.pysarConsequence(
      `Delete ${sound.name || `sound #${sound.id}`} from the archive's sound list?`,
      {
        title: "Delete sound",
        caption: "Exact consequences",
        actions,
        resources: [
          {
            id: "sound",
            resource: { badge: sound.type || "SOUND", name: `${sound.name} [${sound.id}]` },
            outcomes: {
              delete: { text: "Sound entry will be deleted", status: "deleted" },
              ...(cleanupAvailable ? {
                cleanup: { text: "Sound entry will be deleted", status: "deleted" },
              } : {}),
            },
          },
          ...cleanupResources.map((item, index) => ({
            id: `cleanup-${item.resourceType}-${item.fileIndex ?? item.fileId ?? ""}-${item.id}-${index}`,
            resource: {
              badge: cleanupBadge(item),
              name: item.name,
            },
            outcomes: {
              delete: { text: "Kept in the archive", status: "retained" },
              ...(cleanupAvailable ? {
                cleanup: item.protected
                  ? { text: "Retained by Safe Mode", status: "retained" }
                  : { text: "Will be deleted", status: "deleted" },
              } : {}),
            },
          })),
          ...(!cleanupContainsBackingFile ? [{
            id: "backing-file",
            resource: {
              badge: backing.kind || "FILE",
              name: backing.name || `File #${backing.id}`,
              detail: backing.otherUsers
                ? `${backing.otherUsers} other archive reference${backing.otherUsers === 1 ? "" : "s"}`
                : "No other direct sound or bank references",
            },
            outcomes: {
              delete: { text: "Backing data will be retained", status: "retained" },
              ...(cleanupAvailable ? {
                cleanup: { text: "Still referenced; will be retained", status: "retained" },
              } : {}),
            },
          }] : []),
        ],
      },
    );
    if (!choice?.action || !["delete", "cleanup"].includes(choice.action)) return;
    const result = await window.pysar.call(
      "delete_sound",
      sound.id,
      choice.action === "cleanup",
    )
      .catch((error) => ({ ok: false, error: String(error) }));
    if (!result?.ok) {
      setOpenError(result?.error || "Delete failed");
      return;
    }
    const activeTransport = playingSoundRef.current || playingSound;
    const activeSoundId = Number(activeTransport?.id);
    if (Number.isInteger(activeSoundId) && activeSoundId >= Number(sound.id)) {
      playRequestRef.current += 1;
      durationRequestRef.current += 1;
      clearAudio();
      stopBackendPlayback();
      playingSoundRef.current = null;
      durationTargetRef.current = null;
      setPlayingSound(null);
      setPlayingId(null);
      setIsPlaying(false);
      setPlayheadMs(0);
      setDurationMs(0);
    }
    // Numeric sound identities after the removed row may shift, so no cached
    // selection may safely retain its old key.
    setSeqVariationBySound({});
    strmPlaybackRevisionRef.current += 1;
    strmPlaybackLoadsRef.current.clear();
    strmPlaybackBySoundRef.current = {};
    setStrmPlaybackBySound({});
    if (result.dirty) setDirty(true);
    if (result.data) handleDataRefresh(result.data);
    // Sound-table IDs after the removed row shift down. Close sound detail
    // tabs instead of leaving any tab bound to a stale numeric identity.
    setTabs((current) => {
      const remaining = current.filter((item) => item.kind !== "sound");
      return remaining.length ? remaining : [{ id: "all", kind: "view", view: "all", title: "All sounds" }];
    });
    const desiredView = { SEQ: "sequences", WAVE: "waves", STRM: "streams" }[sound.type];
    const resourceView = tabs.find((item) => item.kind === "view" && item.view === desiredView);
    setActiveTab(resourceView?.id || "all");
    setNavView(resourceView?.view || "all");
    setSoundFilter(resourceView ? sound.type : "ALL");
    setSelectedItem(null);
  }

  function deleteSelectedGroup() {
    if (selectedItem?.kind !== "group") return;
    deleteGroup(selectedItem.id, selectedItem.item?.name || selectedItem.name);
  }

  async function toggleSafeMode() {
    if (!window.pysar || !archive) return;
    const enabled = !safeMode;
    let result = await window.pysar.call("set_safe_mode", enabled, false)
      .catch((error) => ({ ok: false, error: String(error) }));
    if (result?.requiresConfirmation && !enabled) {
      const confirmed = await window.pysarConfirm(
        "This permits deleting, renaming and reindexing " +
        "resources that belong to the original game. Some non-structural property " +
        "edits remain available while Safe Mode is on.",
        {
          title: "Disable Safe Mode",
          confirmLabel: "Disable Safe Mode",
          danger: true,
        },
      );
      if (!confirmed) return;
      result = await window.pysar.call("set_safe_mode", false, true)
        .catch((error) => ({ ok: false, error: String(error) }));
    }
    if (!result?.ok) {
      setOpenError(result?.error || "Could not change Safe Mode");
      return;
    }
    setSafeMode(result.safeMode !== false);
    if (result.data) handleDataRefresh(result.data);
  }

  // close menu on outside click
  useEffectA(() => {
    if (!menuOpen) return;
    const handler = () => setMenuOpen(null);
    const keyHandler = (event) => { if (event.key === "Escape") setMenuOpen(null); };
    window.addEventListener("click", handler);
    window.addEventListener("keydown", keyHandler);
    return () => {
      window.removeEventListener("click", handler);
      window.removeEventListener("keydown", keyHandler);
    };
  }, [menuOpen]);

  function openApplicationMenu(menu) {
    if (menuOpen === menu) return;
    setMenuOpen(menu);
    if (menu === "file") refreshRecentArchives();
  }

  function closeApplicationMenu(menu) {
    setMenuOpen((current) => current === menu ? null : current);
  }

  // keyboard shortcuts for save, open, close
  useEffectA(() => {
    function onKey(event) {
      if (dumpStatus?.busy && (event.metaKey || event.ctrlKey) && ["s", "o", "w"].includes(event.key.toLowerCase())) {
        event.preventDefault();
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (event.shiftKey) saveArchiveAs();
        else saveArchive();
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "o") {
        event.preventDefault();
        requestOpen();
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "w" && archive) {
        event.preventDefault();
        requestClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dirty, archive, dumpStatus?.busy, loadingArchive]);

  const tab = tabs.find((t) => t.id === activeTab);
  const pendingCloseDocument = documents.find((document) => document.id === pendingCloseDocumentId);
  const windowDirtyCount = Math.max(1, Number(windowCloseInfo?.dirtyCount || 1));
  const unsavedMessage = unsavedAction === "window"
    ? (windowDirtyCount === 1
      ? "One open archive has unsaved changes. Save it before closing PYSAR?"
      : `${windowDirtyCount} open archives have unsaved changes. Save all of them before closing PYSAR?`)
    : `${pendingCloseDocument?.name || archive?.name || "This archive"} has unsaved changes. Save it before closing the tab?`;

  useEffectA(() => {
    const icon = pysarIconForTab(tab);
    const assetKey = icon ? icon.replace(/\.[^.]+$/, "").toLowerCase() : "project";
    window.pysar?.call("set_discord_presence_icon", assetKey).catch(() => {});
  }, [tab?.id, tab?.kind, tab?.view, tab?.item?.type]);

  // content router
  let content;
  if (!archive) {
    content = <Welcome onOpen={requestOpen} onOpenRecent={requestOpenRecent} onForgetRecent={forgetRecentArchive} recent={recentArchives} loading={loadingArchive} error={openError} />;
  } else if (!tab) {
    content = <div className="empty-state"><div className="empty-card" style={{ borderStyle: "solid" }}><h2>Nothing open</h2><p>Pick something from the sidebar.</p></div></div>;
  } else if (tab.kind === "view") {
    const soundTableActions = {
      onAddSound: () => {
        setAddSoundType(soundFilter === "ALL" ? "WAVE" : soundFilter);
        setShowAddSound(true);
      },
      onReplaceSound: requestSoundReplacement,
      onExportSound: exportSound,
      onRenameSound: renameSound,
      onDeleteSound: deleteSound,
      selectedSoundId: selectedItem?.kind === "sound" ? selectedItem.id : null,
    };
    if (tab.view === "all") content = <SoundsScreen filter={soundFilter} onFilterChange={changeSoundFilter} query={searchQuery} onClearSearch={() => setSearchQuery("")} onOpen={selectOnly} onActivate={openSound} onWarm={warmSoundPreview} onVisibleSoundsChange={rememberVisibleSounds} openId={selectedItem?.kind === "sound" ? selectedItem.id : null} density={tw.density} onPlay={play} playingId={playingId && isPlaying ? playingId : null} {...soundTableActions} />;
    else if (tab.view === "banks") content = <BanksTab query={searchQuery} onSelect={selectOrgItem} onActivate={openItem} onReplace={replaceBank} onExport={exportBank} onRename={renameBank} onDelete={deleteBank} openId={selectedItem?.kind === "bank" ? selectedItem.id : null} onDataRefresh={handleDataRefresh} onDirty={setDirty} onError={setOpenError} />;
    else if (tab.view === "groups") content = <GroupsTab query={searchQuery} onOpen={selectOrgItem} onNavigate={navigateToReferrer} onDelete={deleteGroup} openId={selectedItem?.kind === "group" ? selectedItem.id : null} safeMode={safeMode} onDataRefresh={handleDataRefresh} onDirty={setDirty} onError={setOpenError} />;
    else if (tab.view === "players") content = <PlayersTab query={searchQuery} onOpen={selectOrgItem} onRename={renamePlayer} onDelete={deletePlayer} onClear={() => setSelectedItem(null)} openId={selectedItem?.kind === "player" ? selectedItem.id : null} onDataRefresh={handleDataRefresh} onDirty={setDirty} onError={setOpenError} />;
    else if (tab.view === "archives") content = <ArchivesTab query={searchQuery} onOpen={selectOrgItem} onActivate={openItem} onNavigate={navigateToReferrer} onDelete={deleteWaveArchive} openId={selectedItem?.kind === "archive" ? selectedItem.id : null} onDataRefresh={handleDataRefresh} onDirty={setDirty} onError={setOpenError} />;
    else if (tab.view === "files") content = <FilesTab query={searchQuery} onOpen={selectFile} onNavigate={navigateToReferrer} openId={selectedItem?.kind === "file" ? selectedItem.id : null} openFileIndex={selectedItem?.kind === "file" ? (selectedItem.item?.fileIndex ?? null) : null} />;
  } else if (tab.kind === "sound") {
    if (tab.item.type === "SEQ") {
      content = (
        <SequenceDetail
          sound={tab.item}
          editorSourceText={seqEditorSourceBySound[tab.item.id] ?? null}
          onEditorSourceCommit={rememberSequenceEditorSource}
          durationMs={durationMs}
          isPlaying={isPlaying}
          playingSound={playingSound}
          selectedVariation={seqVariationBySound[tab.item.id] || null}
          onVariation={chooseSeqVariation}
          variations={seqVariationsBySound[tab.item.id] || []}
          onLoadVariations={loadSequenceVariations}
          onSoundChange={selectSharedSequenceSound}
          safeMode={safeMode}
          onDirty={setDirty}
          onDataRefresh={handleDataRefresh}
          onPlaybackInvalidate={invalidateSequencePlayback}
          onError={setOpenError}
          onRename={renameSound}
          onDelete={deleteSound}
        />
      );
    } else if (tab.item.type === "STRM") {
      content = (
        <StreamSoundDetail
          sound={tab.item}
          onNavigate={navigateToReferrer}
          onPlay={play}
          playingId={playingId && isPlaying ? playingId : null}
          refreshRevision={dataRevision}
          onPlaybackInvalidate={invalidateStrmSources}
          onReplace={requestSoundReplacement}
          onExport={exportSound}
          onRename={renameSound}
          onDelete={deleteSound}
        />
      );
    } else {
      content = <SoundDetail
        sound={tab.item}
        onNavigate={navigateToReferrer}
        onPlay={play}
        playingId={playingId && isPlaying ? playingId : null}
        playingSoundId={playingSound?.id ?? null}
        durationMs={durationMs}
        onReplace={requestSoundReplacement}
        onExport={exportSound}
        onRename={renameSound}
        onDelete={deleteSound}
      />;
    }
  } else if (tab.kind === "bank") {
    content = <BankDetail
      bank={tab.item}
      refreshRevision={bankContentRevision}
      onNavigate={navigateToReferrer}
      onDirty={setDirty}
      onDataRefresh={handleDataRefresh}
      onPlaybackInvalidate={invalidateBankSequencePlayback}
      onError={setOpenError}
      onPlayNote={(program, key) => playBankNote(tab.item, program, key)}
      playingNote={playingSound?.kind === "bank_note" && isPlaying ? playingSound : null}
      onReplace={replaceBank}
      onExport={exportBank}
      onRename={renameBank}
      onDelete={deleteBank}
    />;
  } else if (tab.kind === "archive") {
    const focusWave = (selectedItem?.kind === "wave" && selectedItem?.item?.archiveId === tab.item.id)
      ? selectedItem.id
      : null;
    content = (
      <ArchiveDetail
        archive={tab.item}
        onNavigate={navigateToReferrer}
        selectedWaveIndex={focusWave}
        onPlayWave={playWave}
        onImportWave={requestWaveArchiveImport}
        onExportWave={exportWaveArchiveSample}
        onReplaceWave={requestWaveArchiveReplacement}
        onDeleteWave={deleteWaveArchiveSample}
        refreshRevision={dataRevision}
        onSelectWave={(it) => {
          setSelectedItem(it);
          queueWaveForPreview(it);
          pushHistory(snapshot(activeTab, navView, it));
        }}
      />
    );
  }

  function tabAccent(t) {
    if (t.kind === "sound") return t.item?.type || "all";
    if (t.view) return t.view;
    const detailMap = { bank: "banks", group: "groups", player: "players", archive: "archives", file: "files" };
    return detailMap[t.kind] || t.kind || "all";
  }

  return (
    <div className="app">
      {/* Menu bar */}
      <div className="menubar">
        <div className={`menu-item${menuOpen === "file" ? " open" : ""}`}
             onMouseEnter={() => openApplicationMenu("file")}
             onMouseLeave={() => closeApplicationMenu("file")}
             onClick={(e) => {
               e.stopPropagation();
               const opening = menuOpen !== "file";
               setMenuOpen(opening ? "file" : null);
               if (opening) refreshRecentArchives();
             }}>
          File
          {menuOpen === "file" && (
            <div className="menu-dropdown" role="menu">
              <button className="menu-entry" onClick={() => { setMenuOpen(null); requestOpen(); }} disabled={loadingArchive || !!dumpStatus?.busy}>
                Open…<span className="shortcut">{appShortcut("O")}</span>
              </button>
              <div className="menu-submenu">
                <button
                  className="menu-entry menu-submenu-trigger"
                  aria-haspopup="menu"
                  onClick={(event) => event.stopPropagation()}
                >
                  Open Recent…<span className="submenu-arrow" aria-hidden="true">›</span>
                </button>
                <div className="menu-dropdown menu-submenu-panel" role="menu" aria-label="Open Recent">
                  {recentArchives.length === 0 && (
                    <button className="menu-entry" disabled>No Recent Archives</button>
                  )}
                  {recentArchives.map((item) => (
                    <button
                      key={item.path}
                      className="menu-entry menu-recent-entry"
                      title={item.path}
                      disabled={loadingArchive || !!dumpStatus?.busy || item.exists === false}
                      onClick={(event) => {
                        event.stopPropagation();
                        requestOpenRecent(item.path);
                      }}
                    >
                      <span className="menu-recent-text">
                        <span className="menu-recent-name">{item.name || item.path}</span>
                        <span className="menu-recent-path">{item.path}</span>
                      </span>
                      {item.exists === false && <span className="menu-recent-missing">Missing</span>}
                    </button>
                  ))}
                </div>
              </div>
              <div className="menu-sep" />
              <button className="menu-entry" onClick={() => { setMenuOpen(null); saveArchive(); }} disabled={!archive}>
                Save<span className="shortcut">{appShortcut("S")}</span>
              </button>
              <button className="menu-entry" onClick={() => { setMenuOpen(null); saveArchiveAs(); }} disabled={!archive}>
                Save As…<span className="shortcut">{appShortcut("S", { shift: true })}</span>
              </button>
              <div className="menu-sep" />
              <button className="menu-entry" onClick={() => { setMenuOpen(null); requestClose(); }} disabled={!archive}>
                Close<span className="shortcut">{appShortcut("W")}</span>
              </button>
            </div>
          )}
        </div>
        {archive && (
          <div className={`menu-item${menuOpen === "edit" ? " open" : ""}`}
               onMouseEnter={() => openApplicationMenu("edit")}
               onMouseLeave={() => closeApplicationMenu("edit")}
               onClick={(e) => { e.stopPropagation(); setMenuOpen(menuOpen === "edit" ? null : "edit"); }}>
            Edit
            {menuOpen === "edit" && (
              <div className="menu-dropdown">
                <button className="menu-entry" onClick={() => { setMenuOpen(null); setAddSoundType("WAVE"); setShowAddSound(true); }}>
                  Add Sound…
                </button>
                <button className="menu-entry" onClick={() => { setMenuOpen(null); if (selectedItem?.kind === "sound") requestSoundReplacement(selectedItem.id); }}
                        disabled={!selectedItem || selectedItem.kind !== "sound"}>
                  Replace Sound…
                </button>
                <div className="menu-sep" />
                <button className="menu-entry" onClick={() => { setMenuOpen(null); renameSelectedGroup(); }}
                        disabled={selectedItem?.kind !== "group" || !!selectedItem?.item?.protected}>
                  Rename Group…
                </button>
                <button className="menu-entry" onClick={() => { setMenuOpen(null); deleteSelectedGroup(); }}
                        disabled={selectedItem?.kind !== "group" || !!selectedItem?.item?.protected}>
                  Delete Group…
                </button>
                <div className="menu-sep" />
                <button className="menu-entry" onClick={() => { setMenuOpen(null); findUnusedArchiveResources(); }}>
                  Find Unused Resources…
                </button>
                <button className="menu-entry" onClick={chooseArchiveDump} disabled={!!dumpStatus?.busy}>
                  {dumpStatus?.busy ? "Dumping Archive…" : "Dump Archive…"}
                </button>
              </div>
            )}
          </div>
        )}
        <div className={`menu-item${menuOpen === "help" ? " open" : ""}`}
             onMouseEnter={() => openApplicationMenu("help")}
             onMouseLeave={() => closeApplicationMenu("help")}
             onClick={(e) => { e.stopPropagation(); setMenuOpen(menuOpen === "help" ? null : "help"); }}>
          Help
          {menuOpen === "help" && (
            <div className="menu-dropdown" role="menu">
              <button className="menu-entry" onClick={() => { setMenuOpen(null); setShowAbout(true); }}>
                About PYSAR
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="archive-tabbar" role="tablist" aria-label="Open archives">
        <div className="archive-tabs-scroll">
          {documents.map((document) => (
            <div
              role="tab"
              aria-selected={document.id === activeDocumentId}
              aria-disabled={loadingArchive || !!dumpStatus?.busy}
              tabIndex={document.id === activeDocumentId ? 0 : -1}
              key={document.id}
              className={`archive-tab${document.id === activeDocumentId ? " active" : ""}`}
              title={document.path || document.name}
              onClick={() => { if (!loadingArchive && !dumpStatus?.busy) activateArchiveDocument(document.id); }}
              onKeyDown={(event) => {
                if ((event.key === "Enter" || event.key === " ") && !loadingArchive && !dumpStatus?.busy) {
                  event.preventDefault();
                  activateArchiveDocument(document.id);
                }
              }}
            >
              <PysarIcon name="project.png" className="archive-tab-icon" />
              <span className="archive-tab-name">{document.name}</span>
              {document.dirty && <span className="archive-tab-dirty" title="Unsaved changes">{"\u2022"}</span>}
              <button
                type="button"
                className="archive-tab-close"
                aria-label={`Close ${document.name}`}
                title={`Close ${document.name}`}
                onClick={(event) => {
                  event.stopPropagation();
                  if (!loadingArchive && !dumpStatus?.busy) requestClose(document.id);
                }}
              >{"\u00d7"}</button>
            </div>
          ))}
        </div>
        <button
          type="button"
          className="archive-tab-add"
          onClick={requestOpen}
          disabled={loadingArchive || !!dumpStatus?.busy}
          title="Open another archive"
          aria-label="Open another archive"
        >+</button>
      </div>

      <div className="titlebar">
        <div className="tb-left">
          {!window.PYSAR_NATIVE_CHROME && (
            <div className="traffic">
              <span className="dot close"></span>
              <span className="dot min"></span>
              <span className="dot max"></span>
            </div>
          )}
          <div className="tb-brand" title={appMeta.version}>
            {dirty && <span className="dirty-dot" title="Unsaved changes"></span>}
          </div>
          <button className="tb-textbtn" onClick={() => setTweak("sidebarCollapsed", !tw.sidebarCollapsed)}>
            Sidebar
          </button>
          <button className="tb-iconbtn" onClick={() => goHistory(-1)} disabled={historyIndex <= 0} title="Back"><AppNavIcons.Back /></button>
          <button className="tb-iconbtn" onClick={() => goHistory(1)} disabled={historyIndex >= history.length - 1} title="Forward"><AppNavIcons.Forward /></button>
          <button
            className={`tb-textbtn safe-mode-toggle ${safeMode ? "active" : "unsafe"}`}
            onClick={toggleSafeMode}
            title={safeMode
              ? "Original game identities are protected; click to unlock destructive edits"
              : "Original game identities may be renamed, deleted or reindexed; click to lock"}
          >
            {safeMode ? "Safe Mode: On" : "Safe Mode: Off"}
          </button>
        </div>
        <div className="tb-center">
          <div className="tb-title">
            {archive ? (
              <span className="archive">
                {archive.name}
                {dirty && <span style={{ color: "var(--accent)", marginLeft: 6, fontSize: 11 }}>• modified</span>}
              </span>
            ) : (
              <span className="archive">No archive loaded</span>
            )}
          </div>
        </div>
        <div className="tb-right">
          <div className="tb-search">
            <input
              ref={searchInputRef}
              placeholder="Search across the archive…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              disabled={!archive}
            />
            <kbd>{appShortcut("F", { spaced: true })}</kbd>
          </div>
          {/* Virtual keyboard is intentionally hidden for now. The component stays
              wired below so we can bring it back once the workflow is ready. */}
          {false && (
            <button className={"tb-textbtn" + (kbdOpen ? " active" : "")} onClick={() => setKbdOpen(!kbdOpen)} title="Virtual keyboard">
              Keyboard
            </button>
          )}
          <button className={"tb-textbtn" + (tw.showInspector ? " active" : "")}
                  onClick={() => setTweak("showInspector", !tw.showInspector)} title="Inspector">
            Inspector
          </button>
        </div>
      </div>

      {archive && openError && (
        <div
          className="app-error-toast"
          role="alert"
          key={openErrorToast.id}
          onMouseEnter={() => setErrorToastHovered(true)}
          onMouseLeave={() => setErrorToastHovered(false)}
        >
          <span>{openError}</span>
          <button onClick={() => setOpenError(null)} aria-label="Dismiss error"><span aria-hidden="true">×</span></button>
        </div>
      )}

      <div className="workspace"
           data-sidebar={tw.sidebarCollapsed ? "collapsed" : "expanded"}
           data-inspector={tw.showInspector ? "visible" : "hidden"}
           style={{
             "--sidebar-w": tw.sidebarCollapsed ? "52px" : sidebarWidth + "px",
             "--inspector-w": tw.showInspector ? inspectorWidth + "px" : "0px",
           }}>
        <Sidebar active={navView} onPick={pickNav} archive={archive} collapsed={tw.sidebarCollapsed} />
        {!tw.sidebarCollapsed && (
          <div className="pane-resizer sidebar-resizer" onMouseDown={(e) => startPanelResize("sidebar", e)} title="Resize sidebar"></div>
        )}
        <main className="main">
          <div
            className="tabstrip"
            onWheel={(e) => {
              if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
              e.preventDefault();
              e.currentTarget.scrollLeft += e.deltaY;
            }}
          >
            {tabs.map((t) => (
              <div key={t.id}
                   className={"tab" + (t.id === activeTab ? " active" : "") + (t.id === draggingTabId ? " dragging" : "")}
                   style={accentVars(tabAccent(t), "tab")}
                   draggable
                   onDragStart={(e) => {
                     setDraggingTabId(t.id);
                     e.dataTransfer.effectAllowed = "move";
                     e.dataTransfer.setData("text/plain", t.id);
                   }}
                   onDragOver={(e) => {
                     e.preventDefault();
                     e.dataTransfer.dropEffect = "move";
                   }}
                   onDrop={(e) => {
                     e.preventDefault();
                     moveTab(e.dataTransfer.getData("text/plain") || draggingTabId, t.id);
                     setDraggingTabId(null);
                   }}
                   onDragEnd={() => setDraggingTabId(null)}
                   onClick={() => activateTab(t)}>
                <PysarIcon name={pysarIconForTab(t)} className="tab-icon" />
                <span className="name">{t.title}</span>
                {tabs.length > 1 && (
                  <span className="close-x" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }}>
                    x
                  </span>
                )}
              </div>
            ))}
          </div>
          <div className="content" key={activeDocumentId || "welcome"}>{content}</div>
        </main>
        {tw.showInspector && (
          <div className="pane-resizer inspector-resizer" onMouseDown={(e) => startPanelResize("inspector", e)} title="Resize inspector"></div>
        )}
        {tw.showInspector && (
          <Inspector
            key={activeDocumentId || "welcome"}
            active={inspectorTab}
            onSwitch={setInspectorTab}
            item={selectedItem}
            onNavigateReferrer={navigateToReferrer}
            onUpdateSound={updateSoundProperty}
            onRenameSound={renameSound}
            onDeleteSound={deleteSound}
            onRenameBank={renameBank}
            onReplaceBank={replaceBank}
            onExportBank={exportBank}
            onDeleteBank={deleteBank}
            onUpdateGroup={updateGroupProperty}
            onUpdatePlayer={updatePlayerProperty}
            onRenamePlayer={renamePlayer}
            onDeletePlayer={deletePlayer}
            onDeleteGroup={deleteGroup}
            onDeleteArchive={deleteWaveArchive}
            onReplaceSound={requestSoundReplacement}
            onExportSound={exportSound}
            onReplaceWave={requestWaveArchiveReplacement}
            onExportWave={exportWaveArchiveSample}
            onDeleteWave={deleteWaveArchiveSample}
            onUpdateWave={updateWaveArchiveSample}
          />
        )}
      </div>

      <MediaPlayerBar
        playingSound={playingSound}
        playingId={playingId}
        isPlaying={isPlaying}
        durationMs={durationMs}
        volume={volume}
        strmPlayback={playingSound?.type === "STRM" ? strmPlaybackBySound[playingSound.id] : null}
        seqPlayback={playingSound?.type === "SEQ" ? seqPlaybackBySound[playingSound.id] : null}
        autoPlayEnabled={soundListAutoPlayEnabled}
        seqVariations={playingSound?.type === "SEQ" ? seqVariationsBySound[playingSound.id] : null}
        onPlay={resumeCurrent}
        onPause={pause}
        onStop={stop}
        onSeek={seek}
        onNext={nextTransportSound}
        onVolume={changeVolume}
        onStrmLoopChange={changeStrmLoop}
        onSeqLoopChange={changeSeqLoop}
        onAutoPlayChange={changeSoundListAutoPlay}
        onStrmTrackSelectionChange={changeStrmTrackSelection}
        onSeqVariationChange={(variation) => chooseSeqVariation(playingSound, variation)}
      />

      {kbdOpen && <VirtualKeyboard onClose={() => setKbdOpen(false)} />}

      {/* Dialogs */}
      {showAddSound && (
        <AddSoundDialog
          onClose={() => setShowAddSound(false)}
          onDirtyChange={handleDirtyChange}
          onDataRefresh={handleDataRefresh}
          initialSoundType={addSoundType}
        />
      )}
      {replaceSoundId != null && (
        <ReplaceSoundDialog
          soundId={replaceSoundId}
          onClose={() => setReplaceSoundId(null)}
          onDirtyChange={handleDirtyChange}
          onDataRefresh={handleDataRefresh}
          onPlaybackInvalidate={invalidateSequencePlayback}
        />
      )}
      {replaceWaveTarget && (
        <ChooseRwavEncodingDialog
          target={replaceWaveTarget}
          onClose={() => setReplaceWaveTarget(null)}
          onReplace={(encoding, looped, loopStart) => {
            const target = replaceWaveTarget;
            setReplaceWaveTarget(null);
            if (target.kind === "sound") {
              replaceWaveSound(target.soundId, target.path, encoding, looped, loopStart);
            }
            else if (target.operation === "add") {
              addWaveArchiveSample(target.archiveId, target.path, encoding, looped, loopStart);
            } else {
              replaceWaveArchiveSample(
                target.archiveId,
                target.waveIndex,
                target.path,
                encoding,
                looped,
                loopStart,
              );
            }
          }}
        />
      )}
      {unsavedAction && (
        <UnsavedDialog
          onSave={handleUnsavedSave}
          onDiscard={handleUnsavedDiscard}
          onCancel={handleUnsavedCancel}
          busy={unsavedBusy}
          message={unsavedMessage}
          saveLabel={unsavedAction === "window" && windowDirtyCount > 1 ? "Save all" : "Save"}
          discardLabel={unsavedAction === "window" && windowDirtyCount > 1 ? "Don't save any" : "Don't save"}
        />
      )}
      {showAbout && (
        <AboutDialog appMeta={appMeta} onClose={() => setShowAbout(false)} onError={setOpenError} />
      )}
      {dumpStatus && (
        <DumpArchiveStatusDialog
          {...dumpStatus}
          onClose={() => { if (!dumpStatus.busy) setDumpStatus(null); }}
          onAbort={abortArchiveDump}
        />
      )}
      {dumpOptionsOpen && (
        <DumpArchiveOptionsDialog
          onClose={() => setDumpOptionsOpen(false)}
          onStart={(mode) => { setDumpOptionsOpen(false); dumpArchive(mode); }}
        />
      )}
      <PysarDialogHost />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
