(() => {
  // Once per document, however often the shell runs it: twice would double every
  // observer and listener below
  if (window.__pysarChrome) return;
  window.__pysarChrome = true;

  const root = document.documentElement;


  const CONTROLS = "button, input, select, textarea, label, form, a[href], [role=button], [data-no-drag]";
  const METHODS = {
    minimize: "window.minimize",
    maximize: "window.toggleMaximize",
    close: "window.close"
  };

  // pywebview adds window.pywebview.api some time after the page starts and fires pywebviewready once it's there.
  // The script may run before or after that.
  const api = new Promise((resolve) => {
    if (window.pywebview?.api?.call) {
      resolve(window.pywebview.api);
    }
    else {
      window.addEventListener("pywebviewready", () => resolve(window.pywebview.api), { once: true });
    }
  });


  // Every method goes through api.call, which answers with {value} or {error: {name, message}}.
  const call = (method, params = {}) =>
      api.then((pywebview) => pywebview.call(method, params)).then((answer) => {
        if (!answer.error)
          return answer.value;

        throw Object.assign(new Error(answer.error.message), { name: answer.error.name });
      }
  );


  // Boxes are [x, y, width, height] in CSS pixels from the page's top left.
  // The backend scales them by devicePixelRatio.
  const round = (value) => Math.round(value * 100) / 100;
  const rect = (element) => {
    const { left, top, width, height } = element.getBoundingClientRect();
    return [round(left), round(top), round(width), round(height)];
  };


  const shown = (element) => element.getClientRects().length > 0;

  // Whether an element paints a background of its own
  // Computed styles give a transparent color as "transparent" or rgba(…, 0)
  const painted = (style) =>
      style.backgroundImage !== "none" || !/^(transparent|rgba\(.*,\s*0\))$/.test(style.backgroundColor);

  // Where the system draws the caption buttons itself, it has to paint the
  // page's background under them, so they sit on the title bar like the page's own.
  const background = (bar) => {
    let element = bar.querySelector("[data-caption]")?.parentElement ?? bar;

    while (element !== bar && !painted(getComputedStyle(element)))
      element = element.parentElement;

    const style = getComputedStyle(element);
    return {
      image: style.backgroundImage,
      color: style.backgroundColor,
      rect: rect(element)
    };
  };


  const showState = (state) => {
    root.toggleAttribute("data-maximized", state.maximized);
    root.toggleAttribute("data-fullscreen", state.fullscreen);
    root.toggleAttribute("data-inactive", !state.focused);
    // "dark" or "light", as the window's theme (or the system) says
    root.dataset.theme = state.dark ? "dark" : "light";
  };

  const showCaption = ({ hover, pressed }) => {
    for (const button of document.querySelectorAll("[data-caption]")) {
      button.toggleAttribute("data-hover", button.dataset.caption === hover);
      button.toggleAttribute("data-pressed", button.dataset.caption === pressed);
    }
  };

  //
  // Whenever something may have moved, report() measures the bar again and sends
  // it, but only if it changed, and at most once a frame (schedule).
  //
  // - the bar's size (ResizeObserver), which also catches fonts and wrapping
  // - the window's size (resize)
  // - elements coming and going (MutationObserver)

  let bar = null;
  let frame = 0;
  let reported = "";
  const sizes = new ResizeObserver(() => schedule());
  const report = () => {
    frame = 0;
    // The page may swap or drop its bar, so it's looked up again every time
    const current = document.querySelector("[data-titlebar]");
    if (current !== bar) {
      if (bar)
        sizes.unobserve(bar);

      bar = current;

      if (bar)
        sizes.observe(bar);
    }

    // null => no title bar (none, or hidden), and the window has no draggable top
    const regions = bar && shown(bar) ? {
      bar: rect(bar),
      background: background(bar),

      // Caption buttons are reported on their own, not as controls
      controls: [...bar.querySelectorAll(CONTROLS)]
        .filter((element) => !element.closest("[data-caption]") && shown(element))
        .map(rect),

      caption: Object.fromEntries([...bar.querySelectorAll("[data-caption]")]
        .filter(shown)
        .map((element) => [element.dataset.caption, rect(element)])),

      scale: window.devicePixelRatio,
    } : null;

    const text = JSON.stringify(regions);
    if (text === reported)
      return Promise.resolve();

    reported = text;

    // If Python didn't take it, the next report sends it again
    return call("window.titleBarRegions", { regions }).catch(() => { reported = ""; });
  };

  const schedule = () => {
    if (!frame)
      frame = requestAnimationFrame(report);
  };

  new MutationObserver(schedule).observe(root, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["class", "style", "hidden", "data-titlebar", "data-caption", "data-no-drag"],
  });

  window.addEventListener("resize", schedule);
  window.addEventListener("gui:event", ({ detail }) => {
    if (detail.name === "window.state") {
      showState(detail.payload);
    }
    else if (detail.name === "window.caption") {
      showCaption(detail.payload);
    }
  });

  // anything beside the window controls are ordinary buttons
  document.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("[data-caption]") : null;
    const method = button && METHODS[button.dataset.caption];

    if (method)
      call(method).catch(console.error);
  });

  // keep the focus first bullshittery out of the title bar.
  // On windows, it can happen that the first button on the title bar is automatically focused
  // right after opening pysar, so we suppress that
  let interacted = false;
  for (const type of ["pointerdown", "keydown"]) {
    window.addEventListener(type, () => { interacted = true; }, { capture: true, once: true });
  }

  const unfocus = (element) => {
    if (!interacted && element instanceof HTMLElement && element.closest("[data-titlebar]"))
      element.blur();
  };

  document.addEventListener("focusin", (event) => unfocus(event.target), true);
  unfocus(document.activeElement);


  // start the jank but actually display only after everything is ready to be drawn
  const start = (chrome) => {
    root.dataset.platform = chrome.platform;
    root.dataset.chrome = chrome.chrome;
    showState(chrome.state);
    schedule();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      report().finally(() => call("window.ready").catch(() => {}));
    }));
  };

  // The shell passes the chrome along when it runs the script, which saves a round
  // trip to Python before the first layout
  if (window.__pysarChromeInfo) {
    start(window.__pysarChromeInfo);
  }
  else {
    call("window.chrome").then(start).catch(console.error);
  }
})();
