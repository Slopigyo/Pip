(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TASKS = {
    conversation: "Find notes for my Person Map",
    discover: "Discover — explore connected paths",
    brief: "Brief — compare paths and plan next steps",
  };
  const TOKEN_KEY = "jobwolf.sessionToken";
  const MESSAGE_LIMIT = 24;
  const STATEMENT_LIMIT = 100;
  let token = "";
  let sessionNotice = "";
  let state = null;
  let busy = false;
  let syncRequired = false;
  let authFailed = false;
  let selectionsInitialized = false;
  let selectedMessages = new Set();
  let selectedStatements = new Set();
  let selectedDocuments = [];
  let attachedDocuments = [];
  let resumeFormat = "paste";
  let expandedSourceId = null;
  let manualPacket = null;
  let pendingImport = null;
  let editingStatement = null;
  let confirmationResolver = null;
  let confirmationWord = "";
  let activeReply = null;
  let chatActivity = "";
  let chatOmissions = "";
  let followChat = true;
  let composing = false;
  let draftVersion = 0;
  let inputCheck = { key: "", status: "idle", result: null, error: null };
  let inputCheckTimer = null;
  const proposalChoices = new Map();
  const feedbackDrafts = new Map();

  class ActionError extends Error {
    constructor(message, { status = 0, uncertain = false } = {}) {
      super(message);
      this.status = status;
      this.uncertain = uncertain;
    }
  }

  function node(tag, className = "", text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }

  function button(text, handler, className = "") {
    const result = node("button", className, text);
    result.type = "button";
    result.dataset.lock = "";
    result.addEventListener("click", handler);
    return result;
  }

  function option(value, text) {
    const result = node("option", "", text);
    result.value = value;
    return result;
  }

  function human(value) {
    if (!value) return "Not specified";
    const text = String(value).replaceAll("_", " ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function dateText(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleString();
  }

  function excerpt(text, limit = 240) {
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  }

  function empty(text) {
    return node("p", "empty-state", text);
  }

  function metadata(entries) {
    const list = node("dl", "metadata");
    for (const [label, value] of entries) {
      list.append(node("dt", "", label), node("dd", "", value ?? "Not specified"));
    }
    return list;
  }

  function badge(text, tone = "") {
    return node("span", `badge ${tone}`.trim(), text);
  }

  function listSection(parent, title, values, heading = "h4", ordered = false) {
    parent.append(node(heading, "", title));
    if (!values.length) {
      parent.append(node("p", "small muted", "None listed — this does not mean none exist."));
      return;
    }
    const list = node(ordered ? "ol" : "ul");
    for (const value of values) list.append(node("li", "preserve-lines", value));
    parent.append(list);
  }

  function readToken() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const supplied = fragment.get("token");
    try {
      if (supplied) {
        token = supplied;
        sessionStorage.setItem(TOKEN_KEY, token);
      } else {
        token = sessionStorage.getItem(TOKEN_KEY) || "";
      }
    } catch {
      sessionNotice = "Session storage is unavailable. This tab can use the startup link now, but you will need to reopen that link after a page reload.";
    } finally {
      // Consume the credential before any API request. Never place it in a URL again.
      if (fragment.has("token")) window.history.replaceState(null, "", window.location.pathname);
    }
  }

  async function api(path, { method = "GET", body, download = false, onEvent = null, timeout = 30000 } = {}) {
    if (!token) throw new ActionError("Open the complete startup link printed by the running Pip test bench. This page needs that link's session access token.");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const mutation = method !== "GET" && !["/api/context", "/api/context/size", "/api/documents/preview"].includes(path);
    try {
      const headers = { "X-JobWolf-Token": token, Accept: onEvent ? "application/x-ndjson" : download ? "*/*" : "application/json" };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
      if (!response.ok) {
        let detail;
        let unchanged = false;
        try {
          const error = await response.json();
          if (typeof error?.detail === "string") detail = error.detail;
          unchanged = error?.state_changed === false;
        } catch {
          // Never display a raw error body: it may contain private or unsafe content.
        }
        throw new ActionError(detail || `The local server could not complete this request (HTTP ${response.status}).`, {
          status: response.status,
          uncertain: mutation && response.status >= 500 && !unchanged,
        });
      }
      if (onEvent) return await readChatStream(response, onEvent);
      return download ? await response.blob() : await response.json();
    } catch (error) {
      if (error instanceof ActionError) throw error;
      throw new ActionError(
        controller.signal.aborted
          ? "The request timed out. The server may still finish it. Reload state before retrying, especially after an inference."
          : "Could not complete the request to the local server. Check that Pip is still running, then reload state. No automatic retry was made.",
        { uncertain: mutation },
      );
    } finally {
      clearTimeout(timer);
    }
  }

  async function readChatStream(response, onEvent) {
    const invalid = () => new ActionError("The reply stream was interrupted or unexpected. Retry reply checks saved history first; it will not resend your message.", { uncertain: true });
    if (!response.body || !response.headers.get("Content-Type")?.startsWith("application/x-ndjson")) throw invalid();
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let buffer = "";
    let terminalEvent = null;
    let ended = false;
    const consume = (line) => {
      let event;
      try { event = JSON.parse(line); } catch { throw invalid(); }
      if (!event || typeof event !== "object" || terminalEvent) throw invalid();
      if (event.type === "done" || event.type === "error") terminalEvent = event;
      else onEvent(event);
    };
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          if (line.trim()) consume(line);
        }
        if (done) {
          ended = true;
          if (buffer.trim()) consume(buffer);
          // Let the server close cleanly, including its inference lease, before
          // exposing retry controls. EOF alone is not a confirmed save.
          if (!terminalEvent) throw invalid();
          onEvent(terminalEvent);
          return;
        }
      }
    } finally {
      if (!ended) {
        try { await reader.cancel(); } catch { /* The aborted connection may already be closed. */ }
      }
      reader.releaseLock();
    }
  }

  function clearErrors() {
    $("error-notice").hidden = true;
    $("error-notice").textContent = "";
    $("statement-error").hidden = true;
    $("statement-error").textContent = "";
    $("resume-error").hidden = true;
    $("resume-error").textContent = "";
    $("bench-notice").hidden = true;
    $("bench-notice").textContent = "";
  }

  function showError(error) {
    const known = error instanceof ActionError;
    if (known && (error.uncertain || error.status === 409)) syncRequired = true;
    if (known && (error.status === 401 || error.status === 403)) {
      authFailed = true;
      $("startup-notice").hidden = false;
      $("startup-notice").textContent = "The session link was not accepted. Reopen the complete startup link printed by the currently running Pip test bench. Do not share that link.";
    }
    let text = known ? error.message : "This action could not be completed. Your unsaved input has been kept. Reload state before retrying.";
    if (syncRequired) text += " Reload state to reconcile this tab, or use Retry reply for an unanswered message. Drafts and the frozen manual packet will be kept.";
    $("error-notice").textContent = text;
    $("error-notice").hidden = false;
    if ($("statement-dialog").open) {
      $("statement-error").textContent = text;
      $("statement-error").hidden = false;
    }
    if ($("resume-dialog").open) {
      $("resume-error").textContent = text;
      $("resume-error").hidden = false;
    }
  }

  function showBenchNotice(text, tone = "") {
    const notice = $("bench-notice");
    notice.className = `notice ${tone}`.trim();
    notice.textContent = text;
    notice.hidden = false;
  }

  async function runAction(label, action, { bench = false } = {}) {
    if (busy) return;
    busy = true;
    try {
      clearErrors();
      $("status-message").textContent = label;
      if (bench) showBenchNotice(label);
      updateControls();
      const message = await action();
      $("status-message").textContent = message || "Ready. Changes are saved locally.";
      if (bench) showBenchNotice($("status-message").textContent);
    } catch (error) {
      showError(error);
      $("status-message").textContent = bench ? "Bench action not completed. See the notice beside the task controls." : "Action not completed. See the message above the workspace.";
      if (bench) {
        showBenchNotice($("error-notice").textContent, "error");
        $("bench-notice").focus({ preventScroll: true });
        $("bench-notice").scrollIntoView({ block: "nearest" });
      }
    } finally {
      busy = false;
      updateControls();
    }
  }

  function currentSelection() {
    return {
      task: $("task-select").value,
      message_ids: state.workspace.messages.filter((message) => selectedMessages.has(message.id)).map((message) => message.id),
      statement_ids: state.workspace.statements.filter((statement) => selectedStatements.has(statement.id)).map((statement) => statement.id),
      document_ranges: selectedDocuments.map((range) => ({ ...range })),
    };
  }

  function generationBlock() {
    if (!state) return "Load your local workspace to begin.";
    if (!selectedMessages.size && !selectedStatements.size && !selectedDocuments.length) return "Choose at least one saved message, map entry, or document excerpt as context.";
    return "";
  }

  function scheduleInputCheck() {
    const request = state && $("provider-select").value === "ollama" && !generationBlock()
      ? { revision: state.revision, selection: currentSelection() } : null;
    const key = request ? JSON.stringify(request) : "";
    if (key !== inputCheck.key) {
      clearTimeout(inputCheckTimer);
      inputCheck = { key, status: "idle", result: null, error: null };
    }
    if (!request || busy || syncRequired || authFailed || !$("bench-tools").open || inputCheck.status !== "idle") return;
    const check = inputCheck;
    check.status = "pending";
    inputCheckTimer = setTimeout(async () => {
      if (check !== inputCheck) return;
      if (busy || !$("bench-tools").open) { check.status = "idle"; return; }
      try {
        const result = await api("/api/context/size", { method: "POST", body: request });
        if (result?.revision !== request.revision || JSON.stringify(result.selection) !== JSON.stringify(request.selection)
          || !Number.isSafeInteger(result.input_bytes) || result.input_bytes < 0
          || !Number.isSafeInteger(result.input_budget) || result.input_budget <= 0
          || result.fits !== (result.input_bytes <= result.input_budget)) {
          throw new ActionError("The server returned an unexpected size check. Nothing was sent to a model.");
        }
        check.status = "ready";
        check.result = result;
      } catch (error) {
        check.status = "error";
        check.error = error instanceof ActionError ? error : new ActionError("Could not check the selection size. No model was called.");
      }
      // A delayed check for an old selection must not enable Generate for a new one.
      if (check === inputCheck) updateControls();
    }, 180);
  }

  function inputSizeText(result) {
    const amount = `${result.input_bytes.toLocaleString()} / ${result.input_budget.toLocaleString()} bytes, including instructions and output schema.`;
    return result.fits
      ? `Local input fits: ${amount}`
      : `Too much context for one local proposal: ${amount} Select a smaller group of messages, fewer map notes, or fewer document lines. Ollama has not been called; your saved conversation is unchanged.`;
  }

  function renderInputCheck() {
    const result = inputCheck.result;
    const error = inputCheck.error;
    $("input-size").className = `notice ${error || (result && !result.fits) ? "warning" : ""}`.trim();
    $("input-size").textContent = generationBlock() || (error
      ? `Size check unavailable: ${error.message} No inference was started. ${error.status === 409 ? "Reload state to refresh this selection." : "Check again before generating."}`
      : result ? inputSizeText(result) : "Checking the selection size locally… No model is being called.");
    $("recheck-size").hidden = !error;
    $("recheck-size").disabled = busy || !state || authFailed || syncRequired;
    $("bench-reload-state").hidden = !syncRequired && error?.status !== 409;
    $("bench-reload-state").disabled = busy || !token;
  }

  function choiceCount(choice) {
    return choice ? choice.statements.size + choice.opportunities.size + Number(choice.brief) : 0;
  }

  function updateControls() {
    scheduleInputCheck();
    const locked = busy || !state || syncRequired || authFailed;
    document.querySelectorAll("[data-lock]").forEach((control) => { control.disabled = locked; });
    $("bench-tools").setAttribute("aria-busy", String(busy));
    $("reload-state").disabled = busy || !token;
    $("map-filter").disabled = !state;
    $("sync-notice").hidden = !syncRequired;
    const isManual = $("provider-select").value === "manual";
    $("message-text").disabled = !state;
    $("save-message").disabled = locked || !$("message-text").value.trim() || (!isManual && !$("model-select").value);
    $("save-message").textContent = isManual ? "Save for handoff" : "Send";
    $("message-help").textContent = isManual ? "Enter to save locally · Shift+Enter for a new line" : "Enter to send · Shift+Enter for a new line";
    $("message-draft-status").textContent = $("message-text").value.trim()
      ? busy ? "Next draft — not sent while Pip is working." : "Draft — not yet saved or sent."
      : isManual ? "Prepare your prompt in Notebook & bench tools." : "Chat does not change your Person Map.";
    $("chat-model").hidden = isManual;
    const unanswered = latestUnanswered();
    $("retry-reply").hidden = isManual || !unanswered || activeReply?.status === "streaming";
    $("retry-reply").disabled = busy || !token || authFailed || !$("model-select").value;
    $("chat-status").textContent = chatActivity || (isManual
      ? "Manual handoff: messages save locally. No automatic AI reply."
      : unanswered ? "Your last message is saved without a reply. Retry it or keep writing."
        : chatOmissions || "Your conversation stays here. Keep what helps; question the rest.");
    $("ollama-panel").hidden = isManual;
    $("manual-panel").hidden = !isManual;
    const provider = isManual ? "Manual handoff — you choose the tool and its provider" : `Ollama — ${$("model-select").value || "no model selected"}`;
    $("current-route").textContent = `Task: ${TASKS[$("task-select").value]} · Provider: ${provider}`;
    $("context-counts").textContent = `${selectedMessages.size} / ${MESSAGE_LIMIT} messages selected · ${selectedStatements.size} / ${STATEMENT_LIMIT} map entries selected · ${selectedDocuments.length} / 8 document excerpts`;
    const blockedReason = generationBlock();
    $("generation-help").textContent = blockedReason || "Only your checked, saved items will be used, not your unsent draft. Results must still be reviewed.";
    renderInputCheck();
    $("generate-button").disabled = locked || Boolean(blockedReason) || !$("model-select").value || inputCheck.status !== "ready" || !inputCheck.result.fits;
    $("prepare-packet").disabled = locked || Boolean(blockedReason) || Boolean(manualPacket);
    $("submit-reply").disabled = locked || !manualPacket || !$("manual-reply").value.trim();
    $("discard-packet").disabled = busy || !manualPacket;
    $("copy-prompt").disabled = busy || !manualPacket;
    $("refresh-models").disabled = busy || !state || authFailed;
    $("export-brief").disabled = locked || !state?.workspace.brief;
    $("apply-import").disabled = locked || !pendingImport;
    $("cancel-import").disabled = busy || !pendingImport;
    $("cancel-statement").disabled = busy;
    $("cancel-resume").disabled = busy;
    $("save-resume").disabled = locked || !$("resume-text").value.trim() || !$("resume-name").value.trim();
    $("resume-reload").hidden = !syncRequired;
    $("resume-reload").disabled = busy || !token;
    $("open-sources").hidden = !state?.workspace.documents.length;
    $("attachment-route").hidden = !attachedDocuments.length;
    $("attachment-route").textContent = attachedDocuments.length
      ? `These excerpts go with your next message only. ${provider}. Earlier conversation may repeat source content.` : "";
    document.querySelectorAll("[data-accept-proposal]").forEach((control) => {
      const count = choiceCount(proposalChoices.get(control.dataset.acceptProposal));
      control.disabled = locked || count === 0;
      control.textContent = `Accept selected (${count})`;
    });
    if (manualPacket) {
      $("packet-summary").textContent = `Manual handoff · ${TASKS[manualPacket.selection.task]} · frozen revision ${manualPacket.revision} · ${manualPacket.selection.message_ids.length} messages · ${manualPacket.selection.statement_ids.length} map entries · ${manualPacket.selection.document_ranges.length} document excerpts`;
      const stale = state && manualPacket.revision !== state.revision;
      $("packet-stale").hidden = !stale;
      $("packet-stale").textContent = stale
        ? `This packet belongs to revision ${manualPacket.revision}; the workspace is now revision ${state.revision}. Submission still uses the frozen revision and selection, and the server will reject it as stale. Keep any reply you need, then explicitly discard and prepare a new packet.`
        : "";
    }
  }

  function applyState(next, { resetSelections = false } = {}) {
    if (!next || !Number.isInteger(next.revision) || !next.workspace || !Number.isInteger(next.workspace.map_version)
      || !["messages", "statements", "opportunities", "proposals", "documents"].every((key) => Array.isArray(next.workspace[key]))
      || !next.domains || typeof next.domains !== "object" || Array.isArray(next.domains)) {
      throw new ActionError("The server returned an unexpected state. Reload before making another change.", { uncertain: true });
    }
    if (resetSelections) clearPrivateDrafts();
    state = next;
    if (activeReply && state.workspace.messages.at(-1)?.id !== activeReply.messageId) activeReply = null;
    const messageIds = new Set(state.workspace.messages.map((item) => item.id));
    const statementIds = new Set(state.workspace.statements.map((item) => item.id));
    if (!selectionsInitialized || resetSelections) {
      selectedMessages = new Set(state.workspace.messages.slice(-6).map((item) => item.id));
      selectedStatements = new Set(state.workspace.statements.slice(0, STATEMENT_LIMIT).map((item) => item.id));
      selectionsInitialized = true;
    } else {
      selectedMessages = new Set([...selectedMessages].filter((id) => messageIds.has(id)));
      selectedStatements = new Set([...selectedStatements].filter((id) => statementIds.has(id)));
    }
    const documentIds = new Set(state.workspace.documents.map((item) => item.id));
    selectedDocuments = selectedDocuments.filter((range) => documentIds.has(range.document_id));
    attachedDocuments = attachedDocuments.filter((range) => documentIds.has(range.document_id));
    const proposalIds = new Set(state.workspace.proposals.map((item) => item.id));
    const opportunityIds = new Set(state.workspace.opportunities.map((item) => item.id));
    for (const id of proposalChoices.keys()) if (!proposalIds.has(id)) proposalChoices.delete(id);
    for (const id of feedbackDrafts.keys()) if (!opportunityIds.has(id)) feedbackDrafts.delete(id);
    $("main").hidden = false;
    $("storage-status").textContent = `Server storage: ${state.storage} · Revision ${state.revision} · Map v${state.workspace.map_version}`;
    populateDomains();
    renderMessages();
    renderMessageContext();
    renderStatementContext();
    renderDocuments();
    renderDocumentSelections();
    renderProposals();
    renderMap();
    renderOpportunities();
    renderBrief();
    updateControls();
  }

  function populateDomains() {
    const filterValue = $("map-filter").value;
    const editValue = $("statement-domain").value;
    $("map-filter").replaceChildren(option("", "All seven domains"));
    $("statement-domain").replaceChildren();
    for (const [id, label] of Object.entries(state.domains)) {
      $("map-filter").append(option(id, label));
      $("statement-domain").append(option(id, label));
    }
    $("map-filter").value = filterValue;
    if (editValue) $("statement-domain").value = editValue;
  }

  function selectionCheckbox(id, selected, limit, labelText, accessibleText) {
    const label = node("label", "check-label");
    const input = node("input");
    input.type = "checkbox";
    input.checked = selected.has(id);
    input.dataset.lock = "";
    if (accessibleText) input.setAttribute("aria-label", accessibleText);
    input.addEventListener("change", () => {
      if (input.checked && selected.size >= limit) {
        input.checked = false;
        showError(new ActionError(`This task can include at most ${limit} items of this type. Uncheck another item first.`));
      } else if (input.checked) {
        selected.add(id);
      } else {
        selected.delete(id);
      }
      updateControls();
    });
    label.append(input, node("span", "", labelText));
    return label;
  }

  function latestUnanswered() {
    const last = state?.workspace.messages.at(-1);
    return last?.role === "user" ? last : null;
  }

  function scrollChat() {
    const list = $("message-list");
    if (followChat) list.scrollTop = list.scrollHeight;
    $("jump-latest").hidden = list.scrollHeight - list.scrollTop - list.clientHeight < 64;
  }

  function renderMessages() {
    const list = $("message-list");
    const scroll = list.scrollTop;
    list.replaceChildren();
    $("message-count").textContent = `${state.workspace.messages.length} saved`;
    if (!state.workspace.messages.length) {
      const welcome = node("div", "chat-welcome");
      welcome.append(node("p", "eyebrow", "A little room for possibility"), node("h3", "", "You don't need a job title to start."), node("p", "", "What has been on your mind lately? Something you enjoy, something that no longer fits, or a life you keep imagining. Rambling is welcome."));
      list.append(welcome);
    }
    for (const message of state.workspace.messages) {
      const card = node("article", `message-card ${message.role}`);
      const meta = node("div", "message-meta", message.role === "user" ? "You" : "Pip");
      meta.title = dateText(message.created_at);
      card.append(meta, node("p", "message-text", message.text));
      for (const range of message.document_ranges) card.append(documentExcerpt(range));
      list.append(card);
    }
    if (activeReply) {
      const card = node("article", `message-card assistant ${activeReply.status}`);
      card.append(node("div", "message-meta", `Pip · ${activeReply.model}`));
      activeReply.content = node("p", "message-text", activeReply.text || "Pip is thinking…");
      activeReply.note = node("p", "reply-note small", "");
      activeReply.card = card;
      card.append(activeReply.content, activeReply.note);
      list.append(card);
      updateReplyBubble();
    }
    list.setAttribute("aria-busy", String(activeReply?.status === "streaming"));
    list.scrollTop = scroll;
    scrollChat();
  }

  function updateReplyBubble() {
    if (!activeReply?.card) return;
    const streaming = activeReply.status === "streaming";
    activeReply.card.className = `message-card assistant ${activeReply.status}`;
    activeReply.content.textContent = activeReply.text || (streaming ? "Pip is thinking…" : "No completed reply received.");
    activeReply.note.hidden = streaming;
    activeReply.note.textContent = activeReply.status === "uncertain"
      ? "Interrupted — saving was not confirmed. Retry checks saved history first."
      : "Incomplete reply — not saved. Your message is safely in the conversation.";
    $("message-list").setAttribute("aria-busy", String(streaming));
    scrollChat();
  }

  function renderMessageContext() {
    const list = $("message-context");
    const scroll = list.scrollTop;
    list.replaceChildren();
    if (!state.workspace.messages.length) list.append(empty("Sent messages appear here for deliberate bench tasks."));
    state.workspace.messages.forEach((message, index) => {
      const label = selectionCheckbox(message.id, selectedMessages, MESSAGE_LIMIT, excerpt(message.text), `Use message ${index + 1} from ${message.role === "user" ? "you" : "Pip"} in the next bench task`);
      label.className = "context-choice";
      label.lastElementChild.prepend(node("small", "", `${index + 1} · ${message.role === "user" ? "You" : "Pip"} · ${dateText(message.created_at)}`));
      list.append(label);
    });
    list.scrollTop = scroll;
  }

  function recordChatContext(event) {
    if (!event.selection || !Array.isArray(event.selection.message_ids) || !Array.isArray(event.selection.statement_ids) || !Array.isArray(event.selection.document_ranges)
      || !Number.isInteger(event.omitted_messages) || !Number.isInteger(event.omitted_statements)) {
      throw new ActionError("The server returned an unexpected chat context. Reload before retrying.", { uncertain: true });
    }
    const selection = event.selection;
    chatOmissions = event.omitted_messages || event.omitted_statements
      ? `Context: ${event.omitted_messages} older messages and ${event.omitted_statements} map notes not included. Inspect in Notebook & bench tools.`
      : "";
    $("chat-context-summary").textContent = `${selection.message_ids.length} recent messages · ${selection.statement_ids.length} reviewed map notes · ${selection.document_ranges.length} document excerpts · ${event.omitted_messages} messages and ${event.omitted_statements} map notes omitted. Selected source text below; evidence and relationships outside this selection are excluded from the prompt.`;
    const list = $("chat-context");
    list.replaceChildren();
    for (const [kind, ids, items] of [
      ["Message", selection.message_ids, state.workspace.messages],
      ["Map note", selection.statement_ids, state.workspace.statements],
    ]) {
      for (const id of ids) {
        const item = items.find((value) => value.id === id);
        if (!item) continue;
        const detail = node("details");
        detail.append(node("summary", "small", `${kind}: ${excerpt(item.text, 100)}`), node("p", "preserve-lines", item.text), node("span", "reference", id));
        list.append(detail);
      }
    }
    for (const range of selection.document_ranges) list.append(documentExcerpt(range));
  }

  function renderStatementContext() {
    const list = $("statement-context");
    const scroll = list.scrollTop;
    list.replaceChildren();
    if (!state.workspace.statements.length) list.append(empty("No map entries yet. Use saved conversation to propose some, or add one manually."));
    for (const statement of state.workspace.statements) {
      const label = selectionCheckbox(statement.id, selectedStatements, STATEMENT_LIMIT, statement.text);
      label.className = "context-choice";
      label.lastElementChild.append(node("small", "", `${state.domains[statement.domain]} · ${human(statement.source)} · ${human(statement.confidence)}`));
      list.append(label);
    }
    list.scrollTop = scroll;
  }

  function references(parent, title, ids, kind = "statement") {
    parent.append(node("h5", "", title));
    if (!ids.length) {
      parent.append(node("p", "small muted", "No references supplied."));
      return;
    }
    const list = node("ul", "reference-list");
    for (const id of ids) {
      const target = (kind === "message" ? state.workspace.messages : state.workspace.statements).find((item) => item.id === id);
      const item = node("li");
      const prefix = kind === "message" && target ? `${target.role === "user" ? "You" : "Assistant"}: ` : "";
      item.append(node("span", "", target ? `${prefix}${excerpt(target.text)}` : "Referenced entry is not in the current workspace."), node("span", "reference", id));
      list.append(item);
    }
    parent.append(list);
  }

  function provenance(statement) {
    const details = node("details");
    details.append(node("summary", "", `Evidence & relationships (${statement.evidence.length} message quotes, ${statement.document_evidence.length} document quotes, ${statement.related_statement_ids.length} links)`));
    if (!statement.evidence.length) details.append(node("p", "small muted", "No message evidence attached. Review is not a substitute for evidence."));
    for (const evidence of statement.evidence) {
      const source = state.workspace.messages.find((message) => message.id === evidence.message_id);
      details.append(node("blockquote", "", evidence.quote));
      details.append(node("p", "small muted", source ? `From your saved message · ${dateText(source.created_at)}` : "Source message unavailable"));
      details.append(node("span", "reference", evidence.message_id));
    }
    for (const evidence of statement.document_evidence) {
      details.append(node("blockquote", "", evidence.quote));
      details.append(documentExcerpt(evidence));
    }
    references(details, "Related existing map entries", statement.related_statement_ids);
    if (statement.id) {
      details.append(metadata([
        ["Statement ID", statement.id], ["Origin", statement.origin],
        ["Created", dateText(statement.created_at)], ["Updated", dateText(statement.updated_at)],
      ]));
    }
    return details;
  }

  function statementContent(statement, proposed = false) {
    const content = node("div");
    content.append(node("p", "small muted", `${state.domains[statement.domain] || statement.domain} / ${statement.dimension} / ${statement.facet}`));
    content.append(node("p", "statement-text", statement.text));
    if (proposed && state.workspace.statements.some((s) => s.text.toLocaleLowerCase() === statement.text.toLocaleLowerCase())) content.append(node("p", "notice warning", "A note with this text is already in your map. Leave this duplicate unchecked unless you need a separate record."));
    const badges = node("div", "badge-group");
    badges.append(badge(statement.source === "document" ? "Document claim — not independently verified" : statement.source === "inferred" ? "Inferred — not an explicit fact" : "Explicit — directly stated", statement.source !== "explicit" ? "warning" : ""));
    badges.append(badge(proposed ? "Review pending" : statement.confirmed ? "Reviewed by you" : "Not yet reviewed", !proposed && statement.confirmed ? "accent" : ""));
    content.append(badges, metadata([
      ["Importance", human(statement.importance)], ["Flexibility", human(statement.flexibility)],
      ["Source", human(statement.source)], ["Confidence", human(statement.confidence)],
      ["Time horizon", human(statement.time_horizon)], ["Condition", statement.condition], ["Tension", statement.tension],
    ]), provenance(statement));
    return content;
  }

  function freshness(version) {
    const stale = version !== state.workspace.map_version;
    return node("p", stale ? "notice warning" : "small muted", stale
      ? `Stale: based on map v${version}; your map is now v${state.workspace.map_version}. Regenerate and review against the current map before relying on this.`
      : `Based on map v${version} · current map version. This does not verify its claims.`);
  }

  function opportunityContent(opportunity, persisted = false, unverifiedLinks = opportunity.unverified_links) {
    const content = node("div");
    content.append(node(persisted ? "h3" : "h4", "", opportunity.title));
    const badges = node("div", "badge-group");
    badges.append(badge(human(opportunity.kind)), badge("Hypothesis · unverified", "warning"));
    content.append(badges);
    if (unverifiedLinks) {
      content.append(node("p", "notice warning", "Unverified Person Map links were omitted: the model supplied links outside the selected context. Review this possibility's rationale carefully; no replacement evidence was inferred."));
    }
    if (persisted) content.append(freshness(opportunity.based_on_map_version));
    const heading = persisted ? "h4" : "h5";
    content.append(node(heading, "", "Why this path may fit"), node("p", "preserve-lines", opportunity.rationale));
    listSection(content, "Tradeoffs to consider", opportunity.tradeoffs, heading);
    listSection(content, "Questions and unknowns that matter", opportunity.unknowns, heading);
    listSection(content, "A way in · small investigations", opportunity.next_steps, heading, true);
    const refs = node("details");
    refs.append(node("summary", "", `Person Map connections and context (${opportunity.statement_ids.length} references)`));
    references(refs, "Existing map references", opportunity.statement_ids);
    if (persisted) refs.append(metadata([
      ["Origin", opportunity.origin], ["Feedback", human(opportunity.feedback)], ["Created", dateText(opportunity.created_at)],
    ]));
    content.append(refs);
    return content;
  }

  function briefContent(brief, persisted = false) {
    const content = node("div", "brief-body");
    content.append(node(persisted ? "h3" : "h4", "", brief.title));
    content.append(badge("Exploration plan · not verified world data", "warning"));
    if (persisted) {
      content.append(freshness(brief.based_on_map_version), metadata([["Origin", brief.origin], ["Created", dateText(brief.created_at)]]));
    }
    const heading = persisted ? "h4" : "h5";
    content.append(node(heading, "", "What matters and what differs"), node("p", "preserve-lines", brief.summary));
    listSection(content, "Reviewed hard gates", brief.hard_gates, heading);
    content.append(node(heading, "", "Paths to investigate"));
    if (brief.directions.length) {
      const paths = node("div", "path-list");
      brief.directions.forEach((direction, index) => {
        const path = node("section", "path-card");
        path.append(node(persisted ? "h5" : "h6", "", `Path ${index + 1}`), node("p", "preserve-lines", direction));
        paths.append(path);
      });
      content.append(paths);
    } else {
      content.append(node("p", "small muted", "No paths listed yet. This does not rule out other possibilities."));
    }
    listSection(content, "Questions that could change the choice", brief.open_questions, heading);
    listSection(content, "A small investigation plan", brief.next_steps, heading, true);
    listSection(content, "Search queries to take elsewhere", brief.search_queries, heading);
    return content;
  }

  function proposalItem(choice, kind, index, labelText, content) {
    const item = node("div", "proposal-item");
    const label = node("label", "check-label");
    const check = node("input");
    check.type = "checkbox";
    check.dataset.lock = "";
    check.checked = kind === "brief" ? choice.brief : choice[kind].has(index);
    check.addEventListener("change", () => {
      if (kind === "brief") choice.brief = check.checked;
      else if (check.checked) choice[kind].add(index);
      else choice[kind].delete(index);
      updateControls();
    });
    label.append(check, node("span", "", labelText));
    item.append(label, content);
    return item;
  }

  function renderProposals() {
    const list = $("proposal-list");
    list.replaceChildren();
    $("nav-review-count").textContent = state.workspace.proposals.length;
    $("proposal-count").textContent = `${state.workspace.proposals.length} pending`;
    if (!state.workspace.proposals.length) list.append(empty("Nothing waiting for review. Generate with Ollama or submit a manual JSON reply when you're ready."));
    state.workspace.proposals.forEach((proposal, position) => {
      if (!proposalChoices.has(proposal.id)) proposalChoices.set(proposal.id, { statements: new Set(), opportunities: new Set(), brief: false });
      const choice = proposalChoices.get(proposal.id);
      const card = node("article", "proposal");
      card.append(node("h3", "", `Proposal ${position + 1} · ${TASKS[proposal.selection.task]}`));
      card.append(metadata([["Provider / origin", proposal.origin], ["Created", dateText(proposal.created_at)]]), freshness(proposal.based_on_map_version));
      const selected = node("details");
      selected.append(node("summary", "", `Inspect the exact context (${proposal.selection.message_ids.length} messages, ${proposal.selection.statement_ids.length} map entries)`));
      references(selected, "Selected messages", proposal.selection.message_ids, "message");
      references(selected, "Selected existing map entries", proposal.selection.statement_ids);
      const assistant = node("details");
      assistant.append(node("summary", "", "Assistant response (may already be in the conversation)"), node("p", "preserve-lines", proposal.reply.assistant_text));
      card.append(selected, assistant);
      proposal.reply.proposed_statements.forEach((statement, index) => {
        card.append(proposalItem(choice, "statements", index, `Accept map addition ${index + 1}`, statementContent(statement, true)));
      });
      proposal.reply.opportunities.forEach((opportunity, index) => {
        card.append(proposalItem(choice, "opportunities", index, `Accept possibility ${index + 1}: ${opportunity.title}`, opportunityContent(opportunity, false, proposal.unverified_opportunity_indices?.includes(index))));
      });
      if (proposal.reply.brief) card.append(proposalItem(choice, "brief", 0, state.workspace.brief ? "Accept this brief (replaces the saved brief)" : "Accept this Search Brief", briefContent(proposal.reply.brief)));
      if (!proposal.reply.proposed_statements.length && !proposal.reply.opportunities.length && !proposal.reply.brief) {
        card.append(node("p", "notice", "This response proposes no map additions, possibilities, or brief. Read the assistant response and dismiss this review."));
      }
      const actions = node("div", "toolbar proposal-footer");
      const accept = button("Accept selected (0)", () => acceptProposal(proposal.id), "primary");
      accept.dataset.acceptProposal = proposal.id;
      actions.append(accept, button("Reject / dismiss proposal", () => rejectProposal(proposal.id)));
      card.append(actions);
      list.append(card);
    });
  }

  function renderMap() {
    const list = $("map-list");
    list.replaceChildren();
    const statements = state.workspace.statements;
    const reviewed = statements.filter((item) => item.confirmed).length;
    const inferred = statements.filter((item) => item.source === "inferred").length;
    $("map-summary").textContent = `Map v${state.workspace.map_version} · ${statements.length} entries · ${reviewed} reviewed by you · ${inferred} with inferred source. No completeness score: a useful map is not a filled-out form.`;
    for (const [domain, label] of Object.entries(state.domains)) {
      if ($("map-filter").value && $("map-filter").value !== domain) continue;
      const group = node("section", "domain");
      const members = statements.filter((item) => item.domain === domain);
      const heading = node("h3", "", label);
      heading.append(node("span", "count", members.length));
      group.append(heading);
      if (!members.length) group.append(empty("Not mapped yet. Nothing is assumed."));
      for (const statement of members) {
        const card = node("article", "card");
        card.append(statementContent(statement));
        const actions = node("div", "toolbar compact");
        const edit = button("Edit", () => openStatementDialog(statement));
        edit.setAttribute("aria-label", `Edit statement: ${excerpt(statement.text, 100)}`);
        const remove = button("Delete…", () => deleteStatement(statement), "danger");
        remove.setAttribute("aria-label", `Delete statement: ${excerpt(statement.text, 100)}`);
        actions.append(edit, remove);
        card.append(actions);
        group.append(card);
      }
      list.append(group);
    }
  }

  function renderOpportunities() {
    const list = $("opportunity-list");
    list.replaceChildren();
    $("opportunity-count").textContent = state.workspace.opportunities.length;
    if (!state.workspace.opportunities.length) list.append(empty("No saved possibilities yet. Choose Discover, select useful context, then review what comes back."));
    for (const opportunity of state.workspace.opportunities) {
      const card = node("article", "card");
      card.append(opportunityContent(opportunity, true));
      const draft = feedbackDrafts.get(opportunity.id) || { feedback: opportunity.feedback === "unreviewed" ? "" : opportunity.feedback, note: opportunity.feedback_note };
      const form = node("form", "feedback-form");
      const select = node("select");
      select.id = `feedback-${opportunity.id}`;
      select.required = true;
      select.dataset.lock = "";
      select.append(option("", "Choose your reaction"), option("worth_exploring", "Worth exploring"), option("not_for_me", "Not for me"), option("unsure", "Unsure"));
      select.value = draft.feedback;
      const selectLabel = node("label", "", "What is your reaction?");
      selectLabel.htmlFor = select.id;
      const note = node("textarea");
      note.id = `feedback-note-${opportunity.id}`;
      note.rows = 2;
      note.maxLength = 2000;
      note.value = draft.note;
      note.dataset.lock = "";
      const noteLabel = node("label", "", "What appeals, what doesn't, or what would you need to know? (optional)");
      noteLabel.htmlFor = note.id;
      const remember = () => feedbackDrafts.set(opportunity.id, { feedback: select.value, note: note.value });
      select.addEventListener("change", remember);
      note.addEventListener("input", remember);
      const submit = node("button", "", "Save feedback");
      submit.type = "submit";
      submit.dataset.lock = "";
      form.append(selectLabel, select, noteLabel, note, node("p", "help", "Feedback saves a conversation note for a future selected task. It does not verify the path or automatically change your Person Map."), submit);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        runAction("Saving your feedback locally…", async () => {
          const next = await api(`/api/opportunities/${encodeURIComponent(opportunity.id)}/feedback`, {
            method: "POST", body: { revision: state.revision, feedback: select.value, note: note.value.trim() },
          });
          feedbackDrafts.delete(opportunity.id);
          applyState(next);
          $("opportunity-title").focus();
          return "Feedback saved. The possibility remains an unverified hypothesis.";
        });
      });
      card.append(form);
      list.append(card);
    }
  }

  function renderBrief() {
    $("brief-content").replaceChildren(state.workspace.brief
      ? briefContent(state.workspace.brief, true)
      : empty("No saved brief yet. Choose the Brief task, review its proposed plan, and accept it here."));
  }

  function rangeKey(range) {
    return `${range.document_id}:${range.start_line}:${range.end_line}`;
  }

  function rangesOverlap(a, b) {
    return a.document_id === b.document_id && Math.max(a.start_line, b.start_line) <= Math.min(a.end_line, b.end_line);
  }

  function documentExcerpt(range) {
    const source = state.workspace.documents.find((item) => item.id === range.document_id);
    const details = node("details", "document-excerpt");
    details.append(node("summary", "small", `${source?.filename || "Source unavailable"} · lines ${range.start_line}–${range.end_line}`));
    if (source) {
      details.append(node("p", "preserve-lines small", source.text.split("\n").slice(range.start_line - 1, range.end_line).join("\n")));
      details.append(node("span", "reference", `Snapshot ${source.id} · SHA-256 ${source.text_sha256}`));
    }
    return details;
  }

  function renderDocumentSelections() {
    for (const [id, ranges, isChat] of [["chat-attachments", attachedDocuments, true], ["document-context", selectedDocuments, false]]) {
      const target = $(id);
      target.replaceChildren();
      if (isChat) target.hidden = !ranges.length;
      if (!ranges.length && !isChat) target.append(node("p", "help", "No document excerpts selected."));
      for (const range of ranges) {
        const item = documentExcerpt(range);
        item.append(button("Remove excerpt", () => {
          if (busy) return;
          if (isChat) attachedDocuments = attachedDocuments.filter((r) => rangeKey(r) !== rangeKey(range));
          else selectedDocuments = selectedDocuments.filter((r) => rangeKey(r) !== rangeKey(range));
          renderDocumentSelections();
          updateControls();
        }));
        target.append(item);
      }
    }
  }

  function showSources() {
    $("bench-tools").open = true;
    $("sources-title").focus();
    $("sources").scrollIntoView({ block: "start" });
  }

  function renderDocuments() {
    const list = $("source-list");
    list.replaceChildren();
    if (!state.workspace.documents.length) list.append(empty("No résumé needed to begin. Add one when it helps tell your story."));
    for (const source of state.workspace.documents) {
      const card = node("details", "card source-card");
      card.open = source.id === expandedSourceId;
      card.addEventListener("toggle", () => { if (card.open && card.isConnected) expandedSourceId = source.id; });
      const lines = source.text.split("\n");
      card.append(node("summary", "", `${source.filename} · ${lines.length} lines`));
      card.append(node("p", "help", `Saved ${dateText(source.created_at)} · ${source.format.toUpperCase()} · immutable text snapshot. This save date does not establish when the résumé was written.`));
      const text = node("pre", "source-text code-text", lines.map((line, i) => `${i + 1}  ${line}`).join("\n"));
      text.tabIndex = 0;
      text.setAttribute("aria-label", `Numbered text of ${source.filename}`);
      card.append(text);
      const fields = node("div", "source-range");
      const inputs = ["Start line", "End line"].map((title, index) => {
        const label = node("label", "small", title);
        const input = node("input");
        input.type = "number";
        input.min = "1";
        input.max = String(lines.length);
        input.step = "1";
        input.value = String(index ? Math.min(12, lines.length) : 1);
        input.dataset.lock = "";
        label.append(input);
        fields.append(label);
        return input;
      });
      const select = (isChat) => {
        if (busy) return;
        clearErrors();
        const range = { document_id: source.id, start_line: Number(inputs[0].value), end_line: Number(inputs[1].value) };
        const ranges = isChat ? attachedDocuments : selectedDocuments;
        if (!Number.isInteger(range.start_line) || !Number.isInteger(range.end_line) || range.start_line < 1 || range.end_line > lines.length || range.end_line < range.start_line) {
          showError(new ActionError("Choose an inclusive start/end line range within this source."));
          return;
        }
        if (ranges.length >= 8 || ranges.some((r) => rangesOverlap(r, range))) {
          showError(new ActionError("Use at most 8 non-overlapping excerpts. Remove an existing selection before replacing its range."));
          return;
        }
        ranges.push(range);
        renderDocumentSelections();
        if (isChat) {
          if (!$("message-text").value.trim()) {
            $("message-text").value = "I'd like to use these résumé excerpts as a starting point. What would be useful to explore beyond them?";
            draftVersion += 1;
          }
          $("message-text").focus();
          $("conversation").scrollIntoView({ block: "start" });
          $("status-message").textContent = "Excerpt attached to your next message. Review it above the composer, then press Send when ready.";
        } else {
          $("run").scrollIntoView({ block: "start" });
          showBenchNotice("Excerpt selected for this bench task. Review the rest of the selection and input size before generating.");
        }
        updateControls();
      };
      const actions = node("div", "toolbar compact");
      actions.append(button("Attach to next message", () => select(true)), button("Select for bench task", () => select(false)));
      card.append(fields, actions, node("span", "reference", `Snapshot ${source.id} · SHA-256 ${source.text_sha256}`));
      const management = node("div", "toolbar compact");
      management.append(button("Correct as new snapshot", () => openResumeDialog(source)), button("Delete source…", () => deleteDocument(source), "danger"));
      card.append(management);
      list.append(card);
    }
  }

  function clearResumeDraft() {
    $("resume-form").reset();
    $("resume-error").hidden = true;
    $("resume-error").textContent = "";
    $("resume-warnings").hidden = true;
    $("resume-warnings").textContent = "";
    resumeFormat = "paste";
  }

  function openResumeDialog(source = null) {
    if (busy || !state) return;
    clearResumeDraft();
    if (source) {
      $("resume-name").value = source.filename;
      $("resume-text").value = source.text;
      resumeFormat = source.format;
    }
    $("resume-dialog").showModal();
    updateControls();
  }

  async function previewResume() {
    const file = $("resume-file").files[0];
    if (!file || busy) return;
    await runAction("Reading your résumé locally — no AI call…", async () => {
      if (!file.size || file.size > 2 * 1024 * 1024) throw new ActionError("Choose a nonempty file no larger than 2 MiB, or paste text.");
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      const preview = await api("/api/documents/preview", { method: "POST", body: { filename: file.name, content_base64: btoa(binary) } });
      if (typeof preview?.text !== "string" || typeof preview.filename !== "string" || !["pdf", "docx", "txt", "md"].includes(preview.format) || !Array.isArray(preview.warnings)) throw new ActionError("Unexpected preview. Nothing was saved; paste the text instead.");
      $("resume-name").value = preview.filename;
      $("resume-text").value = preview.text;
      resumeFormat = preview.format;
      $("resume-warnings").textContent = preview.warnings.join(" ");
      $("resume-warnings").hidden = !preview.warnings.length;
      $("resume-file").value = "";
      return "Preview ready. Correct the text and remove unnecessary contact details before saving.";
    });
  }

  async function saveResume(event) {
    event.preventDefault();
    if (busy || !state || !$("resume-form").reportValidity()) return;
    await runAction("Saving only your reviewed résumé text…", async () => {
      const filename = $("resume-name").value.trim();
      if (/[\\/\x00-\x1f:]/.test(filename)) throw new ActionError("Use a display name without a path, slash, colon or control character.");
      const previous = new Set(state.workspace.documents.map((source) => source.id));
      const next = await api("/api/documents", { method: "POST", body: { revision: state.revision, filename, format: resumeFormat, text: $("resume-text").value } });
      expandedSourceId = next.workspace?.documents?.find((source) => !previous.has(source.id))?.id || null;
      applyState(next);
      $("resume-dialog").close();
      showSources();
      return "Reviewed text saved locally. Choose the lines to use in chat or a Person Map proposal; nothing has been sent to a model.";
    });
  }

  async function deleteDocument(source) {
    await runAction("Checking which saved items depend on this source…", async () => {
      const impact = await api(`/api/documents/${encodeURIComponent(source.id)}/impact`);
      if (impact.revision !== state.revision) throw new ActionError("The workspace changed. Reload before reviewing source deletion.", { status: 409 });
      const affected = state.workspace.statements.filter((s) => impact.statement_ids.includes(s.id));
      const confirmed = await confirmAction({
        title: `Delete ${source.filename}?`,
        description: `This removes the saved source text and ${impact.statement_ids.length} directly citing map note(s), including their copied quotes. It also clears all ${impact.proposal_count} pending proposal(s), ${impact.opportunity_count} possibilities, and ${impact.has_brief ? "the saved brief" : "any brief"} because free text may repeat source claims. ${impact.message_count} message attachment reference(s) will be removed.\n\n${affected.map((s) => "• " + s.text).join("\n")}\n\nConversation text and notes without document citations remain and may repeat this information. Review them separately or reset the workspace for broader deletion. Other source snapshots, original files, earlier exports, clipboard contents, and external copies remain. This tab's frozen handoff packet and last-chat preview will be cleared.`,
        action: "Delete source and affected items", word: "DELETE",
      });
      if (!confirmed) return "Source kept. Nothing changed.";
      const next = await api(`/api/documents/${encodeURIComponent(source.id)}`, { method: "DELETE", body: { revision: impact.revision, confirmation: "DELETE" } });
      manualPacket = null;
      activeReply = null;
      $("chat-context").replaceChildren();
      $("chat-context-summary").textContent = "Preview cleared after source deletion. Saved conversation remains.";
      applyState(next);
      renderManualPacket();
      showSources();
      return "Source and affected items deleted. Saved conversation, uncited notes, other source snapshots and external copies remain.";
    });
  }

  async function reloadState() {
    await runAction("Loading the local workspace…", async () => {
      const next = await api("/api/state");
      applyState(next);
      syncRequired = false;
      authFailed = false;
      $("startup-notice").textContent = sessionNotice;
      $("startup-notice").hidden = !sessionNotice;
      chatActivity = "";
      return "Local notebook loaded. Ready when you are.";
    });
  }

  async function loadModels() {
    const previous = $("model-select").value;
    $("model-select").replaceChildren(option("", "Choose an installed model"));
    $("model-status").textContent = "Checking installed models…";
    $("model-endpoint").textContent = "";
    try {
      const result = await api("/api/models");
      if (!result || !Array.isArray(result.models) || !result.models.every((model) => typeof model === "string") || typeof result.endpoint !== "string") {
        throw new ActionError("The server returned an unexpected model list.");
      }
      for (const model of result.models) $("model-select").append(option(model, model));
      if (result.models.includes(previous)) $("model-select").value = previous;
      else if (result.models.includes(result.preferred_model)) $("model-select").value = result.preferred_model;
      $("model-endpoint").textContent = `Server-reported Ollama endpoint: ${result.endpoint}`;
      $("model-status").textContent = result.models.length
        ? `${result.models.length} installed model(s). Lyra is the initial preference when available. Send starts a chat reply; bench proposals are a separate action.`
        : "No installed models found. Install a model with Ollama outside this app, then refresh, or use a manual handoff.";
    } catch (error) {
      $("model-status").textContent = "Could not list models. Check that Ollama is running, then refresh. Manual handoff and local editing are still available.";
      throw error;
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    if (busy || !state || syncRequired || authFailed || composing || !$("message-form").reportValidity()) return;
    const text = $("message-text").value.trim();
    const manual = $("provider-select").value === "manual";
    const model = $("model-select").value;
    if (!text || (!manual && !model)) return;
    const submittedVersion = draftVersion;
    const documentRanges = attachedDocuments.map((range) => ({ ...range }));
    $("message-text").focus({ preventScroll: true });
    await runAction("Saving your message locally…", async () => {
      chatActivity = "Saving your message…";
      updateControls();
      const previous = new Set(state.workspace.messages.map((message) => message.id));
      const next = await api("/api/messages", { method: "POST", body: { revision: state.revision, text, document_ranges: documentRanges } });
      const added = next.workspace?.messages?.find((message) => !previous.has(message.id) && message.role === "user");
      if (!added) throw new ActionError("The message save could not be confirmed. Reload before sending again.", { uncertain: true });
      if (selectedMessages.size < MESSAGE_LIMIT) selectedMessages.add(added.id);
      followChat = true;
      attachedDocuments = attachedDocuments.filter((range) => !documentRanges.some((sent) => rangeKey(sent) === rangeKey(range)));
      if (manual) {
        for (const range of documentRanges) {
          if (selectedDocuments.length < 8 && !selectedDocuments.some((selected) => rangesOverlap(selected, range))) selectedDocuments.push(range);
        }
      }
      applyState(next);
      // A draft changed during the save belongs to the user, not this request.
      if (draftVersion === submittedVersion) $("message-text").value = "";
      if (manual) {
        chatActivity = "Saved locally. Prepare your manual prompt in Notebook & bench tools.";
        $("bench-tools").open = true;
        return "Message saved. No model was called.";
      }
      return await replyTo(added.id, model);
    });
    if (chatActivity === "Saving your message…") {
      chatActivity = "Could not confirm the save. Check the notice above before sending again.";
      updateControls();
    }
  }

  async function replyTo(messageId, model) {
    activeReply = { messageId, model, text: "", status: "streaming" };
    chatActivity = "Pip is thinking… The first reply can take a little longer while the model loads.";
    renderMessages();
    updateControls();
    $("status-message").textContent = "Talking with your selected local model…";
    try {
      await api("/api/chat", {
        method: "POST", body: { revision: state.revision, message_id: messageId, model }, timeout: 300000,
        onEvent: (event) => {
          if (event.type === "context") {
            recordChatContext(event);
            chatActivity = `Pip is thinking… ${chatOmissions}`.trim();
            updateControls();
          } else if (event.type === "token" && typeof event.text === "string" && activeReply) {
            activeReply.text += event.text;
            if (activeReply.text.length > 24000) throw new ActionError("The streamed reply exceeded its size limit.", { uncertain: true });
            chatActivity = `Pip is replying… ${chatOmissions}`.trim();
            $("chat-status").textContent = chatActivity;
            updateReplyBubble();
          } else if (event.type === "done") {
            applyState(event.state);
            chatActivity = chatOmissions || "Reply saved. Your Person Map is unchanged.";
          } else if (event.type === "error" && typeof event.detail === "string") {
            throw new ActionError(event.detail, { status: event.status || 0 });
          } else {
            throw new ActionError("The server returned an unexpected reply event. Check saved history before retrying.", { uncertain: true });
          }
        },
      });
      return "Conversation saved locally.";
    } catch (error) {
      if (activeReply) {
        activeReply.status = error instanceof ActionError && error.uncertain ? "uncertain" : "failed";
        updateReplyBubble();
      }
      chatActivity = "Your message is saved. The reply did not finish here; Retry reply checks saved history first.";
      throw error;
    }
  }

  async function retryReply() {
    const message = latestUnanswered();
    const model = $("model-select").value;
    if (busy || !message || !model || authFailed || $("provider-select").value !== "ollama") return;
    $("message-text").focus({ preventScroll: true });
    await runAction("Checking saved history before retrying…", async () => {
      // A dropped stream may have committed after this tab lost its connection.
      // Refetch, then answer only the same unanswered message at the new revision.
      applyState(await api("/api/state"));
      syncRequired = false;
      if (latestUnanswered()?.id !== message.id) {
        chatActivity = "Saved history has changed or already has a reply. No duplicate was requested.";
        return "Conversation reconciled with local storage.";
      }
      followChat = true;
      return await replyTo(message.id, model);
    });
  }

  async function generate() {
    await runAction("Requesting a proposal from Ollama… Waiting for a complete, validated reply can take several minutes. No map changes are accepted automatically.", async () => {
      const blocked = generationBlock();
      if (blocked) throw new ActionError(blocked);
      if (inputCheck.status !== "ready") throw new ActionError("Wait for the local selection-size check before generating.");
      if (!inputCheck.result.fits) throw new ActionError(inputSizeText(inputCheck.result));
      const model = $("model-select").value;
      if (!model) throw new ActionError("Choose an installed Ollama model first.");
      const next = await api("/api/generate", {
        method: "POST", body: { revision: state.revision, selection: currentSelection(), model }, timeout: 300000,
      });
      applyState(next);
      $("review-title").focus();
      return "Response received. Review the proposed fields and explicitly choose what to accept.";
    }, { bench: true });
  }

  function renderManualPacket() {
    $("manual-packet").hidden = !manualPacket;
    $("manual-prompt").value = manualPacket ? manualPacket.prompt : "";
    if (!manualPacket) {
      $("manual-reply").value = "";
      $("packet-summary").textContent = "";
      $("packet-stale").textContent = "";
      $("packet-stale").hidden = true;
    }
    updateControls();
  }

  async function preparePacket() {
    await runAction("Preparing the selected context locally…", async () => {
      if (manualPacket) throw new ActionError("A packet is already open. Finish or explicitly discard it before preparing another.");
      const blocked = generationBlock();
      if (blocked) throw new ActionError(blocked);
      const result = await api("/api/context", { method: "POST", body: { revision: state.revision, selection: currentSelection() } });
      if (!result || !Number.isInteger(result.revision) || typeof result.prompt !== "string" || !result.selection
        || !Object.hasOwn(TASKS, result.selection.task) || !Array.isArray(result.selection.message_ids) || !Array.isArray(result.selection.statement_ids) || !Array.isArray(result.selection.document_ranges)) {
        throw new ActionError("The server returned an unexpected context packet. No handoff was created.");
      }
      // Preserve the server's exact snapshot, not a later selection or revision.
      manualPacket = Object.freeze({
        revision: result.revision,
        selection: Object.freeze({
          task: result.selection.task,
          message_ids: Object.freeze([...result.selection.message_ids]),
          statement_ids: Object.freeze([...result.selection.statement_ids]),
          document_ranges: Object.freeze(result.selection.document_ranges.map((range) => Object.freeze({ ...range }))),
        }),
        prompt: result.prompt,
      });
      renderManualPacket();
      $("manual-prompt").focus();
      return "Manual packet prepared. Inspect it before copying; nothing has been sent to Zed or ChatGPT.";
    }, { bench: true });
  }

  async function copyPrompt() {
    if (!manualPacket) return;
    await runAction("Copying the selected private context to your clipboard…", async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new ActionError("Clipboard unavailable.");
        await navigator.clipboard.writeText(manualPacket.prompt);
        return "Prompt copied. Your clipboard may be synced. Check your chosen tool's provider before pasting private context.";
      } catch {
        $("manual-prompt").focus();
        $("manual-prompt").select();
        return "Automatic clipboard access is unavailable. The prompt is selected; press Ctrl+C to copy it yourself.";
      }
    });
  }

  async function submitManualReply(event) {
    event.preventDefault();
    if (!manualPacket || !$("manual-reply-form").reportValidity()) return;
    await runAction("Validating the reply against its frozen context…", async () => {
      const packet = manualPacket;
      const reply = $("manual-reply").value.trim();
      if (!reply) throw new ActionError("Paste the JSON reply for this packet first.");
      const next = await api("/api/proposals/manual", {
        method: "POST", body: { revision: packet.revision, selection: packet.selection, reply_text: reply },
      });
      applyState(next);
      manualPacket = null;
      renderManualPacket();
      $("review-title").focus();
      return "Reply validated and staged. Inspect and select the proposed items before accepting them.";
    }, { bench: true });
  }

  async function acceptProposal(id) {
    const choice = proposalChoices.get(id);
    if (!choiceCount(choice)) return;
    await runAction("Accepting only the selected items…", async () => {
      const next = await api(`/api/proposals/${encodeURIComponent(id)}/accept`, {
        method: "POST",
        body: {
          revision: state.revision,
          statement_indices: [...choice.statements].sort((a, b) => a - b),
          opportunity_indices: [...choice.opportunities].sort((a, b) => a - b),
          include_brief: choice.brief,
        },
      });
      applyState(next);
      $("review-title").focus();
      return "Selected items accepted. Source and confidence remain distinct from your review. Select any new map entries explicitly for future tasks.";
    });
  }

  async function rejectProposal(id) {
    if (busy) return;
    const revision = state.revision;
    const confirmed = await confirmAction({
      title: "Dismiss this proposal?",
      description: "None of its unsaved map additions, possibilities, or brief will be accepted. Any assistant response already in the conversation stays there.",
      action: "Dismiss proposal",
    });
    if (!confirmed) return;
    await runAction("Dismissing the proposal…", async () => {
      applyState(await api(`/api/proposals/${encodeURIComponent(id)}/reject`, { method: "POST", body: { revision } }));
      $("review-title").focus();
      return "Proposal dismissed. The saved conversation has not been erased.";
    });
  }

  function openStatementDialog(statement = null) {
    editingStatement = {
      id: statement?.id || null,
      revision: state.revision,
      evidence: statement ? statement.evidence.map((item) => ({ ...item })) : [],
      document_evidence: statement ? statement.document_evidence.map((item) => ({ ...item })) : [],
      related_statement_ids: statement ? [...statement.related_statement_ids] : [],
    };
    const values = statement || {
      text: "", domain: $("map-filter").value || "background", dimension: "General", facet: "Observation",
      importance: "medium", flexibility: "preference", source: "explicit", confidence: "supported",
      condition: "", time_horizon: "unspecified", tension: "",
    };
    for (const field of ["text", "domain", "dimension", "facet", "importance", "flexibility", "source", "confidence", "condition", "time_horizon", "tension"]) {
      $(`statement-${field.replaceAll("_", "-")}`).value = values[field] ?? "";
    }
    $("statement-dialog-title").textContent = statement ? "Edit your map statement" : "Add your own statement";
    $("statement-provenance").replaceChildren(statement ? provenance(statement) : node("p", "help", "New manual statements start with explicit source, supported confidence, and no evidence quotes or related IDs. The server records your review."));
    $("statement-error").hidden = true;
    $("statement-dialog").showModal();
    $("statement-text").focus();
  }

  async function saveStatement(event) {
    event.preventDefault();
    if (!editingStatement || !$("statement-form").reportValidity()) return;
    await runAction("Saving your Person Map statement…", async () => {
      const edit = editingStatement;
      const value = (field) => $(`statement-${field}`).value.trim();
      const statement = {
        text: value("text"), domain: value("domain"), dimension: value("dimension"), facet: value("facet"),
        importance: value("importance"), flexibility: value("flexibility"), source: value("source"), confidence: value("confidence"),
        condition: value("condition") || null, time_horizon: value("time-horizon"), tension: value("tension") || null,
        evidence: edit.evidence, document_evidence: edit.document_evidence, related_statement_ids: edit.related_statement_ids,
      };
      if (statement.source === "explicit" && statement.document_evidence.length && !statement.evidence.length) throw new ActionError("Reviewing a document claim does not make it a direct user quotation. Keep its document source; add your correction in chat and select it for a new note.");
      if (statement.source === "document" && !statement.document_evidence.length) throw new ActionError("Document claims need a source citation. Use a proposal from selected résumé excerpts first.");
      if (!statement.text || !statement.dimension || !statement.facet) throw new ActionError("Statement, dimension, and facet must contain more than spaces.");
      const next = await api(edit.id ? `/api/statements/${encodeURIComponent(edit.id)}` : "/api/statements", {
        method: edit.id ? "PUT" : "POST", body: { revision: edit.revision, statement },
      });
      applyState(next);
      $("statement-dialog").close();
      $("map-title").focus();
      return "Statement saved. Existing evidence and relationship IDs were preserved.";
    });
  }

  async function deleteStatement(statement) {
    if (busy) return;
    const revision = state.revision;
    const confirmed = await confirmAction({
      title: "Delete this map entry?",
      description: `“${statement.text}”\n\nThis removes the map entry and derived output. The saved conversation may still contain your original words. Deleting this entry is not complete erasure of that information.`,
      action: "Delete map entry",
    });
    if (!confirmed) return;
    await runAction("Deleting the map entry…", async () => {
      applyState(await api(`/api/statements/${encodeURIComponent(statement.id)}`, { method: "DELETE", body: { revision } }));
      $("map-title").focus();
      return "Map entry deleted. Conversation text may still contain the original information.";
    });
  }

  function confirmAction({ title, description, action, word = "" }) {
    if (confirmationResolver) return Promise.resolve(false);
    confirmationWord = word;
    $("confirm-title").textContent = title;
    $("confirm-description").textContent = description;
    $("confirm-submit").textContent = action;
    $("confirm-word-group").hidden = !word;
    $("confirm-word-label").textContent = `Type ${word} to continue`;
    $("confirm-word").value = "";
    $("confirm-word").required = Boolean(word);
    $("confirm-submit").disabled = Boolean(word);
    return new Promise((resolve) => {
      confirmationResolver = resolve;
      $("confirm-dialog").showModal();
      $("confirm-cancel").focus();
    });
  }

  async function downloadFile(path, filename) {
    await runAction("Preparing a plaintext download…", async () => {
      const blob = await api(path, { download: true });
      const url = URL.createObjectURL(blob);
      const link = node("a");
      link.href = url;
      link.download = filename;
      link.hidden = true;
      document.body.append(link);
      try {
        link.click();
      } finally {
        link.remove();
        // Let the browser start reading the Blob before releasing it.
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      return "Plaintext download requested. Keep the file private; it is not protected by the workspace's DPAPI encryption.";
    });
  }

  function renderImport() {
    $("import-preview").hidden = !pendingImport;
    if (pendingImport) {
      const workspace = pendingImport.workspace;
      const count = (key) => Array.isArray(workspace[key]) ? workspace[key].length : "not supplied";
      $("import-summary").textContent = `Selected locally: ${pendingImport.name}. Messages: ${count("messages")}; document sources: ${workspace.documents?.length || 0}; map entries: ${count("statements")}; possibilities: ${count("opportunities")}; proposals: ${count("proposals")}; brief: ${workspace.brief ? "present" : "none"}. Nothing has been imported. The server will validate the complete workspace before replacing anything.`;
    } else {
      $("import-summary").textContent = "";
      $("import-file").value = "";
    }
    updateControls();
  }

  async function readImport() {
    const file = $("import-file").files[0];
    pendingImport = null;
    if (!file) { renderImport(); return; }
    await runAction("Reading the selected JSON file locally — not importing…", async () => {
      let workspace;
      try {
        workspace = JSON.parse(await file.text());
      } catch {
        renderImport();
        throw new ActionError("Could not read that file as JSON. Choose a raw JobWolf Workspace JSON export.");
      }
      if (!workspace || typeof workspace !== "object" || Array.isArray(workspace) || Object.hasOwn(workspace, "workspace")) {
        renderImport();
        throw new ActionError("Import expects a raw Workspace JSON object, not an API state wrapper or a list. Nothing was changed.");
      }
      pendingImport = { name: file.name, workspace };
      renderImport();
      return "File read locally. Review the selection, then explicitly confirm replacement to import.";
    });
  }

  function clearStatementEditor() {
    editingStatement = null;
    $("statement-form").reset();
    $("statement-provenance").replaceChildren();
    $("statement-error").textContent = "";
    $("statement-error").hidden = true;
  }

  function clearPrivateDrafts() {
    $("message-text").value = "";
    activeReply = null;
    selectedDocuments = [];
    attachedDocuments = [];
    expandedSourceId = null;
    clearResumeDraft();
    chatActivity = "";
    chatOmissions = "";
    followChat = true;
    $("chat-context-summary").textContent = "No chat request in this tab yet.";
    $("chat-context").replaceChildren();
    manualPacket = null;
    pendingImport = null;
    clearStatementEditor();
    proposalChoices.clear();
    feedbackDrafts.clear();
    $("map-filter").value = "";
    renderManualPacket();
    renderImport();
  }

  async function importWorkspace() {
    if (busy || !pendingImport) return;
    const selected = pendingImport;
    const revision = state.revision;
    const confirmed = await confirmAction({
      title: "Replace the entire workspace?",
      description: `Import “${selected.name}”? This replaces all saved conversation, résumé sources, map entries, possibilities, feedback, pending proposals, and the brief. It also clears drafts and the manual packet in this tab. It does not merge. Export a backup first if you want to keep the current workspace.`,
      action: "Replace workspace", word: "REPLACE",
    });
    if (!confirmed) return;
    await runAction("Validating and replacing the local workspace…", async () => {
      const next = await api("/api/import", { method: "POST", body: { revision, workspace: selected.workspace, confirmation: "REPLACE" } });
      applyState(next, { resetSelections: true });
      $("map-title").focus();
      return "Workspace replaced with the validated import. Check the default context selection before the next task.";
    });
  }

  async function resetWorkspace() {
    if (busy) return;
    const revision = state.revision;
    const confirmed = await confirmAction({
      title: "Delete the local workspace contents?",
      description: "This deletes all saved conversation, résumé sources, Person Map entries, possibilities, feedback, proposals, and the brief, and clears drafts in this tab. Export a backup first if needed. Old exports, clipboard contents, and data shared with other services are not deleted. This cannot be undone here.",
      action: "Delete workspace contents", word: "DELETE",
    });
    if (!confirmed) return;
    await runAction("Deleting the local workspace contents…", async () => {
      const next = await api("/api/reset", { method: "POST", body: { revision, confirmation: "DELETE" } });
      applyState(next, { resetSelections: true });
      requestAnimationFrame(() => $("message-text").focus());
      return "Local workspace reset. Old exports, clipboard contents, and external copies are unaffected.";
    });
  }

  function chooseMessages(count) {
    selectedMessages = new Set(count ? state.workspace.messages.slice(-count).map((message) => message.id) : []);
    renderMessageContext();
    updateControls();
  }

  function bindEvents() {
    $("reload-state").addEventListener("click", reloadState);
    $("add-resume").addEventListener("click", () => openResumeDialog());
    $("add-resume-source").addEventListener("click", () => openResumeDialog());
    $("open-sources").addEventListener("click", showSources);
    $("resume-file").addEventListener("change", previewResume);
    $("resume-form").addEventListener("submit", saveResume);
    $("resume-text").addEventListener("input", updateControls);
    $("resume-name").addEventListener("input", updateControls);
    $("cancel-resume").addEventListener("click", () => $("resume-dialog").close());
    $("resume-dialog").addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
    $("resume-dialog").addEventListener("close", clearResumeDraft);
    $("resume-reload").addEventListener("click", reloadState);
    $("bench-reload-state").addEventListener("click", reloadState);
    $("bench-tools").addEventListener("toggle", updateControls);
    $("recheck-size").addEventListener("click", () => {
      if (busy) return;
      inputCheck = { key: "", status: "idle", result: null, error: null };
      updateControls();
    });
    $("message-form").addEventListener("submit", sendMessage);
    $("message-text").addEventListener("input", () => { draftVersion += 1; updateControls(); });
    $("message-text").addEventListener("compositionstart", () => { composing = true; });
    $("message-text").addEventListener("compositionend", () => { composing = false; });
    $("message-text").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing || composing || event.keyCode === 229) return;
      event.preventDefault();
      if (!event.repeat && !busy && !$("save-message").disabled) $("message-form").requestSubmit();
    });
    $("retry-reply").addEventListener("click", retryReply);
    $("message-list").addEventListener("scroll", () => {
      const list = $("message-list");
      followChat = list.scrollHeight - list.scrollTop - list.clientHeight < 64;
      $("jump-latest").hidden = followChat;
    });
    $("jump-latest").addEventListener("click", () => { followChat = true; scrollChat(); });
    $("task-select").addEventListener("change", updateControls);
    $("provider-select").addEventListener("change", () => {
      chatActivity = "";
      if ($("provider-select").value === "manual") $("bench-tools").open = true;
      updateControls();
    });
    $("model-select").addEventListener("change", updateControls);
    $("manual-reply").addEventListener("input", updateControls);
    $("generate-button").addEventListener("click", generate);
    $("refresh-models").addEventListener("click", () => runAction("Checking installed Ollama models…", async () => {
      await loadModels();
      return "Model list refreshed. Choose the installed model you want to use.";
    }));
    $("messages-recent").addEventListener("click", () => chooseMessages(6));
    $("messages-all").addEventListener("click", () => chooseMessages(MESSAGE_LIMIT));
    $("messages-clear").addEventListener("click", () => chooseMessages(0));
    $("statements-all").addEventListener("click", () => {
      selectedStatements = new Set(state.workspace.statements.slice(0, STATEMENT_LIMIT).map((statement) => statement.id));
      renderStatementContext();
      updateControls();
    });
    $("statements-clear").addEventListener("click", () => {
      selectedStatements = new Set();
      renderStatementContext();
      updateControls();
    });
    $("prepare-packet").addEventListener("click", preparePacket);
    $("copy-prompt").addEventListener("click", copyPrompt);
    $("manual-reply-form").addEventListener("submit", submitManualReply);
    $("discard-packet").addEventListener("click", async () => {
      if (busy || !manualPacket) return;
      const confirmed = await confirmAction({
        title: "Discard this frozen packet?",
        description: "The prompt and any pasted reply will be removed from this tab. This does not delete the saved conversation or map, or recall anything already copied or sent elsewhere. Preparing a new packet is a separate action.",
        action: "Discard packet",
      });
      if (confirmed) {
        manualPacket = null;
        renderManualPacket();
        $("status-message").textContent = "Manual packet discarded. Choose context explicitly before preparing another.";
      }
    });
    $("map-filter").addEventListener("change", () => { renderMap(); updateControls(); });
    $("add-statement").addEventListener("click", () => openStatementDialog());
    $("statement-form").addEventListener("submit", saveStatement);
    $("cancel-statement").addEventListener("click", () => $("statement-dialog").close());
    $("statement-dialog").addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
    $("statement-dialog").addEventListener("close", clearStatementEditor);
    $("confirm-word").addEventListener("input", () => { $("confirm-submit").disabled = $("confirm-word").value !== confirmationWord; });
    $("confirm-form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (confirmationWord && $("confirm-word").value !== confirmationWord) return;
      $("confirm-dialog").close("confirm");
    });
    $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close("cancel"));
    $("confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); $("confirm-dialog").close("cancel"); });
    $("confirm-dialog").addEventListener("close", () => {
      const resolve = confirmationResolver;
      confirmationResolver = null;
      $("confirm-word").value = "";
      $("confirm-description").textContent = "";
      if (resolve) resolve($("confirm-dialog").returnValue === "confirm");
    });
    $("export-workspace").addEventListener("click", () => downloadFile("/api/export", "jobwolf-workspace.json"));
    $("export-brief").addEventListener("click", () => downloadFile("/api/brief.md", "jobwolf-search-brief.md"));
    $("import-file").addEventListener("change", readImport);
    $("cancel-import").addEventListener("click", () => { pendingImport = null; renderImport(); });
    $("apply-import").addEventListener("click", importWorkspace);
    $("reset-workspace").addEventListener("click", resetWorkspace);
    window.addEventListener("beforeunload", (event) => {
      if (busy || manualPacket || pendingImport || editingStatement || feedbackDrafts.size || $("message-text").value.trim() || $("resume-text").value.trim() || attachedDocuments.length) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  async function init() {
    readToken();
    bindEvents();
    updateControls();
    if (!token) {
      $("startup-notice").hidden = false;
      $("startup-notice").textContent = "No session access token was found. Start the Pip Windows test bench and open the complete startup link it prints, including its token fragment. Do not type the token into a form, query string, or exported file, and do not share the startup link.";
      $("status-message").textContent = "Waiting for an authenticated startup link. No workspace data has been requested.";
      return;
    }
    await reloadState();
    if (state && !authFailed) {
      $("message-list").scrollTop = $("message-list").scrollHeight;
      await runAction("Checking installed Ollama models…", async () => {
        await loadModels();
        return "Ready. Type a message and press Enter to talk with Pip.";
      });
    }
  }

  init();
})();
