const SCORE_INITIAL_ZOOM = 0.3;

function scoreQuarterToMidiQuarter(quarter, anchors) {
  if (!anchors?.length) return quarter;
  if (quarter <= Number(anchors[0].scoreQuarter)) {
    return Number(anchors[0].midiQuarter) + quarter - Number(anchors[0].scoreQuarter);
  }
  const last = anchors[anchors.length - 1];
  if (quarter >= Number(last.scoreQuarter)) {
    return Number(last.midiQuarter) + quarter - Number(last.scoreQuarter);
  }
  let low = 0;
  let high = anchors.length - 1;
  while (low + 1 < high) {
    const middle = Math.floor((low + high) / 2);
    if (Number(anchors[middle].scoreQuarter) <= quarter) low = middle;
    else high = middle;
  }
  const start = anchors[low];
  const end = anchors[high];
  const span = Math.max(0.000001, Number(end.scoreQuarter) - Number(start.scoreQuarter));
  const progress = (quarter - Number(start.scoreQuarter)) / span;
  return Number(start.midiQuarter)
    + (Number(end.midiQuarter) - Number(start.midiQuarter)) * progress;
}

function midiQuarterToMilliseconds(quarter, tempoMap) {
  const points = tempoMap?.length
    ? tempoMap
    : [{ quarter: 0, ms: 0, microsecondsPerQuarter: 500000 }];
  let low = 0;
  let high = points.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (Number(points[middle].quarter) <= quarter) low = middle;
    else high = middle - 1;
  }
  const point = points[low];
  return Number(point.ms || 0)
    + (quarter - Number(point.quarter || 0))
      * Number(point.microsecondsPerQuarter || 500000) / 1000;
}

function stabilizeScoreTimeline(timeline) {
  const blocks = [];
  for (let index = 0; index < timeline.length; index += 1) {
    blocks.push({ start: index, end: index, sum: Number(timeline[index].left), count: 1 });
    while (blocks.length > 1) {
      const current = blocks[blocks.length - 1];
      const previous = blocks[blocks.length - 2];
      if (previous.sum / previous.count <= current.sum / current.count) break;
      blocks.splice(blocks.length - 2, 2, {
        start: previous.start,
        end: current.end,
        sum: previous.sum + current.sum,
        count: previous.count + current.count,
      });
    }
  }
  for (const block of blocks) {
    const left = block.sum / block.count;
    for (let index = block.start; index <= block.end; index += 1) {
      timeline[index].left = left;
    }
  }
  return timeline;
}

function scoreCursorLeft(timeline, playheadMs) {
  if (!timeline?.length) return null;
  if (playheadMs <= timeline[0].ms) return timeline[0].left;
  if (playheadMs >= timeline[timeline.length - 1].ms) {
    return timeline[timeline.length - 1].left;
  }
  let low = 0;
  let high = timeline.length - 1;
  while (low + 1 < high) {
    const middle = Math.floor((low + high) / 2);
    if (timeline[middle].ms <= playheadMs) low = middle;
    else high = middle;
  }
  const start = timeline[low];
  const end = timeline[high];
  const duration = Math.max(0.001, end.ms - start.ms);
  const progress = Math.max(0, Math.min(1, (playheadMs - start.ms) / duration));
  return start.left + (end.left - start.left) * progress;
}

function horizontalOffsetWithin(element, ancestor) {
  let offset = 0;
  let current = element;
  while (current && current !== ancestor) {
    offset += Number(current.offsetLeft || 0);
    current = current.offsetParent;
  }
  return offset;
}

function buildScoreTimeline(cursor, payload) {
  const timeline = [];
  cursor.reset();
  cursor.SkipInvisibleNotes = true;
  cursor.show();
  for (let guard = 0; guard < 100000 && !cursor.Iterator.EndReached; guard += 1) {
    cursor.update();
    const timestamp = cursor.Iterator.CurrentSourceTimestamp;
    const sourceQuarter = Number(timestamp?.RealValue ?? timestamp?.realValue ?? 0) * 4;
    const measureIndex = Number(cursor.Iterator.CurrentMeasureIndex);
    const measureTimestamp = cursor.Iterator.CurrentMeasure?.AbsoluteTimestamp;
    const sourceMeasureQuarter = Number(
      measureTimestamp?.RealValue ?? measureTimestamp?.realValue,
    ) * 4;
    const sourceDuration = cursor.Iterator.CurrentMeasure?.Duration;
    const sourceDurationQuarter = Number(
      sourceDuration?.RealValue ?? sourceDuration?.realValue,
    ) * 4;
    const authoredStart = Number(payload.measureStartQuarters?.[measureIndex]);
    const authoredDuration = Number(payload.measureDurationQuarters?.[measureIndex]);
    const relativeSource = sourceQuarter - sourceMeasureQuarter;
    const relativeQuarter = (
      Number.isFinite(sourceDurationQuarter)
      && sourceDurationQuarter > 0
      && Number.isFinite(authoredDuration)
      && authoredDuration > 0
    ) ? relativeSource * authoredDuration / sourceDurationQuarter : relativeSource;
    const scoreQuarter = Number.isFinite(authoredStart) && Number.isFinite(sourceMeasureQuarter)
      ? authoredStart + relativeQuarter
      : sourceQuarter;
    const styledLeft = Number.parseFloat(cursor.cursorElement?.style?.left || "");
    const left = Number.isFinite(styledLeft) ? styledLeft : cursor.cursorElement?.offsetLeft;
    if (Number.isFinite(scoreQuarter) && Number.isFinite(left)) {
      const midiQuarter = scoreQuarterToMidiQuarter(scoreQuarter, payload.scoreTimeMap);
      const point = {
        ms: midiQuarterToMilliseconds(midiQuarter, payload.tempoMap),
        left,
      };
      const previous = timeline[timeline.length - 1];
      if (previous && Math.abs(previous.ms - point.ms) < 0.01) previous.left = point.left;
      else timeline.push(point);
    }
    cursor.Iterator.moveToNextVisibleVoiceEntry(false);
  }
  stabilizeScoreTimeline(timeline);
  const durationMs = Number(payload.durationMs) || 0;
  if (timeline.length && durationMs > 0) {
    const finalLeft = scoreCursorLeft(timeline, durationMs);
    while (timeline.length && timeline[timeline.length - 1].ms >= durationMs) timeline.pop();
    if (Number.isFinite(finalLeft)) timeline.push({ ms: durationMs, left: finalLeft });
  }
  if (cursor.cursorElement) {
    cursor.cursorElement.style.left = "0px";
    cursor.cursorElement.style.willChange = "transform";
  }
  return timeline;
}

function refreshScoreViewportMetrics(state) {
  if (!state?.viewport) return;
  state.viewportWidth = Number(state.viewport.clientWidth || 0);
  state.maxScrollLeft = Math.max(
    0,
    Number(state.viewport.scrollWidth || 0) - state.viewportWidth,
  );
  state.cursorOriginLeft = (
    horizontalOffsetWithin(state.stage, state.viewport)
    + state.baseCursorOrigin * state.scale
  );
}

function applyScoreZoom(state, zoom) {
  if (!state?.paper || !state.stage) return;
  const normalizedZoom = Math.max(0.3, Math.min(1.25, Number(zoom) || SCORE_INITIAL_ZOOM));
  const scale = normalizedZoom / state.renderZoom;
  state.zoom = normalizedZoom;
  state.scale = scale;
  state.paper.style.transform = `scale(${scale})`;
  const viewportWidth = Number(state.viewport?.clientWidth || 0);
  const viewportHeight = Number(state.viewport?.clientHeight || 0);
  state.stage.style.width = `${Math.ceil(Math.max(viewportWidth, state.baseWidth * scale))}px`;
  state.stage.style.height = `${Math.ceil(Math.max(viewportHeight, state.baseHeight * scale))}px`;
  refreshScoreViewportMetrics(state);
}

function renderScore(osmd, payload, viewport, paper, stage, zoom) {
  // Engrave once at a stable scale. Zooming the resulting SVG tree through
  // the compositor is dramatically cheaper than asking OSMD to rebuild its
  // complete graphical score after every slider movement.
  const renderZoom = 0.75;
  osmd.Zoom = renderZoom;
  osmd.render();
  const cursor = osmd.cursor;
  const timeline = buildScoreTimeline(cursor, payload);
  const viewportWidth = Number(viewport?.clientWidth || 0);
  const state = {
    osmd,
    payload,
    cursor,
    timeline,
    viewport,
    paper,
    stage,
    baseWidth: Math.max(1, Number(paper?.scrollWidth || 0)),
    baseHeight: Math.max(1, Number(paper?.scrollHeight || 0)),
    baseCursorOrigin: horizontalOffsetWithin(cursor.cursorElement?.offsetParent, paper),
    renderZoom,
    zoom: 1,
    scale: 1,
    viewportWidth,
    cursorOriginLeft: 0,
    maxScrollLeft: 0,
    cursorLeft: Number.NaN,
    scrollTarget: null,
    scrollDeadline: 0,
  };
  applyScoreZoom(state, zoom);
  return state;
}

function alignScoreViewport(state, playheadMs, active) {
  if (!active || !state?.viewport || !state.timeline?.length) return;
  const left = scoreCursorLeft(state.timeline, Math.max(0, Number(playheadMs) || 0));
  if (!Number.isFinite(left)) return;
  const target = Math.max(0, Math.min(
    state.maxScrollLeft,
    state.cursorOriginLeft + left * state.scale - state.viewportWidth * 0.4,
  ));
  state.scrollTarget = null;
  state.viewport.scrollLeft = target;
}

function positionScoreCursor(state, playheadMs, active, follow) {
  const cursor = state?.cursor;
  if (!active || !cursor?.cursorElement || !state.timeline?.length) {
    try { cursor?.hide(); } catch (_) {}
    return;
  }
  const left = scoreCursorLeft(state.timeline, Math.max(0, Number(playheadMs) || 0));
  if (!Number.isFinite(left)) return;
  if (cursor.hidden) cursor.show();
  cursor.cursorElement.style.left = "0px";
  if (!Number.isFinite(state.cursorLeft) || Math.abs(state.cursorLeft - left) > 0.01) {
    cursor.cursorElement.style.transform = `translate3d(${left}px, 0, 0)`;
    state.cursorLeft = left;
  }
  if (!follow || !state.viewport) return;

  const now = performance.now();
  if (
    state.scrollTarget != null
    && (Math.abs(state.viewport.scrollLeft - state.scrollTarget) < 1 || now > state.scrollDeadline)
  ) {
    state.scrollTarget = null;
  }
  if (state.scrollTarget != null) return;
  const visibleLeft = state.cursorOriginLeft + left * state.scale - state.viewport.scrollLeft;
  if (visibleLeft >= state.viewportWidth * 0.18 && visibleLeft <= state.viewportWidth * 0.72) return;
  const target = Math.max(0, Math.min(
    state.maxScrollLeft,
    state.cursorOriginLeft + left * state.scale - state.viewportWidth * 0.4,
  ));
  if (Math.abs(target - state.viewport.scrollLeft) < 1) return;
  state.scrollTarget = target;
  state.scrollDeadline = now + 600;
  try { state.viewport.scrollTo({ left: target, behavior: "smooth" }); }
  catch (_) { state.viewport.scrollLeft = target; }
}

function SequenceScoreView({
  soundId,
  revision = 0,
  playheadMs = 0,
  active = false,
  isPlaying = false,
  follow = false,
  sequenceVariation = null,
  visible = true,
}) {
  const viewportRef = React.useRef(null);
  const stageRef = React.useRef(null);
  const containerRef = React.useRef(null);
  const scoreStateRef = React.useRef(null);
  const playbackRef = React.useRef({ playheadMs, active, isPlaying, measuredAt: performance.now() });
  const followRef = React.useRef(follow);
  const zoomRef = React.useRef(SCORE_INITIAL_ZOOM);
  const [zoom, setZoom] = React.useState(SCORE_INITIAL_ZOOM);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [warning, setWarning] = React.useState(null);
  followRef.current = follow;
  zoomRef.current = zoom;

  React.useEffect(() => {
    const now = performance.now();
    const previous = playbackRef.current;
    const reported = Number(playheadMs) || 0;
    const predicted = previous.playheadMs
      + (previous.isPlaying ? Math.max(0, now - previous.measuredAt) : 0);
    const discontinuity = !previous.active
      || previous.isPlaying !== isPlaying
      || Math.abs(reported - predicted) > (isPlaying ? 80 : 1);
    playbackRef.current = { playheadMs: reported, active, isPlaying, measuredAt: now };
    positionScoreCursor(scoreStateRef.current, reported, active, false);
    if (follow && discontinuity) alignScoreViewport(scoreStateRef.current, reported, active);
  }, [playheadMs, active, isPlaying, follow]);

  React.useEffect(() => {
    if (!visible) return undefined;
    let frame = 0;
    function update(now) {
      const playback = playbackRef.current;
      const elapsed = playback.isPlaying ? Math.max(0, now - playback.measuredAt) : 0;
      positionScoreCursor(
        scoreStateRef.current,
        playback.playheadMs + elapsed,
        playback.active,
        follow && playback.isPlaying,
      );
      frame = window.requestAnimationFrame(update);
    }
    frame = window.requestAnimationFrame(update);
    return () => window.cancelAnimationFrame(frame);
  }, [soundId, revision, follow, visible]);

  React.useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => {
      const state = scoreStateRef.current;
      if (!state || state.viewport !== viewport) return;
      applyScoreZoom(state, state.zoom);
    });
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [soundId, revision]);

  React.useEffect(() => {
    const previous = scoreStateRef.current;
    if (!previous?.osmd) return undefined;
    const frame = window.requestAnimationFrame(() => {
      try {
        applyScoreZoom(previous, zoom);
        const playback = playbackRef.current;
        positionScoreCursor(previous, playback.playheadMs, playback.active, false);
        if (followRef.current) alignScoreViewport(previous, playback.playheadMs, playback.active);
        setError(null);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [zoom]);

  React.useEffect(() => {
    let mounted = true;
    let osmd = null;
    async function loadScore() {
      setLoading(true);
      setError(null);
      setWarning(null);
      scoreStateRef.current = null;
      containerRef.current?.replaceChildren();
      try {
        const library = window.opensheetmusicdisplay;
        if (!library?.OpenSheetMusicDisplay) {
          throw new Error("The bundled score renderer is unavailable");
        }
        const result = await window.pysar.call(
          "get_sequence_musicxml",
          soundId,
          sequenceVariation?.note ?? null,
          sequenceVariation?.program ?? null,
          sequenceVariation?.randomOverrides || null,
        );
        if (!mounted) return;
        if (!result?.ok) throw new Error(result?.error || "Could not create the score");
        osmd = new library.OpenSheetMusicDisplay(containerRef.current, {
          autoResize: false,
          backend: "svg",
          drawingParameters: "compacttight",
          drawCredits: false,
          drawTitle: false,
          drawPartNames: true,
          drawPartAbbreviations: true,
          followCursor: false,
          pageFormat: "Endless",
          renderSingleHorizontalStaffline: true,
          cursorsOptions: [{ type: 1, color: "#7c5cff", alpha: 0.95, follow: false }],
        });
        await osmd.load(result.musicxml);
        if (!mounted) return;
        const payload = {
          tempoMap: result.tempoMap || [],
          scoreTimeMap: result.scoreTimeMap || [],
          measureStartQuarters: result.measureStartQuarters || [],
          measureDurationQuarters: result.measureDurationQuarters || [],
          durationMs: Number(result.durationMs) || 0,
        };
        const state = renderScore(
          osmd,
          payload,
          viewportRef.current,
          containerRef.current,
          stageRef.current,
          zoomRef.current,
        );
        scoreStateRef.current = state;
        const playback = playbackRef.current;
        positionScoreCursor(state, playback.playheadMs, playback.active, false);
        if (followRef.current) alignScoreViewport(state, playback.playheadMs, playback.active);
        setWarning(result.truncated ? "Score is limited to the preview execution window." : null);
        setLoading(false);
      } catch (reason) {
        if (!mounted) return;
        setLoading(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
    loadScore();
    return () => {
      mounted = false;
      scoreStateRef.current = null;
      try { osmd?.cursor?.Dispose(); } catch (_) {}
      try { osmd?.clear(); } catch (_) {}
    };
  }, [soundId, revision, sequenceVariation?.id]);

  return (
    <div className="seq-score-shell">
      <div className="seq-score-controls">
        <span>{Math.round(zoom * 100)}%</span>
        <input
          type="range"
          min="30"
          max="125"
          step="5"
          value={Math.round(zoom * 100)}
          onChange={(event) => setZoom(Number(event.target.value) / 100)}
          aria-label="Score zoom"
        />
      </div>
      <div className="seq-score-view" ref={viewportRef} aria-busy={loading ? "true" : "false"}>
        {(loading || error || warning) && (
          <div className={`seq-score-status${error ? " error" : warning ? " warning" : ""}`}>
            {error || warning || "Transcribing and engraving score..."}
          </div>
        )}
        <div className="seq-score-stage" ref={stageRef}>
          <div className="seq-score-paper" ref={containerRef}></div>
        </div>
      </div>
    </div>
  );
}

window.SequenceScoreView = SequenceScoreView;
