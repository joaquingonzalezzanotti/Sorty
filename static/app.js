function getDefaultMeta() {
  return { budget: "", deadline: "", note: "" };
}

const state = {
  participants: [],
  exclusions: [],
  meta: getDefaultMeta(),
  emailMode: "smtp",
  editingId: null,
  isSending: false,
  exclusionIssue: null,
};

const participantForm = document.getElementById("participant-form");
const exclusionForm = document.getElementById("exclusion-form");
const toast = document.getElementById("toast");
const themeToggle = document.getElementById("theme-toggle");
const adminCheckbox = document.getElementById("is-admin");
const adminWrapper = document.getElementById("admin-wrapper");
const cancelEditBtn = document.getElementById("cancel-edit");
const nameInput = document.getElementById("name");
const emailInput = document.getElementById("email");
const sendBtn = document.getElementById("send");
const sendOverlay = document.getElementById("send-overlay");
const sendSpinner = document.getElementById("send-spinner");
const sendCheck = document.getElementById("send-check");
const sendOverlayText = document.getElementById("send-overlay-text");
const sendOverlaySub = document.getElementById("send-overlay-sub");
const sendOverlayOk = document.getElementById("send-overlay-ok");
const submitBtn = participantForm.querySelector("button[type='submit']");
const budgetInput = document.getElementById("budget");
const deadlineInput = document.getElementById("deadline");
const noteInput = document.getElementById("note");
const noteError = document.getElementById("note-error");
const toggleExclusionsBtn = document.getElementById("toggle-exclusions");
const exclusionsContent = document.getElementById("exclusions-content");
const exclusionsSummary = document.getElementById("exclusions-summary");
const adminModal = document.getElementById("admin-modal");
const adminModalText = document.getElementById("admin-modal-text");
const adminModalAssign = document.getElementById("admin-modal-assign");
const adminModalChoose = document.getElementById("admin-modal-choose");
const themeKey = "sorty-theme";
const initialTheme = localStorage.getItem(themeKey) || "light";
const deadlinePattern = /^(\d{1,2})[/-](\d{1,2})([/-](\d{2,4}))?$/;
let exclusionsOpen = false;
let adminResolve = null;
setTheme(initialTheme);

function uid() {
  return "p-" + Math.random().toString(16).slice(2, 10);
}

function showToast(message, type = "info") {
  toast.textContent = message;
  toast.className = `toast show ${type === "error" ? "error" : type === "success" ? "success" : ""}`;
  setTimeout(() => {
    toast.classList.remove("show");
  }, 2600);
}

function setSending(active, { message = "", sub = "", status = "sending" } = {}) {
  state.isSending = active;
  if (sendSpinner && status === "sending") {
    sendSpinner.classList.remove("hidden");
  }
  if (sendCheck && status !== "success") {
    sendCheck.classList.add("hidden");
  }
  if (sendBtn) {
    sendBtn.disabled = active;
    sendBtn.classList.toggle("loading", active);
    sendBtn.setAttribute("aria-busy", active ? "true" : "false");
  }
  if (sendOverlay) {
    sendOverlay.classList.toggle("hidden", !active);
  }
  if (sendSpinner) {
    sendSpinner.classList.toggle("hidden", status !== "sending");
  }
  if (sendCheck) {
    sendCheck.classList.toggle("hidden", status !== "success");
  }
  if (sendOverlayOk) {
    if (status === "success") {
      sendOverlayOk.classList.remove("hidden");
    } else {
      sendOverlayOk.classList.add("hidden");
    }
  }
  if (sendOverlayText && message) {
    sendOverlayText.textContent = message;
  }
  if (sendOverlaySub) {
    sendOverlaySub.textContent = sub || (status === "sending" ? "Esto puede tardar algunos segundos." : "");
  }

  if (!active) {
    if (sendSpinner) sendSpinner.classList.remove("hidden");
    if (sendCheck) sendCheck.classList.add("hidden");
  }
}

function focusName() {
  // Evitar abrir teclado en mobile: solo enfocar en escritorio/puntero fino.
  const isDesktop = window.matchMedia("(pointer:fine)").matches && window.innerWidth > 900;
  if (!isDesktop || !nameInput) return;
  nameInput.focus();
  nameInput.select();
}

function createTableButton(text, classNames, ariaLabel, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `table-btn ${classNames}`.trim();
  btn.textContent = text;
  if (ariaLabel) btn.setAttribute("aria-label", ariaLabel);
  btn.addEventListener("click", onClick);
  return btn;
}

function updateExclusionsSummary() {
  if (!exclusionsSummary) return;
  const count = state.exclusions.length;
  const hasItems = count > 0;
  exclusionsSummary.textContent = hasItems
    ? `${count} exclusion${count === 1 ? "" : "es"} configurada${count === 1 ? "" : "s"}.`
    : "";
  exclusionsSummary.classList.toggle("hidden", exclusionsOpen || !hasItems);
}

function setExclusionsOpen(open) {
  exclusionsOpen = open;
  if (exclusionsContent) {
    exclusionsContent.classList.toggle("hidden", !open);
  }
  if (toggleExclusionsBtn) {
    toggleExclusionsBtn.setAttribute("aria-expanded", open ? "true" : "false");
    toggleExclusionsBtn.textContent = open ? "Ocultar exclusiones" : "Configurar exclusiones";
  }
  updateExclusionsSummary();
}

function closeAdminModal() {
  if (adminModal) {
    adminModal.classList.add("hidden");
    adminModal.setAttribute("aria-hidden", "true");
  }
  adminResolve = null;
}

function openAdminModal(first) {
  if (!adminModal || !adminModalText || !adminModalAssign || !adminModalChoose) return Promise.resolve(false);
  adminModalText.textContent = `No seleccionaste Administrador. Se asignara al primero cargado (${first.name}). ¿Continuar?`;
  adminModalAssign.textContent = `Asignar a ${first.name}`;
  adminModal.classList.remove("hidden");
  adminModal.setAttribute("aria-hidden", "false");
  adminModalChoose.focus();
  return new Promise((resolve) => {
    adminResolve = resolve;
  });
}

function renderParticipants() {
  const body = document.getElementById("participants-body");
  const empty = document.getElementById("participants-empty");
  const table = document.getElementById("participants-table");

  body.innerHTML = "";
  if (!state.participants.length) {
    empty.classList.remove("hidden");
    table.classList.add("hidden");
    syncAdminCheckbox();
    return;
  }

  empty.classList.add("hidden");
  table.classList.remove("hidden");

  state.participants.forEach((p) => {
    const tr = document.createElement("tr");

    const adminCell = document.createElement("td");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "admin";
    radio.checked = p.is_admin;
    radio.addEventListener("change", () => setAdmin(p.id));
    adminCell.appendChild(radio);

    const nameCell = document.createElement("td");
    nameCell.className = "name-cell";
    const infoWrap = document.createElement("div");
    infoWrap.className = "participant-info";
    const nameLine = document.createElement("div");
    nameLine.className = "participant-name";
    nameLine.textContent = p.name;
    const emailMobile = document.createElement("div");
    emailMobile.className = "participant-email participant-email-mobile";
    emailMobile.textContent = p.email;
    infoWrap.appendChild(nameLine);
    infoWrap.appendChild(emailMobile);
    nameCell.appendChild(infoWrap);

    const emailCell = document.createElement("td");
    emailCell.className = "email-cell";
    emailCell.textContent = p.email;

    const actionCell = document.createElement("td");
    actionCell.classList.add("table-actions");
    const editBtn = createTableButton("Editar", "ghost", null, () => startEdit(p.id));
    const removeBtn = createTableButton("Eliminar", "danger", null, () => removeParticipant(p.id));
    const editIconBtn = createTableButton("✎", "icon ghost", `Editar ${p.name}`, () => startEdit(p.id));
    const removeIconBtn = createTableButton("🗑", "icon danger", `Eliminar ${p.name}`, () =>
      removeParticipant(p.id)
    );
    [editBtn, removeBtn, editIconBtn, removeIconBtn].forEach((btn) => actionCell.appendChild(btn));

    tr.appendChild(adminCell);
    tr.appendChild(nameCell);
    tr.appendChild(emailCell);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  });

  renderExclusionSelects();
  syncAdminCheckbox();
}

function setAdmin(id) {
  state.participants = state.participants.map((p) => ({ ...p, is_admin: p.id === id }));
  renderParticipants();
}

function removeParticipant(id) {
  state.participants = state.participants.filter((p) => p.id !== id);
  state.exclusions = state.exclusions.filter((ex) => ex.from !== id && ex.to !== id);
  if (state.editingId === id) {
    resetForm();
  }

  renderParticipants();
  renderExclusions();
}

function renderMode() {
  // Modo fijo SMTP en despliegue Vercel.
}

if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    const next = document.body.classList.contains("theme-dark") ? "light" : "dark";
    setTheme(next);
    showToast(next === "dark" ? "Modo oscuro activado." : "Modo claro activado.", "success");
  });
}

participantForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const name = (nameInput?.value || "").trim();
  const email = (emailInput?.value || "").trim().toLowerCase();
  const isEditing = Boolean(state.editingId);
  const editingTarget = state.participants.find((p) => p.id === state.editingId);
  const hasOtherAdmin = state.participants.some((p) => p.is_admin && p.id !== state.editingId);
  const canBeAdmin = !hasOtherAdmin;
  const isAdmin = canBeAdmin && adminCheckbox.checked;

  if (!name || !email) {
    showToast("Completa nombre y email.", "error");
    return;
  }
  const duplicate = state.participants.find(
    (p) => p.email === email && (!isEditing || p.id !== state.editingId)
  );
  if (duplicate) {
    showToast("Ese email ya esta en la lista.", "error");
    return;
  }

  if (isEditing && editingTarget) {
    state.participants = state.participants.map((p) =>
      p.id === state.editingId
        ? { ...p, name, email, is_admin: isAdmin }
        : { ...p, is_admin: isAdmin ? false : p.is_admin }
    );
  } else {
    const participant = {
      id: uid(),
      name,
      email,
      is_admin: isAdmin,
    };
    if (isAdmin) {
      state.participants = state.participants.map((p) => ({ ...p, is_admin: false }));
    }
    state.participants.push(participant);
  }

  resetForm();
  renderParticipants();
});

exclusionForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const from = document.getElementById("ex-from").value;
  const to = document.getElementById("ex-to").value;
  if (!from || !to || from === to) {
    showToast("Elige dos personas distintas.", "error");
    return;
  }
  if (state.exclusions.some((ex) => ex.from === from && ex.to === to)) {
    showToast("Ya cargaste esa exclusion.", "error");
    return;
  }
  state.exclusions.push({ from, to });
  renderExclusions();
});

function renderExclusionSelects() {
  const fromSel = document.getElementById("ex-from");
  const toSel = document.getElementById("ex-to");
  [fromSel, toSel].forEach((sel) => {
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Selecciona";
    placeholder.disabled = false;
    placeholder.selected = true;
    sel.appendChild(placeholder);
  });

  state.participants.forEach((p) => {
    const optFrom = document.createElement("option");
    optFrom.value = p.id;
    optFrom.textContent = p.name;
    fromSel.appendChild(optFrom);

    const optTo = document.createElement("option");
    optTo.value = p.id;
    optTo.textContent = p.name;
    toSel.appendChild(optTo);
  });
  fromSel.value = "";
  toSel.value = "";
}

function renderExclusions() {
  const wrap = document.getElementById("exclusions-list");
  const empty = document.getElementById("exclusions-empty");
  const alert = document.getElementById("exclusions-alert");
  wrap.innerHTML = "";

  if (!state.exclusions.length) {
    empty.classList.remove("hidden");
    updateExclusionsSummary();
    if (alert) alert.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");

  state.exclusions.forEach((ex, idx) => {
    const giver = state.participants.find((p) => p.id === ex.from);
    const receiver = state.participants.find((p) => p.id === ex.to);
    if (!giver || !receiver) return;
    const chip = document.createElement("div");
    const isProblem = state.exclusionIssue && state.exclusionIssue.id === ex.from;
    chip.className = `chip${isProblem ? " error" : ""}`;
    chip.textContent = `${giver.name} no regala a ${receiver.name}`;
    const close = document.createElement("button");
    close.className = "remove";
    close.type = "button";
    close.textContent = "x";
    close.addEventListener("click", () => {
      state.exclusions.splice(idx, 1);
      renderExclusions();
    });
    chip.appendChild(close);
    wrap.appendChild(chip);
  });

  if (state.exclusions.length && !exclusionsOpen) {
    setExclusionsOpen(true);
  } else {
    updateExclusionsSummary();
  }

  if (alert) {
    if (state.exclusionIssue) {
      alert.textContent = `${state.exclusionIssue.name} no tiene receptor con estas exclusiones. Ajusta aqui.`;
      alert.classList.remove("hidden");
    } else {
      alert.classList.add("hidden");
    }
  }
}

document.getElementById("budget").addEventListener("input", (e) => {
  state.meta.budget = e.target.value.trim();
});
document.getElementById("deadline").addEventListener("input", (e) => {
  state.meta.deadline = e.target.value.trim();
});
document.getElementById("note").addEventListener("input", (e) => {
  state.meta.note = e.target.value.trim();
  clearNoteError();
});

function parseDeadline(value) {
  const raw = (value || "").trim();
  if (!raw) return { ok: true, date: null };
  const match = deadlinePattern.exec(raw);
  if (!match) return { ok: false, error: "Usa formato dd/mm o dd/mm/aaaa." };

  const day = Number(match[1]);
  const month = Number(match[2]);
  const yearPart = match[4];
  let year = yearPart ? Number(yearPart) : null;
  if (year && year < 100) {
    year += 2000;
  }

  const today = new Date();
  today.setHours(0, 0, 0, 0);

  let targetYear = year ?? today.getFullYear();
  let date = new Date(targetYear, month - 1, day);

  const isValidDate =
    !Number.isNaN(date.getTime()) &&
    date.getDate() === day &&
    date.getMonth() === month - 1 &&
    date.getFullYear() === targetYear;
  if (!isValidDate) {
    return { ok: false, error: "La fecha limite no es valida." };
  }

  if (!year && date < today) {
    date = new Date(targetYear + 1, month - 1, day);
  }

  if (date < today) {
    return { ok: false, error: "La fecha limite no puede ser pasada." };
  }

  return { ok: true, date };
}

function clearNoteError() {
  if (noteError) {
    noteError.textContent = "";
    noteError.classList.add("hidden");
  }
  if (noteInput) {
    noteInput.classList.remove("input-error");
  }
}

function showNoteError(message) {
  if (noteError) {
    noteError.textContent = message;
    noteError.classList.remove("hidden");
  }
  if (noteInput) {
    noteInput.classList.add("input-error");
    noteInput.focus({ preventScroll: true });
    noteInput.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function validateMeta() {
  state.meta.budget = (state.meta.budget || "").trim();
  state.meta.deadline = (state.meta.deadline || "").trim();
  state.meta.note = (state.meta.note || "").trim();

  clearNoteError();

  if (!state.meta.note) {
    return { ok: false, field: "note", message: "La nota es obligatoria." };
  }

  const deadlineCheck = parseDeadline(state.meta.deadline);
  if (!deadlineCheck.ok) {
    return { ok: false, field: "deadline", message: deadlineCheck.error };
  }

  return { ok: true };
}

function focusAdminRadio() {
  const firstRadio = document.querySelector("#participants-body input[name='admin']");
  if (firstRadio) {
    firstRadio.focus();
  }
}

function ensureAdminBeforeDraw() {
  const adminCount = state.participants.filter((p) => p.is_admin).length;
  if (adminCount === 1) return Promise.resolve(true);
  if (!state.participants.length) return Promise.resolve(false);
  const first = state.participants[0];
  if (!first) return Promise.resolve(false);
  return openAdminModal(first);
}

function localFeasibilityCheck() {
  state.exclusionIssue = null;
  if (state.participants.length < 2) {
    return "Carga al menos dos personas.";
  }
  const adminCount = state.participants.filter((p) => p.is_admin).length;
  if (adminCount !== 1) {
    return "Debe haber exactamente un Administrador.";
  }

  const ids = state.participants.map((p) => p.id);
  const bans = {};
  ids.forEach((id) => (bans[id] = new Set([id])));
  state.exclusions.forEach((ex) => {
    bans[ex.from]?.add(ex.to);
  });

  for (const id of ids) {
    const allowed = ids.filter((other) => !bans[id].has(other));
    if (!allowed.length) {
      const p = state.participants.find((x) => x.id === id);
      if (p) {
        state.exclusionIssue = { id: p.id, name: p.name };
        return `${p.name} no tiene receptor con estas exclusiones. Ajusta exclusiones o agrega participantes.`;
      }
      return "Alguien no tiene ningun receptor posible.";
    }
  }
  return null;
}

async function submitDraw(send) {
  if (state.isSending) {
    showToast("Ya estamos enviando. Espera un momento.", "info");
    return;
  }

  if (state.participants.length < 2) {
    showToast("Carga al menos dos personas.", "error");
    return;
  }

  const hasAdmin = await ensureAdminBeforeDraw();
  if (!hasAdmin) {
    return;
  }

  const metaCheck = validateMeta();
  if (!metaCheck.ok) {
    if (metaCheck.field === "note") {
      showNoteError(metaCheck.message);
    } else {
      showToast(metaCheck.message, "error");
    }
    return;
  }

  const error = localFeasibilityCheck();
  if (error) {
    showToast(error, "error");
    if (state.exclusionIssue) {
      setExclusionsOpen(true);
      document.getElementById("exclusions")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    return;
  }

  const payload = {
    participants: state.participants.map((p) => ({
      name: p.name,
      email: p.email,
      is_admin: p.is_admin,
    })),
    exclusions: state.exclusions.map((ex) => ({
      from: state.participants.find((p) => p.id === ex.from)?.email,
      to: state.participants.find((p) => p.id === ex.to)?.email,
    })),
    meta: state.meta,
    mode: state.emailMode,
    send,
  };

  setSending(true, { message: send ? "Enviando correos..." : "Preparando simulacion..." });
  let showSuccessOverlay = false;
  let count = null;
  try {
    const res = await fetch("/api/draw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Error en el sorteo.");
    count = data.email_status?.emails;
    if (data.email_status?.mode === "smtp" || send) {
      showSuccessOverlay = true;
      setSending(true, {
        message: "Correos enviados",
        sub: count ? `Enviados ${count} correos.` : "",
        status: "success",
      });
      showToast("Correos enviados.", "success");
      resetAppState();
    } else {
      showToast("Simulacion lista.", "success");
    }
  } catch (err) {
    showToast(err.message || "Error inesperado.", "error");
  } finally {
    if (!showSuccessOverlay) {
      setSending(false);
    }
  }
}

document.getElementById("send").addEventListener("click", () => submitDraw(true));

if (sendOverlayOk) {
  sendOverlayOk.addEventListener("click", () => {
    setSending(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

if (toggleExclusionsBtn) {
  toggleExclusionsBtn.addEventListener("click", () => setExclusionsOpen(!exclusionsOpen));
}
setExclusionsOpen(false);
closeAdminModal();

if (adminModalAssign) {
  adminModalAssign.addEventListener("click", () => {
    if (!adminResolve) return;
    state.participants = state.participants.map((p, idx) => ({ ...p, is_admin: idx === 0 }));
    renderParticipants();
    showToast(`Se asigno Administrador a ${state.participants[0]?.name || "el primero"}.`, "info");
    adminResolve(true);
    closeAdminModal();
  });
}

if (adminModalChoose) {
  adminModalChoose.addEventListener("click", () => {
    if (adminResolve) {
      focusAdminRadio();
      adminResolve(false);
    }
    closeAdminModal();
  });
}

renderParticipants();
renderExclusions();
renderTheme();

function syncAdminCheckbox() {
  const hasAdmin = state.participants.some((p) => p.is_admin);
  const editingAdmin = state.editingId
    ? state.participants.find((p) => p.id === state.editingId)?.is_admin
    : false;
  const canShow = !hasAdmin || editingAdmin;
  if (adminWrapper) {
    adminWrapper.classList.toggle("hidden", !canShow);
  }
  if (adminCheckbox) {
    adminCheckbox.disabled = hasAdmin && !editingAdmin;
    if (!canShow) {
      adminCheckbox.checked = false;
    } else if (editingAdmin) {
      adminCheckbox.checked = true;
    }
  }
}

function setTheme(theme) {
  const isDark = theme === "dark";
  document.body.classList.toggle("theme-dark", isDark);
  localStorage.setItem(themeKey, theme);
  renderTheme();
}

function renderTheme() {
  if (themeToggle) {
    // Apariencia del toggle manejada por CSS; sin texto dinamico.
  }
}

function resetMetaInputs() {
  if (budgetInput) budgetInput.value = "";
  if (deadlineInput) deadlineInput.value = "";
  if (noteInput) noteInput.value = "";
}

function resetAppState() {
  state.participants = [];
  state.exclusions = [];
  state.meta = getDefaultMeta();
  state.editingId = null;
  state.exclusionIssue = null;
  resetForm({ skipFocus: true });
  if (exclusionForm) exclusionForm.reset();
  resetMetaInputs();
  renderParticipants();
  renderExclusions();
}

function resetForm(options = {}) {
  const { skipFocus = false } = options;
  participantForm.reset();
  state.editingId = null;
  submitBtn.textContent = "Agregar";
  cancelEditBtn.classList.add("hidden");
  syncAdminCheckbox();
  if (!skipFocus) {
    focusName();
  }
}

cancelEditBtn.addEventListener("click", () => {
  resetForm();
});

function startEdit(id) {
  const target = state.participants.find((p) => p.id === id);
  if (!target) return;
  state.editingId = id;
  if (nameInput) nameInput.value = target.name;
  if (emailInput) emailInput.value = target.email;
  adminCheckbox.checked = target.is_admin;
  submitBtn.textContent = "Guardar cambios";
  cancelEditBtn.classList.remove("hidden");
  syncAdminCheckbox();
  focusName();
}
