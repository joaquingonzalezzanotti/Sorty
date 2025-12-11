const state = {
  participants: [],
  exclusions: [],
  meta: {
    budget: "",
    deadline: "",
    note: "",
  },
  emailMode: (window.initialEmailMode || "smtp").toLowerCase(),
  editingId: null,
};

const participantForm = document.getElementById("participant-form");
const exclusionForm = document.getElementById("exclusion-form");
const toast = document.getElementById("toast");
const modeToggle = document.getElementById("mode-toggle");
const themeToggle = document.getElementById("theme-toggle");
const adminCheckbox = document.getElementById("is-admin");
const adminWrapper = document.getElementById("admin-wrapper");
const cancelEditBtn = document.getElementById("cancel-edit");
const nameInput = document.getElementById("name");
const emailInput = document.getElementById("email");
const submitBtn = participantForm.querySelector("button[type='submit']");
const themeKey = "sorty-theme";
const initialTheme = localStorage.getItem(themeKey) || "light";
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

function focusName() {
  if (nameInput) {
    nameInput.focus();
    nameInput.select();
  }
}

function renderParticipants() {
  const body = document.getElementById("participants-body");
  const empty = document.getElementById("participants-empty");
  const table = document.getElementById("participants-table");
  const hasAdmin = state.participants.some((p) => p.is_admin);

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
    radio.disabled = hasAdmin && !p.is_admin;
    radio.addEventListener("change", () => setAdmin(p.id));
    adminCell.appendChild(radio);

    const nameCell = document.createElement("td");
    nameCell.textContent = p.name;

    const emailCell = document.createElement("td");
    emailCell.textContent = p.email;

    const actionCell = document.createElement("td");
    const removeBtn = document.createElement("button");
    removeBtn.className = "table-btn danger";
    removeBtn.type = "button";
    removeBtn.textContent = "Eliminar";
    removeBtn.addEventListener("click", () => removeParticipant(p.id));
    const editBtn = document.createElement("button");
    editBtn.className = "table-btn ghost";
    editBtn.type = "button";
    editBtn.textContent = "Editar";
    editBtn.addEventListener("click", () => startEdit(p.id));
    actionCell.classList.add("table-actions");
    actionCell.appendChild(editBtn);
    actionCell.appendChild(removeBtn);

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
  const hasAdmin = state.participants.some((p) => p.is_admin);
  const current = state.participants.find((p) => p.id === id);
  if (hasAdmin && current && !current.is_admin) {
    return; // no permitir marcar otro admin si ya existe
  }
  state.participants = state.participants.map((p) => ({ ...p, is_admin: p.id === id }));
  renderParticipants();
}

function removeParticipant(id) {
  state.participants = state.participants.filter((p) => p.id !== id);
  state.exclusions = state.exclusions.filter((ex) => ex.from !== id && ex.to !== id);

  if (!state.participants.some((p) => p.is_admin) && state.participants.length) {
    state.participants[0].is_admin = true;
  }
  if (state.editingId === id) {
    resetForm();
  }

  renderParticipants();
  renderExclusions();
}

function renderMode() {
  const mode = state.emailMode === "smtp" ? "Real (SMTP)" : "Prueba (no envia)";
  modeToggle.textContent = mode;
  modeToggle.classList.toggle("real", state.emailMode === "smtp");
}

modeToggle.addEventListener("click", () => {
  state.emailMode = state.emailMode === "smtp" ? "console" : "smtp";
  renderMode();
  if (state.emailMode === "smtp") {
    showToast("Modo real: requiere SMTP_USER/PASS y host configurados.", "success");
  } else {
    showToast("Modo prueba: solo imprime correos.", "info");
  }
});

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
  const hasAdmin = state.participants.some((p) => p.is_admin);
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
        ? { ...p, name, email, is_admin: isAdmin || p.is_admin }
        : { ...p, is_admin: isAdmin ? false : p.is_admin }
    );
  } else {
    const participant = {
      id: uid(),
      name,
      email,
      is_admin: isAdmin || state.participants.length === 0,
    };
    if (isAdmin) {
      state.participants = state.participants.map((p) => ({ ...p, is_admin: false }));
    }
    state.participants.push(participant);
  }

  if (!state.participants.some((p) => p.is_admin) && state.participants.length) {
    state.participants[0].is_admin = true;
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
  wrap.innerHTML = "";

  if (!state.exclusions.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  state.exclusions.forEach((ex, idx) => {
    const giver = state.participants.find((p) => p.id === ex.from);
    const receiver = state.participants.find((p) => p.id === ex.to);
    if (!giver || !receiver) return;
    const chip = document.createElement("div");
    chip.className = "chip";
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
}

document.getElementById("budget").addEventListener("input", (e) => {
  state.meta.budget = e.target.value.trim();
});
document.getElementById("deadline").addEventListener("input", (e) => {
  state.meta.deadline = e.target.value.trim();
});
document.getElementById("note").addEventListener("input", (e) => {
  state.meta.note = e.target.value.trim();
});

function localFeasibilityCheck() {
  if (state.participants.length < 2) {
    return "Carga al menos dos personas.";
  }
  const adminCount = state.participants.filter((p) => p.is_admin).length;
  if (adminCount !== 1) {
    return "Debe haber exactamente un admin.";
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
      return `${p ? p.name : "Alguien"} no tiene ningun receptor posible.`;
    }
  }
  return null;
}

async function submitDraw(send) {
  const error = localFeasibilityCheck();
  if (error) {
    showToast(error, "error");
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

  try {
    const res = await fetch("/api/draw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Error en el sorteo.");
    if (data.email_status?.mode === "smtp") {
      showToast("Correos enviados.", "success");
    } else if (send) {
      showToast("Modo consola: se imprimen los correos en el servidor.", "success");
    } else {
      showToast("Simulacion lista.", "success");
    }
  } catch (err) {
    showToast(err.message || "Error inesperado.", "error");
  }
}

document.getElementById("send").addEventListener("click", () => submitDraw(true));

renderParticipants();
renderExclusions();
renderMode();
renderTheme();
focusName();

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
    const isDark = document.body.classList.contains("theme-dark");
    themeToggle.textContent = isDark ? "Oscuro" : "Claro";
  }
}

function resetForm() {
  participantForm.reset();
  state.editingId = null;
  submitBtn.textContent = "Agregar";
  cancelEditBtn.classList.add("hidden");
  syncAdminCheckbox();
  focusName();
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
