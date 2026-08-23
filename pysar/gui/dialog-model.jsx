(function installPysarDialogModel(global) {
  "use strict";

  const ACTION_TONES = new Set(["neutral", "primary", "danger"]);
  const OUTCOME_STATUSES = new Set([
    "retained",
    "modified",
    "deleted",
    "warning",
    "unresolved",
  ]);

  function asText(value, fallback = "") {
    const text = String(value ?? "").trim();
    return text || fallback;
  }

  function normalizeSelection(selection, actionId) {
    if (selection == null) return null;
    const options = Array.isArray(selection.options)
      ? selection.options.map((option, index) => {
          const value = String(option?.value ?? "");
          if (!value) throw new Error(`Consequence action '${actionId}' has a selection option without a value`);
          return {
            value,
            label: asText(option?.label, value),
            description: asText(option?.description),
            disabled: !!option?.disabled,
            order: index,
          };
        })
      : [];
    const values = new Set();
    for (const option of options) {
      if (values.has(option.value)) {
        throw new Error(`Consequence action '${actionId}' has duplicate selection value '${option.value}'`);
      }
      values.add(option.value);
    }
    const requested = String(selection.value ?? "");
    const firstEnabled = options.find((option) => !option.disabled)?.value ?? "";
    return {
      label: asText(selection.label, "Replacement"),
      options,
      value: values.has(requested) && !options.find((option) => option.value === requested)?.disabled
        ? requested
        : firstEnabled,
      required: selection.required !== false,
    };
  }

  function normalizeAction(action, index) {
    const id = asText(action?.id);
    if (!id) throw new Error(`Consequence action ${index + 1} needs an id`);
    const tone = ACTION_TONES.has(action?.tone) ? action.tone : "neutral";
    return {
      id,
      label: asText(action?.label, id),
      description: asText(action?.description),
      confirmLabel: asText(action?.confirmLabel, action?.label || id),
      tone,
      selection: normalizeSelection(action?.selection, id),
    };
  }

  function normalizeOutcome(value) {
    if (value == null) return { text: "No change", status: "retained" };
    if (typeof value !== "object") {
      return { text: String(value), status: "retained" };
    }
    return {
      text: asText(value.text, "No change"),
      status: OUTCOME_STATUSES.has(value.status) ? value.status : "retained",
      detail: asText(value.detail),
    };
  }

  function normalizeResource(row, index, actionIds) {
    const resource = row?.resource && typeof row.resource === "object"
      ? row.resource
      : { name: row?.resource };
    const outcomes = {};
    for (const actionId of actionIds) {
      if (!Object.prototype.hasOwnProperty.call(row?.outcomes || {}, actionId)) {
        throw new Error(`Resource '${row?.id || index}' has no outcome for action '${actionId}'`);
      }
      outcomes[actionId] = normalizeOutcome(row?.outcomes?.[actionId]);
    }
    return {
      id: asText(row?.id, `resource-${index}`),
      resource: {
        badge: asText(resource?.badge, "ITEM").toUpperCase(),
        name: asText(resource?.name, "Unnamed resource"),
        detail: asText(resource?.detail),
      },
      outcomes,
    };
  }

  function PysarNormalizeConsequenceOptions(options = {}) {
    if (!Array.isArray(options.actions) || options.actions.length < 1 || options.actions.length > 3) {
      throw new Error("A consequence dialog requires between one and three actions");
    }
    const actions = options.actions.map(normalizeAction);
    const ids = new Set();
    for (const action of actions) {
      if (ids.has(action.id)) throw new Error(`Duplicate consequence action id '${action.id}'`);
      ids.add(action.id);
    }
    const requested = asText(options.initialAction);
    const initialAction = ids.has(requested) ? requested : actions[0].id;
    const resources = Array.isArray(options.resources)
      ? options.resources.map((row, index) => normalizeResource(row, index, ids))
      : [];
    return {
      title: asText(options.title, "Confirm action"),
      caption: asText(options.caption, "Exact consequences"),
      cancelLabel: asText(options.cancelLabel, "Cancel"),
      width: Math.max(440, Math.min(920, Number(options.width) || 760)),
      actions,
      resources,
      initialAction,
    };
  }

  function PysarInitialConsequenceSelections(model) {
    return Object.fromEntries(model.actions.map((action) => [
      action.id,
      action.selection?.value ?? null,
    ]));
  }

  function PysarConsequenceRowsForAction(model, actionId) {
    if (!model.actions.some((action) => action.id === actionId)) return [];
    return model.resources.map((row) => ({
      id: row.id,
      resource: row.resource,
      outcome: row.outcomes[actionId],
    }));
  }

  function PysarConsequenceResult(model, actionId, selections = {}) {
    const action = model.actions.find((candidate) => candidate.id === actionId);
    if (!action) throw new Error(`Unknown consequence action '${actionId}'`);
    const selection = action.selection ? String(selections[action.id] ?? "") : null;
    if (action.selection?.required && !selection) {
      throw new Error(`Consequence action '${actionId}' requires a selection`);
    }
    if (action.selection && selection && !action.selection.options.some(
      (option) => option.value === selection && !option.disabled,
    )) {
      throw new Error(`Invalid selection '${selection}' for consequence action '${actionId}'`);
    }
    return { action: action.id, selection };
  }

  Object.assign(global, {
    PysarNormalizeConsequenceOptions,
    PysarInitialConsequenceSelections,
    PysarConsequenceRowsForAction,
    PysarConsequenceResult,
  });
})(typeof window !== "undefined" ? window : globalThis);
